#!/usr/bin/env python3
"""Guarded Local JVM suite and complete million-row Spark modules in Docker."""

from __future__ import annotations

import argparse
import json
import time
from pathlib import Path
from uuid import uuid4

from spark_docker_cycle import memory_bytes
from spark_docker_guard import (
    IMAGE,
    OWNER_LABEL,
    ROOT,
    assert_population,
    assert_tests,
    docker,
    prepare_evidence,
    protected_inventory,
    remove_owned,
    sanitize_log,
    source_fingerprint,
)


def execute(evidence: Path, rows: int, *, parity_only=False):
    from ci_images import verified_images
    images = verified_images()
    image = images["backend"] if images else IMAGE
    evidence = prepare_evidence(evidence)
    context = docker("context", "show").strip()
    baseline = protected_inventory()
    prefix = "trackvance-v070-test-spark-" + uuid4().hex[:12]
    from ci.local_resources import register_project
    register_project(prefix)
    network, volume, driver = prefix + "-network", prefix + "-data", prefix + "-driver"
    containers: list[str] = []
    made_network = made_volume = False
    summary = {"status": "FAILED", "deployment_mode": "LOCAL", "prefix": prefix,
               "image": image, "image_id": docker("image", "inspect", "--format", "{{.Id}}", image).strip(),
               "source_fingerprint": source_fingerprint(), "rows": rows, "parity_only": parity_only,
               "memory_limit_bytes": 3 * 1024**3, "cpu_limit": 2, "pids_limit": 512,
               "sampled_docker_working_set_peak_bytes": 0, "cgroup_peak_bytes": 0,
               "protected_main_inventory_unchanged": False, "cleanup_finished": False}
    started = time.monotonic()
    try:
        docker("network", "create", "--label", OWNER_LABEL + "=" + prefix, network)
        made_network = True
        docker("volume", "create", "--label", OWNER_LABEL + "=" + prefix, volume)
        made_volume = True
        command = ("uv sync --frozen --group dev --no-install-project && "
                   "python -m pytest tests/test_spark_engine.py tests/test_spark_service_e2e.py -q "
                   "--basetemp=/var/lib/trackvance/parity --junitxml=/var/lib/trackvance/parity.xml")
        if not parity_only:
            command += f" && python /runner.py --root /var/lib/trackvance/million --rows {rows}"
        containers.append(driver)
        docker("run", "--detach", "--name", driver, "--label", OWNER_LABEL + "=" + prefix,
               "--network", network, "--user", "0", "--memory", "3g", "--cpus", "2", "--pids-limit", "512",
               "--mount", f"type=bind,src={ROOT / 'backend' / 'src'},dst=/app/backend/src,readonly",
               "--mount", f"type=bind,src={ROOT / 'backend' / 'tests'},dst=/app/backend/tests,readonly",
               "--mount", f"type=bind,src={ROOT / 'scripts' / 'tests' / 'spark_cycle.py'},dst=/runner.py,readonly",
               "--mount", f"type=volume,src={volume},dst=/var/lib/trackvance",
               "-e", "TRACKVANCE_SPARK_TESTS=1", "-e", "TRACKVANCE_SPARK_MASTER=local[2]",
               "-e", "PYTHONPATH=/app/backend/src", "-e", "POLARS_MAX_THREADS=1", "-e", "TMPDIR=/var/lib/trackvance",
               image, "bash", "-c", command)
        deadline, last_update = time.monotonic() + 3600, 0.0
        while time.monotonic() < deadline:
            if docker("context", "show").strip() != context:
                raise RuntimeError("Docker context changed during the proof")
            state = json.loads(docker("inspect", "--format", "{{json .State}}", driver))
            if not state["Running"]:
                summary.update(driver_exit_code=state["ExitCode"], oom_killed=state["OOMKilled"])
                break
            stats = json.loads(docker("stats", "--no-stream", "--format", "{{json .}}", driver))
            summary["sampled_docker_working_set_peak_bytes"] = max(summary["sampled_docker_working_set_peak_bytes"],
                                                                  memory_bytes(stats["MemUsage"]))
            raw = docker("exec", driver, "cat", "/sys/fs/cgroup/memory.peak", check=False).strip()
            if raw.isdigit():
                summary["cgroup_peak_bytes"] = max(summary["cgroup_peak_bytes"], int(raw))
            if time.monotonic() - last_update > 30:
                print(json.dumps({"status": "RUNNING", "mode": "LOCAL", "rows": rows, "parity_only": parity_only,
                                  "elapsed_seconds": round(time.monotonic() - started)}), flush=True)
                last_update = time.monotonic()
            time.sleep(1)
        else:
            raise RuntimeError("Local proof exceeded the one-hour harness deadline")
        (evidence / "driver.log").write_text(sanitize_log(docker("logs", driver, check=False)), encoding="utf-8")
        if summary.get("driver_exit_code") != 0 or summary.get("oom_killed"):
            raise RuntimeError("Local driver failed; review sanitized synthetic-fixture logs")
        docker("cp", driver + ":/var/lib/trackvance/parity.xml", str(evidence / "parity.xml"))
        summary["tests"] = assert_tests(evidence / "parity.xml")
        if not parity_only:
            docker("cp", driver + ":/var/lib/trackvance/million/evidence.json", str(evidence / "evidence.json"))
            measured = assert_population(evidence / "evidence.json", rows, "LOCAL")
            summary["cgroup_peak_bytes"] = max(summary["cgroup_peak_bytes"],
                *(outcome["driver_container_sampled_peak_bytes"] for outcome in measured["outcomes"].values()))
        summary["status"] = "PASS"
    finally:
        if containers:
            (evidence / "driver.log").write_text(sanitize_log(docker("logs", driver, check=False)), encoding="utf-8")
        try:
            if docker("context", "show").strip() != context:
                raise RuntimeError("Docker context changed; cleanup refused")
            remove_owned(containers, network if made_network else None, volume if made_volume else None, prefix)
            summary["cleanup_finished"] = True
            summary["protected_main_inventory_unchanged"] = protected_inventory() == baseline
            if not summary["protected_main_inventory_unchanged"]:
                summary["status"] = "FAILED"
                raise RuntimeError("Protected installation inventory changed during the proof")
        except Exception:
            summary["status"] = "FAILED"
            raise
        finally:
            summary["elapsed_seconds"] = round(time.monotonic() - started, 3)
            (evidence / "docker-summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
    print(json.dumps(summary), flush=True)
    return summary


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--evidence", type=Path, required=True)
    parser.add_argument("--rows", type=int, default=1000000)
    parser.add_argument("--parity-only", action="store_true")
    options = parser.parse_args()
    if not 1 <= options.rows <= 5000000:
        parser.error("--rows must be between 1 and 5,000,000")
    execute(options.evidence, options.rows, parity_only=options.parity_only)


if __name__ == "__main__":
    main()
