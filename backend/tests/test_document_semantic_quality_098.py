"""Declared format consensus and semantic guards, with no model inference."""
import copy
from types import SimpleNamespace

import pytest
from pydantic import ValidationError
from sqlalchemy import func, select
from test_document_semantic_general_098 import cell, profile, replay, selection
from test_document_semantic_general_lifecycle_098 import (
    all_paths,
    install_reader_double,
    setup_profile,
    upload_fixture,
)

from trackvance.document_models import DocumentExtraction, DocumentTemplate
from trackvance.document_normalization import canonical_hash, normalize_observation
from trackvance.document_quality import quality_report
from trackvance.document_schemas import DocumentDefinition
from trackvance.document_semantic_general import (
    ActivateSemanticProfile,
    catalog,
    compile_profile,
    profile_versions,
    profiles,
    resolve_profile,
)
from trackvance.document_worker import process_once
from trackvance.models import DatasetVersion


def scalar(kind, **policy):
    return DocumentDefinition.model_validate({"fields": [{"name": "scalar", "logical_type": kind,
        "normalization": policy}]}).model_dump(mode="json")["fields"][0]


@pytest.mark.parametrize("observed,expected", [
    ("1.234.567,89", "1234567.89"), ("1,234,567.89", "1234567.89"),
    ("-2 345,60", "-2345.60"), ("+2\u202f345.60", "2345.60"), ("2\u00a0345,60", "2345.60"),
    ("112.000,00", "112000.00"), ("3400", "3400"), ("12,50", "12.50"), ("12.50", "12.50"),
])
def test_declared_numeric_grammars_resolve_only_one_distinct_meaning(observed, expected):
    field = scalar("DECIMAL", decimal_formats="UNAMBIGUOUS_LOCALE_V1")
    value, status, trace = normalize_observation(observed, field)
    assert (value, status) == (expected, "VALID")
    assert trace["observed"] == observed and trace["normalized"] == value
    assert trace["format_candidates"] and trace["rule"] == "DECLARED_DECIMAL_FORMAT_CONSENSUS_V1"
    assert trace["configuration_sha256"] == canonical_hash(field["normalization"])


@pytest.mark.parametrize("observed", ["1,234", "1.234", "-12.345", "+12,345"])
def test_equally_legal_decimal_and_grouping_readings_remain_ambiguous(observed):
    value, status, trace = normalize_observation(observed, scalar("DECIMAL", decimal_formats="UNAMBIGUOUS_LOCALE_V1"))
    assert value == observed and status == "AMBIGUOUS"
    assert len({item["value"] for item in trace["format_candidates"]}) > 1


@pytest.mark.parametrize("observed", ["12,34.56", "1.23.456,00", "1e3", "1\n234,50", "NaN", "(100,00)", "999999999999999999999999999999999999999"])
def test_declared_numeric_formats_do_not_repair_malformed_or_unbounded_text(observed):
    value, status, _ = normalize_observation(observed, scalar("DECIMAL", decimal_formats="UNAMBIGUOUS_LOCALE_V1"))
    assert value == observed and status == "INVALID"


@pytest.mark.parametrize("observed,expected,status", [
    ("480,00 COP", "480.00", "VALID"), ("COP 1.234,56", "1234.56", "VALID"),
    ("- COP 1.234,56", "-1234.56", "VALID"), ("COP -1.234,56", "-1234.56", "VALID"),
    ("US$ 1,234.56", "1234.56", "VALID"), ("EUR 1,234.56", "EUR 1,234.56", "INVALID"),
    ("COP 123 USD", "COP 123 USD", "INVALID"), ("COP COP 123", "COP COP 123", "INVALID"),
    ("COP 1,234", "COP 1,234", "AMBIGUOUS"),
])
def test_declared_single_currency_affix_is_literal_format_not_currency_inference(observed, expected, status):
    field = scalar("DECIMAL", decimal_formats="UNAMBIGUOUS_LOCALE_V1")
    field["currency_symbols"] = ["COP", "USD", "US$"]
    value, validity, trace = normalize_observation(observed, field)
    assert (value, validity) == (expected, status)
    assert trace["observed"] == observed
    if validity == "VALID":
        assert all(item["format"]["currency_affix"] for item in trace["format_candidates"])


@pytest.mark.parametrize("observed,expected,status", [
    ("22/08/2026", "2026-08-22", "VALID"), ("08/22/2026", "2026-08-22", "VALID"),
    ("2026-08-22", "2026-08-22", "VALID"), ("01/02/2026", "01/02/2026", "AMBIGUOUS"),
    ("02/02/2026", "2026-02-02", "VALID"), ("31/02/2026", "31/02/2026", "INVALID"),
])
def test_declared_date_formats_require_consensus_and_valid_calendar(observed, expected, status):
    field = scalar("DATE", date_formats=["%Y-%m-%d", "%d/%m/%Y", "%m/%d/%Y"])
    value, actual, trace = normalize_observation(observed, field)
    assert (value, actual) == (expected, status)
    assert trace["observed"] == observed
    reverse = copy.deepcopy(field)
    reverse["normalization"]["date_formats"].reverse()
    assert normalize_observation(observed, reverse)[:2] == (expected, status)


def test_no_implicit_locale_or_identifier_conversion_and_legacy_trace_unchanged():
    field = {"logical_type": "DECIMAL", "decimal_separator": ".", "required": True}
    assert normalize_observation("1.234,00", field)[:2] == ("1.234,00", "INVALID")
    _, _, trace = normalize_observation("12.50", field)
    assert "format_candidates" not in trace and trace["rule"] == "IDENTITY"
    code = {"logical_type": "STRING", "business_key": True, "required": True}
    assert normalize_observation(r"001.234\nA-9", code)[:2] == (r"001.234\nA-9", "VALID")


@pytest.mark.parametrize("kind,policy", [
    ("STRING", {"decimal_formats": "UNAMBIGUOUS_LOCALE_V1"}),
    ("DECIMAL", {"date_formats": ["%Y-%m-%d"]}),
    ("DATE", {"date_formats": ["%Y-%m-%d", "%Y-%m-%d"]}),
    ("DATE", {"date_formats": ["%y-%m-%d"]}),
])
def test_scalar_format_policy_cannot_be_attached_to_wrong_type_or_unsafe_grammar(kind, policy):
    with pytest.raises(ValidationError):
        scalar(kind, **policy)


def test_catalog_keeps_r1_hashes_and_selects_each_explicit_revision():
    expected = {"invoice-v1": "346660fc427e7286bcaceace48cf512483175a9e3e7bef40adca140ec1a11b35",
        "statement-v1": "6a8bedcdb1e23eeeb6ff2c96f7496793ff96043070c67cfe0d836e81a72dde85",
        "narrative-v1": "cac641a231de8d85af2ba5eb2d5b9074e84cb46a3cd2a0b4f5eb62ccb5d1fe21",
        "certificate-v1": "a90259c2abb61ae00ff8c6affbeaa81e4b664103f36a3176d3f07e9ef6b2372b"}
    assert {item["id"]: canonical_hash(item) for item in profiles()} == expected
    assert len(catalog()["profiles"]) == 9
    for item in profile_versions():
        body = ActivateSemanticProfile(profile_id=item["id"], profile_revision=item["revision"], reason="Explicit frozen contract")
        assert resolve_profile(body) == item
        assert compile_profile(item)["publication"]["review_policy"] == "ALL"
    assert resolve_profile(ActivateSemanticProfile(profile_id="invoice-v1", reason="Latest contract"))["revision"] == 3


def test_null_optional_narrative_date_is_not_a_negated_commitment_but_uncertainty_stays():
    definition = compile_profile(profile(table=True, collection=True))
    row = {"description": cell("Prepare a report", polarity="AFFIRMATIVE"),
        "amount": cell(None, basis="PRINTED", polarity="NEGATED", quote="No amount was committed")}
    result = replay([selection(rows=[row])], definition)["payload"]
    assert result["tables"]["items"][0]["amount"]["validity"] == "VALID"
    assert result["tables"]["items"][0]["amount"]["value"] is None
    assert not result["semantic_evidence"]["guards"]
    row["amount"] = cell(None, ambiguous=True, basis="UNCERTAIN", polarity="NEGATED")
    pending = replay([selection(rows=[row])], definition)["payload"]
    assert pending["tables"]["items"][0]["amount"]["validity"] == "AMBIGUOUS"
    row["amount"] = cell("12.50", basis="PRINTED", polarity="NEGATED")
    assert replay([selection(rows=[row])], definition)["payload"]["tables"]["items"][0]["amount"]["validity"] == "AMBIGUOUS"


@pytest.mark.parametrize("observed,quote,polarity,expected", [
    ("[not provided]", "Reference: [not provided]", "NEGATED", "MISSING"),
    ("[sin informar]", "Referencia: [sin informar]", "NEGATED", "MISSING"),
    ("The [not provided] label is printed", "The [not provided] label is printed", "NEGATED", "VALID"),
    ("[not provided] Ltd", "Reference: [not provided] Ltd", "NEGATED", "VALID"),
    ("[not provided]", "[not provided]", "AFFIRMATIVE", "AMBIGUOUS"),
])
def test_absence_requires_standalone_marker_and_matching_explicit_context(observed, quote, polarity, expected):
    definition = compile_profile(profile())
    raw = selection()
    raw["fields"]["reference"] = cell(observed, basis="PRINTED", polarity=polarity, quote=quote)
    payload = replay([raw], definition)["payload"]
    value = payload["fields"]["reference"]
    assert value["observed"] == observed and value["validity"] == expected
    if expected == "MISSING":
        assert value["value"] is None and value["semantic_absence"]["documentary_absence_verified"] is False
    elif expected == "AMBIGUOUS":
        assert value["value"] is None and value["semantic_absence"]["reason"] == "POSSIBLE_PLACEHOLDER_V1"
        assert value["semantic_absence"]["documentary_absence_verified"] is False
    else:
        assert value["value"] == observed and "semantic_absence" not in value


@pytest.mark.parametrize("polarity", ["AFFIRMATIVE", "UNKNOWN", "NOT_APPLICABLE", "NEGATED", "CONDITIONAL"])
@pytest.mark.parametrize("context_kind", ["BARE", "LABEL"])
def test_short_marker_context_never_uses_model_polarity_as_acceptance_authority(polarity, context_kind):
    definition = compile_profile(profile())
    raw = selection()
    raw["fields"]["reference"] = cell("[not provided]", quote="[not provided]" if context_kind == "BARE" else "Reference: [not provided]", polarity=polarity,
        basis="PRINTED", alternatives=["[not provided]"])
    result = replay([raw], definition)["payload"]
    value = result["fields"]["reference"]
    assert value["observed"] == "[not provided]" and value["validity"] == "AMBIGUOUS" and value["value"] is None
    assert value["semantic_absence"]["documentary_absence_verified"] is False
    observed = next(item for item in result["semantic_evidence"]["observations"] if item["field"] == "reference")
    assert observed["alternatives"] == ["[not provided]"] and observed["model_context"]["polarity"] == polarity
    assert result["semantic_evidence"]["model_labels_are_independent_evidence"] is False


@pytest.mark.parametrize("observed,quote", [
    ("[not provided] Limited", "Registered name: [not provided] Limited"),
    ("ID-[not provided]-0003", "Identifier: ID-[not provided]-0003"),
    ("The text '[not provided]' is a technical label", "The text '[not provided]' is a technical label"),
    ("[not provided]", "The registered company is named '[not provided]' and remains active."),
])
def test_legitimate_embedded_identifiers_and_explanatory_text_are_not_replaced(observed, quote):
    raw = selection()
    raw["fields"]["reference"] = cell(observed, quote=quote, basis="PRINTED", polarity="AFFIRMATIVE")
    value = replay([raw], compile_profile(profile()))["payload"]["fields"]["reference"]
    assert value["observed"] == value["value"] == observed and value["validity"] == "VALID"
    assert "semantic_absence" not in value


def test_authorized_review_can_resolve_a_legitimate_company_literal_without_erasing_source(authenticated, database, monkeypatch):
    family, _ = setup_profile(authenticated, monkeypatch, table=False)
    raw = selection()
    raw["fields"]["reference"] = cell("[not provided]", quote="[not provided]", basis="PRINTED", polarity="AFFIRMATIVE")
    install_reader_double(monkeypatch, [raw])
    original = upload_fixture(authenticated, family, label="legitimate-company-marker-fixture")
    assert process_once("legitimate-company-marker-fixture")
    first = authenticated.get("/api/v1/documents/originals/" + original["id"]).json()["extraction"]
    assert first["payload"]["fields"]["reference"]["validity"] == "AMBIGUOUS"
    response = authenticated.post(f"/api/v1/documents/originals/{original['id']}/review", json={
        "expected_extraction_id": first["id"], "changes": {"headers.0.reference": "[not provided]"},
        "confirmed": all_paths(first),
        "reason": "Authorized synthetic fixture review: the extended registered-company source context identifies '[not provided]' as the literal legal name, not an absent subject. Not AUTO."})
    assert response.status_code == 200, response.text
    current = response.json()
    assert current["extraction"]["previous_id"] == first["id"] and current["extraction"]["reviewed"]
    value = current["extraction"]["payload"]["fields"]["reference"]
    assert value["observed"] == value["value"] == "[not provided]" and value["validity"] == "VALID"
    assert value["reviewed"] and current["semantic_review"]["automatic_approval"] is False
    with database() as db:
        retained = db.get(DocumentExtraction, first["id"])
        assert retained.payload == first["payload"] and retained.engine == first["engine"]


def test_placeholder_does_not_hide_a_contradictory_present_value_on_another_page():
    definition = compile_profile(profile())
    absent = selection()
    absent["fields"]["reference"] = cell("[not provided]", basis="PRINTED", polarity="NEGATED", quote="Reference: [not provided]")
    payload = replay([absent, selection(reference="R-077")], definition)["payload"]
    value = payload["fields"]["reference"]
    assert value["observed"] == "[not provided]" and value["validity"] == "AMBIGUOUS"
    assert len(value["candidate_alternatives"]) == 2


def test_literal_context_uses_only_declared_documentary_linebreak_equivalence():
    value = profile()
    value["fields"][0]["business_key"] = False
    value["fields"][0]["normalization"] = {"text_linebreaks": "DECLARED_MULTILINE_V1"}
    raw = selection()
    raw["fields"]["reference"] = cell(r"First\nsecond", quote="Label: First\nsecond")
    payload = replay([raw], compile_profile(value))["payload"]
    assert payload["fields"]["reference"]["value"] == "First second"
    assert payload["fields"]["reference"]["observed"] == r"First\nsecond"
    assert not payload["semantic_evidence"]["guards"]
    value["fields"][0].pop("normalization")
    assert replay([raw], compile_profile(value))["payload"]["fields"]["reference"]["validity"] == "AMBIGUOUS"
    raw["fields"]["reference"] = cell("400,00", quote="100,00 + 300,00")
    assert replay([raw], compile_profile(value))["payload"]["fields"]["reference"]["validity"] == "AMBIGUOUS"


def test_api_catalog_r1_and_r2_activate_without_mutating_previous_contract(authenticated, database, monkeypatch):
    monkeypatch.setenv("TRACKVANCE_SEMANTIC_GENERAL_ENABLED", "true")
    family = authenticated.post("/api/v1/documents/families", json={"name": "Versioned format catalog"}).json()["id"]
    catalog_response = authenticated.get("/api/v1/documents/semantic-profiles")
    assert catalog_response.status_code == 200
    assert len(catalog_response.json()["profiles"]) == 9
    saved = []
    for revision in [1, 2, 1]:
        response = authenticated.post(f"/api/v1/documents/families/{family}/semantic-profile", json={
            "profile_id": "invoice-v1", "profile_revision": revision, "reason": "Explicit catalog revision selection"})
        assert response.status_code == 200, response.text
        result = response.json()
        assert result["profile"]["revision"] == revision
        assert result["template"]["definition"]["semantic_general"]["profile_revision"] == revision
        saved.append(result["template"])
    with database() as db:
        for template in saved:
            record = db.get(DocumentTemplate, template["id"])
            assert record.definition == template["definition"] and record.definition_hash == template["sha256"]
    unknown = authenticated.post(f"/api/v1/documents/families/{family}/semantic-profile", json={
        "profile_id": "invoice-v1", "profile_revision": 99, "reason": "Unknown revision cannot alias latest"})
    assert unknown.status_code == 422


def test_api_contextual_required_placeholder_cannot_be_confirmed_or_published(authenticated, database, monkeypatch):
    family, _ = setup_profile(authenticated, monkeypatch, table=False)
    raw = selection()
    raw["fields"]["reference"] = cell("[not provided]", quote="Reference: [not provided]", polarity="NEGATED", basis="PRINTED")
    install_reader_double(monkeypatch, [raw])
    original = upload_fixture(authenticated, family, label="explicit-empty-form")
    assert process_once("contextual-empty-form-fixture")
    detail = authenticated.get("/api/v1/documents/originals/" + original["id"]).json()
    assert detail["status"] == "REVIEW_REQUIRED"
    field = detail["extraction"]["payload"]["fields"]["reference"]
    assert field["observed"] == "[not provided]" and field["validity"] == "MISSING"
    response = authenticated.post(f"/api/v1/documents/originals/{original['id']}/review", json={
        "expected_extraction_id": detail["extraction"]["id"], "confirmed": all_paths(detail["extraction"]),
        "reason": "Confirmation alone must not fabricate a missing required value"})
    assert response.status_code == 422
    with database() as db:
        assert db.scalar(select(func.count()).select_from(DatasetVersion)) == 0


def test_item_columns_are_a_new_contract_without_redefining_any_r2_profile():
    expected = {"invoice-v1": "14036630b1c4fac3b763a72dbda1fd6fc7313943b6b40a1a3fad28b71146acd5",
        "statement-v1": "af08eee2da19976b08cea7477e5d2e0255353bfdc540f082b7aca1ea75f3f490",
        "narrative-v1": "d30fd5508c40602009e30f4c2a4734de459ceea5c7e79ddba70cbe3b751deb1e",
        "certificate-v1": "7d1c0e6a700cd48ed1a669d578988faa5de229e846a763c87704080ec959c90f"}
    assert {item["id"]: canonical_hash(item) for item in profile_versions() if item["revision"] == 2} == expected
    latest = resolve_profile(ActivateSemanticProfile(profile_id="invoice-v1", reason="Separate semantic columns"))
    assert latest["revision"] == 3
    fields = {field["name"]: field for field in latest["tables"][0]["fields"]}
    assert fields["item_code"]["logical_type"] == "STRING" and fields["item_code"]["business_key"] is False
    assert fields["item_code"]["normalization"] is None and not fields["item_code"]["required"]
    assert not fields["source_quote"]["required"] and "no" in fields["description"]["description"].casefold()
    assert compile_profile(latest)["publication"]["review_policy"] == "ALL"


@pytest.mark.parametrize("code,description", [("0001.234-A", "Fine powder"), (None, "Uncoded service"),
    (r"00A\n19", "Literal backslash identifier"), (None, "ZX-001\nFine powder")])
def test_item_code_and_description_require_source_proposals_never_a_backend_splitter(code, description):
    latest = next(item for item in profile_versions() if item["id"] == "invoice-v1" and item["revision"] == 3)
    definition = compile_profile(latest)
    quote = (code + "\n" if code else "") + description
    header_values = {"invoice_number": "B-001", "issuer": "Example supplier", "buyer": None,
        "issue_date": None, "currency": None, "subtotal": None, "tax": None, "total": "12.50"}
    raw = {"fields": {name: cell(value) for name, value in header_values.items()}, "tables": {"items": [{
        "item_code": cell(code, quote=quote if code else None), "description": cell(description, quote=quote),
        "quantity": cell(None), "unit_price": cell(None), "amount": cell("12.50"), "source_quote": cell(quote)}]}}
    payload = replay([raw], definition)["payload"]
    row = payload["tables"]["items"][0]
    assert row["item_code"]["observed"] == row["item_code"]["value"] == code
    assert row["description"]["observed"] == description
    assert row["source_quote"]["observed"] == quote
    assert "ZX-001" not in description or "ZX-001" in row["description"]["value"]
    assert len(payload["tables"]["items"]) == 1
    assert payload["semantic_evidence"]["model_labels_are_independent_evidence"] is False


def test_repeated_item_codes_are_legitimate_occurrences_not_unique_row_keys():
    latest = next(item for item in profile_versions() if item["id"] == "invoice-v1" and item["revision"] == 3)
    definition = compile_profile(latest)
    headers = {"invoice_number": "B-001", "issuer": "Example supplier", "buyer": None,
        "issue_date": None, "currency": None, "subtotal": None, "tax": None, "total": "25.00"}
    rows = [{"item_code": cell("00019"), "description": cell(description), "quantity": cell(None),
        "unit_price": cell(None), "amount": cell(amount), "source_quote": cell(f"00019 {description} {amount}")}
        for description, amount in [("Morning delivery", "12.50"), ("Evening delivery", "12.50")]]
    result = replay([{"fields": {name: cell(value) for name, value in headers.items()}, "tables": {"items": rows}}], definition)
    assert len(result["payload"]["tables"]["items"]) == 2
    assert all(row["item_code"]["value"] == "00019" for row in result["payload"]["tables"]["items"])
    quality = quality_report(SimpleNamespace(**result, reviewed=False), definition)
    assert quality["checks"]["duplicate_keys"] is True
    assert not any("DUPLICATE_KEY" in issue for issue in quality["issues"])
