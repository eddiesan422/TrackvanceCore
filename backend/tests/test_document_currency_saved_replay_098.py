"""Currency resolution over saved observations preserves source and conflict gates."""
import copy

import pytest

from trackvance.document_lossless import _resolve, map_representation
from trackvance.document_representation import VERSION
from trackvance.document_schemas import DocumentDefinition


def currency_field():
    return {"name": "currency", "logical_type": "STRING", "required": True,
        "normalization": {"currency_equivalences": {
            "EUR": ["euro", "euros"], "CHF": ["franc suisse"],
        }}}


def observation(value, page, ambiguous=False, alternatives=None):
    return (value, page, f"evidence-{page}", ambiguous, alternatives or [])


def test_declared_equivalent_page_observations_keep_every_source_and_alternative():
    original = [observation("EUR", 1), observation("euros", 3)]
    saved = copy.deepcopy(original)
    result = _resolve(original, currency_field())
    assert (result["value"], result["validity"]) == ("EUR", "VALID")
    assert result["observed"] == "EUR"
    assert result["evidence_refs"] == ["evidence-1", "evidence-3"]
    assert [x["observed"] for x in result["candidate_alternatives"]] == ["EUR", "euros"]
    assert result["normalization"]["observed"] == "EUR"
    assert original == saved


def test_known_distinct_currencies_remain_conflicting():
    result = _resolve([observation("EUR", 1), observation("franc suisse", 3)], currency_field())
    assert result["value"] is None and result["validity"] == "AMBIGUOUS"
    assert {x["observed"] for x in result["candidate_alternatives"]} == {"EUR", "franc suisse"}
    assert result["evidence_refs"] == ["evidence-1", "evidence-3"]


@pytest.mark.parametrize("second", ["unlisted denomination", "$", "USD"])
def test_unknown_or_context_free_representation_is_not_assumed_equivalent(second):
    result = _resolve([observation("EUR", 1), observation(second, 3)], currency_field())
    assert result["value"] is None and result["validity"] == "AMBIGUOUS"
    assert result["candidate_alternatives"][1]["observed"] == second


def test_source_ambiguity_flag_is_not_cleared_by_equivalent_normalization():
    result = _resolve([observation("EUR", 1), observation("euros", 3, True, ["CHF"])], currency_field())
    assert result["value"] is None and result["validity"] == "AMBIGUOUS"
    assert result["candidate_alternatives"][1]["ambiguous"] is True
    assert result["candidate_alternatives"][1]["alternatives"] == ["CHF"]


def test_opted_in_currency_rejects_wrong_section_and_retains_it_for_review():
    def rule(number):
        return {"page": number, "label": "Currency", "section_label": "Invoice",
            "region": [0, 0, 1, 1], "direction": "RIGHT"}

    definition = DocumentDefinition.model_validate({"fields": [
        {**currency_field(), "label": "Currency", "source_association": "DECLARED_LABEL_CONTEXT_V1",
            "visual": rule(1), "visual_alternatives": [rule(2)]},
    ]}).model_dump(mode="json")

    def page(number, value, section):
        return {"id": f"page-{number}", "stage": "QWEN_VISUAL", "page_one_based": number,
            "geometry": {"width": 1200, "height": 1600, "unit": "PIXEL"}, "blocks": [], "tables": [],
            "selection": {"fields": {"currency": {"observed": value, "ambiguous": False,
                "alternatives": [], "source_context": {"anchor_label": "Currency", "section_label": section}}},
                "tables": {}}}

    pages = [page(1, "EUR", "Invoice"), page(2, "CHF", "Other section")]
    before = copy.deepcopy(pages)
    result = map_representation({"version": VERSION, "pages": pages}, definition)
    cell = result["fields"]["currency"]
    assert (cell["value"], cell["validity"]) == ("EUR", "VALID")
    rejected = next(x for x in cell["candidate_alternatives"] if x.get("eligible_for_field") is False)
    assert rejected["observed"] == "CHF"
    assert rejected["association"]["reason"] == "OBSERVED_SECTION_NOT_DECLARED_FOR_FIELD"
    assert result["document_representation"]["pages"] == before
    assert pages == before
