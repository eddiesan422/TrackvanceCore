"""Immutable S3 storage port, restricted to explicitly injected local simulations.

No SDK, credential discovery, remote endpoint or global provider switch exists in
0.9.8. The LOCAL singleton remains authoritative for historical artifacts. A
future authorized AWS integration must provide a separately reviewed transport.
"""
from __future__ import annotations

import base64
import hashlib
import os
import re
from pathlib import Path
from urllib.parse import urlsplit

from sqlalchemy.orm import Session

from .artifactstore import ArtifactIntegrityError, FileArtifactStore, file_hash
from .models import Artifact, uid


def require_simulation(client):
    if getattr(client, "trackvance_local_simulation", False) is not True:
        raise ValueError("AWS_REAL_EXECUTION_DISABLED_098")


def error_code(error: Exception) -> str:
    response = getattr(error, "response", {})
    return str(response.get("Error", {}).get("Code", "")) if isinstance(response, dict) else ""


class S3ArtifactStore(FileArtifactStore):
    """One organization and bucket; verified bounded local staging/materialization.

    Injected clients implement the boto S3 put_object/get_object/head_object
    contract with the explicit local-simulation marker. Conditional writes are
    mandatory. Existing bytes cannot be replaced, including after DB rollback.
    Multipart datasets use the same schema validation and lineage as LOCAL.
    """

    def __init__(self, root: Path, *, client, bucket: str, organization_id: str,
                 prefix: str = "trackvance", max_object_bytes: int = 512 * 1024**2,
                 max_cache_bytes: int = 2 * 1024**3):
        require_simulation(client)
        if (not re.fullmatch(r"[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]", bucket)
                or not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", organization_id)
                or not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", prefix)
                or not 1 <= max_object_bytes <= max_cache_bytes <= 8 * 1024**3):
            raise ValueError("S3_SIMULATION_CONFIGURATION_INVALID")
        super().__init__(root)
        self.client, self.bucket, self.organization_id = client, bucket, organization_id
        self.prefix = f"{prefix}/{organization_id}/"
        self.max_object_bytes, self.max_cache_bytes = max_object_bytes, max_cache_bytes
        self.root.mkdir(parents=True, exist_ok=True)

    def _key(self, reference: str) -> str:
        parsed = urlsplit(reference)
        key = parsed.path.removeprefix("/")
        if (parsed.scheme != "s3" or parsed.netloc != self.bucket or parsed.query or parsed.fragment
                or not key.startswith(self.prefix) or "\\" in key or "%" in key
                or any(part in {"", ".", ".."} for part in key.split("/"))):
            raise ArtifactIntegrityError("S3_ORGANIZATION_REFERENCE_INVALID")
        return key

    def _organization(self, artifact: Artifact):
        if artifact.organization_id != self.organization_id:
            raise ArtifactIntegrityError("S3_ORGANIZATION_MISMATCH")
        self._key(artifact.path)

    def _budget(self, extra: int):
        current = sum(p.stat().st_size for p in self.root.rglob("*") if p.is_file())
        if current + extra > self.max_cache_bytes:
            raise ArtifactIntegrityError("S3_LOCAL_CACHE_CAPACITY_EXCEEDED")

    def materialize(self, artifact: Artifact) -> Path:
        self._organization(artifact)
        return self.materialize_reference(artifact.path, expected_sha256=artifact.sha256,
                                          expected_size=artifact.size_bytes)

    def materialize_reference(self, reference: str, *, expected_sha256: str | None = None,
                              expected_size: int | None = None) -> Path:
        key = self._key(reference)
        if (expected_sha256 is None or not re.fullmatch(r"[a-f0-9]{64}", expected_sha256)
                or type(expected_size) is not int or not 0 <= expected_size <= self.max_object_bytes):
            raise ArtifactIntegrityError("S3_EXPECTED_INTEGRITY_REQUIRED")
        target = self.location("cache", hashlib.sha256(reference.encode()).hexdigest(), expected_sha256)
        if target.exists():
            if target.stat().st_size == expected_size and file_hash(target) == expected_sha256:
                return target
            raise ArtifactIntegrityError("ARTIFACT_HASH_MISMATCH")
        self._budget(expected_size)
        require_simulation(self.client)
        response = self.client.get_object(Bucket=self.bucket, Key=key, ChecksumMode="ENABLED")
        body = response["Body"]
        temporary = self.temporary_path()
        try:
            self._verify_metadata(response, expected_sha256, expected_size)
            digest, size = hashlib.sha256(), 0
            with temporary.open("xb") as stream:
                while block := body.read(min(1024 * 1024, expected_size - size + 1)):
                    size += len(block)
                    if size > expected_size:
                        raise ArtifactIntegrityError("ARTIFACT_SIZE_MISMATCH")
                    digest.update(block)
                    stream.write(block)
                stream.flush()
                os.fsync(stream.fileno())
            if size != expected_size or digest.hexdigest() != expected_sha256:
                raise ArtifactIntegrityError("ARTIFACT_HASH_MISMATCH")
            self.promote(temporary, target)
            return target
        finally:
            body.close()
            temporary.unlink(missing_ok=True)

    def _verify_metadata(self, response: dict, digest: str, size: int):
        metadata = response.get("Metadata", {})
        if (response.get("ContentLength") != size or metadata.get("sha256") != digest
                or metadata.get("organization_id") != self.organization_id):
            raise ArtifactIntegrityError("S3_OBJECT_METADATA_MISMATCH")
        checksum = response.get("ChecksumSHA256")
        if checksum and checksum != base64.b64encode(bytes.fromhex(digest)).decode():
            raise ArtifactIntegrityError("ARTIFACT_HASH_MISMATCH")

    def exists(self, artifact: Artifact) -> bool:
        self._organization(artifact)
        require_simulation(self.client)
        try:
            response = self.client.head_object(Bucket=self.bucket, Key=self._key(artifact.path))
            self._verify_metadata(response, artifact.sha256, artifact.size_bytes)
            return True
        except Exception as error:
            if error_code(error) in {"404", "NoSuchKey", "NotFound"}:
                return False
            raise

    def _part_reference(self, artifact: Artifact) -> str:
        self._organization(artifact)
        return self._key(artifact.path).removeprefix(self.prefix)

    def _materialize_part(self, part: dict) -> Path:
        return self.materialize_reference(f"s3://{self.bucket}/{self.prefix}{part['path']}",
            expected_sha256=part["sha256"], expected_size=part["size_bytes"])

    def put_file(self, db: Session, source: Path, kind: str, organization_id: str,
                 name: str | None = None, artifact_id: str | None = None,
                 media_type: str | None = None) -> Artifact:
        if organization_id != self.organization_id:
            raise ArtifactIntegrityError("S3_ORGANIZATION_MISMATCH")
        identity = artifact_id or uid()
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", identity):
            raise ValueError("ARTIFACT_ID_INVALID")
        size = source.stat().st_size
        if size > self.max_object_bytes:
            raise ArtifactIntegrityError("S3_OBJECT_CAPACITY_EXCEEDED")
        self._budget(size)
        extension = source.suffix.lower()
        if not re.fullmatch(r"\.[a-z0-9]{1,10}", extension):
            extension = ".bin"
        key = f"{self.prefix}artifacts/{identity}/data{extension}"
        locator = f"s3://{self.bucket}/{key}"
        staged = self.temporary_path()
        try:
            with source.open("rb") as incoming, staged.open("xb") as outgoing:
                copied = 0
                while block := incoming.read(1024 * 1024):
                    copied += len(block)
                    if copied > self.max_object_bytes:
                        raise ArtifactIntegrityError("S3_OBJECT_CAPACITY_EXCEEDED")
                    outgoing.write(block)
                outgoing.flush()
                os.fsync(outgoing.fileno())
            digest, size = file_hash(staged), staged.stat().st_size
            previous = db.get(Artifact, identity)
            if previous is not None:
                if (previous.organization_id != organization_id or previous.path != locator
                        or previous.sha256 != digest or previous.size_bytes != size or previous.kind != kind):
                    raise ArtifactIntegrityError("ARTIFACT_IMMUTABLE")
                self.materialize(previous)
                return previous
            require_simulation(self.client)
            try:
                with staged.open("rb") as stream:
                    self.client.put_object(Bucket=self.bucket, Key=key, Body=stream,
                        ContentLength=size, ContentType=media_type or self._media_type(source),
                        Metadata={"sha256": digest, "organization_id": organization_id},
                        ChecksumSHA256=base64.b64encode(bytes.fromhex(digest)).decode(), IfNoneMatch="*")
            except Exception as error:
                if error_code(error) not in {"PreconditionFailed", "412", "ConditionalRequestConflict", "409"}:
                    raise
                # A lost transaction can reuse only identical remotely verified bytes.
                self.materialize_reference(locator, expected_sha256=digest, expected_size=size)
            artifact = Artifact(id=identity, organization_id=organization_id, kind=kind,
                name=(name or source.name)[:240], path=locator, sha256=digest, size_bytes=size,
                media_type=media_type or self._media_type(source))
            self.materialize(artifact)
            db.add(artifact)
            db.flush()
            return artifact
        finally:
            staged.unlink(missing_ok=True)
