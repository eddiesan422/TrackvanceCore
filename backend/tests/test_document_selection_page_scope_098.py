"""Page scope declared in existing visual rules bounds Qwen header candidates."""
import copy

import pytest
from test_document_lossless_098 import attest, definition, replay, selection
from test_document_null_uncertainty_098 import context, multi_page, scoped_config, uncertain

from trackvance.document_quality import quality_report


def page_definition(page=1):
    schema = definition()
    schema["fields"][0]["visual_alternatives"] = [{"kind": "LABEL_VALUE", "page": page,
        "label": "Reference", "direction": "RIGHT", "region": [0, 0, 1, 1]}]
    return schema


@pytest.mark.parametrize("outside", [selection("OTHER"), uncertain(), uncertain("OTHER"), selection(None)])
def test_declared_visual_page_without_association_opt_in_excludes_only_outside_observations(outside):
    schema = page_definition()
    items = multi_page(selection("R-001"), outside)
    frozen = copy.deepcopy(items)
    replayed = copy.deepcopy(items)
    data = replay(replayed, schema)
    cell = data["payload"]["fields"]["reference"]
    assert (cell["observed"], cell["value"], cell["validity"]) == ("R-001", "R-001", "VALID")
    assert cell["origin"]["page"] == 1
    assert items == frozen
    raw = data["payload"]["document_representation"]["pages"][1]["selection"]["fields"]["reference"]
    assert raw == outside["fields"]["reference"]
    if outside["fields"]["reference"]["observed"] is not None or outside["fields"]["reference"]["ambiguous"]:
        rejected = next(value for value in cell["candidate_alternatives"] if value.get("eligible_for_field") is False)
        assert rejected["observed"] == raw["observed"] and rejected["ambiguous"] == raw["ambiguous"]
        assert rejected["alternatives"] == raw["alternatives"]
        assert rejected["association"]["reason"] == "PAGE_OUTSIDE_DECLARED_SOURCE_CONTEXT"
        assert rejected["association"]["declared_pages"] == [1]
        assert rejected["association"]["policy"] == "DECLARED_VISUAL_PAGE_SCOPE_098_V3"
    assert quality_report(attest(data, schema, replayed), schema)["status"] == "AUTO_VERIFIED"


def test_legitimate_third_page_value_is_retained_over_nonnull_and_uncertain_other_pages():
    schema = page_definition(3)
    items = multi_page(selection("OUTSIDE-P1"), uncertain(), selection("P3-REFERENCE"))
    data = replay(items, schema)
    cell = data["payload"]["fields"]["reference"]
    assert (cell["value"], cell["validity"], cell["origin"]["page"]) == ("P3-REFERENCE", "VALID", 3)
    assert {value["page"] for value in cell["candidate_alternatives"] if value.get("eligible_for_field") is False} == {1, 2}


def test_a_relevant_null_on_an_alternative_declared_page_keeps_review_pending():
    schema = page_definition(1)
    schema["fields"][0]["visual"] = {"kind": "LABEL_VALUE", "page": 2,
        "label": "Reference", "direction": "RIGHT", "region": [0, 0, 1, 1]}
    items = multi_page(selection("R-001"), uncertain())
    data = replay(items, schema)
    assert data["payload"]["fields"]["reference"]["validity"] == "AMBIGUOUS"
    assert quality_report(attest(data, schema, items), schema)["status"] == "REVIEW_REQUIRED"


def test_no_declared_page_scope_cannot_turn_a_null_signal_into_an_absence():
    schema = definition()
    items = multi_page(selection("R-001"), uncertain())
    data = replay(items, schema)
    assert data["payload"]["fields"]["reference"]["validity"] == "AMBIGUOUS"
    assert not data["payload"].get("header_association_rejections")


def test_optional_normal_absence_in_its_declared_page_remains_valid_without_inventing_a_value():
    schema = page_definition(1)
    schema["fields"][0]["required"] = False
    items = multi_page(selection(None), uncertain())
    data = replay(items, schema)
    cell = data["payload"]["fields"]["reference"]
    assert cell["value"] is None and cell["validity"] == "VALID"
    assert quality_report(attest(data, schema, items), schema)["status"] == "AUTO_VERIFIED"


def test_section_association_rules_still_distinguish_wrong_section_and_unknown_context():
    schema = scoped_config()
    outside_section = multi_page(context(selection("R-001")), context(uncertain(), "Unrelated section"))
    assert replay(outside_section, schema)["payload"]["fields"]["reference"]["validity"] == "VALID"
    unknown_section = multi_page(context(selection("R-001")), uncertain())
    assert replay(unknown_section, schema)["payload"]["fields"]["reference"]["validity"] == "AMBIGUOUS"
