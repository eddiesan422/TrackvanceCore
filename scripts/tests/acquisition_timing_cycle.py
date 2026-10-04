#!/usr/bin/env python3
"""Measure additional CSV acquisition stages and the complete private volume chain.

Uses existing, hashed host fixtures with fresh datasets and SQL targets. It never
starts/stops services or changes existing DatasetVersions. Stage timings are
interval-censored observations, not exact worker timings. The measured integral
includes verification pauses performed by this certification runner.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import subprocess
import sys
import time
from datetime import UTC, datetime
from itertools import pairwise
from pathlib import Path
from uuid import UUID, uuid4

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(Path(__file__).parent))
import certification_v070 as certification
import volume_cycle as volume

POLL_SECONDS = 0.5
SIZES = (100, 500, 1024)
STAGE_GROUPS = {"READING": "READING_AND_MATERIALIZING", "MATERIALIZING": "READING_AND_MATERIALIZING"}
IDENTITY_FIELDS = ("id", "version", "sha256", "schema_hash", "canonical_artifact_id", "schema", "profile", "ingestion_metadata")


def digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()


def planned_inputs(directory, context, baseline, sizes):
    """Validate all identities and paths before any API or Docker interaction."""
    if baseline.get("project") != context["project"] or baseline.get("rows") != 1_000_000:
        raise ValueError("La evidencia base no pertenece al proyecto y población aislados.")
    if len(sizes) != len(set(sizes)) or any(size not in SIZES for size in sizes):
        raise ValueError("Los tiers deben ser 100, 500 o 1024 MiB, sin duplicados.")
    fixture_root = (directory / "volume-fixtures").resolve()
    selected = []
    for size in sizes:
        tier = next((item for item in baseline["tiers"] if item["target_mib"] == size), None)
        if tier is None or tier["status"] != "PASS":
            raise ValueError("El tier original debe tener evidencia PASS.")
        identifier = tier["formats"]["CSV"]["version_id"]
        UUID(identifier)
        manifest = fixture_root / f"volume-{size}mib-1000000-varied.json"
        fixture = json.loads(manifest.read_text(encoding="utf-8"))
        path = Path(fixture["formats"]["CSV"]["path"]).resolve()
        if not path.is_relative_to(fixture_root) or path.name != f"volume-{size}mib-1000000-varied.csv":
            raise ValueError("La fixture debe permanecer dentro de la carpeta privada propia.")
        if fixture["target_mib"] != size or fixture["rows"] != 1_000_000 or fixture["mode"] != "varied":
            raise ValueError("La fixture no corresponde al tier y población certificados.")
        metadata = fixture["formats"]["CSV"]
        if path.stat().st_size != metadata["actual_bytes"]:
            raise ValueError("El tamaño de la fixture CSV cambió.")
        if fixture["canonical_rows_sha256"] != tier["formats"]["CSV"]["canonical_rows_sha256"]:
            raise ValueError("La población de la fixture no coincide con la evidencia original.")
        selected.append({"size": size, "version_id": identifier, "fixture": fixture, "path": path})
    return selected


def stage_windows(observations):
    """Bounds account for both the polling gap and each HTTP request duration.

The persisted state was read somewhere between request start and response end.
A transition therefore lies between the previous request start and the first
response showing the new state. READING/MATERIALIZING are grouped because they
alternate; no unobserved brief stage is assigned a fabricated duration.
"""
    segments = []
    for observation in observations:
        stage = STAGE_GROUPS.get(observation["stage"], observation["stage"])
        if not segments or segments[-1]["stage"] != stage:
            segments.append({"stage": stage, "observations": []})
        segments[-1]["observations"].append(observation)
    result = []
    for index, segment in enumerate(segments):
        rows = segment["observations"]
        if index + 1 == len(segments):
            continue  # The terminal state has no measured exit.
        previous = segments[index - 1]["observations"][-1] if index else None
        following = segments[index + 1]["observations"][0]
        enter_lower = previous["request_started_seconds"] if previous else rows[0]["request_started_seconds"]
        enter_upper = rows[0]["response_finished_seconds"]
        exit_lower = rows[-1]["request_started_seconds"]
        exit_upper = following["response_finished_seconds"]
        result.append({"stage": segment["stage"], "samples": len(rows),
            "entry_transition_bounds_seconds": [round(enter_lower, 6), round(enter_upper, 6)],
            "exit_transition_bounds_seconds": [round(exit_lower, 6), round(exit_upper, 6)],
            "duration_lower_bound_seconds": round(max(0.0, exit_lower - enter_upper), 6),
            "duration_upper_bound_seconds": round(max(0.0, exit_upper - enter_lower), 6),
            "stable_observed_window_seconds": [round(enter_upper, 6), round(max(enter_upper, exit_lower), 6)],
            "duration_is_exact": False})
    return result


def resource_summary(samples):
    """Phase samples use sampled peaks; lifetime memory.peak is never phase peak."""
    services = {}
    for sample in samples:
        for name, values in sample["services"].items():
            entry = services.setdefault(name, {"samples": [], "rss_peak_bytes": 0,
                "cgroup_sample_peak_bytes": 0, "temporary_peak_bytes": 0, "spill_peak_bytes": 0})
            entry["samples"].append(values)
            for destination, source in [("rss_peak_bytes", "process_sum_rss_bytes"),
                    ("cgroup_sample_peak_bytes", "memory.current"), ("temporary_peak_bytes", "temporary_bytes"),
                    ("spill_peak_bytes", "spill_bytes")]:
                entry[destination] = max(entry[destination], values.get(source, 0))
    for entry in services.values():
        values = entry.pop("samples")
        complete = len(values) >= 2 and all(row.get("container_id") == values[0].get("container_id") for row in values)
        delta = int(values[-1].get("cpu", {}).get("usage_usec", 0)) - int(values[0].get("cpu", {}).get("usage_usec", 0))
        complete = complete and delta >= 0
        entry["cpu_seconds"] = round(delta / 1_000_000, 6) if complete else None
        entry["cpu_measurement_complete"] = complete
        entry["cpu_coverage_scope"] = "CGROUP_COUNTER_DELTA_BETWEEN_FIRST_AND_LAST_RETAINED_PROBES"
        entry["cpu_incomplete_reason"] = None if complete else "INSUFFICIENT_SAMPLES_OR_COUNTER_RESET"
        entry["memory_events_delta"] = {key: int(values[-1].get("memory_events", {}).get(key, 0)) - int(values[0].get("memory_events", {}).get(key, 0))
            for key in set(values[0].get("memory_events", {})) | set(values[-1].get("memory_events", {}))} if complete else None
    return {"samples": len(samples), "services": services,
        "combined_cgroup_sample_peak_bytes": max((sum(row.get("memory.current", 0) for row in sample["services"].values()) for sample in samples), default=None),
        "minimum_host_disk_free_bytes": min((sample["disk_free_bytes"] for sample in samples if sample["disk_free_bytes"] is not None), default=None),
        "method": "SEQUENTIAL_CONTAINER_PROC_RSS_CGROUP_V2_CPU_DISK_SPILL_2S",
        "sampled_combined_peak_is_not_atomic": True, "lifetime_memory_peak_used": False}


def resources_in_stable_window(samples, start, end):
    selected = []
    for sample in samples:
        services = {name: value for name, value in sample["services"].items()
            if start <= value.get("metric_read_started_at", -1) <= value.get("metric_read_finished_at", -1) <= end}
        if services:
            selected.append({**sample, "services": services,
                "disk_free_bytes": sample["disk_free_bytes"] if start <= sample["at"] <= end else None})
    return resource_summary(selected)


def observe_acquisition(api, run, clock, metrics, registration):
    observations = [{**registration, "stage": run["stage"], "status": run["status"],
        "processed_rows": run["processed_rows"], "processed_bytes": run["processed_bytes"]}]
    started = time.monotonic()
    last_group = None
    while time.monotonic() - started < 1800:
        metrics.check()
        requested = time.monotonic()
        current = api.get("/api/v1/acquisitions/" + run["id"])
        completed = time.monotonic()
        observations.append({"request_started_seconds": requested - clock, "response_finished_seconds": completed - clock,
            "stage": current["stage"], "status": current["status"], "processed_rows": current["processed_rows"],
            "processed_bytes": current["processed_bytes"]})
        group = STAGE_GROUPS.get(current["stage"], current["stage"])
        if group != last_group:
            print(json.dumps({"status": current["status"], "acquisition_stage": group,
                "processed_rows": current["processed_rows"]}), flush=True)
            last_group = group
        if current["status"] in volume.TERMINAL:
            if current["status"] != "SUCCESS":
                raise RuntimeError("Adquisición adicional fallida: " + str(current.get("error_code")))
            windows = stage_windows(observations)
            profile = next((item for item in windows if item["stage"] == "PROFILING"), None)
            if profile is None or profile["duration_lower_bound_seconds"] <= 0:
                raise RuntimeError("No se observó una ventana separada de perfil global.")
            return current, observations, windows
        time.sleep(max(0.0, POLL_SECONDS - (completed - requested)))
    raise TimeoutError("La adquisición adicional excedió el timeout.")


def quiescence(directory, context, wait_seconds=0):
    script = """import json
from sqlalchemy import text
from trackvance.db import engine
queries={'jobs':"SELECT count(*) FROM jobs WHERE status IN ('QUEUED','RUNNING')",
'acquisitions':"SELECT count(*) FROM acquisition_runs WHERE status IN ('QUEUED','RUNNING')",
'runs':"SELECT count(*) FROM runs WHERE status IN ('QUEUED','RUNNING')",
'job_leases':'SELECT count(*) FROM jobs WHERE lease_until > CURRENT_TIMESTAMP',
'event_leases':'SELECT count(*) FROM event_consumptions WHERE lease_until > CURRENT_TIMESTAMP'}
with engine.connect() as connection:
 print(json.dumps({name:connection.scalar(text(sql)) for name,sql in queries.items()}))
"""
    deadline = time.monotonic() + wait_seconds
    while True:
        counts = json.loads(certification.compose(directory, context, ["exec", "-T", "api", "python", "-c", script]))
        if not any(counts.values()):
            return counts
        if time.monotonic() >= deadline:
            raise RuntimeError("El proyecto privado tiene trabajos o leases activos.")
        time.sleep(1)


def run(args):
    directory, context = certification.load_context(args.context)
    baseline = json.loads(args.baseline.read_text(encoding="utf-8"))
    selected = planned_inputs(directory, context, baseline, args.sizes)
    if not args.with_chain and not args.prepare_only:
        raise ValueError("La medición integral exige --with-chain.")
    nonce = uuid4().hex[:12]
    destination_path = directory / "evidence" / f"acquisition-timing-{nonce}.json"
    report = {"schema_version": 1, "status": "PREPARED_ONLY" if args.prepare_only else "RUNNING",
        "project": context["project"], "certification_namespace": nonce,
        "poll_interval_seconds": POLL_SECONDS, "tiers": [],
        "stage_timing_method": "INTERVAL_CENSORED_PERSISTED_STAGE_HTTP_OBSERVATIONS",
        "integral_wall_scope": "HTTP_RECEIVE_TO_INTAKE_APPROVED_CHAINED_DELIVERY_COMMITTED_PERSONAL_INBOX_WITH_CERTIFICATION_VERIFICATION_PAUSES",
        "phase_sums_are_not_integral_walltime": True}
    if args.prepare_only:
        report["planned_tiers"] = [{"target_mib": item["size"], "original_version_id": item["version_id"],
            "csv_filename": item["path"].name, "csv_actual_bytes": item["path"].stat().st_size} for item in selected]
        print(json.dumps(report))
        return report
    certification.assert_main_unchanged(context)
    report["initial_quiescence"] = quiescence(directory, context)
    report["runner_sha256"] = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
    report["volume_helper_sha256"] = hashlib.sha256(Path(volume.__file__).read_bytes()).hexdigest()
    report["git_sha"] = subprocess.run(["git", "rev-parse", "HEAD"], cwd=ROOT, capture_output=True, text=True, check=True).stdout.strip()
    volume.OWNED_OPERATIONS.clear()
    if shutil.disk_usage(ROOT).free < 12 * volume.GIB:
        raise RuntimeError("El ensayo adicional necesita 12 GiB libres, incluida la reserva.")
    api = volume.VolumeApi(f'http://127.0.0.1:{context["port"]}', 1800)
    api.csrf = api.post("/api/v1/auth/demo", {}, expected=(200,))["csrf_token"]
    limits = api.get("/api/v1/system/engines")["limits"]["acquisition"]
    if limits["memory_bytes"] != 256 * volume.MIB or limits["batch_bytes"] > 8 * volume.MIB or limits["batch_rows"] > 5000:
        raise RuntimeError("El ensayo exige perfil 256 MiB y lotes como máximo 5000 filas/8 MiB.")
    if any(item["path"].stat().st_size > limits["max_upload_bytes"] for item in selected):
        raise RuntimeError("Una fixture supera el límite de recepción del proyecto privado.")
    report["effective_limits"] = limits
    volume.RESOURCE_MEMORY_BUDGET = 6 * volume.GIB
    probe_template = volume.PROBE.replace("import json, pathlib, os", "import json, pathlib, os, time\nmetric_started=time.time()")
    destination, _, schema, _ = volume.destination_fixture(api, directory, context, nonce)
    try:
        for item in selected:
            fixture, path = item["fixture"], item["path"]
            with path.open("rb") as source:
                if hashlib.file_digest(source, "sha256").hexdigest() != fixture["formats"]["CSV"]["sha256"]:
                    raise RuntimeError("La fixture CSV cambió antes de recibirla.")
            original = api.get("/api/v1/dataset-versions/" + item["version_id"] + "/profile")
            identity = {key: original.get(key) for key in IDENTITY_FIELDS}
            tier = {"target_mib": item["size"], "status": "RUNNING", "original_version_id": item["version_id"],
                "csv_actual_bytes": path.stat().st_size, "source_sha256": fixture["formats"]["CSV"]["sha256"],
                "expected_rows": fixture["rows"], "expected_canonical_rows_sha256": fixture["canonical_rows_sha256"], "phases": {}}
            report["tiers"].append(tier)
            print(json.dumps({"target_mib": item["size"], "status": "RUNNING",
                "csv_actual_bytes": path.stat().st_size}), flush=True)
            metrics = volume.Measurements(directory, context)
            started_at = datetime.now(UTC).isoformat()
            # Count only spill directories touched after this additional cycle
            # starts. Artifacts and abandoned historical spill are untouched.
            probe_started = time.time()
            volume.PROBE = probe_template.replace("print(json.dumps(result))", f"""result['spill_bytes']=0
for directory in (STORAGE_DIR/'tmp').glob('*.spill'):
 try:
  if directory.stat().st_ctime >= {probe_started!r}:
   result['spill_bytes']+=sum(p.stat().st_size for p in directory.rglob('*') if p.is_file())
 except OSError: pass
result['metric_read_started_at']=metric_started
result['metric_read_finished_at']=time.time()
print(json.dumps(result))""")
            with metrics:
                clock, wallclock = time.monotonic(), time.time()
                before = time.monotonic()
                staged = api.binary(path)
                tier["http_receive_seconds"] = round(time.monotonic() - before, 6)
                before = time.monotonic()
                dataset = api.post("/api/v1/datasets", {"name": f'Timing {item["size"]}MiB {nonce}'})
                register_started = time.monotonic()
                run = api.keyed(f'/datasets/{dataset["id"]}/acquisitions', {"upload_id": staged["upload"]["id"]}, "timing-" + uuid4().hex)
                register_finished = time.monotonic()
                if run["output_version_id"] is not None:
                    raise RuntimeError("La petición202 publicó una versión antes de devolver el registro durable.")
                volume.OWNED_OPERATIONS.append("/acquisitions/" + run["id"])
                tier["http_create_register_seconds"] = round(time.monotonic() - before, 6)
                completed, observations, windows = observe_acquisition(api, run, clock, metrics, {
                    "request_started_seconds": register_started - clock, "response_finished_seconds": register_finished - clock})
                tier["durable_acquisition_wait_wall_seconds"] = round(time.monotonic() - register_finished, 6)
                tier["acquisition_success_seconds_from_receive_start"] = round(time.monotonic() - clock, 6)
                tier.update(dataset_id=dataset["id"], acquisition_id=run["id"], version_id=completed["output_version_id"],
                    observations=observations, stage_windows=windows)
                tier["maximum_observation_gap_seconds"] = round(max((second["request_started_seconds"] - first["request_started_seconds"]
                    for first, second in pairwise(observations)), default=0.0), 6)
                tier["maximum_observation_request_seconds"] = round(max(row["response_finished_seconds"] - row["request_started_seconds"] for row in observations), 6)
                before = time.monotonic()
                created = api.get("/api/v1/dataset-versions/" + completed["output_version_id"] + "/profile")
                volume.validate_profile(created, fixture)
                if created["profile"] != original["profile"] or created["schema"] != original["schema"] or created["schema_hash"] != original["schema_hash"]:
                    raise RuntimeError("El perfil completo o el esquema difiere de la versión certificada original.")
                tier["complete_persisted_profile_equal"] = "PASS"
                tier["profile_sha256"] = digest(created["profile"])
                verified = volume.materialized_hash(directory, context, completed["output_version_id"])
                if verified["rows"] != fixture["rows"] or verified["canonical_rows_sha256"] != fixture["canonical_rows_sha256"]:
                    raise RuntimeError("La adquisición adicional cambió la población completa.")
                tier["source"] = verified
                tier["source_complete_verification_seconds"] = round(time.monotonic() - before, 6)
                volume.chain(api, directory, context, fixture, dataset, completed, destination, schema, tier)
                tier["integral_wall_seconds"] = round(time.monotonic() - clock, 6)
                tier["integral_started_at_utc"], tier["integral_finished_at_utc"] = started_at, datetime.now(UTC).isoformat()
            metrics.check()
            tier["integral_resources"] = resource_summary(metrics.samples)
            tier["integral_resources"]["measurement_errors"] = metrics.errors
            for window in windows:
                lower, upper = window["stable_observed_window_seconds"]
                window["resources_stable_observed_window"] = resources_in_stable_window(metrics.samples, wallclock + lower, wallclock + upper)
            tier["chain_phase_elapsed_sum_seconds"] = round(sum(phase["elapsed_seconds"] for phase in tier["phases"].values()), 6)
            unchanged = api.get("/api/v1/dataset-versions/" + item["version_id"] + "/profile")
            if identity != {key: unchanged.get(key) for key in IDENTITY_FIELDS}:
                raise RuntimeError("La versión original cambió durante el ensayo adicional.")
            tier["original_version_unchanged"] = "PASS"
            tier["status"] = "PASS"
            destination_path.parent.mkdir(parents=True, exist_ok=True)
            destination_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
            print(json.dumps({"target_mib": item["size"], "status": "PASS",
                "integral_wall_seconds": tier["integral_wall_seconds"]}), flush=True)
        report["final_quiescence"] = quiescence(directory, context, wait_seconds=30)
        certification.assert_main_unchanged(context)
        report.update(status="PASS", main_inventory="UNCHANGED")
    except Exception as error:
        report.update(status="FAIL", error_type=type(error).__name__)
        # Cancel only subjects registered by this invocation, never historical jobs.
        for owned in volume.OWNED_OPERATIONS:
            try:
                if api.get("/api/v1" + owned).get("status") in {"QUEUED", "RUNNING"}:
                    api.post("/api/v1" + owned + "/cancel", {}, expected=(200, 202))
            except (RuntimeError, OSError, ValueError):
                report.setdefault("cleanup_errors", []).append("OWNED_OPERATION_CANCEL_UNAVAILABLE")
        raise
    finally:
        destination_path.parent.mkdir(parents=True, exist_ok=True)
        destination_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        certification.assert_main_unchanged(context)
    print(json.dumps({"status": report["status"], "evidence": str(destination_path),
        "tiers": [{key: tier[key] for key in ["target_mib", "status", "integral_wall_seconds", "complete_persisted_profile_equal"]} for tier in report["tiers"]]}))
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--context", type=Path, required=True)
    parser.add_argument("--baseline", type=Path, required=True)
    parser.add_argument("--sizes", type=int, nargs="+", default=list(SIZES))
    parser.add_argument("--with-chain", action="store_true")
    parser.add_argument("--prepare-only", action="store_true")
    run(parser.parse_args())


if __name__ == "__main__":
    main()
