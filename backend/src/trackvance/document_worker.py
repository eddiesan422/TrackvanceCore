"""Dedicated DOCUMENT lane, confined OCR child and fenced immutable completion."""
from __future__ import annotations

import json
import logging
import os
import shutil
import signal
import stat
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

from .artifactstore import ArtifactIntegrityError, link_artifact, storage_provider
from .db import SessionLocal, require_record, utcnow
from .document_models import DocumentExtraction, DocumentOriginal, DocumentReference
from .document_providers import execute_provider, freeze_configuration
from .document_quality import certify_candidate, quality_report
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


def _read_engine_progress(path, previous):
    """Read only a bounded regular child file; an empty write keeps last progress."""
    descriptor = None
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise ValueError("not a regular progress file")
        raw = os.read(descriptor, 4)
        if not raw:
            return previous
        if len(raw) > 3 or not raw.isdigit() or int(raw) > 100:
            raise ValueError("invalid progress")
        return max(previous, int(raw))
    except FileNotFoundError:
        return previous
    except (OSError, ValueError):
        raise OperationError(422, "DOCUMENT_ENGINE_FAILURE", "El motor produjo un indicador de progreso inválido.") from None
    finally:
        if descriptor is not None:
            os.close(descriptor)


def run_engine(source, definition, *, check=lambda progress: None, preview_page=None,
               native_only=False, candidate_lines=None):
    if sys.platform != "linux":
        raise OperationError(503, "DOCUMENT_SANDBOX_UNAVAILABLE", "El procesamiento documental requiere el worker Linux aislado.")
    temporary = storage_provider.temporary_path(".ocr")
    folder = Path(tempfile.mkdtemp(prefix=temporary.stem + "-", dir=temporary.parent))
    process = None
    try:
        name = "original" + source.suffix.lower()
        shutil.copyfile(source, folder / name)
        request = {"filename": name, "definition": definition, "preview_page": preview_page}
        if native_only:
            request["native_only"] = True
        if candidate_lines is not None:
            request["candidate_lines"] = candidate_lines
        (folder / "request.json").write_text(json.dumps(request, allow_nan=False), encoding="utf-8")
        environment = {"PATH": "/usr/bin:/bin", "LANG": "C.UTF-8", "HOME": "/nonexistent", "TMPDIR": str(folder),
                       "OMP_THREAD_LIMIT": "1", "OMP_NUM_THREADS": "1", "TZ": "UTC"}
        runtime = Path(sys.base_prefix) / "lib"
        if runtime.is_dir():
            environment["LD_LIBRARY_PATH"] = str(runtime)
        process = subprocess.Popen([sys.executable, "-I", "-B", str(Path(__file__).with_name("document_engine.py")), str(folder)],
                                   env=environment, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                   stderr=subprocess.DEVNULL, close_fds=True, start_new_session=True)
        start, last_progress = time.monotonic(), 0
        while process.poll() is None:
            if time.monotonic() - start >= TIMEOUT_SECONDS:
                raise OperationError(422, "DOCUMENT_TIMEOUT", "La extracción excedió el tiempo total de 120 segundos.")
            last_progress = _read_engine_progress(folder / "progress", last_progress)
            check(last_progress)
            time.sleep(0.2)
        check(100)
        result = folder / "result.json"
        if not result.exists() or result.stat().st_size > 16 * 1024**2:
            raise OperationError(422, "DOCUMENT_ENGINE_FAILURE", "El motor no produjo un resultado completo dentro del límite.")
        try:
            data = json.loads(result.read_text(encoding="utf-8"))
        except (ValueError, UnicodeError):
            raise OperationError(422, "DOCUMENT_ENGINE_FAILURE", "El motor no produjo un resultado JSON válido.") from None
        if not isinstance(data, dict):
            raise OperationError(422, "DOCUMENT_ENGINE_FAILURE", "El motor no produjo un resultado documental válido.")
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
        original.processing_phase = "EXTRACTION"
        try:
            configuration = freeze_configuration(db, original)
            definition = definition_for(db, original).definition
        except OperationError as error:
            original.status, original.error_code, original.finished_at = "FAILED", error.code, utcnow()
            job.status, job.last_error, job.lease_until = "FAILED", error.code, None
            audit(db, "DOCUMENT_EXTRACTION_FAILED", "document", original.id, "La configuración congelada requiere intervención",
                "Document worker", original.organization_id, {"code": error.code, "attempt": job.attempts})
            db.commit()
            return True
        artifact = require_record(db, Artifact, original.artifact_id)
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
        source = storage_provider.materialize(artifact)
        data = execute_provider(source, definition, configuration, legacy_runner=run_engine, check=checkpoint)
        if definition["publication"]["review_policy"] == "AUTO_VERIFIED":
            with SessionLocal() as db:
                current, _, _ = _authority(db, document_id, owner)
                current.processing_phase = "VALIDATION"
                db.commit()
            # This second, confined extraction is a control of the same original;
            # it is never accepted from the provider or the language model.
            try:
                independent = run_engine(source, definition, check=checkpoint)
                data["engine"]["validation"] = certify_candidate(data, independent, definition, artifact.sha256)
            except OperationError as error:
                if error.status != 422:
                    raise
                # A failed independent control prevents automatic approval, but
                # the successfully extracted candidate remains durable/reviewable.
                data["engine"]["validation_failure"] = {"error_code": error.code,
                    "phase": "INDEPENDENT_VALIDATION", "automatic_approval_blocked": True}
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
            blockers = issues(extraction, definition)
            original.status = ("REVIEW_REQUIRED" if blockers else "AUTO_VERIFIED"
                if definition["publication"]["review_policy"] == "AUTO_VERIFIED" else "EXTRACTED")
            original.progress, original.finished_at, original.error_code = 100, utcnow(), None
            job.status, job.lease_until = "SUCCESS", None
            audit(db, "DOCUMENT_EXTRACTED", "document", original.id, "Extracción documental conservada", user.name,
                  user.organization_id, {"extraction_id": extraction.id, "provider": configuration["provider"],
                      "config_hash": original.engine_snapshot_hash, "sha256": extraction.payload_hash})
            if original.status == "AUTO_VERIFIED":
                report = quality_report(extraction, definition)
                audit(db, "DOCUMENT_AUTO_VERIFIED", "document_extraction", extraction.id,
                    "Controles independientes satisfactorios; sin revisión humana declarada", user.name,
                    user.organization_id, {"status": report["status"], "validator_version": report["validator_version"],
                        "extraction_id": extraction.id, "validation_sha256": data["engine"]["validation"]["attestation_sha256"],
                        "independent_extraction": True, "config_hash": original.engine_snapshot_hash})
            db.commit()
        # Publication is separately recoverable and does not discard successful OCR.
        with SessionLocal() as db:
            original = require_record(db, DocumentOriginal, document_id)
            user = db.get(User, original.uploaded_by_id)
            dispatch_individual(db, original, user)
            db.commit()
    except (OperationError, ArtifactIntegrityError) as error:
        error_code = error.code if isinstance(error, OperationError) else "DOCUMENT_ARTIFACT_INTEGRITY"
        with SessionLocal() as db:
            job = require_record(db, Job, job_id)
            original = require_record(db, DocumentOriginal, document_id)
            if job.lease_owner == owner and job.status == "RUNNING":
                original.status = "CANCELLED" if original.cancel_requested else "FAILED"
                original.error_code, original.finished_at = error_code, utcnow()
                job.status, job.last_error, job.lease_until = original.status, error_code, None
                audit(db, "DOCUMENT_EXTRACTION_FAILED", "document", original.id, "La extracción requiere intervención", "Document worker",
                      original.organization_id, {"code": error_code, "attempt": job.attempts})
                db.commit()
    finally:
        active.pop("job_id", None)
    return True


def process_reference(db, job, owner, active):
    """The same fenced DOCUMENT lane reads corpus without operational documents."""
    reference = require_record(db, DocumentReference, job.document_reference_id)
    identity, job_id = reference.id, job.id
    artifact = require_record(db, Artifact, reference.artifact_id)
    reference.status = "RUNNING"
    try:
        configuration = freeze_configuration(db, reference)
    except OperationError as error:
        reference.status, reference.error_code = "FAILED", error.code
        job.status, job.last_error, job.lease_until = "FAILED", error.code, None
        db.commit()
        return True
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
        source = storage_provider.materialize(artifact)
        data = execute_provider(source, {"fields": [], "tables": []}, configuration,
            legacy_runner=run_engine, check=checkpoint)
        with SessionLocal() as session:
            current, item, actor = authority(session)
            if item.payload is None:
                item.payload, item.payload_hash, item.engine = data["payload"], digest(data["payload"]), data["engine"]
            item.status, item.error_code = "READY", None
            current.status, current.lease_until = "SUCCESS", None
            audit(session, "DOCUMENT_REFERENCE_READ", "document_reference", item.id, "Referencia disponible para configurar; sin publicación operativa", actor.name,
                  actor.organization_id, {"sha256": item.sha256, "payload_hash": item.payload_hash, "engine": item.engine})
            session.commit()
    except (OperationError, ArtifactIntegrityError) as error:
        error_code = error.code if isinstance(error, OperationError) else "DOCUMENT_ARTIFACT_INTEGRITY"
        with SessionLocal() as session:
            current = require_record(session, Job, job_id)
            if current.lease_owner == owner and current.status == "RUNNING":
                item = require_record(session, DocumentReference, identity)
                item.status, item.error_code = "FAILED", error_code
                current.status, current.last_error, current.lease_until = "FAILED", error_code, None
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
