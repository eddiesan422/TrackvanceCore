"""Host cleanup transport is tested with synthetic Docker data; no daemon writes."""
import importlib.util
import io
import json
import sys
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
spec = importlib.util.spec_from_file_location("cleanup_operational_test", Path(__file__).resolve().parents[1] / "cleanup_operational.py")
cleanup = importlib.util.module_from_spec(spec)
spec.loader.exec_module(cleanup)


def state():
    return {"project": "trackvance-synthetic", "containers": [
        {"id": "id-" + service, "name": service, "service": service, "running": service == "postgres"}
        for service in sorted(cleanup.docker_state.PRIMARY_SERVICES)],
        "volumes": [{"name": "owned-" + name, "logical_name": name} for name in sorted(cleanup.docker_state.PRIMARY_VOLUMES)],
        "networks": [{"id": "owned-network", "name": "trackvance-synthetic_default"}]}


def test_live_quiescence_requires_only_postgres_and_exact_resource_ids(monkeypatch):
    baseline = state()
    monkeypatch.setattr(cleanup.docker_state, "inventory", lambda _: baseline)
    assert cleanup.require_quiescence("trackvance-synthetic") == baseline
    changed = deepcopy(baseline)
    changed["containers"][0]["running"] = True
    monkeypatch.setattr(cleanup.docker_state, "inventory", lambda _: changed)
    with pytest.raises(ValueError, match="Only PostgreSQL"):
        cleanup.require_quiescence("trackvance-synthetic")
    changed = deepcopy(baseline)
    changed["containers"][0]["id"] = "different-resource"
    monkeypatch.setattr(cleanup.docker_state, "inventory", lambda _: changed)
    with pytest.raises(ValueError, match="resource IDs changed"):
        cleanup.require_quiescence("trackvance-synthetic", baseline)


def test_helper_has_finite_limits_certified_digest_and_no_secret_mounts(monkeypatch, tmp_path):
    digest, sha = "sha256:" + "a" * 64, "b" * 40
    def inspect(kind, identity):
        if kind == "image":
            return {"Config": {"Labels": {"org.opencontainers.image.version": "0.8.5", "org.opencontainers.image.revision": sha}}}
        if identity == "id-postgres":
            return {"Config": {"Env": ["POSTGRES_USER=synthetic", "POSTGRES_DB=synthetic", "POSTGRES_PASSWORD=private+secret"]}}
        return {"Mounts": [{"Type": "volume", "Name": "owned-trackvance_data", "Destination": "/var/lib/trackvance"}]}
    monkeypatch.setattr(cleanup, "inspect_one", inspect)
    args, url, storage = cleanup.helper_options("trackvance-synthetic", digest, sha, state(), tmp_path, tmp_path, write=True)
    assert args[args.index("--memory") + 1] == "512m" and args[args.index("--cpus") + 1] == "0.5"
    assert args[args.index("--pids-limit") + 1] == "128"
    assert "--read-only" in args and "--cap-drop" in args
    assert "private" not in " ".join(args) and "credentials" not in " ".join(args) and "keys" not in " ".join(args)
    assert "private%2Bsecret" in url and storage == "/var/lib/trackvance"
    with pytest.raises(ValueError, match="immutable"):
        cleanup.helper_options("trackvance-synthetic", "trackvance-api:latest", sha, state(), None, None, write=False)


def test_transport_uses_private_stdin_and_checks_live_inventory_for_each_phase(monkeypatch):
    baseline, observed = state(), []
    monkeypatch.setattr(cleanup, "require_quiescence", lambda project, before=None: observed.append("live") or baseline)
    monkeypatch.setattr(cleanup, "helper_options", lambda *args, **kwargs: (["--memory", "512m"], "private-url", "/store"))
    process = SimpleNamespace(stdin=io.StringIO(), stdout=io.StringIO(
        json.dumps({"kind": "quiescence_request", "nonce": "phase-one"}) + "\n" +
        json.dumps({"kind": "quiescence_request", "nonce": "phase-two"}) + "\n" +
        json.dumps({"kind": "result", "result": {"status": "PASS"}}) + "\n"),
        wait=lambda **_: 0, poll=lambda: 0, kill=lambda: None)
    arguments = []
    monkeypatch.setattr(cleanup.subprocess, "Popen", lambda args, **kwargs: arguments.extend(args) or process)
    monkeypatch.setattr(cleanup.subprocess, "run", lambda *args, **kwargs: SimpleNamespace(returncode=1))
    result = cleanup.execute({"project": "trackvance-synthetic", "action": "plan"}, image="digest", source_commit="sha")
    assert result["status"] == "PASS" and observed == ["live"] * 4
    assert "private-url" not in " ".join(arguments)
    messages = [json.loads(line) for line in process.stdin.getvalue().splitlines()]
    assert messages[0]["database_url"] == "private-url" and messages[1] == {"nonce": "phase-one", "quiescent": True}
    assert "--pull" in arguments and arguments[arguments.index("--pull") + 1] == "never"


def test_active_habitual_service_never_starts_helper(monkeypatch):
    baseline = state()
    next(row for row in baseline["containers"] if row["service"] == "api")["running"] = True
    monkeypatch.setattr(cleanup.docker_state, "inventory", lambda _: baseline)
    monkeypatch.setattr(cleanup.subprocess, "Popen", lambda *args, **kwargs: pytest.fail("Live protected API must prevent helper creation."))
    with pytest.raises(ValueError, match="Only PostgreSQL"):
        cleanup.execute({"project": "trackvance-synthetic", "action": "apply"}, image="digest", source_commit="sha")


@pytest.mark.parametrize("containment", ["quarantine_under_backup", "backup_under_quarantine", "same_target"])
def test_host_backup_quarantine_aliases_fail_before_docker(monkeypatch, tmp_path, containment):
    parent = tmp_path / "private"
    parent.mkdir()
    target = parent / "trial"
    if containment == "quarantine_under_backup":
        backup = parent
    elif containment == "backup_under_quarantine":
        backup = target / "backup"
        backup.mkdir(parents=True)
    else:
        backup = target
        backup.mkdir()
    monkeypatch.setattr(cleanup, "require_quiescence", lambda *_args: pytest.fail("Aliased host paths must be rejected before Docker."))
    with pytest.raises(ValueError, match="host paths overlap"):
        cleanup.execute({"project": "trackvance-synthetic", "action": "apply", "quarantine": "/cleanup-quarantine/trial"},
                        image="digest", source_commit="sha", backup=backup, quarantine_parent=parent)


def test_host_quarantine_traversal_fails_before_docker(monkeypatch, tmp_path):
    monkeypatch.setattr(cleanup, "require_quiescence", lambda *_args: pytest.fail("Traversal must fail before Docker."))
    with pytest.raises(ValueError, match="simple quarantine"):
        cleanup.execute({"project": "trackvance-synthetic", "action": "apply", "quarantine": "/cleanup-quarantine/../backup"},
                        image="digest", source_commit="sha", backup=tmp_path, quarantine_parent=tmp_path)
