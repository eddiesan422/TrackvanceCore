"""Shared JobQueue REPORT lane: durable materialization, leases and fenced publication."""
from __future__ import annotations

import logging
import re
import shutil
import signal
import threading
import time
from dataclasses import replace
from datetime import UTC, timedelta
from pathlib import Path
from typing import Any, cast
from uuid import NAMESPACE_URL, uuid5

import polars as pl
from sqlalchemy import and_, or_, select, text, update
from sqlalchemy.engine import CursorResult
from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy.orm import Session

from .acquisition_config import AcquisitionLimits
from .artifactstore import artifact_store, link_artifact, storage_provider
from .audit_context import Actor
from .db import SessionLocal, utcnow
from .governance import add_security_dependency, governance_snapshot, validate_classification
from .governance_models import GovernanceHistory
from .models import Artifact, Dataset, DatasetVersion, Job, User, uid
from .operations_common import OperationError
from .report_config import ReportLimits
from .report_executor import execute_messages
from .report_exports import scalar
from .report_models import ReportExecution
from .report_query import digest
from .report_service import (
    admission,
    check_execution,
    execution_sources,
    resolved_context,
    revalidate,
)
from .worker import LEASE_SECONDS, heartbeat

logger = logging.getLogger(__name__)


def _execution(db: Session, identity: str) -> ReportExecution:
    item = db.get(ReportExecution, identity)
    if item is None:
        raise OperationError(404, "REPORT_EXECUTION_NOT_FOUND", "La ejecución ya no existe.")
    return item


def _user(db: Session, identity: str) -> User:
    item = db.get(User, identity)
    if item is None:
        raise OperationError(403, "REPORT_USER_UNAVAILABLE", "El responsable de la ejecución ya no está disponible.")
    return item


def _cleanup_prepared(organization_id, identity, attempt, owner, part_count):
    """Remove only this attempt's known immutable candidates lacking any DB row.

    Published descriptors/parts have committed Artifact records. A filesystem
    promotion followed by rollback has no such record and is safe to reclaim.
    """
    canonical_id = digest({"execution": identity, "attempt": attempt, "owner": owner})[:64]
    identities = [canonical_id] + [str(uuid5(NAMESPACE_URL,
        f"trackvance:dataset:{organization_id}:{canonical_id}:{ordinal}")) for ordinal in range(part_count)]
    with SessionLocal() as db:
        for artifact_id in identities:
            if db.get(Artifact, artifact_id) is not None:
                continue
            folder = artifact_store.location("artifacts", artifact_id)
            # Exact provider identity, no traversal, symlink or recursive parent.
            if folder.is_dir() and folder.name == artifact_id and folder.parent.name == "artifacts":
                shutil.rmtree(folder)


def _lease(db, execution, owner, *, lock=False):
    if lock:
        execution = db.scalar(select(ReportExecution).where(ReportExecution.id == execution.id).with_for_update())
    if execution is None:
        raise OperationError(404, "REPORT_EXECUTION_NOT_FOUND", "La ejecución ya no existe.")
    db.refresh(execution)
    job = db.scalar(select(Job).where(Job.report_execution_id == execution.id).with_for_update() if lock
                    else select(Job).where(Job.report_execution_id == execution.id))
    expiry = job.lease_until.replace(tzinfo=UTC) if job and job.lease_until and job.lease_until.tzinfo is None else job.lease_until if job else None
    if not job or job.lease_owner != owner or job.status != "RUNNING" or not expiry or expiry <= utcnow():
        raise OperationError(409, "REPORT_LEASE_LOST", "El intento perdió autoridad de publicación.")
    if execution.cancel_requested:
        raise OperationError(409, "REPORT_CANCELLED", "La generación fue cancelada.")
    if lock:
        result = db.execute(update(Job).where(Job.id == job.id, Job.lease_owner == owner, Job.status == "RUNNING",
            Job.lease_until > utcnow()).values(lease_until=utcnow() + timedelta(seconds=LEASE_SECONDS))
            .execution_options(synchronize_session=False))
        if result.rowcount != 1:
            raise OperationError(409, "REPORT_LEASE_LOST", "El intento perdió su fence.")
    return job


def _checkpoint(identity, owner):
    check_execution(identity, "reports:generate")
    with SessionLocal() as db:
        execution = _execution(db, identity)
        start = execution.started_at.replace(tzinfo=UTC) if execution.started_at and execution.started_at.tzinfo is None else execution.started_at
        if execution.status != "RUNNING" or start and start + timedelta(seconds=ReportLimits.configured("DATASET").timeout_seconds) <= utcnow():
            raise OperationError(422, "REPORT_TIMEOUT", "La generación excedió el tiempo global autorizado.")
        _lease(db, execution, owner)


def _logical_columns(columns):
    result = {}
    for column in columns:
        kind = column["type"]
        logical = ("DECIMAL" if kind.startswith("DECIMAL") else "INT64" if kind in {"BIGINT", "INTEGER", "SMALLINT", "TINYINT", "HUGEINT", "UBIGINT"}
                   else "TIMESTAMP" if kind.startswith("TIMESTAMP") else "DATE" if kind == "DATE"
                   else "BOOLEAN" if kind == "BOOLEAN" else "STRING" if kind == "VARCHAR" else None)
        if logical is None:
            raise OperationError(422, "REPORT_PUBLICATION_TYPE", "El resultado contiene un tipo no compatible con la publicación canónica.")
        if kind in {"HUGEINT", "UBIGINT"}:
            logical = "DECIMAL"
        result[column["name"]] = {"logical_type": logical,
                                   **({"semantic_tag": "IDENTIFIER"} if logical == "STRING" else {})}
    return result


def _write_parts(messages, staging, limits, checkpoint, progress):
    columns: list[dict[str, Any]] = []
    names: list[str] = []
    parts: list[Path] = []
    rows, bytes_written, metrics, cardinality = 0, 0, {}, []
    # One part per <=32 MiB observed population and <=50k rows. Buffered rows
    # remain strings and are bounded independently of compressed Parquet size.
    pending: list[list[str | None]] = []
    pending_bytes = 0

    def flush():
        nonlocal pending, pending_bytes, bytes_written
        if not names:
            raise OperationError(422, "REPORT_RESULT_SCHEMA", "No existe un esquema de salida.")
        path = staging / f"part-{len(parts):06d}.parquet"
        data: dict[str, list[str | int | None]] = {name: [row[i] for row in pending] for i, name in enumerate(names)}
        data["__tv_record_number"] = list(range(rows - len(pending) + 1, rows + 1))
        frame = pl.DataFrame(data, schema={**{name: pl.String for name in names}, "__tv_record_number": pl.Int64})
        frame.write_parquet(path, compression="zstd", row_group_size=min(limits.batch_rows, 5000))
        bytes_written += path.stat().st_size
        if bytes_written > limits.max_bytes:
            raise OperationError(422, "REPORT_DATASET_BYTES", "Las partes excedieron el límite de bytes de publicación.")
        parts.append(path)
        pending, pending_bytes = [], 0
        checkpoint()

    for message in messages:
        checkpoint()
        if message["kind"] == "schema":
            columns, names = message["columns"], [c["name"] for c in message["columns"]]
            _logical_columns(columns)
        elif message["kind"] == "cardinality":
            cardinality = message["items"]
        elif message["kind"] == "batch":
            converted = [[None if value is None else "true" if value is True else "false" if value is False else str(scalar(value)) for value in row]
                         for row in message["rows"]]
            size = sum(sum(32 + 4 * len(cell) for cell in row if cell is not None) for row in converted)
            if pending and (pending_bytes + size > 32 * 1024**2 or len(pending) + len(converted) > 50000):
                flush()
            pending.extend(converted)
            pending_bytes += size
            rows += len(converted)
            progress(rows, bytes_written, len(parts))
        elif message["kind"] == "complete":
            metrics = {k: v for k, v in message.items() if k != "kind"}
    if pending or not parts:
        flush()
    if metrics.get("rows") != rows:
        raise OperationError(422, "REPORT_RESULT_INCOMPLETE", "El canal no acredita un resultado completo.")
    return parts, columns, {**metrics, "parts": len(parts), "canonical_size_bytes": bytes_written,
                            "cardinality": cardinality}


def materialize(identity, owner):
    limits = ReportLimits.configured("DATASET")
    with SessionLocal() as db:
        execution = _execution(db, identity)
        job = _lease(db, execution, owner)
        user = _user(db, execution.user_id)
        context = resolved_context(db, execution.context_id, user, expired_ok=True)
        revalidate(db, context, user, "reports:generate")
        inputs = execution_sources(db, context)
        plan, publication, attempt = context.snapshot["plan"], execution.publication, job.attempts
        sources, organization_id = context.snapshot["sources"], execution.organization_id
        db.commit()
    staging = artifact_store.create_directory("report-staging", identity, f"{attempt}-{owner}")
    spill = staging / "spill"
    spill.mkdir()
    checkpoint = lambda: _checkpoint(identity, owner)
    last_progress = 0.0
    part_count = 0
    disk_free_at_start = shutil.disk_usage(staging).free
    min_disk_free_observed = disk_free_at_start
    max_temporary_bytes_sampled = 0

    def progress(rows, byte_count, parts):
        nonlocal last_progress, min_disk_free_observed, max_temporary_bytes_sampled
        now = time.monotonic()
        if now - last_progress < 1:
            return
        last_progress = now
        disk_free = shutil.disk_usage(staging).free
        min_disk_free_observed = min(min_disk_free_observed, disk_free)
        if disk_free < 512 * 1024**2:
            raise OperationError(422, "REPORT_DISK_LIMIT", "No hay reserva de disco suficiente para continuar.")
        # Includes DuckDB spill while running, not only leftovers after exit.
        observed = sum(p.stat().st_size for p in staging.rglob("*") if p.is_file())
        max_temporary_bytes_sampled = max(max_temporary_bytes_sampled, observed)
        if observed > limits.temp_bytes + limits.max_bytes:
            raise OperationError(422, "REPORT_TEMP_LIMIT", "El staging excedió el límite del trabajo.")
        with SessionLocal() as db:
            item = _execution(db, identity)
            _lease(db, item, owner)
            item.progress_stage, item.progress_percent = "Materializando resultado", 40
            item.metrics = {"rows_generated": rows, "bytes_prepared": byte_count, "parts_prepared": parts,
                            "temporary_bytes_observed": observed}
            db.commit()
    try:
        query_started = time.monotonic()
        messages = execute_messages(inputs, plan, "DATASET", staging=spill, check=checkpoint)
        try:
            parts, columns, metrics = _write_parts(messages, staging, limits, checkpoint, progress)
            part_count = len(parts)
        finally:
            messages.close()
        query_seconds = time.monotonic() - query_started
        checkpoint()
        from .dataset_scans import profile_paths
        profile_limits = replace(AcquisitionLimits.configured(), max_rows=limits.max_rows,
                                 timeout_seconds=limits.timeout_seconds, memory_bytes=limits.memory_bytes)
        overrides = _logical_columns(columns)
        profiling_started = time.monotonic()
        profiled = profile_paths(parts, column_overrides=overrides, limits=profile_limits,
                                 check=checkpoint, temp_byte_limit=limits.temp_bytes,
                                 temporary_parent=staging)
        metrics.update(query_seconds=query_seconds, profiling_seconds=time.monotonic() - profiling_started,
                       disk_free_bytes_at_start=disk_free_at_start,
                       disk_free_bytes_min_observed=min_disk_free_observed,
                       temporary_bytes_sampled_max=max_temporary_bytes_sampled)
        checkpoint()
        # File preparation and verification precede the short publication fence.
        # New artifact rows remain uncommitted until version + lineage + job do.
        with SessionLocal() as db:
            item = _execution(db, identity)
            user = _user(db, item.user_id)
            context = resolved_context(db, item.context_id, user, expired_ok=True)
            canonical = storage_provider.put_dataset(db, parts, "REPORT_CANONICAL", organization_id,
                artifact_id=digest({"execution": identity, "attempt": attempt, "owner": owner})[:64],
                metadata={"report_execution_id": identity, "query_hash": plan["query_hash"]})
            prepared = storage_provider.dataset_paths(canonical)
            if sum(pl.scan_parquet(p).select(pl.len()).collect().item() for p in prepared) != metrics["rows"]:
                raise OperationError(422, "REPORT_PUBLICATION_INTEGRITY", "Las partes preparadas no coinciden con el conteo completo.")
            # Linearization point: old worker cannot publish after lease recovery.
            job = _lease(db, item, owner, lock=True)
            revalidate(db, context, user, "reports:generate")
            validate_classification(db, organization_id, publication.get("macro_domain_id"), publication.get("domain_id"))
            sensitivities = {s["governance"].get("information_classification", "UNKNOWN") for s in sources}
            rank = {"PUBLIC": 0, "INTERNAL": 1, "CONFIDENTIAL": 2, "RESTRICTED": 3, "UNKNOWN": 4}
            requested = publication.get("information_classification", "UNKNOWN")
            inherited = max(sensitivities | {requested}, key=lambda value: rank.get(value, 4))
            dataset = Dataset(id=uid(), organization_id=organization_id, name=publication["name"],
                description=publication.get("description", ""), domain="", owner=publication.get("owner") or user.name,
                criticality=publication.get("criticality", "HIGH"), macro_domain_id=publication.get("macro_domain_id"),
                domain_id=publication.get("domain_id"), business_owner_id=publication.get("business_owner_id"),
                steward_id=publication.get("steward_id"), technical_custodian_id=publication.get("technical_custodian_id"),
                information_classification=inherited)
            db.add(dataset)
            db.flush()
            schema, profile, schema_hash = profiled
            version = DatasetVersion(id=uid(), organization_id=organization_id, dataset_id=dataset.id, version=1,
                filename="report.dataset.json", source_type="REPORT_OUTPUT", sha256=canonical.sha256,
                schema_hash=schema_hash, size_bytes=canonical.size_bytes, row_count=metrics["rows"], column_count=len(schema),
                profile_status="READY", original_path="", canonical_path=canonical.path, schema_json=schema,
                profile={**profile, "row_numbering": "DERIVED_RECORD_NUMBER"}, canonical_artifact_id=canonical.id,
                ingestion_metadata={"storage_contract_version": 1, "dataset_descriptor_version": 1,
                    "canonical_size_bytes": metrics["canonical_size_bytes"], "row_numbering": "DERIVED_RECORD_NUMBER",
                    "record_number_column": "__tv_record_number", "report_execution_id": identity,
                    "report_context_id": context.id, "report_revision_id": context.revision_id,
                    "query_hash": plan["query_hash"], "quality_status": "PENDING_VALIDATION"})
            db.add(version)
            db.flush()
            actor = Actor("USER", user.id, user.name)
            for source in sources:
                add_security_dependency(db, dataset.id, source["output_dataset_id"], actor)
                link_artifact(db, organization_id, "REPORT_SOURCE", "REPORT_EXECUTION", identity, "DATASET_VERSION", source["output_version_id"])
                link_artifact(db, organization_id, "DERIVED_FROM", "DATASET_VERSION", version.id, "DATASET_VERSION", source["output_version_id"])
                link_artifact(db, organization_id, "REPORT_APPROVAL", "REPORT_EXECUTION", identity, "RUN", source["approval_run_id"])
            link_artifact(db, organization_id, "REPORT_OUTPUT", "REPORT_EXECUTION", identity, "DATASET_VERSION", version.id)
            link_artifact(db, organization_id, "REPORT_OUTPUT", "REPORT_EXECUTION", identity, "ARTIFACT", canonical.id)
            if context.revision_id:
                link_artifact(db, organization_id, "REPORT_REVISION", "REPORT_EXECUTION", identity, "REPORT_REVISION", context.revision_id)
            db.add(GovernanceHistory(organization_id=organization_id, dataset_id=dataset.id, version=1,
                actor_id=user.id, snapshot=governance_snapshot(db, dataset), reason="Dataset generado desde Reportes"))
            item.output_version_id, item.status, item.generation_status = version.id, "SUCCESS", "COMPLETE"
            item.metrics, item.progress_stage, item.progress_percent, item.finished_at = metrics, "Publicado; pendiente de validación de calidad", 100, utcnow()
            job.status, job.lease_until, job.last_error = "SUCCESS", None, None
            from .services import audit
            audit(db, "REPORT_DATASET_PUBLISHED", "report_execution", identity, "Nuevo dataset publicado íntegramente, pendiente de calidad", actor,
                  organization_id, {"dataset_id": dataset.id, "dataset_version_id": version.id,
                                    "row_count": version.row_count, "artifact_id": canonical.id})
            db.commit()
    finally:
        _cleanup_prepared(organization_id, identity, attempt, owner, part_count)
        # Own attempt only. No referenced artifact lives in report-staging.
        checked = artifact_store.checked_path(staging)
        if checked.parts[-3] == "report-staging" and checked.parent.name == identity:
            shutil.rmtree(checked)


def process_once(owner=None, active=None):
    owner, active = owner or uid(), active if active is not None else {}
    eligible = or_(Job.status == "QUEUED", and_(Job.status == "RUNNING", Job.lease_until < utcnow()))
    with SessionLocal() as db:
        if db.get_bind().dialect.name == "postgresql":
            # Claim and API admission share lock order: global gate precedes
            # Execution→Job. A stale execution reaper cannot deadlock a claim.
            db.execute(text("SELECT pg_advisory_xact_lock(8080080)"))
        else:
            db.connection().exec_driver_sql("BEGIN IMMEDIATE")
        candidate = db.scalar(select(Job).where(Job.lane == "REPORT", eligible).order_by(Job.created_at).limit(1))
        if not candidate:
            return False
        execution = db.scalar(select(ReportExecution).where(ReportExecution.id == candidate.report_execution_id).with_for_update())
        if execution is None:
            raise OperationError(404, "REPORT_EXECUTION_NOT_FOUND", "El trabajo no tiene una ejecución válida.")
        claimed = db.execute(update(Job).where(Job.id == candidate.id, eligible).values(status="RUNNING", lease_owner=owner,
            lease_until=utcnow() + timedelta(seconds=LEASE_SECONDS)).execution_options(synchronize_session=False))
        if cast(CursorResult, claimed).rowcount != 1:
            db.rollback()
            return False
        db.refresh(candidate)
        if execution.output_version_id and execution.status == "SUCCESS":
            candidate.status, candidate.lease_until = "SUCCESS", None
            db.commit()
            return True
        if candidate.attempts >= 3:
            candidate.status, candidate.lease_until = "FAILED", None
            execution.status, execution.error_code, execution.finished_at = "FAILED", "REPORT_ATTEMPTS_EXHAUSTED", utcnow()
            db.commit()
            return True
        candidate.attempts += 1
        try:
            admission(db, execution)
        except OperationError as exc:
            if exc.code != "REPORT_CONCURRENCY_LIMIT":
                raise
            candidate.status, candidate.lease_owner, candidate.lease_until = "QUEUED", None, None
            candidate.attempts -= 1
            db.commit()
            return False
        identity, job_id = execution.id, candidate.id
        active["job_id"] = job_id
    try:
        materialize(identity, owner)
    except Exception as exc:  # noqa: BLE001 - durable worker projects sanitized terminal metadata.
        error = exc if isinstance(exc, OperationError) else OperationError(422, "DATASET_NAME_CONFLICT", "Otra generación publicó ese nombre; elige uno nuevo.") if isinstance(exc, IntegrityError) else OperationError(422, "REPORT_GENERATION_FAILED", "La generación falló sin publicar una versión parcial.")
        with SessionLocal() as db:
            item = _execution(db, identity)
            job = db.get(Job, job_id)
            if job is None:
                return True
            if job.lease_owner != owner or item.status == "SUCCESS":
                return True
            status = "CANCELLED" if error.code == "REPORT_CANCELLED" else "FAILED"
            item.status, item.generation_status, item.error_code, item.error_message = status, status, error.code, error.message
            item.finished_at, item.progress_stage = utcnow(), "Cancelado" if status == "CANCELLED" else "Falló"
            job.status, job.lease_until, job.last_error = status, None, error.code
            db.commit()
        logger.info("Report generation %s ended code=%s", identity, error.code)
    finally:
        active.pop("job_id", None)
    return True


def cleanup_abandoned(db):
    root = artifact_store.location("report-staging")
    if not root.exists():
        return 0
    removed = 0
    for folder in root.iterdir():
        if not folder.is_dir() or not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", folder.name):
            continue
        job = db.scalar(select(Job).where(Job.report_execution_id == folder.name))
        for candidate in folder.iterdir():
            checked = artifact_store.checked_path(candidate)
            if not checked.is_dir() or not re.fullmatch(r"[0-9]+-[A-Za-z0-9_-]{1,64}", checked.name):
                continue
            if job and job.status == "RUNNING" and checked.name == f"{job.attempts}-{job.lease_owner}":
                continue
            # Wait beyond the executor deadline; a recovered worker cannot
            # publish from the old staging, and cleanup cannot race a live scan.
            if time.time() - checked.stat().st_mtime > ReportLimits.configured("DATASET").timeout_seconds + 60:
                if job:
                    attempt_label, old_owner = checked.name.split("-", 1)
                    count = len(list(checked.glob("part-*.parquet")))
                    _cleanup_prepared(job.organization_id, folder.name, int(attempt_label), old_owner, count)
                shutil.rmtree(checked)
                removed += 1
    return removed


def main():
    logging.basicConfig(level=logging.INFO)
    stop, owner = threading.Event(), uid()
    active: dict[str, Any] = {}
    signal.signal(signal.SIGINT, lambda *_: stop.set())
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    pulse = threading.Thread(target=heartbeat, args=(owner, stop, active, "REPORT"), daemon=True)
    pulse.start()
    ticks = 0
    while not stop.is_set():
        try:
            if not process_once(owner, active):
                ticks += 1
                if ticks % 60 == 0:
                    with SessionLocal() as db:
                        cleanup_abandoned(db)
                stop.wait(1)
        except OperationalError:
            stop.wait(3)
    pulse.join(timeout=2)


if __name__ == "__main__":
    main()
