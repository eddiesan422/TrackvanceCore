#!/usr/bin/env python3
"""Isolated two-executor Standalone proof; cleans only UUID-owned resources."""

from __future__ import annotations

import argparse
import json
import re
import time
from pathlib import Path
from uuid import uuid4

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


def memory_bytes(raw):
    value = raw.split(" / ", 1)[0]
    match = re.fullmatch(r"([0-9.]+)(B|KiB|MiB|GiB)", value)
    if not match:
        raise ValueError("Unexpected Docker memory unit")
    return int(float(match[1]) * {"B": 1, "KiB": 1024, "MiB": 1024**2, "GiB": 1024**3}[match[2]])


def observed_population_mode(measured):
    """Publish only the common mode observed in all verified module runtimes."""
    outcomes = measured.get("outcomes", {}) if isinstance(measured, dict) else {}
    if not isinstance(outcomes, dict) or set(outcomes) != {"intake", "recon", "sentinel"}:
        raise RuntimeError("SPARK_DEPLOYMENT_MODE_NOT_OBSERVED")
    modes = []
    for module in ("intake", "recon", "sentinel"):
        outcome = outcomes[module]
        runtime = outcome.get("runtime") if isinstance(outcome, dict) else None
        mode = runtime.get("deployment_mode") if isinstance(runtime, dict) else None
        if not isinstance(mode, str) or mode != "STANDALONE_CLIENT":
            raise RuntimeError("SPARK_DEPLOYMENT_MODE_NOT_OBSERVED")
        modes.append(mode)
    return modes[0]


def execute(evidence: Path, rows: int, *, parity_only: bool = False):
    from ci_images import verified_images
    images = verified_images()
    image = images["backend"] if images else IMAGE
    evidence = prepare_evidence(evidence)
    context = docker("context", "show").strip()
    baseline = protected_inventory()
    prefix = "trackvance-v070-test-spark-" + uuid4().hex[:12]
    from ci.local_resources import register_project
    register_project(prefix)
    network, volume = prefix + "-network", prefix + "-data"
    created_containers = []
    made_network = made_volume = False
    summary = {"status": "FAILED", "prefix": prefix, "image": image, "rows": rows,
               "parity_only": parity_only,
               "image_id": docker("image", "inspect", "--format", "{{.Id}}", image).strip(),
               "source_fingerprint": source_fingerprint(),
               "protected_main_inventory_unchanged": False, "cleanup_finished": False,
               "memory_limits_bytes": {"master": 512 * 1024**2, "executor1": 1536 * 1024**2,
                                       "executor2": 1536 * 1024**2, "driver": 2 * 1024**3},
               "sampled_docker_working_set_peak_bytes": {}, "cgroup_peak_bytes": {}}
    code_mount = f"type=bind,src={ROOT / 'backend' / 'src'},dst=/app/backend/src,readonly"
    started = time.monotonic()
    try:
        docker("network", "create", "--label", OWNER_LABEL + "=" + prefix, network)
        made_network = True
        docker("volume", "create", "--label", OWNER_LABEL + "=" + prefix, volume)
        made_volume = True

        def start(role, command, memory, cpus, environment=()):
            name = prefix + "-" + role
            arguments = ["run", "--detach", "--name", name, "--network", network,
                "--label", OWNER_LABEL + "=" + prefix,
                "--network-alias", role, "--user", "0", "--memory", memory,
                "--cpus", cpus, "--pids-limit", "512", "--mount", code_mount,
                "--mount", f"type=volume,src={volume},dst=/var/lib/trackvance",
                "-e", "PYTHONPATH=/app/backend/src", "-e", "POLARS_MAX_THREADS=1"]
            for value in environment:
                arguments.extend(["-e", value])
            if role == "driver":
                arguments.extend(["--mount", f"type=bind,src={ROOT / 'backend' / 'tests'},dst=/app/backend/tests,readonly",
                                  "--mount", f"type=bind,src={ROOT / 'scripts' / 'tests' / 'spark_cycle.py'},dst=/runner.py,readonly"])
            created_containers.append(name)
            docker(*arguments, image, *command)
            return name

        java_launch = "import os,pyspark; os.execv(pyspark.__path__[0]+'/bin/spark-class', ['spark-class',"
        master = start("master", ["python", "-c", java_launch +
            "'org.apache.spark.deploy.master.Master','--host','master','--port','7077'])"], "512m", "0.5",
            ["SPARK_DAEMON_MEMORY=256m"])
        for role in ("executor1", "executor2"):
            start(role, ["python", "-c", java_launch +
                "'org.apache.spark.deploy.worker.Worker','--host','" + role +
                "','--cores','1','--memory','768m','spark://master:7077'])"], "1536m", "1",
                ["SPARK_DAEMON_MEMORY=256m"])
        # Master admin endpoint is available only in the private Docker network.
        readiness = ("import json,time,urllib.request; "
                     "time.sleep(2); data=json.load(urllib.request.urlopen('http://master:8080/json/',timeout=3)); "
                     "assert len(data['workers'])==2 and all(w['state']=='ALIVE' for w in data['workers'])")
        ready_deadline = time.monotonic() + 90
        while time.monotonic() < ready_deadline:
            try:
                docker("exec", master, "python", "-c", readiness)
                break
            except RuntimeError:
                time.sleep(1)
        else:
            raise RuntimeError("Standalone executors did not register before the deadline")
        driver = start("driver", ["bash", "-c",
            ("uv sync --frozen --group dev --no-install-project && "
            "python -m pytest tests/test_spark_engine.py tests/test_spark_service_e2e.py -q --basetemp=/var/lib/trackvance/parity-tmp --junitxml=/var/lib/trackvance/parity.xml"
            + ("" if parity_only else f" && python /runner.py --root /var/lib/trackvance/million --rows {rows}"))], "2g", "2", [
                "TMPDIR=/var/lib/trackvance",
                "TRACKVANCE_SPARK_TESTS=1", "TRACKVANCE_SPARK_MASTER=spark://master:7077",
                "TRACKVANCE_SPARK_DRIVER_HOST=driver", "TRACKVANCE_SPARK_DRIVER_BIND_ADDRESS=0.0.0.0",
                "TRACKVANCE_SPARK_DRIVER_PORT=7078", "TRACKVANCE_SPARK_BLOCK_MANAGER_PORT=7079",
                "TRACKVANCE_SPARK_MEMORY_BUDGET_BYTES=3221225472"])
        last_update = 0.0
        deadline = time.monotonic() + 3600
        while time.monotonic() < deadline:
            if docker("context", "show").strip() != context:
                raise RuntimeError("Docker context changed during the proof")
            state = json.loads(docker("inspect", "--format", "{{json .State}}", driver))
            stats = docker("stats", "--no-stream", "--format", "{{json .}}", *created_containers)
            for line in stats.splitlines():
                item = json.loads(line)
                role = item["Name"].removeprefix(prefix + "-")
                value = memory_bytes(item["MemUsage"])
                previous = summary["sampled_docker_working_set_peak_bytes"].get(role, 0)
                summary["sampled_docker_working_set_peak_bytes"][role] = max(value, previous)
            if not state["Running"]:
                summary["driver_exit_code"] = state["ExitCode"]
                summary["oom_killed"] = state["OOMKilled"]
                break
            if time.monotonic() - last_update > 30:
                print(json.dumps({"status": "RUNNING", "mode": "STANDALONE_CLIENT", "rows": rows, "parity_only": parity_only,
                                  "elapsed_seconds": round(time.monotonic() - started)}), flush=True)
                last_update = time.monotonic()
            time.sleep(1)
        else:
            raise RuntimeError("Standalone proof exceeded its one-hour harness deadline")
        for name in created_containers:
            role = name.removeprefix(prefix + "-")
            (evidence / f"{role}.log").write_text(sanitize_log(docker("logs", name, check=False)), encoding="utf-8")
            state = json.loads(docker("inspect", "--format", "{{json .State}}", name))
            if state["Running"]:
                raw = docker("exec", name, "cat", "/sys/fs/cgroup/memory.peak", check=False).strip()
                if raw.isdigit():
                    summary["cgroup_peak_bytes"][role] = int(raw)
        if summary.get("driver_exit_code") != 0 or summary.get("oom_killed"):
            raise RuntimeError("Standalone driver failed; review synthetic-fixture logs")
        docker("cp", driver + ":/var/lib/trackvance/parity.xml", str(evidence / "parity.xml"))
        summary["tests"] = assert_tests(evidence / "parity.xml")
        if not parity_only:
            docker("cp", driver + ":/var/lib/trackvance/million/evidence.json", str(evidence / "evidence.json"))
            measured = assert_population(evidence / "evidence.json", rows, "STANDALONE_CLIENT")
            summary["deployment_mode"] = observed_population_mode(measured)
        summary["elapsed_seconds"] = round(time.monotonic() - started, 3)
        summary["status"] = "PASS"
    finally:
        try:
            if docker("context", "show").strip() != context:
                raise RuntimeError("Docker context changed; cleanup refused")
            remove_owned(created_containers, network if made_network else None,
                         volume if made_volume else None, prefix)
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
