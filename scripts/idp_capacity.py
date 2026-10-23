"""Empirical IDP sizing from real benchmark results, with explicit assumptions.

This is an estimate for comparable hardware, not a production guarantee or AWS
homologation. Failed runs cannot supply a processing rate. A fixed corpus is not
a random population sample; quality and sample limitations remain in the output.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
from pathlib import Path


def load(path):
    raw = path.read_bytes()
    return json.loads(raw), {"path": str(path.resolve()), "sha256": hashlib.sha256(raw).hexdigest()}


def capacity(results, *, documents_per_user_per_day, window_hours, target_utilization,
             retention_days, artifact_multiplier, telemetry=None, average_original_bytes=None):
    if (not math.isfinite(documents_per_user_per_day) or documents_per_user_per_day <= 0
            or not 0 < window_hours <= 24 or not 0 < target_utilization < 1
            or not 1 <= retention_days <= 3650 or not 1 <= artifact_multiplier <= 20):
        raise ValueError("IDP_CAPACITY_ASSUMPTIONS_INVALID")
    eligible = []
    for result in results:
        summary = result.get("summary", {})
        count, seconds = summary.get("processed", 0), summary.get("elapsed_seconds", 0)
        if (summary.get("failed") != 0 or count <= 0 or count != summary.get("documents")
                or not isinstance(seconds, (int, float)) or not math.isfinite(seconds) or seconds <= 0):
            continue
        eligible.append((count / seconds, result))
    if not eligible:
        raise ValueError("IDP_CAPACITY_NO_COMPLETE_MEASURED_RUN")
    # Use the slowest completed workload profile; partial or failed throughput
    # cannot silently raise the sizing rate. Profiles and quality stay visible.
    documents_per_second, reference = min(eligible, key=lambda item: item[0])
    sample = reference["summary"]
    sampled = (telemetry or {}).get("summary", {})
    cpu_peak = sampled.get("aggregate_cpu_cores_peak")
    ram_peak = sampled.get("aggregate_ram_bytes_peak")
    vram_peak = sampled.get("device_vram_used_mib_peak")
    cpu_allocation = max(2, math.ceil(cpu_peak * 1.25)) if isinstance(cpu_peak, (int, float)) and cpu_peak > 0 else None
    ram_allocation = max(4, math.ceil(ram_peak / 1024**3 * 1.25)) if isinstance(ram_peak, (int, float)) and ram_peak > 0 else None
    gpu_observed = any(row.get("engine", {}).get("inference_skipped") is False
        or row.get("engine", {}).get("route") in {"PADDLE", "PADDLE_QWEN"} for row in reference.get("results", []))
    peak_vram = math.ceil(vram_peak / 1024) if isinstance(vram_peak, (int, float)) and vram_peak > 0 else None
    scenarios = []
    sustained_rate = documents_per_second * 3600 * target_utilization
    for users in (10, 50, 100, 500, 1000):
        daily_documents = users * documents_per_user_per_day
        demand_per_hour = daily_documents / window_hours
        replicas = math.ceil(demand_per_hour / sustained_rate)
        storage = (daily_documents * retention_days * average_original_bytes * artifact_multiplier / 1024**3
            if average_original_bytes is not None else None)
        scenarios.append({"users": users, "documents_per_day": daily_documents,
            "required_documents_per_hour": demand_per_hour, "estimated_equivalent_replicas": replicas,
            "estimated_sustainable_documents_per_hour": replicas * sustained_rate,
            "estimated_cpu_cores": replicas * cpu_allocation if cpu_allocation else None,
            "estimated_ram_gib": replicas * ram_allocation if ram_allocation else None,
            "estimated_gpus": replicas if gpu_observed else 0,
            "observed_device_vram_gib_per_gpu": peak_vram if gpu_observed else None,
            "estimated_retained_storage_gib": round(storage, 2) if storage is not None else None,
            "local": "Comparable measured hardware; verify sustained concurrency and storage separately.",
            "future_cloud": "Equivalent hardware estimate only; cloud OCR/storage integration and rates are not homologated."})
    return {"schema_version": 1, "status": "EMPIRICAL_ESTIMATE",
        "assumptions": {"documents_per_user_per_day": documents_per_user_per_day, "processing_window_hours": window_hours,
            "target_utilization": target_utilization, "retention_days": retention_days,
            "artifact_storage_multiplier": artifact_multiplier, "average_original_bytes": average_original_bytes,
            "cpu_ram_headroom_multiplier": 1.25, "replica_scaling": "Linear assumption, not measured cross-node scaling."},
        "reference": {"mode": reference.get("mode"), "documents": sample["documents"],
            "client_concurrency": reference.get("client_concurrency"), "elapsed_seconds": sample["elapsed_seconds"],
            "measured_documents_per_minute": documents_per_second * 60,
            "cell_accuracy": sample.get("cell_accuracy"), "critical_errors": sample.get("critical_errors"),
            "auto_approval_eligible": sample.get("auto_approval_eligible", sample.get("auto_verified")),
            "correct_auto_approval_eligible": sample.get("correct_auto_approval_eligible", sample.get("correct_auto_verified")),
            "native_approval_measured": False, "native_auto_approved": None, "publication_measured": False,
            "policy_effective_counts": sample.get("policy_effective_counts", {}),
            "full_corpus": reference.get("full_corpus", False),
            "all_supplied_runs_eligible": len(eligible) == len(results)},
        "scenarios": scenarios,
        "limitations": ["Observed quality is reported unchanged; sizing does not certify accuracy or acceptance.",
            "Automatic-approval eligibility is independent-validator eligibility only; CLI runs do not measure native worker approval or publication. The effective template policy may still require human review.",
            "Resource peaks are interval samples, not guaranteed maxima; whole-device GPU telemetry includes other processes.",
            "Model weights, database growth, backup replicas and disaster recovery capacity require separate storage budgets.",
            "Cold/warm status, mixed document complexity and inference concurrency must match the measured profile.",
            "A fixed corpus cannot establish population accuracy or production service levels."]}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--result", type=Path, action="append", required=True)
    parser.add_argument("--telemetry", type=Path)
    parser.add_argument("--inventory", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--documents-per-user-per-day", type=float, default=5)
    parser.add_argument("--window-hours", type=float, default=8)
    parser.add_argument("--target-utilization", type=float, default=.6)
    parser.add_argument("--retention-days", type=int, default=365)
    parser.add_argument("--artifact-multiplier", type=float, default=2)
    args = parser.parse_args()
    results, provenance = [], []
    for path in args.result:
        value, source = load(path)
        results.append(value)
        provenance.append(source)
    telemetry, average = None, None
    if args.telemetry:
        telemetry, source = load(args.telemetry)
        provenance.append(source)
    if args.inventory:
        inventory, source = load(args.inventory)
        provenance.append(source)
        sources = inventory.get("sources", [])
        if sources:
            average = sum(item["bytes"] for item in sources) / len(sources)
    report = capacity(results, documents_per_user_per_day=args.documents_per_user_per_day,
        window_hours=args.window_hours, target_utilization=args.target_utilization,
        retention_days=args.retention_days, artifact_multiplier=args.artifact_multiplier,
        telemetry=telemetry, average_original_bytes=average)
    report["provenance"] = provenance
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("x", encoding="utf-8") as stream:
        stream.write(json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False) + "\n")
    print(json.dumps({"status": report["status"], "scenarios": report["scenarios"]}))


if __name__ == "__main__":
    main()
