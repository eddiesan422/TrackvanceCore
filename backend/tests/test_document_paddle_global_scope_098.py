"""Uninterpreted original table regions cannot disappear from global scope.

All recognition bytes and registry receipts below are synthetic unit fixtures;
the existing request producer and final proof/report contracts remain real.
"""
import copy
from types import SimpleNamespace

import pytest
import test_document_semantic_corroboration_098 as receipts
import test_document_semantic_corroboration_v2_098 as sources
from test_document_semantic_auto_lifecycle_098 import independent_qwen_events
from test_document_semantic_general_098 import artifact, fixture_events
from test_document_semantic_general_098 import cell as observed_cell

from trackvance import document_semantic_corroboration as corroboration
from trackvance.document_automatic_verification import source_context
from trackvance.document_lossless import adapt_saved_recognition
from trackvance.document_quality import validate_candidate_contract
from trackvance.document_semantic_evidence import semantic_review_dto
from trackvance.document_semantic_general import compile_profile
from trackvance.document_validation_policy import semantic_quality_report


@pytest.fixture(autouse=True)
def explicit_synthetic_auto(monkeypatch):
    monkeypatch.setenv("TRACKVANCE_SEMANTIC_GENERAL_ENABLED", "true")
    monkeypatch.setenv("TRACKVANCE_SEMANTIC_AUTO_ENABLED", "true")


def scalar_source(table_content, *, table_present=True, footer=False, block_label="table"):
    definition = compile_profile({"id": "paddle-original-region-unit", "name": "Paddle region unit", "revision": 1,
        "document_type": "CUSTOM", "description": "Every original source region retains its coverage obligation.",
        "fields": [{"name": "account", "label": "Account", "critical": True}], "tables": []},
        automatic_verification="SEMANTIC_AUTO_V2")
    configuration = {"provider": "PADDLE_QWEN_LOCAL", "extraction_strategy": "QWEN_VISUAL",
        "automatic_verification": "BOUNDED_LOCAL_V1", "llm_model": "qwen3.5:9b-q4_K_M", "paddle_model": "PaddleOCR-VL-1.6"}
    observations = {"fields": {"account": observed_cell("UNIT-584")}, "tables": {}}
    probe = fixture_events([observations], definition, configuration)[0]
    qwen = independent_qwen_events(definition, configuration, "a" * 64, observations=observations)
    page = copy.deepcopy(next(event for event in qwen if event["kind"] == "PAGE"))
    page.update(request_id="paddle-original-region-unit", stage="PADDLE_STRUCTURED")
    blocks = [{"block_label": "text", "block_content": "Account: UNIT-584", "block_bbox": [80, 100, 350, 124]}]
    if table_present:
        blocks.append({"block_label": block_label, "block_content": table_content,
            "block_bbox": [80, 940, 700, 964] if footer else [80, 400, 700, 600]})
    page["payload"] = {"page_geometry": {"width": 1000, "height": 1000, "unit": "PIXEL"},
        "recognition_status": "COMPLETE", "artifacts": [artifact("PADDLE_PAGE_JSON", {"parsing_res_list": blocks})]}
    terminal = copy.deepcopy(qwen[-1])
    terminal.update(request_id=page["request_id"], stage="PADDLE_STRUCTURED")
    events = [probe, *qwen, page, terminal]
    for index, event in enumerate(events, 1):
        event["sequence"] = index
    return definition, configuration, events


def assess_scalar(events, definition, configuration):
    receipts._reseal(events)
    data = adapt_saved_recognition(events, definition, configuration, source_sha256="a" * 64,
        attempt_context={"expected_pages": 1})
    validate_candidate_contract(data, definition)
    context = corroboration.semantic_source_context(events, source_context(events, "a" * 64))
    proof = corroboration.certify_semantic_candidate(data, definition, "a" * 64, source_context=context)
    data["engine"]["validation"] = proof
    extraction = SimpleNamespace(payload=data["payload"], engine=data["engine"], reviewed=False)
    assert corroboration.trusted_semantic_proof(extraction, definition) == proof
    return data, proof, semantic_quality_report(extraction, definition), semantic_review_dto(extraction, definition)


@pytest.mark.parametrize("content", [None, "Description Amount\nCharge 17.60", "Account: UNIT-584", "",
    "<table></table>", "<table><table><tr><td>Charge</td><td>17.60</td></tr></table></table>",
    '<table><tr><td colspan="invalid">Account</td><td>UNIT-584</td></tr></table>',
    "<table><tr><td>Account</td><td>UNIT-584</td></tr><table><tr><td>Charge</td></tr></table></table>",
    "Unmapped 17.60<table><tr><th>Account</th><td>UNIT-584</td></tr></table>",
    "<table><caption>Unmapped 17.60</caption><tr><th>Account</th><td>UNIT-584</td></tr></table>",
    "<table><tr><th>Account</th><td>UNIT-584</td></tr></table>Unmapped 17.60"])
def test_uninterpreted_original_table_blocks_auto_even_without_declared_tables(content):
    definition, configuration, events = scalar_source(content)
    data, proof, quality, dto = assess_scalar(events, definition, configuration)
    assert proof["verified_cells"].keys() == {"headers.0.account"}
    assert proof["global_issues"] == []
    assert proof["issues"] == ["UNCLASSIFIED_SOURCE_SCOPE"]
    assert quality["status"] == "REVIEW_REQUIRED" and not dto["automatic_approval"]
    assert proof["source_checks"]["text"]["status"] == "AVAILABLE"
    refs = proof["source_checks"]["unclassified_scope"]["source_refs"]
    paddle = next(page for page in data["payload"]["document_representation"]["pages"] if page["stage"] == "PADDLE_STRUCTURED")
    original = next(block for block in paddle["blocks"] if block["type"] == "table")
    assert original["text"] == content
    ids = [reference["id"] for reference in refs]
    assert ids.count(original["id"]) == 1 and len(ids) == len(set(ids))
    allowed = {original["id"], *(source_cell["id"] for table in paddle["tables"]
        if table["source_block_id"] == original["id"] for source_cell in table["cells"])}
    assert set(ids) <= allowed
    assert all(reference["stage"] == "PADDLE_STRUCTURED" and reference["page"] == 1
        and reference["mechanism"] == "PADDLE" for reference in refs)


def test_footer_looking_unparsed_table_is_not_a_page_counter_exemption():
    definition, configuration, events = scalar_source("Document page 1 of 1", footer=True)
    _data, proof, quality, dto = assess_scalar(events, definition, configuration)
    assert proof["source_checks"]["unclassified_scope"]["count"] == 1
    assert quality["status"] == "REVIEW_REQUIRED" and not dto["automatic_approval"]


@pytest.mark.parametrize("element", ["<img src='unread-unit-image'>", "<svg></svg>",
    "<canvas></canvas>", "<object></object>"])
def test_opaque_embedded_occurrence_is_not_covered_by_account_cell_text(element):
    html = "<table><tr><th>Account</th><td>UNIT-584" + element + "</td></tr></table>"
    definition, configuration, events = scalar_source(html)
    _data, proof, quality, dto = assess_scalar(events, definition, configuration)
    assert proof["source_checks"]["unclassified_scope"]["count"] == 1
    assert quality["status"] == "REVIEW_REQUIRED" and not dto["automatic_approval"]


@pytest.mark.parametrize("label", ["image", "chart", "text"])
def test_observed_layout_with_empty_string_is_opaque_not_proven_empty(label):
    definition, configuration, events = scalar_source("", block_label=label)
    data, proof, quality, dto = assess_scalar(events, definition, configuration)
    assert proof["verified_cells"].keys() == {"headers.0.account"}
    assert proof["issues"] == ["UNCLASSIFIED_SOURCE_SCOPE"] and proof["global_issues"] == []
    assert proof["source_checks"]["documentary_completeness"]["status"] == "PENDING"
    assert quality["status"] == "REVIEW_REQUIRED" and not dto["automatic_approval"]
    paddle = next(page for page in data["payload"]["document_representation"]["pages"] if page["stage"] == "PADDLE_STRUCTURED")
    original = paddle["blocks"][-1]
    assert original["type"] == label and original["text"] == ""
    assert proof["source_checks"]["unclassified_scope"]["source_refs"][0]["id"] == original["id"]


@pytest.mark.parametrize("content", [None, 0, {"uninterpreted": True}])
def test_nonstring_layout_is_fail_closed_resilience_not_a_claim_about_official_producer(content):
    definition, _configuration, _events = scalar_source(None)
    identity = {"stage": "PADDLE_STRUCTURED", "mechanism": "PADDLE", "event_hashes": []}
    ir = {"pages": [{"stage": "PADDLE_STRUCTURED", "page_one_based": 1, "geometry": {}, "tables": [],
        "blocks": [{"id": "opaque-input-region", "type": "image", "text": content, "geometry": None}]}]}
    facts = corroboration._paddle_facts_v2(ir, {"identity": identity})
    assert len(facts) == 1 and facts[0]["source_scope_unparsed"] is True
    assert facts[0]["region"] is None and facts[0]["geometry_kind"] == "UNKNOWN"
    assert corroboration._unclassified_scope_v2(facts, set(), definition, 1) == [facts[0]["ref"]]


@pytest.mark.parametrize("table_present", [False, True])
@pytest.mark.parametrize("html", ["<table><tr><th>Account</th><td>UNIT-584</td></tr></table>",
    "<table><tr><th>Account<td>UNIT-584</table>",
    "<table><tr><th>Ac&#99;ount<br></th><td>UNIT&#45;584</td></tr></table>"])
def test_complete_scalar_and_fully_associated_valid_kv_keep_auto(table_present, html):
    definition, configuration, events = scalar_source(html, table_present=table_present)
    data, proof, quality, dto = assess_scalar(events, definition, configuration)
    assert proof["issues"] == [] and quality["status"] == "AUTO_VERIFIED" and dto["automatic_approval"]
    assert "unclassified_scope" not in proof["source_checks"]
    identity = next(item for item in proof["recognition_sources"] if item["mechanism"] == "PADDLE")
    facts = corroboration._paddle_facts_v2(data["payload"]["document_representation"], {"identity": identity})
    assert not any(fact.get("source_scope_unparsed") for fact in facts)
    assert len(facts) == (3 if table_present else 1)


def test_unparsed_block_is_global_with_an_independently_complete_declared_table(monkeypatch):
    definition, configuration, events = sources.v2_fixture(monkeypatch, native=False, paddle=True)
    def extra(body):
        body["parsing_res_list"].append({"block_label": "table", "block_content": "Uninterpreted charge 17.60",
            "block_bbox": [80, 550, 900, 580]})
    sources.mutate(events, "PADDLE_PAGE_JSON", extra)
    data, _context, proof = sources.assess(definition, configuration, events)
    extraction = SimpleNamespace(payload=data["payload"], engine=data["engine"], reviewed=False)
    assert "UNCLASSIFIED_SOURCE_SCOPE" in proof["issues"]
    assert proof["source_checks"]["unclassified_scope"]["count"] == 1
    assert proof["table_coverage"] == {"items": False}
    assert semantic_quality_report(extraction, definition)["status"] == "REVIEW_REQUIRED"
    assert not semantic_review_dto(extraction, definition)["automatic_approval"]


def test_unparsed_region_cannot_be_consumed_by_caption_or_another_block_table():
    definition, _configuration, _events = scalar_source(None)
    identity = {"stage": "PADDLE_STRUCTURED", "mechanism": "PADDLE", "event_hashes": []}
    page = {"stage": "PADDLE_STRUCTURED", "page_one_based": 1, "geometry": {},
        "blocks": [{"id": "original-table-region", "type": "table", "text": "Account: UNIT-584", "geometry": None}],
        "tables": [{"source_block_id": "different-original-block", "cells": [{"id": "cell", "text": "UNIT-584"}], "errors": []}]}
    facts = corroboration._paddle_facts_v2({"pages": [page]}, {"identity": identity})
    unresolved = next(fact for fact in facts if fact["ref"]["id"] == "original-table-region")
    assert unresolved["source_scope_unparsed"] is True and unresolved["region"] is None
    assert unresolved["geometry_kind"] == "UNKNOWN"
    assert corroboration._unclassified_scope_v2([unresolved], {"original-table-region"}, definition, 1) == [unresolved["ref"]]


# Literal prior source-facts reader from fe3, retained solely as a compatibility
# witness for the already complete source path (not a second recognizer).
def _prior_paddle_facts_v2(ir, control):
    from trackvance.document_semantic_corroboration import _box, _ref

    identity = control["identity"]
    result = []
    for page in ir["pages"]:
        if page["stage"] != identity["stage"]:
            continue
        geometry = page.get("geometry") or {}
        for block in page["blocks"]:
            if block["type"] == "table" or not isinstance(block.get("text"), str):
                continue
            box = block.get("geometry")
            region = None
            if (isinstance(box, list) and len(box) == 4 and geometry.get("unit") == "PIXEL"
                    and geometry.get("width") and geometry.get("height")):
                candidate = [box[0] / geometry["width"], box[1] / geometry["height"],
                    box[2] / geometry["width"], box[3] / geometry["height"]]
                if _box(candidate):
                    region = candidate
            result.append({"text": block["text"], "page": page["page_one_based"], "region": region,
                "geometry_kind": "MEASURED_BLOCK" if region else "UNKNOWN",
                "source_independent": True, "block_role": block["type"],
                "ref": _ref(identity, page["page_one_based"], block["id"],
                    geometry_kind="MEASURED_BLOCK" if region else "UNKNOWN")})
        for table in page["tables"]:
            for cell in table["cells"]:
                result.append({"text": cell["text"], "page": page["page_one_based"],
                    "region": None, "geometry_kind": "UNKNOWN", "source_independent": True,
                    "ref": _ref(identity, page["page_one_based"], cell["id"])})
    return result


def test_previous_complete_table_proof_remains_canonically_identical(monkeypatch):
    definition, configuration, events = sources.v2_fixture(monkeypatch, native=False, paddle=True)
    _data, _context, actual = sources.assess(definition, configuration, events)
    with monkeypatch.context() as context:
        context.setattr(corroboration, "_paddle_facts_v2", _prior_paddle_facts_v2)
        _data, _context, baseline = sources.assess(definition, configuration, events)
    assert actual == baseline and actual["table_coverage"] == {"items": True}
    assert actual["issues"] == []


@pytest.mark.parametrize(("label", "content"), [("table", "Unmapped 17.60"), ("image", ""),
    ("table", "<table><table><tr><td>Unmapped 17.60</td></tr></table></table>")])
def test_previous_incomplete_scope_attestation_loses_authority_on_regeneration(monkeypatch, label, content):
    definition, configuration, events = scalar_source(content, block_label=label)
    with monkeypatch.context() as context:
        context.setattr(corroboration, "_paddle_facts_v2", _prior_paddle_facts_v2)
        data, previous, quality, dto = assess_scalar(events, definition, configuration)
        assert quality["status"] == "AUTO_VERIFIED" and dto["automatic_approval"]
    preserved = copy.deepcopy(previous)
    extraction = SimpleNamespace(payload=data["payload"], engine=data["engine"], reviewed=False)
    assert corroboration.trusted_semantic_proof(extraction, definition) is None
    assert semantic_quality_report(extraction, definition)["status"] == "REVIEW_REQUIRED"
    assert not semantic_review_dto(extraction, definition)["automatic_approval"]
    assert data["engine"]["validation"] == preserved
