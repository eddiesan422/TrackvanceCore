"""Source control V2 discriminants. Synthetic inputs do not measure OCR quality."""
import base64
import copy
import json
from types import SimpleNamespace

import pytest
import test_document_semantic_corroboration_098 as fixtures

from trackvance.document_automatic_verification import source_context
from trackvance.document_lossless import adapt_saved_recognition
from trackvance.document_semantic_corroboration import (
    certify_semantic_candidate,
    semantic_source_context,
    semantic_verification_decision,
    trusted_semantic_proof,
)
from trackvance.document_semantic_evidence import semantic_review_dto
from trackvance.document_semantic_general import compile_profile


@pytest.fixture(autouse=True)
def explicit_synthetic_activation(monkeypatch):
    monkeypatch.setenv("TRACKVANCE_SEMANTIC_GENERAL_ENABLED", "true")
    monkeypatch.setenv("TRACKVANCE_SEMANTIC_AUTO_ENABLED", "true")


def v2_fixture(monkeypatch, **kwargs):
    # Generate an actual new request contract. Do not relabel a saved request.
    with monkeypatch.context() as context:
        context.setattr(fixtures, "compile_profile",
            lambda profile, **_options: compile_profile(profile, automatic_verification="SEMANTIC_AUTO_V2"))
        return fixtures.corroboration_fixture(**kwargs)


def mutate(events, kind, change, *, page=None):
    for event in events:
        if page is not None and event.get("page_one_based") != page:
            continue
        for index, artifact in enumerate(event["payload"].get("artifacts", [])):
            if artifact["kind"] == kind:
                body = json.loads(base64.b64decode(artifact["content_b64"]))
                change(body)
                event["payload"]["artifacts"][index] = fixtures._artifact(kind, body)


def assess(definition, configuration, events):
    fixtures._reseal(events)
    data = adapt_saved_recognition(events, definition, configuration, source_sha256="a" * 64,
        attempt_context={"expected_pages": events[0]["expected_pages"]})
    context = semantic_source_context(events, source_context(events, "a" * 64))
    proof = certify_semantic_candidate(data, definition, "a" * 64, source_context=context)
    data["engine"]["validation"] = proof
    return data, context, proof


@pytest.mark.parametrize("native,paddle", [(True, False), (False, True)])
def test_new_source_controls_certify_native_and_independent_logical_tables(monkeypatch, native, paddle):
    definition, configuration, events = v2_fixture(monkeypatch, native=native, paddle=paddle)
    data, _context, proof = assess(definition, configuration, events)
    assert not proof["global_issues"]
    assert len(proof["verified_cells"]) == 9
    assert proof["table_coverage"] == {"items": True}
    assert proof["source_checks"]["integrity"]["status"] == "VERIFIED"
    assert proof["source_checks"]["text"]["status"] == "AVAILABLE"
    assert proof["source_checks"]["execution_pages"]["status"] == "COMPLETE"
    assert proof["printed_pagination"]["status"] == "NOT_OBSERVED"
    extraction = SimpleNamespace(payload=data["payload"], engine=data["engine"], reviewed=False)
    assert trusted_semantic_proof(extraction, definition) == proof
    dto = semantic_review_dto(extraction, definition)
    assert dto["automatic_approval"]
    assert dto["verification_version"] == "SEMANTIC_AUTO_V2"
    assert dto["source_checks"]["automatic_eligibility"]["status"] == "ELIGIBLE"
    assert dto["tables"][0]["inventory_summary"][0]["row_count"] == 1
    assert "recognition_events" not in json.dumps(dto)
    assert "automatic_candidate" not in json.dumps(dto)


def test_available_native_text_does_not_veto_a_needed_structural_control(monkeypatch):
    definition, configuration, events = v2_fixture(monkeypatch)
    def replace_header(body):
        body["payload"]["lines"] = [fixtures._physical_line(["Unassigned", "Quantity", "Unit price", "Amount"], .2)
            if line["text"] == "Description Quantity Unit price Amount" else line for line in body["payload"]["lines"]]
    mutate(events, "NATIVE_SOURCE_PROBE_JSON", replace_header)
    data, context, proof = assess(definition, configuration, events)
    assert proof["native_scope_complete"]
    assert proof["source_checks"]["text"]["status"] == "AVAILABLE"
    assert not proof["table_coverage"]["items"]
    decision = semantic_verification_decision(data, definition, context)
    assert decision["actionable"]
    assert decision["pending_controls"]
    assert not any(item["code"] == "INDEPENDENT_SOURCE_REQUIRED" for item in decision["reasons"] if item.get("path", "").startswith("items."))


def test_partial_native_page_text_does_not_claim_full_source_scope(monkeypatch):
    definition, configuration, events = v2_fixture(monkeypatch)
    mutate(events, "NATIVE_SOURCE_PROBE_JSON", lambda body: body["payload"].update(native_incomplete_pages=[1]))
    _data, _context, proof = assess(definition, configuration, events)
    assert proof["source_checks"]["text"]["status"] == "PARTIAL"
    assert proof["source_checks"]["text"]["available_pages"] == [1]
    assert proof["source_checks"]["text"]["missing_pages"] == [1]
    assert not proof["native_scope_complete"]
    assert not proof["table_coverage"]["items"]


def test_existing_second_mechanism_is_not_repeated_for_quality(monkeypatch):
    definition, configuration, events = v2_fixture(monkeypatch, native=False, paddle=True)
    def unknown_header(body):
        for block in body["parsing_res_list"]:
            if block["block_label"] == "table":
                block["block_content"] = block["block_content"].replace("<th>Description</th>", "<th>Unassigned</th>")
    mutate(events, "PADDLE_PAGE_JSON", unknown_header)
    data, context, proof = assess(definition, configuration, events)
    assert not proof["table_coverage"]["items"]
    assert not semantic_verification_decision(data, definition, context)["actionable"]


def test_omitted_duplicate_occurrence_is_not_deduplicated_or_covered(monkeypatch):
    rows = [["Bolts", "2", "5.00", "10.00"]] * 3
    definition, configuration, events = v2_fixture(monkeypatch, rows=rows)
    mutate(events, "QWEN_SELECTION_JSON", lambda body: body["tables"]["items"].pop())
    _data, _context, proof = assess(definition, configuration, events)
    assert not proof["table_coverage"]["items"]
    assert "UNMATCHED_SOURCE_ROW" in proof["coverage_details"]["items"]["reasons"]
    inventory = proof["coverage_details"]["items"]["source_inventory"][0]
    assert len(inventory["rows"]) == 3
    assert len({row["id"] for row in inventory["rows"]}) == 3


def test_headerless_measured_continuation_keeps_every_occurrence(monkeypatch):
    definition, configuration, events = v2_fixture(monkeypatch, count=2)
    def continuation(body):
        body["payload"]["lines"] = [line for line in body["payload"]["lines"]
            if not (line["page"] == 1 and line["text"].startswith("Total due:"))
            and not (line["page"] == 2 and line["text"] == "Description Quantity Unit price Amount")]
    mutate(events, "NATIVE_SOURCE_PROBE_JSON", continuation)
    _data, _context, proof = assess(definition, configuration, events)
    assert proof["table_coverage"]["items"]
    inventory = proof["coverage_details"]["items"]["source_inventory"][0]
    assert [row["page"] for row in inventory["rows"]] == [1, 2]
    assert any(header["basis"] == "MEASURED_CONTINUATION_OF_PREVIOUS_ROLE_COLUMNS" for header in inventory["headers"])


def test_unproved_new_section_does_not_inherit_headerless_table(monkeypatch):
    definition, configuration, events = v2_fixture(monkeypatch, count=2)
    def changed_scope(body):
        body["payload"]["lines"] = [line for line in body["payload"]["lines"]
            if not (line["page"] == 1 and line["text"].startswith("Total due:"))
            and not (line["page"] == 2 and line["text"] == "Description Quantity Unit price Amount")]
        body["payload"]["lines"].append(fixtures._line("Separate schedule", .17, page=2))
    mutate(events, "NATIVE_SOURCE_PROBE_JSON", changed_scope)
    _data, _context, proof = assess(definition, configuration, events)
    assert not proof["table_coverage"]["items"]


def test_printed_document_gap_is_separate_from_successful_file_reading(monkeypatch):
    definition, configuration, events = v2_fixture(monkeypatch)
    mutate(events, "NATIVE_SOURCE_PROBE_JSON",
        lambda body: body["payload"]["lines"].append(fixtures._line("Document page 1 of 2", .95)))
    data, _context, proof = assess(definition, configuration, events)
    assert proof["source_checks"]["integrity"]["status"] == "VERIFIED"
    assert proof["source_checks"]["execution_pages"]["status"] == "COMPLETE"
    assert proof["printed_pagination"]["status"] == "INCOMPLETE"
    assert proof["source_checks"]["documentary_completeness"]["status"] == "INCOMPLETE"
    assert proof["global_issues"]
    extraction = SimpleNamespace(payload=data["payload"], engine=data["engine"], reviewed=False)
    assert not semantic_review_dto(extraction, definition)["automatic_approval"]


def test_modified_cell_invalidates_current_verification_without_erasing_proof(monkeypatch):
    definition, configuration, events = v2_fixture(monkeypatch)
    data, _context, proof = assess(definition, configuration, events)
    original = copy.deepcopy(proof)
    data["payload"]["fields"]["issuer"]["value"] = "Another party"
    extraction = SimpleNamespace(payload=data["payload"], engine=data["engine"], reviewed=True)
    dto = semantic_review_dto(extraction, definition)
    cell = next(cell for cell in dto["values"] if cell["path"] == "headers.0.issuer")
    assert cell["corroboration"]["status"] == "INVALIDATED"
    assert cell["corroboration"]["prior_proof"]["candidate_sha256"] == proof["candidate_sha256"]
    assert proof == original


def test_unscoped_printed_counter_is_uncertainty_not_proved_missing_page(monkeypatch):
    definition, configuration, events = v2_fixture(monkeypatch)
    mutate(events, "NATIVE_SOURCE_PROBE_JSON",
        lambda body: body["payload"]["lines"].append(fixtures._line("Page 1 of 2", .95)))
    _data, _context, proof = assess(definition, configuration, events)
    assert proof["printed_pagination"]["status"] == "UNVERIFIED"
    assert "PRINTED_SCOPE_UNPROVEN" in proof["global_issues"]
    assert proof["source_checks"]["documentary_completeness"]["status"] == "PENDING"


def test_two_actual_source_values_stay_blocked_after_confirmation_flags(monkeypatch):
    definition, configuration, events = v2_fixture(monkeypatch, count=2)
    def real_source_disagreement(body):
        body["payload"]["lines"] = [fixtures._line("Total due: 20.00", .8, page=2)
            if line["page"] == 2 and line["text"].startswith("Total due:") else line
            for line in body["payload"]["lines"]]
    mutate(events, "NATIVE_SOURCE_PROBE_JSON", real_source_disagreement)
    data, _context, proof = assess(definition, configuration, events)
    assert "DOCUMENTARY_SOURCE_CONTRADICTION:headers.0.total" in proof["global_issues"]
    for row in [data["payload"]["fields"], *data["payload"]["tables"]["items"]]:
        for cell in row.values():
            cell["reviewed"] = True
    extraction = SimpleNamespace(payload=data["payload"], engine=data["engine"], reviewed=True)
    assert not semantic_review_dto(extraction, definition)["automatic_approval"]


def test_verification_upgrade_preserves_acquisition_profile_and_rejects_downgrade(monkeypatch):
    from trackvance.document_semantic_general import assert_replay_binding
    from trackvance.operations_common import OperationError

    old, configuration, _events = fixtures.corroboration_fixture()
    new = compile_profile(old["semantic_general"]["contract"], automatic_verification="SEMANTIC_AUTO_V2")
    configuration.update(extraction_mode="SEMANTIC_GENERAL_V1", semantic_profile_sha256=old["semantic_general"]["profile_sha256"])
    assert old["semantic_general"]["contract"] == new["semantic_general"]["contract"]
    assert old["semantic_general"]["profile_sha256"] == new["semantic_general"]["profile_sha256"]
    assert "verification_contract" not in old["semantic_general"]
    assert "verification_contract_sha256" not in old["semantic_general"]
    assert_replay_binding(old, new, configuration)
    with pytest.raises(OperationError, match="contrato"):
        assert_replay_binding(new, old, configuration)
    altered = copy.deepcopy(new)
    altered["semantic_general"]["profile_sha256"] = "b" * 64
    with pytest.raises(OperationError, match="contrato"):
        assert_replay_binding(old, altered, configuration)


def test_original_v1_attestation_keeps_exact_ae70_contract_vector():
    from trackvance.document_normalization import canonical_hash

    definition, configuration, events = fixtures.corroboration_fixture()
    _data, _context, proof = fixtures._certify(definition, configuration, events)
    # Captured from exact ae70 LF source before accepting V2. The vector includes
    # original events, requests, source context, candidate and attestation.
    assert canonical_hash(proof) == "6ae0c8c524d14eb2372499a6a88136ed27e5b755d72dc18f30cd0295fc49c161"
    assert "source_checks" not in proof
    assert "verification_contract_sha256" not in proof


def test_conflicting_complete_source_inventories_are_not_waived_by_flags(monkeypatch):
    definition, configuration, events = v2_fixture(monkeypatch, paddle=True)
    def disagreeing_source(body):
        for block in body["parsing_res_list"]:
            if block["block_label"] == "table":
                block["block_content"] = block["block_content"].replace("<td>10.00</td>", "<td>11.00</td>")
    mutate(events, "PADDLE_PAGE_JSON", disagreeing_source)
    data, _context, proof = assess(definition, configuration, events)
    assert "SOURCE_INVENTORY_CONTRADICTION:items" in proof["global_issues"]
    assert not proof["table_coverage"]["items"]
    assert not any(path.startswith("items.") for path in proof["verified_cells"])
    for row in [data["payload"]["fields"], *data["payload"]["tables"]["items"]]:
        for cell in row.values():
            cell["reviewed"] = True
    extraction = SimpleNamespace(payload=data["payload"], engine=data["engine"], reviewed=True)
    assert not semantic_review_dto(extraction, definition)["automatic_approval"]


@pytest.mark.parametrize("source_case,expected", [
    ("NONE", "INDEPENDENT_SOURCE_REQUIRED"),
    ("ABSENT", "SOURCE_DATA_NOT_OBSERVED"),
    ("LITERAL", "ASSOCIATION_NOT_PROVEN"),
])
def test_source_absence_unobserved_datum_and_unproved_association_are_distinct(monkeypatch, source_case, expected):
    definition, configuration, events = v2_fixture(monkeypatch, native=source_case != "NONE")
    if source_case != "NONE":
        def modify(body):
            body["payload"]["lines"] = [line for line in body["payload"]["lines"] if not line["text"].startswith("Supplier:")]
            if source_case == "LITERAL":
                body["payload"]["lines"].append(fixtures._line("Alpha", .09))
        mutate(events, "NATIVE_SOURCE_PROBE_JSON", modify)
    data, _context, proof = assess(definition, configuration, events)
    observation = proof["corroboration"]["headers.0.issuer"]
    assert observation["status"] == "REVIEW_REQUIRED"
    assert expected in observation["reasons"]
    if source_case == "LITERAL":
        assert "SOURCE_DATA_NOT_OBSERVED" not in observation["reasons"]
        assert all(ref["proof_scope"] == "UNASSOCIATED_LITERAL_ONLY" for ref in observation["source_refs"])
    extraction = SimpleNamespace(payload=data["payload"], engine=data["engine"], reviewed=False)
    assert not semantic_review_dto(extraction, definition)["automatic_approval"]


def scalar_only_fixture(*, partial=False, paddle=False):
    from test_document_semantic_auto_lifecycle_098 import independent_qwen_events
    from test_document_semantic_general_098 import profile

    from trackvance.document_normalization import canonical_hash

    definition = compile_profile(profile(), automatic_verification="SEMANTIC_AUTO_V2")
    configuration = {"provider": "PADDLE_QWEN_LOCAL", "extraction_strategy": "QWEN_VISUAL",
        "automatic_verification": "BOUNDED_LOCAL_V1", "llm_model": "qwen3.5:9b-q4_K_M", "paddle_model": "PaddleOCR-VL-1.6"}
    events = independent_qwen_events(definition, configuration, "a" * 64)
    native = copy.deepcopy(events[0])
    native.update(kind="PHASE", stage="NATIVE_SOURCE_PROBE", page_one_based=None,
        payload={"source_page_count_verified": True, "artifacts": [fixtures._artifact("NATIVE_SOURCE_PROBE_JSON", {
            "engine": {"pages": 1}, "payload": {"pages": [1], "native_incomplete_pages": [1] if partial else [],
                "page_geometry": {"1": {"width": 600, "height": 800, "unit": "PDF_POINT"}},
                "lines": [fixtures._line("Reference: R-001", .1), fixtures._line("Printed amount: 12.50", .2)]}})]})
    events.insert(0, native)
    if paddle:
        page = copy.deepcopy(events[-2])
        page.update(stage="PADDLE_STRUCTURED", request_id="scalar-paddle", payload={
            "artifacts": [fixtures._artifact("PADDLE_PAGE_JSON", {"parsing_res_list": [
                {"block_label": "text", "block_content": "Reference: R-001", "block_bbox": [80, 100, 400, 120]},
                {"block_label": "text", "block_content": "Printed amount: 12.50", "block_bbox": [80, 200, 400, 220]}]})],
            "page_geometry": {"width": 1000, "height": 1000, "unit": "PIXEL"}})
        terminal = copy.deepcopy(events[-1])
        terminal.update(stage="PADDLE_STRUCTURED", request_id="scalar-paddle")
        events.extend([page, terminal])
    for number, event in enumerate(events, 1):
        event["sequence"] = number
        event["definition_sha256"] = canonical_hash(definition)
        event["configuration_sha256"] = canonical_hash(configuration)
    return definition, configuration, events


@pytest.mark.parametrize("partial", [False, True])
def test_final_scope_scalar_only_partial_source_blocks_auto_without_losing_cells(partial):
    definition, configuration, events = scalar_only_fixture(partial=partial)
    data, context, proof = assess(definition, configuration, events)
    assert len(proof["verified_cells"]) == 2 and proof["table_coverage"] == {}
    dto = semantic_review_dto(SimpleNamespace(payload=data["payload"], engine=data["engine"], reviewed=False), definition)
    if partial:
        assert not dto["automatic_approval"]
        assert "SOURCE_SCOPE_INCOMPLETE" in proof["issues"]
        assert "SOURCE_SCOPE_INCOMPLETE" not in proof["global_issues"]
        assert proof["source_checks"]["documentary_completeness"]["status"] == "PENDING"
        assert semantic_verification_decision(data, definition, context)["actionable"]
        for cell in data["payload"]["fields"].values():
            cell["reviewed"] = True
        reviewed = semantic_review_dto(SimpleNamespace(payload=data["payload"], engine=data["engine"], reviewed=True), definition)
        assert not reviewed["automatic_approval"]
        assert reviewed["source_checks"]["automatic_eligibility"]["status"] == "BLOCKED"
    else:
        assert dto["automatic_approval"]


def test_final_scope_complete_independent_paddle_can_repair_scalar_native_gap():
    definition, configuration, events = scalar_only_fixture(partial=True, paddle=True)
    data, _context, proof = assess(definition, configuration, events)
    assert proof["source_checks"]["text"]["status"] == "AVAILABLE"
    assert "SOURCE_SCOPE_INCOMPLETE" not in proof["issues"]
    assert semantic_review_dto(SimpleNamespace(payload=data["payload"], engine=data["engine"], reviewed=False), definition)["automatic_approval"]


def test_final_scope_native_unknown_text_columns_cannot_disappear_before_known_table(monkeypatch):
    definition, configuration, events = v2_fixture(monkeypatch)
    mutate(events, "NATIVE_SOURCE_PROBE_JSON", lambda body: body["payload"]["lines"].insert(2,
        fixtures._physical_line(["Unknown entries", "Unassigned captions", "Further text", "Unmapped values"], .15)))
    _data, _context, proof = assess(definition, configuration, events)
    assert not proof["table_coverage"]["items"]
    inventory = proof["coverage_details"]["items"]["source_inventory"][0]
    assert inventory["unclassified_source_refs"]
    assert "UNCLASSIFIED_SOURCE_SCOPE" in inventory["issues"]


def test_final_scope_paddle_text_occurrence_outside_html_table_is_not_silently_omitted(monkeypatch):
    definition, configuration, events = v2_fixture(monkeypatch, native=False, paddle=True)
    mutate(events, "PADDLE_PAGE_JSON", lambda body: body["parsing_res_list"].append({
        "block_label": "text", "block_content": "2026-10-24 Unrecorded movement -4.00 96.00", "block_bbox": [80, 550, 950, 570]}))
    _data, _context, proof = assess(definition, configuration, events)
    assert not proof["table_coverage"]["items"]
    inventory = proof["coverage_details"]["items"]["source_inventory"][0]
    assert inventory["unclassified_source_refs"]
    assert "UNCLASSIFIED_SOURCE_SCOPE" in inventory["issues"]
