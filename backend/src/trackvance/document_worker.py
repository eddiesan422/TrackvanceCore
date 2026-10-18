"""Dedicated DOCUMENT lane, confined OCR child and fenced immutable completion."""
from __future__ import annotations

import json
import logging
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
from datetime import timedelta
from pathlib import Path
from typing import cast

from sqlalchemy import and_, or_, select, update
from sqlalchemy.engine import CursorResult

from .artifactstore import link_artifact, storage_provider
from .db import SessionLocal, require_record, utcnow
from .document_models import DocumentExtraction, DocumentOriginal, DocumentReference
from .document_service import (
    definition_for,
    digest,
    dispatch_individual,
    issues,
    permission,
    preserve_extraction,
)
from .models import Artifact, Job, User, uid
from .operations_common import OperationError
from .services import audit
from .worker import LEASE_SECONDS, heartbeat

logger = logging.getLogger(__name__)
TIMEOUT_SECONDS = 120


def _authority(db, document_id, owner):
    original = db.scalar(select(DocumentOriginal).where(DocumentOriginal.id == document_id).with_for_update())
    job = db.scalar(select(Job).where(Job.document_id == document_id).with_for_update())
    if not original or not job or job.lease_owner != owner or job.status != "RUNNING" or not job.lease_until:
        raise OperationError(409, "DOCUMENT_LEASE_LOST", "El intento perdió autoridad de publicación.")
    from .scheduler import aware
    if aware(job.lease_until) <= utcnow():
        raise OperationError(409, "DOCUMENT_LEASE_LOST", "El lease documental expiró.")
    if original.cancel_requested:
        raise OperationError(409, "DOCUMENT_CANCELLED", "El procesamiento documental fue cancelado.")
    user = db.get(User, original.uploaded_by_id)
    if not user or user.deleted or not user.active:
        raise OperationError(403, "DOCUMENT_ACTOR_DISABLED", "El responsable ya no está habilitado.")
    permission(db, user, "write")
    return original, job, user


def run_engine(source, definition, *, check=lambda progress: None, preview_page=None):
    if sys.platform != "linux":
        raise OperationError(503, "DOCUMENT_SANDBOX_UNAVAILABLE", "El procesamiento documental requiere el worker Linux aislado.")
    temporary = storage_provider.temporary_path(".ocr")
    folder = Path(tempfile.mkdtemp(prefix=temporary.stem + "-", dir=temporary.parent))
    process = None
    try:
        name = "original" + source.suffix.lower()
        shutil.copyfile(source, folder / name)
        (folder / "request.json").write_text(json.dumps({"filename": name, "definition": definition, "preview_page": preview_page}), encoding="utf-8")
        environment = {"PATH": "/usr/bin:/bin", "LANG": "C.UTF-8", "HOME": "/nonexistent", "TMPDIR": str(folder),
                       "OMP_THREAD_LIMIT": "1", "OMP_NUM_THREADS": "1", "TZ": "UTC"}
        runtime = Path(sys.base_prefix) / "lib"
        if runtime.is_dir():
            environment["LD_LIBRARY_PATH"] = str(runtime)
        process = subprocess.Popen([sys.executable, "-I", "-B", str(Path(__file__).with_name("document_engine.py")), str(folder)],
                                   env=environment, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                   stderr=subprocess.DEVNULL, close_fds=True, start_new_session=True)
        start = time.monotonic()
        while process.poll() is None:
            if time.monotonic() - start >= TIMEOUT_SECONDS:
                raise OperationError(422, "DOCUMENT_TIMEOUT", "La extracción excedió el tiempo total de 120 segundos.")
            progress = folder / "progress"
            check(int(progress.read_text()) if progress.exists() else 0)
            time.sleep(0.2)
        check(100)
        result = folder / "result.json"
        if not result.exists() or result.stat().st_size > 16 * 1024**2:
            raise OperationError(422, "DOCUMENT_ENGINE_FAILURE", "El motor no produjo un resultado completo dentro del límite.")
        data = json.loads(result.read_text(encoding="utf-8"))
        if data.get("error") or process.returncode:
            raise OperationError(422, data.get("error", "DOCUMENT_ENGINE_FAILURE"), "No fue posible extraer el documento; revisa el formato, límites o plantilla.")
        return data
    finally:
        if process:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            process.wait(timeout=5)
        shutil.rmtree(folder)


def process_once(owner=None, active=None):
    owner = owner or uid()
    active = active if active is not None else {}
    eligible = or_(Job.status == "QUEUED", and_(Job.status == "RUNNING", Job.lease_until < utcnow()))
    with SessionLocal() as db:
        job = db.scalar(select(Job).where(Job.lane == "DOCUMENT", eligible).order_by(Job.created_at).limit(1))
        if not job:
            return False
        result = db.execute(update(Job).where(Job.id == job.id, eligible).values(status="RUNNING", lease_owner=owner,
                            lease_until=utcnow() + timedelta(seconds=LEASE_SECONDS), attempts=Job.attempts + 1))
        if cast(CursorResult, result).rowcount != 1:
            db.rollback()
            return False
        if job.document_reference_id:
            return process_reference(db, job, owner, active)
        original = require_record(db, DocumentOriginal, job.document_id)
        document_id, job_id = original.id, job.id
        if original.cancel_requested:
            original.status, job.status = "CANCELLED", "CANCELLED"
            original.finished_at, job.lease_until = utcnow(), None
            db.commit()
            return True
        original.status, original.progress, original.started_at = "RUNNING", 0, utcnow()
        definition = definition_for(db, original).definition
        artifact = require_record(db, Artifact, original.artifact_id)
        source = storage_provider.materialize(artifact)
        db.commit()
    active["job_id"] = job_id
    last_progress, last_check = -1, 0.0

    def checkpoint(progress):
        nonlocal last_progress, last_check
        if progress == last_progress and time.monotonic() - last_check < 0.5:
            return
        with SessionLocal() as db:
            original, job, _ = _authority(db, document_id, owner)
            original.progress = min(progress, 99)
            job.lease_until = utcnow() + timedelta(seconds=LEASE_SECONDS)
            db.commit()
        last_progress, last_check = progress, time.monotonic()

    try:
        data = run_engine(source, definition, check=checkpoint)
        with SessionLocal() as db:
            original, job, user = _authority(db, document_id, owner)
            # Recovered workers never append another automatic extraction after completion.
            if not original.extraction_id:
                extraction = DocumentExtraction(organization_id=original.organization_id, document_id=original.id,
                    template_id=original.template_id, revision=1, payload=data["payload"], payload_hash=digest(data["payload"]),
                    engine=data["engine"], reviewed=False, actor_id=user.id)
                db.add(extraction)
                db.flush()
                preserve_extraction(db, extraction)
                original.extraction_id = extraction.id
                link_artifact(db, original.organization_id, "DOCUMENT_EXTRACTION", "DOCUMENT", original.id, "DOCUMENT_EXTRACTION", extraction.id)
            else:
                extraction = require_record(db, DocumentExtraction, original.extraction_id)
            original.status = "REVIEW_REQUIRED" if issues(extraction, definition) else "EXTRACTED"
            original.progress, original.finished_at, original.error_code = 100, utcnow(), None
            job.status, job.lease_until = "SUCCESS", None
            audit(db, "DOCUMENT_EXTRACTED", "document", original.id, "Extracción documental conservada", user.name,
                  user.organization_id, {"extraction_id": extraction.id, "engine": data["engine"], "sha256": extraction.payload_hash})
            db.commit()
        # Publication is separately recoverable and does not discard successful OCR.
        with SessionLocal() as db:
            original = require_record(db, DocumentOriginal, document_id)
            user = db.get(User, original.uploaded_by_id)
            dispatch_individual(db, original, user)
            db.commit()
    except OperationError as error:
        with SessionLocal() as db:
            job = require_record(db, Job, job_id)
            original = require_record(db, DocumentOriginal, document_id)
            if job.lease_owner == owner and job.status == "RUNNING":
                original.status = "CANCELLED" if original.cancel_requested else "FAILED"
                original.error_code, original.finished_at = error.code, utcnow()
                job.status, job.last_error, job.lease_until = original.status, error.code, None
                audit(db, "DOCUMENT_EXTRACTION_FAILED", "document", original.id, "La extracción requiere intervención", "Document worker",
                      original.organization_id, {"code": error.code, "attempt": job.attempts})
                db.commit()
    finally:
        active.pop("job_id", None)
    return True


def process_reference(db, job, owner, active):
    """The same fenced DOCUMENT lane reads corpus without operational documents."""
    reference = require_record(db, DocumentReference, job.document_reference_id)
    identity, job_id = reference.id, job.id
    source = storage_provider.materialize(require_record(db, Artifact, reference.artifact_id))
    reference.status = "RUNNING"
    db.commit()
    active["job_id"] = job_id

    def authority(session):
        current = session.scalar(select(Job).where(Job.id == job_id).with_for_update())
        from .scheduler import aware
        if not current or current.lease_owner != owner or current.status != "RUNNING" or not current.lease_until or aware(current.lease_until) <= utcnow():
            raise OperationError(409, "DOCUMENT_LEASE_LOST", "La lectura de referencia perdió autoridad.")
        item = require_record(session, DocumentReference, identity)
        actor = require_record(session, User, item.actor_id)
        if actor.deleted or not actor.active:
            raise OperationError(403, "DOCUMENT_ACTOR_DISABLED", "El responsable ya no está habilitado.")
        for grant in ("configure", "write", "download"):
            permission(session, actor, grant)
        return current, item, actor

    def checkpoint(_progress):
        with SessionLocal() as session:
            current, _, _ = authority(session)
            current.lease_until = utcnow() + timedelta(seconds=LEASE_SECONDS)
            session.commit()

    try:
        data = run_engine(source, {"fields": [], "tables": []}, check=checkpoint)
        with SessionLocal() as session:
            current, item, actor = authority(session)
            if item.payload is None:
                item.payload, item.payload_hash, item.engine = data["payload"], digest(data["payload"]), data["engine"]
            item.status, item.error_code = "READY", None
            current.status, current.lease_until = "SUCCESS", None
            audit(session, "DOCUMENT_REFERENCE_READ", "document_reference", item.id, "Referencia disponible para configurar; sin publicación operativa", actor.name,
                  actor.organization_id, {"sha256": item.sha256, "payload_hash": item.payload_hash, "engine": item.engine})
            session.commit()
    except OperationError as error:
        with SessionLocal() as session:
            current = require_record(session, Job, job_id)
            if current.lease_owner == owner and current.status == "RUNNING":
                item = require_record(session, DocumentReference, identity)
                item.status, item.error_code = "FAILED", error.code
                current.status, current.last_error, current.lease_until = "FAILED", error.code, None
                session.commit()
    finally:
        active.pop("job_id", None)
    return True


def main():
    owner, active, stop = uid(), dict[str, str](), threading.Event()
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    signal.signal(signal.SIGINT, lambda *_: stop.set())
    pulse = threading.Thread(target=heartbeat, args=(owner, stop, active, "DOCUMENT"), daemon=True)
    pulse.start()
    while not stop.is_set():
        try:
            if not process_once(owner, active):
                stop.wait(1)
        except Exception:
            logger.exception("DOCUMENT_WORKER_FAILURE")
            stop.wait(1)
    pulse.join(timeout=5)


if __name__ == "__main__":
    main()
