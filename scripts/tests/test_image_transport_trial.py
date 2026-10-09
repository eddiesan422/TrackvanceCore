"""Trial guards must fail before Docker mutations or unsafe cleanup."""
import json
import shutil
from copy import deepcopy

import image_transport_trial as trial
import pytest
from ci.local_resources import image_identity
from ci.owned_cleanup import PROJECT
from image_transport_fixtures import image_archive

PROJECT_ID = "trackvance-v070-test-image-transport-" + "a" * 12
HELPER_ID = "sha256:" + "1" * 64
SHA = "a" * 40


def source():
    return {"Id": "b" * 64, "Image": HELPER_ID, "State": {"Running": True},
            "Config": {"Labels": {trial.LABEL: PROJECT_ID, trial.OWNER: PROJECT_ID}},
            "HostConfig": {"Memory": 768 * 1024**2, "MemorySwap": 768 * 1024**2,
                           "NanoCpus": 10**9, "PidsLimit": 256, "Privileged": True,
                           "RestartPolicy": {"Name": "no"},
                           "PortBindings": {"2375/tcp": [{"HostIp": "127.0.0.1", "HostPort": ""}]}},
            "Mounts": [{"Type": "volume", "Name": PROJECT_ID + "-data", "Destination": "/var/lib/docker"}],
            "NetworkSettings": {"Networks": {PROJECT_ID + "-net": {}},
                                "Ports": {"2375/tcp": [{"HostIp": "127.0.0.1", "HostPort": "32123"}]}}}


def test_source_args_keep_privilege_in_the_bounded_temporary_daemon():
    args = trial.container_args(PROJECT_ID, HELPER_ID)
    assert PROJECT.fullmatch(PROJECT_ID)
    assert args[args.index("--memory") + 1] == "768m"
    assert args[args.index("--cpus") + 1] == "1"
    assert args[args.index("--pids-limit") + 1] == "256"
    assert args[args.index("--restart") + 1] == "no"
    assert args[args.index("--publish") + 1] == "127.0.0.1::2375"
    assert "--privileged" in args and "--feature=containerd-snapshotter=false" in args
    assert "--volume" not in args and "--rm" not in args
    assert args[args.index("--mount") + 1] == "type=volume,src=" + PROJECT_ID + "-data,dst=/var/lib/docker"
    assert trial.source_profile(source(), PROJECT_ID, HELPER_ID) == "tcp://127.0.0.1:32123"


@pytest.mark.parametrize("damage", ["memory", "swap", "cpu", "pids", "restart", "privilege", "bind", "mount",
                                   "network", "exposed", "binding", "owner", "project", "image"])
def test_changed_source_profile_rejects_before_removal(damage):
    row = source()
    if damage in {"memory", "swap", "cpu", "pids"}:
        key = {"memory": "Memory", "swap": "MemorySwap", "cpu": "NanoCpus", "pids": "PidsLimit"}[damage]
        row["HostConfig"][key] = 0
    elif damage == "restart":
        row["HostConfig"]["RestartPolicy"]["Name"] = "always"
    elif damage == "privilege":
        row["HostConfig"]["Privileged"] = False
    elif damage in {"bind", "mount"}:
        row["Mounts"][0]["Type" if damage == "bind" else "Name"] = "bind" if damage == "bind" else "postgres"
    elif damage == "network":
        row["NetworkSettings"]["Networks"]["bridge"] = {}
    elif damage == "exposed":
        row["NetworkSettings"]["Ports"]["2375/tcp"][0]["HostIp"] = "0.0.0.0"
    elif damage == "binding":
        row["HostConfig"]["PortBindings"]["2375/tcp"][0]["HostIp"] = "0.0.0.0"
    elif damage in {"owner", "project"}:
        row["Config"]["Labels"][trial.OWNER if damage == "owner" else trial.LABEL] = "trackvance-certification"
    else:
        row["Image"] = "sha256:" + "f" * 64
    with pytest.raises(ValueError):
        trial.source_profile(row, PROJECT_ID, HELPER_ID, cleanup=True)


@pytest.mark.parametrize("running", [False, True])
def test_owned_source_can_be_cleaned_without_a_live_port(running):
    row = source()
    row["State"]["Running"] = running
    row["NetworkSettings"]["Ports"] = {}
    assert trial.source_profile(row, PROJECT_ID, HELPER_ID, cleanup=True) is None
    with pytest.raises(ValueError):
        trial.source_profile(row, PROJECT_ID, HELPER_ID)


@pytest.mark.parametrize("endpoint", ["tcp://0.0.0.0:2375", "tcp://localhost:2375", "tcp://127.0.0.1:70000",
                                      "ssh://other", "unix:///var/run/docker.sock", "default"])
def test_external_or_implicit_endpoint_rejected(endpoint):
    with pytest.raises(ValueError):
        trial.endpoint(endpoint)


def test_process_environment_never_uses_global_context_or_credentials(tmp_path, monkeypatch):
    monkeypatch.setenv("DOCKER_CONTEXT", "habitual")
    monkeypatch.setenv("DOCKER_AUTH_CONFIG", "must-not-inherit")
    monkeypatch.setenv("DOCKER_TLS_VERIFY", "1")
    env = trial.environment(tmp_path, trial.HOST)
    assert env["DOCKER_CONFIG"] == str(tmp_path)
    assert env["DOCKER_HOST"] == trial.HOST
    assert all(key not in env for key in ("DOCKER_CONTEXT", "DOCKER_AUTH_CONFIG", "DOCKER_TLS_VERIFY"))


def test_existing_anonymous_private_config_can_keep_buildx_plugin_locations():
    assert trial.anonymous_config({"auths": {"https://index.docker.io/v1/": {}},
                                   "cliPluginsExtraDirs": ["C:/private/cli-plugins"]})


@pytest.mark.parametrize("additional", [{"credsStore": "desktop"}, {"credHelpers": {}},
                                        {"currentContext": "usual"}, {"cliPluginsExtraDirs": "invalid"}])
def test_private_config_rejects_helpers_context_or_invalid_plugin_metadata(additional):
    assert not trial.anonymous_config({"auths": {"https://index.docker.io/v1/": {}}, **additional})


@pytest.mark.parametrize("damage", ["memory", "cpu", "unbounded-memory", "unbounded-cpu"])
def test_capacity_includes_external_envelope_and_rejects_unbounded_consumers(damage):
    info = {"MemTotal": 24 * trial.GIB, "NCPU": 16}
    rows = [{"State": {"Status": "running"}, "HostConfig": {"Memory": 10 * trial.GIB, "NanoCpus": 4 * 10**9}}]
    if damage == "memory":
        info["MemTotal"] = 13 * trial.GIB
    elif damage == "cpu":
        info["NCPU"] = 5
    else:
        rows[0]["HostConfig"]["Memory" if damage.endswith("memory") else "NanoCpus"] = 0
    with pytest.raises(ValueError):
        trial.capacity(info, rows, 10 * trial.GIB, 4)


def test_declared_reservations_are_counted_when_services_are_temporarily_absent():
    result = trial.capacity({"MemTotal": 24 * trial.GIB, "NCPU": 16}, [], 10 * trial.GIB, 4)
    assert result["required_memory_bytes"] == int(13.5 * trial.GIB)
    assert result["required_cpus"] == 6


@pytest.mark.parametrize("damage", ["preexisting", "new-tag", "config", "running-consumer", "stopped-consumer"])
def test_image_cleanup_requires_exact_first_identity_and_all_consumers(damage):
    row = {"Id": HELPER_ID, "Config": {"Labels": {}}, "RootFS": {"Layers": []}, "RepoTags": [trial.HELPER]}
    registered = image_identity(deepcopy(row))
    baseline, consumers = set(), []
    if damage == "preexisting":
        baseline.add(HELPER_ID)
    elif damage == "new-tag":
        row["RepoTags"].append("someone-else:latest")
    elif damage == "config":
        row["Config"]["Env"] = ["changed"]
    else:
        consumers = [{"image_id": HELPER_ID, "status": "running" if damage.startswith("running") else "exited"}]
    with pytest.raises(ValueError):
        trial.removable_image(row, baseline, registered, consumers)


def instance(tmp_path):
    config = tmp_path / "config"
    config.mkdir()
    (config / "config.json").write_text(json.dumps({"auths": {"https://index.docker.io/v1/": {}}}))
    tool = trial.Trial(tmp_path / "trial", config, SHA, 10 * trial.GIB, 4)
    tool.registry.update(baseline_volumes=[], baseline_network_names=[])
    return tool


def test_creation_intent_is_durable_before_returning_to_creation(tmp_path, monkeypatch):
    tool = instance(tmp_path)
    monkeypatch.setattr(tool, "guard", lambda: None)
    tool.intent("create-volume", tool.project + "-data")
    recorded = json.loads((tool.directory / "registry.json").read_text())
    assert recorded["intents"][0]["identity"] == tool.project + "-data"


def test_colliding_uuid_volume_is_preserved_before_creation(tmp_path, monkeypatch):
    tool = instance(tmp_path)
    tool.registry["baseline_volumes"] = [tool.project + "-data"]
    monkeypatch.setattr(tool, "guard", lambda: None)
    with pytest.raises(ValueError, match="preexisting"):
        tool.intent("create-volume", tool.project + "-data")
    assert tool.registry["intents"] == []


def test_source_endpoint_context_is_restored_on_failure(tmp_path):
    tool = instance(tmp_path)
    docker, save = trial.image_bundle.docker, trial.image_bundle.save_image
    with pytest.raises(RuntimeError), tool.transfer("tcp://127.0.0.1:12345"):
        raise RuntimeError("fault")
    assert trial.image_bundle.docker is docker and trial.image_bundle.save_image is save


def test_protected_mount_hash_ignores_inspect_array_order_but_detects_actual_changes(tmp_path, monkeypatch):
    tool = instance(tmp_path)
    row = source()
    row["State"]["Status"] = "exited"
    row["Config"]["Labels"] = {trial.LABEL: "trackvance-certification"}
    row["Mounts"].append({"Type": "volume", "Name": "protected", "Destination": "/app/data"})
    monkeypatch.setattr(tool, "rows", lambda: [row])
    before = tool.protected()
    row["Mounts"].reverse()
    assert tool.protected() == before
    row["Mounts"][0]["Destination"] = "/changed"
    assert tool.protected() != before


@pytest.mark.parametrize("cleanup_status", ["PASS", "FAIL"])
def test_original_failure_is_persisted_even_when_cleanup_fails(tmp_path, monkeypatch, cleanup_status):
    tool = instance(tmp_path)
    info = {"ID": "engine", "ServerVersion": "29.1.3", "Driver": "overlayfs", "MemTotal": 24 * trial.GIB, "NCPU": 16}

    def docker(*args, **_kwargs):
        if args[0] == "info":
            return json.dumps(info)
        if args[0] == "pull":
            raise ValueError("original registry pull failure")
        return ""

    monkeypatch.setattr(tool, "docker", docker)
    monkeypatch.setattr(trial, "limit_cpu_affinity", lambda _: {"status": "PASS"})
    monkeypatch.setattr(tool, "cleanup", lambda: {"status": cleanup_status, "removed": []})
    receipt = tool.run()
    assert receipt["status"] == "FAIL"
    assert receipt["error"] == "original registry pull failure"
    saved = json.loads((tool.directory / "trial.json").read_text())
    assert saved["cleanup"]["status"] == cleanup_status and saved["certifies_085"] is False
    assert saved["host_before"] == saved["host_after"] and saved["protected_unchanged"]
    assert json.loads((tool.directory / "registry.json").read_text())["intents"][0]["action"] == "pull-helper"


def test_same_owner_labels_cannot_adopt_different_image_configuration_for_cleanup(tmp_path, monkeypatch):
    tool = instance(tmp_path)
    labels = {"org.opencontainers.image.revision": SHA, "org.opencontainers.image.version": "0.8.5", trial.OWNER: tool.project}
    expected = image_archive(tmp_path / "expected.tar", SHA, "backend", config_change={
        "config": {"Labels": labels, "Env": ["correct"]}})
    wrong = image_archive(tmp_path / "wrong.tar", SHA, "backend", config_change={
        "config": {"Labels": labels, "Env": ["wrong"]}})
    tool.registry.update(baseline_images=[], expected_configurations={"backend": expected["configuration_digest"]})
    row = {"Id": wrong["engine_id"], "Config": wrong["config"]["config"],
           "RootFS": {"Type": "layers", "Layers": wrong["rootfs_diff_ids"]}}
    monkeypatch.setattr(tool, "docker", lambda *args, **_: wrong["engine_id"] if args[:2] == ("image", "ls") else json.dumps([row]))
    monkeypatch.setattr(tool, "save", lambda _, path: shutil.copyfile(tmp_path / "wrong.tar", path))
    with pytest.raises(ValueError, match="Unregistered configuration"):
        tool.register_images()
    assert tool.registry["images"] == []
