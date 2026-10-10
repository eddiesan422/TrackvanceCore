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


def test_download_phase_preserves_memory_and_product_caps_within_whole_budget(tmp_path, monkeypatch):
    monkeypatch.delenv("TRACKVANCE_LOCAL_EXECUTION_ID", raising=False)
    (tmp_path / "compose.json").write_text(json.dumps(topology()))
    context = {"project": "trackvance-v080-test-dl-123456789abc", "main_project": "usual"}
    original = profile.configure(tmp_path, context, 1000000)
    result = profile.configure(tmp_path, context, 1000000, phase="DOWNLOAD")
    data = json.loads((tmp_path / "compose.json").read_text())
    assert result["phase"] == "DOWNLOAD" and result["memory_bytes"] == original["memory_bytes"] == 8128 * 1024**2
    assert data["services"]["api"]["cpus"] == 2 and result["cpus"] == 2.39
    assert result["cpus"] <= profile.CPU_BUDGET
    assert all(data["services"][name]["cpus"] == .01 for name in profile.IDLE_WORKERS)
    assert data["services"]["api"]["environment"].get("REPORT_PREVIEW_TIMEOUT_SECONDS", "60") == "60"
    with pytest.raises(ValueError, match="RESOURCE_PHASE"):
        profile.validate(data, phase="CATALOG")
    data["services"]["api"]["environment"]["REPORT_PREVIEW_TIMEOUT_SECONDS"] = "180"
    with pytest.raises(ValueError, match="PREVIEW_DEADLINE"):
        profile.validate(data)


@pytest.mark.parametrize("mutation", [None, "foreign", "wrong_image", "active_jobs", "api_replaced", "network_changed", "memory_changed"])
def test_download_transition_stops_only_verified_idle_ids_and_journals_before_mutation(tmp_path, monkeypatch, mutation):
    import certification_v080 as guard

    monkeypatch.delenv("TRACKVANCE_LOCAL_EXECUTION_ID", raising=False)
    monkeypatch.delenv("TRACKVANCE_LOCAL_PROJECT_REGISTRY", raising=False)
    (tmp_path / "compose.json").write_text(json.dumps(topology()))
    context = {"project": "trackvance-v080-test-dl-123456789abc", "main_project": "usual", "image": "sha256:" + "f" * 64}
    profile.configure(tmp_path, context, 1000000)
    fixtures = [{"alias": alias, "acquisition_id": f"00000000-0000-0000-0000-{index:012d}",
                 "approval_id": f"00000000-0000-0000-0000-{index + 2:012d}"}
                for index, alias in enumerate(("t", "c"), 1)]
    rows = {name: {"Id": f"{index:064x}", "Image": context["image"],
        "Config": {"Labels": {"com.docker.compose.project": context["project"], "com.docker.compose.service": name}},
        "State": {"Running": True, "Pid": 1000 + index, "StartedAt": "original"},
        "NetworkSettings": {"Networks": {"private": {"IPAddress": f"172.30.0.{index}"}}},
        "HostConfig": {"Memory": profile.PROFILE[name][0] * 1024**2,
        "NanoCpus": round(profile.PROFILE[name][1] * 10**9)}}
        for index, name in enumerate(("api", "postgres", "web", "worker", "acquisition-worker"), 1)}
    if mutation == "foreign":
        rows["worker"]["Config"]["Labels"]["com.docker.compose.project"] = "usual"
    elif mutation == "wrong_image":
        rows["worker"]["Image"] = "sha256:" + "e" * 64
    if mutation == "active_jobs":
        monkeypatch.setattr(profile.time, "monotonic", iter((0, 31)).__next__)
    monkeypatch.setattr(guard, "preflight", lambda *_: None)
    monkeypatch.setattr(guard, "compose_args", lambda *_: ["docker", "compose", "--project-name", context["project"]])
    mutations = []
    def command(args, **_):
        if args[1] == "ps":
            return " ".join(row["Id"] for row in rows.values() if args[2] == "-aq" or row["State"]["Running"])
        if args[1] == "inspect":
            return json.dumps([row for row in rows.values() if row["Id"] in args[2:]])
        if args[1] == "exec" and args[3] == "sh":
            assert "BEGIN READ ONLY" in args[-1] and all(item["approval_id"] in args[-1] for item in fixtures)
            return json.dumps({"active": int(mutation == "active_jobs"), "leased": 0, "completed_subjects": 4})
        if args[1] == "exec":
            row = next(row for row in rows.values() if row["Id"] == args[2])
            return f"{row['HostConfig']['NanoCpus'] // 10000} 100000\n{row['HostConfig']['Memory']}"
        journal = json.loads((tmp_path / "report-download-transition-085.json").read_text())
        assert journal["project"] == context["project"] and journal["status"] == "PLANNED"
        mutations.append(args)
        if args[1] == "stop":
            row = next(row for row in rows.values() if row["Id"] == args[-1])
            assert row["Config"]["Labels"]["com.docker.compose.service"] in profile.IDLE_WORKERS
            row["State"]["Running"] = False
        else:
            assert args == ["docker", "update", "--cpus", "2", rows["api"]["Id"]]
            assert journal["api_update_intent"] == {"id": rows["api"]["Id"], "cpus": 2, "status": "CPU_UPDATE_INTENT"}
            assert json.loads((tmp_path / "compose.json").read_text())["services"]["api"]["cpus"] == 2
            rows["api"]["HostConfig"]["NanoCpus"] = 2000000000
            if mutation == "api_replaced":
                rows["api"]["Id"] = "d" * 64
            elif mutation == "network_changed":
                rows["api"]["NetworkSettings"]["Networks"]["private"]["IPAddress"] = "172.30.0.99"
            elif mutation == "memory_changed":
                rows["api"]["HostConfig"]["Memory"] = 1024 * 1024**2
        return ""
    monkeypatch.setattr(guard, "command", command)
    if mutation:
        with pytest.raises(ValueError, match="OWNED_REPORT"):
            profile.enter_download_phase(tmp_path, context, 1000000, fixtures)
        assert len(mutations) == (3 if mutation in {"api_replaced", "network_changed", "memory_changed"} else 0)
    else:
        receipt = profile.enter_download_phase(tmp_path, context, 1000000, fixtures)
        assert receipt["status"] == "PASS" and receipt["before_cgroups"]["services"]["api"]["cpus"] == .84
        assert receipt["after_cgroups"]["services"]["api"]["cpus"] == 2
        assert receipt["worker_intents"]["report-worker"]["status"] == "ABSENT_NOT_STARTED"
        assert receipt["api_after_id"] == receipt["api_before_id"] == rows["api"]["Id"]
        assert receipt["api_identity_before_sha256"] == receipt["api_identity_after_sha256"]
        assert receipt["api_update_intent"]["status"] == "CPU_UPDATED_IDENTITY_PRESERVED"
        assert len(mutations) == 3 and rows["postgres"]["State"]["Running"] and rows["web"]["State"]["Running"]


@pytest.mark.parametrize("mutation", ["order", "source", "destination", "read_only", "duplicate", "image", "pid", "network"])
def test_api_identity_canonicalizes_only_mount_order_and_rejects_real_drift(mutation):
    original = {"Id": "a" * 64, "Image": "sha256:" + "f" * 64,
        "Config": {"Labels": {"org.opencontainers.image.revision": "c" * 40}},
        "HostConfig": {"NanoCpus": 840000000, "Memory": 2048 * 1024**2},
        "State": {"Running": True, "Pid": 1001, "StartedAt": "original"},
        "NetworkSettings": {"Networks": {"private": {"IPAddress": "172.30.0.2"}}},
        "Mounts": [{"Type": "volume", "Name": name, "Source": "/vol/" + name,
                    "Destination": "/" + name, "RW": True, "Propagation": ""}
                   for name in ("storage", "postgres", "scratch")]}
    observed = copy.deepcopy(original)
    observed["HostConfig"]["NanoCpus"] = 2000000000
    observed["Mounts"].reverse()
    if mutation == "source":
        observed["Mounts"][0]["Source"] = "/foreign"
    elif mutation == "destination":
        observed["Mounts"][0]["Destination"] = "/other"
    elif mutation == "read_only":
        observed["Mounts"][0]["RW"] = False
    elif mutation == "duplicate":
        observed["Mounts"].append(copy.deepcopy(observed["Mounts"][0]))
    elif mutation == "image":
        observed["Image"] = "sha256:" + "e" * 64
    elif mutation == "pid":
        observed["State"]["Pid"] += 1
    elif mutation == "network":
        observed["NetworkSettings"]["Networks"]["private"]["IPAddress"] = "172.30.0.99"
    assert (profile._api_identity(original) == profile._api_identity(observed)) is (mutation == "order")
