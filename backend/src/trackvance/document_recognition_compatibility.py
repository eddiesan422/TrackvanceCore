"""Explicit reuse of unchanged model inputs, without rewriting old events.

Only the native coordinator may persist these receipts in a verification
request. A receipt preserves both complete snapshots and every old event hash;
the IR and validator independently recompute its input equivalence.
"""
from __future__ import annotations

import copy
import json
import re

from .document_normalization import canonical_hash

REUSE_VERSION = "TRACKVANCE_RECOGNITION_INPUT_REUSE_098_V1"
_ASSOCIATION = "DECLARED_LABEL_CONTEXT_V1"
_COMMON_CHANGES = {"automatic_verification", "timeout_seconds", "max_retries", "max_parallel_jobs",
    "configuration_version", "extraction_strategy", "secondary_allow", "scope", "family_id"}


def _need(ok, code):
    if not ok:
        raise ValueError(code)


def visual_semantic_projection(definition):
    """Exact semantic keys consumed by deploy/idp/recognition.semantic_definition.

    Normalization/validation/publication rules are deliberately absent in that
    actual request builder. A contract test binds this projection to its source.
    """
    def field(value, extra_labels=()):
        result = {key: value[key] for key in ("name", "label", "logical_type", "required", "section",
            "date_format", "decimal_separator", "thousands_separator", "currency_symbols", "source_association") if key in value}
        rules = [rule for rule in [value.get("visual"), *value.get("visual_alternatives", [])] if rule]
        labels = [*value.get("aliases", []), *extra_labels]
        contexts = []
        for rule in rules:
            labels.extend([rule.get("label"), *rule.get("label_aliases", [])])
            context = {key: rule[key] for key in ("page", "section_label", "direction", "end_label") if key in rule}
            if rule.get("label"):
                context["anchor_label"] = rule["label"]
            if rule.get("region"):
                context["declared_search_region"] = list(rule["region"])
            if context and context not in contexts:
                contexts.append(context)
        aliases = list(dict.fromkeys(label for label in labels if isinstance(label, str) and label and label != value.get("label")))
        if aliases:
            result["aliases"] = aliases
        if contexts:
            result["source_context"] = contexts
        return result
    tables = []
    for table in definition.get("tables", []):
        visual = table.get("visual") or {}
        labels = table.get("structured_header_labels") or visual.get("header_labels", [])
        aliases = visual.get("header_label_aliases") or []
        columns = [field(value, ([labels[index]] if index < len(labels) else [])
            + (aliases[index] if index < len(aliases) else [])) for index, value in enumerate(table["fields"])]
        tables.append({"name": table["name"], "fields": columns, "declared_column_labels": list(labels)})
    scoped = any(item.get("source_association") == _ASSOCIATION for item in definition.get("fields", []))
    return {"semantic_definition_version": "trackvance-visual-semantic-098-v4-source-association-roles" if scoped else "trackvance-visual-semantic-098-v2",
        "reading_mode": "VERBATIM_OBSERVED_VALUES_NOT_NORMALIZED_OR_EXPECTED_ANSWERS",
        "fields": [field(item) for item in definition.get("fields", [])], "tables": tables}


def _family(events):
    kinds = {a["kind"] for event in events if event["kind"] == "PAGE"
        for a in event["payload"].get("artifacts", [])}
    families = {family for family, marker in (("QWEN", "QWEN_SELECTION_JSON"), ("PADDLE", "PADDLE_PAGE_JSON"),
        ("NATIVE", "NATIVE_PAGE_JSON")) if marker in kinds}
    if not families and not any(e["kind"] == "PAGE" for e in events):
        return "AUXILIARY"
    _need(len(families) == 1, "REUSE_UNKNOWN_OR_MIXED_RAW_MECHANISM")
    return next(iter(families))


def _runtime(events, family):
    if family not in {"QWEN", "PADDLE"}:
        return {}
    engines = [event.get("payload", {}).get("engine", {}) for event in events if event["kind"] in {"PAGE", "TERMINAL"}]
    locks = {engine.get("model_lock_sha256") for engine in engines if engine.get("model_lock_sha256")}
    _need(len(locks) == 1 and re.fullmatch(r"[a-f0-9]{64}", next(iter(locks))), "REUSE_MODEL_RUNTIME_PROVENANCE_REQUIRED")
    return {"model_lock_sha256": next(iter(locks))}


def _inputs(configuration, definition, source_sha256, family, runtime):
    _need(configuration.get("paddle_model") == "PaddleOCR-VL-1.6"
        and configuration.get("llm_model") == "qwen3.5:9b-q4_K_M", "REUSE_MODEL_CONFIGURATION_NOT_PINNED")
    result = {"source_sha256": source_sha256, "mechanism_family": family, "runtime": runtime}
    if family == "QWEN":
        result.update(model=configuration["llm_model"], context_tokens=configuration.get("context_tokens", 32768),
            prompt_version=configuration.get("prompt_version"), semantic_definition=visual_semantic_projection(definition),
            schema_fields=[(f["name"], f.get("source_association") == _ASSOCIATION) for f in definition.get("fields", [])],
            schema_tables=[(t["name"], [(f["name"], f.get("source_association") == _ASSOCIATION) for f in t["fields"]])
                for t in definition.get("tables", [])])
    elif family == "PADDLE":
        # recognition.Session.child passes filename/source/STRUCTURED/budget,
        # never the definition, use_llm, or Qwen context to paddle_runner.
        result.update(model=configuration["paddle_model"], operation="STRUCTURED")
    elif family == "NATIVE":
        result.update(operation="NATIVE_PAGE_SOURCE_LINES")
    else:
        result.update(operation="PRESERVED_PHASE_METADATA_NOT_A_VALUE_CONTROL")
    return result


def _compatible_scope(old, current):
    # Ownership is enforced by the native service before this pure factory.
    # An inherited organization snapshot may become an explicit override for
    # that original's family, while an established different family is never
    # silently relabelled as the current source owner.
    _need(not old.get("family_id") or not current.get("family_id")
        or old["family_id"] == current["family_id"], "REUSE_FAMILY_OWNERSHIP_METADATA_CHANGED")


def _check_visual_requests(events, old_definition, old_configuration):
    from .document_representation import decode_artifact

    requests = [(event, json.loads(decode_artifact(artifact))) for event in events for artifact in event.get("payload", {}).get("artifacts", [])
        if artifact["kind"] == "QWEN_REQUEST_JSON"]
    pages = {event["page_one_based"] for event in events if event["kind"] == "PAGE"
        and any(item["kind"] == "QWEN_SELECTION_JSON" for item in event.get("payload", {}).get("artifacts", []))}
    _need(len(requests) == len(pages) and bool(pages), "REUSE_ORIGINAL_QWEN_REQUESTS_REQUIRED")
    for event, request in requests:
        body = request["request_without_image_bytes"]
        user = json.loads(next(item["content"] for item in body["messages"] if item["role"] == "user"))
        image = request["image"]
        _need(user["definition"] == visual_semantic_projection(old_definition)
            and body["model"] == old_configuration["llm_model"]
            and body["options"]["num_ctx"] == old_configuration.get("context_tokens", 32768)
            and request["source_sha256"] == event["source_sha256"]
            and request["definition_sha256"] == event["definition_sha256"]
            and request["configuration_sha256"] == event["configuration_sha256"]
            and user["page"] == image["source_page"] == event["page_one_based"]
            and user["document_pages"] == event["expected_pages"]
            and image["query_region"] == [0, 0, 1, 1]
            and image["geometry_kind"] == "QUERY_REGION"
            and body["format"] == user["json_schema"],
            "REUSE_ACTUAL_VISUAL_INPUT_MISMATCH")


def approve_recognition_reuse(events, *, source_sha256, configuration, definition,
                              configuration_snapshots, definition_snapshots):
    """Build stage receipts after native ownership/permission checks, without IO."""
    _need(bool(events) and any(event.get("kind") == "PAGE" for event in events), "REUSE_SAVED_PAGES_REQUIRED")
    current_config_hash, current_definition_hash = canonical_hash(configuration), canonical_hash(definition)
    result = {}
    for stage in dict.fromkeys(event["stage"] for event in events):
        current = [event for event in events if event["stage"] == stage]
        for event in current:
            _need(event.get("source_sha256") == source_sha256 and event.get("event_sha256") == canonical_hash({
                key: value for key, value in event.items() if key not in {"event_sha256", "durable_refs"}}), "REUSE_ORIGINAL_EVENT_INTEGRITY")
        config_hashes = {event["configuration_sha256"] for event in current}
        definition_hashes = {event["definition_sha256"] for event in current}
        _need(len(config_hashes) == len(definition_hashes) == 1, "REUSE_STAGE_SNAPSHOT_MIXED")
        config_hash, definition_hash = next(iter(config_hashes)), next(iter(definition_hashes))
        old_config, old_definition = configuration_snapshots.get(config_hash), definition_snapshots.get(definition_hash)
        _need(isinstance(old_config, dict) and canonical_hash(old_config) == config_hash
            and isinstance(old_definition, dict) and canonical_hash(old_definition) == definition_hash, "REUSE_ORIGINAL_SNAPSHOTS_REQUIRED")
        family = _family(current)
        terminals = [event for event in current if event["kind"] == "TERMINAL"]
        if any(event["kind"] == "PAGE" for event in current):
            _need(terminals and all(event.get("resources_released") is True for event in terminals), "REUSE_PRIOR_RESOURCES_UNVERIFIED")
        differences = sorted(key for key in set(old_config) | set(configuration) if old_config.get(key) != configuration.get(key))
        _compatible_scope(old_config, configuration)
        allowed = _COMMON_CHANGES | ({"use_llm", "context_tokens"} if family in {"PADDLE", "AUXILIARY", "NATIVE"} else set())
        _need(set(differences) <= allowed, "REUSE_CONFIGURATION_INPUTS_CHANGED")
        runtime = _runtime(current, family)
        before = _inputs(old_config, old_definition, source_sha256, family, runtime)
        after = _inputs(configuration, definition, source_sha256, family, runtime)
        _need(before == after, "REUSE_MODEL_INPUTS_CHANGED")
        if family == "QWEN":
            requests = {event["request_id"] for event in current}
            _check_visual_requests([event for event in events if event["request_id"] in requests], old_definition, old_config)
        result[stage] = {"version": REUSE_VERSION, "stage": stage, "source_sha256": source_sha256,
            "configuration_sha256": config_hash, "definition_sha256": definition_hash,
            "configuration": copy.deepcopy(old_config), "definition": copy.deepcopy(old_definition),
            "current_configuration": copy.deepcopy(configuration), "current_definition": copy.deepcopy(definition),
            "current_configuration_sha256": current_config_hash, "current_definition_sha256": current_definition_hash,
            "event_hashes": [event["event_sha256"] for event in current],
            "attempt_ids": sorted({event["attempt_id"] for event in current}),
            "request_ids": sorted({event["request_id"] for event in current}), "mechanism_family": family,
            "model": old_config.get("llm_model") if family == "QWEN" else old_config.get("paddle_model") if family == "PADDLE" else None,
            "input_fingerprint_sha256": canonical_hash(before), "configuration_differences": differences,
            "definition_inputs_equivalent": True, "runtime_evidence": runtime}
    return result


def validate_recognition_bindings(bindings, events, *, source_sha256, configuration_sha256, definition_sha256=None):
    """Recompute approved old-input receipts; unbound new events stay strict."""
    _need(isinstance(bindings, dict) and bool(bindings), "REUSE_BINDINGS_REQUIRED")
    first = next(iter(bindings.values()))
    configuration, definition = first["current_configuration"], first["current_definition"]
    _need(canonical_hash(configuration) == configuration_sha256
        and (definition_sha256 is None or canonical_hash(definition) == definition_sha256), "REUSE_CURRENT_SNAPSHOT_BINDING")
    hashes = {value for binding in bindings.values() for value in binding["event_hashes"]}
    reused = [event for event in events if event.get("event_sha256") in hashes]
    _need(len(reused) == len(hashes), "REUSE_ORIGINAL_EVENTS_MISSING_OR_DUPLICATED")
    expected = approve_recognition_reuse(reused, source_sha256=source_sha256, configuration=configuration, definition=definition,
        configuration_snapshots={item["configuration_sha256"]: item["configuration"] for item in bindings.values()},
        definition_snapshots={item["definition_sha256"]: item["definition"] for item in bindings.values()})
    _need(expected == bindings, "REUSE_COMPATIBILITY_RECEIPT_CHANGED")


def validate_reuse_snapshot(binding, *, source_sha256, configuration, definition):
    """Recheck one persisted receipt at certification without model execution."""
    _need(binding.get("version") == REUSE_VERSION and binding.get("source_sha256") == source_sha256,
        "REUSE_SOURCE_RECEIPT_MISMATCH")
    old_config, old_definition = binding["configuration"], binding["definition"]
    _need(canonical_hash(old_config) == binding["configuration_sha256"]
        and canonical_hash(old_definition) == binding["definition_sha256"]
        and binding["current_configuration"] == configuration and binding["current_definition"] == definition
        and canonical_hash(configuration) == binding["current_configuration_sha256"]
        and canonical_hash(definition) == binding["current_definition_sha256"], "REUSE_SNAPSHOT_HASH_MISMATCH")
    family, runtime = binding["mechanism_family"], binding["runtime_evidence"]
    differences = sorted(key for key in set(old_config) | set(configuration) if old_config.get(key) != configuration.get(key))
    _compatible_scope(old_config, configuration)
    allowed = _COMMON_CHANGES | ({"use_llm", "context_tokens"} if family in {"PADDLE", "AUXILIARY", "NATIVE"} else set())
    _need(differences == binding["configuration_differences"] and set(differences) <= allowed, "REUSE_CONFIGURATION_INPUTS_CHANGED")
    before = _inputs(old_config, old_definition, source_sha256, family, runtime)
    after = _inputs(configuration, definition, source_sha256, family, runtime)
    _need(before == after and canonical_hash(before) == binding["input_fingerprint_sha256"]
        and binding["definition_inputs_equivalent"] is True, "REUSE_MODEL_INPUTS_CHANGED")
