"""Existing pure integration replay of authentic saved recognition events.

No event construction, attestation, database, HTTP, models or publication. Raw
Qwen diagnostic replies without the existing recognition contract are unsupported.
Known invoice projection is a declared field-name projection, not a new parser.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace
import sys
import time


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")


def load(path):
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError("DUPLICATE_JSON_KEY")
            result[key] = value
        return result
    def constant(_value):
        raise ValueError("NONFINITE_JSON_CONSTANT")
    raw = Path(path).read_bytes()
    try:
        return json.loads(raw, object_pairs_hook=pairs, parse_constant=constant)
    except json.JSONDecodeError:
        return [json.loads(line, object_pairs_hook=pairs, parse_constant=constant) for line in raw.splitlines() if line]


def ref(path):
    data = Path(path).read_bytes()
    return {"path": str(Path(path).resolve()), "sha256": hashlib.sha256(data).hexdigest(), "bytes": len(data)}


def project_known_invoice(payload, definition):
    source_fields = payload.get("fields", {})
    mapping = {"number": "invoice_number", "date": "issue_date", "issuer": "issuer_name", "buyer": "buyer_name",
               "currency": "currency", "subtotal": "subtotal", "total": "invoice_total"}
    if not set(mapping.values()).issubset(source_fields) or "detalle" not in payload.get("tables", {}):
        return None
    def value(cell):
        return cell.get("value") if isinstance(cell, dict) else None
    fields = {name: value(source_fields[key]) for name, key in mapping.items()}
    fields["tax_total"] = None  # No arithmetic-derived field is invented.
    lines = [{"description": value(row.get("concepto")), "quantity": value(row.get("cantidad")),
              "unit_price": value(row.get("valor_unitario")), "amount": value(row.get("line_base"))}
             for row in payload["tables"]["detalle"]]
    definitions = {field["name"]: field for field in definition["fields"]}
    taxes = []
    for source_name, rate in (("vat_5", "5"), ("vat_19", "19")):
        cell = source_fields.get(source_name)
        if cell and (cell.get("observed") is not None or cell.get("value") is not None):
            taxes.append({"name": definitions[source_name].get("label") or None, "rate": rate, "amount": value(cell)})
    return {"fields": fields, "tables": {"lines": lines, "taxes": taxes}}


def replay(*, document_id, source_sha256, events_path, definition_path, configuration_path, worktree):
    started = time.monotonic()
    result = {"document_id": document_id, "route": "CONTROL_CURRENT_INTEGRATION", "source_route": "A_PADDLE",
              "source_sha256": source_sha256, "semantic": None, "recognized_text": None,
              "models": 0, "HTTP_SQL_calls": 0, "events_fabricated": False,
              "operational_approval_measured": False, "native_publication_measured": False,
              "configuration_class": "EXISTING_KNOWN_LAYOUT_SPECIFIC_CONFIGURATION",
              "inputs": {"events": ref(events_path), "definition": ref(definition_path), "configuration": ref(configuration_path)}}
    events, definition, configuration = load(events_path), load(definition_path), load(configuration_path)
    if isinstance(events, dict):
        events = events.get("events")
    if not isinstance(events, list) or not events or not all(isinstance(event, dict) and "event_sha256" in event for event in events):
        result.update(status="UNSUPPORTED", reason="NO_AUTHENTIC_EXISTING_RECOGNITION_EVENTS",
                      semantic_reason="DIAGNOSTIC_JSON_CANNOT_BE_WRAPPED_AS_NATIVE_PROVENANCE")
        return result
    source_root = Path(worktree) / "backend/src"
    sys.path.insert(0, str(source_root))
    try:
        from trackvance.document_lossless import adapt_saved_recognition
        from trackvance.document_quality import quality_report
        adapted = adapt_saved_recognition(events, definition, configuration, source_sha256=source_sha256)
        result["adapted"] = adapted
        result["semantic"] = project_known_invoice(adapted["payload"], definition)
        extraction = SimpleNamespace(payload=adapted["payload"], engine=adapted["engine"], reviewed=False)
        result["validation"] = quality_report(extraction, definition)
        result["validation_scope"] = "REAL_VALIDATOR_FUNCTION_ON_DIAGNOSTIC_REPLAY_NOT_OPERATIONAL_APPROVAL"
        result["status"] = "COMPLETE" if adapted["engine"]["execution"]["recognition_complete"] else "PARTIAL"
        if result["semantic"] is None:
            result["semantic_reason"] = "EXISTING_ADAPTER_RESULT_DOES_NOT_SUPPORT_REQUESTED_SEMANTIC_PROJECTION"
        result["projection_policy"] = {"version": "KNOWN_INVOICE_FIELD_PROJECTION_V1",
            "tax_name_rate_from_known_declared_field_semantics_not_new_visual_recognition": True,
            "no_derived_tax_total": True, "concepto_preserved_entire_without_SKU_stripping": True,
            "line_amount": "EXISTING_LINE_BASE_VALUE", "new_unknown_layout_template": False}
        modules = ["document_lossless.py", "document_normalization.py", "document_quality.py", "document_validation_policy.py",
                   "document_representation.py", "document_candidate_selection.py", "document_observation_uncertainty.py"]
        result["existing_integration_source_sha256"] = {name: hashlib.sha256((source_root / "trackvance" / name).read_bytes()).hexdigest() for name in modules}
    except Exception as error:
        result.update(status="UNSUPPORTED" if isinstance(error, ImportError) else "ERROR",
                      reason=type(error).__name__ + ":" + str(error),
                      attribution=[{"layer": "ADAPTATION_ASSOCIATION_OR_CONTRACT", "cause": type(error).__name__,
                                    "model_failure_claimed": False}])
    result["metrics"] = {"work_seconds": time.monotonic() - started, "timing_kind": "NEW_CPU_REPLAY_SAVED_RECOGNITION",
                         "call_count": 0, "RAM_sampled": None, "VRAM_sampled": None}
    result["inputs_unchanged"] = {name: ref(path) == result["inputs"][name] for name, path in
                                  (("events", events_path), ("definition", definition_path), ("configuration", configuration_path))}
    return result


def main():
    parser = argparse.ArgumentParser()
    for name in ("document-id", "source-sha256"):
        parser.add_argument("--" + name, required=True)
    for name in ("events", "definition", "configuration", "worktree", "output"):
        parser.add_argument("--" + name, type=Path, required=True)
    args = parser.parse_args()
    result = replay(document_id=args.document_id, source_sha256=args.source_sha256, events_path=args.events,
                    definition_path=args.definition, configuration_path=args.configuration, worktree=args.worktree)
    with args.output.open("xb") as stream:
        stream.write(canonical(result) + b"\n")
    print(json.dumps({"status": result["status"], "document_id": args.document_id, "output": str(args.output)}, ensure_ascii=False))


if __name__ == "__main__":
    main()
