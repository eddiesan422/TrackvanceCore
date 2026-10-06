"""An explicit real exit-23 fault exercises owned Docker cleanup, not product assertions."""
from __future__ import annotations

import json
import time
from uuid import uuid4


def run(directory, environment):
    from ci_images import verified_images

    from ci.local_resources import register_project
    from ci.owned_cleanup import cleanup, docker, snapshot
    from ci.run_suite import PhaseFailed, execute, now

    images = verified_images(environment)
    if not images:
        raise ValueError("Failure cleanup probe requires verified immutable local images")
    directory.mkdir()
    project = "trackvance-v070-test-failure-" + uuid4().hex[:12]
    register_project(project, environment)
    before = snapshot()
    began = time.monotonic()
    result = {"status": "FAIL", "kind": "CONTROLLED_DOCKER_FAILURE_CLEANUP", "project": project,
              "source_sha": environment["TRACKVANCE_SOURCE_SHA"], "started_at": now(),
              "origin": "local", "execution_id": environment["TRACKVANCE_LOCAL_EXECUTION_ID"],
              "expected_exit_code": 23, "runtime_image": images["backend"]}
    label = "com.docker.compose.project=" + project
    volume, network = project + "_marker", project + "_network"
    try:
        execute(["docker", "volume", "create", "--label", label, volume], directory, "probe-volume", 90, environment=environment)
        execute(["docker", "network", "create", "--label", label, network], directory, "probe-network", 90, environment=environment)
        try:
            execute(["docker", "run", "--name", project + "-fault", "--label", label, "--network", network,
                     "--cpus", "0.5", "--memory", "512m", "--memory-swap", "512m", "--pids-limit", "128",
                     "--mount", f"type=volume,src={volume},dst=/var/lib/trackvance", images["backend"],
                     "python", "-c", "from pathlib import Path;Path('/var/lib/trackvance/fault.marker').write_text('synthetic');raise SystemExit(23)"],
                    directory, "probe-expected-failure", 90, environment=environment)
        except PhaseFailed as error:
            result["observed_phase"] = error.record
            if error.record["exit_code"] != 23 or error.record["timed_out"] or error.record["protected_state_changed"]:
                raise
        else:
            raise ValueError("Controlled Docker failure did not fail as required")
    finally:
        (directory / "result.json").write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
        result["cleanup"] = cleanup(before, projects={project})
        remaining = {kind: docker(kind, "ls", "-q", *(["-a"] if kind == "container" else []), "--filter", "label=" + label).split()
                     for kind in ("container", "volume", "network")}
        result["remaining_owned_resources"] = remaining
        result.update(completed_at=now(), duration_seconds=round(time.monotonic() - began, 3))
        phase = result.get("observed_phase", {})
        result["status"] = "PASS" if phase.get("exit_code") == 23 and not phase.get("protected_state_changed") and not phase.get("timed_out") and not any(remaining.values()) else "FAIL"
        (directory / "result.json").write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    if result["status"] != "PASS":
        raise ValueError("Controlled failure cleanup was incomplete")
    return result
