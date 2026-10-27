"""Opt-in literal source context distinguishes association from value equality."""
import copy
import importlib.util
from pathlib import Path

import pytest
from pydantic import ValidationError

from trackvance.document_lossless import map_representation
from trackvance.document_representation import VERSION, _selection
from trackvance.document_schemas import DocumentDefinition

POLICY = "DECLARED_LABEL_CONTEXT_V1"


def rule(label, page, section="", region=None):
    return {"label": label, "page": page, "region": region or [0, 0, 1, 1],
        "section_label": section, "direction": "RIGHT"}


def config(*, pages=(1, 2), region=None, critical=False):
    contracts = [rule("Service agreement", number, region=region) for number in pages]
    payments = [rule("Reference", number, "Payment information") for number in pages]
    return DocumentDefinition.model_validate({"fields": [
        {"name": "contract", "label": "Service contract identifier", "critical": critical,
            "source_association": POLICY, "visual": contracts[0], "visual_alternatives": contracts[1:]},
        {"name": "payment", "label": "Payment reference", "source_association": POLICY,
            "visual": payments[0], "visual_alternatives": payments[1:]},
    ], "validation_policy": {"allow_noncritical_source_bound": True}}).model_dump(mode="json")


def observation(value, anchor=None, section=None, *, ambiguous=False):
    return {"observed": value, "ambiguous": ambiguous, "alternatives": [],
        "source_context": {"anchor_label": anchor, "section_label": section}}


def page(number, contract=None, payment=None):
    return {"id": f"source-{number}", "stage": "QWEN_VISUAL", "page_one_based": number,
        "geometry": {"width": 1200, "height": 1600, "unit": "PIXEL"}, "blocks": [], "tables": [],
        "selection": {"fields": {"contract": contract or observation(None), "payment": payment or observation(None)},
            "tables": {}}}


def mapped(definition, *pages):
    return map_representation({"version": VERSION, "pages": list(pages)}, definition)


def test_different_contract_and_payment_values_follow_their_own_anchors():
    result = mapped(config(), page(1, observation("AG-008", "Service agreement"),
        observation("PAY-713", "Reference", "Payment information")))
    assert result["fields"]["contract"]["value"] == "AG-008"
    assert result["fields"]["payment"]["value"] == "PAY-713"


def test_the_same_value_legitimately_printed_under_both_anchors_is_retained():
    result = mapped(config(), page(1, observation("SAME-072", "Service agreement"),
        observation("SAME-072", "Reference", "Payment information")))
    assert all(cell["value"] == "SAME-072" and cell["validity"] == "VALID" for cell in result["fields"].values())
    assert result["semantic_conflicts"] == []


def test_absent_contract_does_not_borrow_a_payment_caption_on_the_same_page():
    source = page(1, observation("PAY-713", "Reference", "Payment information"),
        observation("PAY-713", "Reference", "Payment information"))
    original = copy.deepcopy(source)
    result = mapped(config(), source)
    contract = result["fields"]["contract"]
    assert contract["value"] is None and contract["validity"] == "MISSING"
    assert result["fields"]["payment"]["value"] == "PAY-713"
    rejected = contract["candidate_alternatives"][0]
    assert rejected["observed"] == "PAY-713" and rejected["eligible_for_field"] is False
    assert rejected["association"]["reason"] == "OBSERVED_ANCHOR_NOT_DECLARED_FOR_FIELD"
    assert result["document_representation"]["pages"][0]["selection"] == original["selection"]
    assert source == original


def test_contract_repeated_correctly_on_later_pages_is_not_first_page_selection():
    result = mapped(config(), page(1, observation("AG-008", "Service agreement")),
        page(2, observation("AG-008", "Service agreement")))
    contract = result["fields"]["contract"]
    assert contract["value"] == "AG-008" and contract["validity"] == "VALID"
    assert len(contract["evidence_refs"]) == 2
    assert {item["page"] for item in contract["candidate_alternatives"]} == {1, 2}
    later_only = mapped(config(), page(1), page(2, observation("AG-008", "Service agreement")))
    assert later_only["fields"]["contract"]["value"] == "AG-008"
    assert later_only["fields"]["contract"]["origin"]["page"] == 2


def test_a_page_without_the_contract_cannot_add_another_sections_reference():
    result = mapped(config(), page(1, observation("AG-008", "Service agreement")),
        page(2, observation("PAY-713", "Reference", "Payment information"),
            observation("PAY-713", "Reference", "Payment information")))
    contract = result["fields"]["contract"]
    assert contract["value"] == "AG-008" and contract["validity"] == "VALID"
    assert len(contract["evidence_refs"]) == 1
    assert any(item["observed"] == "PAY-713" and item.get("eligible_for_field") is False
        for item in contract["candidate_alternatives"])
    assert result["header_association_rejections"]["contract"][0]["page"] == 2


def test_real_conflict_under_the_same_declared_anchor_remains_ambiguous():
    result = mapped(config(), page(1, observation("AG-008", "Service agreement")),
        page(2, observation("AG-009", "Service agreement")))
    assert result["fields"]["contract"]["value"] is None
    assert result["fields"]["contract"]["validity"] == "AMBIGUOUS"
    assert {item["observed"] for item in result["fields"]["contract"]["candidate_alternatives"]} == {"AG-008", "AG-009"}


def test_same_caption_in_the_wrong_section_does_not_meet_declared_context():
    result = mapped(config(), page(1, payment=observation("REF-112", "Reference", "Delivery instructions")))
    assert result["fields"]["payment"]["value"] is None
    assert result["fields"]["payment"]["candidate_alternatives"][0]["association"]["reason"] == "OBSERVED_SECTION_NOT_DECLARED_FOR_FIELD"


def test_declared_page_is_checked_and_an_unproven_narrow_roi_is_not_fabricated():
    outside = mapped(config(pages=(2,)), page(1, observation("AG-008", "Service agreement")))
    assert outside["fields"]["contract"]["value"] is None
    assert outside["fields"]["contract"]["candidate_alternatives"][0]["association"]["reason"] == "PAGE_OUTSIDE_DECLARED_SOURCE_CONTEXT"
    narrow = mapped(config(region=[.1, .1, .6, .5]), page(1, observation("AG-008", "Service agreement")))
    assert narrow["fields"]["contract"]["value"] is None
    assert narrow["fields"]["contract"]["candidate_alternatives"][0]["association"]["reason"] == "DECLARED_REGION_REQUIRES_MEASURED_SOURCE_GEOMETRY"


def test_old_unwitnessed_selection_is_kept_but_cannot_satisfy_new_opt_in():
    source = page(1, {"observed": "AG-008", "ambiguous": False, "alternatives": []})
    result = mapped(config(), source)
    assert result["fields"]["contract"]["value"] is None
    assert result["fields"]["contract"]["candidate_alternatives"][0]["association"]["reason"] == "LITERAL_SOURCE_CONTEXT_MISSING_OR_INVALID"
    assert result["document_representation"]["pages"][0]["selection"]["fields"]["contract"]["observed"] == "AG-008"


def test_legacy_without_opt_in_keeps_original_schema_and_mapping():
    definition = DocumentDefinition.model_validate({"fields": [{"name": "reference", "label": "Reference"}]}).model_dump(mode="json")
    assert "source_association" not in definition["fields"][0]
    source = page(1)
    source["selection"]["fields"] = {"reference": {"observed": "LEGACY-23", "ambiguous": False, "alternatives": []}}
    assert mapped(definition, source)["fields"]["reference"]["value"] == "LEGACY-23"


def test_source_policy_requires_explicit_anchors_and_is_header_only():
    with pytest.raises(ValidationError, match="anclas literales"):
        DocumentDefinition.model_validate({"fields": [{"name": "reference", "source_association": POLICY}]})
    with pytest.raises(ValidationError, match="anclas literales"):
        DocumentDefinition.model_validate({"fields": [{"name": "reference", "source_association": POLICY,
            "visual": rule("///", 1)}]})
    with pytest.raises(ValidationError, match="sólo en cabeceras"):
        DocumentDefinition.model_validate({"fields": [{"name": "reference"}], "tables": [{"name": "items",
            "structured_header_labels": ["Contract"], "fields": [config()["fields"][0]]}]})


@pytest.fixture
def gateway():
    path = Path(__file__).resolve().parents[2] / "deploy/idp/recognition.py"
    spec = importlib.util.spec_from_file_location("source_association_gateway", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_visual_contract_requests_only_configured_literal_witnesses(gateway):
    definition = config()
    schema = gateway.visual_contract(definition)
    assert "source_context" in schema["properties"]["fields"]["properties"]["contract"]["required"]
    valid = page(1, observation("AG-008", "Service agreement"))["selection"]
    gateway.validate_selection(valid, definition)
    assert _selection(valid) == valid
    del valid["fields"]["contract"]["source_context"]
    with pytest.raises(ValueError, match="LLM_SELECTION_INVALID"):
        gateway.validate_selection(valid, definition)
    legacy = DocumentDefinition.model_validate({"fields": [{"name": "reference"}]}).model_dump(mode="json")
    legacy_schema = gateway.visual_contract(legacy)
    assert "source_context" not in legacy_schema["properties"]["fields"]["properties"]["reference"]["properties"]


def test_semantic_contract_keeps_meaning_and_printed_anchor_distinct(gateway):
    definition = config()
    before = copy.deepcopy(definition)
    semantics = gateway.semantic_definition(definition)
    field = semantics["fields"][0]
    assert semantics["semantic_definition_version"] == gateway.ASSOCIATION_SEMANTIC_VERSION
    assert field["label"] == "Service contract identifier"
    assert field["source_context"][0]["anchor_label"] == "Service agreement"
    assert field["source_association"] == POLICY
    assert field["source_context"][0]["declared_search_region"] == [0, 0, 1, 1]
    assert "sample_value" not in str(semantics)
    assert definition == before


def test_a_model_reported_anchor_does_not_corroborate_its_own_critical_value():
    from test_document_lossless_098 import attest, events, replay

    from trackvance.document_quality import quality_report, validate_candidate_contract

    definition = config(critical=True)
    selected = page(1, observation("AG-008", "Service agreement"),
        observation("PAY-713", "Reference", "Payment information"))["selection"]
    items = events(selected)
    data = replay(items, definition)
    validate_candidate_contract(data, definition)
    extraction = attest(data, definition, items)
    assert data["payload"]["fields"]["contract"]["validity"] == "VALID"
    assert quality_report(extraction, definition)["status"] == "REVIEW_REQUIRED"
    assert "headers.0.contract" not in extraction.engine["validation"]["verified_cells"]
