"""Native COMPARISON queue lane; crash recovery restarts private staging with a fence."""
from __future__ import annotations

import logging
import signal
import sqlite3
import threading
import time
from datetime import UTC, timedelta
from typing import cast

from sqlalchemy import and_, or_, select, update
from sqlalchemy.engine import CursorResult

from .artifactstore import link_artifact, storage_provider
from .comparison_api import authorize_comparison
from .comparison_models import VersionComparison
from .dataset_scans import iter_parquet_batches, version_paths
from .db import SessionLocal, require_record, utcnow
from .models import Job, User, uid
from .operations_common import OperationError
from .services import audit
from .version_comparison import compact, compare_rows, scope_warnings
from .worker import LEASE_SECONDS, heartbeat

logger = logging.getLogger(__name__)


def _fence(db, identity, owner):
    job = db.scalar(select(Job).where(Job.comparison_id == identity).with_for_update())
    expiry = job.lease_until.replace(tzinfo=UTC) if job and job.lease_until else None
    if not job or job.lease_owner != owner or job.status != "RUNNING" or not expiry or expiry <= utcnow():
        raise OperationError(409, "COMPARISON_LEASE_LOST", "El intento perdió autoridad para publicar.")
    return job


def process_once(owner=None, active=None):
    owner = owner or uid()
    active = active if active is not None else {}
    now = utcnow()
    with SessionLocal() as db:
        eligible = or_(Job.status == "QUEUED", and_(Job.status == "RUNNING", Job.lease_until <= now))
        candidate = db.scalar(select(Job).where(Job.lane == "COMPARISON", eligible).order_by(Job.created_at).limit(1))
        if candidate is None:
            return False
        identity, job_id = candidate.comparison_id, candidate.id
        claimed = cast(CursorResult, db.execute(update(Job).where(Job.id == job_id, eligible).values(status="RUNNING", lease_owner=owner,
            lease_until=now + timedelta(seconds=LEASE_SECONDS), attempts=Job.attempts + 1).execution_options(synchronize_session=False)))
        if claimed.rowcount != 1:
            db.rollback()
            return False
        item = db.get(VersionComparison, identity)
        if item is None:
            raise RuntimeError("Missing comparison subject")
        item.status, item.started_at = "RUNNING", item.started_at or now
        db.commit()
        active["job_id"] = job_id
    temporary = storage_provider.temporary_path(".sqlite")
    try:
        with SessionLocal() as db:
            item = require_record(db, VersionComparison, identity)
            actor = db.get(User, item.actor_id)
            before, after = authorize_comparison(db, item, actor)
            before_paths, after_paths = version_paths(db, before), version_paths(db, after)
            schemas = [before.schema_json, after.schema_json]
            metadata = [before.ingestion_metadata or {}, after.ingestion_metadata or {}]
            settings = item.settings
            deadline = (item.started_at or now).replace(tzinfo=UTC) + timedelta(seconds=settings["timeout_seconds"])
        last_check, last_progress = [0.0], [0.0]

        def checkpoint(force=False):
            if utcnow() >= deadline:
                raise OperationError(422, "COMPARISON_TIMEOUT", "Se agotó el tiempo total de comparación.")
            if not force and time.monotonic() - last_check[0] < 0.5:
                return
            with SessionLocal() as db:
                current = require_record(db, VersionComparison, identity)
                _fence(db, identity, owner)
                if current.cancel_requested:
                    raise OperationError(409, "COMPARISON_CANCELLED", "La comparación fue cancelada.")
                authorize_comparison(db, current, db.get(User, current.actor_id))
            last_check[0] = time.monotonic()

        def progress(percent, stage):
            checkpoint()
            if time.monotonic() - last_progress[0] < 1:
                return
            with SessionLocal() as db:
                _fence(db, identity, owner)
                current = require_record(db, VersionComparison, identity)
                current.progress_percent, current.progress_stage = percent, stage
                db.commit()
            last_progress[0] = time.monotonic()

        def rows(paths, schema):
            for batch in iter_parquet_batches(paths, columns=[c["name"] for c in schema]):
                checkpoint()
                yield from batch.to_dicts()

        checkpoint(True)
        with sqlite3.connect(temporary) as connection:
            interruption: list[OperationError] = []

            def interrupt_sql():
                try:
                    checkpoint()
                except OperationError as error:
                    interruption.append(error)
                    return 1
                return 0

            connection.set_progress_handler(interrupt_sql, 10_000)
            try:
                summary = compare_rows(connection, rows(before_paths, schemas[0]), rows(after_paths, schemas[1]),
                    *schemas, keys=settings["keys"], checkpoint=checkpoint, progress=progress,
                    max_rows=settings["max_rows"], max_spill_bytes=settings["max_spill_bytes"],
                    timeout_seconds=settings["timeout_seconds"])
            except sqlite3.OperationalError:
                if interruption:
                    raise interruption[0] from None
                raise
            finally:
                connection.set_progress_handler(None, 0)
            summary["warnings"] += scope_warnings(*metadata)
            summary["before_scope"], summary["after_scope"] = metadata
            connection.execute("CREATE TABLE report_metadata(key TEXT PRIMARY KEY,value TEXT NOT NULL)")
            for key, value in {"summary": summary, "settings": settings, "before_version_id": before.id,
                               "after_version_id": after.id}.items():
                connection.execute("INSERT INTO report_metadata VALUES(?,?)", (key, compact(value)))
            connection.commit()
        checkpoint(True)
        with SessionLocal() as db:
            job = _fence(db, identity, owner)
            current = require_record(db, VersionComparison, identity)
            authorize_comparison(db, current, db.get(User, current.actor_id))
            if current.cancel_requested:
                raise OperationError(409, "COMPARISON_CANCELLED", "La comparación fue cancelada.")
            artifact = storage_provider.put_file(db, temporary, "COMPARISON_REPORT", current.organization_id,
                f"comparacion-{current.id}.sqlite", media_type="application/vnd.sqlite3")
            for version_id in (current.before_version_id, current.after_version_id):
                link_artifact(db, current.organization_id, "COMPARISON_INPUT", "VERSION_COMPARISON", current.id,
                              "DATASET_VERSION", version_id)
            link_artifact(db, current.organization_id, "COMPARISON_EVIDENCE", "ARTIFACT", artifact.id,
                          "VERSION_COMPARISON", current.id)
            current.status, current.finished_at = "SUCCESS", utcnow()
            current.summary, current.artifact_id = summary, artifact.id
            current.progress_percent, current.progress_stage = 100, "Informe completo disponible"
            job.status, job.lease_until, job.last_error = "SUCCESS", None, None
            audit(db, "VERSION_COMPARISON_COMPLETED", "version_comparison", current.id,
                  "Poblaciones completas comparadas", "Worker", current.organization_id,
                  {"artifact_id": artifact.id, "sha256": artifact.sha256, "counts": summary["counts"]})
            db.commit()
    except Exception as error:
        code = error.code if isinstance(error, OperationError) else "COMPARISON_FAILED"
        message = error.message if isinstance(error, OperationError) else "La comparación falló; las versiones permanecen intactas."
        with SessionLocal() as db:
            try:
                job = _fence(db, identity, owner)
            except OperationError:
                return True
            current = require_record(db, VersionComparison, identity)
            current.status = "CANCELLED" if code == "COMPARISON_CANCELLED" else "FAILED"
            current.error_code, current.error_message, current.finished_at = code, message, utcnow()
            current.progress_stage = message
            job.status, job.last_error, job.lease_until = current.status, code, None
            audit(db, "VERSION_COMPARISON_" + current.status, "version_comparison", current.id,
                  message, "Worker", current.organization_id, {"error_code": code})
            db.commit()
        if not isinstance(error, OperationError):
            logger.exception("Comparison execution failed")
    finally:
        temporary.unlink(missing_ok=True)
        active.pop("job_id", None)
    return True


def main():
    from .migrate import migrate
    migrate()
    owner, stop = uid(), threading.Event()
    active: dict[str, str] = {}
    for signum in (signal.SIGTERM, signal.SIGINT):
        signal.signal(signum, lambda *_: stop.set())
    thread = threading.Thread(target=heartbeat, args=(owner, stop, active, "COMPARISON"), daemon=True)
    thread.start()
    while not stop.is_set():
        if not process_once(owner, active):
            stop.wait(1)
    thread.join(timeout=2)


if __name__ == "__main__":
    main()
