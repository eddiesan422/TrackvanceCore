"""General semantic contracts: fixture/replay safety, no model inference or oracle selection."""
import base64
import copy
import hashlib
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from trackvance.document_engine import extract
from trackvance.document_lossless import adapt_saved_recognition, run_lossless
from trackvance.document_normalization import canonical_hash
from trackvance.document_providers import EngineConfiguration
from trackvance.document_quality import validate_candidate_contract
from trackvance.document_recognition_compatibility import visual_semantic_projection
from trackvance.document_schemas import DocumentDefinition
from trackvance.document_semantic_evidence import (
    record_table_reviews,
    semantic_review_dto,
    semantic_review_issues,
    validate_model_context,
)
from trackvance.document_semantic_general import (
    MODE,
    SemanticProfile,
    assert_configuration,
    catalog,
    compile_profile,
    configuration_for,
    native_semantic_sufficient,
    profiles,
)
from trackvance.document_service import issues
from trackvance.operations_common import OperationError


def profile(*, table=False, collection=False):
    value = {"id": "general-fixture", "name": "General fixture", "revision": 1, "document_type": "CUSTOM",
        "description": "Declared semantic fixture, not a known supplier layout or expected values.",
        "fields": [{"name": "reference", "label": "Reference", "business_key": True, "critical": True},
            {"name": "amount", "label": "Printed amount", "logical_type": "DECIMAL", "required": False}]}
    if table:
        value["tables"] = [{"name": "items", "kind": "COLLECTION" if collection else "TABLE",
            "description": "Requested affirmative actions only." if collection else "Each printed source row occurrence.",
            "required": not collection, "fields": [{"name": "description", "label": "Description"},
                {"name": "amount", "label": "Amount", "logical_type": "DECIMAL", "required": False}]}]
    return SemanticProfile.model_validate(value).model_dump(mode="json")


def cell(observed, *, ambiguous=False, alternatives=None, basis=None, polarity="NOT_APPLICABLE", quote=None):
    return {"observed": observed, "ambiguous": ambiguous, "alternatives": alternatives or [],
        "semantic_context": {"literal_context": quote if quote is not None else observed,
            "basis": basis or ("PRINTED" if observed is not None else "NOT_ON_PAGE"), "polarity": polarity}}


def selection(reference="R-001", amount="12.50", rows=None):
    return {"fields": {"reference": cell(reference), "amount": cell(amount)},
        "tables": {"items": rows} if rows is not None else {}}


def artifact(kind, value):
    raw = json.dumps(value, ensure_ascii=False, sort_keys=True).encode()
    return {"kind": kind, "media_type": "application/json", "bytes": len(raw),
        "sha256": hashlib.sha256(raw).hexdigest(), "content_b64": base64.b64encode(raw).decode()}


def fixture_events(values, definition, configuration, source_sha="a" * 64, *, complete=True):
    count = len(values)
    binding = {"version": 1, "request_id": "semantic-fixture-request", "attempt_id": "semantic-fixture-attempt",
        "source_sha256": source_sha, "configuration_sha256": canonical_hash(configuration),
        "definition_sha256": canonical_hash(definition), "expected_pages": count}
    result = [{**binding, "kind": "PHASE", "sequence": 1, "stage": "NATIVE_SOURCE_PROBE", "page_one_based": None,
        "payload": {"source_page_count_verified": True, "artifacts": [artifact("NATIVE_SOURCE_PROBE_JSON",
            {"engine": {"pages": count}, "payload": {"native_incomplete_pages": list(range(1, count + 1)), "lines": []}})]}}]
    for page, value in enumerate(values, 1):
        result.append({**binding, "kind": "PAGE", "sequence": len(result) + 1, "stage": "QWEN_VISUAL",
            "page_one_based": page, "payload": {"artifacts": [artifact("QWEN_SELECTION_JSON", value)],
                "recognition_status": "COMPLETE", "page_geometry": {"width": 1000, "height": 800, "unit": "PIXEL"},
                "engine": {"fixture_no_model_inference": True}}})
    result.append({**binding, "kind": "TERMINAL", "sequence": len(result) + 1, "stage": "QWEN_VISUAL",
        "page_one_based": None, "status": "COMPLETE" if complete else "PARTIAL", "resources_released": True,
        "emitted_pages": list(range(1, count + 1)), "completed_pages": list(range(1, count + 1)) if complete else [],
        "pending_pages": [] if complete else list(range(1, count + 1)), "payload": {}})
    for event in result:
        event["event_sha256"] = canonical_hash(event)
    return result


def replay(values, definition, *, complete=True):
    configuration = {"provider": "PADDLE_QWEN_LOCAL", "extraction_strategy": "QWEN_VISUAL"}
    events = fixture_events(values, definition, configuration, complete=complete)
    result = adapt_saved_recognition(events, definition, configuration, source_sha256="a" * 64)
    validate_candidate_contract(result, definition)
    return result


def gateway():
    path = Path(__file__).resolve().parents[2] / "deploy/idp/recognition.py"
    spec = importlib.util.spec_from_file_location("semantic_general_gateway_fixture", path)
    value = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(value)
    return value


def test_feature_default_off_and_existing_definitions_serialize_unchanged(monkeypatch):
    monkeypatch.delenv("TRACKVANCE_SEMANTIC_GENERAL_ENABLED", raising=False)
    assert catalog()["enabled"] is False and catalog()["default_enabled"] is False
    legacy = DocumentDefinition.model_validate({"fields": [{"name": "reference", "label": "Reference"}]}).model_dump(mode="json")
    assert "semantic_general" not in legacy
    assert DocumentDefinition.model_validate(legacy).model_dump(mode="json") == legacy
    with pytest.raises(OperationError) as error:
        configuration_for(compile_profile(profile()), {})
    assert error.value.code == "DOCUMENT_SEMANTIC_GENERAL_DISABLED"


@pytest.mark.parametrize("identity", ["invoice-v1", "statement-v1", "narrative-v1", "certificate-v1"])
def test_general_profiles_are_versioned_layout_free_and_use_native_projections(identity):
    selected = next(value for value in profiles() if value["id"] == identity)
    definition = compile_profile(selected)
    assert definition["semantic_general"]["profile_sha256"] == canonical_hash(selected)
    assert definition["publication"]["review_policy"] == "ALL"
    assert all(field.get("visual") is None and not field.get("visual_alternatives") for field in definition["fields"])
    assert all(table["semantic_collection"] and table.get("visual") is None and not table["row_pattern"] for table in definition["tables"])
    assert definition == DocumentDefinition.model_validate(definition).model_dump(mode="json")


@pytest.mark.parametrize("mutation", ["hash", "required", "critical", "label", "format", "normalization", "table_required", "visual", "auto"])
def test_profile_hash_binds_projection_controls_and_cannot_hide_visual_or_auto(mutation):
    definition = compile_profile(profile(table=True))
    if mutation == "hash":
        definition["semantic_general"]["profile_sha256"] = "0" * 64
    elif mutation == "required":
        definition["fields"][0]["required"] = False
    elif mutation == "critical":
        definition["fields"][0]["critical"] = False
    elif mutation == "label":
        definition["fields"][0]["label"] = "Different meaning"
    elif mutation == "format":
        definition["fields"][1]["decimal_separator"] = ","
    elif mutation == "normalization":
        definition["fields"][1]["normalization"] = {"numeric_semantics": "DEDUCTION_MAGNITUDE"}
    elif mutation == "table_required":
        definition["tables"][0]["required"] = False
    elif mutation == "visual":
        definition["fields"][0]["pattern"] = "SUPPLIER(.+)"
    else:
        definition["publication"]["review_policy"] = "AUTO_VERIFIED"
    with pytest.raises(ValidationError):
        DocumentDefinition.model_validate(definition)


def test_effective_semantic_configuration_is_frozen_independent_and_no_mandatory_second_engine(monkeypatch):
    monkeypatch.setenv("TRACKVANCE_SEMANTIC_GENERAL_ENABLED", "true")
    definition = compile_profile(profile())
    legacy = EngineConfiguration().model_dump()
    frozen = configuration_for(definition, legacy)
    assert legacy["provider"] == "TESSERACT_LEGACY"
    assert frozen["provider"] == "PADDLE_QWEN_LOCAL" and frozen["extraction_strategy"] == "QWEN_VISUAL"
    assert frozen["extraction_mode"] == MODE and frozen["automatic_verification"] == "OFF" and frozen["max_retries"] == 0
    assert_configuration(definition, frozen)
    with pytest.raises(OperationError):
        assert_configuration(definition, {**frozen, "automatic_verification": "BOUNDED_LOCAL_V1"})


def test_gateway_contract_keeps_literal_transport_and_unverified_context_without_legacy_delta():
    api = gateway()
    definition = compile_profile(profile(table=True))
    raw = selection(rows=[{"description": cell("Printed row"), "amount": cell("12.50")}])
    api.validate_selection(raw, definition)
    assert api.semantic_definition(definition) == visual_semantic_projection(definition)
    schema = api.visual_contract(definition)
    assert "semantic_context" in schema["properties"]["fields"]["properties"]["amount"]["required"]
    assert "destination types" in api.SEMANTIC_GENERAL_LITERAL_INSTRUCTIONS
    assert "Never fill an unprinted subtotal" in api.SEMANTIC_GENERAL_GUARD_INSTRUCTIONS
    legacy = {"fields": [{"name": "reference"}], "tables": []}
    assert "semantic_context" not in api.visual_contract(legacy)["properties"]["fields"]["properties"]["reference"]["properties"]
    api.validate_selection({"fields": {"reference": {"observed": "R", "ambiguous": False, "alternatives": []}}, "tables": {}}, legacy)
    broken = copy.deepcopy(raw)
    broken["fields"]["amount"]["observed"] = 12.5
    with pytest.raises(ValueError):
        api.validate_selection(broken, definition)


def test_contradictory_pages_are_ambiguous_and_local_absence_does_not_erase_documentary_value():
    definition = compile_profile(profile())
    value = replay([selection(amount="12.50"), selection(reference=None, amount=None)], definition)
    assert value["payload"]["fields"]["amount"]["value"] == "12.50"
    assert len(value["payload"]["semantic_evidence"]["observations"]) == 4
    conflict = replay([selection(amount="12.50"), selection(amount="13.50")], definition)
    amount = conflict["payload"]["fields"]["amount"]
    assert amount["observed"] == "12.50" and amount["value"] is None and amount["validity"] == "AMBIGUOUS"
    assert len(amount["candidate_alternatives"]) == 2
    assert "VALUE_INVALID:headers.0.amount" in issues(SimpleNamespace(**conflict, reviewed=False), definition)


def test_ambiguous_null_and_alternatives_remain_unresolved():
    definition = compile_profile(profile())
    second = selection(amount=None)
    second["fields"]["amount"] = cell(None, ambiguous=True, alternatives=["12.50", "13.50"], basis="UNCERTAIN")
    result = replay([selection(), second], definition)
    amount = result["payload"]["fields"]["amount"]
    assert amount["validity"] == "AMBIGUOUS" and amount["value"] is None
    retained = next(item for item in result["payload"]["semantic_evidence"]["observations"]
        if item["source_page"] == 2 and item["field"] == "amount")
    assert retained["alternatives"] == ["12.50", "13.50"]


@pytest.mark.parametrize("basis", ["UNREADABLE", "UNCERTAIN"])
@pytest.mark.parametrize("observed", [None, "12.50"])
def test_context_uncertainty_is_retained_even_when_model_ambiguous_flag_is_false(basis, observed):
    definition = compile_profile(profile())
    second = selection()
    second["fields"]["amount"] = cell(observed, basis=basis)
    result = replay([selection(), second], definition)
    amount = result["payload"]["fields"]["amount"]
    assert amount["observed"] == "12.50" and amount["value"] is None and amount["validity"] == "AMBIGUOUS"
    assert "FIELD:amount" in result["payload"]["semantic_conflicts"]


def test_nonnull_value_cannot_be_reported_as_not_on_page():
    definition = compile_profile(profile())
    raw = selection()
    raw["fields"]["amount"] = cell("12.50", basis="NOT_ON_PAGE")
    result = replay([raw], definition)
    assert result["payload"]["fields"]["amount"]["validity"] == "AMBIGUOUS"
    assert "VALUE_CONTRADICTS_REPORTED_LOCAL_ABSENCE" in result["payload"]["semantic_evidence"]["guards"][0]["reasons"]


@pytest.mark.parametrize("alternative,ambiguous", [("13.50", True), ("12.5", False)])
def test_alternatives_are_resolved_by_declared_type_equivalence_not_model_flag(alternative, ambiguous):
    definition = compile_profile(profile())
    raw = selection()
    raw["fields"]["amount"] = cell("12.50", alternatives=[alternative], ambiguous=False)
    result = replay([raw], definition)
    assert (result["payload"]["fields"]["amount"]["validity"] == "AMBIGUOUS") is ambiguous


@pytest.mark.parametrize("key", ["basis", "polarity"])
@pytest.mark.parametrize("invalid", [None, [], {}, 1])
def test_invalid_model_context_enum_is_a_contract_value_error_not_server_type_error(key, invalid):
    context = cell("value")["semantic_context"]
    context[key] = invalid
    with pytest.raises(ValueError):
        validate_model_context(context)
    definition = compile_profile(profile())
    raw = selection()
    raw["fields"]["amount"]["semantic_context"] = context
    with pytest.raises(ValueError):
        gateway().validate_selection(raw, definition)


@pytest.mark.parametrize("invalid", [None, [], {}, 1])
def test_invalid_semantic_route_is_a_validation_error_not_server_type_error(invalid):
    definition = compile_profile(profile())
    definition["semantic_general"]["route"] = invalid
    with pytest.raises(ValidationError):
        DocumentDefinition.model_validate(definition)


@pytest.mark.parametrize("basis", ["CALCULATED", "INFERRED"])
def test_unprinted_proposals_are_retained_but_block_publication_even_if_arithmetic_could_match(basis):
    definition = compile_profile(profile())
    raw = selection()
    raw["fields"]["amount"] = cell("12.50", basis=basis)
    result = replay([raw], definition)
    amount = result["payload"]["fields"]["amount"]
    assert amount["observed"] == "12.50" and amount["validity"] == "AMBIGUOUS" and amount["value"] is None
    assert "FIELD:amount" in result["payload"]["semantic_conflicts"]
    evidence = result["payload"]["semantic_evidence"]
    assert not evidence["model_labels_are_independent_evidence"] and not evidence["arithmetic_proves_printed_values"]


@pytest.mark.parametrize("polarity", ["NEGATED", "CONDITIONAL"])
def test_nonaffirmative_narrative_proposals_are_not_silently_accepted_as_commitments(polarity):
    definition = compile_profile(profile(table=True, collection=True))
    raw = selection(rows=[{"description": cell("Purchase", polarity=polarity, quote="Purchase"), "amount": cell(None)}])
    result = replay([raw], definition)
    assert result["payload"]["tables"]["items"][0]["description"]["validity"] == "AMBIGUOUS"
    assert "CELL:items.0.description" in result["payload"]["semantic_conflicts"]
    assert "NONAFFIRMATIVE_NARRATIVE_PROPOSAL" in result["payload"]["semantic_evidence"]["guards"][0]["reasons"]


def test_repeated_rows_are_retained_and_require_source_coverage_instead_of_deduplication():
    definition = compile_profile(profile(table=True))
    row = {"description": cell("Repeated item"), "amount": cell("12.50")}
    result = replay([selection(rows=[row, row])], definition)
    payload = result["payload"]
    assert len(payload["tables"]["items"]) == 2 and len(payload["row_provenance"]["items"]) == 2
    assert "TABLE:items" in payload["semantic_conflicts"]
    assert semantic_review_issues(payload, definition) == ["SEMANTIC_TABLE_COVERAGE_REVIEW_REQUIRED:items"]


def test_full_collection_review_is_explicit_hash_bound_and_invalidated_by_row_changes():
    definition = compile_profile(profile(table=True, collection=True))
    result = replay([selection(rows=[])], definition)
    payload = result["payload"]
    record_table_reviews(payload, definition, ["items"], source_sha256="a" * 64,
        actor_id="authorized-fixture-reviewer", reason="Checked the synthetic original, no eligible actions.", created_at="2026-11-01T00:00:00Z")
    assert semantic_review_issues(payload, definition, reviewed=True, source_sha256="a" * 64) == []
    assert semantic_review_issues(payload, definition, reviewed=False, source_sha256="a" * 64)
    assert semantic_review_issues(payload, definition, reviewed=True, source_sha256="b" * 64)
    payload["tables"]["items"].append({"description": {"observed": "Unexpected", "reviewed": True}})
    assert semantic_review_issues(payload, definition, reviewed=True, source_sha256="a" * 64)


def test_provider_cannot_forge_authorized_collection_confirmation():
    definition = compile_profile(profile(table=True))
    result = replay([selection(rows=[])], definition)
    result["payload"]["semantic_table_reviews"] = []
    with pytest.raises(OperationError) as error:
        validate_candidate_contract(result, definition)
    assert error.value.code == "DOCUMENT_PROVIDER_CONTRACT"


def test_native_shortcut_needs_exact_unique_label_value_context_and_no_unmeasured_rows():
    definition = compile_profile(profile())
    lines = [{"text": "Reference: R-001", "page": 1, "region": [0, .1, 1, .2], "confidence": None, "method": "DIGITAL"}]
    native = {"payload": extract(lines, definition), "engine": {"pages": 1}}
    assert native_semantic_sufficient(native, definition)
    duplicate = {"payload": extract(lines + lines, definition), "engine": {"pages": 1}}
    assert not native_semantic_sufficient(duplicate, definition)
    wrong = copy.deepcopy(native)
    wrong["payload"]["lines"][0]["text"] = "See Reference R-001 in another section"
    assert not native_semantic_sufficient(wrong, definition)
    table_definition = compile_profile(profile(table=True))
    assert extract(lines, table_definition)["tables"] == {"items": []}
    assert not native_semantic_sufficient(native, table_definition)


def test_qwen_semantic_route_does_not_shortcut_to_native_or_call_paddle(monkeypatch, tmp_path):
    monkeypatch.setenv("TRACKVANCE_SEMANTIC_GENERAL_ENABLED", "true")
    definition = compile_profile(profile())
    configuration = configuration_for(definition, EngineConfiguration(context_tokens=32768).model_dump())
    source = tmp_path / "source.pdf"
    source.write_bytes(b"synthetic no-model source for coordinator fixture")
    lines = [{"text": "Reference: R-001", "page": 1, "region": [0, .1, 1, .2], "confidence": None, "method": "DIGITAL"}]
    native = {"payload": extract(lines, definition), "engine": {"pages": 1}}
    calls = []
    def recognize(_source, _definition, _configuration, **kwargs):
        calls.append(kwargs["strategy"])
        values = fixture_events([selection()], definition, configuration, hashlib.sha256(source.read_bytes()).hexdigest())
        values = [event for event in values if event["stage"] != "NATIVE_SOURCE_PROBE"]
        for value in values:
            kwargs["on_event"](value)
        return {"pages": values[:-1], "terminal": values[-1]}
    result = run_lossless(source, definition, configuration, legacy_runner=lambda *_args, **_kwargs: copy.deepcopy(native),
        check=lambda _progress: None, budget=None, recognize=recognize)
    assert calls == ["QWEN_VISUAL"] and result["engine"]["route"] == "QWEN_VISUAL"
    assert result["engine"]["execution"]["recognition_complete"] is True
    assert result["payload"]["semantic_evidence"]["verification"] == "AUTHORIZED_REVIEW_REQUIRED"


def test_complete_candidate_remains_review_required_and_value_states_do_not_claim_auto():
    definition = compile_profile(profile())
    result = replay([selection()], definition)
    extraction = SimpleNamespace(**result, reviewed=False)
    assert "REVIEW_REQUIRED:headers.0.reference" in issues(extraction, definition)
    dto = semantic_review_dto(extraction, definition)
    assert dto["automatic_approval"] is False
    assert all(item["verified"] is None for item in dto["values"])
    result["payload"]["fields"]["reference"].update(reviewed=True, corrected=True)
    dto = semantic_review_dto(SimpleNamespace(**result, reviewed=True), definition)
    assert dto["values"][0]["state"] == "AUTHORIZED_CORRECTION"
    assert dto["values"][0]["observed"] == "R-001" and dto["values"][0]["verified"] == "R-001"


def test_incomplete_execution_remains_blocked_even_with_valid_cells():
    definition = compile_profile(profile())
    result = replay([selection()], definition, complete=False)
    assert "RECOGNITION_INCOMPLETE" in issues(SimpleNamespace(**result, reviewed=False), definition)
