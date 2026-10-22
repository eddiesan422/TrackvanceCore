"""Human source confirmation cannot discard products or alter automatic evidence."""
import copy
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from trackvance.document_builder_workspace import test_dto as dto
from trackvance.document_service import digest, issues
from trackvance.operations_common import OperationError


@pytest.fixture(autouse=True)
def confined_worker_fake(monkeypatch):
    # Tiny generic rules only: the actual executor proof is exercised without
    # spawning a child or touching services during the independent volume trial.
    from trackvance import document_builder_executor
    from trackvance.document_engine import extract
    def run(operation, references, definition):
        assert operation == "EXTRACT"
        return {reference["id"]: extract(reference["lines"], definition, reference.get("page_geometry")) for reference in references}
    monkeypatch.setattr(document_builder_executor, "run_builder", run)


def test_initial_automatic_resolution_injection_is_rejected():
    automatic = {"ref": {"fields": {}, "tables": {}, "header_resolutions": []}}
    snapshot = {"definition": {"publication": {}, "tables": [], "fields": []}, "references": []}
    item = SimpleNamespace(snapshot=snapshot, snapshot_hash=digest(snapshot), automatic_results=automatic,
        automatic_results_hash=digest(automatic), reviewed_results={}, review_history=[], id="test",
        draft_id="draft", draft_version=1, review_version=1, activated_template_id=None)
    with pytest.raises(OperationError, match="automático"):
        dto(item)


def test_review_dto_retains_exact_header_confirmation():
    from trackvance.document_workspace_api import ReviewTest
    request = {"observation_id": "a" * 64, "expected_observation_hash": "b" * 64,
               "expected_payload_hash": "c" * 64, "decision": "SOURCE_HEADER_ONLY_NO_PRODUCTS_CONFIRMED",
               "source_note": "Comprobé los rótulos en la imagen original, sin productos."}
    body = ReviewTest.model_validate({"expected_version": 1, "reference_id": "ref", "reason": "Fuente",
                                     "header_confirmations": [request]})
    assert body.model_dump()["header_confirmations"] == [request]


def ready(two_pages=False):
    from test_document_declared_rows_097 import definition, sources

    from trackvance.document_engine import extract
    from trackvance.document_schemas import DocumentDefinition
    declared = definition(partial=True)
    declared["fields"] = [{"name": "optional_header", "label": "Document", "required": False}]
    definition = DocumentDefinition.model_validate(declared).model_dump(mode="json")
    raw = sources(partial=True)
    if two_pages:
        raw += sources(partial=True, page=2)
    automatic = extract(raw, definition)
    payload = copy.deepcopy(automatic)
    for row in [payload["fields"], *[row for rows in payload["tables"].values() for row in rows]]:
        for cell in row.values():
            cell["reviewed"] = True
    region = payload["table_unparsed"]["detail"][0]
    binding = {"kind": "DOCUMENT", "document_id": "own-document", "source_sha256": "d" * 64,
               "definition_hash": digest(definition), "automatic_payload_hash": digest(automatic)}
    request = {"observation_id": region["header_observation"]["observation_id"],
        "expected_observation_hash": digest(region["header_observation"]), "expected_payload_hash": digest(payload),
        "decision": "SOURCE_HEADER_ONLY_NO_PRODUCTS_CONFIRMED", "source_note": "Imagen original comprobada: sólo encabezado."}
    return payload, automatic, definition, binding, request


def append(payload, automatic, definition, binding, requests):
    from trackvance.document_header_review import append_confirmations
    return append_confirmations(payload, automatic, definition, requests, binding, "reviewer", "2026-01-01T00:00:00Z", "Revisión del original")


def test_exact_confirmation_preserves_automatic_observation_and_unblocks_only_reviewed_result():
    payload, automatic, definition, binding, request = ready()
    original = copy.deepcopy(automatic)
    assert "TABLE_ROWS_UNRECOGNIZED:detail" in issues(SimpleNamespace(payload=payload, reviewed=True), definition)
    final = append(payload, automatic, definition, binding, [request])
    assert final["table_unparsed"] == automatic["table_unparsed"]
    assert final["lines"] == automatic["lines"]
    assert automatic == original and "header_resolutions" not in payload
    assert issues(SimpleNamespace(payload=final, reviewed=True), definition, source_binding=binding) == []
    assert "TABLE_ROWS_UNRECOGNIZED:detail" in issues(SimpleNamespace(payload=final, reviewed=False), definition)


def test_authentic_header_cannot_hide_an_omitted_product_without_body_region():
    payload, automatic, definition, binding, request = ready()
    automatic["tables"]["detail"].pop()
    payload["tables"]["detail"].pop()
    request["expected_payload_hash"] = digest(payload)
    with pytest.raises(OperationError, match="cuerpo automático"):
        append(payload, automatic, definition, binding, [request])


def test_declared_row_start_requires_explicit_review_even_with_high_confidence():
    payload, _, definition, _, _ = ready()
    definition["publication"]["review_policy"] = "CRITICAL_OR_UNCERTAIN"
    payload["table_unparsed"]["detail"] = []
    cell = payload["tables"]["detail"][0]["description"]
    cell.update(reviewed=False, confidence=100)
    assert "REVIEW_REQUIRED:detail.0.description" in issues(SimpleNamespace(payload=payload), definition)


def test_later_review_event_invalidates_old_confirmation_even_when_values_return_exactly():
    from trackvance.document_header_review import invalidate_confirmations
    payload, automatic, definition, binding, request = ready()
    final = append(payload, automatic, definition, binding, [request])
    first = copy.deepcopy(final["header_resolutions"][0])
    invalidated = invalidate_confirmations(final, "reviewer", "2026-01-02T00:00:00Z", "Nueva revisión")
    assert invalidated["tables"] == final["tables"]
    assert invalidated["header_resolutions"][0] == first
    assert "TABLE_ROWS_UNRECOGNIZED:detail" in issues(SimpleNamespace(payload=invalidated, reviewed=True), definition)
    request["expected_payload_hash"] = digest(invalidated)
    renewed = append(invalidated, automatic, definition, binding, [request])
    assert len(renewed["header_resolutions"]) == 3
    assert issues(SimpleNamespace(payload=renewed, reviewed=True), definition) == []


def test_multiple_confirmations_verify_body_once_and_bad_last_request_has_no_partial_effect(monkeypatch):
    from trackvance import document_builder_executor
    payload, automatic, definition, binding, first = ready(two_pages=True)
    second_region = payload["table_unparsed"]["detail"][1]
    second = {**first, "observation_id": second_region["header_observation"]["observation_id"],
              "expected_observation_hash": digest(second_region["header_observation"])}
    run = document_builder_executor.run_builder
    calls = []
    def counted(*args):
        calls.append(1)
        return run(*args)
    monkeypatch.setattr(document_builder_executor, "run_builder", counted)
    invalid = {**second, "expected_payload_hash": "0" * 64}
    before = copy.deepcopy(payload)
    with pytest.raises(OperationError):
        append(payload, automatic, definition, binding, [first, invalid])
    assert calls == [] and payload == before
    final = append(payload, automatic, definition, binding, [first, second])
    assert calls == [1] and len(final["header_resolutions"]) == 2
    assert issues(SimpleNamespace(payload=final, reviewed=True), definition) == []


def test_builder_stale_review_version_rejected_before_transport_or_persistence(monkeypatch):
    from trackvance import document_builder_workspace as workspace
    monkeypatch.setattr(workspace, "authority", lambda *args: None)
    monkeypatch.setattr(workspace, "permission", lambda *args: None)
    monkeypatch.setattr(workspace, "owned", lambda *args: SimpleNamespace(family_id="family"))
    monkeypatch.setattr(workspace, "lock_family", lambda *args: None)
    monkeypatch.setattr(workspace, "test_dto", lambda *args: {})
    db = SimpleNamespace(refresh=lambda item: None, execute=lambda *args: pytest.fail("PARTIAL_WRITE"))
    item = SimpleNamespace(draft_id="draft", review_version=2)
    with pytest.raises(OperationError) as failure:
        workspace.review_test(db, item, "ref", 1, {}, [], "Fuente", SimpleNamespace(id="reviewer"), [{}])
    assert failure.value.code == "DOCUMENT_REVIEW_CONFLICT"


def test_operational_table_reconstruction_cannot_delete_new_header_observation(monkeypatch):
    from trackvance import document_service as service
    payload, _, definition, _, _ = ready()
    previous = SimpleNamespace(id="extraction", payload=payload)
    monkeypatch.setattr(service, "permission", lambda *args: None)
    monkeypatch.setattr(service, "owned", lambda *args: SimpleNamespace())
    monkeypatch.setattr(service, "lock_family", lambda *args: None)
    monkeypatch.setattr(service, "extraction_for", lambda *args: previous)
    monkeypatch.setattr(service, "definition_for", lambda *args: SimpleNamespace(definition=definition))
    db = SimpleNamespace(refresh=lambda item: None, add=lambda *args: pytest.fail("PARTIAL_WRITE"))
    with pytest.raises(OperationError) as failure:
        service.review(db, SimpleNamespace(family_id="family"), "extraction", {}, [], "Fuente", SimpleNamespace(id="reviewer"), {"detail": []})
    assert failure.value.code == "DOCUMENT_HEADER_SOURCE_CHANGED"


def test_header_confirmation_requires_existing_independent_source_download_permission(monkeypatch):
    from trackvance import document_service as service
    monkeypatch.setattr(service, "effective_permissions", lambda *args: {"documents:review"})
    # Value review authority remains valid, while source attestation is denied
    # before locks, source verification or persistence can occur.
    service.permission(None, SimpleNamespace(), "review")
    with pytest.raises(OperationError) as failure:
        service.review(None, None, "extraction", {}, [], "Fuente", SimpleNamespace(), header_confirmations=[{}])
    assert failure.value.status == 403 and failure.value.code == "DOCUMENT_FORBIDDEN"


def test_legacy_values_and_issue_order_do_not_acquire_resolution_metadata():
    from trackvance.document_header_review import append_confirmations, header_review_dto
    payload, _, definition, _, _ = ready()
    definition["tables"][0]["visual"].pop("row_start")
    definition["tables"][0]["visual"].pop("header_observation_policy")
    payload["table_unparsed"]["detail"] = []
    for row in [payload["fields"], *payload["tables"]["detail"]]:
        for cell in row.values():
            cell["reviewed"] = False
    before = digest(payload)
    assert append_confirmations(payload, None, definition, [], None, None, None, None) is payload
    assert header_review_dto(payload, definition) is None and digest(payload) == before
    assert issues(SimpleNamespace(payload=payload), definition) == [
        "REVIEW_REQUIRED:headers.0.optional_header",
        *[f"REVIEW_REQUIRED:detail.{index}.{name}" for index in range(2) for name in ("description", "quantity", "price", "amount")]]


def test_confined_failure_cannot_partially_persist_confirmation(monkeypatch):
    from trackvance import document_builder_executor
    payload, automatic, definition, binding, request = ready()
    before = copy.deepcopy(payload)
    def failed(*args):
        raise OperationError(422, "DOCUMENT_BUILDER_TIMEOUT", "Verificación acotada")
    monkeypatch.setattr(document_builder_executor, "verify_automatic_payload", failed)
    with pytest.raises(OperationError) as failure:
        append(payload, automatic, definition, binding, [request])
    assert failure.value.code == "DOCUMENT_BUILDER_TIMEOUT" and payload == before


@pytest.mark.parametrize("part", ["expected_payload_hash", "expected_observation_hash", "observation_id"])
def test_stale_or_unknown_hash_rejected_atomically(part):
    payload, automatic, definition, binding, request = ready()
    before = copy.deepcopy(payload)
    request[part] = "0" * 64
    with pytest.raises(OperationError):
        append(payload, automatic, definition, binding, [request])
    assert payload == before


@pytest.mark.parametrize("change", ["body", "missing", "unreviewed", "source", "definition", "duplicate", "row_swap", "origin"])
def test_nonheader_unreviewed_or_changed_evidence_cannot_be_confirmed(change):
    payload, automatic, definition, binding, request = ready()
    if change == "body":
        payload["table_unparsed"]["detail"].append({"reason": "AMBIGUOUS_ROW_BOUNDARY"})
    elif change in {"missing", "unreviewed"}:
        cell = payload["tables"]["detail"][0]["price"]
        cell["validity" if change == "missing" else "reviewed"] = "MISSING" if change == "missing" else False
    elif change == "source":
        payload["lines"][0]["words"][0]["text"] = "Changed"
    elif change == "definition":
        definition["tables"][0]["visual"]["column_bounds"][0] = .03
    elif change == "row_swap":
        payload["tables"]["detail"].reverse()
    elif change == "origin":
        payload["tables"]["detail"][0]["price"]["origin"] = None
    requests = [request, request] if change == "duplicate" else [request]
    with pytest.raises(OperationError):
        append(payload, automatic, definition, binding, requests)


@pytest.mark.parametrize("change", ["value", "origin", "reviewed", "line", "definition", "binding"])
def test_later_value_source_definition_or_actor_scope_change_invalidates_resolution(change):
    payload, automatic, definition, binding, request = ready()
    final = append(payload, automatic, definition, binding, [request])
    if change in {"value", "origin", "reviewed"}:
        final["tables"]["detail"][0]["price"][change] = {"value": "99.99", "origin": None, "reviewed": False}[change]
    elif change == "line":
        final["lines"][0]["text"] = "Changed"
    elif change == "definition":
        definition["tables"][0]["name"] = "other"
    else:
        binding["document_id"] = "foreign-document"
    assert any("UNRECOGNIZED" in issue for issue in issues(SimpleNamespace(payload=final, reviewed=True), definition, source_binding=binding))


@pytest.mark.parametrize("field,value", [("decision", "IGNORE"), ("source_note", " "),
    ("source_note", "x" * 2001), ("region", [0, 0, 1, 1]), ("expected_payload_hash", "bad")])
def test_resolution_dto_rejects_generic_ignore_replacement_coordinates_and_invalid_hash(field, value):
    from trackvance.document_header_review import HeaderConfirmation
    *_, request = ready()
    request[field] = value
    with pytest.raises(ValidationError):
        HeaderConfirmation.model_validate(request)
