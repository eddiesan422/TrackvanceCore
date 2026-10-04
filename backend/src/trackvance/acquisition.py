"""Persisted asynchronous acquisition, pinned source identity and fenced publication."""

from __future__ import annotations

import hashlib
import json
import logging
import os
import shutil
import time
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import cast

import duckdb
import polars as pl
from sqlalchemy import and_, or_, select, update
from sqlalchemy.engine import CursorResult
from sqlalchemy.orm import Session

from .acquisition_config import AcquisitionLimits
from .acquisition_errors import AcquisitionReadError, limit_error, processing_diagnostic
from .acquisition_models import AcquisitionRun, AcquisitionUpload
from .artifactstore import ArtifactIntegrityError, artifact_store, link_artifact
from .audit_context import Actor
from .batch_readers import RECORD_NUMBER_COLUMN, DatasetBatch, FileBatchReader, inspect_file
from .connections_service import (
    ConnectionOperationError,
    current_version,
    saved_source,
    settings_for,
)
from .dataset_readers import DatasetReadResult, ReaderOptions
from .dataset_scans import profile_paths, publish_materialized_version
from .dataset_sources import SourceError, source_registry
from .db import SessionLocal, iso, utcnow
from .jobqueue import job_queue
from .models import (
    Artifact,
    Dataset,
    DatasetSourceBinding,
    DatasetVersion,
    ExternalConnection,
    ExternalConnectionVersion,
    Job,
    User,
    uid,
)
from .permissions import effective_permissions
from .processing import ProcessingError, validate_column_overrides
from .services import audit

LEASE_SECONDS = 90
TERMINAL = frozenset({"SUCCESS", "FAILED", "CANCELLED"})
logger = logging.getLogger(__name__)


class AcquisitionOperationError(ConnectionOperationError):
    def __init__(self, status: int, code: str, message: str, details: dict | None = None):
        super().__init__(status, code, message)
        self.details = details


class AcquisitionStopped(Exception):
    def __init__(self, code: str, message: str):
        self.code, self.message = code, message
        super().__init__(message)


def upload_dto(upload: AcquisitionUpload) -> dict:
    return {"id": upload.id, "filename": upload.filename, "size_bytes": upload.size_bytes,
            "sha256": upload.sha256, "format": upload.source_format, "status": upload.status,
            "expires_at": iso(upload.expires_at), "transfer_complete": True}


def acquisition_dto(run: AcquisitionRun) -> dict:
    end = run.finished_at or utcnow()
    duration = (end.replace(tzinfo=UTC) - run.started_at.replace(tzinfo=UTC)).total_seconds() if run.started_at else None
    source_format = run.source_type if run.source_type != "UPLOAD" else run.source_snapshot.get("format_variant") or run.source_snapshot.get("source_format") or Path(run.filename).suffix.lstrip(".").upper()
    source_format = {"NDJSON": "JSON_LINES", "JSONL": "JSON_LINES", "PQ": "PARQUET", "TSV": "TXT"}.get(source_format, source_format)
    route_limits = None if source_format == "XLSX" and "xlsx_max_rows" not in run.effective_limits else AcquisitionLimits(**run.effective_limits).describe(source_format,
        header_row=run.source_snapshot.get("physical_header_row_number"))
    error = {"code": run.error_code, "message": run.error_message, "details": run.error_details,
             "reference": run.error_reference} if run.error_code else None
    return {"id": run.id, "dataset_id": run.dataset_id, "source_type": run.source_type,
            "filename": run.filename, "status": run.status, "stage": run.stage,
            "attempt_id": run.attempt_id, "attempts": run.attempts,
            "initiated_by_id": run.initiated_by_id, "initiated_by": run.initiated_by_name,
            "processed_rows": run.processed_rows, "processed_bytes": run.processed_bytes,
            "total_rows": run.total_rows, "total_bytes": run.total_bytes,
            "duration_seconds": duration, "cancel_requested": run.cancel_requested,
            "output_version_id": run.output_version_id, "error_code": run.error_code,
            "error_message": run.error_message, "created_at": iso(run.created_at),
            "started_at": iso(run.started_at), "finished_at": iso(run.finished_at),
            "source_snapshot": run.source_snapshot, "reader_options": run.reader_options,
            "column_overrides": run.column_overrides, "effective_limits": run.effective_limits,
            "route_limits": route_limits, "error": error,
            "received_bytes": run.total_bytes, "materialized_rows": run.processed_rows,
            "materialized_bytes": run.processed_bytes,
            "published_rows": run.processed_rows if run.status == "SUCCESS" and run.output_version_id else None}


def owned_acquisition(db: Session, identifier: str, user: User, *, lock=False) -> AcquisitionRun:
    query = select(AcquisitionRun).where(AcquisitionRun.id == identifier, AcquisitionRun.organization_id == user.organization_id)
    run = db.scalar(query.with_for_update() if lock else query)
    if run is None:
        raise AcquisitionOperationError(404, "NOT_FOUND", "No se encontró la adquisición solicitada.")
    return run


def _request_hash(value: dict) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()).hexdigest()


def _idempotent(db: Session, user: User, key: str | None, request_hash: str) -> AcquisitionRun | None:
    if key is None:
        return None
    if not key.strip() or len(key) > 128:
        raise AcquisitionOperationError(422, "INVALID_IDEMPOTENCY_KEY", "La clave de solicitud debe tener entre 1 y 128 caracteres.")
    # Serialize this user's key lookup and registration in the same transaction.
    # The unique constraint is the final backstop; PostgreSQL row locking makes
    # concurrent retries observe the first committed registration.
    db.scalar(select(User).where(User.id == user.id).with_for_update())
    existing = db.scalar(select(AcquisitionRun).where(AcquisitionRun.organization_id == user.organization_id,
        AcquisitionRun.initiated_by_id == user.id, AcquisitionRun.idempotency_key == key))
    if existing and existing.request_hash != request_hash:
        raise AcquisitionOperationError(409, "IDEMPOTENCY_CONFLICT", "La clave de solicitud ya corresponde a otra adquisición.")
    return existing


def register_upload(db: Session, user: User, dataset: Dataset, upload_id: str, *,
                    reader_options: dict | None = None, column_overrides: dict | None = None,
                    idempotency_key: str | None = None) -> AcquisitionRun:
    try:
        options = ReaderOptions.from_mapping(reader_options).as_dict()
    except ProcessingError:
        raise AcquisitionReadError("ACQUISITION_READER_OPTIONS_INVALID", "Las opciones de lectura de adquisición no son válidas.") from None
    overrides = column_overrides or {}
    digest = _request_hash({"operation": "UPLOAD", "dataset_id": dataset.id, "upload_id": upload_id,
                            "reader_options": options, "column_overrides": overrides})
    existing = _idempotent(db, user, idempotency_key, digest)
    if existing:
        return existing
    upload = db.scalar(select(AcquisitionUpload).where(AcquisitionUpload.id == upload_id,
        AcquisitionUpload.organization_id == user.organization_id, AcquisitionUpload.user_id == user.id).with_for_update())
    if upload is None:
        raise AcquisitionOperationError(404, "UPLOAD_NOT_FOUND", "No se encontró la transferencia recibida para este usuario.")
    if upload.status != "RECEIVED" or upload.expires_at.replace(tzinfo=UTC) <= utcnow():
        raise AcquisitionOperationError(409, "UPLOAD_UNAVAILABLE", "La transferencia venció o ya se registró; vuelve a recibir el archivo.")
    limits = AcquisitionLimits.configured()
    artifact_store.materialize_reference(upload.path, expected_sha256=upload.sha256, expected_size=upload.size_bytes)
    # Only inspection is synchronous; complete inference/override validation
    # follows in the durable acquisition worker over the full population.
    inspection = inspect_file(Path(upload.path), upload.filename, options, limits)
    # Unknown sample values/headers are not guessed when HTTP reaches its
    # metadata budget. The complete worker profile validates those overrides.
    if inspection["columns"]:
        validate_column_overrides(overrides, [column["name"] for column in inspection["columns"]])
    elif overrides:
        raise AcquisitionReadError("ACQUISITION_INSPECTION_LIMITED", "La inspección acotada no pudo observar el encabezado. Registra sin correcciones de tipos o elige otra hoja.")
    run = AcquisitionRun(id=uid(), organization_id=user.organization_id, dataset_id=dataset.id,
        upload_id=upload.id, source_type="UPLOAD", filename=upload.filename,
        source_snapshot={"upload_id": upload.id, "sha256": upload.sha256, "size_bytes": upload.size_bytes,
                         "source_format": inspection["format"], "format_variant": "JSON_LINES" if inspection["effective_limits"]["format"] == "JSON_LINES" else None,
                         "physical_header_row_number": inspection["effective_limits"]["header_row_number"]},
        reader_options=options, column_overrides=overrides, effective_limits=limits.as_dict(),
        request_hash=digest, idempotency_key=idempotency_key, initiated_by_id=user.id,
        initiated_by_name=user.name, total_bytes=upload.size_bytes)
    db.add(run)
    db.flush()
    upload.status = "REGISTERED"
    job_queue.submit_acquisition(db, run)
    audit(db, "ACQUISITION_REGISTERED", "acquisition", run.id, "Archivo recibido; adquisición registrada en segundo plano",
          user.name, user.organization_id, {"dataset_id": dataset.id, "source_type": "UPLOAD"})
    return run


def register_source(db: Session, user: User, connection: ExternalConnection,
                    binding: DatasetSourceBinding, dataset: Dataset, *,
                    idempotency_key: str | None = None, request_identity: dict | None = None) -> AcquisitionRun:
    if dataset.organization_id != user.organization_id or binding.organization_id != user.organization_id:
        raise AcquisitionOperationError(404, "NOT_FOUND", "No se encontró la selección solicitada.")
    version = current_version(db, connection)
    digest = _request_hash(request_identity or {"operation": "SOURCE", "dataset_id": dataset.id, "connection_version_id": version.id,
        "schema_name": binding.schema_name, "object_name": binding.object_name,
        "object_kind": binding.object_kind, "column_overrides": binding.column_overrides})
    existing = _idempotent(db, user, idempotency_key, digest)
    if existing:
        return existing
    source, version = saved_source(db, connection, schema_name=binding.schema_name, object_name=binding.object_name)
    columns = source.columns(binding.schema_name, binding.object_name)
    validate_column_overrides(binding.column_overrides, [column["name"] for column in columns])
    previous = db.scalar(select(DatasetVersion).where(DatasetVersion.dataset_id == dataset.id).order_by(DatasetVersion.version.desc()))
    source_snapshot = {"source_type": connection.source_type, "connection_id": connection.id,
        "connection_version_id": version.id, "connection_version": version.version,
        "config_hash": version.config_hash, "schema_name": binding.schema_name, "object_name": binding.object_name,
        "object_kind": binding.object_kind, "source_native_schema": columns,
        "previous_version_id": previous.id if previous else None}
    limits = AcquisitionLimits.configured()
    run = AcquisitionRun(id=uid(), organization_id=user.organization_id, dataset_id=dataset.id,
        source_type=connection.source_type, connection_version_id=version.id,
        filename=f"{binding.schema_name}.{binding.object_name}.parquet"[:240], source_snapshot=source_snapshot,
        reader_options={}, column_overrides=dict(binding.column_overrides), effective_limits=limits.as_dict(),
        request_hash=digest, idempotency_key=idempotency_key, initiated_by_id=user.id, initiated_by_name=user.name)
    db.add(run)
    db.flush()
    job_queue.submit_acquisition(db, run)
    audit(db, "ACQUISITION_REGISTERED", "acquisition", run.id, "Lectura de fuente registrada en segundo plano",
          user.name, user.organization_id, {"dataset_id": dataset.id, "source_type": connection.source_type,
                                          "connection_version_id": version.id})
    return run


def cancel_acquisition(db: Session, user: User, identifier: str) -> AcquisitionRun:
    run = owned_acquisition(db, identifier, user, lock=True)
    job = db.scalar(select(Job).where(Job.acquisition_id == run.id).with_for_update())
    if run.status in TERMINAL:
        return run
    run.cancel_requested = True
    if run.status == "QUEUED":
        run.status, run.stage, run.finished_at = "CANCELLED", "CANCELLED", utcnow()
        if job:
            job.status, job.lease_until = "CANCELLED", None
    audit(db, "ACQUISITION_CANCEL_REQUESTED", "acquisition", run.id, "Cancelación de adquisición solicitada",
          user.name, user.organization_id, {"dataset_id": run.dataset_id})
    return run


def cleanup_expired_uploads(db: Session, *, now=None, limit: int = 100) -> int:
    """Only expired RECEIVED files with no acquisition are candidates.

    Registered/active/referenced uploads and every business artifact are excluded.
    Claimed rows are locked, and the exact private temporary file is rechecked.
    """
    moment, removed = now or utcnow(), 0
    candidates = db.scalars(select(AcquisitionUpload).where(AcquisitionUpload.status == "RECEIVED",
        AcquisitionUpload.expires_at < moment, ~select(AcquisitionRun.id).where(
            AcquisitionRun.upload_id == AcquisitionUpload.id).exists()).limit(limit).with_for_update(skip_locked=True)).all()
    for upload in candidates:
        path = artifact_store.checked_path(upload.path)
        if not path.is_relative_to(artifact_store.location("tmp")):
            raise ArtifactIntegrityError("UPLOAD_STAGING_INVALID: El temporal no pertenece a staging.")
        path.unlink(missing_ok=True)
        upload.status = "EXPIRED"
        removed += 1
    return removed


def cleanup_abandoned_acquisitions(db: Session, *, now=None, limit: int = 100,
                                   grace_seconds: int | None = None) -> int:
    """Remove only obsolete private attempts and expired terminal upload staging.

    Acquisition -> Job -> Upload locks use publication's order. Live leases,
    queued work, successful originals, referenced artifacts and unrelated tmp
    directories are excluded. No immutable business directory is traversed.
    """
    moment = now or utcnow()
    grace = grace_seconds if grace_seconds is not None else max(LEASE_SECONDS * 2, AcquisitionLimits.configured().timeout_seconds)
    cutoff, removed = moment - timedelta(seconds=grace), 0
    eligible = or_(and_(AcquisitionRun.status.in_(TERMINAL), AcquisitionRun.finished_at < cutoff),
                   and_(AcquisitionRun.status == "RUNNING", Job.status == "RUNNING", Job.lease_until < cutoff))
    rows = db.scalars(select(AcquisitionRun).join(Job, Job.acquisition_id == AcquisitionRun.id)
        .where(eligible).order_by(AcquisitionRun.created_at).limit(limit)
        .with_for_update(of=AcquisitionRun, skip_locked=True)).all()
    root = artifact_store.location("tmp", "acquisitions")
    for run in rows:
        job = db.scalar(select(Job).where(Job.acquisition_id == run.id).with_for_update(skip_locked=True))
        if job is None or (job.status == "RUNNING" and (job.lease_until is None or job.lease_until.replace(tzinfo=UTC) >= cutoff)):
            continue
        folder = artifact_store.checked_path(root / run.id)
        if not folder.is_relative_to(root) or not folder.is_dir():
            attempts = []
        else:
            attempts = list(folder.iterdir())[:100]
        for attempt in attempts:
            private = artifact_store.checked_path(attempt)
            if not private.is_relative_to(folder) or not private.is_dir():
                continue
            modified = datetime.fromtimestamp(private.stat().st_mtime, UTC)
            if modified >= cutoff:
                continue
            prefix = str(private) + os.sep
            if db.scalar(select(Artifact.id).where(Artifact.path.startswith(prefix, autoescape=True)).limit(1)) or db.scalar(
                select(DatasetVersion.id).where(or_(DatasetVersion.original_path.startswith(prefix, autoescape=True),
                    DatasetVersion.canonical_path.startswith(prefix, autoescape=True))).limit(1)):
                continue
            shutil.rmtree(private)
            removed += 1
        if run.status in {"FAILED", "CANCELLED"} and run.output_version_id is None and run.upload_id:
            upload = db.scalar(select(AcquisitionUpload).where(AcquisitionUpload.id == run.upload_id).with_for_update())
            if upload is None or upload.status != "REGISTERED" or upload.expires_at.replace(tzinfo=UTC) >= moment:
                continue
            path = artifact_store.checked_path(upload.path)
            if not path.is_relative_to(artifact_store.location("tmp")) or db.scalar(select(Artifact.id).where(Artifact.path == str(path))):
                continue
            path.unlink(missing_ok=True)
            upload.status = "EXPIRED"
            removed += 1
    return removed


def _authorize(db: Session, run: AcquisitionRun) -> tuple[User, Dataset]:
    user, dataset = db.get(User, run.initiated_by_id), db.get(Dataset, run.dataset_id)
    if user is None or dataset is None or user.organization_id != run.organization_id or dataset.organization_id != run.organization_id:
        raise AcquisitionStopped("ACQUISITION_ACTOR_UNAVAILABLE", "No se puede resolver al usuario o dataset de la adquisición.")
    required = {"datasets:write"} | ({"connections:use"} if run.source_type != "UPLOAD" else set())
    if not required <= set(effective_permissions(db, user)):
        raise AcquisitionStopped("ACQUISITION_PERMISSION_REVOKED", "El usuario ya no tiene los permisos necesarios para adquirir estos datos.")
    return user, dataset


def execute_acquisition(identifier: str, owner: str) -> None:
    started = time.monotonic()
    iterable: Iterator[DatasetBatch | DatasetReadResult]
    with SessionLocal() as db:
        run = db.get(AcquisitionRun, identifier)
        if run is None:
            raise LookupError("La adquisición no existe.")
        _authorize(db, run)
        limits = AcquisitionLimits(**run.effective_limits)
        attempt = run.attempt_id
        if not attempt:
            raise AcquisitionStopped("ACQUISITION_LEASE_LOST", "La adquisición no tiene un intento vigente.")
        snapshot, options, overrides = dict(run.source_snapshot), dict(run.reader_options), dict(run.column_overrides)
        source_type, filename, upload_id = run.source_type, run.filename, run.upload_id
        original = None
        if upload_id:
            upload = db.get(AcquisitionUpload, upload_id)
            if upload is None:
                raise AcquisitionStopped("UPLOAD_NOT_FOUND", "El archivo recibido no está disponible.")
            original = artifact_store.materialize_reference(upload.path, expected_sha256=upload.sha256,
                                                             expected_size=upload.size_bytes)
            reader = FileBatchReader(original, filename, options, limits)
            iterable = iter(reader)
        else:
            source_version = db.get(ExternalConnectionVersion, run.connection_version_id)
            connection = db.get(ExternalConnection, snapshot["connection_id"])
            if source_version is None or connection is None or connection.organization_id != run.organization_id or connection.deleted or not connection.enabled:
                raise AcquisitionStopped("CONNECTION_DISABLED", "La conexión fuente ya no está habilitada.")
            if source_version.connection_id != connection.id or source_version.config_hash != snapshot["config_hash"]:
                raise AcquisitionStopped("SOURCE_REVISION_MISMATCH", "La identidad de la revisión fuente no coincide.")
            source = source_registry.create(settings_for(connection, source_version), snapshot["schema_name"], snapshot["object_name"])
            reader = None
            iterable = source.read_batches(limits=limits)

    work = artifact_store.create_directory("tmp", "acquisitions", identifier, attempt)
    last_state_check = 0.0

    def checkpoint(*, stage: str | None = None, rows: int | None = None, size: int | None = None):
        nonlocal last_state_check
        moment = time.monotonic()
        if moment - started > limits.timeout_seconds:
            raise AcquisitionReadError("ACQUISITION_TIMEOUT", "La adquisición excedió su tiempo efectivo.", {"maximum_seconds": limits.timeout_seconds})
        # A narrow, byte-bounded SQL fetch can contain only a handful of rows.
        # Probe its persisted cancellation/lease at most four times per second;
        # every materialization/progress/stage boundary still probes immediately.
        # Final publication additionally holds and verifies the authoritative locks.
        if stage is None and rows is None and size is None and moment - last_state_check < 0.25:
            return
        if shutil.disk_usage(work).free < limits.min_free_bytes:
            raise AcquisitionStopped("RESOURCE_DISK_INSUFFICIENT", "No queda la reserva de disco requerida para completar la adquisición.")
        if reader is not None and reader.format == "XLSX":
            used = sum(item.stat().st_size for item in work.iterdir() if item.is_file())
            if used > limits.xlsx_temp_bytes:
                raise limit_error("ACQUISITION_TEMP_DISK_LIMIT", "temporary_bytes", limits.xlsx_temp_bytes, used)
        with SessionLocal() as state:
            job = state.scalar(select(Job).where(Job.acquisition_id == identifier))
            acquisition = state.get(AcquisitionRun, identifier)
            if job is None or acquisition is None or job.lease_owner != owner or job.status != "RUNNING" or job.lease_until is None or job.lease_until.replace(tzinfo=UTC) <= utcnow() or acquisition.attempt_id != attempt:
                raise AcquisitionStopped("ACQUISITION_LEASE_LOST", "El worker perdió su lease; no puede publicar esta versión.")
            if acquisition.cancel_requested:
                raise AcquisitionStopped("ACQUISITION_CANCELLED", "La adquisición fue cancelada; no se publicaron datos parciales.")
            if stage is not None:
                acquisition.stage = stage
            if rows is not None:
                acquisition.processed_rows = rows
            if size is not None:
                acquisition.processed_bytes = size
            if stage is not None or rows is not None or size is not None:
                state.commit()
            last_state_check = moment

    if reader is not None:
        reader.check = checkpoint
        reader.work_dir = work
    else:
        # The frozen source adapter uses the same cooperative checks between
        # fetches. No source credential is forwarded to a quality/Spark worker.
        iterable = source.read_batches(limits=limits, check=checkpoint)
    paths: list[Path] = []
    total_rows = total_bytes = 0
    native: dict = {}
    metadata: dict = {}
    media_type = None
    try:
        checkpoint(stage="READING")
        for batch in iterable:
            checkpoint(stage="MATERIALIZING")
            if isinstance(batch, DatasetBatch):
                frame = batch.physical_frame()
                native, metadata, media_type = batch.native_schema, {**batch.metadata, "source_format": batch.source_format,
                    "format_label": batch.format_label, "row_numbering": batch.row_numbering}, batch.media_type
            else:
                columns = batch.metadata.get("source_native_schema")
                if columns != snapshot.get("source_native_schema"):
                    raise AcquisitionStopped("SOURCE_SCHEMA_DRIFT", "El esquema de la fuente cambió después de confirmar la selección.")
                frame = batch.frame.with_columns(pl.Series(RECORD_NUMBER_COLUMN,
                    range(total_rows + 1, total_rows + batch.frame.height + 1), dtype=pl.Int64))
                native, metadata, media_type = batch.native_schema, {**batch.metadata, "source_format": source_type,
                    "format_label": batch.format_label, "row_numbering": "SNAPSHOT_ROW"}, batch.media_type
            path = work / f"part-{len(paths):06d}.parquet"
            frame.write_parquet(path)
            paths.append(path)
            total_rows += frame.height
            total_bytes += sum(len(value.encode("utf-8")) for row in frame.drop(RECORD_NUMBER_COLUMN).iter_rows()
                               for value in row if value is not None)
            checkpoint(stage="READING", rows=total_rows, size=total_bytes)
        checkpoint(stage="PROFILING")
        spill_limit = None
        if reader is not None and reader.format == "XLSX":
            used = sum(item.stat().st_size for item in work.iterdir() if item.is_file())
            spill_limit = limits.xlsx_temp_bytes - used
            if spill_limit <= 0:
                raise limit_error("ACQUISITION_TEMP_DISK_LIMIT", "temporary_bytes", limits.xlsx_temp_bytes, used)
        profiled = profile_paths(paths, column_overrides=overrides, native_types=native, limits=limits, check=checkpoint,
                                 temp_byte_limit=spill_limit)
        checkpoint(stage="PUBLISHING")
        with SessionLocal() as db:
            run = db.scalar(select(AcquisitionRun).where(AcquisitionRun.id == identifier).with_for_update())
            job = db.scalar(select(Job).where(Job.acquisition_id == identifier).with_for_update())
            if run is None or job is None or job.lease_owner != owner or job.status != "RUNNING" or job.lease_until is None or job.lease_until.replace(tzinfo=UTC) <= utcnow() or run.attempt_id != attempt:
                raise AcquisitionStopped("ACQUISITION_LEASE_LOST", "El worker perdió su lease antes de publicar.")
            if run.cancel_requested:
                raise AcquisitionStopped("ACQUISITION_CANCELLED", "La adquisición fue cancelada antes de publicar.")
            _, dataset = _authorize(db, run)
            if source_type != "UPLOAD":
                assert run.connection_version_id is not None
                connection = db.get(ExternalConnection, snapshot["connection_id"])
                if connection is None or connection.deleted or not connection.enabled:
                    raise AcquisitionStopped("CONNECTION_DISABLED", "La conexión fuente ya no está habilitada.")
                metadata["source"] = {**snapshot, "captured_at": iso(utcnow())}
            metadata.update(acquisition_id=identifier, acquisition_attempt_id=attempt,
                            record_number_column=RECORD_NUMBER_COLUMN,
                            observed_size_bytes=total_bytes, effective_limits=limits.as_dict())
            # Holding Acquisition -> Job -> Dataset locks serializes cancel and
            # publication. The checked lease stays the authoritative fence.
            version = publish_materialized_version(db, dataset, paths, filename=filename,
                actor=Actor("WORKER", owner, "Worker adquisición"), source_type=source_type, column_overrides=overrides,
                native_types=native, metadata=metadata, original=original, original_media_type=media_type,
                limits=limits, profiled=profiled)
            if job.lease_until.replace(tzinfo=UTC) <= utcnow():
                raise AcquisitionStopped("ACQUISITION_LEASE_LOST", "El lease venció durante la publicación; la versión no se publicó.")
            if source_type != "UPLOAD":
                assert run.connection_version_id is not None
                link_artifact(db, run.organization_id, "SOURCE_SNAPSHOT", "DATASET_VERSION", version.id,
                              "CONNECTION_VERSION", run.connection_version_id)
                if snapshot.get("previous_version_id"):
                    link_artifact(db, run.organization_id, "REFRESH_OF", "DATASET_VERSION", version.id,
                                  "DATASET_VERSION", snapshot["previous_version_id"])
            link_artifact(db, run.organization_id, "ACQUISITION_OUTPUT", "ACQUISITION", run.id, "DATASET_VERSION", version.id)
            run.output_version_id, run.status, run.stage, run.finished_at = version.id, "SUCCESS", "COMPLETED", utcnow()
            run.total_rows, run.processed_rows, run.processed_bytes = total_rows, total_rows, total_bytes
            # total_bytes describes transfer bytes; processed_bytes are observed
            # UTF-8 values. Unknown SQL transfer totals remain null.
            job.status, job.lease_until, job.last_error = "SUCCESS", None, None
            if upload_id:
                upload = db.get(AcquisitionUpload, upload_id)
                assert upload is not None
                upload.status = "CONSUMED"
            audit(db, "ACQUISITION_COMPLETED", "acquisition", run.id, "Adquisición completada y versión publicada",
                  Actor("WORKER", owner, "Worker adquisición"), run.organization_id, {"dataset_id": dataset.id,
                  "user_id": run.initiated_by_id,
                  "dataset_version_id": version.id, "row_count": total_rows})
            db.commit()
        if original is not None:
            original.unlink(missing_ok=True)
    finally:
        close = getattr(iterable, "close", None)
        if close is not None:
            close()
        shutil.rmtree(artifact_store.checked_path(work), ignore_errors=True)


def process_once(owner: str | None = None, active: dict | None = None) -> bool:
    """Separate acquisition worker, same Job queue/claims/lease/heartbeat model."""
    owner, active = owner or uid(), active if active is not None else {}
    eligible = or_(Job.status == "QUEUED", and_(Job.status == "RUNNING", Job.lease_until < utcnow()))
    with SessionLocal() as db:
        candidate = db.scalar(select(Job).where(eligible, Job.lane == "ACQUISITION").order_by(Job.created_at).limit(1))
        if candidate is None:
            return False
        run = db.scalar(select(AcquisitionRun).where(AcquisitionRun.id == candidate.acquisition_id).with_for_update())
        if run is None:
            raise LookupError("El trabajo no tiene una adquisición existente.")
        claimed = db.execute(update(Job).where(Job.id == candidate.id, eligible).values(status="RUNNING",
            lease_owner=owner, lease_until=utcnow() + timedelta(seconds=LEASE_SECONDS)).execution_options(synchronize_session=False))
        if cast(CursorResult, claimed).rowcount != 1:
            db.rollback()
            return False
        db.refresh(candidate)
        if run.status in TERMINAL:
            candidate.status, candidate.lease_until = run.status, None
            db.commit()
            return True
        if run.cancel_requested or candidate.attempts >= 3:
            run.status = "CANCELLED" if run.cancel_requested else "FAILED"
            run.stage, run.finished_at = run.status, utcnow()
            if run.status == "FAILED":
                run.error_code, run.error_message = "ACQUISITION_ATTEMPTS_EXHAUSTED", "La adquisición agotó los intentos tras interrupciones del worker."
            candidate.status, candidate.lease_until = run.status, None
            db.commit()
            return True
        candidate.attempts += 1
        run.attempts, run.attempt_id = candidate.attempts, uid()
        run.status, run.stage, run.started_at = "RUNNING", "READING", run.started_at or utcnow()
        run.processed_rows, run.processed_bytes = 0, 0
        run.error_code, run.error_message, run.error_details, run.error_reference = None, None, None, None
        identity, job_id = run.id, candidate.id
        db.commit()
    active["job_id"] = job_id
    try:
        execute_acquisition(identity, owner)
    except Exception as exc:  # noqa: BLE001 - worker boundary persists only sanitized closed errors.
        # Closed error categories only. Source adapters already sanitize driver
        # errors; parser/internal exceptions never cross into persistent text.
        details = None
        if isinstance(exc, (AcquisitionStopped, SourceError)):
            code, message = exc.code, exc.message
        elif isinstance(exc, ArtifactIntegrityError):
            code, message = "ARTIFACT_INTEGRITY_ERROR", "La integridad de los datos de adquisición no coincide."
        elif isinstance(exc, ProcessingError):
            diagnostic = processing_diagnostic(exc)
            code, message, details = diagnostic.code, diagnostic.message, diagnostic.details
        elif isinstance(exc, (MemoryError, duckdb.OutOfMemoryException)):
            code, message = "ACQUISITION_MEMORY_LIMIT", "No hay memoria suficiente para completar la adquisición dentro del presupuesto."
        elif isinstance(exc, PermissionError):
            code, message = "ACQUISITION_STORAGE_PERMISSION", "El almacenamiento no permite completar la adquisición."
        elif isinstance(exc, OSError) and exc.errno == 28:
            code, message = "RESOURCE_DISK_INSUFFICIENT", "No hay espacio de disco suficiente para completar la adquisición."
        elif isinstance(exc, duckdb.IOException) and "max_temp_directory_size" in str(exc):
            code, message = "ACQUISITION_TEMP_DISK_LIMIT", "El perfil completo excede el presupuesto efectivo de almacenamiento temporal."
        else:
            code, message = "ACQUISITION_FAILED", "No se pudo completar la adquisición. Conserva la referencia para diagnóstico."
        logger.error("Acquisition failed reference=%s code=%s exception_type=%s", identity, code, type(exc).__name__)
        with SessionLocal() as db:
            run = db.scalar(select(AcquisitionRun).where(AcquisitionRun.id == identity).with_for_update())
            job = db.scalar(select(Job).where(Job.id == job_id).with_for_update())
            if run is not None and job is not None and job.lease_owner == owner and job.status == "RUNNING" and job.lease_until and job.lease_until.replace(tzinfo=UTC) > utcnow():
                run.status = "CANCELLED" if code == "ACQUISITION_CANCELLED" else "FAILED"
                run.stage, run.finished_at = run.status, utcnow()
                run.error_code, run.error_message = code, message
                run.error_details, run.error_reference = details, run.id
                job.status, job.last_error, job.lease_until = run.status, message, None
                audit(db, "ACQUISITION_" + run.status, "acquisition", run.id, message,
                      Actor("WORKER", owner, "Worker adquisición"), run.organization_id,
                      {"dataset_id": run.dataset_id, "error_code": code, "user_id": run.initiated_by_id})
                db.commit()
    finally:
        active.pop("job_id", None)
    return True
