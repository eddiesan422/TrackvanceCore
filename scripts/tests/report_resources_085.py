"""Finite profile for owned Catalog/Report download tests only."""
from __future__ import annotations

import json
import math
from pathlib import Path

from ci.local_resources import apply_limits, memory_bytes

PROFILE = {
    "postgres": (512, 0.15), "api": (2048, 0.50), "worker": (2048, 0.45),
    "acquisition-worker": (1536, 0.35), "report-worker": (1536, 0.35),
    "web": (128, 0.10), "delivery-worker": (128, 0.04),
    "scheduler": (64, 0.02), "events-notifications": (64, 0.02),
    "events-chaining": (64, 0.02),
}
MEMORY_BUDGET = 8 * 1024**3
CPU_BUDGET = 2


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
                or actual_cpu <= 0 or actual_cpu > service_cpu_ceiling + 0.000001):
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
            "api_memory_bytes": services["api"]["mem_limit"],
            "engine_memory_mib": 512, "executor_process_memory_mib": 2048,
            "capacity_claim": "FINITE_CONFIG_ONLY_REAL_PEAK_MEASUREMENT_REQUIRED"}


def configure(directory: Path, context: dict, limit: int) -> dict:
    """Call before startup; all resource/config mutations stay in owned Compose."""
    if (not context["project"].startswith("trackvance-v080-test-")
            or context["project"] == context["main_project"]):
        raise ValueError("OWNED_REPORT_CONTEXT_REQUIRED")
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
