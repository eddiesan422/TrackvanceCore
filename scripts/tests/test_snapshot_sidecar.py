"""Trusted snapshot transport preserves verifier assertions and permanent limits."""
import copy
import json
from pathlib import Path

import docker_state as state
import pytest


def source_api(project="trackvance-codex"):
    destinations = {"trackvance_data": "/var/lib/trackvance", "connection_credentials": "/var/lib/trackvance-credentials",
        "connection_keys": "/var/lib/trackvance-keys", "delivery_credentials": "/var/lib/trackvance-delivery-credentials",
        "delivery_keys": "/var/lib/trackvance-delivery-keys"}
    labels = {state.PROJECT_LABEL: project, state.SERVICE_LABEL: "api", "io.trackvance.owner": project}
    if project == "trackvance-codex":
        labels["io.trackvance.permanent"] = "true"
    return {"Id": "a" * 64, "Image": "sha256:" + "b" * 64, "State": {"Running": True},
        "Config": {"Labels": labels, "User": "trackvance", "Env": [
            "DATABASE_URL=postgresql+psycopg://synthetic:private-password@postgres:5432/synthetic",
            "TRACKVANCE_STORAGE_DIR=/var/lib/trackvance", "AWS_SECRET_ACCESS_KEY=never-forward"]},
        "Mounts": [{"Type": "volume", "Name": project + "_" + logical, "Destination": path, "RW": True}
            for logical, path in destinations.items()], "NetworkSettings": {"Networks": {project + "_default": {}}}}


@pytest.mark.parametrize("damage", ["user-stack", "bind", "foreign-volume", "extra-network", "external-db", "stopped", "missing-owner"])
def test_snapshot_plan_rejects_unsafe_scope_before_any_helper(damage):
    source = source_api()
    if damage == "user-stack":
        source = source_api("trackvance-certification")
    elif damage == "bind":
        source["Mounts"][0]["Type"] = "bind"
    elif damage == "foreign-volume":
        source["Mounts"][0]["Name"] = "trackvance-certification_trackvance_data"
    elif damage == "extra-network":
        source["NetworkSettings"]["Networks"]["foreign"] = {}
    elif damage == "external-db":
        source["Config"]["Env"][0] = "DATABASE_URL=postgresql+psycopg://user:pw@external:5432/db"
    elif damage == "stopped":
        source["State"]["Running"] = False
    else:
        source["Config"]["Labels"].pop("io.trackvance.owner")
    with pytest.raises(state.OperationError, match="SNAPSHOT_SIDECAR"):
        state._snapshot_sidecar_plan(source, 2048)


def test_snapshot_transport_bounds_and_restores_ambient_mode_after_failure():
    assert state._SNAPSHOT_MEMORY_MIB.get() is None
    with pytest.raises(RuntimeError), state.snapshot_transport(2048):
        assert state._SNAPSHOT_MEMORY_MIB.get() == 2048
        raise RuntimeError("synthetic")
    assert state._SNAPSHOT_MEMORY_MIB.get() is None
    with pytest.raises(state.OperationError, match="MEMORY_INVALID"), state.snapshot_transport(999999):
        pass


@pytest.fixture
def transport(monkeypatch, tmp_path):
    api = source_api()
    helper_id = "c" * 64
    calls, holder = [], {}
    monkeypatch.setattr(state, "ROOT", tmp_path)

    def docker_json(arguments):
        if arguments[:2] == ["container", "inspect"]:
            if arguments[2] == api["Id"]:
                return [api]
            return [holder["helper"]]
        if arguments[:2] == ["volume", "inspect"]:
            return [{"Labels": {state.PROJECT_LABEL: "trackvance-codex"}} for _ in arguments[2:]]
        assert arguments[:2] == ["network", "inspect"]
        return [{"Labels": {state.PROJECT_LABEL: "trackvance-codex"}}]

    def execute(arguments, **kwargs):
        calls.append(arguments)
        if arguments[:2] == ["docker", "create"]:
            token = next(value.partition("=")[2] for value in arguments if value.startswith("io.trackvance.snapshot-token="))
            env_file = Path(arguments[arguments.index("--env-file") + 1])
            contents = env_file.read_text()
            assert "PGOPTIONS=-c default_transaction_read_only=on" in contents
            assert "AWS_" not in contents and "private-password" not in " ".join(arguments)
            assert arguments[arguments.index("--memory") + 1] == str(2048 * 1024**2)
            helper = copy.deepcopy(api)
            helper.update(Id=helper_id)
            helper["Config"]["Labels"] = {"io.trackvance.snapshot-token": token}
            helper["Config"]["Env"] = contents.splitlines()
            helper["HostConfig"] = {"Memory": 2048 * 1024**2, "NanoCpus": 750_000_000, "PidsLimit": 128,
                "ReadonlyRootfs": True, "Privileged": False, "CapDrop": ["ALL"], "CapAdd": [], "SecurityOpt": ["no-new-privileges:true"]}
            helper["Mounts"] = [{**item, "RW": False} for item in helper["Mounts"]]
            holder.update(helper=helper, token=token, env_file=env_file)
            return helper_id
        if arguments[:2] == ["docker", "exec"]:
            assert arguments[2] == helper_id and kwargs["timeout"] == 600
            assert state.digest(state.VERIFY_SCRIPT) in arguments[-2]
            if holder.get("bad_cleanup"):
                holder["helper"]["Config"]["Labels"]["io.trackvance.snapshot-token"] = "other"
            return json.dumps({"schema_version": 12, "migration": state.CORE097_MIGRATION, "tables": {"documents": {"exact": "d" * 64}}})
        if arguments[:2] == ["docker", "start"] and holder.get("bad_read_only"):
            holder["helper"]["Mounts"][0]["RW"] = True
        return ""

    monkeypatch.setattr(state, "docker_json", docker_json)
    monkeypatch.setattr(state, "execute", execute)
    return api, calls, holder


def test_snapshot_sidecar_keeps_exact_output_assertions_ro_mounts_and_owned_cleanup(transport, tmp_path):
    api, calls, holder = transport
    original = copy.deepcopy(api)
    with state.snapshot_transport(2048):
        result = state._copy_snapshot(api["Id"], tmp_path / "state.json")
    assert result["schema_version"] == 12 and result["tables"]["documents"]["exact"] == "d" * 64
    assert api == original
    create = next(call for call in calls if call[:2] == ["docker", "create"])
    assert all(value.endswith(",readonly") for index, value in enumerate(create) if index and create[index - 1] == "--mount")
    assert "postgres_data" not in " ".join(create) and "--privileged" not in create
    assert ["docker", "rm", "--force", "c" * 64] in calls
    assert not holder["env_file"].exists()
    receipts = list((tmp_path / ".codex-local/snapshot-transports").glob("*.json"))
    assert len(receipts) == 1
    receipt = json.loads(receipts[0].read_text())
    assert receipt["status"] == "PASS" and receipt["helper_removed"]
    assert "environment" not in receipt


def test_snapshot_cleanup_rejects_changed_helper_identity(transport, tmp_path):
    api, calls, holder = transport
    holder["bad_cleanup"] = True
    with pytest.raises(state.OperationError, match="CLEANUP_IDENTITY_INVALID"), state.snapshot_transport(2048):
        state._copy_snapshot(api["Id"], tmp_path / "state.json")
    assert not any(call[:2] == ["docker", "rm"] for call in calls)


def test_snapshot_rejects_writable_helper_before_verifier_and_cleans_only_owned_id(transport, tmp_path):
    api, calls, holder = transport
    holder["bad_read_only"] = True
    with pytest.raises(state.OperationError, match="RUNTIME_CONFINEMENT_INVALID"), state.snapshot_transport(2048):
        state._copy_snapshot(api["Id"], tmp_path / "state.json")
    assert not any(call[:2] == ["docker", "exec"] for call in calls)
    assert ["docker", "rm", "--force", "c" * 64] in calls
