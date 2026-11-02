"""Explicit replay into a new native extraction revision; no inference clients."""
from __future__ import annotations

import copy
import uuid

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
    from .document_automatic_verification import source_context

    if any(not isinstance(event.get("durable_refs"), dict)
           or not event["durable_refs"].get("event_sha256") for event in events):
        raise OperationError(422, "DOCUMENT_RECOGNITION_INTEGRITY", "Falta la procedencia durable del reconocimiento.")
    return source_context(events, source_sha256)


def _semantic_verification_upgrade(db, original, events, before, template, configuration, job, user, reason):
    """Append an authorized CPU reuse receipt; never relabel original acquisition.

    The existing reuse factory checks the exact original request/schema/profile
    and all event hashes. Only the mapping/verification contract changes.
    """
    from .document_recognition_compatibility import approve_recognition_reuse
    from .document_recognition_store import persist_recognition_event
    from .document_verification_service import _source_snapshots

    if job is None:
        raise OperationError(409, "DOCUMENT_REPLAY_JOB_BINDING", "Falta la identidad de la adquisición conservada.")
    snapshots, definitions = _source_snapshots(db, original, configuration, template.definition)
    try:
        bindings = approve_recognition_reuse(events, source_sha256=original.sha256,
            configuration=configuration, definition=template.definition,
            configuration_snapshots=snapshots, definition_snapshots=definitions)
    except (ValueError, TypeError, KeyError) as error:
        raise OperationError(409, "DOCUMENT_SEMANTIC_REPLAY_ACQUISITION",
            "La nueva comprobación requiere conservar exactamente las entradas de adquisición.",
            {"reason": str(error)}) from None
    prior = before.definition["semantic_general"]
    current = template.definition["semantic_general"]
    information = {"version": "TRACKVANCE_SEMANTIC_VERIFICATION_REPLAY_098_V2",
        "acquisition_profile_sha256": prior["profile_sha256"],
        "acquisition_definition_sha256_by_stage": {stage: binding["definition_sha256"] for stage, binding in bindings.items()},
        "verification_contract_sha256": current["verification_contract_sha256"],
        "verification_definition_sha256": template.definition_hash,
        "previous_verification_version": prior["automatic_verification"],
        "current_verification_version": current["automatic_verification"],
        "source_template_id": before.id, "mapping_template_id": template.id,
        "recognition_model_calls_new": 0, "actor_id": user.id, "reason": reason.strip()}
    attempt = "verification-replay-" + uuid.uuid4().hex
    event = {"version": 1, "kind": "PHASE", "sequence": 0, "request_id": uuid.uuid4().hex,
        "attempt_id": attempt, "source_sha256": original.sha256,
        "configuration_sha256": canonical_hash(configuration), "definition_sha256": template.definition_hash,
        "stage": "RECOVERY_REUSE", "page_one_based": None,
        "expected_pages": next((e.get("expected_pages") for e in events if e.get("stage") == "NATIVE_SOURCE_PROBE"), None),
        "payload": {"version": information["version"], "semantic_verification_replay": information,
            "reused_event_hashes": list(dict.fromkeys(e["event_sha256"] for e in events)),
            "reused_attempt_ids": list(dict.fromkeys(e["attempt_id"] for e in events)),
            "recognition_bindings": bindings, "artifacts": []}}
    event["event_sha256"] = canonical_hash(event)
    event["durable_refs"] = persist_recognition_event(db, original, event, attempt_id=attempt,
        configuration_sha256=canonical_hash(configuration), job_id=job.id,
        worker_attempt_number=job.attempts)
    return [*events, event], bindings, information


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
    base_configuration = copy.deepcopy(original.engine_snapshot or {})
    if not base_configuration or canonical_hash(base_configuration) != original.engine_snapshot_hash:
        raise OperationError(409, "DOCUMENT_ENGINE_SNAPSHOT_INTEGRITY", "La configuración guardada perdió integridad.")
    configuration = base_configuration
    previous = owned(db, DocumentExtraction, original.extraction_id, user) if original.extraction_id else None
    if previous is not None and previous.engine.get("verification_request"):
        configuration = copy.deepcopy(previous.engine.get("configuration") or {})
        if (not configuration or canonical_hash(configuration) != previous.engine.get("configuration_sha256")):
            raise OperationError(409, "DOCUMENT_ENGINE_SNAPSHOT_INTEGRITY", "La configuración de verificación conservada perdió integridad.")
    from .document_semantic_general import assert_replay_binding

    current_template = owned(db, DocumentTemplate, original.template_id, user)
    assert_replay_binding(current_template.definition, template.definition, configuration)
    attempt_context = {"replay": True, "original_attempt_id": events[-1]["attempt_id"]}
    upgrade = None
    if ((current_template.definition.get("semantic_general") or {}).get("automatic_verification") == "SEMANTIC_AUTO_V1"
            and (template.definition.get("semantic_general") or {}).get("automatic_verification") == "SEMANTIC_AUTO_V2"):
        events, bindings, upgrade = _semantic_verification_upgrade(db, original, events, current_template,
            template, configuration, job, user, reason)
        attempt_context["recognition_bindings"] = bindings
    from .document_lossless import adapt_saved_recognition
    from .document_validation_policy import certify_lossless_candidate

    data = adapt_saved_recognition(events, template.definition, configuration, source_sha256=original.sha256,
        attempt_context=attempt_context)
    if upgrade is not None:
        data["engine"]["semantic_verification_replay"] = copy.deepcopy(upgrade)
    if previous is not None and previous.engine.get("verification_request"):
        # This immutable proof explains the per-attempt configuration on every
        # later replay; the current replay actor/reason is recorded separately.
        data["engine"]["verification_request"] = copy.deepcopy(previous.engine["verification_request"])
    context = recognition_source_context(events, original.sha256)
    from .document_semantic_general import automatic_contract

    if automatic_contract(template.definition):
        from .document_semantic_corroboration import semantic_source_context

        context = semantic_source_context(events, context)
    # Journal progress describes the original attempt, not this replay's
    # current assessment. Preserve it and refresh only the new revision.
    retained_verification = previous.engine.get("verification") if previous is not None else None
    if not isinstance(retained_verification, dict):
        retained_verification = next((event["payload"]["verification"] for event in reversed(events)
            if event.get("stage") == "AUTOMATIC_VERIFICATION"
            and isinstance(event.get("payload", {}).get("verification"), dict)), None)
    if retained_verification is not None:
        from .document_automatic_verification import decision, report

        current_decision = decision(data, template.definition, context)
        prior_decision = {"reasons": copy.deepcopy(retained_verification.get("reasons_before", [])),
            "scope": copy.deepcopy(current_decision["scope"])}
        refreshed = report(prior_decision, current_decision,
            status="REVIEW_REQUIRED" if current_decision["required"] else "CONTROLS_COMPLETED",
            primary_reused=True, calls=copy.deepcopy(retained_verification.get("recognition_calls", [])))
        refreshed.update(replay_recomputed=True, recognition_model_calls_new=0,
            source_report="PREVIOUS_EXTRACTION_OR_IMMUTABLE_JOURNAL")
        if retained_verification.get("requested_reason"):
            refreshed["requested_reason"] = retained_verification["requested_reason"]
        data["engine"]["verification"] = refreshed
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
