"""Read-only original ZIP inventory and reproducible private inference benchmark.

Expected values are loaded only by the evaluator after inference completes. The
engine receives an immutable original and separately supplied family definitions.
No publication or human correction occurs here; browser/native workflow proofs
must be recorded separately. Output can contain business data: keep it outside Git.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import re
import signal
import statistics
import tempfile
import threading
import time
import unicodedata
import zipfile
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal, InvalidOperation
from pathlib import Path
from types import SimpleNamespace

HEADER_ALIASES = {"invoice_number": "numero_factura", "issue_date": "fecha_emision",
    "due_date": "fecha_vencimiento", "purchase_order": "orden_compra", "currency": "moneda",
    "issuer.name": "emisor", "buyer.name": "adquiriente", "issuer.nit": "nit_emisor",
    "buyer.nit": "nit_adquiriente", "totals.taxable_base": "base_imponible",
    "totals.line_discounts": "descuento", "totals.invoice_total": "total"}
LINE_ALIASES = {"unidad": "unit", "cantidad": "quantity", "valor_unitario": "unit_price",
    "descuento_pct": "discount_pct", "impuesto_pct": "tax_pct"}
NUMERIC_LINES = {"cantidad", "valor_unitario", "descuento_pct", "impuesto_pct", "line_base", "line_total", "tax_amount"}


def digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
        separators=(",", ":"), allow_nan=False).encode()).hexdigest()


def save(path, value):
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8")
    temporary.replace(path)


def document_kind(doc):
    return "scanned" if doc["kind"] in {"scanned", "escaneada"} else doc["kind"]


def inspect_corpus(path):
    with zipfile.ZipFile(path) as archive:
        names = archive.namelist()
        if len(names) != len(set(names)):
            raise ValueError("CORPUS_DUPLICATE_ZIP_ENTRY")
        oracle_bytes = archive.read("oraculo_100_facturas.json")
        oracle = json.loads(oracle_bytes)
        documents = oracle["invoices"]
        if any(not isinstance(doc.get("id"), str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,120}", doc["id"]) for doc in documents):
            raise ValueError("CORPUS_DOCUMENT_ID_INVALID")
        actual = {name for name in names if name.lower().endswith(".pdf")}
        if (len(documents) != 100 or len(actual) != 100
                or {doc["relative_path"] for doc in documents} != actual
                or len({doc["id"] for doc in documents}) != 100
                or Counter(document_kind(doc) for doc in documents) != {"digital": 50, "scanned": 50}
                or len({doc["family_id"] for doc in documents}) != 5):
            raise ValueError("CORPUS_ORIGINAL_POPULATION_INVALID")
        sources = []
        for doc in documents:
            original = archive.read(doc["relative_path"])
            if hashlib.sha256(original).hexdigest() != doc["sha256"] or len(original) != doc["file_size"]:
                raise ValueError("CORPUS_SOURCE_INTEGRITY_FAILURE")
            sources.append({"id": doc["id"], "sha256": doc["sha256"], "bytes": len(original),
                "family": doc["family_id"], "kind": document_kind(doc), "pages": doc["page_count"]})
    with path.open("rb") as stream:
        zip_hash = hashlib.file_digest(stream, "sha256").hexdigest()
    return oracle, {"status": "PASS", "zip_sha256": zip_hash,
        "oracle_sha256": hashlib.sha256(oracle_bytes).hexdigest(), "documents": len(sources),
        "families": dict(Counter(doc["family"] for doc in sources)),
        "kinds": dict(Counter(doc["kind"] for doc in sources)),
        "pages": sum(doc["pages"] for doc in sources), "expected_rows": sum(doc["line_count"] for doc in documents),
        "sources": sources, "read_only": True, "replacement_documents": 0}


def normalized(value):
    return " ".join(unicodedata.normalize("NFKC", str(value)).split()) if value is not None else None


def equal(actual, expected, numeric=False):
    if numeric:
        try:
            a, b = Decimal(str(actual)), Decimal(str(expected))
            return a.is_finite() and b.is_finite() and a == b
        except InvalidOperation:
            return False
    return normalized(actual) == normalized(expected)


def nested(value, path):
    for key in path.split("."):
        value = value[key]
    return value


def row_values(row):
    concept = row.get("concepto", row.get("descripcion_codigo", {})).get("value")
    split = re.match(r"^([A-Z0-9]+(?:-[A-Z0-9]+)+)\s+(.+)$", normalized(concept) or "")
    result = {key: cell.get("value") for key, cell in row.items()}
    if split:
        result.update(code=split[1], description=split[2])
    return result


def evaluate(payload, doc, definition):
    """Reuse 0.9.7 oracle mappings, with separate positional and identity checks.

    Numeric interpretation follows the declared field type. Percentage STRINGs
    retain the literal percent suffix; the evaluator never coerces them to reach
    a higher score. Row identity must be unique before cells can count as exact.
    """
    fields = {field["name"]: field for field in definition["fields"]}
    table = next((item for item in definition["tables"] if item["name"] == "detalle"), {"fields": []})
    row_fields = {field["name"]: field for field in table["fields"]}
    checks = []
    for path in doc["visible_header_fields"]:
        key = path.replace("totals.", "").replace(".", "_")
        if key not in fields:
            key = HEADER_ALIASES.get(path, key)
        value = payload.get("fields", {}).get(key, {}).get("value")
        field = fields.get(key, {})
        expected = nested(doc["expected"], path)
        checks.append({"path": "headers." + path, "correct": equal(value, expected,
            field.get("logical_type") in {"DECIMAL", "INT64"}),
            "critical": path.startswith("totals.") or path.endswith("nit") or path in {"invoice_number", "issue_date", "due_date"}})
    rows = [row_values(row) for row in payload.get("tables", {}).get("detalle", [])]
    expected_rows = doc["expected"]["lines"]
    expected_counts = Counter(row["code"] for row in expected_rows)
    actual_counts = Counter(row.get("code") for row in rows)
    exact_rows, positional_exact = 0, 0
    for index, expected_row in enumerate(expected_rows):
        code = expected_row["code"]
        matches = [row for row in rows if row.get("code") == code]
        row = matches[0] if len(matches) == 1 and expected_counts[code] == 1 else {}
        row_ok = True
        for key in doc["visible_line_fields"]:
            expected = expected_row[LINE_ALIASES.get(key, key)]
            numeric = row_fields.get(key, {}).get("logical_type") in {"DECIMAL", "INT64"}
            if key in {"descuento_pct", "impuesto_pct"} and not numeric:
                expected = str(expected) + "%"
            correct = equal(row.get(key), expected, numeric)
            positional_exact += index < len(rows) and equal(rows[index].get(key), expected, numeric)
            row_ok &= correct
            checks.append({"path": f"detalle.{index}.{key}", "correct": correct,
                "critical": key in NUMERIC_LINES or key == "code"})
        exact_rows += row_ok
    missing = sum((expected_counts - actual_counts).values())
    extra = sum((actual_counts - expected_counts).values())
    duplicate = sum(max(0, count - max(1, expected_counts.get(code, 0))) for code, count in actual_counts.items())
    unresolved = sum(len(values) for values in payload.get("table_unparsed", {}).values())
    return {"id": doc["id"], "family": doc["family_id"], "kind": document_kind(doc), "pages": doc["page_count"],
        "cells": len(checks), "exact_cells": sum(check["correct"] for check in checks),
        "header_fields": len(doc["visible_header_fields"]),
        "exact_header_fields": sum(check["correct"] for check in checks if check["path"].startswith("headers.")),
        "positional_detail_exact_cells": positional_exact, "expected_rows": len(expected_rows),
        "actual_rows": len(rows), "exact_rows": exact_rows, "missing_rows": missing,
        "extra_rows": extra, "duplicate_rows": duplicate, "unparsed_regions": unresolved,
        "critical_errors": sum(not check["correct"] and check["critical"] for check in checks),
        "document_exact": all(check["correct"] for check in checks) and not (missing or extra or unresolved),
        "table_exact": exact_rows == len(expected_rows) and not (missing or extra or unresolved),
        "failed_paths": [check["path"] for check in checks if not check["correct"]],
        "human_corrections": 0}


def percentile(values, fraction):
    ordered = sorted(values)
    return ordered[max(0, math.ceil(len(ordered) * fraction) - 1)] if ordered else None


def summarize(rows, elapsed):
    complete = [row for row in rows if row.get("status") == "PROCESSED"]
    counts = {key: sum(row.get(key, 0) for row in complete) for key in (
        "cells", "exact_cells", "header_fields", "exact_header_fields", "expected_rows", "actual_rows",
        "exact_rows", "missing_rows", "extra_rows", "duplicate_rows", "critical_errors", "pages")}
    for key in ("cells", "header_fields", "expected_rows", "pages"):
        counts[key] = sum(row.get(key, 0) for row in rows)
    processed_pages = sum(row.get("pages", 0) for row in complete)
    return {"documents": len(rows), "processed": len(complete), "failed": len(rows) - len(complete), **counts,
        "cell_accuracy": counts["exact_cells"] / counts["cells"] if counts["cells"] else None,
        "accuracy_failed_documents_count_as_zero": True,
        "unprocessed_expected_rows": sum(row.get("expected_rows", 0) for row in rows if row.get("status") != "PROCESSED"),
        "document_exact": sum(row["document_exact"] for row in complete),
        "table_exact": sum(row["table_exact"] for row in complete),
        "auto_verified": sum(row.get("auto_verified", False) for row in complete),
        "correct_auto_verified": sum(row.get("auto_verified", False) and row["document_exact"] for row in complete),
        "elapsed_seconds": elapsed, "documents_per_minute": len(complete) * 60 / elapsed if elapsed else None,
        "processed_pages": processed_pages,
        "pages_per_minute": processed_pages * 60 / elapsed if elapsed else None,
        "latency_p50_seconds": percentile([row["latency_seconds"] for row in rows], .5),
        "latency_p95_seconds": percentile([row["latency_seconds"] for row in rows], .95),
        "latency_max_seconds": max((row["latency_seconds"] for row in rows), default=None),
        "latency_mean_seconds": statistics.mean(row["latency_seconds"] for row in rows) if rows else None,
        "human_corrections": 0, "publication_measured": False}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--corpus", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--definitions", type=Path)
    parser.add_argument("--mode", choices=["inventory", "legacy", "paddle", "paddle-qwen"], default="inventory")
    parser.add_argument("--limit", type=int, choices=[5, 10, 25, 50, 100], default=100)
    parser.add_argument("--concurrency", type=int, choices=[1, 2, 3, 4], default=1)
    parser.add_argument("--kind", choices=["all", "digital", "scanned"], default="all")
    parser.add_argument("--timeout-seconds", type=int, default=300)
    args = parser.parse_args()
    repository = Path(__file__).resolve().parents[1]
    output = args.output.resolve()
    if output.is_relative_to(repository) and not output.is_relative_to(repository / ".codex-local"):
        parser.error("Corpus results must be private and outside tracked repository paths.")
    output.mkdir(parents=True, exist_ok=False)
    oracle, inventory = inspect_corpus(args.corpus)
    save(output / "inventory.json", inventory)
    if args.mode == "inventory":
        print(json.dumps({k: v for k, v in inventory.items() if k != "sources"}))
        return
    if args.definitions is None:
        parser.error("--definitions is required; oracle values never configure inference.")
    from trackvance.document_local_inference import run_local
    from trackvance.document_providers import EngineConfiguration
    from trackvance.document_quality import certify_candidate, quality_report
    from trackvance.document_worker import run_engine
    from trackvance.operations_common import OperationError
    definitions = json.loads(args.definitions.read_text(encoding="utf-8"))
    selected = [doc for doc in oracle["invoices"] if args.kind == "all" or document_kind(doc) == args.kind][:args.limit]
    configuration = EngineConfiguration(provider="PADDLE_QWEN_LOCAL", use_llm=args.mode == "paddle-qwen",
        timeout_seconds=args.timeout_seconds, max_parallel_jobs=1).model_dump()
    began = time.monotonic()
    stopped = threading.Event()
    signal.signal(signal.SIGTERM, lambda *_: stopped.set())
    signal.signal(signal.SIGINT, lambda *_: stopped.set())

    def checkpoint(_progress):
        if stopped.is_set():
            raise OperationError(409, "DOCUMENT_CANCELLED", "El benchmark recibió una interrupción segura.")

    def run(doc):
        execution_began = time.monotonic()
        definition = definitions[doc["family_id"]]
        phases = {}
        try:
            checkpoint(0)
            with tempfile.TemporaryDirectory(prefix="idp-corpus-") as directory:
                source = Path(directory) / "document.pdf"
                with zipfile.ZipFile(args.corpus) as archive:
                    source.write_bytes(archive.read(doc["relative_path"]))
                if args.mode == "legacy":
                    result = run_engine(source, definition, check=checkpoint)
                else:
                    result = run_local(source, definition, configuration, legacy_runner=run_engine, check=checkpoint)
                phases["extraction_seconds"] = time.monotonic() - execution_began
                save(output / (doc["id"] + ".private.json"), result)
                # Attestation must be computed by the independent worker validator.
                control_began = time.monotonic()
                validation = None
                try:
                    independent = run_engine(source, definition, check=checkpoint)
                    validation = certify_candidate(result, independent, definition, doc["sha256"])
                    result["engine"]["validation"] = validation
                except OperationError as error:
                    if error.status != 422:
                        raise
                    result["engine"]["validation_failure"] = {"error_code": error.code, "automatic_approval_blocked": True}
                phases["independent_validation_seconds"] = time.monotonic() - control_began
                quality = quality_report(SimpleNamespace(payload=result["payload"], engine=result["engine"], reviewed=False), definition)
                row = evaluate(result["payload"], doc, definition)
                row.update(status="PROCESSED", auto_verified=quality["status"] == "AUTO_VERIFIED",
                    validation=validation, quality=quality, engine=result["engine"], source_sha256=doc["sha256"])
        except Exception as error:  # noqa: BLE001 -- preserve each genuine failure and continue the remaining original corpus.
            row = {"id": doc["id"], "family": doc["family_id"], "kind": document_kind(doc),
                "status": "FAILED", "error_type": type(error).__name__, "error_code": getattr(error, "code", None),
                "cells": len(doc["visible_header_fields"]) + len(doc["visible_line_fields"]) * doc["line_count"],
                "header_fields": len(doc["visible_header_fields"]), "expected_rows": doc["line_count"], "pages": doc["page_count"]}
        row["latency_seconds"] = time.monotonic() - began
        row["processing_seconds"] = time.monotonic() - execution_began
        row["queue_seconds"] = execution_began - began
        row["phases"] = phases
        save(output / (doc["id"] + ".metrics.json"), row)
        print(json.dumps({"id": row["id"], "status": row["status"], "latency_seconds": row["latency_seconds"]}), flush=True)
        return row

    with ThreadPoolExecutor(max_workers=args.concurrency) as pool:
        rows = list(pool.map(run, selected))
    report = {"schema_version": 1, "mode": args.mode, "client_concurrency": args.concurrency,
        "gpu_effective_concurrency_limit": 1, "configuration": configuration,
        "definitions_sha256": digest(definitions), "oracle_sha256": inventory["oracle_sha256"],
        "summary": summarize(rows, time.monotonic() - began), "full_corpus": len(selected) == 100,
        "by_kind": {kind: summarize([row for row in rows if row["kind"] == kind], time.monotonic() - began)
                    for kind in {row["kind"] for row in rows}},
        "by_family": {family: summarize([row for row in rows if row["family"] == family], time.monotonic() - began)
                      for family in {row["family"] for row in rows}}, "results": rows,
        "statistical_limitation": "Fixed corpus, not a random population sample; percentages do not guarantee production accuracy."}
    save(output / "result.json", report)
    print(json.dumps(report["summary"]))
    if report["summary"]["processed"] != len(selected):
        raise SystemExit(2)


if __name__ == "__main__":
    main()
