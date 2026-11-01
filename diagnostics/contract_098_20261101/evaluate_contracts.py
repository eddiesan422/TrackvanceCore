"""Small post-seal diagnostic adapter. No model/service calls or operational writes.

The previous evaluator, normalizer, page aggregator and wire validator are loaded
unchanged with fixed hashes. References are used only by --evaluate after ROOT
has closed and sealed the new 12-case campaign. --self-test uses synthetic data.
"""
from __future__ import annotations

import argparse
from collections import Counter
from copy import deepcopy
from decimal import Decimal
import hashlib
import importlib.util
import json
from pathlib import Path
import re

ARMS = ("BASE_CONTRACT", "LITERAL_CONTRACT")
DOCUMENTS = ("NINV-A-D", "NINV-A-S", "STMT-A-D", "STMT-A-S", "NARR-A-D", "NEG-CONTRACT-01")
PINNED = {
    "evaluate_matrix.py": "34053eebed70d2bf5e1f24586ada261d5dbc037d835e6eeeda83fcf2b7b78659",
    "run_matrix.py": "30e63d1d42d827ba0f246b930694512fa647b3f37fe585c0fe8506e23f14145f",
    "gateway_diagnostic_worker.py": "045336cd216cc7d4108672be7dcd93f91e11ec10a5182f6fa0aecb4781de4c01",
}
UNCERTAINTY_RULES = {
    "TYPE_SCHEMA": r"\b(schema|decimal|data type|string/null|numeric type|tipo de dato|esquema)\b",
    "READING": r"\b(unreadable|illegible|blurred|blur|ilegible|borroso|lectura)\b",
    "ASSOCIATION": r"\b(association|associated|role|mapped|mapping|asociaci[oó]n|rol|correspondencia)\b",
    "CONTRADICTION": r"\b(conflicting|conflict|contradictory|contradiction|contradicci[oó]n|contradictori[oa]s?|incompatible)\b",
    "ABSENCE": r"\b(absent|absence|not present|not stated|missing from|ausente|ausencia|no aparece|no se indica)\b",
}
POLICY = {
    "literal": "Prior STRING policy: NFC and collapsed whitespace, case/signs/separators preserved; no currency aliases or numeric/date conversion",
    "row_literal": "Use only frozen evaluator all-optimal one-to-one row mappings; lower credit requires every possible mapped occurrence to match, upper is per-cell possibility, not a globally attainable score",
    "pages": "Page/role references declared before models; page occurrence denominators are distinct from document denominators and are not pooled",
    "uncertainty": "Only explicit model reason patterns; multiple categories allowed, unmatched is UNACCREDITED; never infer cause from gold or alternatives",
    "negative": "Contradictory reference null is not legitimate absence or accuracy credit; require null plus explicit contradiction uncertainty at the same role",
    "approval": "Diagnostic only; no operational approval, publication, candidate selection or alternative promotion",
    "status_transport": "External NOT_RUN is passed to the unchanged prior evaluator as NOT_RUN_BUDGET solely for its unmeasured-output interface; preserve actual NOT_RUN and original reason, without asserting a budget cause",
    "uncertainty_rules": UNCERTAINTY_RULES,
}


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()


def need(value, reason):
    if not value:
        raise ValueError(reason)


def load_modules(directory):
    result = []
    for index, (name, wanted) in enumerate(PINNED.items()):
        path = Path(directory) / "scripts" / name
        need(sha(path) == wanted, "PREVIOUS_SOURCE_HASH_CHANGED:" + name)
        spec = importlib.util.spec_from_file_location("contract_frozen_" + str(index), path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        result.append(module)
    return result


def categories(reason):
    return [name for name, pattern in UNCERTAINTY_RULES.items() if re.search(pattern, reason, re.I)] or ["UNACCREDITED"]


def get_path(value, path):
    current = value
    try:
        for part in path.strip("/").split("/"):
            current = current[int(part)] if isinstance(current, list) else current[part]
        return current
    except (KeyError, IndexError, TypeError, ValueError):
        return None


def literal_measure(ev, wanted, actual, schema, identities, *, mappings=None):
    """No substring search. All observations retain their emitted semantic role."""
    excluded = set(wanted.get("excluded_literal_paths", []))
    uncertainty = {x["path"] for x in actual.get("uncertainty", [])}
    counts = Counter(expected_nonnull=0, correct_min=0, correct_possible_cell_upper=0, numeric_expected=0,
                     numeric_correct_min=0, numeric_interpretable=0, numeric_normalized_correct_min=0)
    cells = {}

    def cell(path, expected, values, field, paths):
        if path in excluded or expected is None or ev.string(expected) == "":
            return
        counts["expected_nonnull"] += 1
        matches = [isinstance(value, str) and ev.string(value) == ev.string(expected) and candidate not in uncertainty
                   for value, candidate in zip(values, paths)]
        low, high = bool(matches) and all(matches), any(matches)
        counts["correct_min"] += low
        counts["correct_possible_cell_upper"] += high
        cells[path] = {"correct_guaranteed": low, "correct_possible": high,
                       "observed_values": values, "observed_paths": paths}
        if field.get("type") == "decimal":
            counts["numeric_expected"] += 1
            parsed = [ev.normalize(value, field) for value in values]
            interpretable = [isinstance(value, Decimal) and candidate not in uncertainty for value, candidate in zip(parsed, paths)]
            norm_correct = [ok and value == ev.normalize(expected, field) for ok, value in zip(interpretable, parsed)]
            counts["numeric_correct_min"] += low
            counts["numeric_interpretable"] += bool(interpretable) and all(interpretable)
            counts["numeric_normalized_correct_min"] += bool(norm_correct) and all(norm_correct)

    for field in schema["fields"]:
        name = field["name"]
        path = "/fields/" + name
        cell(path, wanted["fields"].get(name), [actual.get("fields", {}).get(name)], field, [path])
    for table in schema["tables"]:
        name = table["name"]
        expected_rows, rows = wanted["tables"][name], actual.get("tables", {}).get(name, [])
        mapping = mappings[name] if mappings is not None else ev.table_compare(expected_rows, rows, table["columns"], identities.get(name, []))
        for row in mapping["rows"]:
            i, possible = row["expected_row"], row["possible_actual_rows"]
            for field in table["columns"]:
                key = field["name"]
                paths = [f"/tables/{name}/{j}/{key}" if j is not None else "UNMATCHED" for j in possible]
                values = [rows[j].get(key) if j is not None else None for j in possible]
                cell(f"/tables/{name}/{i}/{key}", expected_rows[i].get(key), values, field, paths)
    return {"counts": dict(counts), "cells": cells, "excluded_paths": sorted(excluded)}


def score_case(ev, runner, worker, reference, literal_ref, schema, original):
    need(original["document_id"] == reference["document_id"] == literal_ref["document_id"], "CASE_ID_MISMATCH")
    need(original["source_sha256"] == reference["source_sha256"] == literal_ref["source_sha256"], "SOURCE_HASH_MISMATCH")
    need(original["arm"] in ARMS, "UNDECLARED_ARM")
    pages = original.get("pages", [])
    replayed = runner.aggregate(pages, schema) if pages else None
    need(replayed == original.get("semantic"), "STORED_AGGREGATE_DIFFERS_FROM_FROZEN_AGGREGATOR")
    status = original["status"]
    internal_status = "NOT_RUN_BUDGET" if status == "NOT_RUN" else status
    need(internal_status in ev.STATUSES, "UNSUPPORTED_ENVELOPE_STATUS")
    wrapper = {**original, "status": internal_status, "route": original["arm"], "uncertainty": (replayed or {}).get("uncertainty", []),
               "operational_approval_measured": False}
    normalized = ev.evaluate(reference, wrapper, schema)
    aggregate_wanted = {**literal_ref["literals"], "excluded_literal_paths": literal_ref.get("excluded_literal_paths", [])}
    aggregate_literal = literal_measure(ev, aggregate_wanted, replayed, schema,
        reference.get("table_identity_fields", {}), mappings=normalized["tables"]) if normalized["extraction_measured"] else None
    page_refs = {x["page"]: x for x in literal_ref["pages"]}
    page_metrics, reason_counts = [], Counter()
    uncertainties = []
    for page in pages:
        number = page["page"]
        need(number in page_refs, "UNDECLARED_PAGE")
        raw = page.get("reply", {}).get("message", {}).get("content")
        raw_valid, semantic_valid, parsed, error = False, False, None, None
        try:
            parsed = ev.strict_json(raw) if isinstance(raw, str) else None
            raw_valid = True
            worker.validate_semantic(parsed, schema)
            semantic_valid = True
        except (ValueError, TypeError, KeyError) as problem:
            error = type(problem).__name__
        need(not semantic_valid or parsed == page.get("semantic"), "RAW_PAGE_SEMANTIC_MISMATCH")
        observed = page.get("semantic")
        if isinstance(observed, dict):
            need(semantic_valid, "SEMANTIC_OUTPUT_WITHOUT_VALID_ORIGINAL_JSON")
            worker.validate_semantic(observed, schema)
            for item in observed["uncertainty"]:
                kind = categories(item["reason"])
                reason_counts.update(kind)
                uncertainties.append({"page": number, **item, "explicit_reason_categories": kind})
        measured = literal_measure(ev, page_refs[number], observed, schema, reference.get("table_identity_fields", {})) if isinstance(observed, dict) else None
        empty = {"fields": {}, "tables": {t["name"]: [] for t in schema["tables"]}, "uncertainty": []}
        planned = literal_measure(ev, page_refs[number], empty, schema, reference.get("table_identity_fields", {}))["counts"]["expected_nonnull"]
        page_metrics.append({"page": number, "status": page["status"], "json_valid": raw_valid,
            "semantic_contract_valid": semantic_valid, "parse_error_class": error, "literal": measured,
            "planned_nonnull_literal_occurrences": planned, "raw_content_characters": len(raw) if isinstance(raw, str) else None,
            "done_reason": page.get("reply", {}).get("done_reason"), "tokens": page.get("tokens"),
            "elapsed_seconds": page.get("elapsed_seconds"), "uncertainty_count": len((observed or {}).get("uncertainty", [])),
            "uncertainty_reason_characters": sum(len(x["reason"]) for x in (observed or {}).get("uncertainty", [])),
            "uncertainty_exact_repeated_items": sum(n-1 for n in Counter(canonical(x) for x in (observed or {}).get("uncertainty", [])).values())})
    negative = []
    for item in literal_ref.get("unresolved_expected", []):
        path = item["path"]
        selected = get_path(replayed, path)
        evidence = [x for x in uncertainties if x["path"] == path]
        measured = any(isinstance(p.get("semantic"), dict) and ("page" not in item or p["page"] == item["page"]) for p in pages)
        negative.append({"path": path, "reference_category": item["category"], "measured": measured, "selected_value": selected,
            "false_automatic_resolution": selected is not None and ev.string(selected) != "" if measured else None,
            "null_retained": selected is None or isinstance(selected, str) and not selected.strip() if measured else None,
            "same_role_uncertainty_retained": bool(evidence) if measured else None,
            "explicit_contradiction_retained": any("CONTRADICTION" in x["explicit_reason_categories"] for x in evidence) if measured else None,
            "required_alternatives_preserved_as_evidence_only": all(any(ev.string(v) in [ev.string(a) for a in x["alternatives"]] for x in evidence) for v in item["alternatives"]) if measured else None})
    guards = []
    for guard in literal_ref.get("narrative_guards", []):
        parts = guard["path"].strip("/").split("/")
        mapping = normalized.get("tables", {}).get(parts[1], {}).get("rows", [])
        row = next((x for x in mapping if x["expected_row"] == int(parts[2])), None)
        values = [replayed["tables"][parts[1]][j].get(parts[3]) if j is not None else None for j in row["possible_actual_rows"]] if row else []
        clauses = [isinstance(x, str) and ev.string(guard["exact_clause"]) in ev.string(x) for x in values]
        guards.append({"path": guard["path"], "same_role_exact_clause_guaranteed": bool(clauses) and all(clauses),
                       "same_role_exact_clause_possible": any(clauses)})
    legit = normalized.get("counts", {}).get("legitimate_nulls_not_accuracy_credit")
    return {"document_id": original["document_id"], "arm": original["arm"], "status": status,
        "executed": original.get("executed"), "reason": original.get("reason"), "normalizer_interface_status": internal_status,
        "planned_nonnull_reference_cells": normalized["planned_nonnull_reference_cells"],
        "page_metrics": page_metrics, "aggregate_literal": aggregate_literal, "normalized": normalized,
        "uncertainty_explicit_reason_counts": dict(reason_counts), "page_uncertainties": uncertainties,
        "negative_guards": negative, "narrative_guards": guards,
        "legitimate_absence_nulls": legit - sum(x["null_retained"] is True for x in negative) if legit is not None and any(isinstance(p.get("semantic"), dict) for p in pages) else None,
        "contradictory_reference_nulls_separate_no_credit": len(negative),
        "operational_approval_measured": False, "native_publication_measured": False,
        "paired_variants_not_independent": True, "metrics": original.get("metrics", {}),
        "resource_samples": original.get("resource_samples"), "failure_code": original.get("failure_code")}


def self_test(ev, runner, worker):
    schema = {"fields": [{"name": "number", "type": "string"}, {"name": "total", "type": "decimal"}, {"name": "optional", "type": "string"}], "tables": []}
    ref = {"document_id": "SELFTEST", "source_sha256": "synthetic", "fields": {"number": "T-1", "total": "749.50", "optional": None}, "tables": {}, "critical_paths": ["headers.total"]}
    sem = {"fields": deepcopy(ref["fields"]), "tables": {}, "uncertainty": []}
    literals = {"document_id": "SELFTEST", "source_sha256": "synthetic", "literals": {"fields": {**ref["fields"], "total": "749,50"}, "tables": {}}, "pages": [{"page": 1, "fields": {**ref["fields"], "total": "749,50"}, "tables": {}}]}
    sem["fields"]["total"] = "749,50"
    def result(value, status="COMPLETE", raw=None):
        page = {"page": 1, "status": status, "semantic": value, "reply": {"message": {"content": json.dumps(value) if raw is None else raw}, "done_reason": "stop"}}
        if value is None:
            page.pop("semantic")
        return {"document_id": "SELFTEST", "arm": ARMS[0], "source_sha256": "synthetic", "status": status, "executed": True, "pages": [page], "semantic": runner.aggregate([page], schema)}
    complete = score_case(ev, runner, worker, ref, literals, schema, result(sem))
    assert complete["normalized"]["counts"]["correct_min"] == 2
    assert complete["aggregate_literal"]["counts"]["numeric_interpretable"] == 1
    assert complete["legitimate_absence_nulls"] == 1
    real = deepcopy(sem)
    real["fields"]["total"] = None
    real["uncertainty"] = [{"path": "/fields/total", "reason": "Conflicting totals; contradiction unresolved", "alternatives": ["749,50", "932,75"]}]
    negref, neglit = deepcopy(ref), deepcopy(literals)
    negref["fields"]["total"] = neglit["literals"]["fields"]["total"] = neglit["pages"][0]["fields"]["total"] = None
    neglit["unresolved_expected"] = [{"path": "/fields/total", "category": "CONTRADICTION", "alternatives": ["749,50", "932,75"]}]
    ambiguity = score_case(ev, runner, worker, negref, neglit, schema, result(real))
    assert ambiguity["negative_guards"][0]["explicit_contradiction_retained"]
    assert not ambiguity["negative_guards"][0]["false_automatic_resolution"]
    assert ambiguity["legitimate_absence_nulls"] == 1
    assert ambiguity["aggregate_literal"]["counts"]["numeric_expected"] == 0
    truncated = score_case(ev, runner, worker, ref, literals, schema, result(None, "ERROR", '{"fields":'))
    assert not truncated["page_metrics"][0]["json_valid"] and truncated["planned_nonnull_reference_cells"] == 2
    partial_sem = deepcopy(sem)
    partial_sem["fields"]["total"] = None
    partial = score_case(ev, runner, worker, ref, literals, schema, result(partial_sem, "PARTIAL"))
    assert partial["normalized"]["counts"]["correct_min"] == 1 and partial["planned_nonnull_reference_cells"] == 2
    typedoubt = deepcopy(real)
    typedoubt["uncertainty"][0]["reason"] = "Schema requires decimal, but wire is string/null"
    typed = score_case(ev, runner, worker, ref, literals, schema, result(typedoubt))
    assert typed["aggregate_literal"]["counts"]["numeric_correct_min"] == 0
    assert typed["uncertainty_explicit_reason_counts"] == {"TYPE_SCHEMA": 1}
    assert categories("I am uncertain") == ["UNACCREDITED"]
    both = [result(sem), {"document_id": "SELFTEST", "arm": ARMS[1], "source_sha256": "synthetic", "status": "TIMEOUT", "executed": True, "pages": [], "semantic": None}]
    kept = [score_case(ev, runner, worker, ref, literals, schema, x) for x in both]
    assert len(kept) == 2 and sum(x["planned_nonnull_reference_cells"] for x in kept) == 4
    unrun = {**both[1], "status": "NOT_RUN", "executed": False, "reason": "TWO_CONSECUTIVE_INFRASTRUCTURE_FAILURES_STOP_ALL"}
    unavailable = score_case(ev, runner, worker, negref, neglit, schema, unrun)
    assert unavailable["status"] == "NOT_RUN" and unavailable["normalizer_interface_status"] == "NOT_RUN_BUDGET"
    assert unavailable["planned_nonnull_reference_cells"] == 1 and not unavailable["normalized"]["extraction_measured"]
    assert unavailable["negative_guards"][0]["measured"] is False
    assert unavailable["negative_guards"][0]["null_retained"] is None and unavailable["negative_guards"][0]["false_automatic_resolution"] is None
    return {"status": "PASS", "cases": ["complete", "legitimate_null", "real_ambiguity", "truncated_json", "numeric_string", "partial"], "models": 0, "actual_reference_files_read": 0, "old_campaign_scoring_calls": 0,
            "synthetic_only": True, "alternative_promotion": False, "not_run_denominator_and_unmeasured_negative": "PASS", "policy_sha256": hashlib.sha256(canonical(POLICY)).hexdigest()}


def checked(ev, pin):
    need(sha(pin["path"]) == pin["sha256"], "SEALED_INPUT_CHANGED")
    return ev.load(pin["path"])


def preflight_sample(ev, manifest_path, schema_path):
    sample, schemas = ev.load(manifest_path), ev.load(schema_path)["schemas"]
    need({x["document_id"] for x in sample["documents"]} == set(DOCUMENTS), "EXACT_SIX_DOCUMENTS_REQUIRED")
    denominators = []
    for doc in sample["documents"]:
        need(sha(doc["reference_file"]) == doc["reference_sha256"] and sha(doc["literal_reference_file"]) == doc["literal_reference_sha256"], "REFERENCE_PIN_CHANGED")
        ref, lit = ev.load(doc["reference_file"]), ev.load(doc["literal_reference_file"])
        schema = next(x for x in schemas.values() if x["schema_id"] == ref["schema_id"])
        ev.validate_reference(ref, schema)
        if doc.get("old_reference_file"):
            old = ev.load(doc["old_reference_file"])
            for key in ("fields", "tables", "critical_paths", "table_identity_fields", "source_sha256", "schema_id"):
                need(ref.get(key) == old.get(key), "PREVIOUS_REFERENCE_MEANING_CHANGED:" + key)
        empty = {"fields": {}, "tables": {t["name"]: [] for t in schema["tables"]}, "uncertainty": []}
        wanted = {**lit["literals"], "excluded_literal_paths": lit.get("excluded_literal_paths", [])}
        counts = literal_measure(ev, wanted, empty, schema, ref.get("table_identity_fields", {}))["counts"]
        page_count = sum(literal_measure(ev, page, empty, schema, ref.get("table_identity_fields", {}))["counts"]["expected_nonnull"] for page in lit["pages"])
        need(counts["expected_nonnull"] == doc["literal_denominators"]["document_nonnull_literal_cells"], "LITERAL_DENOMINATOR_MISMATCH")
        need(page_count == doc["literal_denominators"]["page_nonnull_literal_occurrences"], "PAGE_LITERAL_DENOMINATOR_MISMATCH")
        denominators.append({"document_id": doc["document_id"], "literal_nonnull": counts["expected_nonnull"], "numeric_nonnull": counts["numeric_expected"], "page_literal_occurrences": page_count})
    return {"status": "REFERENCE_CONTRACT_PREFLIGHT_PASS", "documents": denominators, "model_calls": 0, "scoring_model_outputs": 0}


def evaluate_campaign(ev, runner, worker, release):
    need(release.get("authorized") is True and release.get("model_admission") == "CLOSED", "ROOT_POSTSEAL_RELEASE_REQUIRED")
    need(sha(__file__) == release["evaluator_adapter_sha256"], "ADAPTER_HASH_CHANGED")
    for pin in release["files"]:
        need(sha(pin["path"]) == pin["sha256"], "RELEASE_PIN_CHANGED")
    seal = checked(ev, release["output_seal"])
    for pin in seal["files"]:
        need(sha(pin["path"]) == pin["sha256"], "OUTPUT_SEAL_CHANGED")
    sample = checked(ev, release["sample_manifest"])
    schemas = checked(ev, release["schema"])["schemas"]
    summary = checked(ev, release["model_summary"])
    documents = {x["document_id"]: x for x in sample["documents"]}
    need(set(documents) == set(DOCUMENTS), "EXACT_SIX_DOCUMENTS_REQUIRED")
    rows = summary["rows"]
    need(len(rows) == 12 and {(x["document_id"], x["arm"]) for x in rows} == {(d, a) for d in DOCUMENTS for a in ARMS}, "EXACT_TWELVE_CASES_REQUIRED")
    destination = Path(release["evaluation_output_dir"])
    destination.mkdir()
    evaluated = []
    for row in rows:
        doc = documents[row["document_id"]]
        reference = ev.load(doc["reference_file"])
        literal_ref = ev.load(doc["literal_reference_file"])
        schema = next(x for x in schemas.values() if x["schema_id"] == reference["schema_id"])
        ev.validate_reference(reference, schema)
        original = checked(ev, row["result"])
        need(original["arm"] == row["arm"] and original["status"] == row["status"], "SUMMARY_BINDING_MISMATCH")
        metrics = score_case(ev, runner, worker, reference, literal_ref, schema, original)
        metrics["result_input"] = row["result"]
        name = destination / (row["document_id"] + "." + row["arm"] + ".metrics.private.json")
        with name.open("xb") as stream:
            stream.write(canonical(metrics) + b"\n")
        evaluated.append(metrics)
    for pin in release["files"] + seal["files"]:
        need(sha(pin["path"]) == pin["sha256"], "INPUT_CHANGED_DURING_EVALUATION")
    with (destination / "summary.private.json").open("xb") as stream:
        stream.write(canonical({"status": "EVALUATED_NEW_SAVED_OUTPUTS_ONCE", "case_count": 12, "policy": POLICY, "rows": evaluated, "models": 0, "operational_approval_measured": False}) + b"\n")
    return {"status": "EVALUATED_NEW_SAVED_OUTPUTS_ONCE", "cases": 12, "output": str(destination), "models": 0}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--old-directory", type=Path, required=True)
    parser.add_argument("--self-test", action="store_true")
    parser.add_argument("--release", type=Path)
    parser.add_argument("--release-sha256")
    parser.add_argument("--preflight-sample", type=Path)
    parser.add_argument("--schema", type=Path)
    args = parser.parse_args()
    ev, runner, worker = load_modules(args.old_directory)
    if args.self_test:
        print(json.dumps(self_test(ev, runner, worker)))
    elif args.preflight_sample:
        need(args.schema, "SCHEMA_REQUIRED")
        print(json.dumps(preflight_sample(ev, args.preflight_sample, args.schema)))
    else:
        need(args.release and args.release_sha256 and sha(args.release) == args.release_sha256, "EXACT_ROOT_RELEASE_HASH_REQUIRED")
        print(json.dumps(evaluate_campaign(ev, runner, worker, ev.load(args.release))))


if __name__ == "__main__":
    main()
