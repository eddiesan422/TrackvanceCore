"""Source-bound selection of retained recognition hypotheses, without inference."""
from __future__ import annotations

import copy
from decimal import Decimal

from .document_normalization import canonical_hash, normalization_matches, normalize_observation

SELECTION_VERSION = "TRACKVANCE_CANDIDATE_SELECTION_098_V2"
SECONDARY_VERSION = "TRACKVANCE_SELECTIVE_DECISION_098_V2"


def mechanism_family(stage):
    if isinstance(stage, str):
        for family in ("QWEN", "PADDLE"):
            if stage.startswith(family):
                return family
        if stage == "DIGITAL_NATIVE":
            return "NATIVE"
    return None


def normalized_value(cell, field):
    if not isinstance(cell, dict) or cell.get("validity") != "VALID" or not normalization_matches(cell, field):
        return None
    value, validity, _ = normalize_observation(cell.get("observed"), field)
    if validity != "VALID" or value is None:
        return None
    if field["logical_type"] == "DECIMAL":
        return Decimal(value)
    if field["logical_type"] == "INT64":
        return int(value)
    return value


def _complete(candidate):
    execution = candidate.get("execution", {})
    return (execution.get("recognition_complete") is True and execution.get("resources_released") is True
        and mechanism_family(candidate.get("stage")) is not None
        and not candidate["payload"].get("structural_issues"))


def _located(cell, candidate):
    return bool(cell.get("evidence_refs") and (cell.get("origin") or {}).get("page")
        in candidate.get("execution", {}).get("completed_pages", []))


def _absent(cell):
    # An unobserved value with actual competing suggestions is an exception,
    # rather than an empty slot that another mechanism may silently fill.
    def empty(value):
        return value is None or isinstance(value, str) and not value.strip()

    if not empty(cell.get("observed")):
        return False
    for alternative in cell.get("candidate_alternatives", []):
        if isinstance(alternative, str) and alternative.strip():
            return False
        if isinstance(alternative, dict):
            observation = alternative.get("candidate", alternative)
            if not empty(observation.get("observed")) or any(
                    isinstance(value, str) and value.strip() for value in observation.get("alternatives", [])):
                return False
    return True


def _record(cell, stage, reason, identity):
    cell["candidate_selection"] = {"version": SELECTION_VERSION, "selected_stage": stage,
        "selected_mechanism": mechanism_family(stage), "reason": reason, "identity_basis": identity}


def _resolve_cell(first, field, choices, selected, *, identity):
    if first.get("reviewed") or first.get("corrected"):
        return first, {"case": "PROTECTED_REVIEW", "reason": "REVIEWED_VALUE_PRESERVED"}
    original = copy.deepcopy(first)
    eligible = [(candidate, cell) for candidate, cell in choices if _complete(candidate)
        and normalized_value(cell, field) is not None and _located(cell, candidate)]
    retained = [{"stage": candidate["stage"], "candidate": copy.deepcopy(cell)}
        for candidate, cell in choices if cell.get("observed") is not None]
    first["candidate_alternatives"] = [*first.get("candidate_alternatives", []), *retained]
    values = {normalized_value(cell, field) for _, cell in eligible}
    current = normalized_value(first, field)
    if current is not None:
        values.add(current)
    if len(values) > 1:
        first.update(value=None, validity="AMBIGUOUS")
        _record(first, selected, "CONTRADICTORY_NORMALIZED_OBSERVATIONS", identity)
        return first, {"case": "C", "reason": "CONTRADICTORY_NORMALIZED_OBSERVATIONS"}
    if _absent(original) and eligible:
        candidate, cell = eligible[0]
        result = copy.deepcopy(cell)
        result["candidate_alternatives"] = [{"stage": selected, "candidate": original},
            *copy.deepcopy(retained), *result.get("candidate_alternatives", [])]
        _record(result, candidate["stage"], "ABSENT_PRIMARY_SOURCE_BOUND_ALTERNATIVE", identity)
        return result, {"case": "A", "reason": "ABSENT_PRIMARY_SOURCE_BOUND_ALTERNATIVE",
            "selected_stage": candidate["stage"]}
    if current is not None and eligible:
        _record(first, selected, "DECLARED_NORMALIZATION_EQUIVALENCE", identity)
        return first, {"case": "B", "reason": "DECLARED_NORMALIZATION_EQUIVALENCE"}
    if first.get("observed") is not None and current is None:
        _record(first, selected, "INVALID_OR_UNRESOLVED_PRIMARY_OBSERVATION", identity)
        return first, {"case": "D", "reason": "INVALID_OR_UNRESOLVED_PRIMARY_OBSERVATION"}
    _record(first, selected, "INSUFFICIENT_SOURCE_BOUND_ALTERNATIVE", identity)
    return first, {"case": "E", "reason": "INSUFFICIENT_SOURCE_BOUND_ALTERNATIVE"}


def combine_candidates(payload, candidates, definition, selected):
    """Preserve whole hypotheses and resolve only bound fields/row occurrences."""
    from .document_validation_policy import align_rows

    payload["candidate_history"] = [{"stage": item["stage"], "payload_sha256": item["payload_sha256"],
        "execution": copy.deepcopy(item.get("execution", {})),
        "fields": copy.deepcopy(item["payload"]["fields"]), "tables": copy.deepcopy(item["payload"]["tables"]),
        "row_provenance": copy.deepcopy(item["payload"].get("row_provenance", {})),
        "header_associations": copy.deepcopy(item["payload"].get("header_associations", {})),
        "structural_issues": copy.deepcopy(item["payload"].get("structural_issues", [])),
        "table_unparsed": copy.deepcopy(item["payload"].get("table_unparsed", {}))}
        for item in candidates]
    others = [item for item in candidates if item["stage"] != selected]
    decisions = {}
    for field in definition["fields"]:
        name, path = field["name"], "headers.0." + field["name"]
        choices = [(item, item["payload"]["fields"][name]) for item in others]
        payload["fields"][name], decisions[path] = _resolve_cell(payload["fields"][name], field,
            choices, selected, identity="DECLARED_FIELD_AND_DURABLE_SOURCE_LOCATION")
        if decisions[path]["case"] == "C":
            payload["semantic_conflicts"].append("FIELD:" + name)
    for table in definition["tables"]:
        name, rows = table["name"], payload["tables"][table["name"]]
        aligned = []
        for item in others:
            control = item["payload"]["tables"][name]
            if not control:
                continue
            matches, bases = align_rows(rows, control, table["fields"])
            if len(rows) != len(control) or len(matches) != len(rows):
                payload["semantic_conflicts"].append("TABLE:" + name)
                decisions["table:" + name + ":" + item["stage"]] = {
                    "case": "E", "reason": "COMPLETE_ONE_TO_ONE_ROW_IDENTITY_UNPROVEN"}
                continue
            if item["payload"].get("table_unparsed", {}).get(name):
                decisions["table:" + name + ":" + item["stage"]] = {
                    "case": "D", "reason": "ALTERNATIVE_TABLE_STRUCTURE_UNRESOLVED"}
                continue
            aligned.append((item, matches, bases))
        for index, row in enumerate(rows):
            for field in table["fields"]:
                path = f"{name}.{index}.{field['name']}"
                choices = [(item, item["payload"]["tables"][name][matches[index]][field["name"]])
                    for item, matches, _ in aligned]
                identity = [{"stage": item["stage"], "source_occurrence": index,
                    "alternative_occurrence": matches[index], "basis": bases[index]}
                    for item, matches, bases in aligned]
                for evidence, (item, matches, _) in zip(identity, aligned, strict=True):
                    sources = payload.get("row_provenance", {}).get(name, [])
                    alternatives = item["payload"].get("row_provenance", {}).get(name, [])
                    evidence["source_occurrence_id"] = sources[index].get("occurrence_id") if index < len(sources) else None
                    target = matches[index]
                    evidence["alternative_occurrence_id"] = alternatives[target].get("occurrence_id") if target < len(alternatives) else None
                row[field["name"]], decisions[path] = _resolve_cell(row[field["name"]], field,
                    choices, selected, identity=identity or "ROW_IDENTITY_UNPROVEN")
                if decisions[path]["case"] == "C":
                    payload["semantic_conflicts"].append(f"CELL:{name}.{index}.{field['name']}")
    payload["semantic_conflicts"] = list(dict.fromkeys(payload["semantic_conflicts"]))
    payload["candidate_selection"] = {"version": SELECTION_VERSION,
        "primary_stage": selected, "primary_basis": "FIRST_RETAINED_RECOGNITION_STAGE",
        "decisions": decisions, "candidate_hypothesis_hashes": [item["payload_sha256"] for item in candidates]}


def secondary_decision(data, definition):
    """Explain unmet controls; this is not a provider coverage attestation."""
    from .document_validation_policy import _corroborated, _independent_candidates, cells

    payload, engine = data["payload"], data["engine"]
    controls = _independent_candidates(data, None)
    corroborated, structure, _ = _corroborated(payload, definition, controls, primary_stage=engine.get("route"))
    reasons = []
    policy = definition.get("validation_policy") or {}
    native = engine.get("route") == "DIGITAL_NATIVE"
    for path, cell, field in cells(payload, definition):
        if not isinstance(cell, dict) or cell.get("validity") != "VALID":
            reasons.append({"kind": "INCIDENT", "code": "UNRESOLVED_VALUE", "path": path})
            continue
        if cell.get("value") is None and not field.get("required", True):
            continue
        risk = field.get("critical") or field["logical_type"] in {"DECIMAL", "INT64"}
        standard_allowed = not risk and policy.get("allow_noncritical_source_bound") is True
        measured_native = native and cell.get("geometry_kind") == "MEASURED_SOURCE"
        if path not in corroborated and not measured_native and not standard_allowed:
            reasons.append({"kind": "RISK_POLICY", "code": "INDEPENDENT_SOURCE_REQUIRED", "path": path})
    for table in definition["tables"]:
        name = table["name"]
        rows, unparsed = payload["tables"].get(name, []), payload.get("table_unparsed", {}).get(name)
        if unparsed or not rows and table.get("required", True):
            reasons.append({"kind": "INCIDENT", "code": "TABLE_INCOMPLETE_OR_UNASSOCIATED", "table": name})
        elif rows and not native and not structure.get(name):
            reasons.append({"kind": "RISK_POLICY", "code": "INDEPENDENT_ROW_STRUCTURE_REQUIRED", "table": name})
    if payload.get("structural_issues") or payload.get("semantic_conflicts"):
        reasons.append({"kind": "INCIDENT", "code": "STRUCTURE_OR_SEMANTIC_CONFLICT"})
    if any(item.get("status") != "VALID" for key in ("arithmetic", "cross_totals") for item in payload.get(key, [])):
        reasons.append({"kind": "INCIDENT", "code": "DECLARED_ARITHMETIC_CONSISTENCY"})
    return {"version": SECONDARY_VERSION, "required": bool(reasons), "reasons": reasons,
        "scope": {"kind": "WHOLE_DOCUMENT", "pages": list(payload.get("pages", [])), "focal_reread": False},
        "decision_sha256": canonical_hash(reasons)}
