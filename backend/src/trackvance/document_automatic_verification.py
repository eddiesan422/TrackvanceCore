"""Coordinator-owned, bounded completion of actionable document controls.

The policy is explicitly frozen in new engine configurations. It never changes
historical snapshots, repeats one model to manufacture independence, or grants
approval: the source-bound validator remains the authority.
"""
from __future__ import annotations

import copy
from typing import Any

from .document_normalization import canonical_hash

POLICY_VERSION = "TRACKVANCE_AUTOMATIC_VERIFICATION_098_V1"
CONFIGURATION_POLICY = "BOUNDED_LOCAL_V1"
MINIMUM_CONTROL_SECONDS = 30


def enabled(configuration):
    return bool(configuration.get("automatic_verification") == CONFIGURATION_POLICY
                and configuration.get("provider") == "PADDLE_QWEN_LOCAL"
                and configuration.get("extraction_strategy", "LEGACY") != "LEGACY")


def recognition_sources(events, configuration, bindings=None):
    """Describe mechanism identity from retained original artifact kinds.

    These bindings are not attestations. Certification independently checks the
    IR, immutable event hashes, source, configuration and durable registry refs.
    """
    result = {}
    for stage in dict.fromkeys(e["stage"] for e in events if e.get("kind") == "PAGE"):
        stage_configuration = (bindings or {}).get(stage, {}).get("configuration", configuration)
        current = [e for e in events if e.get("stage") == stage and e.get("kind") in {"PAGE", "TERMINAL"}]
        pages = [e for e in current if e["kind"] == "PAGE"]
        kinds = {a.get("kind") for e in pages for a in e.get("payload", {}).get("artifacts", [])}
        families = set()
        if kinds & {"NATIVE_PAGE_JSON", "NATIVE_DOCUMENT_JSON"}:
            families.add("NATIVE")
        if kinds & {"QWEN_SELECTION_JSON", "QWEN_REPLY_JSON"}:
            families.add("QWEN")
        if kinds & {"PADDLE_PAGE_JSON", "PADDLE_TABLE_HTML"}:
            families.add("PADDLE")
        family = next(iter(families)) if len(families) == 1 else "UNKNOWN"
        terminal: dict[str, Any] = next((e for e in reversed(current) if e["kind"] == "TERMINAL"), {})
        source_hashes = {e.get("source_sha256") for e in current}
        configuration_hashes = {e.get("configuration_sha256") for e in current}
        if len(source_hashes) != 1 or len(configuration_hashes) != 1:
            family = "UNKNOWN"
        result[stage] = {
            "mechanism_family": family,
            "model": (stage_configuration.get("llm_model") if family == "QWEN" else
                      stage_configuration.get("paddle_model") if family == "PADDLE" else
                      "PDF_NATIVE" if family == "NATIVE" else None),
            "source_sha256": next(iter(source_hashes), None),
            "configuration_sha256": next(iter(configuration_hashes), None),
            "request_ids": sorted({e["request_id"] for e in current}),
            "event_hashes": [e.get("event_sha256") for e in current],
            "manifest_artifact_ids": [e["durable_refs"]["manifest_artifact_id"] for e in current
                                      if e.get("durable_refs", {}).get("manifest_artifact_id")],
            "raw_artifact_ids": sorted({a["artifact_id"] for e in current
                                        for a in e.get("durable_refs", {}).get("artifacts", []) if a.get("artifact_id")}),
            "emitted_pages": sorted({e["page_one_based"] for e in pages}),
            "completed_pages": list(terminal.get("completed_pages", [])),
            "terminal_status": terminal.get("status", "INCOMPLETE"),
            "resources_released": terminal.get("resources_released") is True,
        }
    return result


def source_context(events, source_sha256):
    """Same source registry contract used by the final worker certification."""
    probes = [e for e in events if e.get("kind") == "PHASE" and e.get("stage") == "NATIVE_SOURCE_PROBE"]
    count = probes[0].get("expected_pages") if len(probes) == 1 else 0
    terminals = [e for e in events if e.get("kind") == "TERMINAL"]
    result = {"source_sha256": source_sha256,
            "expected_pages": count if type(count) is int and 1 <= count <= 50 else 0,
            "persisted_event_hashes": [e["durable_refs"]["event_sha256"] for e in events if e.get("durable_refs")],
            "artifacts": [a for e in events for a in e.get("durable_refs", {}).get("artifacts", [])],
            "resources_released": bool(terminals and all(e.get("resources_released") is True for e in terminals))}
    binding_event = next((e for e in reversed(events) if e.get("stage") in {"VERIFICATION_REQUEST", "RECOVERY_REUSE"}
                          and e.get("payload", {}).get("recognition_bindings")), None)
    if binding_event is not None:
        result["recognition_bindings"] = copy.deepcopy(binding_event["payload"]["recognition_bindings"])
        result["recognition_binding_event_hash"] = binding_event["event_sha256"]
    return result


def decision(data, definition, context):
    """Plan only remaining controls using the same V3 corroboration boundaries."""
    from .document_evidence_v3 import corroboration_v3
    from .document_quality import _arithmetic_fields
    from .document_validation_policy import cells

    payload, engine = data["payload"], data["engine"]
    proof = corroboration_v3(data, definition, source_context=context)
    paths, structure = proof["paths"], proof["structure"]
    reasons: list[dict[str, Any]] = []
    native = any(control["stage"] == engine.get("route") and control["mechanism"] == "NATIVE"
                 for control in proof["controls"])
    policy = definition.get("validation_policy") or {}
    if not policy:
        reasons.append({"kind": "CONFIGURATION", "code": "VALIDATION_POLICY_REQUIRED"})
    arithmetic_fields = _arithmetic_fields(definition)
    reasons.extend({"kind": "INTEGRITY", "code": "RETAINED_SOURCE_INTEGRITY", "detail": issue}
                   for issue in proof["integrity_issues"])
    for path, cell, field in cells(payload, definition):
        if not isinstance(cell, dict) or cell.get("validity") != "VALID":
            reasons.append({"kind": "INCIDENT", "code": "UNRESOLVED_VALUE", "path": path})
            continue
        if path not in proof["source_bound_paths"]:
            reasons.append({"kind": "INTEGRITY", "code": "SOURCE_VALUE_BINDING", "path": path})
            continue
        if cell.get("value") is None and not field.get("required", True):
            continue
        if field["logical_type"] == "DECIMAL" and (path.split(".")[0], field["name"]) not in arithmetic_fields:
            reasons.append({"kind": "CONFIGURATION", "code": "ARITHMETIC_CONTROL_UNDECLARED", "path": path})
            continue
        risk = field.get("critical") or field["logical_type"] in {"DECIMAL", "INT64"}
        standard = not risk and policy.get("allow_noncritical_source_bound") is True
        measured = native and cell.get("geometry_kind") == "MEASURED_SOURCE"
        if path not in paths and not standard and not measured:
            reasons.append({"kind": "RISK_POLICY", "code": "INDEPENDENT_SOURCE_REQUIRED", "path": path})
    for table in definition["tables"]:
        name = table["name"]
        rows = payload["tables"].get(name, [])
        if payload.get("table_unparsed", {}).get(name) or not rows and table.get("required", True):
            reasons.append({"kind": "INCIDENT", "code": "TABLE_INCOMPLETE_OR_UNASSOCIATED", "table": name})
        elif not rows and not table.get("required", True) and not proof["optional_absence"].get(name):
            reasons.append({"kind": "INCIDENT", "code": "OPTIONAL_TABLE_ABSENCE_UNVERIFIED", "table": name})
        elif rows and native and not proof["table_inventory"].get(name, {}).get(engine.get("route"), {}).get("covered"):
            reasons.append({"kind": "CONFIGURATION", "code": "DECLARED_TABLE_STRUCTURE_REQUIRED", "table": name})
        elif rows and not native and not structure.get(name):
            reasons.append({"kind": "RISK_POLICY", "code": "INDEPENDENT_ROW_STRUCTURE_REQUIRED", "table": name})
    if payload.get("structural_issues") or payload.get("semantic_conflicts"):
        reasons.append({"kind": "INCIDENT", "code": "STRUCTURE_OR_SEMANTIC_CONFLICT"})
    if any(item.get("status") != "VALID" for key in ("arithmetic", "cross_totals") for item in payload.get(key, [])):
        reasons.append({"kind": "INCIDENT", "code": "DECLARED_ARITHMETIC_CONSISTENCY"})
    return {"version": POLICY_VERSION, "required": bool(reasons),
            "actionable": bool(policy) and any(reason["kind"] in {"RISK_POLICY", "INCIDENT"} for reason in reasons), "reasons": reasons,
            "scope": {"kind": "WHOLE_DOCUMENT", "pages": list(payload.get("pages", [])),
                      "focal_reread": False,
                      "reason": "CURRENT_RECOGNITION_CONTRACT_REQUIRES_COMPLETE_DOCUMENT_FOR_SOURCE_AND_TABLE_ALIGNMENT"},
            "decision_sha256": canonical_hash(reasons)}


def report(before, after, *, status, primary_reused, calls, blocked_reason=None):
    old_paths = {r["path"] for r in before["reasons"] if r.get("path")}
    pending_paths = {r["path"] for r in after["reasons"] if r.get("path")}
    return {"policy_version": POLICY_VERSION, "status": status, "primary_reused": primary_reused,
            "reason_codes": sorted({r["code"] for r in before["reasons"]}),
            "reasons_before": copy.deepcopy(before["reasons"]), "remaining_reasons": copy.deepcopy(after["reasons"]),
            "resolved_paths": sorted(old_paths - pending_paths), "scope": copy.deepcopy(before["scope"]),
            "recognition_calls": copy.deepcopy(calls), "max_secondary_calls": 1, "rereads_allowed": 0,
            "blocked_reason": blocked_reason, "automatic_approval_granted": False, "human_reviewed": False}
