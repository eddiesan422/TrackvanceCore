"""Explicit local runner ownership and an aggregate per-stack resource budget."""
from __future__ import annotations

import json
import os
import re
import subprocess
from pathlib import Path


def register_project(project: str, environment=None) -> None:
    registry = (environment or os.environ).get("TRACKVANCE_LOCAL_PROJECT_REGISTRY")
    if not registry:
        return
    if not re.fullmatch(r"trackvance-[a-z0-9-]+-[a-f0-9]{12}", project):
        raise ValueError("Local ownership requires a fresh Trackvance UUID project")
    path = Path(registry)
    projects = json.loads(path.read_text(encoding="utf-8")) if path.is_file() else []
    if project not in projects:
        projects.append(project)
        path.write_text(json.dumps(projects, indent=2) + "\n", encoding="utf-8")


def memory_bytes(value):
    if isinstance(value, int):
        return value
    match = re.fullmatch(r"([0-9.]+)([kmg]?)b?", str(value).lower())
    if not match:
        raise ValueError("Unrecognized explicit service memory budget")
    return int(float(match[1]) * 1024 ** {"": 0, "k": 1, "m": 2, "g": 3}[match[2]])


def service_closure(services: dict, requested) -> set[str]:
    pending, closure = list(requested), set()
    while pending:
        name = pending.pop()
        if name in closure:
            continue
        if name not in services:
            raise ValueError("Selected local service/dependency is absent from resolved Compose")
        closure.add(name)
        pending.extend(services[name].get("depends_on", {}))
    return closure


def apply_limits(override: dict, project: str, *, active_services=None) -> None:
    """Serial runner caps all services in the actual startup dependency closure.

    Every resolved connector database is included before applying this cap.
    When a caller selects a subset, its generated profile disables the rest.
    Limits affect only generated test Compose.
    """
    if not os.environ.get("TRACKVANCE_LOCAL_EXECUTION_ID"):
        return
    register_project(project)
    services = override["services"]
    if active_services is not None:
        if not set(active_services) <= services.keys():
            raise ValueError("The selected resource topology must be complete")
        services = {name: service for name, service in services.items() if name in active_services}
    cpu_budget = float(os.environ["TRACKVANCE_LOCAL_MAX_CPUS"])
    memory_budget = int(os.environ["TRACKVANCE_LOCAL_MAX_MEMORY_BYTES"])
    if os.environ.get("TRACKVANCE_LOCAL_GROUP") == "backup-restore":
        # Native restore owns source+target stacks and a separate 512 MiB DB.
        memory_budget = max(1024**3, (memory_budget - 512 * 1024**2) // 2)
        cpu_budget = max(0.25, (cpu_budget - 0.5) / 2)
    cpu_total = sum(float(s.get("cpus", 1)) for s in services.values())
    memory_total = sum(memory_bytes(s.get("mem_limit", "256m")) for s in services.values())
    for service in services.values():
        service["cpus"] = round(float(service.get("cpus", 1)) * min(1, cpu_budget / cpu_total), 4)
        service["mem_limit"] = int(memory_bytes(service.get("mem_limit", "256m")) * min(1, memory_budget / memory_total))
        service.setdefault("labels", {})["io.trackvance.local-execution"] = os.environ["TRACKVANCE_LOCAL_EXECUTION_ID"]


def build_legacy(compose: list[str], directory: Path, project: str, environment: dict[str, str]) -> None:
    """Cold historical images use a bounded builder in an owned local run."""
    from ci.run_suite import execute
    name = "tv-local-legacy-" + os.environ["TRACKVANCE_LOCAL_EXECUTION_ID"][-12:] + project[-12:]
    registry = Path(os.environ["TRACKVANCE_LOCAL_BUILDER_REGISTRY"])
    names = json.loads(registry.read_text(encoding="utf-8")) if registry.exists() else []
    names.append(name)
    registry.write_text(json.dumps(names) + "\n", encoding="utf-8")
    config = directory / "local-buildkit.toml"
    config.write_text('[worker.oci]\n max-parallelism = 1\n', encoding="utf-8")
    memory = min(3 * 1024**3, int(os.environ["TRACKVANCE_LOCAL_MAX_MEMORY_BYTES"]) // 2)
    quota = int(min(2, float(os.environ["TRACKVANCE_LOCAL_MAX_CPUS"]) / 2) * 100000)
    try:
        execute(["docker", "buildx", "create", "--name", name, "--driver", "docker-container",
            "--buildkitd-config", str(config), "--driver-opt", "memory=" + str(memory),
            "--driver-opt", "memory-swap=" + str(memory), "--driver-opt", "cpu-quota=" + str(quota),
            "--driver-opt", "cpu-period=100000", "--driver-opt", "restart-policy=no"], directory,
            "local-builder-create", 180, environment=environment)
        execute([*compose, "build", "--builder", name, "api", "web"], directory,
            "local-authentic-source-build", 1800, environment=environment)
    finally:
        with (directory / "local-builder-cleanup.private.log").open("w", encoding="utf-8") as output:
            subprocess.run(["docker", "buildx", "rm", name], stdout=output, stderr=subprocess.STDOUT, check=False, timeout=180)
