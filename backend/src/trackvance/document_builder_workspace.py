"""Exact builder snapshots, independent corpus and optimistic backend drafts."""
import copy
import hashlib
from pathlib import Path
from types import SimpleNamespace
from typing import cast

from sqlalchemy import LargeBinary, Text, func, select, update
from sqlalchemy.engine import CursorResult

from .db import iso, utcnow
from .document_builder_executor import MAX_BYTES as BUILDER_MAX_BYTES
from .document_builder_executor import run_builder
from .document_engine import arithmetic_checks, validate_value
from .document_header_review import (
    append_confirmations,
    header_review_dto,
    invalidate_confirmations,
    reject_initial_resolutions,
)
from .document_models import (
    DocumentBuilderTest,
    DocumentDraft,
    DocumentFamily,
    DocumentReference,
    DocumentTemplate,
)
from .document_schemas import DocumentDefinition, DocumentDraftDefinition, structures
from .document_service import (
    activate_verified_template,
    create_template,
    digest,
    issues,
    lock_family,
    owned,
    permission,
    template_dto,
)
from .operations_common import OperationError
from .services import audit


def authority(db, user):
    for action in ("configure", "download", "write"):
        permission(db, user, action)


def processor_identity():
    return {name: hashlib.sha256(Path(__file__).with_name(name).read_bytes()).hexdigest() for name in
            ("document_engine.py", "document_template_builder_rules.py", "document_invoice_controls.py", "document_invoice_schemas.py", "document_schemas.py", "document_template_builder_schemas.py", "document_discovery.py", "document_builder_workspace.py", "document_builder_executor.py", "document_header_review.py", "document_local_inference.py", "document_semantic_fragments.py", "document_literal_tables.py", "document_quality.py", "document_providers.py", "document_worker.py", "document_inference_completion.py")}


def reference_dto(item, detail=False):
    result = {"id": item.id, "family_id": item.family_id, "filename": item.filename, "sha256": item.sha256,
              "kind": "REFERENCE", "is_example": True, "template_id": None, "role": item.role, "association": item.association,
              "status": item.status, "error_code": item.error_code, "configuration_used": item.configuration_used, "created_at": iso(item.created_at),
              "engine_snapshot": ({**item.engine_snapshot, "snapshot_sha256": item.engine_snapshot_hash} if item.engine_snapshot else None)}
    if detail:
        verify_reference(item)
        result.update(payload=item.payload, payload_hash=item.payload_hash, engine=item.engine)
    return result


def verify_reference(item):
    if item.payload is not None and digest(item.payload) != item.payload_hash:
        raise OperationError(409, "DOCUMENT_REFERENCE_INTEGRITY", "La lectura de referencia perdió su integridad; conserva el original y vuelve a leerlo.")


def references_for(db, family_id, ids, user, *, ready=True):
    if not 1 <= len(ids) <= 20 or len(set(ids)) != len(ids):
        raise OperationError(422, "DOCUMENT_REFERENCE_SELECTION", "Selecciona de 1 a 20 referencias distintas.")
    # Refuse oversized selections in SQL before ORM materializes JSON payloads,
    # including draft saves that select references before they become READY.
    serialized = DocumentReference.payload.cast(Text)
    byte_length = (func.octet_length(serialized) if db.get_bind().dialect.name == "postgresql"
                   else func.length(serialized.cast(LargeBinary)))
    size = db.scalar(select(func.coalesce(func.sum(byte_length), 0)).where(
        DocumentReference.organization_id == user.organization_id, DocumentReference.id.in_(ids)))
    if size > BUILDER_MAX_BYTES:
        raise OperationError(422, "DOCUMENT_BUILDER_BYTES_LIMIT", "Las referencias seleccionadas superan 64 MiB. Reduce la selección; el borrador se conserva.")
    references = [owned(db, DocumentReference, identity, user) for identity in ids]
    for item in references:
        if item.family_id != family_id:
            raise OperationError(422, "DOCUMENT_BUILDER_FAMILY", "Todas las referencias deben pertenecer a esta familia.")
        if ready and (item.status != "READY" or not item.payload):
            raise OperationError(409, "DOCUMENT_BUILDER_PENDING", "Espera a que termine la lectura local de todas las referencias seleccionadas.")
        verify_reference(item)
    return references


def draft_dto(item):
    return {"id": item.id, "family_id": item.family_id, "name": item.name, "version": item.version,
            "definition": item.definition, "sha256": item.definition_hash, "reference_ids": item.reference_ids,
            "discovered_from": item.discovered_from, "source_template_id": item.source_template_id,
            "state": "DRAFT", "created_at": iso(item.created_at)}


def create_draft(db, family_id, user, source_template_id=None, name="Borrador"):
    authority(db, user)
    owned(db, DocumentFamily, family_id, user)
    definition: dict = {"fields": [], "tables": []}
    if source_template_id:
        template = owned(db, DocumentTemplate, source_template_id, user)
        if template.family_id != family_id or digest(template.definition) != template.definition_hash:
            raise OperationError(422, "DOCUMENT_DRAFT_SOURCE", "Elige una revisión íntegra de esta familia.")
        definition = copy.deepcopy(template.definition)
    item = DocumentDraft(organization_id=user.organization_id, family_id=family_id, name=name,
        definition=definition, definition_hash=digest(definition), actor_id=user.id, source_template_id=source_template_id)
    db.add(item)
    db.flush()
    audit(db, "DOCUMENT_DRAFT_CREATED", "document_draft", item.id, "Borrador independiente creado", user.name, user.organization_id,
          {"source_template_id": source_template_id})
    return item


def save_draft(db, item, expected_version, definition, reference_ids, name, user, *, discovered_from=None):
    authority(db, user)
    lock_family(db, owned(db, DocumentFamily, item.family_id, user))
    db.refresh(item)
    if digest(item.definition) != item.definition_hash:
        raise OperationError(409, "DOCUMENT_DRAFT_INTEGRITY", "El borrador perdió su integridad.")
    if reference_ids:
        references_for(db, item.family_id, reference_ids, user, ready=False)
    normalized = DocumentDraftDefinition.model_validate(definition).model_dump(mode="json")
    result = db.execute(update(DocumentDraft).where(DocumentDraft.id == item.id, DocumentDraft.version == expected_version).values(
        definition=normalized, definition_hash=digest(normalized), reference_ids=reference_ids, name=name,
        version=DocumentDraft.version + 1, actor_id=user.id,
        discovered_from=discovered_from if discovered_from is not None else item.discovered_from))
    if cast(CursorResult, result).rowcount != 1:
        raise OperationError(409, "DOCUMENT_DRAFT_CONFLICT", "Otra persona guardó este borrador. Recarga su versión antes de guardar; tus cambios no se sobrescribieron.")
    db.refresh(item)
    audit(db, "DOCUMENT_DRAFT_SAVED", "document_draft", item.id, "Borrador guardado; las pruebas previas conservan su instantánea", user.name,
          user.organization_id, {"version": item.version, "definition_hash": item.definition_hash})
    return item


def discover_draft(db, item, expected_version, user):
    authority(db, user)
    lock_family(db, owned(db, DocumentFamily, item.family_id, user))
    db.refresh(item)
    selected = references_for(db, item.family_id, item.reference_ids, user)
    references = [reference for reference in selected if reference.role == "CONFIGURATION"]
    if not references:
        raise OperationError(422, "DOCUMENT_DISCOVERY_REFERENCES", "Selecciona referencias de configuración; la variante reservada se utiliza solo para probar.")
    result = run_builder("DISCOVER", [{"id": reference.id, "lines": reference.payload["lines"],
        **({"page_geometry": reference.payload["page_geometry"]} if "page_geometry" in reference.payload else {})} for reference in references])
    for reference in references:
        reference.configuration_used = True
    save_draft(db, item, expected_version, result["definition"], item.reference_ids, item.name, user,
               discovered_from=[reference.id for reference in references])
    return {"draft": draft_dto(item), "proposal": result}


def snapshot_for(db, item, user):
    if digest(item.definition) != item.definition_hash:
        raise OperationError(409, "DOCUMENT_DRAFT_INTEGRITY", "El borrador perdió su integridad.")
    if not item.definition.get("fields"):
        raise OperationError(422, "DOCUMENT_DRAFT_EMPTY", "Analiza las referencias o configura al menos un campo antes de probar el borrador.")
    definition = DocumentDefinition.model_validate(item.definition).model_dump(mode="json")
    references = references_for(db, item.family_id, item.reference_ids, user)
    return {"draft_id": item.id, "draft_version": item.version, "definition": definition, "definition_hash": digest(definition),
            "processor": processor_identity(), "references": [{"id": reference.id, "sha256": reference.sha256,
                "payload_hash": reference.payload_hash, "engine": reference.engine, "role": reference.role,
                "association": reference.association, "configuration_used": reference.configuration_used,
                **({"page_geometry": copy.deepcopy(reference.payload["page_geometry"])} if "page_geometry" in reference.payload else {})}
                for reference in references], "discovered_from": item.discovered_from}


def test_draft(db, item, expected_version, user):
    authority(db, user)
    family = owned(db, DocumentFamily, item.family_id, user)
    lock_family(db, family)
    db.refresh(item)
    if item.version != expected_version:
        raise OperationError(409, "DOCUMENT_DRAFT_CONFLICT", "El borrador cambió. Recarga y prueba su versión actual.")
    snapshot = snapshot_for(db, item, user)
    hashed = digest(snapshot)
    previous = db.scalar(select(DocumentBuilderTest).where(DocumentBuilderTest.draft_id == item.id, DocumentBuilderTest.snapshot_hash == hashed))
    if previous:
        return previous
    results = run_builder("EXTRACT", [{"id": reference.id, "lines": reference.payload["lines"],
        **({"page_geometry": reference.payload["page_geometry"]} if "page_geometry" in reference.payload else {})}
        for reference in references_for(db, item.family_id, item.reference_ids, user)], snapshot["definition"])
    tested = DocumentBuilderTest(organization_id=user.organization_id, draft_id=item.id, draft_version=item.version,
        snapshot=snapshot, snapshot_hash=hashed, automatic_results=results, automatic_results_hash=digest(results), actor_id=user.id)
    db.add(tested)
    db.flush()
    audit(db, "DOCUMENT_DRAFT_TESTED", "document_builder_test", tested.id, "Misma instantánea probada sobre todas las referencias seleccionadas", user.name,
          user.organization_id, {"snapshot_hash": hashed, "reference_ids": item.reference_ids})
    return tested


def measure(payload):
    cells = [*payload["fields"].values(), *[cell for rows in payload["tables"].values() for row in rows for cell in row.values()]]
    return {"fields_and_cells": len(cells), "recognized": sum(cell.get("observed") is not None for cell in cells),
            "valid": sum(cell.get("validity") == "VALID" for cell in cells), "ambiguous": sum(cell.get("validity") == "AMBIGUOUS" for cell in cells),
            "rows": sum(len(rows) for rows in payload["tables"].values()), "unparsed_regions": sum(len(rows) for rows in payload.get("table_unparsed", {}).values()),
            "human_confirmed": sum(bool(cell.get("reviewed")) for cell in cells), "corrected": sum(bool(cell.get("corrected")) for cell in cells),
            "accuracy": None, "accuracy_notice": "La cobertura reconocida no demuestra exactitud; requiere oráculo independiente o confirmación humana."}


def test_dto(item):
    if digest(item.snapshot) != item.snapshot_hash or digest(item.automatic_results) != item.automatic_results_hash:
        raise OperationError(409, "DOCUMENT_TEST_INTEGRITY", "La instantánea de prueba perdió su integridad.")
    for payload in item.automatic_results.values():
        reject_initial_resolutions(payload)
    reviewed_hashes = {entry["reference_id"]: entry.get("reviewed_payload_hash") for entry in item.review_history}
    if any(identity not in item.automatic_results or digest(payload) != reviewed_hashes.get(identity)
           for identity, payload in item.reviewed_results.items()):
        raise OperationError(409, "DOCUMENT_TEST_INTEGRITY", "La revisión de referencia perdió su integridad.")
    final = {**item.automatic_results, **item.reviewed_results}
    definition = item.snapshot["definition"]
    header_review = {identity: header_review_dto(payload, definition, reviewed=identity in item.reviewed_results,
                     source_binding=header_source_binding(item, identity)) for identity, payload in final.items()}
    return {"id": item.id, "draft_id": item.draft_id, "draft_version": item.draft_version, "snapshot": item.snapshot,
            "snapshot_hash": item.snapshot_hash, "automatic_results": item.automatic_results, "automatic_results_hash": item.automatic_results_hash, "reviewed_results": item.reviewed_results,
            "review_version": item.review_version, "review_history": item.review_history, "activated_template_id": item.activated_template_id,
            "automatic_metrics": {identity: measure(payload) for identity, payload in item.automatic_results.items()},
            "reviewed_metrics": {identity: measure(payload) for identity, payload in final.items()},
            "issues": {identity: issues(SimpleNamespace(payload=payload, reviewed=identity in item.reviewed_results), definition,
                       source_binding=header_source_binding(item, identity)) for identity, payload in final.items()},
            "comparisons": compare_associations(item.snapshot, item.automatic_results),
            **({"header_review": header_review} if any(value is not None for value in header_review.values()) else {})}


def compare_associations(snapshot, results):
    groups: dict[str, list[str]] = {}
    for reference in snapshot["references"]:
        if reference.get("association"):
            groups.setdefault(reference["association"], []).append(reference["id"])
    def values(payload):
        return {"fields": {name: cell["value"] for name, cell in payload["fields"].items()},
                "tables": {name: [{key: cell["value"] for key, cell in row.items()} for row in rows] for name, rows in payload["tables"].items()}}
    return [{"association": name, "reference_ids": ids, "equal": all(values(results[identity]) == values(results[ids[0]]) for identity in ids[1:]),
             "basis": "USER_CONFIRMED_ASSOCIATION"} for name, ids in groups.items() if len(ids) > 1]


def header_source_binding(item, reference_id):
    payload = item.reviewed_results.get(reference_id, item.automatic_results[reference_id])
    if not any(region.get("header_observation") for regions in payload.get("table_unparsed", {}).values() for region in regions):
        return None
    reference = next(value for value in item.snapshot["references"] if value["id"] == reference_id)
    return {"kind": "BUILDER_REFERENCE", "test_id": item.id, "snapshot_hash": item.snapshot_hash,
            "reference_id": reference_id, "source_sha256": reference["sha256"],
            "source_payload_hash": reference["payload_hash"], "definition_hash": item.snapshot["definition_hash"]}


def review_test(db, item, reference_id, expected_version, changes, confirmed, reason, user, header_confirmations=None):
    authority(db, user)
    permission(db, user, "review")
    if not reason.strip():
        raise OperationError(422, "DOCUMENT_REVIEW_REASON", "Registra el motivo de la revisión de esta referencia.")
    draft = owned(db, DocumentDraft, item.draft_id, user)
    lock_family(db, owned(db, DocumentFamily, draft.family_id, user))
    db.refresh(item)
    test_dto(item)
    if item.review_version != expected_version:
        raise OperationError(409, "DOCUMENT_REVIEW_CONFLICT", "La revisión cambió. Recarga antes de guardar.")
    if item.activated_template_id:
        raise OperationError(409, "DOCUMENT_TEST_ACTIVATED", "Esta prueba activada es inmutable. Crea una prueba de otro borrador para cambiarla.")
    if reference_id not in item.automatic_results:
        raise OperationError(422, "DOCUMENT_TEST_REFERENCE", "La referencia no forma parte de esta prueba.")
    payload = copy.deepcopy(item.reviewed_results.get(reference_id, item.automatic_results[reference_id]))
    definition = item.snapshot["definition"]
    if header_confirmations:
        if changes or confirmed:
            raise OperationError(422, "DOCUMENT_HEADER_CELLS_REQUIRED", "Guarda primero los valores confirmados; revisa después el encabezado.")
        if reference_id not in item.reviewed_results:
            raise OperationError(422, "DOCUMENT_HEADER_CELLS_REQUIRED", "Guarda primero la revisión humana de esta referencia.")
        if digest(snapshot_for(db, draft, user)) != item.snapshot_hash:
            raise OperationError(409, "DOCUMENT_TEST_STALE", "Cambió la fuente o definición. Prueba de nuevo antes de confirmar el encabezado.")
        reference = next(value for value in references_for(db, draft.family_id, [reference_id], user) if value.id == reference_id)
        automatic = item.automatic_results[reference_id]
        if (automatic.get("lines") != reference.payload["lines"]
            or automatic.get("page_geometry") != reference.payload.get("page_geometry")):
            raise OperationError(409, "DOCUMENT_HEADER_SOURCE_CHANGED", "La lectura automática no coincide con la referencia original.")
    all_cells = {}
    for name, fields in structures(definition).items():
        rows = [payload["fields"]] if name == "headers" else payload["tables"].get(name, [])
        for index, row in enumerate(rows):
            for field in fields:
                all_cells[f"{name}.{index}.{field['name']}"] = (row[field["name"]], field)
    if (set(changes) | set(confirmed)) - set(all_cells):
        raise OperationError(422, "DOCUMENT_REVIEW_PATH", "La revisión contiene una celda inexistente.")
    for path, raw in changes.items():
        cell, field = all_cells[path]
        value, validity = validate_value(raw, field)
        cell.update(value=value, validity=validity, corrected=True, reviewed=False,
                    observed_origin=cell.get("observed_origin", cell.get("origin")), origin=None, confidence=None)
    for path in confirmed:
        cell, _ = all_cells[path]
        if cell["validity"] != "VALID":
            raise OperationError(422, "DOCUMENT_REVIEW_INVALID", "Resuelve los valores ausentes, inválidos o ambiguos antes de confirmarlos.")
        cell["reviewed"] = True
    if definition.get("arithmetic") or definition.get("invoice_controls"):
        payload["arithmetic"] = arithmetic_checks(payload, definition)
    if changes or confirmed:
        payload = invalidate_confirmations(payload, user.id, iso(utcnow()), reason)
    payload = append_confirmations(payload, item.automatic_results[reference_id], definition,
        header_confirmations, header_source_binding(item, reference_id), user.id, iso(utcnow()), reason)
    results = {**item.reviewed_results, reference_id: payload}
    history = [*item.review_history, {"reference_id": reference_id, "actor_id": user.id, "reason": reason.strip(), "reviewed_at": iso(utcnow()),
               "changed_paths": sorted(changes), "confirmed_paths": confirmed, "reviewed_payload_hash": digest(payload),
               **({"header_confirmations": payload["header_resolutions"][-len(header_confirmations):]} if header_confirmations else {})}]
    result = db.execute(update(DocumentBuilderTest).where(DocumentBuilderTest.id == item.id, DocumentBuilderTest.review_version == expected_version,
        DocumentBuilderTest.activated_template_id.is_(None)).values(reviewed_results=results, review_history=history, review_version=DocumentBuilderTest.review_version + 1))
    if cast(CursorResult, result).rowcount != 1:
        raise OperationError(409, "DOCUMENT_REVIEW_CONFLICT", "La revisión cambió. Recarga antes de guardar.")
    db.refresh(item)
    audit(db, "DOCUMENT_REFERENCE_REVIEWED", "document_builder_test", item.id, "Corrección de este documento; las reglas y la exactitud automática se conservan", user.name,
          user.organization_id, history[-1])
    return item


def activate_test(db, item, user):
    authority(db, user)
    draft = owned(db, DocumentDraft, item.draft_id, user)
    family = owned(db, DocumentFamily, draft.family_id, user)
    lock_family(db, family)
    db.refresh(item)
    db.refresh(draft)
    test_dto(item)
    if digest(snapshot_for(db, draft, user)) != item.snapshot_hash:
        raise OperationError(409, "DOCUMENT_TEST_STALE", "Cambió el borrador, referencias, formatos o motor. Vuelve a probar la configuración actual antes de activar.")
    if item.activated_template_id:
        return template_dto(owned(db, DocumentTemplate, item.activated_template_id, user))
    holdouts = [reference for reference in item.snapshot["references"] if reference["role"] == "HOLDOUT" and not reference["configuration_used"]
                and reference["id"] not in item.snapshot["discovered_from"]]
    independently_unused = [reference for reference in holdouts if not db.scalar(select(DocumentReference.id).where(
        DocumentReference.family_id == family.id, DocumentReference.sha256 == reference["sha256"], DocumentReference.configuration_used.is_(True)).limit(1))
        and reference["sha256"] not in {other["sha256"] for other in item.snapshot["references"] if other["role"] == "CONFIGURATION"}]
    if not independently_unused:
        raise OperationError(422, "DOCUMENT_HOLDOUT_REQUIRED", "Incluye y prueba una variante reservada que no se utilizó para proponer la plantilla.")
    if any(test_dto(item)["issues"].values()):
        raise OperationError(422, "DOCUMENT_TEMPLATE_TEST_REQUIRED", "Resuelve y confirma todas las incidencias de todas las referencias antes de activar.")
    template = create_template(db, family, item.snapshot["definition"], user)
    activate_verified_template(db, family, template, user)
    item.activated_template_id = template.id
    audit(db, "DOCUMENT_TEMPLATE_ACTIVATED", "document_family", family.id, "Revisión formal creada desde la instantánea conjunta aprobada", user.name,
          user.organization_id, {"template_id": template.id, "builder_test_id": item.id, "snapshot_hash": item.snapshot_hash})
    return template_dto(template)
