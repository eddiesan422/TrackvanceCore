"""A new declared currency alias preserves source and independent evidence gates."""
import copy

import pytest
from pydantic import ValidationError
from test_document_lossless_098 import attest, definition, events, replay, selection

from trackvance.document_normalization import (
    DocumentNormalization,
    canonical_hash,
    normalize_observation,
)
from trackvance.document_quality import quality_report, validate_candidate_contract


def currency_policy():
    return {"currency_equivalences": {"COP": [
        "Peso colombiano", "COP - Peso colombiano", "COP - pesos colombianos",
    ]}}


@pytest.mark.parametrize("observed", [
    "COP", "Peso colombiano", "COP - Peso colombiano", "COP - pesos colombianos",
    "  cOp -   pEsOs Colombianos  ",
])
def test_only_declared_currency_variants_resolve_and_keep_source(observed):
    field = definition(normalization=currency_policy())["fields"][0]
    value, status, trace = normalize_observation(observed, field, evidence_refs=["source-page-block"])
    assert (value, status) == ("COP", "VALID")
    assert trace["observed"] == observed
    assert trace["normalized"] == "COP"
    assert trace["evidence_refs"] == ["source-page-block"]
    assert trace["applied_rules"] == ["DECLARED_CURRENCY_EQUIVALENCE"]
    assert trace["configuration_sha256"] == canonical_hash(field["normalization"])


@pytest.mark.parametrize("observed", ["moneda nacional", "pesos colombianos", "$", "USD"])
def test_undeclared_currency_representation_stays_pending(observed):
    field = definition(normalization=currency_policy())["fields"][0]
    value, status, trace = normalize_observation(observed, field, evidence_refs=["source"])
    assert value is None and status == "AMBIGUOUS"
    assert trace["observed"] == observed and trace["evidence_refs"] == ["source"]


@pytest.mark.parametrize("equivalences", [
    {"COP": ["pesos"], "MXN": ["  PESOS  "]},
    {"COP": ["USD"], "USD": ["US dollars"]},
])
def test_one_declared_representation_cannot_identify_two_currencies(equivalences):
    with pytest.raises(ValidationError, match="dos monedas"):
        DocumentNormalization.model_validate({"currency_equivalences": equivalences})


@pytest.mark.parametrize("observed", ["USD", "US dollars"])
def test_another_declared_currency_is_not_converted_to_a_different_code(observed):
    policy = currency_policy()
    policy["currency_equivalences"]["USD"] = ["US dollars"]
    field = definition(normalization=policy)["fields"][0]
    value, status, trace = normalize_observation(observed, field)
    assert (value, status) == ("USD", "VALID")
    assert trace["observed"] == observed and trace["normalized"] == "USD"


def test_new_alias_revision_resolves_page_alternatives_without_auto_authority():
    old = definition(critical=True, normalization={"currency_equivalences": {
        "COP": ["Peso colombiano", "COP - Peso colombiano"],
    }})
    new = copy.deepcopy(old)
    new["fields"][0]["normalization"]["currency_equivalences"]["COP"].append("COP - pesos colombianos")
    first = events(selection("COP"), count=2, emitted=2)
    second = events(selection("COP - pesos colombianos"), count=2, emitted=2)
    items = [first[0], second[1], first[-1]]
    source_before = copy.deepcopy(items)
    old_before = copy.deepcopy(old)

    unresolved = replay(copy.deepcopy(items), old)
    new_items = copy.deepcopy(items)
    resolved = replay(new_items, new)
    before = unresolved["payload"]["fields"]["reference"]
    after = resolved["payload"]["fields"]["reference"]
    assert before["validity"] == "AMBIGUOUS" and before["value"] is None
    assert after["validity"] == "VALID" and after["value"] == "COP"
    assert after["observed"] == before["observed"] == "COP"
    assert after["evidence_refs"] == before["evidence_refs"]
    assert after["candidate_alternatives"] == before["candidate_alternatives"]
    assert {entry["observed"] for entry in after["candidate_alternatives"]} == {
        "COP", "COP - pesos colombianos",
    }
    assert old == old_before and items == source_before
    validate_candidate_contract(resolved, new)
    extraction = attest(resolved, new, new_items)
    # Equivalent observations of one mechanism are not independent evidence.
    assert quality_report(extraction, new)["status"] == "REVIEW_REQUIRED"
    assert extraction.engine["validation"]["verified_cells"] == {}
