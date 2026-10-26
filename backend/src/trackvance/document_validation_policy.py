"""Worker-authored, source-bound evidence policy for opt-in documentary routes.

No provider confidence/coverage flag or repeated LLM answer is an attestation.
Historical definitions keep their existing independent validation policy.
"""
from __future__ import annotations

from decimal import Decimal
from typing import Any

from .document_candidate_selection import mechanism_family
from .document_engine import arithmetic_checks, cross_table_checks
from .document_normalization import canonical_hash, normalization_matches, normalize_observation
from .document_representation import validate_representation

VALIDATOR_VERSION = "TRACKVANCE_DOCUMENT_EVIDENCE_098_V2"


def cells(payload, definition):
    for field in definition["fields"]:
        yield "headers.0." + field["name"], payload["fields"].get(field["name"]), field
    for table in definition["tables"]:
        for index, row in enumerate(payload["tables"].get(table["name"], [])):
            for field in table["fields"]:
                yield f"{table['name']}.{index}.{field['name']}", row.get(field["name"]), field


def cell_hash(cell):
    return canonical_hash({key: value for key, value in cell.items() if key != "reviewed"})


def _normalized(cell, field):
    if not isinstance(cell, dict) or cell.get("validity") != "VALID":
        return None
    value, validity, _ = normalize_observation(cell.get("observed"), field)
    if validity != "VALID":
        return None
    if field["logical_type"] == "DECIMAL" and value is not None:
        return Decimal(value)
    if field["logical_type"] == "INT64" and value is not None:
        return int(value)
    return value


def _independent_candidates(data, independent):
    result = []
    if isinstance(independent, dict) and isinstance(independent.get("payload"), dict):
        result.append({**independent["payload"], "_mechanism": "LEGACY_INDEPENDENT", "_stage": "LEGACY_INDEPENDENT",
            "_completed_pages": independent["payload"].get("pages", data["engine"].get("execution", {}).get("completed_pages", []))})
    for candidate in data["payload"].get("candidate_history", []):
        family = mechanism_family(candidate.get("stage"))
        if family is not None and not candidate.get("structural_issues"):
            from .document_lossless import _execution

            execution = _execution(data["payload"]["document_representation"], candidate["stage"])
            if execution["recognition_complete"] and execution["resources_released"]:
                result.append({"fields": candidate["fields"], "tables": candidate["tables"],
                    "table_unparsed": candidate.get("table_unparsed", {}),
                    "_mechanism": family, "_stage": candidate["stage"], "_completed_pages": execution["completed_pages"]})
    return result


def _row_vector(row, fields):
    page = next(((cell.get("origin") or {}).get("page") for cell in row.values() if isinstance(cell, dict)), None)
    return (page, *(_normalized(row.get(field["name"]), field) for field in fields))


def align_rows(rows, control_rows, fields):
    """Conservative one-to-one source occurrences, with no SKU/ordinal shortcut."""
    left = [_row_vector(row, fields) for row in rows]
    right = [_row_vector(row, fields) for row in control_rows]
    matches, bases = {}, {}
    # Indistinguishable complete rows are matched by occurrence only when both
    # readings retain exactly the same multiplicity on the same source page.
    for vector in set(left):
        if vector[0] is None or sum(value is not None and value != "" for value in vector[1:]) < 2:
            continue
        a = [i for i, value in enumerate(left) if value == vector]
        b = [i for i, value in enumerate(right) if value == vector]
        if len(a) == len(b):
            for first, second in zip(a, b, strict=True):
                matches[first], bases[first] = second, "FULL_ROW_OCCURRENCE"
    anchors = [index + 1 for index, field in enumerate(fields) if not field.get("critical")]
    remaining_left = set(range(len(rows))) - set(matches)
    remaining_right = set(range(len(control_rows))) - set(matches.values())
    identity = [index + 1 for index, field in enumerate(fields) if field.get("business_key")]

    def score(first, second):
        if first[0] is None or first[0] != second[0] or any(first[column] is not None
                and second[column] is not None and first[column] != second[column] for column in identity):
            return 0
        return sum(first[column] is not None and first[column] != "" and first[column] == second[column]
            for column in anchors)

    def unique_best(first, population):
        best, target, tied = 1, None, False
        for index, second in enumerate(population):
            support = score(first, second)
            if support > best:
                best, target, tied = support, index, False
            elif support == best:
                tied = True
        return None if tied else target

    # Global uniqueness is measured against immutable original populations.
    # Consuming a complete match must not turn two weak EA/quantity matches into
    # a supposedly unique identity. Nor may a consumed stronger match be ignored.
    forward = {index: unique_best(left[index], right) for index in remaining_left}
    reverse = {index: unique_best(right[index], left) for index in remaining_right}
    for i, j in forward.items():
        if j in remaining_right and reverse.get(j) == i:
            matches[i], bases[i] = j, "UNIQUE_MULTI_COLUMN_SOURCE_PAGE"
    return matches, bases


def _selected_mechanism(cell, primary_stage):
    return (cell.get("candidate_selection") or {}).get("selected_mechanism") or mechanism_family(primary_stage)


def _different_source(cell, control, primary_stage):
    selected = _selected_mechanism(cell, primary_stage)
    return bool(selected and control.get("_mechanism") and selected != control["_mechanism"])


def _control_located(cell, control, field):
    if not isinstance(cell, dict) or (cell.get("origin") or {}).get("page") not in control.get("_completed_pages", []):
        return False
    if cell.get("evidence_refs"):
        return True
    if control.get("_mechanism") == "LEGACY_INDEPENDENT":
        from .document_quality import _observed_in_source

        return _observed_in_source(cell, control.get("lines", []), field)
    return False


def _corroborated(payload, definition, controls, *, primary_stage=None):
    paths, structural = set(), {}
    provenance: dict[str, list[dict[str, Any]]] = {}
    for field in definition["fields"]:
        value = _normalized(payload["fields"].get(field["name"]), field)
        if value is not None and any(_different_source(payload["fields"][field["name"]], other, primary_stage)
                and _control_located(other.get("fields", {}).get(field["name"]), other, field)
                and _normalized(other.get("fields", {}).get(field["name"]), field) == value for other in controls):
            paths.add("headers.0." + field["name"])
    for table in definition["tables"]:
        name, fields = table["name"], table["fields"]
        rows = payload["tables"].get(name, [])
        structural[name] = False
        for other in controls:
            if other.get("table_unparsed", {}).get(name):
                continue
            control_rows = other.get("tables", {}).get(name, [])
            matches, bases = align_rows(rows, control_rows, fields)
            independent_identity = all(sum(_different_source(rows[i][field["name"]], other, primary_stage)
                and _control_located(control_rows[j].get(field["name"]), other, field)
                and _normalized(rows[i].get(field["name"]), field) is not None
                and _normalized(rows[i].get(field["name"]), field) == _normalized(control_rows[j].get(field["name"]), field)
                for field in fields) >= 2 for i, j in matches.items())
            if rows and len(rows) == len(control_rows) == len(matches) and independent_identity:
                structural[name] = True
            for index, target in sorted(matches.items()):
                if sum(_different_source(rows[index][field["name"]], other, primary_stage)
                        and _control_located(control_rows[target].get(field["name"]), other, field)
                        and _normalized(rows[index].get(field["name"]), field) is not None
                        and _normalized(rows[index].get(field["name"]), field)
                            == _normalized(control_rows[target].get(field["name"]), field) for field in fields) >= 2:
                    provenance.setdefault(name, []).append({"source_occurrence": index,
                        "control_occurrence": target, "basis": bases[index], "control_stage": other.get("_stage")})
                for field in fields:
                    value = _normalized(rows[index].get(field["name"]), field)
                    if (value is not None and _different_source(rows[index][field["name"]], other, primary_stage)
                            and _control_located(control_rows[target].get(field["name"]), other, field)
                            and value == _normalized(control_rows[target].get(field["name"]), field)):
                        paths.add(f"{name}.{index}.{field['name']}")
    return paths, structural, provenance


def certify_lossless_candidate(data, definition, source_sha256, *, source_context, independent=None,
                               check=lambda: None):
    """Coordinator-only attestation after immutable source/event persistence."""
    check()
    payload, engine = data["payload"], data["engine"]
    representation = validate_representation(payload["document_representation"])
    policy = definition.get("validation_policy") or {}
    execution = engine.get("execution", {})
    source = dict(source_context or {})
    hashes = [value for value in engine.get("recognition_event_hashes", []) if isinstance(value, str)]
    durable_hashes = set(source.get("persisted_event_hashes", []))
    expected_artifacts = {(artifact["kind"], artifact["sha256"], page["stage"], page["page_one_based"])
        for page in representation["pages"] for artifact in page["artifact_refs"]}
    expected_artifacts.update((artifact["kind"], artifact["sha256"], "NATIVE_SOURCE_PROBE", None)
        for artifact in (representation.get("source_probe") or {}).get("artifact_refs", []))
    durable_artifacts = {(artifact.get("kind"), artifact.get("sha256"), artifact.get("stage"),
        artifact.get("page", artifact.get("page_one_based"))) for artifact in source.get("artifacts", [])}
    source_bound = bool(source.get("source_sha256") == source_sha256 == engine.get("source_sha256")
        == representation.get("source_sha256") and hashes and set(hashes) <= durable_hashes
        and expected_artifacts and expected_artifacts <= durable_artifacts
        and source.get("expected_pages") == representation.get("expected_pages"))
    complete = bool(source_bound and execution.get("recognition_complete") is True
        and execution.get("resources_released") is True and source.get("resources_released") is True
        and not execution.get("pending_pages"))
    controls = _independent_candidates(data, independent)
    corroborated, structure, row_alignment = _corroborated(payload, definition, controls, primary_stage=engine.get("route"))
    verified, findings, coverage = {}, [], {}
    if policy.get("version") != "EVIDENCE_V2":
        findings.append("QUALITY_POLICY_REQUIRED")
    if not source_bound:
        findings.append("QUALITY_DURABLE_SOURCE_EVIDENCE")
    if not complete:
        findings.append("RECOGNITION_INCOMPLETE_OR_RESOURCES_UNVERIFIED")
    emitted = set(execution.get("emitted_pages", []))
    native = engine.get("route") == "DIGITAL_NATIVE"
    from .document_quality import _arithmetic_fields
    arithmetic_fields = _arithmetic_fields(definition)
    for path, cell, field in cells(payload, definition):
        check()
        if not isinstance(cell, dict) or cell.get("validity") != "VALID" or not normalization_matches(cell, field):
            findings.append("QUALITY_VALUE:" + path)
            continue
        if cell.get("value") is None and not field.get("required", True):
            verified[path] = cell_hash(cell)
            continue
        origin = cell.get("origin") or {}
        located = bool(cell.get("evidence_refs") and origin.get("page") in emitted)
        if not located:
            findings.append("QUALITY_SOURCE_LOCATION:" + path)
            continue
        risk = field.get("critical") or field["logical_type"] in {"DECIMAL", "INT64"}
        if field["logical_type"] == "DECIMAL" and (path.split(".")[0], field["name"]) not in arithmetic_fields:
            findings.append("QUALITY_ARITHMETIC_UNDECLARED:" + path)
            continue
        independent_value = path in corroborated
        measured_native = native and cell.get("geometry_kind") == "MEASURED_SOURCE"
        allowed_standard = bool(not risk and policy.get("allow_noncritical_source_bound") is True)
        if complete and (independent_value or measured_native or allowed_standard):
            verified[path] = cell_hash(cell)
        else:
            findings.append("QUALITY_INDEPENDENT_SOURCE_REQUIRED:" + path)
    for table in definition["tables"]:
        name = table["name"]
        rows = payload["tables"].get(name, [])
        occurrence_ids = [value.get("occurrence_id") for value in payload.get("row_provenance", {}).get(name, [])]
        occurrences = bool(len(occurrence_ids) == len(rows) and len(set(occurrence_ids)) == len(occurrence_ids)
            and all(occurrence_ids))
        if native:
            occurrences = bool(rows)  # The native parser retains individual source occurrences.
        optional_absent = not rows and not table.get("required", True) and not payload.get("table_unparsed", {}).get(name)
        coverage[name] = bool(complete and not payload.get("structural_issues") and (optional_absent or (rows
            and not payload.get("table_unparsed", {}).get(name) and occurrences
            and (native or structure.get(name)))))
        if not coverage[name]:
            findings.append("QUALITY_ROW_COVERAGE:" + name)
    arithmetic_checks_actual = [*arithmetic_checks(payload, definition), *cross_table_checks(payload, definition)]
    arithmetic = all(item["status"] == "VALID" for item in arithmetic_checks_actual)
    if not arithmetic:
        findings.append("QUALITY_ARITHMETIC")
    proof = {"validator_version": VALIDATOR_VERSION, "source_sha256": source_sha256,
        "definition_sha256": canonical_hash(definition), "candidate_sha256": canonical_hash(payload),
        "source_context_sha256": canonical_hash(source), "durable_source_bound": source_bound,
        "recognition_complete": complete, "verified_cells": verified, "table_coverage": coverage,
        "arithmetic": arithmetic, "issues": findings, "independent_extraction": native or any(
            control.get("_mechanism") != mechanism_family(engine.get("route")) for control in controls
            if mechanism_family(engine.get("route")) is not None),
        "risk_policy": policy, "source_context": source, "row_alignment": row_alignment}
    proof["attestation_sha256"] = canonical_hash(proof)
    check()
    return proof


def lossless_quality_report(extraction, definition, *, ignore_incomplete=False):
    payload, engine = extraction.payload, extraction.engine
    proof = engine.get("validation") or {}
    signed = {key: value for key, value in proof.items() if key != "attestation_sha256"}
    trusted = bool(proof.get("validator_version") == VALIDATOR_VERSION
        and proof.get("definition_sha256") == canonical_hash(definition)
        and proof.get("attestation_sha256") == canonical_hash(signed)
        and proof.get("durable_source_bound") is True
        and proof.get("risk_policy", {}).get("version") == "EVIDENCE_V2"
        and (extraction.reviewed or proof.get("candidate_sha256") == canonical_hash(payload)))
    findings, total, verified = [], 0, 0
    if not extraction.reviewed and (definition.get("validation_policy") or {}).get("version") != "EVIDENCE_V2":
        findings.append("QUALITY_POLICY_REQUIRED")
    if payload.get("structural_issues") and not extraction.reviewed:
        findings.append("QUALITY_DOCUMENT_STRUCTURE")
    execution = engine.get("execution", {})
    if not execution.get("recognition_complete") or not execution.get("resources_released"):
        findings.append("RECOGNITION_INCOMPLETE_OR_RESOURCES_UNVERIFIED")
    for path, cell, field in cells(payload, definition):
        total += 1
        if not isinstance(cell, dict) or cell.get("validity") != "VALID":
            findings.append("QUALITY_VALUE:" + path)
        elif extraction.reviewed and cell.get("reviewed") or trusted and proof.get("verified_cells", {}).get(path) == cell_hash(cell):
            verified += 1
        else:
            findings.append("REVIEW_REQUIRED:" + path)
    coverage = True
    for table in definition["tables"]:
        rows = payload["tables"].get(table["name"], [])
        human = bool(rows and extraction.reviewed and all(cell.get("reviewed") for row in rows for cell in row.values()))
        if not human and not (trusted and proof.get("table_coverage", {}).get(table["name"])):
            coverage = False
            findings.append("QUALITY_ROW_COVERAGE:" + table["name"])
    from .document_quality import semantic_issues
    findings.extend(semantic_issues(payload, reviewed=extraction.reviewed))
    checks = [*arithmetic_checks(payload, definition), *cross_table_checks(payload, definition)]
    arithmetic = all(item["status"] == "VALID" for item in checks)
    if not arithmetic:
        findings.append("QUALITY_ARITHMETIC")
    status = "REVIEW_REQUIRED" if findings else "HUMAN_VERIFIED" if extraction.reviewed else "AUTO_VERIFIED"
    return {"validator_version": VALIDATOR_VERSION, "status": status, "issues": list(dict.fromkeys(findings)),
        "checks": {"durable_source_bound": trusted, "recognition_complete": bool(execution.get("recognition_complete")),
            "scalar_and_evidence": verified == total, "arithmetic": arithmetic, "row_coverage": coverage,
            "independent_extraction": bool(trusted and proof.get("independent_extraction")), "duplicate_keys": True},
        "cells": {"total": total, "verified": verified, "pending": total - verified}}
