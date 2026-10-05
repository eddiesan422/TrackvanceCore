"""Local immutable artifact storage and explicit, organization-scoped lineage."""

import hashlib
import json
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

    def dataset_paths(self, artifact: Artifact) -> list[Path]: ...

    def put_dataset(self, db: Session, parts: list[Path], kind: str,
                    organization_id: str, *, name: str = "canonical.dataset.json",
                    artifact_id: str | None = None, metadata: dict | None = None,
                    temporary_parent: Path | None = None) -> Artifact: ...

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

    def dataset_descriptor(self, artifact: Artifact) -> dict | None:
        """Descriptors contain portable provider-relative locators, never host roots.

        Historical single-file Parquet artifacts retain their bytes and contract.
        Publication records every part independently, so ordinary inventory and
        backups continue to verify all referenced bytes.
        """
        if artifact.media_type != "application/vnd.trackvance.parquet-set+json":
            return None
        path = self.materialize(artifact)
        if path.stat().st_size > 16 * 1024 * 1024:
            raise ArtifactIntegrityError("DATASET_DESCRIPTOR_INVALID: Descriptor demasiado grande.")
        try:
            descriptor = json.loads(path.read_text(encoding="utf-8"))
            parts = descriptor["parts"]
            if (descriptor["schema_version"] != 1 or descriptor["kind"] != "PARQUET_DATASET"
                    or not isinstance(parts, list) or not 1 <= len(parts) <= 100_000
                    or not isinstance(descriptor["schema"], list)
                    or type(descriptor["row_count"]) is not int or descriptor["row_count"] < 0):
                raise ValueError()
            seen = set()
            total_rows = total_bytes = 0
            for ordinal, part in enumerate(parts):
                reference = part["path"]
                if (not isinstance(reference, str) or Path(reference).is_absolute()
                        or "\\" in reference or ".." in Path(reference).parts
                        or reference in seen or part["ordinal"] != ordinal
                        or type(part["row_count"]) is not int or part["row_count"] < 0
                        or type(part["size_bytes"]) is not int or part["size_bytes"] < 0
                        or not re.fullmatch(r"[0-9a-f]{64}", part["sha256"])):
                    raise ValueError()
                seen.add(reference)
                total_rows += part["row_count"]
                total_bytes += part["size_bytes"]
            if total_rows != descriptor["row_count"] or total_bytes != descriptor["data_size_bytes"]:
                raise ValueError()
        except (KeyError, TypeError, ValueError, json.JSONDecodeError):
            raise ArtifactIntegrityError("DATASET_DESCRIPTOR_INVALID: El descriptor del dataset no es válido.") from None
        return descriptor

    def dataset_paths(self, artifact: Artifact) -> list[Path]:
        descriptor = self.dataset_descriptor(artifact)
        if descriptor is None:
            return [self.materialize(artifact)]
        import polars as pl
        import pyarrow.parquet as pq  # type: ignore[import-untyped]

        expected_schema = descriptor["schema"]
        paths = []
        try:
            for part in descriptor["parts"]:
                path = self.materialize_reference(str(self.root / part["path"]),
                                                  expected_sha256=part["sha256"],
                                                  expected_size=part["size_bytes"])
                schema = [{"name": key, "type": str(value)}
                          for key, value in pl.read_parquet_schema(path).items()]
                with pq.ParquetFile(path, memory_map=False, pre_buffer=False) as parquet:
                    count = parquet.metadata.num_rows
                if schema != expected_schema or count != part["row_count"]:
                    raise ArtifactIntegrityError("DATASET_PART_INVALID: El esquema o conteo de una parte no coincide.")
                paths.append(path)
        except ArtifactIntegrityError:
            raise
        except (pl.exceptions.PolarsError, OSError, ValueError):
            raise ArtifactIntegrityError("DATASET_PART_INVALID: No fue posible verificar una parte Parquet.") from None
        return paths

    def put_dataset(self, db: Session, parts: list[Path], kind: str,
                    organization_id: str, *, name: str = "canonical.dataset.json",
                    artifact_id: str | None = None, metadata: dict | None = None,
                    temporary_parent: Path | None = None) -> Artifact:
        """Publish parts first and a complete verified descriptor last, in one DB tx.

        The caller owns the publication lease and transaction. A rollback can
        leave unreferenced immutable bytes; it cannot expose a DatasetVersion or
        overwrite the result of another attempt.

        A caller with recoverable attempt staging supplies ``temporary_parent``
        so a process crash cannot strand the descriptor in the shared tmp area.
        The local adapter requires an existing directory inside its storage.
        """
        import polars as pl
        import pyarrow.parquet as pq  # type: ignore[import-untyped]

        if not parts or len(parts) > 100_000:
            raise ArtifactIntegrityError("DATASET_DESCRIPTOR_INVALID: Se requiere al menos una parte.")
        temporary_directory = self.checked_path(temporary_parent) if temporary_parent is not None else None
        if temporary_directory is not None and not temporary_directory.is_dir():
            raise ArtifactIntegrityError("DATASET_STAGING_INVALID: El staging debe ser un directorio existente.")
        identity = artifact_id or uid()
        descriptions: list[dict] = []
        schema = None
        try:
            for ordinal, path in enumerate(parts):
                current_schema = [{"name": key, "type": str(value)}
                                  for key, value in pl.read_parquet_schema(path).items()]
                if schema is not None and schema != current_schema:
                    raise ArtifactIntegrityError("DATASET_SCHEMA_MISMATCH: Las partes tienen esquemas distintos.")
                schema = current_schema
                with pq.ParquetFile(path, memory_map=False, pre_buffer=False) as parquet:
                    count = parquet.metadata.num_rows
                part_identity = str(uuid5(NAMESPACE_URL, f"trackvance:dataset:{organization_id}:{identity}:{ordinal}"))
                artifact = self.put_file(db, path, f"{kind}_PART", organization_id,
                                         f"part-{ordinal:06d}.parquet", part_identity,
                                         media_type="application/vnd.apache.parquet")
                descriptions.append({"ordinal": ordinal, "artifact_id": artifact.id,
                                     "path": self.checked_path(artifact.path).relative_to(self.root).as_posix(),
                                     "sha256": artifact.sha256, "size_bytes": artifact.size_bytes,
                                     "row_count": count})
            descriptor = {"schema_version": 1, "kind": "PARQUET_DATASET", "schema": schema,
                          "parts": descriptions, "row_count": sum(p["row_count"] for p in descriptions),
                          "data_size_bytes": sum(p["size_bytes"] for p in descriptions),
                          "metadata": metadata or {}}
            staged = (temporary_directory / f"descriptor-{uid()}.json"
                      if temporary_directory is not None else self.temporary_path(".json"))
            try:
                staged.write_text(json.dumps(descriptor, ensure_ascii=False, sort_keys=True,
                                             separators=(",", ":")), encoding="utf-8")
                result = self.put_file(db, staged, kind, organization_id, name, identity,
                                       media_type="application/vnd.trackvance.parquet-set+json")
                self.dataset_paths(result)
            finally:
                staged.unlink(missing_ok=True)
            for part in descriptions:
                link_artifact(db, organization_id, "DATASET_PART", "ARTIFACT", result.id,
                              "ARTIFACT", part["artifact_id"])
            return result
        except ArtifactIntegrityError:
            raise
        except (pl.exceptions.PolarsError, OSError, ValueError):
            raise ArtifactIntegrityError("DATASET_PART_INVALID: No fue posible publicar el conjunto Parquet.") from None

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
