"""Database-backed local worker with atomic claims and crash recovery."""

import json
import logging
import os
import signal
import threading
from datetime import datetime, timedelta
from typing import cast

from sqlalchemy import and_, or_, select, update
from sqlalchemy.engine import CursorResult
from sqlalchemy.exc import IntegrityError, OperationalError

from .artifactstore import artifact_store
from .db import SessionLocal, iso, require_record, utcnow
from .execution import ExecutionEngine, execution_engine
from .models import DeliveryAttempt, Job, Run, uid
from .scheduler import ScheduleError, tick
from .services import audit

logger = logging.getLogger(__name__)
LEASE_SECONDS = 90
WORKER_LANES = frozenset({"DEFAULT", "DELIVERY"})
DELIVERY_TERMINAL_STATUSES = frozenset(
    {"SUCCESS", "FAILED", "FAILED_PRECONDITION", "UNKNOWN", "CANCELLED"}
)


def configured_lane(raw: str | None = None) -> str:
    configured = raw if raw is not None else (os.getenv("TRACKVANCE_WORKER_LANE") or "DEFAULT")
    lane = configured.strip().upper()
    if lane not in WORKER_LANES:
        raise RuntimeError("TRACKVANCE_WORKER_LANE debe ser DEFAULT o DELIVERY.")
    return lane


def heartbeat_path(lane: str):
    checked = configured_lane(lane)
    return artifact_store.location(f"worker-heartbeat-{checked.lower()}.json")


def worker_status(lane: str = "DEFAULT"):
    heartbeat_file = heartbeat_path(lane)
    try:
        data = json.loads(heartbeat_file.read_text(encoding="utf-8"))
        age = (utcnow() - datetime.fromisoformat(data["last_seen"])).total_seconds()
        return {
            "status": "RUNNING" if age < 30 else "OFFLINE",
            "last_seen": data["last_seen"],
            "lane": configured_lane(lane),
        }
    except (OSError, ValueError, KeyError):
        return {"status": "OFFLINE", "last_seen": None, "lane": configured_lane(lane)}


def heartbeat(owner, stop, active, lane="DEFAULT"):
    heartbeat_file = heartbeat_path(lane)
    while not stop.is_set():
        try:
            temporary = heartbeat_file.with_suffix(f".{owner}.tmp")
            temporary.write_text(
                json.dumps(
                    {
                        "last_seen": iso(utcnow()),
                        "worker_id": owner,
                        "lane": configured_lane(lane),
                    }
                ),
                encoding="utf-8",
            )
            os.replace(temporary, heartbeat_file)
            if active.get("job_id"):
                with SessionLocal() as db:
                    db.execute(update(Job).where(Job.id == active["job_id"], Job.lease_owner == owner, Job.status == "RUNNING").values(lease_until=utcnow() + timedelta(seconds=LEASE_SECONDS)))
                    db.commit()
        except (OSError, OperationalError):
            logger.warning("No se pudo actualizar el heartbeat del worker", exc_info=True)
        stop.wait(10)


def _latest_delivery_attempt(db, run_id: str) -> DeliveryAttempt | None:
    return db.scalar(
        select(DeliveryAttempt)
        .where(DeliveryAttempt.run_id == run_id)
        .order_by(DeliveryAttempt.attempt_number.desc())
    )


def _reconcile_durable_delivery_state(db, job: Job, run: Run) -> bool:
    """Repair the queue projection without replaying a durable remote outcome."""
    attempt = _latest_delivery_attempt(db, run.id)
    if attempt and attempt.status == "COMMITTED":
        job.status, job.last_error, job.lease_until = "SUCCESS", None, None
        run.status, run.decision, run.error = "SUCCESS", "COMMITTED", None
        run.finished_at = run.finished_at or attempt.finished_at or utcnow()
        run.progress_percent = 100
        if run.evidence_path:
            run.progress_stage = "Completado"
        else:
            run.progress_stage = "Completado; evidencia local pendiente"
            run.metrics = {
                **(run.metrics or {}),
                "evidence_status": "PENDING_REPAIR",
            }
        return True
    if attempt and attempt.status in {"STARTED", "UNKNOWN"}:
        was_started = attempt.status == "STARTED"
        message = (
            attempt.error_message
            or "Trackvance no pudo confirmar el resultado del intento remoto."
        )
        attempt.status = "UNKNOWN"
        attempt.error_code = attempt.error_code or "WORKER_CONFIRMATION_LOST"
        attempt.error_message = message
        attempt.finished_at = attempt.finished_at or utcnow()
        job.status, job.last_error, job.lease_until = "UNKNOWN", message, None
        run.status, run.decision, run.error = "UNKNOWN", "UNKNOWN", message
        run.finished_at = run.finished_at or utcnow()
        run.progress_stage = "Confirmación remota desconocida"
        if was_started:
            audit(
                db,
                "DELIVERY_UNKNOWN",
                "delivery_attempt",
                attempt.id,
                message,
                "Worker",
                run.organization_id,
                {"status": "UNKNOWN", "error_code": attempt.error_code},
                run_id=run.id,
            )
        return True
    if attempt and attempt.status == "FAILED":
        message = attempt.error_message or run.error or "La entrega remota falló."
        job.status, job.last_error, job.lease_until = "FAILED", message, None
        run.status, run.decision, run.error = "FAILED", "FAILED", message
        run.finished_at = run.finished_at or attempt.finished_at or utcnow()
        run.progress_stage = "Entrega fallida"
        return True
    if run.status in DELIVERY_TERMINAL_STATUSES:
        if run.status == "SUCCESS" and run.decision != "COMMITTED":
            return False
        job.status, job.lease_until = run.status, None
        job.last_error = run.error if run.status not in {"SUCCESS", "CANCELLED"} else None
        return True
    return False


def process_once(
    owner: str | None = None,
    active: dict | None = None,
    engine: ExecutionEngine | None = None,
    lane: str = "DEFAULT",
) -> bool:
    """Process at most one job; callable by integration tests without a daemon."""
    owner, active = owner or uid(), active if active is not None else {}
    lane = configured_lane(lane)
    selected_engine = engine or execution_engine
    eligible = or_(Job.status == "QUEUED", and_(Job.status == "RUNNING", Job.lease_until < utcnow()))
    with SessionLocal() as db:
        job = db.scalar(
            select(Job).where(eligible, Job.lane == lane).order_by(Job.created_at).limit(1)
        )
        if not job:
            return False
        # Every path that can race with API cancellation locks Run before Job.
        # Preserve that global order while claiming an expired delivery.
        run = db.scalar(select(Run).where(Run.id == job.run_id).with_for_update())
        if run is None:
            raise LookupError(f"Run {job.run_id} no existe")
        # Own the eligible projection before interpreting any durable Delivery
        # outcome. A previous owner may renew its lease after our SELECT; the
        # conditional UPDATE is the linearization point that prevents a second
        # worker from converting its live STARTED attempt to UNKNOWN.
        claimed = db.execute(
            update(Job)
            .where(Job.id == job.id, eligible)
            .values(
                status="RUNNING",
                lease_owner=owner,
                lease_until=utcnow() + timedelta(seconds=LEASE_SECONDS),
            )
            .execution_options(synchronize_session=False)
        )
        if cast(CursorResult, claimed).rowcount != 1:
            db.rollback()
            return False
        db.refresh(job)
        db.refresh(run)
        if job.lane == "DELIVERY" and _reconcile_durable_delivery_state(
            db, job, run
        ):
            db.commit()
            return True
        if job.attempts >= 3:
            job.status, job.last_error = "FAILED", "El trabajo agotó los intentos tras interrupciones del worker."
            job.lease_until = None
            run.status, run.error, run.finished_at, run.progress_stage = "FAILED", job.last_error, utcnow(), "Falló"
            db.commit()
            return True
        job.attempts += 1
        job_id, run_id = job.id, job.run_id
        db.commit()
        active["job_id"] = job_id
    try:
        with SessionLocal() as db:
            run = require_record(db, Run, run_id)
            selected_engine.execute(db, run, lease_owner=owner)
            job = require_record(db, Job, job_id)
            job.status, job.lease_until = run.status, None
            db.commit()
        logger.info("Ejecución %s completada", run_id)
    except Exception as exc:
        with SessionLocal() as db:
            job, run = require_record(db, Job, job_id), require_record(db, Run, run_id)
            if job.lease_owner == owner:
                if job.lane == "DELIVERY" and _reconcile_durable_delivery_state(
                    db, job, run
                ):
                    logger.error(
                        "La entrega %s se reconcilió desde su estado durable", run_id
                    )
                else:
                    logger.exception("Falló ejecución %s", run_id)
                    message = str(exc)[:1000] or type(exc).__name__
                    job.status, job.last_error, job.lease_until = "FAILED", message, None
                    run.status, run.error = "FAILED", message
                    run.finished_at, run.progress_stage = utcnow(), "Falló"
                    audit(db, "RUN_FAILED", "run", run.id, message, "Worker", run.organization_id)
                db.commit()
    finally:
        active.pop("job_id", None)
    return True


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    active: dict = {}
    lane = configured_lane()
    stop, owner = threading.Event(), uid()
    signal.signal(signal.SIGINT, lambda *_: stop.set())
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    pulse = threading.Thread(target=heartbeat, args=(owner, stop, active, lane), daemon=True)
    pulse.start()
    logger.info("Worker local listo. Esperando trabajos persistidos en lane=%s.", lane)
    while not stop.is_set():
        try:
            if lane == "DEFAULT":
                try:
                    tick()
                except (ScheduleError, ValueError, IntegrityError):
                    # Scheduler metadata errors must not prevent execution of independent jobs.
                    logger.error("No se pudo despachar Sentinel; revisa la integridad de las programaciones.")
            if not process_once(owner, active, lane=lane):
                stop.wait(1)
        except OperationalError:
            logger.info("Esperando inicialización de la base de datos por la API...")
            stop.wait(3)
    pulse.join(timeout=2)
    logger.info("Worker detenido.")


if __name__ == "__main__":
    main()
