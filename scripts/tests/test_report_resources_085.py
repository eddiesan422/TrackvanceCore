"""The private mass profile preserves finite guards and API child headroom."""
import copy
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import report_resources_085 as profile


def topology():
    return {"services": {name: {"environment": {}, "labels": {}, "pids_limit": 512}
                         for name in profile.PROFILE}}


def test_private_profile_aggregate_and_concurrency_before_service_start(tmp_path, monkeypatch):
    monkeypatch.delenv("TRACKVANCE_LOCAL_EXECUTION_ID", raising=False)
    (tmp_path / "compose.json").write_text(json.dumps(topology()))
    context = {"project": "trackvance-v080-test-download-123456789abc", "main_project": "trackvance-certification"}
    receipt = profile.configure(tmp_path, context, 1000000)
    assert receipt["memory_bytes"] == 8128 * 1024**2 < 8 * 1024**3
    assert receipt["cpus"] == 3 and receipt["concurrency"] == 1
    assert receipt["host_client_cpu_ceiling"] == 1 and receipt["docker_plus_client_cpu_budget"] == 4
    assert receipt["controller_monitor_infrastructure_excluded"] is True
    actual = json.loads((tmp_path / "compose.json").read_text())
    assert actual["services"]["api"]["mem_limit"] == 2048 * 1024**2
    assert actual["services"]["report-worker"]["environment"]["REPORT_CONCURRENCY"] == "1"
    assert (tmp_path / "report-resource-profile-085.json").is_file()


def test_local_cap_too_small_fails_before_replacing_owned_configuration(tmp_path, monkeypatch):
    original = json.dumps(topology())
    (tmp_path / "compose.json").write_text(original)
    monkeypatch.setenv("TRACKVANCE_LOCAL_EXECUTION_ID", "local-" + "a" * 32)
    monkeypatch.setenv("TRACKVANCE_LOCAL_MAX_MEMORY_BYTES", str(7 * 1024**3))
    monkeypatch.setenv("TRACKVANCE_LOCAL_MAX_CPUS", "4")
    monkeypatch.delenv("TRACKVANCE_LOCAL_PROJECT_REGISTRY", raising=False)
    monkeypatch.delenv("TRACKVANCE_LOCAL_GROUP", raising=False)
    with pytest.raises(ValueError, match="PENDING_CAPACITY"):
        profile.configure(tmp_path, {"project": "trackvance-v080-test-dl-123456789abc", "main_project": "usual"}, 120)
    assert (tmp_path / "compose.json").read_text() == original


@pytest.mark.parametrize("mutation", ["api_memory", "unbounded_cpu", "lower_cpu", "extra_service", "concurrency"])
def test_borrowed_context_cannot_claim_incomplete_or_weakened_profile(tmp_path, monkeypatch, mutation):
    monkeypatch.delenv("TRACKVANCE_LOCAL_EXECUTION_ID", raising=False)
    (tmp_path / "compose.json").write_text(json.dumps(topology()))
    profile.configure(tmp_path, {"project": "trackvance-v080-test-dl-123456789abc", "main_project": "usual"}, 120)
    data = copy.deepcopy(json.loads((tmp_path / "compose.json").read_text()))
    if mutation == "api_memory":
        data["services"]["api"]["mem_limit"] = 1024 * 1024**2
    elif mutation == "unbounded_cpu":
        data["services"]["api"]["cpus"] = float("inf")
    elif mutation == "lower_cpu":
        data["services"]["api"]["cpus"] = .5
    elif mutation == "extra_service":
        data["services"]["foreign"] = {"cpus": 1, "mem_limit": "1g"}
    else:
        data["services"]["api"]["environment"]["REPORT_CONCURRENCY"] = "2"
    with pytest.raises(ValueError):
        profile.validate(data)


def test_four_cpu_local_budget_keeps_exact_profile_after_generic_limits(tmp_path, monkeypatch):
    monkeypatch.setenv("TRACKVANCE_LOCAL_EXECUTION_ID", "local-" + "a" * 32)
    monkeypatch.setenv("TRACKVANCE_LOCAL_MAX_CPUS", "4")
    monkeypatch.setenv("TRACKVANCE_LOCAL_MAX_MEMORY_BYTES", str(8 * 1024**3))
    monkeypatch.delenv("TRACKVANCE_LOCAL_PROJECT_REGISTRY", raising=False)
    monkeypatch.delenv("TRACKVANCE_LOCAL_GROUP", raising=False)
    (tmp_path / "compose.json").write_text(json.dumps(topology()))
    receipt = profile.configure(tmp_path, {"project": "trackvance-v080-test-dl-123456789abc", "main_project": "usual"}, 120)
    actual = json.loads((tmp_path / "compose.json").read_text())
    assert {name: (row["mem_limit"] // 1024**2, row["cpus"]) for name, row in actual["services"].items()} == profile.PROFILE
    assert receipt["cpus"] + receipt["host_client_cpu_ceiling"] == 4


def test_under_four_cpu_whole_budget_fails_before_any_configuration_write(tmp_path, monkeypatch):
    original = json.dumps(topology())
    (tmp_path / "compose.json").write_text(original)
    monkeypatch.setenv("TRACKVANCE_LOCAL_EXECUTION_ID", "local-" + "a" * 32)
    monkeypatch.setenv("TRACKVANCE_LOCAL_MAX_CPUS", "3")
    with pytest.raises(ValueError, match="REQUIRES_MAX_CPUS_4"):
        profile.configure(tmp_path, {"project": "trackvance-v080-test-dl-123456789abc", "main_project": "usual"}, 120)
    assert (tmp_path / "compose.json").read_text() == original


@pytest.mark.parametrize("mutation", [None, "foreign", "missing", "host_cpu", "kernel_cpu", "kernel_memory", "unbounded"])
def test_actual_owned_cgroups_match_exact_config_and_reject_drift(tmp_path, monkeypatch, mutation):
    monkeypatch.delenv("TRACKVANCE_LOCAL_EXECUTION_ID", raising=False)
    (tmp_path / "compose.json").write_text(json.dumps(topology()))
    project = "trackvance-v080-test-dl-123456789abc"
    profile.configure(tmp_path, {"project": project, "main_project": "usual"}, 120)
    actual = json.loads((tmp_path / "compose.json").read_text())
    selected = {"api", "report-worker"}
    containers = [{"Id": name, "Config": {"Labels": {"com.docker.compose.project": project,
        "com.docker.compose.service": name}}, "State": {"Running": True},
        "HostConfig": {"Memory": profile.PROFILE[name][0] * 1024**2,
                       "NanoCpus": round(profile.PROFILE[name][1] * 10**9)}} for name in sorted(selected)]
    if mutation == "foreign":
        containers[0]["Config"]["Labels"]["com.docker.compose.project"] = "usual"
    elif mutation == "missing":
        containers.pop()
    elif mutation == "host_cpu":
        containers[0]["HostConfig"]["NanoCpus"] = 500000000
    commands = []
    def command(arguments):
        commands.append(arguments)
        if arguments[1] == "ps":
            return " ".join(row["Id"] for row in containers)
        if arguments[1] == "inspect":
            return json.dumps(containers)
        assert arguments[1:2] == ["exec"] and arguments[3:] == ["cat", "/sys/fs/cgroup/cpu.max", "/sys/fs/cgroup/memory.max"]
        mib, cpu = profile.PROFILE[arguments[2]]
        quota, memory = round(cpu * 100000), mib * 1024**2
        if mutation == "kernel_cpu":
            quota //= 2
        elif mutation == "kernel_memory":
            memory //= 2
        elif mutation == "unbounded":
            return "max 100000\nmax"
        return f"{quota} 100000\n{memory}"
    if mutation:
        with pytest.raises(ValueError, match="OWNED_REPORT_CGROUP"):
            profile.effective_cgroups(actual, project, selected, command=command)
    else:
        receipt = profile.effective_cgroups(actual, project, selected, command=command)
        assert receipt["status"] == "PASS" and set(receipt["services"]) == selected
        assert receipt["docker_plus_client_cpu_budget"] == 4
        assert len(commands) == 4
