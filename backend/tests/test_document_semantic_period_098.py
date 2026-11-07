"""Declared period projection and native dataset metadata, without inference.

The API fixture supplies its own measured synthetic source and image-only
recognizer double. It proves transaction plumbing, not document accuracy.
"""
from copy import deepcopy

import pytest
from pydantic import ValidationError
from test_document_semantic_general_098 import profile
from test_document_semantic_verification_replay_v2_098 import activate_v2, v1_original

from trackvance.document_engine import extract
from trackvance.document_normalization import canonical_hash
from trackvance.document_schemas import DocumentDefinition
from trackvance.document_semantic_general import SemanticProfile, compile_profile


@pytest.mark.parametrize("field,expected", [("reference", "R-001"), ("amount", "12.50")])
def test_declared_period_reaches_auto_publication_without_retyping_or_authorized_review(authenticated, monkeypatch, field, expected):
    requested = profile()
    requested["business_period_field"] = field

    def activation(client, patch):
        return activate_v2(client, patch, acquisition_profile=requested)

    _family, template, original, detail, calls = v1_original(authenticated, monkeypatch,
        "semantic-period-" + field, activation=activation)
    assert calls == ["QWEN_VISUAL"]  # Recognizer double only, no real model.
    assert template["definition"]["business_period_field"] == field
    contract = template["definition"]["semantic_general"]
    assert contract["contract"]["business_period_field"] == field
    assert contract["profile_sha256"] == canonical_hash(contract["contract"])
    payload = detail["extraction"]["payload"]
    assert payload["business_period"] == {"field": field, **payload["fields"][field]}
    assert payload["business_period"]["value"] == expected
    assert not detail["extraction"]["reviewed"] and detail["quality"]["status"] == "AUTO_VERIFIED"
    version = detail["publications"][0]["versions"]["headers"]
    response = authenticated.get(f"/api/v1/dataset-versions/{version}/profile")
    assert response.status_code == 200, response.text
    dataset = response.json()
    assert dataset["row_count"] == 1 and dataset["sample"][0][field] == expected
    periods = dataset["ingestion_metadata"]["document_publication"]["business_periods"]
    assert periods == {original["id"]: payload["business_period"]}


@pytest.mark.parametrize("target", ["unknown_header", "description", ""])
def test_api_rejects_missing_or_table_only_period_binding(authenticated, monkeypatch, target):
    monkeypatch.setenv("TRACKVANCE_SEMANTIC_GENERAL_ENABLED", "true")
    response = authenticated.post("/api/v1/documents/families", json={"name": "Invalid period binding fixture"})
    assert response.status_code == 200, response.text
    requested = profile(table=True)
    requested["business_period_field"] = target
    response = authenticated.post(f"/api/v1/documents/families/{response.json()['id']}/semantic-profile", json={
        "profile": requested, "reason": "A period must reference a declared header, never a table column."})
    assert response.status_code == 422


@pytest.mark.parametrize("mutation", ["native", "contract", "omitted_contract"])
def test_period_cannot_escape_the_exact_profile_projection_even_with_a_recomputed_profile_hash(mutation):
    requested = profile()
    requested["business_period_field"] = "reference"
    definition = compile_profile(requested)
    metadata = definition["semantic_general"]
    if mutation == "native":
        definition["business_period_field"] = "amount"
    else:
        if mutation == "contract":
            metadata["contract"]["business_period_field"] = "amount"
        else:
            metadata["contract"].pop("business_period_field")
        metadata["profile_sha256"] = canonical_hash(metadata["contract"])
    with pytest.raises(ValidationError):
        DocumentDefinition.model_validate(definition)


def test_missing_period_option_serializes_as_the_existing_contract_and_does_not_change_native_field_semantics():
    old = profile()
    explicit_none = {**deepcopy(old), "business_period_field": None}
    assert SemanticProfile.model_validate(explicit_none).model_dump(mode="json") == old
    assert compile_profile(old) == compile_profile(explicit_none)
    requested = deepcopy(old)
    requested["fields"].append({"name": "issue_date", "label": "Issue date", "logical_type": "DATE", "date_format": "%d/%m/%Y"})
    requested["business_period_field"] = "issue_date"
    definition = compile_profile(requested)
    lines = [{"page": 1, "text": "Issue date: 07/11/2026", "region": [.1, .1, .8, .2], "method": "DIGITAL", "confidence": 100}]
    payload = extract(lines, definition)
    assert payload["business_period"] == {"field": "issue_date", **payload["fields"]["issue_date"]}
    assert payload["business_period"]["observed"] == "07/11/2026"
    assert payload["business_period"]["value"] == "2026-11-07"
    assert next(field for field in definition["fields"] if field["name"] == "issue_date")["date_format"] == "%d/%m/%Y"
