"""Delivery connector cgroups preserve engine headroom within the whole stack."""
import copy
import sys
from pathlib import Path

import pytest

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


@pytest.mark.parametrize("group", ["delivery", "catalog-reports"])
def test_local_capacity_gate_matches_full_private_stack_requirement(group):
    from ci.run_local import capacity

    assert capacity(group)["memory_bytes"] == 8 * 1024**3
    assert capacity(group)["cpus"] == 2
