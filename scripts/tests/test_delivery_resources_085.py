"""Delivery connector cgroups preserve engine headroom within the whole stack."""
import copy
import json
import sys
from pathlib import Path

import pytest
import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent))
import delivery_cycle as runner


def topology():
    # Emulate the generic overlay after its initial proportional reduction.
    return {"services": {name: {"mem_limit": "128m", "cpus": 0.02}
                         for name in runner.PRIVATE_RESOURCES}}


def local_budget(monkeypatch, memory_mib=8192):
    monkeypatch.setenv("TRACKVANCE_LOCAL_EXECUTION_ID", "local-" + "a" * 32)
    monkeypatch.setenv("TRACKVANCE_LOCAL_MAX_MEMORY_BYTES", str(memory_mib * 1024**2))
    monkeypatch.setenv("TRACKVANCE_LOCAL_MAX_CPUS", "2")
    monkeypatch.delenv("TRACKVANCE_LOCAL_PROJECT_REGISTRY", raising=False)
    monkeypatch.delenv("TRACKVANCE_LOCAL_GROUP", raising=False)


def test_full_fourteen_service_profile_keeps_sqlserver_headroom_and_exact_aggregate(monkeypatch):
    local_budget(monkeypatch)
    profile = topology()
    receipt = runner.configure_private_resources(profile, "trackvance-delivery-e2e-123456789abc")
    assert receipt["memory_bytes"] == 8192 * 1024**2 == 8 * 1024**3
    assert receipt["cpus"] == 2 and len(profile["services"]) == 14
    assert profile["services"]["acquisition-worker"]["cpus"] == .20
    assert profile["services"]["delivery-worker"]["cpus"] == .20
    for name, (mib, _) in runner.PRIVATE_RESOURCES.items():
        assert profile["services"][name]["mem_limit"] == mib * 1024**2
    sql = profile["services"]["destination-sqlserver"]
    assert sql["mem_limit"] == 3 * 1024**3
    assert sql["environment"]["MSSQL_MEMORY_LIMIT_MB"] == "2048"
    assert receipt["report_process_virtual_memory_mib"] == 2048
    assert all(not service.get("profiles") for service in profile["services"].values())
    assert receipt["active_services"] == sorted(runner.PRIVATE_RESOURCES)


def test_insufficient_capacity_never_approves_scaled_down_sqlserver(monkeypatch):
    local_budget(monkeypatch, 6144)
    with pytest.raises(ValueError, match="PENDING_CAPACITY"):
        runner.configure_private_resources(topology(), "trackvance-delivery-e2e-123456789abc")


@pytest.mark.parametrize("mutation", ["sql_engine", "report_engine", "report_process", "service_disabled", "extra", "missing", "cpu", "sql_cgroup"])
def test_effective_configuration_mismatch_fails_closed(monkeypatch, mutation):
    local_budget(monkeypatch)
    profile = topology()
    runner.configure_private_resources(profile, "trackvance-delivery-e2e-123456789abc")
    changed = copy.deepcopy(profile)
    if mutation == "sql_engine":
        changed["services"]["destination-sqlserver"]["environment"]["MSSQL_MEMORY_LIMIT_MB"] = "3072"
    elif mutation in {"report_engine", "report_process"}:
        name = "REPORT_MEMORY_MB" if mutation == "report_engine" else "REPORT_PROCESS_MEMORY_MB"
        changed["services"]["report-worker"]["environment"] = {name: "1024"}
    elif mutation == "service_disabled":
        changed["services"]["scheduler"]["profiles"] = ["local-unused"]
    elif mutation == "extra":
        changed["services"]["unbudgeted-mysql"] = {"cpus": 1, "mem_limit": "1g"}
    elif mutation == "missing":
        changed["services"].pop("destination-postgres18")
    elif mutation == "cpu":
        changed["services"]["api"]["cpus"] = float("inf")
    else:
        changed["services"]["destination-sqlserver"]["mem_limit"] = "2048m"
    with pytest.raises(ValueError):
        runner.validate_private_resources(changed)


def test_optional_connector_overlay_is_expanded_with_explicit_known_limits(monkeypatch):
    local_budget(monkeypatch)
    profile = topology()
    for name in ("report-worker", "destination-postgres", "destination-postgres18", "destination-sqlserver"):
        profile["services"].pop(name)
    receipt = runner.configure_private_resources(profile, "trackvance-delivery-e2e-123456789abc")
    assert receipt["memory_bytes"] == 8192 * 1024**2
    assert set(profile["services"]) == set(runner.PRIVATE_RESOURCES)


@pytest.mark.parametrize("group,cpus", [("delivery", 2), ("catalog-reports", 4)])
def test_local_capacity_gate_matches_full_private_stack_requirement(group, cpus):
    from ci.run_local import capacity

    assert capacity(group)["memory_bytes"] == 8 * 1024**3
    assert capacity(group)["cpus"] == cpus


def probe_profile(monkeypatch):
    """Use the actual Compose probe definitions, not a substitute successful CMD."""
    local_budget(monkeypatch)
    profile = topology()
    for relative in ("compose.yml", "deploy/docker/compose.delivery-test.yml", "deploy/docker/compose.identity-test.yml"):
        definitions = yaml.safe_load((runner.ROOT / relative).read_text(encoding="utf-8"))["services"]
        for name, definition in definitions.items():
            if "healthcheck" in definition:
                profile["services"][name]["healthcheck"] = copy.deepcopy(definition["healthcheck"])
    for service in profile["services"].values():
        service["pids_limit"] = 128
    runner.configure_private_resources(profile, "trackvance-delivery-e2e-123456789abc")
    return profile


def test_private_probe_deadlines_preserve_every_real_command_and_original_compose(monkeypatch):
    paths = [runner.ROOT / name for name in ("compose.yml", "deploy/docker/compose.delivery-test.yml",
                                            "deploy/docker/compose.identity-test.yml")]
    originals = [path.read_bytes() for path in paths]
    profile = probe_profile(monkeypatch)
    commands = {}
    for path in paths:
        for name, service in yaml.safe_load(path.read_text(encoding="utf-8"))["services"].items():
            if "healthcheck" in service:
                commands[name] = service["healthcheck"]["test"]
    assert {name: row["healthcheck"]["test"] for name, row in profile["services"].items()} == commands
    assert "urllib.request.urlopen" in commands["mock-oidc"][3]
    for lane, name in (("DEFAULT", "worker"), ("ACQUISITION", "acquisition-worker"),
                       ("DELIVERY", "delivery-worker"), ("REPORT", "report-worker")):
        assert "worker_status" in commands[name][3] and lane in commands[name][3]
        assert profile["services"][name]["healthcheck"]["timeout"] == "30s"
    assert profile["services"]["mock-oidc"]["cpus"] == .05
    assert profile["services"]["mock-oidc"]["healthcheck"]["timeout"] == "15s"
    receipt = runner.validate_private_resources(profile)
    assert receipt["cpus"] == 2 and receipt["memory_bytes"] == 8 * 1024**3
    assert [path.read_bytes() for path in paths] == originals


@pytest.mark.parametrize("mutation", ["disabled", "none", "short", "unbounded", "start", "absent_probe"])
def test_private_probe_configuration_cannot_disable_or_remove_a_real_healthcheck(monkeypatch, mutation):
    services = probe_profile(monkeypatch)["services"]
    health = services["mock-oidc"]["healthcheck"]
    if mutation == "disabled":
        health["disable"] = True
    elif mutation == "none":
        health["test"] = ["NONE"]
    elif mutation == "short":
        health["timeout"] = "3s"
    elif mutation == "unbounded":
        health["timeout"] = "999s"
    elif mutation == "start":
        health["start_period"] = "999s"
    else:
        health.pop("test")
    with pytest.raises(ValueError, match="DELIVERY_PRIVATE_HEALTHCHECK_MISMATCH"):
        runner.validate_private_healthchecks(services, require_probes=True)


def healthy_diagnostics(profile):
    return [{"service": name, "state": "running", "health": "healthy", "oom_killed": False,
             "exit_code": 0, "pid": 100, "memory_limit_bytes": row["mem_limit"],
             "nano_cpus": round(row["cpus"] * 10**9), "pids_limit": row["pids_limit"],
             "probes": [{"exit_code": 0, "duration_seconds": 4.2, "timed_out": False}]}
            for name, row in profile["services"].items()]


def test_real_probe_receipt_requires_all_fourteen_service_cgroups_and_completed_probes(monkeypatch):
    profile = probe_profile(monkeypatch)
    diagnostics = healthy_diagnostics(profile)
    # Compose JSON can represent CPU/memory as strings; do not multiply a string.
    for row in profile["services"].values():
        row["cpus"], row["mem_limit"] = str(row["cpus"]), str(row["mem_limit"])
        if row["healthcheck"]["start_period"] == "60s":
            row["healthcheck"]["start_period"] = "1m0s"
    receipt = runner.private_healthcheck_evidence(diagnostics, profile["services"])
    assert receipt["status"] == "PASS" and len(receipt["services"]) == 14
    assert receipt["scope"] == "OWNED_REAL_DOCKER_HEALTHCHECKS"
    assert receipt["timings"]["mock-oidc"]["timeout_seconds"] == 15


@pytest.mark.parametrize("value", ["15s", "15000ms", "0m15s"])
def test_compose_canonical_duration_keeps_the_same_finite_probe_budget(value):
    assert runner.healthcheck_seconds(value) == 15


@pytest.mark.parametrize("value", ["15", "-15s", "15s;echo", "15sunknown", None])
def test_invalid_probe_duration_never_becomes_a_finite_budget(value):
    assert runner.healthcheck_seconds(value) is None


@pytest.mark.parametrize("mutation", ["missing", "duplicate", "unhealthy", "no_probe", "timeout",
                                      "exit", "oom", "pid", "memory", "cpu", "pids", "duration"])
def test_unhealthy_missing_or_misbudgeted_native_probe_fails_before_delivery(monkeypatch, mutation):
    profile = probe_profile(monkeypatch)
    diagnostics = healthy_diagnostics(profile)
    row = next(item for item in diagnostics if item["service"] == "mock-oidc")
    if mutation == "missing":
        diagnostics.remove(row)
    elif mutation == "duplicate":
        diagnostics.append(copy.deepcopy(row))
    elif mutation == "unhealthy":
        row["health"] = "unhealthy"
    elif mutation == "no_probe":
        row["probes"] = []
    elif mutation == "timeout":
        row["probes"][-1]["timed_out"] = True
    elif mutation == "exit":
        row["probes"][-1]["exit_code"] = 1
    elif mutation == "oom":
        row["oom_killed"] = True
    elif mutation == "pid":
        row["pid"] = 0
    elif mutation == "memory":
        row["memory_limit_bytes"] //= 2
    elif mutation == "cpu":
        row["nano_cpus"] *= 2
    elif mutation == "pids":
        row["pids_limit"] = 0
    else:
        row["probes"][-1]["duration_seconds"] = float("nan")
    with pytest.raises(RuntimeError, match="DELIVERY_REAL_HEALTHCHECK"):
        runner.private_healthcheck_evidence(diagnostics, profile["services"])


def test_runtime_reader_includes_native_targets_only_when_requested_and_never_probe_output(monkeypatch):
    from isolation_profile import runtime_diagnostics

    profile = probe_profile(monkeypatch)
    rows = []
    for name, definition in profile["services"].items():
        rows.append({"Config": {"Labels": {"com.docker.compose.service": name},
                                "Env": ["PRIVATE_SYNTHETIC_MARKER"]},
                     "HostConfig": {"PidsLimit": definition["pids_limit"], "Memory": definition["mem_limit"],
                                    "NanoCpus": round(definition["cpus"] * 10**9)},
                     "State": {"Status": "running", "ExitCode": 0, "OOMKilled": False, "Pid": 100,
                               "Health": {"Status": "healthy", "Log": [{"ExitCode": 0,
                                   "Start": "2026-10-09T19:00:00+00:00", "End": "2026-10-09T19:00:04+00:00",
                                   "Output": "PRIVATE_SYNTHETIC_PROBE_MARKER"}]}}})

    def read(arguments):
        assert arguments[:2] in (["docker", "ps"], ["docker", "inspect"])
        if arguments[1] == "ps":
            assert arguments[-1] == "label=com.docker.compose.project=trackvance-delivery-e2e-123456789abc"
            return "a"
        return json.dumps(rows)

    original = runtime_diagnostics("trackvance-delivery-e2e-123456789abc", read)
    extended = runtime_diagnostics("trackvance-delivery-e2e-123456789abc", read,
                                   include_delivery_destinations=True)
    assert len(original) == 11 and len(extended) == 14
    assert "PRIVATE_SYNTHETIC" not in json.dumps(extended)
    assert runner.private_healthcheck_evidence(extended, profile["services"])["status"] == "PASS"
