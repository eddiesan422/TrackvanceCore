"""Local immutable artifact storage and explicit, organization-scoped lineage."""

import hashlib
import os
import re
import shutil
from pathlib import Path
from typing import BinaryIO, Protocol, runtime_checkable
from uuid import NAMESPACE_URL, uuid5

from sqlalchemy import select
from sqlalchemy.orm import Session

from .config import STORAGE_DIR
from .models import Artifact, ArtifactLink, uid


class ArtifactIntegrityError(ValueError):
    pass


@runtime_checkable
class StorageProvider(Protocol):
    """Internal immutable storage port.

    Domain/application services depend on this contract rather than on the
    Docker-volume layout. ``FileArtifactStore`` is the local adapter. A future
    object-storage adapter may materialize an object in a bounded local cache
    for Polars and persist a remote locator in ``Artifact.path``; migrating
    historical local locators remains an infrastructure concern.
    """

    def temporary_path(self, suffix: str = ".tmp") -> Path: ...

    def materialize(self, artifact: Artifact) -> Path: ...

    def materialize_reference(
        self,
        reference: str,
        *,
        expected_sha256: str | None = None,
        expected_size: int | None = None,
    ) -> Path: ...

    def open_read(self, artifact: Artifact) -> BinaryIO: ...

    def exists(self, artifact: Artifact) -> bool: ...

    def put_file(
        self,
        db: Session,
        source: Path,
        kind: str,
        organization_id: str,
        name: str | None = None,
        artifact_id: str | None = None,
        media_type: str | None = None,
    ) -> Artifact: ...


def file_hash(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def artifact_dto(artifact: Artifact) -> dict:
    return {"artifact_id": artifact.id, "kind": artifact.kind, "name": artifact.name,
            "sha256": artifact.sha256, "size_bytes": artifact.size_bytes,
            "media_type": artifact.media_type}


def link_artifact(db: Session, organization_id: str, relation: str, source_type: str,
                  source_id: str, target_type: str, target_id: str) -> ArtifactLink:
    values = {"organization_id": organization_id, "relation": relation, "source_type": source_type,
              "source_id": source_id, "target_type": target_type, "target_id": target_id}
    found = db.scalar(select(ArtifactLink).filter_by(**values))
    if found:
        return found
    found = ArtifactLink(**values)
    db.add(found)
    db.flush()
    return found


class FileArtifactStore:
    """Files are promoted with no replacement; metadata registration is idempotent.

    Files staged by a lost worker can never replace a completed worker's bytes.
    If a transaction rolls back after promotion, a later retry can reuse identical bytes.
    """

    def __init__(self, root: Path = STORAGE_DIR):
        self.root = root.resolve()

    def location(self, *parts: str) -> Path:
        """Resolve a provider-owned location and reject traversal/root access."""
        if not parts:
            raise ArtifactIntegrityError("La ubicación de almacenamiento está vacía.")
        return self.checked_path(self.root.joinpath(*parts))

    def create_directory(self, *parts: str, exist_ok: bool = False) -> Path:
        directory = self.location(*parts)
        directory.mkdir(parents=True, exist_ok=exist_ok)
        return directory

    def temporary_path(self, suffix: str = ".tmp") -> Path:
        """Allocate a private local work path without exposing the storage root.

        Callers own cleanup.  A future object-storage adapter can return a path
        in its bounded local staging cache while keeping the persisted artifact
        reference remote.
        """
        if not re.fullmatch(r"\.[A-Za-z0-9._-]{1,20}", suffix):
            raise ValueError("Extensión temporal inválida.")
        folder = self.root / "tmp"
        folder.mkdir(parents=True, exist_ok=True)
        return self.checked_path(folder / f"{uid()}{suffix}")

    def checked_path(self, path: str | Path) -> Path:
        resolved = Path(path).resolve()
        if not resolved.is_relative_to(self.root) or resolved == self.root:
            raise ArtifactIntegrityError("La ruta del artefacto queda fuera del almacenamiento local.")
        return resolved

    def verify(self, artifact: Artifact) -> Path:
        return self.materialize(artifact)

    def materialize(self, artifact: Artifact) -> Path:
        path = self.materialize_reference(
            artifact.path,
            expected_sha256=artifact.sha256,
            expected_size=artifact.size_bytes,
        )
        return path

    def materialize_reference(
        self,
        reference: str,
        *,
        expected_sha256: str | None = None,
        expected_size: int | None = None,
    ) -> Path:
        path = self.checked_path(reference)
        if (
            not path.is_file()
            or (expected_size is not None and path.stat().st_size != expected_size)
            or (expected_sha256 is not None and file_hash(path) != expected_sha256)
        ):
            raise ArtifactIntegrityError("ARTIFACT_HASH_MISMATCH: La integridad del artefacto no coincide con su SHA-256.")
        return path

    def open_read(self, artifact: Artifact) -> BinaryIO:
        return self.verify(artifact).open("rb")

    def exists(self, artifact: Artifact) -> bool:
        return self.checked_path(artifact.path).is_file()

    @staticmethod
    def _media_type(path: Path) -> str:
        return {".parquet": "application/vnd.apache.parquet", ".pq": "application/vnd.apache.parquet", ".csv": "text/csv",
                ".json": "application/json", ".jsonl": "application/x-ndjson",
                ".ndjson": "application/x-ndjson", ".txt": "text/plain",
                ".tsv": "text/tab-separated-values",
                ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"}.get(path.suffix.lower(), "application/octet-stream")

    def register_existing(self, db: Session, path: Path, kind: str, organization_id: str,
                          name: str | None = None, artifact_id: str | None = None,
                          expected_sha256: str | None = None,
                          media_type: str | None = None) -> Artifact:
        path = self.checked_path(path)
        digest = file_hash(path)
        if expected_sha256 and digest != expected_sha256:
            raise ArtifactIntegrityError("ARTIFACT_HASH_MISMATCH: El hash histórico no coincide con el archivo.")
        previous = db.scalar(select(Artifact).where(Artifact.path == str(path)))
        if previous:
            if previous.organization_id != organization_id or previous.sha256 != digest:
                raise ArtifactIntegrityError("Un artefacto registrado es inmutable y pertenece a una organización.")
            return previous
        identity = artifact_id or str(uuid5(NAMESPACE_URL, f"trackvance:artifact:{organization_id}:{path.relative_to(self.root).as_posix()}"))
        artifact = Artifact(id=identity, organization_id=organization_id, kind=kind,
                            name=(name or path.name)[:240], path=str(path), sha256=digest,
                            size_bytes=path.stat().st_size,
                            media_type=media_type or self._media_type(path))
        db.add(artifact)
        db.flush()
        return artifact

    def promote(self, temporary: Path, destination: Path) -> Path:
        """Publish complete bytes without overwriting a historical artifact."""
        temporary, destination = self.checked_path(temporary), self.checked_path(destination)
        destination.parent.mkdir(parents=True, exist_ok=True)
        try:
            os.link(temporary, destination)
        except FileExistsError:
            if file_hash(temporary) != file_hash(destination):
                raise ArtifactIntegrityError("ARTIFACT_IMMUTABLE: Un resultado histórico no puede sobrescribirse.") from None
        finally:
            temporary.unlink(missing_ok=True)
        return destination

    def put_file(self, db: Session, source: Path, kind: str, organization_id: str,
                 name: str | None = None, artifact_id: str | None = None,
                 media_type: str | None = None) -> Artifact:
        identity = artifact_id or uid()
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", identity):
            raise ValueError("Identidad de artefacto inválida.")
        extension = source.suffix.lower()
        if not re.fullmatch(r"\.[a-z0-9]{1,10}", extension):
            extension = ".bin"
        folder = self.root / "artifacts" / identity
        folder.mkdir(parents=True, exist_ok=True)
        temporary, destination = folder / f"{uid()}.tmp", folder / f"data{extension}"
        with source.open("rb") as incoming, temporary.open("xb") as outgoing:
            shutil.copyfileobj(incoming, outgoing, length=1024 * 1024)
            outgoing.flush()
            os.fsync(outgoing.fileno())
        self.promote(temporary, destination)
        return self.register_existing(
            db, destination, kind, organization_id, name, identity, media_type=media_type
        )


# Application code depends on this port.  The concrete alias remains available
# only for local-infrastructure compatibility and legacy backfill operations.
artifact_store = FileArtifactStore()
storage_provider: StorageProvider = artifact_store
