"""Frozen documentary diagnostic evaluator; no models, product writes or services.

Expected values are read ONLY here, after extraction. This module must never be
imported by a model prompt builder or candidate selector. All source values and
complete outputs remain private. No approval/publication is inferred from scores.
"""
from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from datetime import date
from decimal import Decimal, InvalidOperation
import hashlib
import json
from pathlib import Path
import re
import unicodedata

VERSION = "DOCUMENTARY_DIAGNOSTIC_EVALUATOR_20261101_V1"
STATUSES = {"COMPLETE", "PARTIAL", "TIMEOUT", "ERROR", "UNSUPPORTED", "NOT_RUN_DEPENDENCY", "NOT_RUN_BUDGET"}
POLICY = {
    "version": VERSION,
    "strings": "Unicode NFC, collapse whitespace, preserve case and punctuation; no fuzzy matching or paraphrase credit",
    "dates": "ISO YYYY-MM-DD or YYYY/MM/DD; DD/MM/YYYY only when day>12 or day=month. Ambiguous short day/month dates stay unresolved; no inferred document locale",
    "decimals": "Exact Decimal; no tolerance or inferred separator; grouping and decimal forms are explicitly enumerated below",
    "decimal_forms": ["1234.56", "1234,56", "1,234.56", "1.234,56", "1234", "-1234.56"],
    "ambiguous_numeric_form": "A single separator followed by exactly three digits is not interpreted as grouping unless the input is numeric JSON",
    "currency": {"COP": ["COP", "PESO COLOMBIANO", "PESOS COLOMBIANOS"], "USD": ["USD", "US DOLLAR", "US DOLLARS", "DOLAR ESTADOUNIDENSE", "DOLARES ESTADOUNIDENSES"], "EUR": ["EUR", "EURO", "EUROS"]},
    "unqualified_currency_symbol": "The bare dollar symbol is not an ISO currency equivalence",
    "text_metrics": "NFC and whitespace collapse, case preserved; full-document reference only when explicitly adequate; semantic fields are not a substitute for text",
    "text_scope_aliases": {"FULL_DOCUMENT_NATIVE_PDF_TEXT": "FULL_DOCUMENT", "FULL_DOCUMENT_VERIFIED_DIGITAL_TWIN_TEXT_INCLUDING_LABELS_AND_FOOTERS": "FULL_DOCUMENT"},
    "row_matching": "Global maximum identity-then-content assignment, with dummy unmatched rows. One-to-one occurrences; no deduplication and no ordinal-only identities. All-optimal-assignment metric bounds.",
    "row_eligibility": "At least one declared non-null identity component matches, or at least two other non-null components match; an all-wrong row is missing plus additional, not matched by ordinal",
    "legitimate_nulls": "Separate; correct nulls never increase precision or recall; non-null output for reference null is added without reference support",
    "unsupported_or_unrun": "Retained as rows, planned reference denominators recorded; no fabricated extraction score",
    "critical": "Reference critical_paths fixed before inference; all-optimal candidate possibilities must be correct for per-cell credit",
    "approval": "Only report a validator result actually executed; diagnostic scores grant no operational approval or publication",
    "attribution": "First boundary demonstrated by artifacts. Missing text or a matching raw substring alone cannot establish role, model error, or loss in integration.",
}


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")


def file_ref(path):
    data = Path(path).read_bytes()
    return {"path": str(Path(path).resolve()), "sha256": hashlib.sha256(data).hexdigest(), "bytes": len(data)}


def strict_json(raw):
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError("DUPLICATE_JSON_KEY:" + key)
            result[key] = value
        return result
    def constant(value):
        raise ValueError("NONFINITE_JSON_CONSTANT:" + value)
    return json.loads(raw, object_pairs_hook=pairs, parse_constant=constant)


def load(path):
    return strict_json(Path(path).read_bytes())


def string(value):
    return " ".join(unicodedata.normalize("NFC", str(value)).split())


def decimal(value):
    if isinstance(value, bool):
        raise ValueError("BOOLEAN_NOT_DECIMAL")
    if isinstance(value, (int, float, Decimal)):
        result = Decimal(str(value))
    else:
        text = string(value)
        if re.fullmatch(r"[+-]?\d+", text):
            result = Decimal(text)
        elif re.fullmatch(r"[+-]?\d{1,3}(,\d{3})+\.\d+", text):
            result = Decimal(text.replace(",", ""))
        elif re.fullmatch(r"[+-]?\d{1,3}(\.\d{3})+,\d+", text):
            result = Decimal(text.replace(".", "").replace(",", "."))
        elif re.fullmatch(r"[+-]?\d+[.,]\d+", text) and len(re.split(r"[.,]", text)[1]) != 3:
            result = Decimal(text.replace(",", "."))
        else:
            raise ValueError("UNDECLARED_OR_AMBIGUOUS_DECIMAL")
    if not result.is_finite():
        raise ValueError("NONFINITE_DECIMAL")
    return result


def normalize(value, field):
    if value is None or isinstance(value, str) and not value.strip():
        return None
    kind = field.get("type", "string")
    if isinstance(value, (dict, list)):
        return ("INVALID_COMPLEX", canonical(value).decode())
    try:
        if kind == "decimal":
            return decimal(value)
        if kind == "date":
            text = string(value)
            if re.fullmatch(r"\d{4}[-/]\d{2}[-/]\d{2}", text):
                return date.fromisoformat(text.replace("/", "-")).isoformat()
            if re.fullmatch(r"\d{2}/\d{2}/\d{4}", text):
                day, month, year = map(int, text.split("/"))
                if day <= 12 and month <= 12 and day != month:
                    return ("INVALID_DATE", text)
                return date(year, month, day).isoformat()
            return ("INVALID_DATE", text)
        result = string(value)
        if field["name"] == "currency":
            upper = result.upper()
            for iso, aliases in POLICY["currency"].items():
                if upper in aliases:
                    return iso
        return result
    except (ValueError, InvalidOperation):
        return ("INVALID_SCALAR", string(value))


def equal(expected, actual, field):
    return normalize(expected, field) == normalize(actual, field)


def outcome(expected, actual, field, *, present=True):
    wanted, observed = normalize(expected, field), normalize(actual, field)
    if wanted is None:
        return "LEGITIMATE_NULL" if observed is None else "ADDED_WITHOUT_REFERENCE_SUPPORT"
    if observed is None:
        return "MISSING"
    if isinstance(observed, tuple) and str(observed[0]).startswith("INVALID"):
        return "UNRESOLVED_NORMALIZATION"
    return "CORRECT" if wanted == observed else "INCORRECT"


def maximum_assignment(weights):
    """Hungarian assignment; indices only break indistinguishable ties."""
    n = len(weights)
    if not n:
        return 0, []
    costs = [[-value for value in row] for row in weights]
    u, v, p, way = [0] * (n + 1), [0] * (n + 1), [0] * (n + 1), [0] * (n + 1)
    for i in range(1, n + 1):
        p[0] = i
        j0 = 0
        minimum, used = [10**30] * (n + 1), [False] * (n + 1)
        while True:
            used[j0] = True
            i0, delta, j1 = p[j0], 10**30, 0
            for j in range(1, n + 1):
                if not used[j]:
                    current = costs[i0 - 1][j - 1] - u[i0] - v[j]
                    if current < minimum[j]:
                        minimum[j], way[j] = current, j0
                    if minimum[j] < delta:
                        delta, j1 = minimum[j], j
            for j in range(n + 1):
                if used[j]:
                    u[p[j]] += delta
                    v[j] -= delta
                else:
                    minimum[j] -= delta
            j0 = j1
            if not p[j0]:
                break
        while True:
            j1 = way[j0]
            p[j0] = p[j1]
            j0 = j1
            if not j0:
                break
    assignment = [-1] * n
    for j in range(1, n + 1):
        assignment[p[j] - 1] = j - 1
    return sum(weights[i][j] for i, j in enumerate(assignment)), assignment


def table_compare(expected, actual, columns, identity_fields, *, uncertainty_paths=frozenset(), table_name=""):
    m, n = len(expected), len(actual)
    size = m + n
    if size > 128:
        raise ValueError("DIAGNOSTIC_ROW_LIMIT_128_EXCEEDED")
    names = {field["name"] for field in columns}
    identities = set(identity_fields) & names
    weight = [[0] * size for _ in range(size)]
    eligible = set()
    metrics = [[0] * size for _ in range(size)]
    scale = (m + 1) * (len(columns) + 1)
    for i, wanted in enumerate(expected):
        for j, observed in enumerate(actual):
            matches = [field["name"] for field in columns if normalize(wanted.get(field["name"]), field) is not None
                       and equal(wanted.get(field["name"]), observed.get(field["name"]), field)]
            anchors = len(set(matches) & identities)
            if anchors or len(matches) >= 2:
                eligible.add((i, j))
                weight[i][j] = anchors * scale + len(matches)
                metrics[i][j] = len(matches)
            else:
                weight[i][j] = -(10**12)
    best, assignment = maximum_assignment(weight)
    possibilities = []
    for i in range(m):
        possible = []
        for j in range(n):
            if (i, j) not in eligible:
                continue
            forced = [row[:] for row in weight]
            for k in range(size):
                if k != j:
                    forced[i][k] = -(10**12)
                if k != i:
                    forced[k][j] = -(10**12)
            score, _ = maximum_assignment(forced)
            if score == best:
                possible.append(j)
        forced = [row[:] for row in weight]
        for j in range(n):
            forced[i][j] = -(10**12)
        score, _ = maximum_assignment(forced)
        if score == best:
            possible.append(None)
        possibilities.append(possible)
    def bounds(metric):
        factor = min(m, n) * max(1, len(columns)) + 1
        minimum_matrix = [[weight[i][j] * factor - metric[i][j] for j in range(size)] for i in range(size)]
        maximum_matrix = [[weight[i][j] * factor + metric[i][j] for j in range(size)] for i in range(size)]
        _, low = maximum_assignment(minimum_matrix)
        _, high = maximum_assignment(maximum_matrix)
        return sum(metric[i][j] for i, j in enumerate(low)), sum(metric[i][j] for i, j in enumerate(high))
    correct_min, correct_max = bounds(metrics)
    classes = {}
    for kind in sorted({f.get("type", "string") for f in columns}):
        subset = [field for field in columns if field.get("type", "string") == kind]
        metric = [[0] * size for _ in range(size)]
        for i, j in eligible:
            metric[i][j] = sum(normalize(expected[i].get(f["name"]), f) is not None
                and equal(expected[i].get(f["name"]), actual[j].get(f["name"]), f) for f in subset)
        minimum, maximum = bounds(metric)
        e = sum(normalize(row.get(f["name"]), f) is not None for row in expected for f in subset)
        a = sum(normalize(row.get(f["name"]), f) is not None for row in actual for f in subset)
        classes[kind] = {"expected_nonnull": e, "produced_nonnull": a, "correct_min": minimum, "correct_max": maximum,
                         "precision_min": minimum / a if a else None, "recall_min": minimum / e if e else None}
    selected = {i: assignment[i] for i in range(m) if assignment[i] < n and (i, assignment[i]) in eligible}
    details = []
    for i, wanted in enumerate(expected):
        cells = {}
        for field in columns:
            name = field["name"]
            values = [actual[j].get(name) if j is not None else None for j in possibilities[i]]
            outcomes = [outcome(wanted.get(name), value, field) for value in values]
            outcomes = ["UNRESOLVED_SOURCE_AMBIGUITY" if label == "MISSING" and j is not None
                        and f"/tables/{table_name}/{j}/{name}" in uncertainty_paths else label
                        for j, label in zip(possibilities[i], outcomes)]
            if not outcomes:
                raise ValueError("EMPTY_ALL_OPTIMAL_MATCH_SET")
            cells[name] = {"outcome": outcomes[0] if len(set(outcomes)) == 1 else "ALIGNMENT_AMBIGUOUS",
                           "possible_outcomes": sorted(set(outcomes)), "expected_nonnull": normalize(wanted.get(name), field) is not None}
        details.append({"expected_row": i, "selected_actual_row": selected.get(i), "possible_actual_rows": possibilities[i],
                        "alignment_ambiguous": len(possibilities[i]) > 1, "cells": cells})
    expected_nonnull = sum(normalize(row.get(field["name"]), field) is not None for row in expected for field in columns)
    produced_nonnull = sum(normalize(row.get(field["name"]), field) is not None for row in actual for field in columns)
    normalized_expected = [canonical({f["name"]: str(normalize(row.get(f["name"]), f)) for f in columns}) for row in expected]
    normalized_actual = [canonical({f["name"]: str(normalize(row.get(f["name"]), f)) for f in columns}) for row in actual]
    ecount, acount = Counter(normalized_expected), Counter(normalized_actual)
    duplicates = sum(max(0, count - max(1, ecount[value])) for value, count in acount.items())
    order = [selected[i] for i in sorted(selected)]
    inversions = sum(order[i] > order[j] for i in range(len(order)) for j in range(i + 1, len(order)))
    return {"expected_rows": m, "actual_rows": n, "missing_rows": m - len(selected), "additional_rows": n - len(selected),
            "additional_duplicate_occurrences": duplicates, "legitimate_duplicate_occurrences": sum(max(0, count - 1) for count in ecount.values()),
            "expected_columns": len(columns), "actual_schema_columns": sorted(set().union(*(set(row) for row in actual))) if actual else [],
            "expected_nonnull_cells": expected_nonnull, "produced_nonnull_cells": produced_nonnull,
            "correct_min": correct_min, "correct_max": correct_max,
            "precision_min": correct_min / produced_nonnull if produced_nonnull else None,
            "recall_min": correct_min / expected_nonnull if expected_nonnull else None,
            "reading_order_inversions_selected_alignment": inversions,
            "reading_order_limit": "Indistinguishable duplicate occurrences have no measurable individual ordering",
            "per_class": classes, "rows": details, "assignment_score": best, "matching_uses_row_ordinal_as_identity": False}


def levenshtein(left, right):
    if len(left) < len(right):
        left, right = right, left
    previous = list(range(len(right) + 1))
    for i, first in enumerate(left, 1):
        current = [i]
        for j, second in enumerate(right, 1):
            current.append(min(current[-1] + 1, previous[j] + 1, previous[j - 1] + (first != second)))
        previous = current
    return previous[-1]


def text_metrics(reference, output):
    textref = reference.get("text_reference") or {}
    recognized = output.get("recognized_text")
    if not textref.get("adequate_for_cer_wer") or not isinstance(textref.get("full_text"), str):
        return {"measured": False, "reason": "NO_ADEQUATE_FULL_TEXT_REFERENCE", "CER": None, "WER": None}
    reference_scope = textref.get("scope", "FULL_DOCUMENT")
    normalized_scope = POLICY["text_scope_aliases"].get(reference_scope, reference_scope)
    if not isinstance(recognized, str) or output.get("recognized_text_scope") != normalized_scope:
        return {"measured": False, "reason": "NO_COMPARABLE_RECOGNITION_TEXT_SCOPE", "CER": None, "WER": None}
    wanted, observed = string(textref["full_text"]), string(recognized)
    chars, words = len(wanted), len(wanted.split())
    char_errors = levenshtein(wanted, observed)
    word_errors = levenshtein(wanted.split(), observed.split())
    return {"measured": True, "scope": reference_scope, "compared_scope": normalized_scope,
            "reference_scope_provenance_method": textref.get("method"), "reference_chars": chars,
            "reference_words": words, "recognized_chars": len(observed), "recognized_words": len(observed.split()),
            "character_edit_distance": char_errors, "word_edit_distance": word_errors,
            "CER": char_errors / chars if chars else None, "WER": word_errors / words if words else None,
            "policy": "NFC_WHITESPACE_PRESERVE_CASE", "not_derived_from_field_scores": True}


def is_critical(path, patterns):
    return any(re.fullmatch(re.escape(pattern).replace(r"\*", r"[^.]+"), path) for pattern in patterns)


def evaluate(reference, output, schema):
    if output["status"] not in STATUSES:
        raise ValueError("UNDECLARED_TECHNICAL_STATUS")
    expected_denominator = sum(normalize(reference.get("fields", {}).get(f["name"]), f) is not None for f in schema["fields"])
    for table in schema["tables"]:
        expected_denominator += sum(normalize(row.get(f["name"]), f) is not None
            for row in reference.get("tables", {}).get(table["name"], []) for f in table["columns"])
    result = {"evaluator_version": VERSION, "document_id": reference["document_id"], "route": output["route"],
              "status": output["status"], "planned_nonnull_reference_cells": expected_denominator,
              "technical_reason": output.get("reason"), "extraction_measured": False,
              "text_metrics": text_metrics(reference, output), "recognition_inventory": output.get("recognition_inventory"),
              "performance": output.get("metrics", {}), "actual_validator": output.get("validation"),
              "operational_approval_measured": output.get("operational_approval_measured") is True,
              "native_publication_measured": False, "synthetic": reference.get("synthetic"),
              "design_id": reference.get("design_id"), "paired_variants_not_independent": True,
              "attribution": output.get("attribution", []), "field_scores_not_model_intrinsic_accuracy": True}
    semantic = output.get("semantic")
    if not isinstance(semantic, dict):
        result["unmeasured_extraction_reason"] = output.get("semantic_reason") or "NO_USABLE_SEMANTIC_OUTPUT"
        return result
    fields, tables = semantic.get("fields"), semantic.get("tables")
    if not isinstance(fields, dict) or not isinstance(tables, dict):
        result["unmeasured_extraction_reason"] = "SEMANTIC_OBJECT_CONTRACT_INVALID"
        return result
    header_comparisons = {}
    correct = produced = expected = 0
    outcomes = Counter()
    critical = Counter()
    comparisons_by_class = defaultdict(Counter)
    class_metrics = defaultdict(Counter)
    patterns = reference.get("critical_paths", [])
    uncertainty_paths = {item["path"] for item in output.get("uncertainty", []) if isinstance(item, dict) and isinstance(item.get("path"), str)}
    for field in schema["fields"]:
        name = field["name"]
        wanted, observed = reference.get("fields", {}).get(name), fields.get(name)
        cell_outcome = outcome(wanted, observed, field, present=name in fields)
        if cell_outcome == "MISSING" and "/fields/" + name in uncertainty_paths:
            cell_outcome = "UNRESOLVED_SOURCE_AMBIGUITY"
        path = "headers." + name
        cell = {"outcome": cell_outcome, "key_emitted": name in fields,
                "expected_nonnull": normalize(wanted, field) is not None,
                "observed_nonnull": normalize(observed, field) is not None}
        if cell_outcome == "INCORRECT" and field.get("type") == "string" and len(string(observed)) >= 4:
            other = [other["name"] for other in schema["fields"] if other["name"] != name
                     and other.get("type") == "string" and normalize(reference.get("fields", {}).get(other["name"]), other) is not None
                     and equal(reference["fields"][other["name"]], observed, other)]
            if other:
                cell["wrong_association_evidenced_against_reference_roles"] = other
        header_comparisons[path] = cell
        outcomes[cell_outcome] += 1
        comparisons_by_class["headers." + field.get("type", "string")][cell_outcome] += 1
        class_key = "headers." + field.get("type", "string")
        class_metrics[class_key]["expected_nonnull"] += cell["expected_nonnull"]
        class_metrics[class_key]["produced_nonnull"] += cell["observed_nonnull"]
        class_metrics[class_key]["correct_min"] += cell_outcome == "CORRECT"
        class_metrics[class_key]["correct_max"] += cell_outcome == "CORRECT"
        expected += cell["expected_nonnull"]
        produced += cell["observed_nonnull"]
        correct += cell_outcome == "CORRECT"
        if is_critical(path, patterns):
            critical["expected"] += 1
            critical["expected_nonnull"] += cell["expected_nonnull"]
            critical[cell_outcome] += 1
    table_results = {}
    table_correct_max = 0
    for table in schema["tables"]:
        name = table["name"]
        actual = tables.get(name, [])
        if not isinstance(actual, list) or any(not isinstance(row, dict) for row in actual):
            raise ValueError("SEMANTIC_TABLE_CONTRACT_INVALID:" + name)
        comparison = table_compare(reference.get("tables", {}).get(name, []), actual, table["columns"],
                                   reference.get("table_identity_fields", {}).get(name, []), uncertainty_paths=uncertainty_paths, table_name=name)
        table_results[name] = comparison
        correct += comparison["correct_min"]
        table_correct_max += comparison["correct_max"]
        expected += comparison["expected_nonnull_cells"]
        produced += comparison["produced_nonnull_cells"]
        for kind, metric in comparison["per_class"].items():
            class_metrics[name + "." + kind].update({key: metric[key] for key in ("expected_nonnull", "produced_nonnull", "correct_min", "correct_max")})
        for row in comparison["rows"]:
            for field in table["columns"]:
                value = row["cells"][field["name"]]
                cell_outcome = value["outcome"]
                outcomes[cell_outcome] += 1
                comparisons_by_class[name + "." + field.get("type", "string")][cell_outcome] += 1
                if is_critical("tables." + name + "." + str(row["expected_row"]) + "." + field["name"], patterns):
                    critical["expected"] += 1
                    critical["expected_nonnull"] += value["expected_nonnull"]
                    critical[cell_outcome] += 1
    for metric in class_metrics.values():
        metric["precision_min"] = metric["correct_min"] / metric["produced_nonnull"] if metric["produced_nonnull"] else None
        metric["recall_min"] = metric["correct_min"] / metric["expected_nonnull"] if metric["expected_nonnull"] else None
    result.update(extraction_measured=True, counts={"expected_nonnull_cells": expected, "produced_nonnull_cells": produced,
        "correct_min": correct, "correct_max": sum(item["outcome"] == "CORRECT" for item in header_comparisons.values()) + table_correct_max,
        "precision_min": correct / produced if produced else None, "recall_min": correct / expected if expected else None,
        "per_expected_cell_outcomes": dict(outcomes), "legitimate_nulls_not_accuracy_credit": outcomes["LEGITIMATE_NULL"],
        "additional_table_rows": sum(item["additional_rows"] for item in table_results.values()),
        "missing_table_rows": sum(item["missing_rows"] for item in table_results.values()),
        "additional_duplicate_occurrences": sum(item["additional_duplicate_occurrences"] for item in table_results.values())},
        headers=header_comparisons, tables=table_results, critical=dict(critical),
        per_class_expected_cell_outcomes={key: dict(value) for key, value in comparisons_by_class.items()},
        per_class_precision_recall={key: dict(value) for key, value in class_metrics.items()},
        unresolved_uncertainty=output.get("uncertainty", []),
        unrequested_field_keys=sorted(set(fields) - {f["name"] for f in schema["fields"]}),
        unrequested_table_keys=sorted(set(tables) - {t["name"] for t in schema["tables"]}))
    return result


def self_test():
    schema = {"fields": [{"name": "number", "type": "string"}, {"name": "optional", "type": "string"}],
              "tables": [{"name": "lines", "columns": [{"name": "description", "type": "string"}, {"name": "amount", "type": "decimal"}]}]}
    reference = {"document_id": "SELFTEST", "fields": {"number": "CASE-1", "optional": None},
                 "tables": {"lines": [{"description": "Alpha", "amount": "10.00"}, {"description": "Alpha", "amount": "10.00"}]},
                 "table_identity_fields": {"lines": ["description"]}, "critical_paths": ["headers.number", "tables.lines.*.amount"]}
    base = {"document_id": "SELFTEST", "route": "B_QWEN_DIRECT", "status": "COMPLETE",
            "semantic": {"fields": reference["fields"], "tables": reference["tables"]}}
    full = evaluate(reference, base, schema)
    assert full["counts"]["correct_min"] == 5 and full["counts"]["precision_min"] == 1
    assert full["counts"]["legitimate_nulls_not_accuracy_credit"] == 1
    assert full["tables"]["lines"]["legitimate_duplicate_occurrences"] == 1
    assert full["critical"]["CORRECT"] == 3
    partial = json.loads(json.dumps(base))
    partial["status"] = "PARTIAL"
    partial["semantic"]["tables"]["lines"].pop()
    assert evaluate(reference, partial, schema)["counts"]["missing_table_rows"] == 1
    for status in ("UNSUPPORTED", "TIMEOUT", "ERROR", "NOT_RUN_DEPENDENCY", "NOT_RUN_BUDGET"):
        item = evaluate(reference, {"route": "A_PADDLE", "status": status, "semantic": None}, schema)
        assert not item["extraction_measured"] and item["planned_nonnull_reference_cells"] == 5
    try:
        strict_json('{"fields": INVALID}')
        raise AssertionError("INVALID_JSON_NOT_REJECTED")
    except json.JSONDecodeError:
        pass
    for invalid in ('{"a":NaN}', '{"a":Infinity}', '{"a":1,"a":2}'):
        try:
            strict_json(invalid)
            raise AssertionError("STRICT_JSON_NOT_REJECTED")
        except ValueError:
            pass
    assert normalize("1.234", {"name": "a", "type": "decimal"})[0] == "INVALID_SCALAR"
    assert equal("COP", "Pesos colombianos", {"name": "currency", "type": "string"})
    assert not equal("COP", "$", {"name": "currency", "type": "string"})
    assert equal("2026-01-31", "31/01/2026", {"name": "date", "type": "date"})
    assert normalize("02/03/2026", {"name": "date", "type": "date"})[0] == "INVALID_DATE"
    assert outcome("2026-03-02", "02/03/2026", {"name": "date", "type": "date"}) == "UNRESOLVED_NORMALIZATION"
    assert full["per_class_precision_recall"]["lines.decimal"]["recall_min"] == 1
    assert not text_metrics(reference, base)["measured"]
    textref = {"text_reference": {"adequate_for_cer_wer": True, "full_text": "Alpha beta", "scope": "FULL_DOCUMENT"}}
    assert text_metrics(textref, {"recognized_text": "Alpha beta", "recognized_text_scope": "FULL_DOCUMENT"})["CER"] == 0
    for scope in POLICY["text_scope_aliases"]:
        scoped = {"text_reference": {**textref["text_reference"], "scope": scope, "method": "FIXED_REFERENCE_PROVENANCE"}}
        measured = text_metrics(scoped, {"recognized_text": "Alpha beta", "recognized_text_scope": "FULL_DOCUMENT"})
        assert measured["CER"] == 0 and measured["scope"] == scope
    unknown = {"text_reference": {**textref["text_reference"], "scope": "FULL_DOCUMENT_UNKNOWN_UNDECLARED"}}
    assert not text_metrics(unknown, {"recognized_text": "Alpha beta", "recognized_text_scope": "FULL_DOCUMENT"})["measured"]
    return {"status": "PASS", "cases": ["complete", "incomplete", "invalid_json", "unsupported", "timeout", "dependency_budget", "duplicate_occurrences", "currency_equivalence", "ambiguous_number", "text_scope"],
            "models": 0, "product_changes": 0, "policy_sha256": hashlib.sha256(canonical(POLICY)).hexdigest()}


def validate_reference(reference, schema):
    if set(reference.get("fields", {})) != {f["name"] for f in schema["fields"]}:
        raise ValueError("REFERENCE_HEADER_KEYS_DO_NOT_MATCH_SCHEMA:" + reference["document_id"])
    if set(reference.get("tables", {})) != {t["name"] for t in schema["tables"]}:
        raise ValueError("REFERENCE_TABLE_KEYS_DO_NOT_MATCH_SCHEMA:" + reference["document_id"])
    for field in schema["fields"]:
        value = normalize(reference["fields"][field["name"]], field)
        if isinstance(value, tuple) and value and str(value[0]).startswith("INVALID"):
            raise ValueError("REFERENCE_UNSUPPORTED_NORMALIZATION:" + reference["document_id"] + ":" + field["name"])
    for table in schema["tables"]:
        for row in reference["tables"][table["name"]]:
            if set(row) != {f["name"] for f in table["columns"]}:
                raise ValueError("REFERENCE_ROW_KEYS_DO_NOT_MATCH_SCHEMA")
            for field in table["columns"]:
                value = normalize(row[field["name"]], field)
                if isinstance(value, tuple) and value and str(value[0]).startswith("INVALID"):
                    raise ValueError("REFERENCE_UNSUPPORTED_NORMALIZATION:" + reference["document_id"] + ":" + table["name"] + ":" + field["name"])
    return True


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--self-test", action="store_true")
    parser.add_argument("--policy-out", type=Path)
    parser.add_argument("--schema", type=Path)
    parser.add_argument("--reference", type=Path)
    parser.add_argument("--result", type=Path)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--reference-directory", type=Path)
    args = parser.parse_args()
    if args.self_test:
        print(json.dumps(self_test(), ensure_ascii=False))
        return
    if args.policy_out:
        with args.policy_out.open("xb") as stream:
            stream.write(canonical(POLICY) + b"\n")
        print(json.dumps(file_ref(args.policy_out)))
        return
    if args.reference_directory:
        bundle = load(args.schema)["schemas"]
        by_id = {item["schema_id"]: item for item in bundle.values()}
        references = sorted(args.reference_directory.glob("*.private.json"))
        for path in references:
            reference = load(path)
            validate_reference(reference, by_id[reference["schema_id"]])
        print(json.dumps({"status": "REFERENCE_CONTRACT_PREFLIGHT_PASS", "references": len(references),
                          "scoring_calls": 0, "models": 0, "references_modified": False}))
        return
    if not all((args.schema, args.reference, args.result, args.output)):
        parser.error("schema/reference/result/output required")
    bundle = load(args.schema)
    reference, output = load(args.reference), load(args.result)
    schemas = bundle.get("schemas", bundle)
    if "document_type" in reference:
        schema = schemas[reference["document_type"]]
    else:
        matches = [item for item in schemas.values() if item.get("schema_id") == reference.get("schema_id")]
        if len(matches) != 1:
            raise ValueError("UNIQUE_REFERENCE_SCHEMA_ID_REQUIRED")
        schema = matches[0]
    if output.get("document_id") != reference["document_id"]:
        raise ValueError("OUTPUT_REFERENCE_DOCUMENT_ID_MISMATCH")
    if output.get("source_sha256") and output["source_sha256"] != reference.get("source_sha256"):
        raise ValueError("OUTPUT_REFERENCE_SOURCE_HASH_MISMATCH")
    validate_reference(reference, schema)
    evaluated = evaluate(reference, output, schema)
    evaluated["inputs"] = {"schema": file_ref(args.schema), "reference": file_ref(args.reference), "result": file_ref(args.result)}
    evaluated["policy_sha256"] = hashlib.sha256(canonical(POLICY)).hexdigest()
    with args.output.open("xb") as stream:
        stream.write(canonical(evaluated) + b"\n")
    print(json.dumps({"status": "EVALUATED_SAVED_OUTPUT_ONLY", "output": file_ref(args.output)}, ensure_ascii=False))


if __name__ == "__main__":
    main()
