"""Document scope survives an empty declared table set.

These pure synthetic unit sources exercise the validator boundary. They are
separate from the consumed native reserved evaluation and do not measure OCR.
"""
import copy
from types import SimpleNamespace

import pytest
import test_document_semantic_corroboration_098 as fixtures
import test_document_semantic_corroboration_v2_098 as v2_fixtures

from trackvance import document_semantic_corroboration as corroboration
from trackvance.document_automatic_verification import source_context
from trackvance.document_engine import extract
from trackvance.document_lossless import _native_events, adapt_saved_recognition
from trackvance.document_normalization import canonical_hash
from trackvance.document_semantic_general import compile_profile
from trackvance.document_validation_policy import semantic_quality_report


@pytest.fixture(autouse=True)
def explicit_semantic_activation(monkeypatch):
    monkeypatch.setenv("TRACKVANCE_SEMANTIC_GENERAL_ENABLED", "true")
    monkeypatch.setenv("TRACKVANCE_SEMANTIC_AUTO_ENABLED", "true")


def scalar_definition():
    return compile_profile({"id": "global-source-scope-unit", "name": "Global scope unit", "revision": 1,
        "document_type": "CUSTOM", "description": "One printed scalar is declared; other relevant source text is not waived.",
        "fields": [{"name": "account", "label": "Account", "critical": True}], "tables": []},
        automatic_verification="SEMANTIC_AUTO_V2")


def source_fixture(mechanism, *, extra=None, footer=False):
    definition = scalar_definition()
    if mechanism == "NATIVE":
        lines = [fixtures._line("Account: UNIT-584", .1)]
        if extra:
            lines.append(fixtures._line(extra, .4))
        if footer:
            lines.append(fixtures._line("Document page 1 of 1", .94))
        words = [{**word, "id": f"native-word-{index}-{ordinal}"}
            for index, line in enumerate(lines) for ordinal, word in enumerate(line["words"])]
        source = {"identity": {"stage": "NATIVE_SOURCE_PROBE", "mechanism": "PDF_NATIVE", "model": "PDF_NATIVE",
            "event_hashes": ["native-source-unit"], "completed_pages": [1]}, "artifact_sha256": "b" * 64,
            "pages": [{"page": 1, "bands": corroboration._bands(words)}], "expected_pages": [1],
            "complete": True, "unavailable_pages": [], "incomplete_scope_pages": []}
        collector = []
        inventories, _headers = corroboration._native_inventory_v2(source, definition, unclassified_scope=collector)
        facts = corroboration._native_facts_v2(source)
        ir = {"expected_pages": 1}
        return definition, inventories, collector, facts, ir, source
    identity = {"stage": "PADDLE_STRUCTURED", "mechanism": "PADDLE", "model": "Paddle-source-unit",
        "event_hashes": ["paddle-source-unit"], "completed_pages": [1]}
    blocks = [{"id": "paddle-account", "type": "text", "text": "Account: UNIT-584", "geometry": [80, 100, 350, 124]}]
    if extra:
        blocks.append({"id": "paddle-extra", "type": "text", "text": extra, "geometry": [80, 400, 500, 424]})
    if footer:
        blocks.append({"id": "paddle-footer", "type": "text", "text": "Document page 1 of 1", "geometry": [80, 940, 350, 964]})
    ir = {"expected_pages": 1, "pages": [{"stage": "PADDLE_STRUCTURED", "page_one_based": 1,
        "geometry": {"unit": "PIXEL", "width": 1000, "height": 1000}, "blocks": blocks, "tables": []}]}
    control, collector = {"identity": identity}, []
    inventories, _headers = corroboration._paddle_inventory_v2(ir, control, definition, unclassified_scope=collector)
    facts = corroboration._paddle_facts_v2(ir, control)
    return definition, inventories, collector, facts, ir, None


def source_check_inputs():
    proof = {"global_issues": [], "issues": [], "recognition_complete": True, "durable_source_bound": True,
        "corroboration": {"headers.0.account": {"status": "VERIFIED", "reasons": [], "association": [{"basis": "UNIT_DECLARED_RELATION"}]}},
        "verified_cells": {"headers.0.account": "current-cell-unit"}, "table_coverage": {}, "coverage_details": {}}
    data = {"payload": {"fields": {"account": {"validity": "VALID", "value": "UNIT-584"}}},
        "engine": {"execution": {"completed_pages": [1]}}}
    return proof, data


@pytest.mark.parametrize("mechanism", ["NATIVE", "PADDLE"])
@pytest.mark.parametrize("extra", ["Unassigned amount 17.60", "Additional delivery instruction remains unexplained"])
def test_unclassified_regions_block_document_auto_without_declared_tables(mechanism, extra):
    definition, inventories, collector, facts, ir, native = source_fixture(mechanism, extra=extra)
    assert inventories == {} and len(collector) == 1
    proof, data = source_check_inputs()
    corroboration._complete_source_checks_v2(proof, data, definition, native, [inventories], facts, ir,
        unclassified_scope=collector)
    assert "UNCLASSIFIED_SOURCE_SCOPE" in proof["issues"]
    assert "UNCLASSIFIED_SOURCE_SCOPE" not in proof["global_issues"]
    assert proof["durable_source_bound"] is True
    assert proof["source_checks"]["integrity"] == {"status": "VERIFIED", "reasons": [],
        "source_refs": proof["source_checks"]["integrity"]["source_refs"]}
    assert proof["source_checks"]["text"]["status"] == "AVAILABLE"
    assert proof["source_checks"]["documentary_completeness"]["status"] == "PENDING"
    assert proof["source_checks"]["unclassified_scope"] == {"status": "PENDING", "count": 1,
        "source_refs": collector, "reasons": ["UNCLASSIFIED_SOURCE_SCOPE"]}
    assert proof["source_checks"]["automatic_eligibility"]["status"] == "BLOCKED"
    assert proof["verified_cells"] == {"headers.0.account": "current-cell-unit"}
    # This pure planner scenario represents a Qwen-primary request; it does not
    # create source authority or invoke an additional reader.
    proof.update(recognition_sources=[{"mechanism": "QWEN"}], native_scope_complete=True)
    decision = corroboration._verification_decision_v2(data, definition, proof)
    assert decision["actionable"]
    assert {reason["kind"] for reason in decision["reasons"]} == {"INCIDENT"}
    assert decision["pending_controls"][0]["code"] == "UNCLASSIFIED_SOURCE_SCOPE"


@pytest.mark.parametrize("mechanism", ["NATIVE", "PADDLE"])
@pytest.mark.parametrize("footer", [False, True])
def test_complete_scalar_sources_keep_identical_empty_scope_proof(mechanism, footer):
    definition, inventories, collector, facts, ir, native = source_fixture(mechanism, footer=footer)
    assert inventories == {} and collector == []
    proof, data = source_check_inputs()
    baseline = copy.deepcopy(proof)
    corroboration._complete_source_checks_v2(baseline, data, definition, native, [inventories], facts, ir)
    corroboration._complete_source_checks_v2(proof, data, definition, native, [inventories], facts, ir,
        unclassified_scope=collector)
    assert canonical_hash(proof) == canonical_hash(baseline)
    assert "unclassified_scope" not in proof["source_checks"]
    assert proof["global_issues"] == [] and proof["issues"] == []
    assert proof["source_checks"]["documentary_completeness"]["status"] == "VERIFIED"


@pytest.mark.parametrize("native,paddle", [(True, False), (False, True)])
def test_existing_complete_table_attestation_is_canonically_unchanged(monkeypatch, native, paddle):
    definition, configuration, events = v2_fixtures.v2_fixture(monkeypatch, native=native, paddle=paddle)
    complete = corroboration._complete_source_checks_v2

    def without_new_scope_keyword(*args, **kwargs):
        kwargs.pop("unclassified_scope", None)
        return complete(*args, **kwargs)

    with monkeypatch.context() as context:
        context.setattr(corroboration, "_complete_source_checks_v2", without_new_scope_keyword)
        _data, _source, baseline = v2_fixtures.assess(definition, configuration, events)
    _data, _source, proof = v2_fixtures.assess(definition, configuration, events)
    assert proof["table_coverage"] == {"items": True}
    assert len(proof["verified_cells"]) == 9
    assert proof["issues"] == []
    assert "unclassified_scope" not in proof["source_checks"]
    assert canonical_hash(proof) == canonical_hash(baseline)
    assert proof["attestation_sha256"] == baseline["attestation_sha256"]


@pytest.mark.parametrize("native,paddle", [(True, False), (False, True)])
def test_v1_never_calls_new_scope_collectors(monkeypatch, native, paddle):
    definition, configuration, events = fixtures.corroboration_fixture(native=native, paddle=paddle)
    _data, _context, baseline = fixtures._certify(definition, configuration, events)

    def forbidden(*_args, **_kwargs):
        raise AssertionError("V1_MUST_NOT_USE_V2_SCOPE_COLLECTOR")

    with monkeypatch.context() as context:
        for name in ("_native_inventory_v2", "_paddle_inventory_v2", "_complete_source_checks_v2"):
            context.setattr(corroboration, name, forbidden)
        _data, _context, proof = fixtures._certify(definition, configuration, events)
    assert canonical_hash(proof) == canonical_hash(baseline)


def measured_table_source(counter="Document page 1 of 1", *, top=.94, stretch=2):
    definition = compile_profile({"id": "wide-footer-scope-unit", "name": "Wide footer scope", "revision": 1,
        "document_type": "CUSTOM", "description": "Measured columns do not redefine a documentary footer.",
        "fields": [{"name": "account", "label": "Account"}],
        "tables": [{"name": "items", "description": "Every independently measured row.", "fields": [
            {"name": "item_code", "label": "Code"}, {"name": "description", "label": "Description"},
            {"name": "quantity", "label": "Quantity", "logical_type": "DECIMAL"},
            {"name": "amount", "label": "Amount", "logical_type": "DECIMAL"}]}]},
        route="NATIVE_FIRST", automatic_verification="SEMANTIC_AUTO_V2")
    positions = [.05, .20, .38, .65]

    def physical(values, y):
        words = []
        for value, left in zip(values, positions, strict=True):
            for token in value.split():
                words.append(fixtures._word(token, left, y))
                left = words[-1]["region"][2] + .004
        return {"words": words}

    lines = [fixtures._line("Account: UNIT-751", .1),
        physical(["Code", "Description", "Quantity", "Amount"], .2),
        physical(["C7", "Bracket", "1", "9.00"], .3), physical(["C7", "Sleeve", "2", "12.00"], .4)]
    footer = fixtures._line(counter, top, left=.05)
    for word in footer["words"]:
        word["region"][0] = .05 + (word["region"][0] - .05) * stretch
        word["region"][2] = .05 + (word["region"][2] - .05) * stretch
    lines.append(footer)
    words = [{**word, "id": f"wide-word-{index}-{ordinal}"}
        for index, line in enumerate(lines) for ordinal, word in enumerate(line["words"])]
    source = {"identity": {"stage": "NATIVE_SOURCE_PROBE", "mechanism": "PDF_NATIVE", "model": "PDF_NATIVE",
        "event_hashes": ["wide-footer-source-unit"], "completed_pages": [1]}, "artifact_sha256": "c" * 64,
        "pages": [{"page": 1, "bands": corroboration._bands(words)}], "expected_pages": [1],
        "complete": True, "unavailable_pages": [], "incomplete_scope_pages": []}
    return definition, source


@pytest.mark.parametrize("stretch,crossed_columns", [(2, 2), (4, 3)])
def test_exact_measured_footer_counter_can_cross_multiple_table_columns(stretch, crossed_columns):
    definition, source = measured_table_source(stretch=stretch)
    inventory, _observations = corroboration._native_inventory_v2(source, definition)
    table = inventory["items"]
    assert table["complete"] and not table["issues"]
    assert len(table["rows"]) == 2
    assert table["ignored_page_counters"][0]["literal"] == "Document page 1 of 1"
    footer = source["pages"][0]["bands"][-1]
    boundaries = [.14, .309, .529]
    columns = {sum((word["region"][0] + word["region"][2]) / 2 >= boundary for boundary in boundaries)
        for word in footer["words"]}
    assert len(columns) == crossed_columns


@pytest.mark.parametrize("counter,top", [("Document page 1 of 1", .7), ("Document page 1 of 2", .94),
    ("Section page 1 of 1", .94), ("Document page 1 of 1 total 12.50", .94)])
def test_body_wrong_count_section_and_extra_text_are_not_ignored_as_counters(counter, top):
    definition, source = measured_table_source(counter, top=top)
    inventory, _observations = corroboration._native_inventory_v2(source, definition)
    assert not inventory["items"].get("ignored_page_counters")
    assert not inventory["items"]["complete"]


@pytest.mark.parametrize("extra", ["Unassigned amount 21.70", "Undeclared delivery instruction remains relevant"])
def test_certified_current_scope_issue_reaches_final_quality_without_losing_verified_cells(extra):
    from trackvance.document_semantic_evidence import semantic_review_dto
    from trackvance.document_service import issues as publication_issues

    definition = scalar_definition()
    configuration = {"provider": "PADDLE_QWEN_LOCAL", "extraction_strategy": "NATIVE_FIRST",
        "automatic_verification": "BOUNDED_LOCAL_V1", "llm_model": "qwen3.5:9b-q4_K_M", "paddle_model": "PaddleOCR-VL-1.6"}
    lines = [fixtures._line("Account: UNIT-584", .1), fixtures._line(extra, .4)]
    payload = extract(lines, definition, {"1": {"width": 600, "height": 800, "unit": "PDF_POINT"}})
    payload.update(pages=[1], native_incomplete_pages=[])
    native = {"payload": payload, "engine": {"key": "POPPLER_TESSERACT_CPU_V1", "pages": 1}}
    probe = {"version": 1, "kind": "PHASE", "sequence": 1, "request_id": "native-scope-unit-probe",
        "attempt_id": "native-scope-unit-attempt", "source_sha256": "a" * 64,
        "configuration_sha256": canonical_hash(configuration), "definition_sha256": canonical_hash(definition),
        "stage": "NATIVE_SOURCE_PROBE", "page_one_based": None, "expected_pages": 1,
        "payload": {"source_page_count_verified": True, "artifacts": [fixtures._artifact("NATIVE_SOURCE_PROBE_JSON", native)]}}
    probe["event_sha256"] = canonical_hash(probe)
    events = [probe, *_native_events(native, "a" * 64, canonical_hash(configuration),
        {"attempt_id": "native-scope-unit-attempt"}, definition_sha256=canonical_hash(definition))]
    fixtures._reseal(events)
    data = adapt_saved_recognition(events, definition, configuration, source_sha256="a" * 64,
        attempt_context={"expected_pages": 1})
    context = corroboration.semantic_source_context(events, source_context(events, "a" * 64))
    proof = corroboration.certify_semantic_candidate(data, definition, "a" * 64, source_context=context)
    data["engine"]["validation"] = proof
    current = SimpleNamespace(payload=data["payload"], engine=data["engine"], reviewed=False)
    quality = semantic_quality_report(current, definition)
    assert corroboration.trusted_semantic_proof(current, definition) == proof
    assert proof["durable_source_bound"] and proof["source_checks"]["integrity"]["status"] == "VERIFIED"
    assert proof["global_issues"] == []
    assert "headers.0.account" in proof["verified_cells"]
    assert proof["source_checks"]["documentary_completeness"]["status"] == "PENDING"
    assert proof["source_checks"]["automatic_eligibility"]["status"] == "BLOCKED"
    assert quality["status"] == "REVIEW_REQUIRED"
    assert "UNCLASSIFIED_SOURCE_SCOPE" in quality["issues"]
    assert "REVIEW_REQUIRED:headers.0.account" not in quality["issues"]
    dto = semantic_review_dto(current, definition)
    assert not dto["automatic_approval"]
    assert dto["source_checks"]["documentary_completeness"]["status"] == "PENDING"
    assert dto["source_checks"]["automatic_eligibility"]["status"] == "BLOCKED"
    assert "UNCLASSIFIED_SOURCE_SCOPE" in publication_issues(current, definition)
