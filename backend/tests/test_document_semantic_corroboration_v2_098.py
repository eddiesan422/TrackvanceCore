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


def v2_fixture(monkeypatch, *, scalar_before_detail=True, **kwargs):
    # Generate an actual new request contract. Do not relabel a saved request.
    with monkeypatch.context() as context:
        context.setattr(fixtures, "compile_profile",
            lambda profile, **_options: compile_profile(profile, automatic_verification="SEMANTIC_AUTO_V2"))
        definition, configuration, events = fixtures.corroboration_fixture(**kwargs)
    if scalar_before_detail:
        # A native scalar above the detail header has independent scope. The
        # old fixture's footer without a boundary is tested separately below.
        def scalar_scope(body):
            body["payload"]["lines"] = [fixtures._line(line["text"], .16, page=line["page"])
                if line["text"].startswith("Total due:") else line for line in body["payload"]["lines"]]
        mutate(events, "NATIVE_SOURCE_PROBE_JSON", scalar_scope)
    return definition, configuration, events


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
    assert "items.0.item_code" not in proof["verified_cells"]
    assert "OPTIONAL_ABSENCE_UNVERIFIED" in proof["corroboration"]["items.0.item_code"]["reasons"]


def test_complete_local_source_can_prove_optional_column_absence_despite_missing_output_row(monkeypatch):
    definition, configuration, events = v2_fixture(monkeypatch)
    mutate(events, "NATIVE_SOURCE_PROBE_JSON", lambda body: body["payload"]["lines"].append(
        fixtures._physical_line(["Nuts", "1", "2.00", "2.00"], .31)))
    _data, _context, proof = assess(definition, configuration, events)
    assert not proof["table_coverage"]["items"] and "UNMATCHED_SOURCE_ROW:items" in proof["issues"]
    assert "items.0.item_code" in proof["verified_cells"]


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
            if line["page"] != 2 or line["region"][1] > .2]
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
            if line["page"] != 2 or line["region"][1] > .2]
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
    definition, configuration, events = v2_fixture(monkeypatch)
    # Both values precede the start of the physical detail columns and thus
    # have the same proven scalar role. No uncertain footer is needed.
    mutate(events, "NATIVE_SOURCE_PROBE_JSON", lambda body: body["payload"]["lines"].append(
        fixtures._line("Total due: 20.00", .18)))
    data, _context, proof = assess(definition, configuration, events)
    assert "DOCUMENTARY_SOURCE_CONTRADICTION:headers.0.total" in proof["global_issues"]
    for row in [data["payload"]["fields"], *data["payload"]["tables"]["items"]]:
        for cell in row.values():
            cell["reviewed"] = True
    extraction = SimpleNamespace(payload=data["payload"], engine=data["engine"], reviewed=True)
    assert not semantic_review_dto(extraction, definition)["automatic_approval"]


def test_native_scope_uncertainty_on_next_page_does_not_become_a_proved_contradiction(monkeypatch):
    definition, configuration, events = v2_fixture(monkeypatch, count=2)
    def uncertain_second_page_scope(body):
        body["payload"]["lines"] = [fixtures._line("Total due: 20.00", .16, page=2)
            if line["page"] == 2 and line["text"].startswith("Total due:") else line
            for line in body["payload"]["lines"]]
    mutate(events, "NATIVE_SOURCE_PROBE_JSON", uncertain_second_page_scope)
    data, _, proof = assess(definition, configuration, events)
    assert "DOCUMENTARY_SOURCE_CONTRADICTION:headers.0.total" not in proof["global_issues"]
    assert "OBSERVATION_UNCERTAINTY:headers.0.total" in proof["issues"]
    trace = proof["corroboration"]["headers.0.total"]["rejected_associations"]
    assert any(item["status"] == "UNCERTAIN" and item["raw_observed"] == "20.00" for item in trace)
    assert not _ownership_dto(data, definition)["automatic_approval"]


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


def _detail_ownership_fixture(monkeypatch, mechanism, *, proposed, source_rows, scalar_separate=False,
                              scalar_name="closing_balance", scalar_label="Closing balance", scalar_value="100.00",
                              scalar_type="DECIMAL", partial=False):
    numeric = {"logical_type": "DECIMAL", "normalization": {"decimal_formats": "UNAMBIGUOUS_LOCALE_V1"}}
    columns = [{"name": "description", "label": "Description"}]
    if partial:
        columns.append({"name": "quantity", "label": "Quantity", **numeric})
    columns.append({"name": "amount", "label": "Amount", **numeric})
    field = {"name": scalar_name, "label": scalar_label, "logical_type": scalar_type, "critical": True}
    if scalar_type == "DECIMAL":
        field.update(numeric)
    profile = {"id": "detail-ownership-discriminant", "revision": 1, "name": "Detail ownership discriminant",
        "document_type": "CUSTOM", "description": "Keep every detail occurrence independently of caption text",
        "fields": [field], "tables": [{"name": "items", "kind": "TABLE",
            "description": "Every measured detail occurrence", "fields": columns}]}
    with monkeypatch.context() as context:
        context.setattr(fixtures, "compile_profile", lambda _profile, **_options:
            compile_profile(profile, automatic_verification="SEMANTIC_AUTO_V2"))
        definition, configuration, events = fixtures.corroboration_fixture(
            native=mechanism == "native", paddle=mechanism == "paddle")

    def proposals(body):
        def cell(value):
            return {"observed": value, "ambiguous": False, "alternatives": []}
        body.update(fields={scalar_name: cell(scalar_value)}, tables={"items": [
            {field["name"]: cell(value) for field, value in zip(columns, row, strict=True)} for row in proposed]})
    mutate(events, "QWEN_SELECTION_JSON", proposals)

    if mechanism == "native":
        def measured(row, top):
            words = []
            positions = [.08, .52, .72] if partial else [.08, .72]
            for value, left in zip(row, positions, strict=True):
                for token in (value or "").split():
                    word = fixtures._word(token, left, top)
                    words.append(word)
                    left = word["region"][2] + .004
            return {"text": " ".join(word["text"] for word in words), "page": 1, "method": "DIGITAL",
                "region": [words[0]["region"][0], top, words[-1]["region"][2], top + .012], "words": words}

        def native_source(body):
            body["payload"]["lines"] = [
                *([fixtures._line(f"{scalar_label}: {scalar_value}", .1)] if scalar_separate else []),
                measured([field["label"] for field in columns], .2),
                *(measured(row, .25 + index * .04) for index, row in enumerate(source_rows))]
        mutate(events, "NATIVE_SOURCE_PROBE_JSON", native_source)
    else:
        def paddle_source(body):
            html = "<table><tr>" + "".join(f"<th>{field['label']}</th>" for field in columns) + "</tr>"
            for row in source_rows:
                html += "<tr>" + "".join(f"<td>{value or ''}</td>" for value in row) + "</tr>"
            body["parsing_res_list"] = [{"block_label": "table", "block_content": html + "</table>",
                "block_bbox": [80, 200, 950, 500]}]
            if scalar_separate:
                body["parsing_res_list"].append({"block_label": "table", "block_bbox": [80, 800, 500, 850],
                    "block_content": f"<table><tr><th>{scalar_label}</th><td>{scalar_value}</td></tr></table>"})
        mutate(events, "PADDLE_PAGE_JSON", paddle_source)
    return definition, configuration, events


def _ownership_dto(data, definition):
    return semantic_review_dto(SimpleNamespace(payload=data["payload"], engine=data["engine"], reviewed=False), definition)


@pytest.mark.parametrize("mechanism", ["native", "paddle"])
def test_detail_row_matching_a_scalar_caption_remains_in_source_inventory(monkeypatch, mechanism):
    definition, configuration, events = _detail_ownership_fixture(monkeypatch, mechanism,
        proposed=[["Charge", "10.00"]], source_rows=[["Charge", "10.00"], ["Closing balance", "100.00"]])
    data, _, proof = assess(definition, configuration, events)
    inventory = proof["coverage_details"]["items"]["source_inventory"][0]
    assert [row["values"]["description"] for row in inventory["rows"]] == ["Charge", "Closing balance"]
    assert inventory["rows"][1]["source_refs"] and "UNMATCHED_SOURCE_ROW:items" in proof["issues"]
    assert not proof["table_coverage"]["items"] and "headers.0.closing_balance" not in proof["verified_cells"]
    rejected = proof["corroboration"]["headers.0.closing_balance"]["rejected_associations"]
    assert any(trace["status"] == "REJECTED" and trace["reason"] == "DECLARED_SOURCE_SCOPE_MISMATCH"
        and trace["raw_observed"] == "100.00" and trace["label_refs"] and trace["value_refs"] for trace in rejected)
    assert not _ownership_dto(data, definition)["automatic_approval"]


@pytest.mark.parametrize("mechanism", ["native", "paddle"])
def test_separate_scalar_and_all_detail_occurrences_can_still_verify(monkeypatch, mechanism):
    rows = [["Charge", "10.00"], ["Charge", "10.00"], ["Closing balance", "100.00"]]
    definition, configuration, events = _detail_ownership_fixture(monkeypatch, mechanism,
        proposed=rows, source_rows=rows, scalar_separate=True)
    data, _, proof = assess(definition, configuration, events)
    inventory = proof["coverage_details"]["items"]["source_inventory"][0]
    assert len({row["id"] for row in inventory["rows"]}) == 3
    assert proof["table_coverage"]["items"] and "headers.0.closing_balance" in proof["verified_cells"]
    assert _ownership_dto(data, definition)["automatic_approval"]


def test_paddle_unresolved_neighbour_alternative_stays_pending_with_covered_detail(monkeypatch):
    rows = [["Charge", "10.00"], ["Closing balance", "100.00"], ["Charge", "10.00"]]
    definition, configuration, events = _detail_ownership_fixture(monkeypatch, "paddle",
        proposed=rows, source_rows=rows, scalar_separate=True)
    data, _, proof = assess(definition, configuration, events)
    field = data["payload"]["fields"]["closing_balance"]
    paddle = next(item["candidate"] for item in field["candidate_alternatives"] if item.get("stage") == "PADDLE_STRUCTURED")
    bindings = paddle["association"]["bindings"]
    assert any(trace["rule"] == "DECLARED_TABLE_BELOW" and trace["raw_observed"] == "Charge"
        and trace["label_refs"] and trace["value_refs"] for trace in bindings)
    assert field["validity"] == "AMBIGUOUS" and field["value"] is None
    assert proof["table_coverage"]["items"] and "VALUE_UNRESOLVED:headers.0.closing_balance" in proof["issues"]
    assert not _ownership_dto(data, definition)["automatic_approval"]


def test_paddle_nonempty_row_before_first_header_in_same_table_requires_review(monkeypatch):
    definition, configuration, events = _detail_ownership_fixture(monkeypatch, "paddle",
        proposed=[["Charge", "10.00"]], source_rows=[["Closing balance", "100.00"], ["Charge", "10.00"]])

    def before_header(body):
        block = body["parsing_res_list"][0]
        row = "<tr><td>Closing balance</td><td>100.00</td></tr>"
        block["block_content"] = block["block_content"].replace(row, "", 1).replace("<table>", "<table>" + row, 1)

    mutate(events, "PADDLE_PAGE_JSON", before_header)
    data, _, proof = assess(definition, configuration, events)
    assert data["payload"]["fields"]["closing_balance"]["validity"] == "VALID"
    inventory = proof["coverage_details"]["items"]["source_inventory"][0]
    assert len(inventory["rows"]) == 1 and len(inventory["scope_refs"]) == 2
    assert "UNPARSED_SOURCE_STRUCTURE" in inventory["issues"] and not proof["table_coverage"]["items"]
    field = proof["corroboration"]["headers.0.closing_balance"]
    assert any(trace["status"] == "UNCERTAIN" and trace["eligible_for_field"]
        and trace["raw_observed"] == "100.00" and trace["label_refs"] and trace["value_refs"]
        for trace in field["rejected_associations"])
    assert "headers.0.closing_balance" not in proof["verified_cells"]
    assert not _ownership_dto(data, definition)["automatic_approval"]


@pytest.mark.parametrize("mechanism", ["native", "paddle"])
def test_incomplete_row_and_invalid_body_text_are_not_promoted_to_scalar(monkeypatch, mechanism):
    definition, configuration, events = _detail_ownership_fixture(monkeypatch, mechanism,
        proposed=[["Charge", "2", "10.00"]], source_rows=[["Charge", "2", "10.00"], ["Reference:", None, "invalid"]],
        scalar_name="reference", scalar_label="Reference", scalar_type="STRING", scalar_value="invalid", partial=True)
    data, _, proof = assess(definition, configuration, events)
    inventory = proof["coverage_details"]["items"]["source_inventory"][0]
    assert inventory["rows"][1]["values"]["amount"] == "invalid"
    assert inventory["rows"][1]["association_proven"] is False and inventory["rows"][1]["source_refs"]
    assert "headers.0.reference" not in proof["verified_cells"] and not proof["table_coverage"]["items"]
    assert not _ownership_dto(data, definition)["automatic_approval"]


def test_native_footer_caption_without_independent_scope_stays_pending(monkeypatch):
    definition, configuration, events = v2_fixture(monkeypatch, scalar_before_detail=False)
    data, _, proof = assess(definition, configuration, events)
    inventory = proof["coverage_details"]["items"]["source_inventory"][0]
    assert "UNPARSED_SOURCE_STRUCTURE" in inventory["issues"] and inventory["scope_refs"]
    assert "headers.0.total" not in proof["verified_cells"] and not proof["table_coverage"]["items"]
    assert not _ownership_dto(data, definition)["automatic_approval"]


@pytest.mark.parametrize("mechanism", ["native", "paddle"])
def test_body_text_resembling_column_captions_is_not_a_proved_repeated_header(monkeypatch, mechanism):
    definition, configuration, events = _detail_ownership_fixture(monkeypatch, mechanism, scalar_separate=True,
        proposed=[["Charge", "10.00"]], source_rows=[["Charge", "10.00"], ["Description", "Amount"]])
    data, _, proof = assess(definition, configuration, events)
    inventory = proof["coverage_details"]["items"]["source_inventory"][0]
    assert len(inventory["headers"]) == 1 and len(inventory["rows"]) == 2
    assert inventory["rows"][1]["values"] == {"description": "Description", "amount": "Amount"}
    assert not proof["table_coverage"]["items"] and not _ownership_dto(data, definition)["automatic_approval"]


@pytest.mark.parametrize("headerless", [False, True])
def test_paddle_following_page_needs_column_roles_not_only_width(monkeypatch, headerless):
    definition, configuration, events = v2_fixture(monkeypatch, count=2, native=False, paddle=True)
    if headerless:
        def remove_header(body):
            for block in body["parsing_res_list"]:
                if block["block_label"] == "table":
                    block["block_content"] = block["block_content"].replace(
                        "<tr><th>Description</th><th>Quantity</th><th>Unit price</th><th>Amount</th></tr>", "", 1)
        mutate(events, "PADDLE_PAGE_JSON", remove_header, page=2)
    data, _, proof = assess(definition, configuration, events)
    assert proof["table_coverage"]["items"] is not headerless
    assert _ownership_dto(data, definition)["automatic_approval"] is not headerless
    assert ("items.1.amount" in proof["verified_cells"]) is not headerless
