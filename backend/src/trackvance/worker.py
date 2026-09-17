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
from sqlalchemy.exc import OperationalError

from .artifactstore import artifact_store
from .db import SessionLocal, iso, require_record, utcnow
from .execution import ExecutionEngine, execution_engine
from .models import Job, Run, uid
from .services import audit

logger = logging.getLogger(__name__)
HEARTBEAT = artifact_store.location("worker-heartbeat.json")
LEASE_SECONDS = 90


def worker_status():
    try:
        data = json.loads(HEARTBEAT.read_text(encoding="utf-8"))
        age = (utcnow() - datetime.fromisoformat(data["last_seen"])).total_seconds()
        return {"status": "RUNNING" if age < 30 else "OFFLINE", "last_seen": data["last_seen"]}
    except (OSError, ValueError, KeyError):
        return {"status": "OFFLINE", "last_seen": None}


def heartbeat(owner, stop, active):
    while not stop.is_set():
        try:
            temporary = HEARTBEAT.with_suffix(f".{owner}.tmp")
            temporary.write_text(json.dumps({"last_seen": iso(utcnow()), "worker_id": owner}), encoding="utf-8")
            os.replace(temporary, HEARTBEAT)
            if active.get("job_id"):
                with SessionLocal() as db:
                    db.execute(update(Job).where(Job.id == active["job_id"], Job.lease_owner == owner, Job.status == "RUNNING").values(lease_until=utcnow() + timedelta(seconds=LEASE_SECONDS)))
                    db.commit()
        except (OSError, OperationalError):
            logger.warning("No se pudo actualizar el heartbeat del worker", exc_info=True)
        stop.wait(10)


def process_once(
    owner: str | None = None,
    active: dict | None = None,
    engine: ExecutionEngine | None = None,
) -> bool:
    """Process at most one job; callable by integration tests without a daemon."""
    owner, active = owner or uid(), active if active is not None else {}
    selected_engine = engine or execution_engine
    eligible = or_(Job.status == "QUEUED", and_(Job.status == "RUNNING", Job.lease_until < utcnow()))
    with SessionLocal() as db:
        job = db.scalar(select(Job).where(eligible).order_by(Job.created_at).limit(1))
        if not job:
            return False
        if job.attempts >= 3:
            run = require_record(db, Run, job.run_id)
            job.status, job.last_error = "FAILED", "El trabajo agotó los intentos tras interrupciones del worker."
            run.status, run.error, run.finished_at, run.progress_stage = "FAILED", job.last_error, utcnow(), "Falló"
            db.commit()
            return True
        claimed = db.execute(update(Job).where(Job.id == job.id, eligible).values(status="RUNNING", lease_owner=owner, lease_until=utcnow() + timedelta(seconds=LEASE_SECONDS), attempts=Job.attempts + 1).execution_options(synchronize_session=False))
        if cast(CursorResult, claimed).rowcount != 1:
            db.rollback()
            return False
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
        logger.exception("Falló ejecución %s", run_id)
        with SessionLocal() as db:
            job, run = require_record(db, Job, job_id), require_record(db, Run, run_id)
            if job.lease_owner == owner:
                message = str(exc)[:1000] or type(exc).__name__
                job.status, job.last_error, job.lease_until = "FAILED", message, None
                run.status, run.error, run.finished_at, run.progress_stage = "FAILED", message, utcnow(), "Falló"
                audit(db, "RUN_FAILED", "run", run.id, message, "Worker", run.organization_id)
                db.commit()
    finally:
        active.pop("job_id", None)
    return True


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    active: dict = {}
    stop, owner = threading.Event(), uid()
    signal.signal(signal.SIGINT, lambda *_: stop.set())
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    pulse = threading.Thread(target=heartbeat, args=(owner, stop, active), daemon=True)
    pulse.start()
    logger.info("Worker local listo. Esperando trabajos persistidos.")
    while not stop.is_set():
        try:
            if not process_once(owner, active):
                stop.wait(1)
        except OperationalError:
            logger.info("Esperando inicialización de la base de datos por la API...")
            stop.wait(3)
    pulse.join(timeout=2)
    logger.info("Worker detenido.")


if __name__ == "__main__":
    main()
