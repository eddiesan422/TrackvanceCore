"""Native, fenced verification attempts over conserved recognition evidence.

The original acquisition snapshot and all extraction revisions stay immutable.
A new request freezes the explicitly configured verification settings in the
existing recognition journal and queues the existing DOCUMENT job once.
"""
from __future__ import annotations

import copy
import uuid
from typing import Any

from sqlalchemy import select

from .document_models import DocumentExtraction, DocumentFamily, DocumentTemplate
from .document_providers import EngineConfiguration, hash_value, settings_for
from .models import Job
from .operations_common import OperationError

REQUEST_VERSION = "TRACKVANCE_VERIFICATION_REQUEST_098_V1"


def _configuration(db, original):
    item = settings_for(db, original.organization_id, original.family_id)
    values = EngineConfiguration.model_validate(item.configuration if item else {}).model_dump()
    return {**values, "configuration_version": item.version if item else 0,
        "scope": "FAMILY" if item and item.scope_key else "ORGANIZATION",
        "family_id": item.scope_key if item and item.scope_key else None}


def _requests(db, original):
    from .document_recognition_store import recognition_journal

    return [row["event"] for row in recognition_journal(db, original)
        if row["event"].get("kind") == "PHASE" and row["event"].get("stage") == "VERIFICATION_REQUEST"]


def verification_action_dto(db, original, user):
    """Explain a user action without model probes, writes or fabricated review."""
    from .document_quality import quality_report
    from .document_service import permission

    result: dict[str, Any] = {"available": False, "configuration_version": None, "blocked_reason": None}
    if user is None:
        return result
    try:
        permission(db, user, "write")
        permission(db, user, "review")
        if not original.extraction_id:
            result["blocked_reason"] = "Espera a que exista una extracción antes de verificarla."
            return result
        configuration = _configuration(db, original)
        result["configuration_version"] = configuration["configuration_version"]
        if original.status in {"QUEUED", "RUNNING"}:
            result["blocked_reason"] = "La extracción o su verificación ya está en curso."
        elif (configuration.get("provider") != "PADDLE_QWEN_LOCAL" or configuration.get("extraction_strategy", "LEGACY") == "LEGACY"
                or configuration.get("automatic_verification") != "BOUNDED_LOCAL_V1"):
            result["blocked_reason"] = "Activa la verificación automática local en la configuración de esta familia."
        else:
            extraction = db.get(DocumentExtraction, original.extraction_id)
            template = db.get(DocumentTemplate, original.template_id)
            if extraction is None or template is None:
                raise OperationError(409, "DOCUMENT_VERIFICATION_SOURCE", "La extracción conservada requiere atención.")
            if template.definition.get("validation_policy") is None:
                result["blocked_reason"] = "Declara la política de validación de esta plantilla antes de solicitar otra lectura."
            elif quality_report(extraction, template.definition)["status"] != "REVIEW_REQUIRED":
                result["blocked_reason"] = "Esta extracción ya tiene sus controles resueltos."
            elif (extraction.engine.get("route") == "PADDLE_STRUCTURED" and not configuration.get("use_llm")
                    and not extraction.payload.get("candidate_history")):
                result["blocked_reason"] = "El control independiente necesita Qwen local; habilítalo en una nueva configuración de la familia."
            elif any(event["payload"].get("configuration_sha256") == hash_value(configuration)
                    for event in _requests(db, original)):
                result["blocked_reason"] = "Ya se intentó esta verificación. Revisa las incidencias que siguen pendientes."
            else:
                result["available"] = True
    except OperationError as error:
        result["blocked_reason"] = error.message
    return result


def _source_snapshots(db, original, configuration, definition):
    configurations = {hash_value(configuration): copy.deepcopy(configuration)}
    if original.engine_snapshot is not None:
        if hash_value(original.engine_snapshot) != original.engine_snapshot_hash:
            raise OperationError(409, "DOCUMENT_ENGINE_SNAPSHOT_INTEGRITY", "La configuración original perdió integridad.")
        configurations[original.engine_snapshot_hash] = copy.deepcopy(original.engine_snapshot)
    for extraction in db.scalars(select(DocumentExtraction).where(
            DocumentExtraction.document_id == original.id,
            DocumentExtraction.organization_id == original.organization_id)):
        candidate = extraction.engine.get("configuration")
        if isinstance(candidate, dict):
            configurations[hash_value(candidate)] = copy.deepcopy(candidate)
    definitions = {hash_value(definition): copy.deepcopy(definition)}
    for template in db.scalars(select(DocumentTemplate).where(DocumentTemplate.family_id == original.family_id,
            DocumentTemplate.organization_id == original.organization_id)):
        if hash_value(template.definition) != template.definition_hash:
            raise OperationError(409, "DOCUMENT_TEMPLATE_INTEGRITY", "Una plantilla conservada perdió integridad.")
        definitions[template.definition_hash] = copy.deepcopy(template.definition)
    return configurations, definitions


def queue_verification(db, original, *, expected_extraction_id, expected_settings_version, reason, user):
    from .document_recognition_compatibility import approve_recognition_reuse
    from .document_recognition_store import latest_recognition_events, persist_recognition_event
    from .document_service import owned, permission
    from .services import audit

    permission(db, user, "write")
    permission(db, user, "review")
    if not reason.strip():
        raise OperationError(422, "DOCUMENT_VERIFICATION_REASON", "Indica el motivo de la verificación.")
    # Same family/original/job ordering as the existing native operations.
    family = owned(db, DocumentFamily, original.family_id, user)
    db.scalar(select(DocumentFamily).where(DocumentFamily.id == family.id).with_for_update())
    db.refresh(original, with_for_update=True)
    job = db.scalar(select(Job).where(Job.document_id == original.id,
        Job.organization_id == original.organization_id).with_for_update())
    if job is None or job.lane != "DOCUMENT":
        raise OperationError(409, "DOCUMENT_JOB_MISSING", "No se puede verificar la ejecución documental.")
    configuration = _configuration(db, original)
    if configuration["configuration_version"] != expected_settings_version:
        raise OperationError(409, "DOCUMENT_ENGINE_CONFIGURATION_CONFLICT", "La configuración cambió; actualiza antes de verificar.")
    if (configuration.get("provider") != "PADDLE_QWEN_LOCAL" or configuration.get("extraction_strategy", "LEGACY") == "LEGACY"
            or configuration.get("automatic_verification") != "BOUNDED_LOCAL_V1"):
        raise OperationError(409, "DOCUMENT_VERIFICATION_DISABLED", "Activa la verificación automática local para esta familia.")
    configuration_sha = hash_value(configuration)
    previous_requests = _requests(db, original)
    same = [event for event in previous_requests if event["payload"].get("expected_extraction_id") == expected_extraction_id
        and event["payload"].get("configuration_sha256") == configuration_sha]
    if same:
        return {"idempotent": True, "attempt_id": same[-1]["attempt_id"], "job_id": job.id}
    if original.extraction_id != expected_extraction_id:
        raise OperationError(409, "DOCUMENT_REVIEW_CONFLICT", "La extracción cambió; actualiza antes de verificar.")
    if not original.extraction_id or original.status in {"QUEUED", "RUNNING"} or job.status in {"QUEUED", "RUNNING"}:
        raise OperationError(409, "DOCUMENT_VERIFICATION_RUNNING", "Espera el cierre del intento activo antes de verificar.")
    if any(event["payload"].get("configuration_sha256") == configuration_sha for event in previous_requests):
        raise OperationError(409, "DOCUMENT_VERIFICATION_ALREADY_ATTEMPTED", "Esta configuración ya agotó su verificación; revisa las incidencias pendientes.")
    action = verification_action_dto(db, original, user)
    if not action["available"]:
        raise OperationError(409, "DOCUMENT_VERIFICATION_NOT_ACTIONABLE", action["blocked_reason"] or "No hay una verificación disponible.")
    template = owned(db, DocumentTemplate, original.template_id, user)
    if template.family_id != family.id or hash_value(template.definition) != template.definition_hash:
        raise OperationError(409, "DOCUMENT_TEMPLATE_INTEGRITY", "La plantilla conservada perdió integridad.")
    events = latest_recognition_events(db, original, hydrate=True)
    if not any(event.get("kind") == "PAGE" for event in events):
        raise OperationError(409, "DOCUMENT_VERIFICATION_UNAVAILABLE", "No hay resultados originales compatibles para verificar.")
    snapshots, definitions = _source_snapshots(db, original, configuration, template.definition)
    try:
        bindings = approve_recognition_reuse(events, source_sha256=original.sha256, configuration=configuration,
            definition=template.definition, configuration_snapshots=snapshots, definition_snapshots=definitions)
    except ValueError as error:
        raise OperationError(409, "DOCUMENT_VERIFICATION_REUSE_UNAVAILABLE",
            "La lectura conservada no es compatible con esta verificación; necesita una nueva extracción autorizada.",
            {"reason": str(error)}) from None
    number = job.attempts + 1
    attempt_id = f"{job.id}-{number}"
    hashes = list(dict.fromkeys(event["event_sha256"] for event in events))
    payload = {"version": REQUEST_VERSION, "expected_extraction_id": expected_extraction_id,
        "template_id": template.id, "configuration": configuration, "configuration_sha256": configuration_sha,
        "actor_id": user.id, "reason": reason.strip(), "reused_event_hashes": hashes,
        "reused_attempt_ids": list(dict.fromkeys(event["attempt_id"] for event in events)),
        "recognition_bindings": bindings}
    event = {"version": 1, "kind": "PHASE", "sequence": 0, "request_id": uuid.uuid4().hex,
        "attempt_id": attempt_id, "source_sha256": original.sha256, "configuration_sha256": configuration_sha,
        "definition_sha256": template.definition_hash, "stage": "VERIFICATION_REQUEST", "page_one_based": None,
        "expected_pages": next((e.get("expected_pages") for e in events if e.get("stage") == "NATIVE_SOURCE_PROBE"), None),
        "payload": payload}
    event["event_sha256"] = hash_value(event)
    persist_recognition_event(db, original, event, attempt_id=attempt_id, configuration_sha256=configuration_sha,
        job_id=job.id, worker_attempt_number=number)
    original.status, original.progress, original.cancel_requested = "QUEUED", 0, False
    original.error_code, original.started_at, original.finished_at = None, None, None
    original.processing_phase = "VALIDATION"
    job.status, job.lease_owner, job.lease_until, job.last_error = "QUEUED", None, None, None
    family.lock_version += 1
    audit(db, "DOCUMENT_VERIFICATION_QUEUED", "document", original.id,
        "Verificación automática acotada desde evidencia conservada; sin revisión humana", user.name,
        original.organization_id, {"extraction_id": expected_extraction_id, "config_hash": configuration_sha,
            "attempt": number, "reason": reason.strip()})
    return {"idempotent": False, "attempt_id": attempt_id, "job_id": job.id}
