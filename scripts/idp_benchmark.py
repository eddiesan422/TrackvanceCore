"""Bounded IDP matrix on an explicitly owned Linux container, with host telemetry.

No container is created, stopped, reconfigured or removed. Workload and image IDs
must match exactly. Results must use a private host directory already mounted
read/write into the workload container. Safety aborts signal only the process
group this invocation created, after verifying its PID start time and token.
"""
from __future__ import annotations

import argparse
import csv
import ctypes
import io
import json
import os
import re
import signal
import subprocess
import time
import uuid
from pathlib import Path

from idp_corpus import digest, implementation_record

PROTECTED = {"trackvance-codex", "trackvance-certification", "bikerwash"}
SUPERVISE = r'''
import json, os, pathlib, subprocess, sys
token, command = sys.argv[1], json.loads(sys.argv[2])
control = pathlib.Path("/tmp/idp-benchmark-" + token + ".json")
process = subprocess.Popen(command, start_new_session=True)
try:
    start = pathlib.Path("/proc/" + str(process.pid) + "/stat").read_text().rsplit(") ", 1)[1].split()[19]
    control.write_text(json.dumps({"pid": process.pid, "start": start, "token": token}))
    raise SystemExit(process.wait())
finally:
    control.unlink(missing_ok=True)
'''
CANCEL = r'''
import json, os, pathlib, signal, sys
token = sys.argv[1]
control = pathlib.Path("/tmp/idp-benchmark-" + token + ".json")
if control.exists():
    state = json.loads(control.read_text())
    pid = state["pid"]
    proc = pathlib.Path("/proc/" + str(pid) + "/stat")
    if state["token"] == token and proc.exists() and proc.read_text().rsplit(") ", 1)[1].split()[19] == state["start"]:
        os.killpg(pid, getattr(signal, sys.argv[2]))
'''


def command(args, timeout=10):
    return subprocess.run(args, capture_output=True, text=True, check=True, timeout=timeout).stdout


def verified_host_source(repository):
    """Verify host Git metadata separately from the workload's actual file hashes."""
    repository = repository.resolve()
    prefix = ["git", "-C", str(repository)]
    root = Path(command([*prefix, "rev-parse", "--show-toplevel"]).strip()).resolve()
    sha = command([*prefix, "rev-parse", "HEAD"]).strip()
    dirty = command([*prefix, "status", "--porcelain", "--untracked-files=normal"]).strip()
    if root != repository or not re.fullmatch(r"[a-f0-9]{40}", sha) or dirty:
        raise ValueError("IDP_BENCHMARK_CLEAN_EXACT_HOST_GIT_SOURCE_REQUIRED")
    return {"source_sha": sha, "clean_git_verified_on_host": True,
        "implementation": implementation_record(repository, sha), "oci_content_certified": False}


def workload_provenance(result_path, host_source):
    result = json.loads(result_path.read_text(encoding="utf-8"))
    actual = result["implementation"]
    matched = (actual["source_sha"] == host_source["source_sha"]
        and actual["source_sha_origin"] == "CALLER_DECLARED"
        and actual["manifest_sha256"] == host_source["implementation"]["manifest_sha256"]
        and digest(actual["files"]) == actual["manifest_sha256"]
        and actual["files"] == host_source["implementation"]["files"]
        and actual["runtime_version"] == actual["version"] == host_source["implementation"]["version"]
        and actual.get("source_unchanged_during_run") is True)
    return {"actual_implementation": actual, "actual_bytes_match_verified_host": matched,
        "oci_content_certified": False}


def inspect(identity):
    if not re.fullmatch(r"[a-f0-9]{64}", identity):
        raise ValueError("IDP_BENCHMARK_REQUIRES_IMMUTABLE_CONTAINER_ID")
    rows = json.loads(command(["docker", "inspect", identity]))
    if len(rows) != 1 or rows[0]["Id"] != identity:
        raise ValueError("IDP_BENCHMARK_CONTAINER_ID_CHANGED")
    return rows[0]


def authorize_workload(row, image_id, host_output, container_output):
    labels = row["Config"].get("Labels") or {}
    name = row["Name"].lstrip("/")
    project = labels.get("com.docker.compose.project", "")
    if (any(value in name.casefold() or value in project.casefold() for value in PROTECTED)
            or labels.get("io.trackvance.permanent") is not None
            or row["Image"] != image_id or not row["State"]["Running"]
            or row["HostConfig"].get("Memory", 0) <= 0 or row["HostConfig"].get("PidsLimit", 0) <= 0
            or not (row["HostConfig"].get("NanoCpus", 0) > 0 or row["HostConfig"].get("CpuQuota", 0) > 0)):
        raise ValueError("IDP_BENCHMARK_WORKLOAD_IDENTITY_OR_LIMITS_INVALID")
    if not re.fullmatch(r"/[A-Za-z0-9_/-]+", container_output) or ".." in container_output.split("/"):
        raise ValueError("IDP_BENCHMARK_CONTAINER_OUTPUT_INVALID")
    mounts = [item for item in row.get("Mounts", []) if item.get("RW") and item.get("Type") == "bind"
        and mount_source(item.get("Source", "")) == host_output.resolve()
        and item.get("Destination", "").rstrip("/") == container_output.rstrip("/")]
    if len(mounts) != 1:
        raise ValueError("IDP_BENCHMARK_PRIVATE_OUTPUT_BIND_REQUIRED")


def mount_source(value):
    prefix = "/run/desktop/mnt/host/"
    if os.name == "nt" and value.startswith(prefix):
        relative = value.removeprefix(prefix)
        drive, _, tail = relative.partition("/")
        if re.fullmatch(r"[A-Za-z]", drive):
            value = drive + ":/" + tail
    return Path(value).resolve()


def bytes_value(value):
    match = re.fullmatch(r"\s*([0-9.]+)\s*(B|kB|KB|KiB|MB|MiB|GB|GiB|TB|TiB)\s*", value)
    if not match:
        raise ValueError("IDP_DOCKER_MEMORY_FORMAT_UNKNOWN")
    multipliers = {"B": 1, "kB": 1000, "KB": 1000, "KiB": 1024, "MB": 1000**2, "MiB": 1024**2,
        "GB": 1000**3, "GiB": 1024**3, "TB": 1000**4, "TiB": 1024**4}
    return int(float(match[1]) * multipliers[match[2]])


def host_available_bytes():
    if os.name == "nt":
        class MemoryStatus(ctypes.Structure):
            _fields_ = [("length", ctypes.c_ulong), ("load", ctypes.c_ulong),
                ("total", ctypes.c_ulonglong), ("available", ctypes.c_ulonglong),
                ("page_total", ctypes.c_ulonglong), ("page_available", ctypes.c_ulonglong),
                ("virtual_total", ctypes.c_ulonglong), ("virtual_available", ctypes.c_ulonglong),
                ("extended", ctypes.c_ulonglong)]
        state = MemoryStatus()
        state.length = ctypes.sizeof(state)
        if not ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(state)):
            raise OSError("HOST_MEMORY_STATUS_UNAVAILABLE")
        return state.available
    values = dict(line.split(":", 1) for line in Path("/proc/meminfo").read_text().splitlines())
    return int(values["MemAvailable"].strip().split()[0]) * 1024


def sample(identities):
    value = {"monotonic_seconds": time.monotonic(), "containers": [], "gpus": [], "errors": []}
    try:
        raw = command(["docker", "stats", "--no-stream", "--format", "{{json .}}", *identities], timeout=10)
        for line in raw.splitlines():
            item = json.loads(line)
            used, limit = item["MemUsage"].split("/")
            value["containers"].append({"id": item["ID"], "name": item["Name"],
                "cpu_cores": float(item["CPUPerc"].removesuffix("%")) / 100,
                "ram_bytes": bytes_value(used), "ram_limit_bytes": bytes_value(limit)})
    except (OSError, ValueError, subprocess.SubprocessError) as error:
        value["errors"].append("DOCKER_STATS:" + type(error).__name__)
    try:
        raw = command(["nvidia-smi", "--query-gpu=uuid,name,memory.total,memory.used,utilization.gpu,temperature.gpu",
            "--format=csv,noheader,nounits"], timeout=5)
        for item in csv.reader(io.StringIO(raw)):
            identity, name, total, used, utilization, temperature = (part.strip() for part in item)
            value["gpus"].append({"uuid": identity, "name": name, "vram_total_mib": float(total),
                "vram_used_mib": float(used), "utilization_percent": float(utilization), "temperature_c": float(temperature)})
    except (OSError, ValueError, subprocess.SubprocessError) as error:
        value["errors"].append("NVIDIA_SMI:" + type(error).__name__)
    try:
        value["host_available_bytes"] = host_available_bytes()
    except (OSError, ValueError) as error:
        value["errors"].append("HOST_RAM:" + type(error).__name__)
    return value


def summary(samples):
    cpu = [sum(item["cpu_cores"] for item in value["containers"]) for value in samples if value["containers"]]
    ram = [sum(item["ram_bytes"] for item in value["containers"]) for value in samples if value["containers"]]
    gpu = [item for value in samples for item in value["gpus"]]
    return {"samples": len(samples), "peak_is_sampled_not_exact": True,
        "aggregate_cpu_cores_peak": max(cpu, default=None),
        "aggregate_cpu_cores_mean": sum(cpu) / len(cpu) if cpu else None,
        "aggregate_ram_bytes_peak": max(ram, default=None),
        "aggregate_ram_bytes_mean": sum(ram) / len(ram) if ram else None,
        "device_vram_used_mib_peak": max((item["vram_used_mib"] for item in gpu), default=None),
        "device_vram_used_mib_mean": sum(item["vram_used_mib"] for item in gpu) / len(gpu) if gpu else None,
        "device_gpu_utilization_percent_mean": sum(item["utilization_percent"] for item in gpu) / len(gpu) if gpu else None,
        "host_available_bytes_minimum": min((value["host_available_bytes"] for value in samples if "host_available_bytes" in value), default=None),
        "sampling_error_count": sum(len(value["errors"]) for value in samples),
        "gpu_scope": "Whole physical device, including display and other processes; not attributed to this workload."}


def interrupt(container_id, token, force=False):
    command(["docker", "exec", container_id, "python", "-c", CANCEL, token, "SIGKILL" if force else "SIGTERM"])


def terminate_workload(process, container_id, token):
    errors = []
    for force, timeout in ((False, 30), (True, 10)):
        if process.poll() is not None:
            return True, errors
        try:
            interrupt(container_id, token, force=force)
        except (OSError, subprocess.SubprocessError) as error:
            errors.append(type(error).__name__)
        try:
            process.wait(timeout=timeout)
            return True, errors
        except subprocess.TimeoutExpired:
            continue
    # Docker control can be unavailable. Do not claim that signalling its CLI
    # cancelled the process inside the container; retain this material failure.
    return False, errors


def safety_reason(value, args):
    if value.get("host_available_bytes", 0) < args.minimum_host_free_mib * 1024**2:
        return "HOST_MEMORY_RESERVE"
    if not value["containers"]:
        return "DOCKER_TELEMETRY_UNAVAILABLE"
    if args.mode != "legacy" and not value["gpus"]:
        return "GPU_TELEMETRY_UNAVAILABLE"
    if any(item["ram_limit_bytes"] and item["ram_bytes"] / item["ram_limit_bytes"] >= .95 for item in value["containers"]):
        return "CONTAINER_MEMORY_RESERVE"
    if any(item["vram_total_mib"] - item["vram_used_mib"] < args.minimum_vram_free_mib for item in value["gpus"]):
        return "GPU_MEMORY_RESERVE"
    if any(item["temperature_c"] >= args.maximum_gpu_temperature_c for item in value["gpus"]):
        return "GPU_TEMPERATURE_LIMIT"
    return None


def save_matrix(host_output, report):
    """Each invocation preserves an exclusive report; retries never replace history."""
    path = host_output / ("benchmark-matrix-" + uuid.uuid4().hex + ".json")
    with path.open("x", encoding="utf-8") as stream:
        stream.write(json.dumps(report, indent=2, allow_nan=False) + "\n")
    return path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--container-id", required=True)
    parser.add_argument("--expected-image-id", required=True)
    parser.add_argument("--monitor-container-id", action="append", default=[])
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--container-output", required=True)
    parser.add_argument("--corpus", required=True, help="Read-only original ZIP path inside the container.")
    parser.add_argument("--definitions", required=True, help="Independent frozen definitions path inside the container.")
    parser.add_argument("--source-literals", help="Private original-source literal evidence inside the container; evaluator only.")
    parser.add_argument("--sizes", default="5")
    parser.add_argument("--concurrencies", default="1")
    parser.add_argument("--mode", choices=["legacy", "paddle", "paddle-qwen"], default="paddle-qwen")
    parser.add_argument("--kind", choices=["all", "digital", "scanned"], default="all")
    parser.add_argument("--sampling", choices=["balanced", "sequential"], default="balanced")
    parser.add_argument("--profile", choices=["COLD_REQUESTED", "WARM_REQUESTED", "UNSPECIFIED"], default="UNSPECIFIED")
    parser.add_argument("--timeout-seconds", type=int, default=300)
    parser.add_argument("--context-tokens", type=int, default=8192)
    parser.add_argument("--max-retries", type=int, choices=[0, 1, 2], default=1)
    parser.add_argument("--max-total-documents", type=int, default=100)
    parser.add_argument("--minimum-host-free-mib", type=int, default=2048)
    parser.add_argument("--minimum-vram-free-mib", type=int, default=512)
    parser.add_argument("--maximum-gpu-temperature-c", type=int, default=85)
    args = parser.parse_args()
    sizes = [int(value) for value in args.sizes.split(",")]
    concurrencies = [int(value) for value in args.concurrencies.split(",")]
    if (set(sizes) - {5, 10, 25, 50, 100} or set(concurrencies) - {1, 2, 3, 4}
            or len(set(sizes)) != len(sizes) or len(set(concurrencies)) != len(concurrencies)
            or not 1 <= args.max_total_documents <= 5000
            or sum(sizes) * len(concurrencies) > args.max_total_documents
            or not 30 <= args.timeout_seconds <= 1800 or not 1024 <= args.context_tokens <= 32768
            or args.minimum_host_free_mib < 1024
            or args.minimum_vram_free_mib < 256 or not 60 <= args.maximum_gpu_temperature_c <= 90):
        parser.error("Matrix or safety limits exceed the bounded benchmark contract.")
    repository = Path(__file__).resolve().parents[1]
    host_source = verified_host_source(repository)
    host_output = args.output.resolve()
    if host_output.is_relative_to(repository) and not host_output.is_relative_to(repository / ".codex-local"):
        parser.error("Potentially sensitive benchmark output must remain private.")
    host_output.mkdir(parents=True, exist_ok=True)
    identities = list(dict.fromkeys([args.container_id, *args.monitor_container_id]))
    inventory = [inspect(identity) for identity in identities]
    authorize_workload(inventory[0], args.expected_image_id, host_output, args.container_output)
    preflight = sample(identities)
    preflight_reason = safety_reason(preflight, args)
    if preflight_reason:
        path = save_matrix(host_output, {"status": "BLOCKED", "reason": preflight_reason, "sample": preflight})
        print(json.dumps({"report": str(path), "status": "BLOCKED"}))
        raise SystemExit(2)
    cancelled = False

    def stop(*_):
        nonlocal cancelled
        cancelled = True

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    runs = []
    for size in sizes:
        for concurrency in concurrencies:
            if cancelled:
                break
            name = f"{args.mode}-{args.kind}-{size}-c{concurrency}-{uuid.uuid4().hex[:8]}"
            token = uuid.uuid4().hex
            workload = ["python", "/source/scripts/idp_corpus.py", "--corpus", args.corpus,
                "--definitions", args.definitions, "--output", args.container_output.rstrip("/") + "/" + name,
                "--mode", args.mode, "--kind", args.kind, "--limit", str(size),
                "--concurrency", str(concurrency), "--timeout-seconds", str(args.timeout_seconds),
                "--source-sha", host_source["source_sha"], "--sampling", args.sampling,
                "--context-tokens", str(args.context_tokens), "--max-retries", str(args.max_retries)]
            if args.source_literals:
                workload.extend(["--source-literals", args.source_literals])
            samples, reason, control_errors = [], None, []
            termination_confirmed = True
            with (host_output / (name + ".log")).open("x", encoding="utf-8") as log:
                process = subprocess.Popen(["docker", "exec", "--workdir", "/source/backend",
                    "--env", "PYTHONPATH=/source/backend/src", "--env", "TRACKVANCE_BACKEND_DIR=/source/backend",
                    args.container_id, "python", "-c", SUPERVISE, token, json.dumps(workload)],
                    stdout=log, stderr=subprocess.STDOUT)
                began = time.monotonic()
                try:
                    while process.poll() is None:
                        value = sample(identities)
                        samples.append(value)
                        if cancelled:
                            reason = "USER_INTERRUPTION"
                        else:
                            reason = safety_reason(value, args)
                        if not reason and time.monotonic() - began > size * (args.timeout_seconds + 250):
                            reason = "MATRIX_RUN_DEADLINE"
                        if reason:
                            termination_confirmed, control_errors = terminate_workload(process, args.container_id, token)
                            cancelled = True
                            break
                        time.sleep(1)
                finally:
                    if process.poll() is None:
                        termination_confirmed, errors = terminate_workload(process, args.container_id, token)
                        control_errors.extend(errors)
            report = {"schema_version": 2, "run": name, "requested_profile": args.profile,
                "host_source": host_source,
                "cold_warm_verified": False, "workload_container_id": args.container_id,
                "workload_image_id": inventory[0]["Image"], "monitored_container_ids": identities,
                "requested_sample_interval_seconds": 1, "interval_includes_sampler_overhead": True,
                "elapsed_seconds": time.monotonic() - began, "exit_code": process.returncode,
                "workload_termination_confirmed": termination_confirmed, "control_errors": control_errors,
                "abort_reason": reason, "status": "ABORTED" if reason else "PASS" if process.returncode == 0 else "FAIL",
                "summary": summary(samples), "samples": samples}
            try:
                proof = workload_provenance(host_output / name / "result.json", host_source)
                unchanged_host = verified_host_source(repository) == host_source
                report["implementation_provenance"] = {**proof, "host_source_unchanged": unchanged_host}
                if not proof["actual_bytes_match_verified_host"] or not unchanged_host:
                    report["implementation_error"] = "IDP_BENCHMARK_SOURCE_CHANGED_OR_MISMATCHED"
                    if report["status"] == "PASS":
                        report["status"] = "FAIL"
            except (OSError, ValueError, KeyError, subprocess.SubprocessError) as error:
                report.update(implementation_error="IDP_BENCHMARK_PROVENANCE_UNVERIFIED",
                    implementation_error_type=type(error).__name__)
                if report["status"] == "PASS":
                    report["status"] = "FAIL"
            try:
                report["container_restart_deltas"] = {item["Id"]: inspect(item["Id"]).get("RestartCount", 0) - item.get("RestartCount", 0)
                    for item in inventory}
            except (OSError, ValueError, subprocess.SubprocessError) as error:
                report["container_restart_deltas"] = None
                report["restart_verification_error"] = type(error).__name__
            (host_output / (name + ".telemetry.json")).write_text(json.dumps(report, indent=2, allow_nan=False) + "\n", encoding="utf-8")
            runs.append({key: value for key, value in report.items() if key != "samples"})
    matrix_path = save_matrix(host_output, {"schema_version": 2, "host_source": host_source,
        "runs": runs, "cancelled": cancelled,
        "containers_preserved": True, "protected_containers_used_as_workload": False})
    print(json.dumps({"report": str(matrix_path), "runs": len(runs),
        "status": "PASS" if not cancelled and all(run["status"] == "PASS" for run in runs) else "FAIL"}))
    if cancelled or any(run["status"] != "PASS" for run in runs):
        raise SystemExit(2)


if __name__ == "__main__":
    main()
