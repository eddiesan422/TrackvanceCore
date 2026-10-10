"""Finite profile for owned Catalog/Report download tests only."""
from __future__ import annotations

import hashlib
import json
import math
import os
import re
import time
from pathlib import Path

from ci.local_resources import apply_limits, memory_bytes

PROFILE = {
    "postgres": (512, 0.20), "api": (2048, 0.84), "worker": (2048, 0.45),
    "acquisition-worker": (1536, 0.35), "report-worker": (1536, 1.00),
    "web": (128, 0.10), "delivery-worker": (128, 0.01),
    "scheduler": (64, 0.02), "events-notifications": (64, 0.02),
    "events-chaining": (64, 0.01),
}
DOWNLOAD_PROFILE = {name: (mib, 2.0 if name == "api" else .01 if name in
                    {"worker", "acquisition-worker", "report-worker"} else cpus)
                    for name, (mib, cpus) in PROFILE.items()}
IDLE_WORKERS = {"worker", "acquisition-worker", "report-worker"}
PHASE_KEY = "x-trackvance-report-resource-phase"
MEMORY_BUDGET = 8 * 1024**3
CPU_BUDGET = 3
HOST_CLIENT_CPUS = 1
DOCKER_CLIENT_CPU_BUDGET = CPU_BUDGET + HOST_CLIENT_CPUS


def phase_profile(phase: str) -> dict:
    if phase not in {"CATALOG", "DOWNLOAD"}:
        raise ValueError("OWNED_REPORT_RESOURCE_PHASE")
    return PROFILE if phase == "CATALOG" else DOWNLOAD_PROFILE


def validate(override: dict, *, phase: str | None = None) -> dict:
    """Reject incomplete shapes, lost API memory and unbounded aggregate caps."""
    phase = phase or override.get(PHASE_KEY, "CATALOG")
    if override.get(PHASE_KEY, phase) != phase:
        raise ValueError("OWNED_REPORT_RESOURCE_PHASE")
    expected = phase_profile(phase)
    services = override["services"]
    if set(services) != set(expected):
        raise ValueError("OWNED_REPORT_RESOURCE_TOPOLOGY")
    memory, cpus = 0, 0.0
    for name, (mib, service_cpu_ceiling) in expected.items():
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
            if env.get("REPORT_PREVIEW_TIMEOUT_SECONDS", "60") != "60":
                raise ValueError("OWNED_REPORT_PREVIEW_DEADLINE")
    if memory > MEMORY_BUDGET or cpus > CPU_BUDGET + 0.000001:
        raise ValueError("OWNED_REPORT_AGGREGATE_BUDGET")
    return {"profile": "OWNED_CATALOG_DOWNLOAD_085", "phase": phase, "status": "PASS",
            "memory_bytes": memory, "memory_ceiling_bytes": MEMORY_BUDGET,
            "cpus": round(cpus, 6), "cpu_ceiling": CPU_BUDGET, "concurrency": 1,
            "host_client_cpu_ceiling": HOST_CLIENT_CPUS,
            "docker_plus_client_cpu_budget": DOCKER_CLIENT_CPU_BUDGET,
            "cpu_budget_scope": "OWNED_DOCKER_AND_DOWNLOAD_CLIENT",
            "controller_monitor_infrastructure_excluded": True,
            "api_memory_bytes": services["api"]["mem_limit"],
            "engine_memory_mib": 512, "executor_process_memory_mib": 2048,
            "capacity_claim": "FINITE_CONFIG_ONLY_REAL_PEAK_MEASUREMENT_REQUIRED"}


def configure(directory: Path, context: dict, limit: int, *, phase: str = "CATALOG") -> dict:
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
    for name, (mib, cpus) in phase_profile(phase).items():
        service = override["services"][name]
        service.update(mem_limit=mib * 1024**2, cpus=cpus)
        if name not in {"postgres", "web"}:
            service["environment"].update(REPORT_DOWNLOAD_MAX_ROWS=str(limit),
                REPORT_XLSX_MAX_ROWS=str(limit), REPORT_CONCURRENCY="1",
                REPORT_MEMORY_MB="512", REPORT_PROCESS_MEMORY_MB="2048")
    override[PHASE_KEY] = phase
    apply_limits(override, context["project"])
    receipt = validate(override, phase=phase)  # Never silently scale the API cgroup.
    path.write_text(json.dumps(override, indent=2) + "\n", encoding="utf-8")
    (directory / "report-resource-profile-085.json").write_text(
        json.dumps(receipt, indent=2) + "\n", encoding="utf-8")
    return receipt


def effective_cgroups(override: dict, project: str, required_services: set[str], *, command=None,
                      phase: str | None = None) -> dict:
    """Verify owned kernel cgroup v2 limits before any certification query."""
    import certification_v080 as guard

    receipt = validate(override, phase=phase)
    expected = phase_profile(receipt["phase"])
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
        if receipt["phase"] == "DOWNLOAD" and name in IDLE_WORKERS:
            raise ValueError("OWNED_REPORT_DOWNLOAD_WORKER_RUNNING")
        mib, cpus = expected[name]
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
    return {"status": "PASS", "phase": receipt["phase"],
            "scope": "OWNED_RUNNING_KERNEL_CGROUP_V2_LIMITS", "services": observed,
            "configured_docker_cpu_ceiling": CPU_BUDGET, "host_client_cpu_ceiling": HOST_CLIENT_CPUS,
            "docker_plus_client_cpu_budget": DOCKER_CLIENT_CPU_BUDGET,
            "controller_monitor_infrastructure_excluded": True}


def owned_containers(context: dict, *, command=None) -> dict:
    """Discover and validate only this private project's exact container IDs."""
    import certification_v080 as guard

    project = context["project"]
    if not guard.PROJECT.fullmatch(project) or project == context["main_project"]:
        raise ValueError("OWNED_REPORT_CONTEXT_REQUIRED")
    command = command or guard.command
    ids = command(["docker", "ps", "-aq", "--no-trunc", "--filter",
                   "label=com.docker.compose.project=" + project]).split()
    if not ids or len(ids) != len(set(ids)) or any(not re.fullmatch(r"[a-f0-9]{64}", value) for value in ids):
        raise ValueError("OWNED_REPORT_CONTAINER_IDS")
    rows = json.loads(command(["docker", "inspect", *ids]))
    result = {}
    for row in rows:
        labels = row.get("Config", {}).get("Labels", {})
        service = labels.get("com.docker.compose.service")
        if (row.get("Id") not in ids or service not in PROFILE or service in result
                or labels.get("com.docker.compose.project") != project
                or os.getenv("TRACKVANCE_LOCAL_EXECUTION_ID") and labels.get("io.trackvance.local-execution") != os.environ["TRACKVANCE_LOCAL_EXECUTION_ID"]
                or service not in {"postgres", "web"} and row.get("Image") != context["image"]):
            raise ValueError("OWNED_REPORT_CONTAINER_IDENTITY")
        result[service] = row
    if {row["Id"] for row in result.values()} != set(ids):
        raise ValueError("OWNED_REPORT_CONTAINER_IDENTITY")
    return result


def idle_metadata(context: dict, fixtures: list[dict], *, command=None) -> dict:
    """Read only this synthetic PostgreSQL; require completed subjects and leases."""
    import certification_v080 as guard

    command = command or guard.command
    if len(fixtures) != 2 or {item.get("alias") for item in fixtures} != {"t", "c"}:
        raise ValueError("OWNED_REPORT_FIXTURE_SUBJECTS")
    ids = [item[key] for item in fixtures for key in ("acquisition_id", "approval_id")]
    if any(not re.fullmatch(r"[a-f0-9]{8}(?:-[a-f0-9]{4}){3}-[a-f0-9]{12}", value) for value in ids) or len(set(ids)) != 4:
        raise ValueError("OWNED_REPORT_FIXTURE_SUBJECTS")
    acquisitions = ",".join("'" + item["acquisition_id"] + "'" for item in fixtures)
    approvals = ",".join("'" + item["approval_id"] + "'" for item in fixtures)
    sql = ("BEGIN READ ONLY; SELECT json_build_object('active',"
           "(SELECT count(*) FROM jobs WHERE status IN ('QUEUED','RUNNING')), 'leased',"
           "(SELECT count(*) FROM jobs WHERE lease_until > now()), 'completed_subjects',"
           "(SELECT count(*) FROM jobs WHERE status='SUCCESS' AND lease_until IS NULL AND "
           f"(acquisition_id IN ({acquisitions}) OR run_id IN ({approvals})))); COMMIT;")
    postgres = owned_containers(context, command=command).get("postgres")
    if not postgres or not postgres.get("State", {}).get("Running"):
        raise ValueError("OWNED_REPORT_POSTGRES_REQUIRED")
    value = json.loads(command(["docker", "exec", postgres["Id"], "sh", "-eu", "-c",
        'psql -X -qAt -v ON_ERROR_STOP=1 --username="$POSTGRES_USER" --dbname="$POSTGRES_DB" -c "$1"',
        "sh", sql]))
    if set(value) != {"active", "leased", "completed_subjects"} or any(type(v) is not int or v < 0 for v in value.values()):
        raise ValueError("OWNED_REPORT_IDLE_METADATA")
    return value


def _transition_write(path: Path, receipt: dict) -> None:
    temporary = path.with_suffix(".partial")
    with temporary.open("w", encoding="utf-8") as stream:
        stream.write(json.dumps(receipt, indent=2) + "\n")
        stream.flush()
        os.fsync(stream.fileno())
    temporary.replace(path)
    if os.name != "nt":
        descriptor = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)


def _api_identity(row: dict) -> str:
    """Hash unchanged config/mounts/network/process without publishing env values."""
    host = {key: value for key, value in row["HostConfig"].items() if key != "NanoCpus"}
    preserved = {key: row.get(key) for key in ("Id", "Image", "Config", "Mounts", "NetworkSettings", "RestartCount")}
    preserved.update(HostConfig=host, State={key: row["State"].get(key) for key in ("Running", "Pid", "StartedAt")})
    return hashlib.sha256(json.dumps(preserved, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def enter_download_phase(directory: Path, context: dict, limit: int, fixtures: list[dict]) -> dict:
    """Journal exact-ID stops and a CPU-only update preserving API process/IP."""
    import certification_v080 as guard
    from ci.local_resources import register_project

    guard.preflight(directory, context)
    before = json.loads((directory / "compose.json").read_text(encoding="utf-8"))
    validate(before, phase="CATALOG")
    deadline = time.monotonic() + 30
    while True:
        idle = idle_metadata(context, fixtures)
        if idle == {"active": 0, "leased": 0, "completed_subjects": 4}:
            break
        if time.monotonic() >= deadline:
            raise ValueError("OWNED_REPORT_WORK_NOT_FINISHED")
        time.sleep(.5)
    rows = owned_containers(context)
    selected = {name for name, row in rows.items() if row.get("State", {}).get("Running")}
    before_cgroups = effective_cgroups(before, context["project"], selected, phase="CATALOG")
    receipt = {"status": "PLANNED", "project": context["project"], "phase": "DOWNLOAD",
               "idle_metadata": idle, "before_cgroups": before_cgroups,
               "api_before_id": rows["api"]["Id"], "image": context["image"],
               "api_identity_before_sha256": _api_identity(rows["api"]),
               "api_update_intent": {"id": rows["api"]["Id"], "cpus": 2, "status": "CPU_UPDATE_INTENT"},
               "worker_intents": {name: {"id": rows[name]["Id"], "status": "STOP_INTENT"}
                                  if name in rows else {"status": "ABSENT_NOT_STARTED"}
                                  for name in sorted(IDLE_WORKERS)}}
    path = directory / "report-download-transition-085.json"
    if path.exists():
        raise ValueError("OWNED_REPORT_TRANSITION_ALREADY_EXISTS")
    register_project(context["project"])
    _transition_write(path, receipt)
    for name in sorted(IDLE_WORKERS):
        if name not in rows:
            continue
        guard.preflight(directory, context)
        current = owned_containers(context).get(name)
        if not current or current["Id"] != rows[name]["Id"]:
            raise ValueError("OWNED_REPORT_WORKER_DRIFT")
        if current.get("State", {}).get("Running"):
            guard.command(["docker", "stop", "--time", "60", current["Id"]], timeout=90)
        stopped = owned_containers(context).get(name)
        if not stopped or stopped["Id"] != current["Id"] or stopped.get("State", {}).get("Running"):
            raise ValueError("OWNED_REPORT_WORKER_STOP_FAILED")
        receipt["worker_intents"][name]["status"] = "STOPPED_VERIFIED"
        _transition_write(path, receipt)
    if idle_metadata(context, fixtures) != idle:
        raise ValueError("OWNED_REPORT_WORK_AFTER_STOP")
    receipt["configured_profile"] = configure(directory, context, limit, phase="DOWNLOAD")
    guard.preflight(directory, context)
    current = owned_containers(context)["api"]
    if _api_identity(current) != receipt["api_identity_before_sha256"]:
        raise ValueError("OWNED_REPORT_API_IDENTITY_DRIFT")
    _transition_write(path, receipt)
    guard.command(["docker", "update", "--cpus", "2", current["Id"]], timeout=60)
    after = owned_containers(context)
    receipt["api_identity_after_sha256"] = _api_identity(after["api"])
    if receipt["api_identity_after_sha256"] != receipt["api_identity_before_sha256"]:
        raise ValueError("OWNED_REPORT_API_IDENTITY_DRIFT")
    receipt["api_after_id"] = after["api"]["Id"]
    receipt["api_update_intent"]["status"] = "CPU_UPDATED_IDENTITY_PRESERVED"
    receipt["after_cgroups"] = effective_cgroups(json.loads((directory / "compose.json").read_text()),
        context["project"], {"postgres", "api", "web"}, phase="DOWNLOAD")
    guard.preflight(directory, context)
    receipt["status"] = "PASS"
    _transition_write(path, receipt)
    return receipt


def api_counters(context: dict) -> dict:
    """Request-interval cgroup counters include all API processes, not just DuckDB."""
    import certification_v080 as guard

    api = owned_containers(context)["api"]
    counters = {}
    for name in ("cpu.stat", "memory.events", "memory.peak"):
        output = guard.command(["docker", "exec", api["Id"], "cat", "/sys/fs/cgroup/" + name])
        counters[name] = int(output.strip()) if name == "memory.peak" else {
            line.split()[0]: int(line.split()[1]) for line in output.splitlines()}
    if (not {"usage_usec", "user_usec", "system_usec", "nr_periods", "nr_throttled", "throttled_usec"}
            <= counters["cpu.stat"].keys() or not {"oom", "oom_kill"} <= counters["memory.events"].keys()):
        raise ValueError("OWNED_REPORT_CGROUP_COUNTERS_MISSING")
    return {"container_id": api["Id"], **counters}
