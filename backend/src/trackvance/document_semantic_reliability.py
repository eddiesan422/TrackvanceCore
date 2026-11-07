"""Current deterministic reliability projection. The validator remains the approval authority."""
from __future__ import annotations

import copy
from typing import Any

from .document_normalization import canonical_hash
from .document_validation_policy import cells

VERSION = "SEMANTIC_RELIABILITY_V1"
POLICY = {"version": VERSION, "decision_authority": "CURRENT_SEMANTIC_QUALITY_REPORT",
    "calibration": "PENDING", "cell_binding": "CURRENT_CELL_HASH",
    "row_binding": "CURRENT_TABLE_HASH_AND_SOURCE_OCCURRENCE",
    "coverage_authority": "CURRENT_SOURCE_PROOF_OR_AUTHORIZED_TABLE_REVIEW",
    "association": "CURRENT_PROVEN_LABEL_RELATION_OR_COLUMN_OCCURRENCE",
    "alternatives": "UNIQUE_EXACT_RETAINED_RECORDS", "applicability": "DECLARED_REQUIREDNESS_AND_CURRENT_OBSERVATION"}
POLICY_SHA256 = canonical_hash(POLICY)
AUTO = "VERIFIED_AUTOMATICALLY"
REVIEWED = "VERIFIED_BY_AUTHORIZED_REVIEW"
CORRECTED = "AUTHORIZED_CORRECTION"
PENDING = "PENDING"


def _reasons(issues: list[str], location: str) -> list[str]:
    return [issue for issue in issues if issue.partition(":")[2] == location
        or issue.partition(":")[2].startswith(location + ".")]


def _association_basis(observation: dict[str, Any], field: dict[str, Any]) -> list[str]:
    associations = observation.get("association", [])
    if not isinstance(associations, list) or observation.get("status") != "VERIFIED":
        return []
    return sorted({str(item["basis"]) for item in associations if isinstance(item, dict)
        and isinstance(item.get("basis"), str) and item["basis"]
        and (isinstance(item.get("label"), str) and item["label"] and isinstance(item.get("relation"), str) and item["relation"]
            or item.get("column_role") == field["name"] and item.get("source_occurrence")
            and item.get("scope") in {"EVIDENCE_ONLY", "PRINTED_COLUMN", "OPTIONAL_COLUMN_ABSENT_COMPLETE_HEADER"})})


def reliability_report(extraction: Any, definition: dict[str, Any], automatic: dict[str, Any],
                       quality: dict[str, Any], source_checks: dict[str, Any],
                       table_reviews: list[dict[str, Any]]) -> dict[str, Any]:
    """Describe the current revision without creating or modifying source evidence."""
    proof, payload = automatic["proof"], extraction.payload
    issues = quality["issues"]
    values = []
    by_path = {}
    for path, cell, field in cells(payload, definition):
        valid = isinstance(cell, dict) and cell.get("validity") == "VALID"
        reviewed = bool(valid and extraction.reviewed and cell.get("reviewed") is True)
        status = PENDING
        if reviewed:
            status = CORRECTED if cell.get("corrected") else REVIEWED
        elif valid and path in automatic["verified_cells"]:
            status = AUTO
        observation = proof.get("corroboration", {}).get(path, {})
        current_source = bool(valid and path in automatic["verified_cells"])
        associations = _association_basis(observation, field) if current_source and observation.get("source_refs") else []
        alternatives = [*cell.get("alternatives", []), *cell.get("candidate_alternatives", [])] if isinstance(cell, dict) else []
        unobserved = isinstance(cell, dict) and cell.get("observed") is None and cell.get("value") is None
        reasons = _reasons(issues, path)
        value = {"path": path, "status": status, "required": field.get("required", True),
            "critical": bool(field.get("critical")), "logical_type": field["logical_type"],
            "applicability": "REQUIRED" if field.get("required", True) else "OPTIONAL_NOT_OBSERVED" if unobserved else "OPTIONAL_PRESENT",
            "normalization": cell.get("validity", "MISSING") if isinstance(cell, dict) else "MISSING",
            "source_verified": current_source, "association_verified": bool(associations), "association_basis": associations,
            "alternatives_retained": len({canonical_hash(item) for item in alternatives}),
            "review_required": status == PENDING and bool(reasons), "reasons": reasons,
            "source_refs": copy.deepcopy(observation.get("source_refs", [])) if current_source else []}
        values.append(value)
        by_path[path] = value
    tables = []
    for table in definition["tables"]:
        name = table["name"]
        rows = payload["tables"].get(name, [])
        normalized_rows = [{key: {k: v for k, v in cell.items() if k != "reviewed"}
            for key, cell in row.items()} for row in rows]
        current_table = bool(proof and proof.get("table_hashes", {}).get(name) == canonical_hash(normalized_rows))
        review = next(item for item in table_reviews if item["name"] == name)
        coverage = review["coverage_state"]
        alignments = proof.get("coverage_details", {}).get(name, {}).get("occurrence_alignment", [])
        row_results = []
        for index, row in enumerate(rows):
            paths = [f"{name}.{index}.{field['name']}" for field in table["fields"]]
            states = [by_path[path]["status"] for path in paths]
            sources = [item for item in alignments if current_table and item.get("output_occurrence") == index
                and item.get("source_occurrence") and set(row) <= set(item.get("verified_fields", []))]
            status = PENDING
            if states and all(state != PENDING for state in states) and sources:
                status = AUTO if all(state == AUTO for state in states) else REVIEWED
            elif states and all(state != PENDING for state in states) and coverage == "AUTHORIZED_REVIEW":
                status = REVIEWED
            reasons = _reasons(issues, f"{name}.{index}")
            if status == PENDING and not reasons:
                reasons = ["ROW_ASSOCIATION_NOT_PROVEN"]
            row_results.append({"index": index, "status": status, "cell_paths": paths, "reasons": reasons,
                "source_occurrences": [{key: source[key] for key in ("source_occurrence", "source_page", "mechanism", "basis")
                    if key in source} for source in sources] if status != PENDING else []})
        tables.append({"name": name, "coverage_state": coverage, "current_table_binding": current_table,
            "row_count": len(rows), "verified_rows": sum(row["status"] == AUTO for row in row_results),
            "reviewed_rows": sum(row["status"] == REVIEWED for row in row_results),
            "pending_rows": sum(row["status"] == PENDING for row in row_results),
            "rows": row_results, "reasons": _reasons(issues, name)})
    business = quality.get("business_validation", {})
    arithmetic_declared = bool(business.get("required") or definition.get("arithmetic")
        or definition.get("cross_totals") or definition.get("invoice_controls"))
    business_status = "NOT_APPLICABLE"
    if arithmetic_declared:
        source_ready = (bool(business.get("checks")) and all(check.get("source_inputs_verified") is True
            and check.get("status") == "VALID" for check in business["checks"])) if business.get("required") else (
                quality["checks"]["scalar_and_evidence"] and quality["checks"]["row_coverage"])
        business_status = "VERIFIED" if quality["checks"]["arithmetic"] and source_ready else PENDING
    return {"version": VERSION, "policy_sha256": POLICY_SHA256, "calibration": {"status": "PENDING"},
        "binding": {"payload_sha256": canonical_hash(payload), "definition_sha256": canonical_hash(definition),
            "source_attestation_sha256": proof.get("attestation_sha256")},
        "cells": values, "tables": tables,
        "document": {"status": quality["status"], "reasons": list(issues),
            "required_cells": sum(value["required"] for value in values),
            "critical_cells": sum(value["critical"] for value in values),
            "verified_cells": sum(value["status"] == AUTO for value in values),
            "reviewed_cells": sum(value["status"] in {REVIEWED, CORRECTED} for value in values),
            "pending_cells": sum(value["status"] == PENDING for value in values),
            "gates": {**copy.deepcopy(source_checks), "business_rules": {"status": business_status,
                "reasons": [issue for issue in issues if issue.startswith(("QUALITY_BUSINESS_", "QUALITY_ARITHMETIC"))],
                "checks": [{key: check[key] for key in ("name", "status", "source_inputs_verified") if key in check}
                    for check in business.get("checks", [])]}}}}
