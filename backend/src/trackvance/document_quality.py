"""Independent Trackvance validation. Model output is always only a candidate.

An automatic approval requires lexical evidence, exact scalar/Decimal checks,
declared row coverage and agreement with a separately executed confined legacy
extraction of the same original. Confidence is never an approval criterion.
"""
from __future__ import annotations

import hashlib
import json
import math

from .document_engine import arithmetic_checks, cross_table_checks, validate_value
from .document_inference_completion import completion_issues, unresolved_completion
from .document_invoice_controls import invoice_arithmetic_fields
from .document_schemas import structures
from .document_semantic_fragments import canonical_observed, multiline_fields
from .operations_common import OperationError

VALIDATOR_VERSION = "TRACKVANCE_IDP_VALIDATOR_098_V1"


def _hash(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
        separators=(",", ":"), allow_nan=False).encode()).hexdigest()


def _region(region):
    return (isinstance(region, list) and len(region) == 4
        and all(type(value) in (int, float) and math.isfinite(value) and 0 <= value <= 1 for value in region)
        and region[0] <= region[2] and region[1] <= region[3])


def _cells(payload, definition):
    for name, fields in structures(definition).items():
        rows = [payload["fields"]] if name == "headers" else payload["tables"].get(name, [])
        for index, row in enumerate(rows):
            for field in fields:
                yield f"{name}.{index}.{field['name']}", row.get(field["name"]), field


def cell_hash(cell):
    # Human confirmations alter review state, never the evidence/value proof.
    return _hash({key: value for key, value in cell.items() if key != "reviewed"})


def validate_candidate_contract(data, definition, *, allow_worker_completion=False):
    """Reject malformed or self-reviewed provider output before durable storage."""
    try:
        if (not isinstance(data, dict) or set(data) != {"payload", "engine"}
                or not isinstance(data["engine"], dict)
                or not allow_worker_completion and {"inference_completion", "inference_completion_sha256"} & data["engine"].keys()):
            raise ValueError()
        if len(json.dumps(data, allow_nan=False).encode()) > 16 * 1024**2:
            raise ValueError()
        payload = data["payload"]
        if (not isinstance(payload, dict) or not isinstance(payload.get("fields"), dict)
                or not isinstance(payload.get("tables"), dict) or "header_resolutions" in payload
                or "inference_completion_resolution" in payload):
            raise ValueError()
        if ("semantic_conflicts" in payload and (not isinstance(payload["semantic_conflicts"], list)
                or any(not isinstance(value, str) or len(value) > 160 for value in payload["semantic_conflicts"]))):
            raise ValueError()
        scopes = structures(definition)
        table_names = set(scopes) - {"headers"}
        if (set(payload["fields"]) != {field["name"] for field in definition["fields"]}
                or set(payload["tables"]) != table_names
                or not isinstance(payload.get("table_unparsed"), dict)
                or set(payload["table_unparsed"]) != table_names):
            raise ValueError()
        for name in table_names:
            rows = payload["tables"][name]
            if (not isinstance(rows, list) or len(rows) > 10000
                    or any(not isinstance(row, dict) or set(row) != {field["name"] for field in scopes[name]} for row in rows)
                    or not isinstance(payload["table_unparsed"][name], list)):
                raise ValueError()
        lines = payload.get("lines")
        pages = payload.get("pages")
        if (not isinstance(lines, list) or len(lines) > 100000 or not isinstance(pages, list)
                or any(type(page) is not int or not 1 <= page <= 50 for page in pages)
                or pages != sorted(set(pages))):
            raise ValueError()
        text_bytes = 0
        for line in lines:
            if (not isinstance(line, dict) or not isinstance(line.get("text"), str)
                    or type(line.get("page")) is not int or line["page"] not in pages
                    or not _region(line.get("region")) or not isinstance(line.get("method"), str)):
                raise ValueError()
            text_bytes += len(line["text"].encode())
        if text_bytes > 2_000_000:
            raise ValueError()
        for _, cell, _ in _cells(payload, definition):
            if (not isinstance(cell, dict)
                    or not {"value", "observed", "validity", "origin", "confidence", "reviewed"}.issubset(cell)
                    or cell["reviewed"] is not False or cell.get("corrected")
                    or cell["validity"] not in {"VALID", "INVALID", "MISSING", "AMBIGUOUS"}
                    or any(cell[key] is not None and (not isinstance(cell[key], str) or len(cell[key]) > 65536) for key in ("value", "observed"))):
                raise ValueError()
            origin = cell["origin"]
            if origin is not None and (not isinstance(origin, dict) or origin.get("page") not in pages
                    or not _region(origin.get("region")) or not isinstance(origin.get("method"), str)):
                raise ValueError()
            confidence = cell["confidence"]
            if confidence is not None and (type(confidence) not in (int, float)
                    or not math.isfinite(confidence) or not 0 <= confidence <= 100):
                raise ValueError()
    except (KeyError, TypeError, ValueError, OverflowError):
        raise OperationError(422, "DOCUMENT_PROVIDER_CONTRACT", "El proveedor devolvió candidatos fuera del contrato documental.") from None


def _contained(inner, outer, tolerance=.003):
    return (inner[0] >= outer[0] - tolerance and inner[1] >= outer[1] - tolerance
        and inner[2] <= outer[2] + tolerance and inner[3] <= outer[3] + tolerance)


def _observed_in_source(cell, lines, field):
    observed, origin = cell.get("observed"), cell.get("origin")
    if observed is None:
        return cell.get("value") is None and not field.get("required", True)
    if not origin or not _region(origin.get("region")):
        return False
    origins = [origin, *origin.get("continuation_origins", [])]
    words: list[str] = []
    for line in lines:
        for area in origins:
            if not isinstance(area, dict) or line.get("page") != area.get("page") or not _region(area.get("region")):
                continue
            if line.get("words"):
                words.extend(word["text"] for word in line["words"] if isinstance(word, dict)
                    and isinstance(word.get("text"), str) and _region(word.get("region"))
                    and _contained(word["region"], area["region"]))
            elif _contained(line["region"], area["region"]) or _contained(area["region"], line["region"]):
                words.append(line["text"])
    normalized = " ".join(str(observed).split())
    return bool(normalized and normalized in " ".join(" ".join(words).split()))


def cell_is_valid(cell, field, *, allow_multiline=False):
    raw = cell.get("observed")
    if cell.get("reviewed"):
        raw = cell.get("value")
        # Human revisions store canonical ISO dates/decimal points. Their original
        # formatting was checked at review time; recheck the canonical form here.
        field = {**field, "date_format": "%Y-%m-%d", "decimal_separator": ".",
            "thousands_separator": None, "currency_symbols": []}
    else:
        try:
            raw = canonical_observed(cell, allow_multiline=allow_multiline and field["logical_type"] == "STRING")
        except (ValueError, TypeError, KeyError):
            return False
    value, status = validate_value(raw, field)
    return status == "VALID" and cell.get("validity") == "VALID" and value == cell.get("value")


def _arithmetic_fields(definition):
    covered: set[tuple[str, str]] = set()
    for rule in definition.get("arithmetic", []):
        if rule["result"] in [*rule["terms"], *rule.get("subtract", [])]:
            # A tautological/model-suggested self check cannot establish an
            # independent arithmetic control for automatic approval.
            continue
        scope = rule.get("table") or "headers"
        covered.update((scope, name) for name in [rule["result"], *rule["terms"], *rule.get("subtract", [])])
    for rule in definition.get("cross_totals", []):
        covered.update(((rule["table"], rule["column"]), ("headers", rule["header"])))
    covered.update(invoice_arithmetic_fields(definition))
    return covered


def semantic_issues(payload, *, reviewed=False):
    result = []
    for conflict in payload.get("semantic_conflicts", []):
        kind, _, name = conflict.partition(":")
        if kind in {"FIELD", "REEXTRACTION_FIELD"}:
            resolved = reviewed and payload.get("fields", {}).get(name, {}).get("reviewed")
        elif kind in {"TABLE", "REEXTRACTION_TABLE"}:
            rows = payload.get("tables", {}).get(name, [])
            resolved = reviewed and bool(rows) and all(cell.get("reviewed") for row in rows for cell in row.values())
        else:
            resolved = False
        if not resolved:
            result.append("SEMANTIC_CONFLICT:" + conflict)
    return result


def certify_candidate(data, independent, definition, source_sha256):
    """Worker-only attestation. Never accept this object from an OCR/LLM response."""
    payload, control = data["payload"], independent["payload"]
    validate_candidate_contract(independent, definition)
    independent_cells = {path: cell for path, cell, _ in _cells(control, definition)}
    arithmetic_fields = _arithmetic_fields(definition)
    multiline = multiline_fields(definition)
    verified, findings = {}, []
    for path, cell, field in _cells(payload, definition):
        other = independent_cells.get(path)
        allow_multiline = (path.split(".")[0], field["name"]) in multiline
        if not cell_is_valid(cell, field, allow_multiline=allow_multiline):
            findings.append("QUALITY_VALUE:" + path)
        elif field["logical_type"] == "DECIMAL" and (path.split(".")[0], field["name"]) not in arithmetic_fields:
            findings.append("QUALITY_ARITHMETIC_UNDECLARED:" + path)
        elif not _observed_in_source(cell, payload["lines"], field):
            findings.append("QUALITY_EVIDENCE:" + path)
        elif not other or not cell_is_valid(other, field, allow_multiline=allow_multiline) or other["value"] != cell["value"]:
            findings.append("QUALITY_INDEPENDENT_DISAGREEMENT:" + path)
        elif not _observed_in_source(other, control["lines"], field):
            findings.append("QUALITY_INDEPENDENT_EVIDENCE:" + path)
        else:
            verified[path] = cell_hash(cell)
    coverage = {}
    for table in definition["tables"]:
        name = table["name"]
        visual = table.get("visual") or {}
        # Regex-only rows cannot prove that an unmatched visual row was retained.
        # The declared grammar parser preserves every unparsed body observation.
        coverage[name] = bool(visual.get("row_start") and visual.get("flowing")
            and len(payload["tables"][name]) == len(control["tables"][name])
            and not payload["table_unparsed"][name] and not control["table_unparsed"][name])
        if (any(field["logical_type"] == "DECIMAL" for field in table["fields"])
                and any(field["logical_type"] == "DECIMAL" for field in definition["fields"])
                and not any(rule["table"] == name for rule in definition.get("cross_totals", []))):
            coverage[name] = False
        if data["engine"].get("independent_row_coverage") is False and data["engine"].get("route") != "DIGITAL_NATIVE":
            coverage[name] = False
        if not coverage[name]:
            findings.append("QUALITY_ROW_COVERAGE:" + name)
    checks = [*arithmetic_checks(payload, definition), *cross_table_checks(payload, definition)]
    arithmetic = all(check["status"] == "VALID" for check in checks)
    if not arithmetic:
        findings.append("QUALITY_ARITHMETIC")
    proof = {"validator_version": VALIDATOR_VERSION, "source_sha256": source_sha256,
        "definition_sha256": _hash(definition), "candidate_sha256": _hash(payload),
        "independent_payload_sha256": _hash(control), "independent_engine": independent["engine"].get("key"),
        "verified_cells": verified, "table_coverage": coverage, "arithmetic": arithmetic,
        "issues": findings, "independent_extraction": True}
    proof["attestation_sha256"] = _hash(proof)
    return proof


def quality_report(extraction, definition, *, ignore_incomplete=False):
    if extraction is None:
        return {"validator_version": VALIDATOR_VERSION, "status": "REVIEW_REQUIRED", "issues": ["EXTRACTION_PENDING"], "checks": {}}
    payload = extraction.payload
    proof = (getattr(extraction, "engine", None) or {}).get("validation") or {}
    signed = {key: value for key, value in proof.items() if key != "attestation_sha256"}
    trusted = (proof.get("validator_version") == VALIDATOR_VERSION
        and proof.get("definition_sha256") == _hash(definition)
        and proof.get("attestation_sha256") == _hash(signed)
        and proof.get("independent_extraction") is True
        and (getattr(extraction, "reviewed", False) or proof.get("candidate_sha256") == _hash(payload)))
    findings = []
    if not ignore_incomplete:
        findings.extend(completion_issues(extraction, definition))
    multiline = multiline_fields(definition)
    findings.extend(semantic_issues(payload, reviewed=getattr(extraction, "reviewed", False)))
    cell_count = verified_count = 0
    for path, cell, field in _cells(payload, definition):
        cell_count += 1
        if not cell or not cell_is_valid(cell, field, allow_multiline=(path.split(".")[0], field["name"]) in multiline):
            findings.append("QUALITY_VALUE:" + path)
            continue
        if cell.get("reviewed") and getattr(extraction, "reviewed", False) or trusted and proof.get("verified_cells", {}).get(path) == cell_hash(cell):
            verified_count += 1
        else:
            findings.append("REVIEW_REQUIRED:" + path)
    coverage = True
    duplicates = True
    for table in definition["tables"]:
        name = table["name"]
        rows = payload["tables"].get(name, [])
        human_table = bool(rows) and getattr(extraction, "reviewed", False) and all(cell.get("reviewed") for row in rows for cell in row.values())
        if not human_table and not (trusted and proof.get("table_coverage", {}).get(name)):
            coverage = False
            findings.append("QUALITY_ROW_COVERAGE:" + name)
        keys = [field["name"] for field in table["fields"] if field.get("business_key")]
        if keys:
            values = [tuple(row.get(key, {}).get("value") for key in keys) for row in rows]
            if len(set(values)) != len(values):
                duplicates = False
                findings.append("QUALITY_DUPLICATE_KEY:" + name)
        elif not human_table:
            start = (table.get("visual") or {}).get("row_start")
            columns = [table["fields"][start["column"]]["name"]] if start else [field["name"] for field in table["fields"]]
            values = [tuple(row.get(key, {}).get("value") for key in columns) for row in rows]
            if len(set(values)) != len(values):
                duplicates = False
                findings.append("QUALITY_DUPLICATE_ROW:" + name)
    checks = [*arithmetic_checks(payload, definition), *cross_table_checks(payload, definition)]
    arithmetic = all(check["status"] == "VALID" for check in checks)
    for check in checks:
        if check["status"] != "VALID":
            findings.append(f"ARITHMETIC_{check['status']}:{check['name']}:{check.get('row', 'ALL')}")
    status = "REVIEW_REQUIRED" if findings or not ignore_incomplete and unresolved_completion(extraction) else "HUMAN_VERIFIED" if getattr(extraction, "reviewed", False) else "AUTO_VERIFIED"
    return {"validator_version": VALIDATOR_VERSION, "status": status, "issues": list(dict.fromkeys(findings)),
        "checks": {"independent_extraction": bool(trusted), "scalar_and_evidence": verified_count == cell_count,
            "arithmetic": arithmetic, "row_coverage": coverage, "duplicate_keys": duplicates},
        "cells": {"total": cell_count, "verified": verified_count, "pending": cell_count - verified_count}}
