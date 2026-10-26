"""Explicit replay into a new native extraction revision; no inference clients."""
from __future__ import annotations

import copy

from sqlalchemy import func, select

from .artifactstore import link_artifact, storage_provider
from .document_inference_completion import canonical_hash
from .document_models import DocumentExtraction, DocumentFamily, DocumentTemplate
from .document_recognition_store import latest_recognition_events
from .document_service import digest, issues, lock_family, owned, permission, preserve_extraction
from .models import Artifact, Job
from .operations_common import OperationError
from .services import audit


def recognition_source_context(events, source_sha256):
    terminal = [event for event in events if event["kind"] == "TERMINAL"]
    probes = [event for event in events if event["kind"] == "PHASE" and event["stage"] == "NATIVE_SOURCE_PROBE"]
    expected = probes[0].get("expected_pages") if len(probes) == 1 else None
    if type(expected) is not int or not 1 <= expected <= 50:
        expected = 0
    return {"source_sha256": source_sha256, "expected_pages": expected,
        "persisted_event_hashes": [event["durable_refs"]["event_sha256"] for event in events],
        "artifacts": [record for event in events for record in event["durable_refs"]["artifacts"]],
        "resources_released": bool(terminal and all(event.get("resources_released") is True for event in terminal))}


def replay_original(db, original, expected_extraction_id, reason, user, template_id=None):
    permission(db, user, "review")
    if not reason.strip():
        raise OperationError(422, "DOCUMENT_REPLAY_REASON", "Indica el motivo de la nueva reproducción.")
    lock_family(db, owned(db, DocumentFamily, original.family_id, user))
    db.refresh(original)
    if original.extraction_id != expected_extraction_id:
        raise OperationError(409, "DOCUMENT_REVIEW_CONFLICT", "La extracción cambió; actualiza antes de reproducir.")
    job = db.scalar(select(Job).where(Job.document_id == original.id))
    if original.status in {"QUEUED", "RUNNING"} or job and job.status in {"QUEUED", "RUNNING"}:
        raise OperationError(409, "DOCUMENT_REPLAY_RUNNING", "Espera el cierre del intento activo antes de reproducir.")
    template = owned(db, DocumentTemplate, template_id or original.template_id, user)
    if template.family_id != original.family_id or digest(template.definition) != template.definition_hash:
        raise OperationError(409, "DOCUMENT_TEMPLATE_INTEGRITY", "La plantilla de reproducción no corresponde a esta familia.")
    if template.id != original.template_id:
        permission(db, user, "configure")
    events = latest_recognition_events(db, original, hydrate=True)
    if not any(event["kind"] == "PAGE" for event in events):
        raise OperationError(409, "DOCUMENT_REPLAY_UNAVAILABLE", "Aún no hay páginas originales persistidas para reproducir.")
    artifact = owned(db, Artifact, original.artifact_id, user)
    storage_provider.materialize(artifact)
    configuration = copy.deepcopy(original.engine_snapshot or {})
    if not configuration or canonical_hash(configuration) != original.engine_snapshot_hash:
        raise OperationError(409, "DOCUMENT_ENGINE_SNAPSHOT_INTEGRITY", "La configuración guardada perdió integridad.")
    from .document_lossless import adapt_saved_recognition
    from .document_validation_policy import certify_lossless_candidate

    data = adapt_saved_recognition(events, template.definition, configuration, source_sha256=original.sha256,
        attempt_context={"replay": True, "original_attempt_id": events[-1]["attempt_id"]})
    context = recognition_source_context(events, original.sha256)
    data["engine"]["validation"] = certify_lossless_candidate(data, template.definition, original.sha256,
        source_context=context)
    previous_id = original.extraction_id
    number = db.scalar(select(func.max(DocumentExtraction.revision)).where(DocumentExtraction.document_id == original.id)) or 0
    data["engine"]["replay"] = {"previous_extraction_id": previous_id,
        "manifest_artifact_ids": [event["durable_refs"]["manifest_artifact_id"] for event in events],
        "recognition_model_calls": 0, "actor_id": user.id, "reason": reason.strip(),
        "source_template_id": original.template_id, "mapping_template_id": template.id}
    extraction = DocumentExtraction(organization_id=original.organization_id, document_id=original.id,
        template_id=template.id, revision=number + 1, previous_id=previous_id, payload=data["payload"],
        payload_hash=digest(data["payload"]), engine=data["engine"], reviewed=False, actor_id=user.id,
        reason="Reproducción desde originales persistidos: " + reason.strip())
    db.add(extraction)
    db.flush()
    preserve_extraction(db, extraction)
    original.extraction_id, original.template_id = extraction.id, template.id
    blockers = issues(extraction, template.definition)
    original.status = ("REVIEW_REQUIRED" if blockers else "AUTO_VERIFIED"
        if template.definition["publication"]["review_policy"] == "AUTO_VERIFIED" else "EXTRACTED")
    original.processing_phase = "VALIDATION"
    link_artifact(db, original.organization_id, "DOCUMENT_EXTRACTION", "DOCUMENT", original.id, "DOCUMENT_EXTRACTION", extraction.id)
    for event in events:
        link_artifact(db, original.organization_id, "DOCUMENT_REPLAY_SOURCE", "DOCUMENT_EXTRACTION", extraction.id,
            "ARTIFACT", event["durable_refs"]["manifest_artifact_id"])
    audit(db, "DOCUMENT_RECOGNITION_REPLAYED", "document_extraction", extraction.id,
        "Nueva revisión desde resultados originales persistidos, sin nueva inferencia", user.name, user.organization_id,
        {"document_id": original.id, "previous_extraction_id": previous_id, "reason": reason.strip(),
         "recognition_model_calls": 0, "mapping_template_id": template.id, "publication_dispatched": False})
    return extraction
