"""Finite profile for owned Catalog/Report download tests only."""
from __future__ import annotations

import json
import math
import os
from pathlib import Path

from ci.local_resources import apply_limits, memory_bytes

PROFILE = {
    "postgres": (512, 0.20), "api": (2048, 0.84), "worker": (2048, 0.45),
    "acquisition-worker": (1536, 0.35), "report-worker": (1536, 1.00),
    "web": (128, 0.10), "delivery-worker": (128, 0.01),
    "scheduler": (64, 0.02), "events-notifications": (64, 0.02),
    "events-chaining": (64, 0.01),
}
MEMORY_BUDGET = 8 * 1024**3
CPU_BUDGET = 3
HOST_CLIENT_CPUS = 1
DOCKER_CLIENT_CPU_BUDGET = CPU_BUDGET + HOST_CLIENT_CPUS


def validate(override: dict) -> dict:
    """Reject incomplete shapes, lost API memory and unbounded aggregate caps."""
    services = override["services"]
    if set(services) != set(PROFILE):
        raise ValueError("OWNED_REPORT_RESOURCE_TOPOLOGY")
    memory, cpus = 0, 0.0
    for name, (mib, service_cpu_ceiling) in PROFILE.items():
        service = services[name]
        actual_memory = memory_bytes(service["mem_limit"])
        actual_cpu = float(service["cpus"])
        if (actual_memory != mib * 1024**2 or not math.isfinite(actual_cpu)
                or not math.isclose(actual_cpu, service_cpu_ceiling, abs_tol=0.000001)):
            raise ValueError("PENDING_CAPACITY: OWNED_REPORT_PROFILE_REQUIRES_8128_MIB")
        memory += actual_memory
        cpus += actual_cpu
        if name not in {"postgres", "web"}:
            env = service.get("environment", {})
            if env.get("REPORT_CONCURRENCY") != "1":
                raise ValueError("OWNED_REPORT_CONCURRENCY")
            if env.get("REPORT_MEMORY_MB") != "512" or env.get("REPORT_PROCESS_MEMORY_MB") != "2048":
                raise ValueError("OWNED_REPORT_EXECUTOR_BUDGET")
    if memory > MEMORY_BUDGET or cpus > CPU_BUDGET + 0.000001:
        raise ValueError("OWNED_REPORT_AGGREGATE_BUDGET")
    return {"profile": "OWNED_CATALOG_DOWNLOAD_085", "status": "PASS",
            "memory_bytes": memory, "memory_ceiling_bytes": MEMORY_BUDGET,
            "cpus": round(cpus, 6), "cpu_ceiling": CPU_BUDGET, "concurrency": 1,
            "host_client_cpu_ceiling": HOST_CLIENT_CPUS,
            "docker_plus_client_cpu_budget": DOCKER_CLIENT_CPU_BUDGET,
            "cpu_budget_scope": "OWNED_DOCKER_AND_DOWNLOAD_CLIENT",
            "controller_monitor_infrastructure_excluded": True,
            "api_memory_bytes": services["api"]["mem_limit"],
            "engine_memory_mib": 512, "executor_process_memory_mib": 2048,
            "capacity_claim": "FINITE_CONFIG_ONLY_REAL_PEAK_MEASUREMENT_REQUIRED"}


def configure(directory: Path, context: dict, limit: int) -> dict:
    """Call before startup; all resource/config mutations stay in owned Compose."""
    if (not context["project"].startswith("trackvance-v080-test-")
            or context["project"] == context["main_project"]):
        raise ValueError("OWNED_REPORT_CONTEXT_REQUIRED")
    if os.getenv("TRACKVANCE_LOCAL_EXECUTION_ID"):
        runner_cpus = float(os.environ["TRACKVANCE_LOCAL_MAX_CPUS"])
        if not math.isfinite(runner_cpus) or runner_cpus < DOCKER_CLIENT_CPU_BUDGET:
            raise ValueError("PENDING_CAPACITY: OWNED_REPORT_PROFILE_REQUIRES_MAX_CPUS_4")
    path = directory / "compose.json"
    override = json.loads(path.read_text(encoding="utf-8"))
    if set(override["services"]) != set(PROFILE):
        raise ValueError("OWNED_REPORT_RESOURCE_TOPOLOGY")
    for name, (mib, cpus) in PROFILE.items():
        service = override["services"][name]
        service.update(mem_limit=mib * 1024**2, cpus=cpus)
        if name not in {"postgres", "web"}:
            service["environment"].update(REPORT_DOWNLOAD_MAX_ROWS=str(limit),
                REPORT_XLSX_MAX_ROWS=str(limit), REPORT_CONCURRENCY="1",
                REPORT_MEMORY_MB="512", REPORT_PROCESS_MEMORY_MB="2048")
    apply_limits(override, context["project"])
    receipt = validate(override)  # Never write a scaled-down API cgroup as valid.
    path.write_text(json.dumps(override, indent=2) + "\n", encoding="utf-8")
    (directory / "report-resource-profile-085.json").write_text(
        json.dumps(receipt, indent=2) + "\n", encoding="utf-8")
    return receipt


def effective_cgroups(override: dict, project: str, required_services: set[str], *, command=None) -> dict:
    """Verify owned kernel cgroup v2 limits before any certification query."""
    import certification_v080 as guard

    validate(override)
    if not guard.PROJECT.fullmatch(project) or not required_services <= PROFILE.keys():
        raise ValueError("OWNED_REPORT_CGROUP_SCOPE")
    command = command or guard.command
    identifiers = command(["docker", "ps", "-q", "--no-trunc", "--filter", "label=com.docker.compose.project=" + project]).split()
    if not identifiers:
        raise ValueError("OWNED_REPORT_CGROUP_CONTAINERS_MISSING")
    containers = json.loads(command(["docker", "inspect", *identifiers]))
    observed = {}
    for row in containers:
        labels = row.get("Config", {}).get("Labels", {})
        name = labels.get("com.docker.compose.service")
        identifier = row.get("Id")
        if (labels.get("com.docker.compose.project") != project or name not in PROFILE
                or name in observed or identifier not in identifiers or not row.get("State", {}).get("Running")):
            raise ValueError("OWNED_REPORT_CGROUP_IDENTITY")
        mib, cpus = PROFILE[name]
        host = row["HostConfig"]
        if host.get("Memory") != mib * 1024**2 or host.get("NanoCpus") != round(cpus * 10**9):
            raise ValueError("OWNED_REPORT_CGROUP_HOSTCONFIG_MISMATCH")
        values = command(["docker", "exec", identifier, "cat", "/sys/fs/cgroup/cpu.max", "/sys/fs/cgroup/memory.max"]).split()
        if len(values) != 3 or any(not value.isdigit() for value in values):
            raise ValueError("OWNED_REPORT_CGROUP_UNBOUNDED")
        quota, period, memory = map(int, values)
        if period <= 0 or memory != mib * 1024**2 or not math.isclose(quota / period, cpus, abs_tol=0.000001):
            raise ValueError("OWNED_REPORT_CGROUP_KERNEL_MISMATCH")
        observed[name] = {"memory_bytes": memory, "cpu_quota": quota, "cpu_period": period, "cpus": cpus}
    if not required_services <= observed.keys():
        raise ValueError("OWNED_REPORT_CGROUP_SERVICES_MISSING")
    return {"status": "PASS", "scope": "OWNED_RUNNING_KERNEL_CGROUP_V2_LIMITS", "services": observed,
            "configured_docker_cpu_ceiling": CPU_BUDGET, "host_client_cpu_ceiling": HOST_CLIENT_CPUS,
            "docker_plus_client_cpu_budget": DOCKER_CLIENT_CPU_BUDGET,
            "controller_monitor_infrastructure_excluded": True}
