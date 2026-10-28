"""Document lifecycle, immutable reviews, reservations and native publications."""
from __future__ import annotations

import copy
import hashlib
import json
from datetime import datetime, timedelta
from types import SimpleNamespace

import polars as pl
from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from .artifactstore import link_artifact, storage_provider
from .dataset_readers import DatasetReadResult
from .db import iso, require_record, utcnow
from .document_engine import arithmetic_checks, cross_table_checks, validate_value
from .document_header_review import (
    append_confirmations,
    header_review_dto,
    invalidate_confirmations,
    reject_initial_resolutions,
    resolved_observations,
)
from .document_inference_completion import (
    all_cells_reviewed,
    canonical_hash,
    completion_issues,
    confirmation_binding,
    marker_hash,
    unresolved_completion,
)
from .document_models import (
    DocumentBatch,
    DocumentBatchMember,
    DocumentExtraction,
    DocumentFamily,
    DocumentOriginal,
    DocumentPublicationClaim,
    DocumentTemplate,
)
from .document_schemas import DocumentDefinition, structures
from .models import Configuration, Dataset, User
from .operations_common import OperationError
from .permissions import effective_permissions
from .scheduler import aware
from .services import audit, create_version

MAX_BYTES = 32 * 1024**2
MAX_BATCH_DOCUMENTS = 1000


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode()).hexdigest()


def owned(db, model, identity, user):
    item = db.get(model, identity)
    if item is None or item.organization_id != user.organization_id:
        raise OperationError(404, "DOCUMENT_NOT_FOUND", "No se encontró el recurso documental.")
    return item


def permission(db, user, action):
    if f"documents:{action}" not in effective_permissions(db, user):
        raise OperationError(403, "DOCUMENT_FORBIDDEN", "Tu rol no permite esta operación documental.")


def lock_family(db, family):
    # A physical write serializes SQLite too; PostgreSQL takes the row lock.
    db.execute(update(DocumentFamily).where(DocumentFamily.id == family.id).values(lock_version=DocumentFamily.lock_version + 1))
    db.refresh(family)


def definition_for(db, original):
    template = db.get(DocumentTemplate, original.template_id)
    if template is None or digest(template.definition) != template.definition_hash:
        raise OperationError(409, "DOCUMENT_TEMPLATE_INTEGRITY", "No se puede verificar la plantilla congelada.")
    return template


def extraction_for(db, original):
    item = require_record(db, DocumentExtraction, original.extraction_id) if original.extraction_id else None
    if item and digest(item.payload) != item.payload_hash:
        raise OperationError(409, "DOCUMENT_EXTRACTION_INTEGRITY", "La extracción perdió su integridad.")
    if item and not item.reviewed:
        reject_initial_resolutions(item.payload)
    return item


def issues(extraction, definition, *, source_binding=None, ignore_incomplete=False):
    if extraction is None:
        return ["EXTRACTION_PENDING"]
    payload = extraction.payload
    policy = definition["publication"]
    result = []
    if getattr(extraction, "engine", {}).get("lossless_contract_version") == 1:
        execution = extraction.engine.get("execution", {})
        if execution.get("recognition_complete") is not True:
            result.append("RECOGNITION_INCOMPLETE")
        if execution.get("resources_released") is not True:
            result.append("RECOGNITION_CLOSURE_UNVERIFIED")
    if not ignore_incomplete:
        result.extend(completion_issues(extraction, definition))
    from .document_quality import cell_is_valid, semantic_issues
    result.extend(semantic_issues(payload, reviewed=getattr(extraction, "reviewed", False)))
    # The application validator recomputes checks; provider-authored arithmetic
    # statuses cannot authorize publication.
    for check in [*arithmetic_checks(payload, definition), *cross_table_checks(payload, definition)]:
        if check["status"] != "VALID":
            result.append(f"ARITHMETIC_{check['status']}:{check['name']}:{check.get('row', 'ALL')}")
    if policy["review_policy"] == "AUTO_VERIFIED":
        from .document_quality import quality_report
        result.extend(quality_report(extraction, definition, ignore_incomplete=ignore_incomplete)["issues"])
    resolved = (resolved_observations(payload, definition, reviewed=getattr(extraction, "reviewed", False),
                source_binding=source_binding) if payload.get("header_resolutions") else set())
    for name, fields in structures(definition).items():
        rows = [payload["fields"]] if name == "headers" else payload["tables"].get(name, [])
        table = next((t for t in definition["tables"] if t["name"] == name), None)
        if not rows and table and table["required"]:
            result.append(f"TABLE_REQUIRED:{name}")
        if any(region.get("header_observation", {}).get("observation_id") not in resolved
               for region in payload.get("table_unparsed", {}).get(name, [])):
            result.append(f"TABLE_ROWS_UNRECOGNIZED:{name}")
        for row_index, row in enumerate(rows):
            for column, field in enumerate(fields):
                cell = row.get(field["name"])
                path = f"{name}.{row_index}.{field['name']}"
                if not cell or not cell_is_valid(cell, field):
                    result.append("VALUE_INVALID:" + path)
                    continue
                confidence = cell.get("confidence")
                declared_start = ((table or {}).get("visual") or {}).get("row_start")
                needs_review = policy["review_policy"] != "AUTO_VERIFIED" and (policy["review_policy"] == "ALL" or field["critical"]
                                or declared_start is not None and declared_start["column"] == column
                                or confidence is not None and confidence < policy["minimum_confidence"])
                if needs_review and not cell.get("reviewed"):
                    result.append("REVIEW_REQUIRED:" + path)
    declared_tables = {table["name"] for table in definition["tables"]}
    for name, regions in payload.get("table_unparsed", {}).items():
        if name not in declared_tables and regions:
            result.append(f"TABLE_ROWS_UNRECOGNIZED:{name}")
    return list(dict.fromkeys(result))


def template_dto(item):
    return {"id": item.id, "revision": item.revision, "definition": item.definition,
            "sha256": item.definition_hash, "tested_extraction_id": item.tested_extraction_id,
            "created_at": iso(item.created_at)}


def original_dto(db, original, *, detail=False, user=None):
    extraction = extraction_for(db, original)
    result = {"id": original.id, "family_id": original.family_id, "template_id": original.template_id,
              "filename": original.filename, "sha256": original.sha256, "received_at": iso(original.received_at),
              "status": original.status, "progress": original.progress, "error_code": original.error_code,
              "duplicate_of_id": original.duplicate_of_id, "correction_of_id": original.correction_of_id,
              "is_example": original.is_example, "extraction_id": original.extraction_id,
              "started_at": iso(original.started_at), "finished_at": iso(original.finished_at),
              "engine_snapshot": ({**original.engine_snapshot, "snapshot_sha256": original.engine_snapshot_hash}
                  if original.engine_snapshot else None),
              "phase": (original.processing_phase if original.status == "RUNNING" and original.processing_phase else {"RUNNING": "EXTRACTION", "EXTRACTED": "VALIDATION", "REVIEWED": "PUBLICATION",
                  "AUTO_VERIFIED": "PUBLICATION", "PUBLISHED": "COMPLETE"}.get(original.status, original.status))}
    if extraction and extraction.engine.get("inference_completion"):
        marker = extraction.engine["inference_completion"]
        pending = unresolved_completion(extraction)
        result["incomplete_inference"] = {"status": "PARTIAL" if pending else "HUMAN_RESOLVED",
            "failed_stage": marker["failed_stage"], "error_code": marker["error_code"],
            "automatic_approval_blocked": True, "requires_full_source_review": pending, "can_confirm": False}
    if original.engine_snapshot and original.engine_snapshot.get("extraction_strategy", "LEGACY") != "LEGACY":
        from .document_recognition_store import recognition_metadata

        execution, recognition_artifacts = recognition_metadata(db, original)
        if execution is not None:
            execution["validation_complete"] = bool(extraction and original.status not in {"QUEUED", "RUNNING"})
            result["execution"] = execution
        if detail:
            result["recognition_artifacts"] = recognition_artifacts
    if detail:
        if user is not None:
            from .document_verification_service import verification_action_dto

            result["verification_action"] = verification_action_dto(db, original, user)
        pending_verification = None
        if original.status in {"QUEUED", "RUNNING"}:
            from .document_recognition_store import verification_request_for

            pending_verification = verification_request_for(db, original)
        if pending_verification:
            result["verification_configuration"] = {
                **copy.deepcopy(pending_verification["payload"]["configuration"]),
                "snapshot_sha256": pending_verification["configuration_sha256"],
                "base_snapshot_sha256": original.engine_snapshot_hash}
        elif extraction and extraction.engine.get("verification_request"):
            result["verification_configuration"] = {
                **copy.deepcopy(extraction.engine["configuration"]),
                "snapshot_sha256": extraction.engine["configuration_sha256"],
                "base_snapshot_sha256": original.engine_snapshot_hash}
        result["template"] = template_dto(definition_for(db, original))
        result["extraction"] = extraction_dto(extraction) if extraction else None
        result["issues"] = issues(extraction, result["template"]["definition"])
        if result["template"]["definition"]["publication"]["review_policy"] == "AUTO_VERIFIED" or extraction and extraction.engine.get("inference_completion"):
            from .document_quality import quality_report
            result["quality"] = quality_report(extraction, result["template"]["definition"])
        if extraction and extraction.engine.get("inference_completion"):
            marker = extraction.engine["inference_completion"]
            pending = unresolved_completion(extraction)
            result["incomplete_inference"] = {"status": "PARTIAL" if pending else "HUMAN_RESOLVED",
                "failed_stage": marker["failed_stage"], "error_code": marker["error_code"],
                "automatic_approval_blocked": True, "requires_full_source_review": pending,
                "can_confirm": bool(pending and extraction.reviewed and all_cells_reviewed(extraction.payload)
                    and not issues(extraction, result["template"]["definition"], ignore_incomplete=True))}
            if pending:
                result["incomplete_inference"]["confirmation_binding"] = confirmation_binding(original, extraction)
            else:
                resolution = extraction.payload["inference_completion_resolution"]
                result["incomplete_inference"]["resolution"] = {
                    key: resolution[key] for key in ("actor_id", "reason", "created_at")}
        if extraction:
            header_review = header_review_dto(extraction.payload, result["template"]["definition"], reviewed=extraction.reviewed)
            if header_review is not None:
                result["header_review"] = header_review
        result["revisions"] = [{"id": e.id, "revision": e.revision, "reviewed": e.reviewed,
                                "actor_id": e.actor_id, "reason": e.reason, "created_at": iso(e.created_at)}
                               for e in db.scalars(select(DocumentExtraction).where(DocumentExtraction.document_id == original.id)
                                                   .order_by(DocumentExtraction.revision))]
        claims = db.scalars(select(DocumentPublicationClaim).where(DocumentPublicationClaim.document_id == original.id)).all()
        result["publications"] = [{"batch_id": c.batch_id, "extraction_id": c.extraction_id,
                                    "versions": db.get(DocumentBatch, c.batch_id).published_versions} for c in claims]
    return result


def extraction_dto(item):
    return {"id": item.id, "revision": item.revision, "previous_id": item.previous_id,
            "payload": item.payload, "sha256": item.payload_hash, "engine": item.engine,
            "reviewed": item.reviewed, "actor_id": item.actor_id, "reason": item.reason,
            "created_at": iso(item.created_at), "artifact_id": item.artifact_id}


def preserve_extraction(db, extraction):
    temporary = storage_provider.temporary_path(".json")
    try:
        temporary.write_text(json.dumps({"schema_version": 1, "extraction_id": extraction.id,
            "document_id": extraction.document_id, "template_id": extraction.template_id,
            "revision": extraction.revision, "previous_id": extraction.previous_id,
            "payload": extraction.payload, "payload_hash": extraction.payload_hash, "engine": extraction.engine,
            "actor_id": extraction.actor_id, "reason": extraction.reason}, ensure_ascii=False, sort_keys=True), encoding="utf-8")
        artifact = storage_provider.put_file(db, temporary, "DOCUMENT_EXTRACTION", extraction.organization_id,
                                           "extraction.json", media_type="application/json")
        extraction.artifact_id = artifact.id
        link_artifact(db, extraction.organization_id, "DOCUMENT_EXTRACTION_RESULT", "DOCUMENT_EXTRACTION", extraction.id, "ARTIFACT", artifact.id)
    finally:
        temporary.unlink(missing_ok=True)


def family_dto(db, family):
    template = db.get(DocumentTemplate, family.active_template_id) if family.active_template_id else None
    return {"id": family.id, "name": family.name, "description": family.description,
            "responsible_user_id": family.responsible_user_id, "active_template_id": family.active_template_id,
            "output_datasets": family.output_datasets, "next_cut_at": iso(family.next_cut_at),
            "publication": template.definition["publication"] if template else None,
            "document_count": db.scalar(select(func.count()).select_from(DocumentOriginal).where(DocumentOriginal.family_id == family.id, DocumentOriginal.is_example.is_(False))),
            "pending_count": db.scalar(select(func.count()).select_from(DocumentOriginal).where(DocumentOriginal.family_id == family.id,
                                          DocumentOriginal.is_example.is_(False),
                                          DocumentOriginal.status.in_(["QUEUED", "RUNNING", "REVIEW_REQUIRED", "FAILED"])))}


def create_template(db, family, definition, user):
    permission(db, user, "configure")
    lock_family(db, family)
    normalized = DocumentDefinition.model_validate(definition).model_dump(mode="json")
    revision = (db.scalar(select(func.max(DocumentTemplate.revision)).where(DocumentTemplate.family_id == family.id)) or 0) + 1
    item = DocumentTemplate(organization_id=user.organization_id, family_id=family.id, revision=revision,
                            definition=normalized, definition_hash=digest(normalized), created_by_id=user.id)
    db.add(item)
    db.flush()
    audit(db, "DOCUMENT_TEMPLATE_CREATED", "document_template", item.id, "Revisión de plantilla creada", user.name,
          user.organization_id, {"family_id": family.id, "revision": revision, "sha256": item.definition_hash})
    return item


def activate_template(db, family, template, test_extraction_id, user):
    permission(db, user, "configure")
    lock_family(db, family)
    test = owned(db, DocumentExtraction, test_extraction_id, user)
    original = owned(db, DocumentOriginal, test.document_id, user)
    if template.family_id != family.id or test.template_id != template.id or not original.is_example or issues(test, template.definition):
        raise OperationError(422, "DOCUMENT_TEMPLATE_TEST_REQUIRED", "Prueba y revisa un ejemplo completo con esta revisión antes de activarla.")
    activate_verified_template(db, family, template, user)
    template.tested_extraction_id = test.id
    audit(db, "DOCUMENT_TEMPLATE_ACTIVATED", "document_family", family.id, "Plantilla probada activada para nuevas recepciones", user.name,
          user.organization_id, {"template_id": template.id, "test_extraction_id": test.id})


def activate_verified_template(db, family, template, user):
    """Shared binding/schema checks after a legacy extraction or exact builder test."""
    permission(db, user, "configure")
    current = db.get(DocumentTemplate, family.active_template_id) if family.active_template_id else None
    signature = lambda value: {name: [(f["name"], f["logical_type"]) for f in fields] for name, fields in structures(value).items()}
    if current and signature(current.definition) != signature(template.definition):
        raise OperationError(422, "DOCUMENT_FAMILY_SCHEMA", "La familia debe conservar un esquema común; crea otra familia para estructuras incompatibles.")
    settings = template.definition["publication"]
    required = set(structures(template.definition))
    if settings["stop_after"] != "PUBLICATION" and set(settings["intake_configuration_ids"]) != required:
        raise OperationError(422, "DOCUMENT_INTAKE_STRUCTURES", "Cada estructura requiere contrato Intake nativo.")
    for name, config_id in settings["intake_configuration_ids"].items():
        permission(db, user, "publish")
        config = owned(db, Configuration, config_id, user)
        if config.module != "intake" or config.dataset_id != family.output_datasets.get(name):
            raise OperationError(422, "DOCUMENT_INTAKE_CONFIGURATION", "Vincula un contrato del dataset estable de esta estructura.")
    if settings["automatic_delivery"]:
        from .automation_models import DeliveryAutomation, DeliveryAutomationVersion
        if not {"delivery:configure", "delivery:schedule", "intake:execute"}.issubset(effective_permissions(db, user)):
            raise OperationError(403, "DOCUMENT_DELIVERY_PERMISSION", "La entrega automática requiere permisos nativos de Intake y Delivery.")
        for name, automation_id in settings["delivery_automation_ids"].items():
            automation = owned(db, DeliveryAutomation, automation_id, user)
            revision = db.scalar(select(DeliveryAutomationVersion).where(DeliveryAutomationVersion.automation_id == automation.id)
                                 .order_by(DeliveryAutomationVersion.version.desc()))
            if not revision or revision.settings.get("mode") != "CHAINED" or revision.settings.get("intake_configuration_id") != settings["intake_configuration_ids"].get(name):
                raise OperationError(422, "DOCUMENT_DELIVERY_CONFIGURATION", "La automatización debe consumir la salida aprobada del contrato vinculado.")
    family.active_template_id = template.id
    family.next_cut_at = datetime.fromisoformat(settings["starts_at"]) + timedelta(seconds=settings["interval_seconds"]) if settings["mode"] == "SCHEDULED" else None
    if settings["stop_after"] != "PUBLICATION":
        from .intake_automation import IntakeScheduleSettings, save_schedule, schedule_for
        if not {"intake:schedule", "intake:configure", "intake:execute"}.issubset(effective_permissions(db, user)):
            raise OperationError(403, "DOCUMENT_INTAKE_PERMISSION", "La activación del flujo exige permisos nativos para configurar, programar y ejecutar Intake.")
        for config_id in settings["intake_configuration_ids"].values():
            config = owned(db, Configuration, config_id, user)
            schedule = schedule_for(db, config)
            save_schedule(db, config, user, IntakeScheduleSettings(mode="EVENT", timezone=settings["timezone"], starts_at=utcnow()),
                          enabled=True, responsible_user_id=family.responsible_user_id,
                          expected_version=schedule.version if schedule else None)


def prepare_pipeline(db, family, user, stop_after, intake_bindings, delivery_bindings, automatic_delivery, *, as_draft=False):
    """Create/link native contracts; return a new template, still requiring a test."""
    permission(db, user, "configure")
    if not family.active_template_id or not family.output_datasets:
        raise OperationError(422, "DOCUMENT_PIPELINE_INITIAL_PUBLICATION", "Publica primero un documento revisado para crear los datasets estables.")
    grants = effective_permissions(db, user)
    if stop_after != "PUBLICATION" and "intake:configure" not in grants:
        raise OperationError(403, "DOCUMENT_INTAKE_PERMISSION", "Se requiere permiso nativo para configurar Intake.")
    definition = copy.deepcopy(db.get(DocumentTemplate, family.active_template_id).definition)
    bindings = {}
    if stop_after != "PUBLICATION":
        from .config_semantics import validate_config
        for name, fields in structures(definition).items():
            config_id = intake_bindings.get(name)
            if config_id:
                config = owned(db, Configuration, config_id, user)
                if config.module != "intake" or config.dataset_id != family.output_datasets[name]:
                    raise OperationError(422, "DOCUMENT_INTAKE_CONFIGURATION", "El contrato debe usar el dataset estable de su estructura.")
            else:
                keys = [f["name"] for f in fields if f["business_key"]]
                config = Configuration(organization_id=user.organization_id, module="intake",
                    name=f"{family.name} · {name} · validación"[:160], dataset_id=family.output_datasets[name], owner=user.name,
                    description="Contrato nativo creado desde el asistente documental; revisar reglas antes de activar.",
                    config=validate_config("intake", {"schema_version": 2, "rules": [], "transforms": [],
                        "required_columns": [f["name"] for f in fields if f["required"]],
                        "unique_columns": keys if len(keys) == 1 and name == "headers" else [],
                        "numeric_columns": [f["name"] for f in fields if f["logical_type"] in {"DECIMAL", "INT64"}],
                        "positive_columns": [], "max_error_rate": 0}))
                db.add(config)
                db.flush()
                audit(db, "CONFIGURATION_PUBLISHED", "configuration", config.id, "Contrato Intake creado por asistente documental", user.name,
                      user.organization_id, {"module": "intake", "family_id": family.id, "structure": name, "version": 1})
            bindings[name] = config.id
    definition["publication"].update(stop_after=stop_after, intake_configuration_ids=bindings,
        delivery_automation_ids=delivery_bindings, automatic_delivery=automatic_delivery)
    if as_draft:
        from .document_builder_workspace import create_draft, save_draft
        draft = create_draft(db, family.id, user, family.active_template_id, "Flujo posterior · borrador")
        return save_draft(db, draft, draft.version, definition, [], draft.name, user)
    return create_template(db, family, definition, user)


def review(db, original, expected_extraction_id, changes, confirmed, reason, user, table_rows=None, header_confirmations=None,
           incomplete_source_confirmation=None):
    permission(db, user, "review")
    if header_confirmations:
        permission(db, user, "download")
    family = owned(db, DocumentFamily, original.family_id, user)
    lock_family(db, family)
    db.refresh(original)
    previous = extraction_for(db, original)
    if not previous or previous.id != expected_extraction_id:
        raise OperationError(409, "DOCUMENT_REVIEW_CONFLICT", "La extracción cambió; actualiza antes de revisar.")
    if not reason.strip():
        raise OperationError(422, "DOCUMENT_REVIEW_REASON", "Registra el motivo de la revisión.")
    definition = definition_for(db, original).definition
    payload = copy.deepcopy(previous.payload)
    if incomplete_source_confirmation is not None:
        if (changes or confirmed or table_rows or header_confirmations or not previous.reviewed
                or not unresolved_completion(previous)):
            raise OperationError(422, "DOCUMENT_INCOMPLETE_REVIEW_REQUIRED", "Guarda primero la revisión completa de todas las celdas y regiones del original.")
        permission(db, user, "download")
        if not user.active or user.deleted:
            raise OperationError(403, "DOCUMENT_ACTOR_DISABLED", "El responsable ya no está habilitado.")
        marker = previous.engine["inference_completion"]
        binding = confirmation_binding(original, previous)
        acknowledgement = {key: value for key, value in incomplete_source_confirmation.items()
            if key not in {"all_pages_confirmed", "all_rows_confirmed"}}
        if (acknowledgement != binding or incomplete_source_confirmation.get("all_pages_confirmed") is not True
                or incomplete_source_confirmation.get("all_rows_confirmed") is not True
                or marker.get("source_sha256") != original.sha256
                or marker.get("definition_sha256") != canonical_hash(definition)):
            raise OperationError(409, "DOCUMENT_INCOMPLETE_REVIEW_CONFLICT", "La extracción, el original o la fase pendiente cambiaron; actualiza antes de confirmar.")
        if not all_cells_reviewed(payload) or issues(previous, definition, ignore_incomplete=True):
            raise OperationError(422, "DOCUMENT_INCOMPLETE_REVIEW_REQUIRED", "Resuelve todos los valores, filas, regiones y controles antes de confirmar el original completo.")
        payload["inference_completion_resolution"] = {"version": 1, "marker_sha256": marker_hash(marker),
            "source_sha256": original.sha256, "definition_sha256": canonical_hash(definition),
            "pages": marker["source_pages"], "all_pages_confirmed": True, "all_rows_confirmed": True,
            "actor_id": user.id, "reason": reason.strip(), "created_at": iso(utcnow()),
            "acknowledged_extraction_id": previous.id, "acknowledged_payload_hash": previous.payload_hash,
            "acknowledged_revision": previous.revision}
    elif changes or confirmed or table_rows or header_confirmations:
        payload.pop("inference_completion_resolution", None)
    if header_confirmations and (changes or confirmed or table_rows):
        raise OperationError(422, "DOCUMENT_HEADER_CELLS_REQUIRED", "Guarda primero la revisión de valores; confirma después el encabezado exacto.")
    if header_confirmations and not previous.reviewed:
        raise OperationError(422, "DOCUMENT_HEADER_CELLS_REQUIRED", "Guarda primero la revisión humana de todos los valores.")
    if table_rows and any(region.get("header_observation") for regions in payload.get("table_unparsed", {}).values() for region in regions):
        raise OperationError(422, "DOCUMENT_HEADER_SOURCE_CHANGED", "Las observaciones de encabezado conservan las filas y regiones originales.")
    for name, rows in (table_rows or {}).items():
        table = next((t for t in definition["tables"] if t["name"] == name), None)
        if not table or len(rows) > 10000 or any(set(row) != {f["name"] for f in table["fields"]} for row in rows):
            raise OperationError(422, "DOCUMENT_REVIEW_TABLE", "La tabla corregida debe conservar sus columnas y hasta 10000 filas.")
        corrected = []
        for row in rows:
            cells = {}
            for field in table["fields"]:
                raw = row[field["name"]]
                if raw is not None and (not isinstance(raw, str) or len(raw) > 65536):
                    raise OperationError(422, "DOCUMENT_REVIEW_VALUE", "Conserva texto exacto en las celdas corregidas.")
                if previous.engine.get("lossless_contract_version") == 1:
                    from .document_normalization import normalize_observation

                    value, validity, normalization = normalize_observation(raw, field)
                else:
                    value, validity = validate_value(raw, field)
                    normalization = None
                cells[field["name"]] = {"value": value, "validity": validity, "observed": None,
                    "origin": None, "confidence": None, "reviewed": validity == "VALID", "corrected": True,
                    **({"correction_normalization": normalization, "correction_input": raw} if normalization else {})}
            corrected.append(cells)
        payload["tables"][name] = corrected
        payload.setdefault("table_unparsed", {})[name] = []
    all_cells = {}
    for name, fields in structures(definition).items():
        rows = [payload["fields"]] if name == "headers" else payload["tables"].get(name, [])
        for index, row in enumerate(rows):
            for field in fields:
                all_cells[f"{name}.{index}.{field['name']}"] = (row, field)
    if (set(changes) | set(confirmed)) - set(all_cells):
        raise OperationError(422, "DOCUMENT_REVIEW_PATH", "La revisión contiene una celda inexistente.")
    for path, (row, field) in all_cells.items():
        cell = row[field["name"]]
        if path in changes:
            raw = changes[path]
            if raw is not None and (not isinstance(raw, str) or len(raw) > 65536):
                raise OperationError(422, "DOCUMENT_REVIEW_VALUE", "Las correcciones conservan texto exacto hasta 65536 caracteres.")
            if previous.engine.get("lossless_contract_version") == 1:
                from .document_normalization import normalize_observation

                value, validity, normalization = normalize_observation(raw, field)
                cell.update(correction_normalization=normalization, correction_input=raw)
            else:
                value, validity = validate_value(raw, field)
            cell.update(value=value, validity=validity, reviewed=False)
            # The region proves the observed source, never a newly invented value.
            cell["corrected"] = value != cell.get("observed")
            if cell["corrected"]:
                cell["observed_origin"] = cell.get("observed_origin", cell.get("origin"))
                cell["origin"], cell["confidence"] = None, None
        if path in confirmed:
            if cell["validity"] != "VALID":
                raise OperationError(422, "DOCUMENT_REVIEW_INVALID", "Corrige o completa los valores inválidos antes de confirmarlos.")
            cell["reviewed"] = True
            if cell.get("alternatives"):
                cell["alternatives"] = []
    if definition.get("arithmetic") or definition.get("invoice_controls"):
        payload["arithmetic"] = arithmetic_checks(payload, definition)
    if definition.get("cross_totals"):
        payload["cross_totals"] = cross_table_checks(payload, definition)
    if definition.get("business_period_field"):
        field = definition["business_period_field"]
        payload["business_period"] = {"field": field, **payload["fields"][field]}
    if changes or confirmed:
        payload = invalidate_confirmations(payload, user.id, iso(utcnow()), reason)
    if header_confirmations:
        automatic = previous
        seen: set[str] = set()
        while automatic.reviewed:
            if not automatic.previous_id or automatic.id in seen or len(seen) >= 1000:
                raise OperationError(409, "DOCUMENT_HEADER_INTEGRITY", "No se puede verificar la extracción automática original.")
            seen.add(automatic.id)
            automatic = owned(db, DocumentExtraction, automatic.previous_id, user)
            if (automatic.document_id != original.id or automatic.template_id != original.template_id
                or digest(automatic.payload) != automatic.payload_hash):
                raise OperationError(409, "DOCUMENT_HEADER_INTEGRITY", "La cadena de revisiones perdió su integridad.")
        if automatic.document_id != original.id or automatic.template_id != original.template_id:
            raise OperationError(409, "DOCUMENT_HEADER_INTEGRITY", "La extracción automática pertenece a otro origen.")
        payload = append_confirmations(payload, automatic.payload, definition, header_confirmations,
            {"kind": "DOCUMENT", "document_id": original.id, "source_sha256": original.sha256,
             "template_id": original.template_id, "definition_hash": digest(definition),
             "automatic_extraction_id": automatic.id, "automatic_payload_hash": automatic.payload_hash},
            user.id, iso(utcnow()), reason)
    if incomplete_source_confirmation is not None:
        pending = issues(SimpleNamespace(payload=payload, engine=previous.engine, reviewed=True), definition)
        if pending:
            raise OperationError(422, "DOCUMENT_INCOMPLETE_REVIEW_REQUIRED", "La confirmación conserva controles pendientes; revisa el original.")
    item = DocumentExtraction(organization_id=user.organization_id, document_id=original.id, template_id=original.template_id,
                              revision=previous.revision + 1, previous_id=previous.id, payload=payload, payload_hash=digest(payload),
                              engine=previous.engine, actor_id=user.id, reason=reason.strip(), reviewed=True)
    db.add(item)
    db.flush()
    preserve_extraction(db, item)
    original.extraction_id = item.id
    original.status = "REVIEW_REQUIRED" if issues(item, definition) else "REVIEWED"
    audit(db, "DOCUMENT_REVIEW_RECORDED", "document_extraction", item.id, "Revisión humana inmutable registrada", user.name,
          user.organization_id, {"document_id": original.id, "previous_id": previous.id,
                                 "changed_paths": sorted(changes), "confirmed_paths": confirmed, "reason": reason,
                                 **({"header_confirmations": payload["header_resolutions"][-len(header_confirmations):]} if header_confirmations else {})})
    if incomplete_source_confirmation is not None:
        audit(db, "DOCUMENT_INCOMPLETE_SOURCE_CONFIRMED", "document_extraction", item.id,
            "Original completo confirmado por una persona; fase IA incompleta conservada", user.name,
            user.organization_id, {"document_id": original.id, "previous_id": previous.id,
                "confirmation": payload["inference_completion_resolution"], "status": "HUMAN_VERIFIED"})
    return item


def batch_dto(db, batch):
    return {"id": batch.id, "family_id": batch.family_id, "template_id": batch.template_id, "status": batch.status,
            "idempotency_key": batch.idempotency_key, "window_start": iso(batch.window_start), "window_end": iso(batch.window_end),
            "settings": batch.settings, "wait_until": iso(batch.wait_until), "blocked_reason": batch.blocked_reason,
            "scope": "PUBLICATION_BATCH_ONLY", "published_versions": batch.published_versions,
            "manifest_hash": batch.manifest_hash,
            "members": [{"document_id": m.document_id, "extraction_id": m.extraction_id, "status": m.status, "reason": m.reason}
                        for m in db.scalars(select(DocumentBatchMember).where(DocumentBatchMember.batch_id == batch.id))]}


def open_batch(db, family, template, user, key, *, document_ids=None, start=None, end=None, correction=False):
    permission(db, user, "publish")
    lock_family(db, family)
    existing = db.scalar(select(DocumentBatch).where(DocumentBatch.family_id == family.id, DocumentBatch.idempotency_key == key))
    request_hash = digest({"template_id": template.id, "documents": sorted(document_ids) if document_ids is not None else None,
                           "start": iso(start), "end": iso(end), "correction": correction})
    if existing:
        if existing.settings.get("request_hash") != request_hash:
            raise OperationError(409, "DOCUMENT_IDEMPOTENCY_CONFLICT", "La clave de publicación ya pertenece a otra composición.")
        return existing
    query = select(DocumentOriginal).where(DocumentOriginal.family_id == family.id, DocumentOriginal.template_id == template.id,
                                           DocumentOriginal.is_example.is_(False), DocumentOriginal.duplicate_of_id.is_(None))
    if document_ids is not None:
        query = query.where(DocumentOriginal.id.in_(document_ids))
    elif start is not None and end is not None:
        query = query.where(DocumentOriginal.received_at >= start, DocumentOriginal.received_at < end)
    else:
        raise OperationError(422, "DOCUMENT_BATCH_SELECTION", "Selecciona documentos o una ventana explícita.")
    originals = db.scalars(query.order_by(DocumentOriginal.received_at, DocumentOriginal.id).limit(MAX_BATCH_DOCUMENTS + 1)).all()
    if len(originals) > MAX_BATCH_DOCUMENTS:
        raise OperationError(422, "DOCUMENT_BATCH_LIMIT", "El lote excede 1000 documentos; reduce la ventana.")
    if document_ids is not None and len(originals) != len(set(document_ids)):
        raise OperationError(422, "DOCUMENT_BATCH_MEMBERS", "Hay documentos incompatibles, ejemplos o duplicados idénticos en la selección.")
    settings = copy.deepcopy(template.definition["publication"])
    batch = DocumentBatch(organization_id=user.organization_id, family_id=family.id, template_id=template.id,
                          idempotency_key=key, window_start=start, window_end=end,
                          settings={**settings, "correction": correction, "timezone": settings["timezone"], "request_hash": request_hash},
                          actor_id=user.id, wait_until=utcnow() + timedelta(seconds=settings["wait_seconds"]))
    db.add(batch)
    db.flush()
    for original in originals:
        previous = db.scalar(select(DocumentPublicationClaim.id).where(DocumentPublicationClaim.document_id == original.id).limit(1))
        if previous and not correction:
            continue
        if (previous or original.correction_of_id) and not correction:
            raise OperationError(409, "DOCUMENT_CORRECTION_EXPLICIT", "La corrección exige publicación manual explícita y revisión de impacto en Delivery.")
        db.add(DocumentBatchMember(organization_id=user.organization_id, batch_id=batch.id, document_id=original.id))
    db.flush()
    audit(db, "DOCUMENT_BATCH_CREATED", "document_batch", batch.id, "Composición de documentos reservada", user.name,
          user.organization_id, {"family_id": family.id, "window_start": iso(start), "window_end": iso(end), "correction": correction})
    return batch


def publish_batch(db: Session, batch: DocumentBatch, user: User, *, seal_only=False):
    permission(db, user, "publish")
    if "datasets:write" not in effective_permissions(db, user):
        raise OperationError(403, "DOCUMENT_DATASET_PERMISSION", "La publicación requiere permiso de creación de datasets.")
    family = owned(db, DocumentFamily, batch.family_id, user)
    lock_family(db, family)
    db.refresh(batch)
    if batch.status in {"PUBLISHED", "EMPTY"}:
        return batch
    template = db.get(DocumentTemplate, batch.template_id)
    if template is None or digest(template.definition) != template.definition_hash:
        raise OperationError(409, "DOCUMENT_TEMPLATE_INTEGRITY", "No se pudo verificar la plantilla.")
    members = db.scalars(select(DocumentBatchMember).where(DocumentBatchMember.batch_id == batch.id).order_by(DocumentBatchMember.document_id)).all()
    ready, pending = [], []
    if batch.sealed_manifest:
        if digest(batch.sealed_manifest) != batch.manifest_hash:
            raise OperationError(409, "DOCUMENT_BATCH_INTEGRITY", "La composición sellada perdió integridad.")
        ready = [m for m in members if m.status == "RESERVED"]
    else:
        for member in members:
            original = require_record(db, DocumentOriginal, member.document_id)
            extraction = extraction_for(db, original)
            blockers = issues(extraction, template.definition)
            if original.status in {"FAILED", "CANCELLED", "QUEUED", "RUNNING"}:
                blockers = [original.error_code or original.status] + blockers
            if not blockers:
                claim = db.scalar(select(DocumentPublicationClaim).where(DocumentPublicationClaim.extraction_id == extraction.id))
                if claim and claim.batch_id != batch.id:
                    if batch.settings["correction"]:
                        member.status, member.reason = "PENDING", "CORRECTION_REQUIRES_NEW_REVIEW"
                        pending.append(member)
                        continue
                    member.status, member.reason = "EXCLUDED", "ALREADY_PUBLISHED"
                    continue
                member.extraction_id = extraction.id
                ready.append(member)
            else:
                member.status, member.reason = "PENDING", blockers[0]
                pending.append(member)
        if pending and (batch.settings["close_policy"] == "STRICT" or batch.settings["correction"]):
            batch.status = "BLOCKED" if batch.settings["correction"] or utcnow() >= aware(batch.wait_until) else "WAITING"
            batch.blocked_reason = "CORRECTION_REVIEW_REQUIRED" if batch.settings["correction"] else "EXTRACTION_OR_REVIEW_REQUIRED"
            return batch
        for member in pending:
            member.status, member.reason = "DEFERRED", member.reason
        if not ready:
            batch.status, batch.blocked_reason = ("EMPTY", "NO_READY_DOCUMENTS") if not pending else ("BLOCKED", "NO_READY_DOCUMENTS")
            return batch
        # Business duplicate detection is complete across this batch and prior publications.
        key_fields = [field["name"] for field in template.definition["fields"] if field["business_key"]]
        keys = {}
        if key_fields and not batch.settings["correction"]:
            previous = db.scalars(select(DocumentPublicationClaim).join(DocumentOriginal, DocumentOriginal.id == DocumentPublicationClaim.document_id)
                                  .where(DocumentOriginal.family_id == family.id)).all()
            for claim in previous:
                payload = require_record(db, DocumentExtraction, claim.extraction_id).payload
                keys[tuple(payload["fields"][k]["value"] for k in key_fields)] = claim.document_id
        for member in ready:
            extraction = require_record(db, DocumentExtraction, member.extraction_id)
            if key_fields:
                key = tuple(extraction.payload["fields"][k]["value"] for k in key_fields)
                if key in keys and keys[key] != member.document_id:
                    batch.status, batch.blocked_reason = "BLOCKED", "BUSINESS_DUPLICATE_REQUIRES_DECISION"
                    return batch
                keys[key] = member.document_id
        manifest = {"schema_version": 1, "family_id": family.id, "template_id": template.id,
                    "template_hash": template.definition_hash, "definition": template.definition,
                    "settings": batch.settings, "scope": "PUBLICATION_BATCH_ONLY",
                    "members": [{"document_id": m.document_id, "extraction_id": m.extraction_id,
                                  "payload_hash": require_record(db, DocumentExtraction, m.extraction_id).payload_hash} for m in ready]}
        batch.sealed_manifest, batch.manifest_hash = manifest, digest(manifest)
        for member in ready:
            member.status, member.reason = "RESERVED", None
            db.add(DocumentPublicationClaim(organization_id=user.organization_id, document_id=member.document_id,
                                           extraction_id=member.extraction_id, batch_id=batch.id))
        batch.status, batch.blocked_reason = "SEALED", None
        db.flush()
    if seal_only:
        return batch
    outputs = dict(family.output_datasets)
    published = {}
    total_rows = total_cells = total_content_bytes = 0
    for name, fields in structures(template.definition).items():
        records = []
        provenance = []
        for member in ready:
            extraction = require_record(db, DocumentExtraction, member.extraction_id)
            if extraction is None or digest(extraction.payload) != extraction.payload_hash:
                raise OperationError(409, "DOCUMENT_EXTRACTION_INTEGRITY", "La revisión sellada perdió integridad.")
            rows = [extraction.payload["fields"]] if name == "headers" else extraction.payload["tables"].get(name, [])
            for index, row in enumerate(rows):
                total_rows += 1
                total_cells += len(fields) + 4
                total_content_bytes += sum(len(str(row[f["name"]]["value"] or "").encode()) for f in fields)
                if total_rows > 100000 or total_cells > 500000 or total_content_bytes > MAX_BYTES:
                    raise OperationError(422, "DOCUMENT_PUBLICATION_LIMIT", "La publicación excede 100000 filas, 500000 celdas o 32 MiB de contenido; requiere una composición menor.")
                records.append({"document_id": member.document_id, "extraction_id": extraction.id, "batch_id": batch.id,
                                **({"row_number": str(index + 1)} if name != "headers" else {}),
                                **{field["name"]: row[field["name"]]["value"] for field in fields}})
                provenance.append({"record_number": len(records), "document_id": member.document_id,
                                   "extraction_id": extraction.id, "source_row": index,
                                   "fields": {f["name"]: row[f["name"]].get("origin") for f in fields}})
        column_names = ["document_id", "extraction_id", "batch_id"] + (["row_number"] if name != "headers" else []) + [f["name"] for f in fields]
        frame = pl.DataFrame(records, schema={column: pl.String for column in column_names})
        dataset = db.get(Dataset, outputs.get(name)) if name in outputs else None
        if dataset is None:
            dataset = Dataset(organization_id=user.organization_id, name=f"{family.name} · {name}",
                              description="Registros de cada publicación documental; no acumulado histórico.", owner=user.name)
            db.add(dataset)
            db.flush()
            outputs[name] = dataset.id
        from .governance import authorize_dataset
        authorize_dataset(db, user, dataset.id)
        metadata = {"document_publication": {"family_id": family.id, "batch_id": batch.id, "manifest_hash": batch.manifest_hash,
                    "structure": name, "scope": "PUBLICATION_BATCH_ONLY", "window_start": iso(batch.window_start),
                    "window_end": iso(batch.window_end), "timezone": batch.settings["timezone"],
                    "business_periods": {m.document_id: require_record(db, DocumentExtraction, m.extraction_id).payload.get("business_period") for m in ready},
                    "correction": batch.settings["correction"],
                    "automatic_processing_allowed": not batch.settings["correction"] and batch.settings["stop_after"] != "PUBLICATION",
                    "automatic_delivery_allowed": not batch.settings["correction"] and batch.settings["stop_after"] == "DELIVERY" and batch.settings["automatic_delivery"],
                    "group_structures": list(structures(template.definition)), "row_provenance": provenance}}
        membership = sorted(m.document_id for m in ready)
        metadata["population_scope"] = {"schema_version": 1, "kind": "DOCUMENT_SET",
            "identity": digest({"family_id": family.id, "structure": name, "documents": membership}),
            "membership_sha256": digest(membership)}
        read_result = DatasetReadResult(frame, "DOCUMENTS", "Extracción documental", "application/json", "PUBLICATION_ROW_1_BASED", metadata=metadata)
        overrides = {f["name"]: {"logical_type": f["logical_type"], "semantic_tag": "IDENTIFIER" if f["business_key"] and f["logical_type"] == "STRING" else None} for f in fields}
        for identity_column in column_names[:3]:
            overrides[identity_column] = {"logical_type": "STRING", "semantic_tag": "IDENTIFIER"}
        version = create_version(db, dataset, None, f"{name}.parquet", user.name, "DOCUMENTS", read_result=read_result, column_overrides=overrides)
        published[name] = version.id
        link_artifact(db, user.organization_id, "DOCUMENT_PUBLICATION", "DOCUMENT_BATCH", batch.id, "DATASET_VERSION", version.id)
    family.output_datasets = outputs
    batch.published_versions, batch.status = published, "PUBLISHED"
    for member in ready:
        member.status = "PUBLISHED"
        original = require_record(db, DocumentOriginal, member.document_id)
        if original.extraction_id == member.extraction_id:
            original.status = "PUBLISHED"
        extraction = require_record(db, DocumentExtraction, member.extraction_id)
        link_artifact(db, user.organization_id, "DOCUMENT_BATCH_MEMBER", "DOCUMENT", original.id, "DOCUMENT_BATCH", batch.id)
        link_artifact(db, user.organization_id, "DOCUMENT_EXTRACTION", "DOCUMENT", original.id, "DOCUMENT_EXTRACTION", extraction.id)
        link_artifact(db, user.organization_id, "EXTRACTION_BATCH", "DOCUMENT_EXTRACTION", extraction.id, "DOCUMENT_BATCH", batch.id)
    audit(db, "DOCUMENT_BATCH_PUBLISHED", "document_batch", batch.id, "Lote y estructuras publicados consistentemente", user.name,
          user.organization_id, {"manifest_hash": batch.manifest_hash, "versions": published, "scope": "PUBLICATION_BATCH_ONLY"})
    return batch


def publish_durable(db, batch, user):
    """Persist reservations/composition before materialization; retries reuse it.

    This orchestration boundary deliberately commits its own lifecycle. It does
    not encompass external destinations and never promises a distributed commit.
    """
    publish_batch(db, batch, user, seal_only=True)
    db.commit()
    if batch.status == "SEALED":
        publish_batch(db, batch, user)
    return batch


def dispatch_individual(db, original, user):
    template = definition_for(db, original)
    if original.is_example or original.duplicate_of_id or original.correction_of_id or template.definition["publication"]["mode"] != "INDIVIDUAL":
        return
    if issues(extraction_for(db, original), template.definition):
        return
    if db.scalar(select(DocumentPublicationClaim.id).where(DocumentPublicationClaim.document_id == original.id).limit(1)):
        return  # A review of a published document is an explicit correction.
    family = db.get(DocumentFamily, original.family_id)
    batch = open_batch(db, family, template, user, "document:" + original.id, document_ids=[original.id])
    publish_durable(db, batch, user)


def dispatch_due(db, now=None, limit=20):
    instant = aware(now or utcnow())
    count = 0
    families = db.scalars(select(DocumentFamily).where(DocumentFamily.next_cut_at <= instant).limit(limit)).all()
    for family in families:
        lock_family(db, family)
        if not family.next_cut_at or aware(family.next_cut_at) > instant:
            continue
        template = db.get(DocumentTemplate, family.active_template_id)
        user = db.get(User, family.responsible_user_id)
        settings = template.definition["publication"]
        end = aware(family.next_cut_at)
        start = end - timedelta(seconds=settings["interval_seconds"])
        try:
            batch = open_batch(db, family, template, user, f"window:{template.id}:{iso(start)}:{iso(end)}", start=start, end=end)
            # Deferred originals retain their original window in their previous member.
            prior_deferred = db.scalars(select(DocumentBatchMember.document_id).join(DocumentBatch, DocumentBatchMember.batch_id == DocumentBatch.id)
                                       .where(DocumentBatch.family_id == family.id, DocumentBatchMember.status == "DEFERRED")).all()
            current = set(db.scalars(select(DocumentBatchMember.document_id).where(DocumentBatchMember.batch_id == batch.id)))
            for identity in set(prior_deferred) - current:
                original = require_record(db, DocumentOriginal, identity)
                if original.template_id == template.id and not db.scalar(select(DocumentPublicationClaim.id).where(DocumentPublicationClaim.document_id == identity).limit(1)):
                    db.add(DocumentBatchMember(organization_id=family.organization_id, batch_id=batch.id, document_id=identity,
                                               reason="DEFERRED_FROM_ORIGINAL_WINDOW"))
            db.flush()
            publish_durable(db, batch, user)
            family.next_cut_at = end + timedelta(seconds=settings["interval_seconds"])
            count += 1
        except OperationError as error:
            audit(db, "DOCUMENT_SCHEDULE_BLOCKED", "document_family", family.id, "El corte documental requiere intervención", "Scheduler",
                  family.organization_id, {"code": error.code, "planned_at": iso(end)})
    # Receipts pin the template/settings in force at reception. Activating a new
    # revision must not strand old documents or reinterpret their calendar.
    outstanding = db.scalars(select(DocumentOriginal).where(
        DocumentOriginal.is_example.is_(False), DocumentOriginal.duplicate_of_id.is_(None),
        DocumentOriginal.correction_of_id.is_(None),
        ~select(DocumentPublicationClaim.id).where(DocumentPublicationClaim.document_id == DocumentOriginal.id).exists()
    ).order_by(DocumentOriginal.received_at).limit(1000)).all()
    windows: set[tuple[str, str | None, str | None]] = set()
    for original in outstanding:
        template = db.get(DocumentTemplate, original.template_id)
        settings = template.definition["publication"]
        family = db.get(DocumentFamily, original.family_id)
        if settings["mode"] != "SCHEDULED" or family.active_template_id == template.id:
            continue
        starts = datetime.fromisoformat(settings["starts_at"])
        interval = settings["interval_seconds"]
        slot = max(0, int((aware(original.received_at) - starts).total_seconds() // interval))
        start = starts + timedelta(seconds=slot * interval)
        end = start + timedelta(seconds=interval)
        key = (template.id, iso(start), iso(end))
        if end > instant or key in windows or len(windows) >= limit:
            continue
        windows.add(key)
        user = db.get(User, family.responsible_user_id)
        try:
            batch = open_batch(db, family, template, user, f"window:{template.id}:{iso(start)}:{iso(end)}", start=start, end=end)
            publish_durable(db, batch, user)
            count += 1
        except OperationError as error:
            audit(db, "DOCUMENT_SCHEDULE_BLOCKED", "document_family", family.id, "La revisión anterior conserva documentos pendientes", "Scheduler",
                  family.organization_id, {"code": error.code, "template_id": template.id})
    # Strict membership is frozen at reception; missing extraction/review may later complete.
    for batch in db.scalars(select(DocumentBatch).where(DocumentBatch.status.in_(["OPEN", "WAITING"])).limit(limit)):
        user = db.get(User, batch.actor_id)
        try:
            publish_durable(db, batch, user)
        except OperationError:
            continue
    return count
