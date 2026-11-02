"""A new attestation retains unresolved source evidence without rewriting history."""
import copy

from test_document_lossless_098 import attest, definition, events, replay, selection
from test_document_null_uncertainty_098 import multi_page

from trackvance.document_quality import quality_report


def currency_sources():
    primary = multi_page(selection("EUR"), selection("EUR"))
    first = events(raw={"parsing_res_list": [
        {"block_label": "text", "block_content": "Reference: EUR"},
    ]}, count=2, emitted=2, stage="PADDLE_STRUCTURED")
    last = events(raw={"parsing_res_list": [
        {"block_label": "text", "block_content": "Reference: EUR - euros"},
    ]}, count=2, emitted=2, stage="PADDLE_STRUCTURED")
    return [*primary, first[0], last[1], first[-1]]


def legacy_currency_contract():
    return definition(critical=True, normalization={
        "currency_equivalences": {"EUR": ["euro"]},
    })


def test_uncertain_secondary_currency_cannot_be_discarded_behind_clear_primary():
    schema = legacy_currency_contract()
    items = currency_sources()
    frozen_schema, frozen_items = copy.deepcopy(schema), copy.deepcopy(items)
    replayed = copy.deepcopy(items)
    data = replay(replayed, schema)

    primary, secondary = data["payload"]["candidate_history"]
    assert primary["fields"]["reference"]["validity"] == "VALID"
    assert primary["fields"]["reference"]["value"] == "EUR"
    assert secondary["fields"]["reference"]["validity"] == "AMBIGUOUS"
    assert {item["observed"] for item in secondary["fields"]["reference"]["candidate_alternatives"]} == {
        "EUR", "EUR - euros",
    }
    cell = data["payload"]["fields"]["reference"]
    assert cell["value"] is None and cell["validity"] == "AMBIGUOUS"
    assert cell["candidate_selection"]["reason"] == "UNRESOLVED_SOURCE_CANDIDATE"
    assert quality_report(attest(data, schema, replayed), schema)["status"] == "REVIEW_REQUIRED"
    assert schema == frozen_schema and items == frozen_items


def test_explicit_new_alias_contract_resolves_the_value_and_preserves_original_literals():
    old = legacy_currency_contract()
    old_before = copy.deepcopy(old)
    revised = copy.deepcopy(old)
    revised["fields"][0]["normalization"]["currency_equivalences"]["EUR"].append("EUR - euros")
    items = currency_sources()
    source_before = copy.deepcopy(items)
    data = replay(copy.deepcopy(items), revised)
    cell = data["payload"]["fields"]["reference"]

    assert cell["observed"] == cell["value"] == "EUR" and cell["validity"] == "VALID"
    assert cell["candidate_selection"]["reason"] == "DECLARED_NORMALIZATION_EQUIVALENCE"
    secondary = data["payload"]["candidate_history"][1]["fields"]["reference"]
    assert secondary["value"] == "EUR" and secondary["validity"] == "VALID"
    assert {item["observed"] for item in secondary["candidate_alternatives"]} == {"EUR", "EUR - euros"}
    assert old == old_before and items == source_before
