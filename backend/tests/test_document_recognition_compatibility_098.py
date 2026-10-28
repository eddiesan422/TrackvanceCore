"""Reused input receipts preserve original events across explicit new snapshots."""
import copy
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from test_document_lossless_098 import artifact, definition, events, selection

from trackvance.document_lossless import adapt_saved_recognition
from trackvance.document_normalization import canonical_hash
from trackvance.document_quality import quality_report
from trackvance.document_recognition_compatibility import (
    approve_recognition_reuse,
    visual_semantic_projection,
)
from trackvance.document_representation import REUSED_VERSION, representation_from_events
from trackvance.document_validation_policy import certify_lossless_candidate


def gateway_contract():
    path = Path(__file__).resolve().parents[2] / "deploy/idp/recognition.py"
    spec = importlib.util.spec_from_file_location("test_reuse_visual_contract", path)
    result = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(result)
    return result


def configuration():
    return {"provider": "PADDLE_QWEN_LOCAL", "paddle_model": "PaddleOCR-VL-1.6", "llm_model": "qwen3.5:9b-q4_K_M",
        "prompt_version": "trackvance-idp-098-v2", "context_tokens": 8192, "use_llm": False,
        "extraction_strategy": "PADDLE_STRUCTURED", "timeout_seconds": 600, "max_retries": 0,
        "max_parallel_jobs": 1, "configuration_version": 2}


def bundle(config, schema, family="PADDLE"):
    if family == "PADDLE":
        result = events(raw={"parsing_res_list": [{"block_label": "text", "block_content": "Reference: R-001"}]}, stage="PADDLE_STRUCTURED")
    else:
        result = events(selection())
    first = result[0]
    probe = {**{key: first[key] for key in ("version", "request_id", "attempt_id", "source_sha256", "expected_pages")},
        "kind": "PHASE", "stage": "NATIVE_SOURCE_PROBE", "sequence": 0, "page_one_based": None,
        "payload": {"source_page_count_verified": True, "artifacts": [artifact("NATIVE_SOURCE_PROBE_JSON",
            {"engine": {"pages": 1}, "payload": {"native_incomplete_pages": []}})]}}
    result.insert(0, probe)
    if family == "QWEN":
        contract = gateway_contract()
        format_schema = contract.visual_contract(schema)
        body = {"model": config["llm_model"], "format": format_schema, "options": {"num_ctx": config["context_tokens"]},
            "messages": [{"role": "system", "content": "Synthetic contract fixture; no model is called."},
                {"role": "user", "content": json.dumps({"page": 1, "document_pages": 1,
                    "definition": contract.semantic_definition(schema), "json_schema": format_schema})}]}
        request = contract.visual_request_metadata(body, source_sha256="a" * 64,
            definition_sha256=canonical_hash(schema), configuration_sha256=canonical_hash(config),
            image={"sha256": "e" * 64}, page={"page": 1, "page_geometry": {"width": 100, "height": 100}})
        result.insert(1, {**{key: first[key] for key in ("version", "request_id", "attempt_id", "source_sha256", "expected_pages")},
            "kind": "PHASE", "stage": "QWEN_GENERATION", "sequence": 1, "page_one_based": 1,
            "payload": {"artifacts": [artifact("QWEN_REQUEST_JSON", request)]}})
    for index, event in enumerate(result):
        event.update(configuration_sha256=canonical_hash(config), definition_sha256=canonical_hash(schema), sequence=index)
        if event["kind"] in {"PAGE", "TERMINAL"}:
            event["payload"]["engine"] = {"model_lock_sha256": "d" * 64}
        event["event_sha256"] = canonical_hash({key: value for key, value in event.items() if key != "event_sha256"})
    return result


def approved(old_events, old_config, old_definition, new_config, new_definition):
    return approve_recognition_reuse(old_events, source_sha256="a" * 64, configuration=new_config,
        definition=new_definition, configuration_snapshots={canonical_hash(old_config): old_config},
        definition_snapshots={canonical_hash(old_definition): old_definition})


def request(bindings, config, schema):
    event = {"version": 1, "kind": "PHASE", "stage": "VERIFICATION_REQUEST", "sequence": 0,
        "attempt_id": "new-attempt", "request_id": "new-request", "source_sha256": "a" * 64,
        "configuration_sha256": canonical_hash(config), "definition_sha256": canonical_hash(schema),
        "expected_pages": 1, "page_one_based": None,
        "payload": {"recognition_bindings": copy.deepcopy(bindings), "configuration": copy.deepcopy(config)}}
    event["event_sha256"] = canonical_hash(event)
    return event


def test_explicit_paddle_reuse_keeps_old_hashes_and_actual_old_configuration():
    old, schema = configuration(), definition(critical=True)
    saved = bundle(old, schema)
    unchanged = copy.deepcopy(saved)
    new = {**old, "automatic_verification": "BOUNDED_LOCAL_V1", "use_llm": True,
        "context_tokens": 32768, "configuration_version": 3, "timeout_seconds": 900}
    bindings = approved(saved, old, schema, new, schema)
    mixed = [*saved, request(bindings, new, schema)]
    ir = representation_from_events(mixed, source_sha256="a" * 64,
        configuration_sha256=canonical_hash(new), approved_bindings=bindings)
    assert ir["version"] == REUSED_VERSION
    assert ir["configuration_sha256"] == canonical_hash(new)
    assert ir["pages"][0]["configuration_sha256"] == canonical_hash(old)
    assert bindings["PADDLE_STRUCTURED"]["configuration"] == old
    assert saved == unchanged
    with pytest.raises(ValueError, match="RECOGNITION_EVENT_BINDING"):
        representation_from_events(mixed, source_sha256="a" * 64, configuration_sha256=canonical_hash(new))


def test_old_paddle_and_new_qwen_can_corroborate_without_aliasing_either_snapshot():
    old, schema = configuration(), definition(critical=True)
    saved = bundle(old, schema)
    new = {**old, "automatic_verification": "BOUNDED_LOCAL_V1", "use_llm": True, "configuration_version": 3}
    bindings = approved(saved, old, schema, new, schema)
    phase = request(bindings, new, schema)
    secondary = [event for event in bundle(new, schema, "QWEN") if event["stage"] != "NATIVE_SOURCE_PROBE"]
    mixed = [*saved, phase, *secondary]
    data = adapt_saved_recognition(mixed, schema, new, source_sha256="a" * 64,
        attempt_context={"recognition_bindings": bindings})
    context = {"source_sha256": "a" * 64, "expected_pages": 1,
        "persisted_event_hashes": [event["event_sha256"] for event in mixed],
        "resources_released": True, "recognition_bindings": bindings,
        "recognition_binding_event_hash": phase["event_sha256"],
        "artifacts": [{"kind": item["kind"], "sha256": item["sha256"], "page": event["page_one_based"], "stage": event["stage"]}
            for event in mixed for item in event["payload"].get("artifacts", [])]}
    data["engine"]["validation"] = certify_lossless_candidate(data, schema, "a" * 64, source_context=context)
    extraction = SimpleNamespace(payload=data["payload"], engine=data["engine"], reviewed=False)
    assert quality_report(extraction, schema)["status"] == "AUTO_VERIFIED"
    assert data["engine"]["recognition_sources"]["PADDLE_STRUCTURED"]["configuration_sha256"] == canonical_hash(old)
    assert data["engine"]["recognition_sources"]["QWEN_VISUAL"]["configuration_sha256"] == canonical_hash(new)
    context["recognition_binding_event_hash"] = "f" * 64
    data["engine"]["validation"] = certify_lossless_candidate(data, schema, "a" * 64, source_context=context)
    assert quality_report(extraction, schema)["status"] == "REVIEW_REQUIRED"


def test_qwen_reuse_allows_mapping_only_currency_alias_but_not_changed_actual_inputs():
    old = {**configuration(), "use_llm": True, "extraction_strategy": "QWEN_VISUAL"}
    schema = definition(critical=True)
    saved = bundle(old, schema, "QWEN")
    current = copy.deepcopy(schema)
    current["fields"][0]["normalization"] = {"currency_equivalences": {"COP": ["pesos colombianos"]}}
    new = {**old, "automatic_verification": "BOUNDED_LOCAL_V1", "configuration_version": 3}
    bindings = approved(saved, old, schema, new, current)
    assert bindings["QWEN_VISUAL"]["definition_sha256"] != bindings["QWEN_VISUAL"]["current_definition_sha256"]
    changed = copy.deepcopy(current)
    changed["fields"][0]["label"] = "Different requested caption"
    with pytest.raises(ValueError, match="REUSE_MODEL_INPUTS_CHANGED"):
        approved(saved, old, schema, new, changed)
    with pytest.raises(ValueError, match="REUSE_CONFIGURATION_INPUTS_CHANGED"):
        approved(saved, old, schema, {**new, "context_tokens": 16384}, current)


@pytest.mark.parametrize("kind", ["event_hash", "missing_snapshot", "source", "model", "unreleased", "request_body"])
def test_reuse_rejects_incompatible_or_unclosed_inputs(kind):
    old, schema = configuration(), definition(critical=True)
    family = "QWEN" if kind == "request_body" else "PADDLE"
    if family == "QWEN":
        old.update(use_llm=True, extraction_strategy="QWEN_VISUAL")
    saved = bundle(old, schema, family)
    new = {**old, "automatic_verification": "BOUNDED_LOCAL_V1"}
    snapshots = {canonical_hash(old): old}
    if kind == "event_hash":
        saved[-1]["event_sha256"] = "f" * 64
    elif kind == "missing_snapshot":
        snapshots = {}
    elif kind == "source":
        saved[-1]["source_sha256"] = "f" * 64
    elif kind == "model":
        new["paddle_model"] = "invented-model"
    elif kind == "unreleased":
        saved[-1]["resources_released"] = False
        saved[-1]["event_sha256"] = canonical_hash({key: value for key, value in saved[-1].items() if key != "event_sha256"})
    else:
        saved[:] = [event for event in saved if event["stage"] != "QWEN_GENERATION"]
    with pytest.raises(ValueError):
        approve_recognition_reuse(saved, source_sha256="a" * 64, configuration=new, definition=schema,
            configuration_snapshots=snapshots, definition_snapshots={canonical_hash(schema): schema})


def test_backend_projection_matches_actual_gateway_semantic_builder():
    schema = definition(table=True)
    schema["fields"][0].update(source_association="DECLARED_LABEL_CONTEXT_V1", aliases=["Reference id"],
        visual={"page": 1, "label": "Reference", "region": [.1, .1, .9, .3], "direction": "RIGHT", "section_label": "Invoice"})
    schema["tables"][0]["structured_header_labels"] = ["Code", "Description"]
    assert visual_semantic_projection(schema) == gateway_contract().semantic_definition(schema)


def test_inherited_organization_metadata_can_become_an_explicit_same_original_family_override():
    old, schema = {**configuration(), "scope": "ORGANIZATION", "family_id": None}, definition(critical=True)
    saved = bundle(old, schema)
    new = {**old, "scope": "FAMILY", "family_id": "owned-original-family", "use_llm": True,
        "automatic_verification": "BOUNDED_LOCAL_V1"}
    bindings = approved(saved, old, schema, new, schema)
    assert {"scope", "family_id"} <= set(bindings["PADDLE_STRUCTURED"]["configuration_differences"])
    established = {**old, "scope": "FAMILY", "family_id": "another-established-family"}
    with pytest.raises(ValueError, match="REUSE_FAMILY_OWNERSHIP_METADATA_CHANGED"):
        approved(bundle(established, schema), established, schema, new, schema)
