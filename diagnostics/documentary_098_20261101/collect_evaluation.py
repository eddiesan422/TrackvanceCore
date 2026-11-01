"""Evaluate exactly 150 sealed saved cases after explicit ROOT release.

Only transport projections are performed: nested uncertainty is copied, RAW A
inventory is named consistently, and compatible A semantics may come from the
actual existing adapter applied to the same genuine A event stream. No models,
product writes, candidate selection, scorer changes, CSV authoring or publication.
"""
from __future__ import annotations

import argparse
from collections import Counter, defaultdict
import copy
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import time

ROUTES = ("A_PADDLE", "B_QWEN_DIRECT", "C_PADDLE_QWEN", "CONTROL_DIGITAL_NATIVE", "CONTROL_CURRENT_INTEGRATION")


def need(value, reason):
    if not value:
        raise RuntimeError(reason)


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def ref(path):
    path = Path(path).resolve()
    return {"path": str(path), "bytes": path.stat().st_size, "sha256": sha(path)}


def checked(record):
    path = Path(record["path"])
    need(path.is_absolute() and ref(path) == record, "PINNED_INPUT_CHANGED")
    return path


def read(path):
    def pairs(items):
        value = {}
        for key, item in items:
            need(key not in value, "DUPLICATE_JSON_KEY")
            value[key] = item
        return value
    def constant(_value):
        raise ValueError("NONFINITE_JSON")
    return json.loads(Path(path).read_bytes(), object_pairs_hook=pairs, parse_constant=constant)


def save(path, value):
    data = (json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False) + "\n").encode("utf-8")
    with Path(path).open("xb") as stream:
        stream.write(data)
    return ref(path)


def transport_wrapper(original, *, adapter, source_route):
    wrapper = copy.deepcopy(original)
    notes = []
    if isinstance(wrapper.get("semantic"), dict) and "uncertainty" in wrapper["semantic"]:
        wrapper["uncertainty"] = copy.deepcopy(wrapper["semantic"]["uncertainty"])
        notes.append("COPIED_EXACT_SEMANTIC_UNCERTAINTY_TO_EVALUATOR_ENVELOPE")
    if "physical_table_inventory" in wrapper:
        wrapper["recognition_inventory"] = {"physical_tables": copy.deepcopy(wrapper["physical_table_inventory"]),
            "raw_reading_order": copy.deepcopy(wrapper.get("raw_block_order")),
            "semantic_table_correspondence_not_invented": True}
        notes.append("NAMED_EXISTING_RAW_PHYSICAL_TABLE_INVENTORY")
    if source_route == "A_PADDLE" and isinstance(adapter.get("semantic"), dict):
        actual = original.get("recognition_events") or {}
        consumed = adapter.get("inputs", {}).get("events", {})
        need(actual.get("sha256") and actual["sha256"] == consumed.get("sha256"), "ADAPTER_MUST_CONSUME_SAME_GENUINE_A_EVENTS")
        need(adapter.get("source_route") == "A_PADDLE" and adapter.get("document_id") == original.get("document_id")
             and adapter.get("source_sha256") == original.get("source_sha256"), "ADAPTER_SOURCE_BINDING")
        wrapper["semantic"] = copy.deepcopy(adapter["semantic"])
        wrapper["semantic_reason"] = "EXISTING_KNOWN_LAYOUT_ADAPTER_PROJECTION_NOT_BARE_PADDLE_SEMANTIC_ACCURACY"
        notes.append("COPIED_EXISTING_COMPATIBLE_ADAPTER_RESULT_FROM_SAME_SAVED_A")
    wrapper["collector_transport_projections"] = notes
    wrapper["operational_approval_measured"] = False
    return wrapper


def flat_row(document, output, metric):
    counts = metric.get("counts") or {}
    outcomes = counts.get("per_expected_cell_outcomes") or {}
    critical = metric.get("critical") or {}
    text = metric["text_metrics"]
    performance = output.get("metrics") or {}
    tables = metric.get("tables") or {}
    return {"document_id": document["document_id"], "document_type": document["type"], "design_id": document["design_id"],
        "group": document["group"], "modality": document["modality"], "provenance": document["provenance"], "pair_id": document.get("pair_id"),
        "same_content_variants": document["same_content_variants"], "route": output["route"], "status": output["status"],
        "executed": output.get("executed"), "reason": output.get("reason"), "semantic_measured": metric["extraction_measured"],
        "semantic_method": output.get("semantic_reason"), "planned_nonnull_reference_cells": metric["planned_nonnull_reference_cells"],
        "correct_min": counts.get("correct_min"), "correct_max": counts.get("correct_max"), "precision_min": counts.get("precision_min"),
        "recall_min": counts.get("recall_min"), "incorrect_guaranteed": outcomes.get("INCORRECT", 0) if counts else None,
        "missing_guaranteed": outcomes.get("MISSING", 0) if counts else None,
        "unresolved_normalization": outcomes.get("UNRESOLVED_NORMALIZATION", 0) if counts else None,
        "unresolved_source_ambiguity": outcomes.get("UNRESOLVED_SOURCE_AMBIGUITY", 0) if counts else None,
        "alignment_ambiguous_cells": outcomes.get("ALIGNMENT_AMBIGUOUS", 0) if counts else None,
        "added_without_reference_support": outcomes.get("ADDED_WITHOUT_REFERENCE_SUPPORT", 0) if counts else None,
        "legitimate_nulls_separate": counts.get("legitimate_nulls_not_accuracy_credit"),
        "expected_rows": sum(t["expected_rows"] for t in tables.values()) if tables else None,
        "actual_rows": sum(t["actual_rows"] for t in tables.values()) if tables else None,
        "missing_rows": counts.get("missing_table_rows"), "additional_rows": counts.get("additional_table_rows"),
        "additional_duplicates": counts.get("additional_duplicate_occurrences"),
        "critical_expected_nonnull": critical.get("expected_nonnull"), "critical_correct_guaranteed": critical.get("CORRECT", 0) if counts else None,
        "critical_incorrect": critical.get("INCORRECT", 0) if counts else None, "critical_missing": critical.get("MISSING", 0) if counts else None,
        "CER": text.get("CER"), "WER": text.get("WER"), "text_measured": text["measured"], "text_metric_reason": text.get("reason"),
        "work_seconds": performance.get("work_seconds"), "cleanup_seconds": performance.get("cleanup_seconds"),
        "total_seconds": performance.get("total_seconds"), "controller_elapsed_seconds": performance.get("controller_elapsed_seconds"),
        "combined_total_seconds_including_A": performance.get("combined_total_seconds_including_A"),
        "combined_cost_method": performance.get("combined_cost_method"), "call_count": performance.get("call_count"),
        "tokens_by_page": performance.get("tokens_by_page"), "actual_validator_status": (metric.get("actual_validator") or {}).get("status"),
        "operational_approval_measured": False, "native_publication_measured": False}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--release", type=Path, required=True)
    parser.add_argument("--release-sha256", required=True)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    need(args.execute and os.environ.get("TV_DOCUMENTARY_EVALUATE") == "ROOT_RELEASED_SAVED_OUTPUTS_ONLY", "ROOT_RELEASE_REQUIRED")
    need(sha(args.release) == args.release_sha256, "EXACT_RELEASE_PIN_REQUIRED")
    release = read(args.release)
    need(release.get("kind") == "DOCUMENTARY_EVALUATION_SAVED_OUTPUT_RELEASE_V1" and release.get("authorized") is True
         and release.get("model_admission") == "CLOSED", "SEALED_OUTPUT_RELEASE_REQUIRED")
    need(release["collector"]["sha256"] == sha(__file__), "FROZEN_COLLECTOR_REQUIRED")
    for name in ("sample_manifest", "schema", "evaluator", "output_seal"):
        checked(release[name])
    sample = read(release["sample_manifest"]["path"])
    schemas = read(release["schema"]["path"])["schemas"]
    docs = {doc["document_id"]: doc for doc in sample["documents"]}
    need(len(docs) == 30 and sample["document_count"] == 30, "EXACT_SAMPLE30_REQUIRED")
    descriptors = release["cases"]
    cases = {(item["document_id"], item["route"]): item for item in descriptors}
    need(len(descriptors) == len(cases) == 150 and set(cases) == {(identity, route) for identity in docs for route in ROUTES}, "EXACT150_CASE_MATRIX_REQUIRED")
    outputs = {}
    for key, item in cases.items():
        output = read(checked(item["result"]))
        need(output.get("document_id") == key[0] and output.get("route") == key[1], "CASE_IDENTITY_BINDING")
        need(output.get("source_sha256") == docs[key[0]]["source_sha256"], "CASE_SOURCE_BINDING")
        outputs[key] = output
    spec = importlib.util.spec_from_file_location("documentary_frozen_saved_output_evaluator", release["evaluator"]["path"])
    need(spec and spec.loader, "FROZEN_EVALUATOR_LOAD")
    evaluator = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(evaluator)
    need(not args.out.exists(), "FRESH_COLLECTOR_OUTPUT_REQUIRED_NO_OVERWRITE")
    args.out.mkdir(parents=True)
    started = time.monotonic()
    results, flat, inputs = [], [], []
    for identity, document in docs.items():
        reference_path = Path(document["reference_file"])
        need(sha(reference_path) == document["reference_sha256"], "FROZEN_REFERENCE_CHANGED")
        reference = read(reference_path)
        need(reference["document_id"] == identity and reference["source_sha256"] == document["source_sha256"], "REFERENCE_DOCUMENT_BINDING")
        evaluator.validate_reference(reference, schemas[document["type"]])
        inputs.append(ref(reference_path))
        for route in ROUTES:
            need(re.fullmatch(r"[A-Za-z0-9_-]+", identity), "SAFE_DOCUMENT_OUTPUT_NAME")
            output = transport_wrapper(outputs[(identity, route)], adapter=outputs[(identity, "CONTROL_CURRENT_INTEGRATION")], source_route=route)
            reference_metadata = {**reference, "synthetic": True, "design_id": document["design_id"]}
            metric = evaluator.evaluate(reference_metadata, output, schemas[document["type"]])
            metric["input_result"] = cases[(identity, route)]["result"]
            metric["collector_transport_projections"] = output["collector_transport_projections"]
            metric["row_counts_across_different_tables_not_independent_quality_rate"] = True
            saved = save(args.out / (identity + "." + route + ".metrics.private.json"), metric)
            results.append({"document_id": identity, "route": route, "result": saved})
            flat.append(flat_row(document, output, metric))
    status_counts = defaultdict(Counter)
    for row in flat:
        key = (row["document_type"], row["design_id"], row["modality"], row["route"])
        status_counts[key][row["status"]] += 1
    summary = {"status": "EXACT150_SAVED_CASES_EVALUATED", "release": ref(args.release), "evaluator": release["evaluator"],
        "collector": ref(__file__), "case_count": len(flat), "scoring_calls": 150,
        "flat_rows_for_CSV_authoring": flat, "per_case_metrics": results,
        "status_by_type_design_modality_route": [{"type": key[0], "design": key[1], "modality": key[2], "route": key[3], "statuses": dict(value)} for key, value in status_counts.items()],
        "reference_inputs": inputs, "historical_evaluations_modified": False,
        "models": 0, "HTTP_SQL_calls": 0, "product_changes": 0, "native_publications": 0,
        "operational_approvals": 0, "CSV_files_authored": 0, "elapsed_seconds": time.monotonic() - started,
        "no_global_precision_combines_types_or_pairs_or_source_methods": True}
    saved = save(args.out / "summary.private.json", summary)
    for item in descriptors:
        checked(item["result"])
    for item in inputs:
        checked(item)
    receipt = save(args.out / "receipt.private.json", {"status": summary["status"], "summary": saved,
        "scoring_calls": 150, "models_HTTP_SQL_product_calls": 0, "all_input_outputs_references_unchanged": True})
    print(json.dumps({"status": summary["status"], "summary": saved, "receipt": receipt}))


if __name__ == "__main__":
    main()
