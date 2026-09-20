import importlib.util
import io
import json
import tarfile
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "docker_state.py"
spec = importlib.util.spec_from_file_location("docker_state", SCRIPT)
docker_state = importlib.util.module_from_spec(spec)
spec.loader.exec_module(docker_state)


def sample_inventory(project="trackvance-recovery-test"):
    return {
        "project": project,
        "containers": [
            {
                "id": f"container-{service}",
                "name": f"{project}-{service}-1",
                "service": service,
                "image_id": "sha256:image",
                "running": True,
            }
            for service in ("postgres", "api", "worker", "web")
        ],
        "volumes": [
            {"name": f"{project}_{logical}", "logical_name": logical}
            for logical in sorted(docker_state.PRIMARY_VOLUMES)
        ],
        "networks": [{"id": "network-id", "name": f"{project}_default"}],
    }


@pytest.mark.parametrize(
    "project", ["trackvance", "trackvance_core", "other-test", "TRACKVANCE-test"]
)
def test_project_guard_rejects_non_trackvance_compose_names(project):
    with pytest.raises(docker_state.OperationError, match="trackvance"):
        docker_state.validate_project(project)


def test_backup_inventory_rejects_unknown_volume():
    state = sample_inventory()
    state["volumes"].append({"name": "foreign", "logical_name": "foreign_data"})
    with pytest.raises(docker_state.OperationError, match="ajenos"):
        docker_state.require_backup_inventory(state)


def test_archive_traversal_and_links_are_rejected(tmp_path):
    archive_path = tmp_path / "malicious.tar.gz"
    with tarfile.open(archive_path, "w:gz") as archive:
        data = b"outside"
        entry = tarfile.TarInfo("../outside")
        entry.size = len(data)
        archive.addfile(entry, io.BytesIO(data))
    with pytest.raises(docker_state.OperationError, match="insegura"):
        docker_state.inspect_archive(archive_path)


def test_reset_requires_literal_confirmation_before_docker_mutation(
    monkeypatch, tmp_path
):
    state = sample_inventory()
    monkeypatch.setattr(docker_state, "inventory", lambda _project: state)
    plan_path = tmp_path / "reset.json"
    plan = docker_state.create_reset_plan(state["project"], plan_path)
    calls = []
    monkeypatch.setattr(
        docker_state, "execute", lambda arguments, **_kwargs: calls.append(arguments) or ""
    )

    with pytest.raises(docker_state.OperationError, match="Confirmación literal"):
        docker_state.reset(plan_path, "RESET:wrong")
    assert calls == []

    confirmation = f"RESET:{state['project']}:{plan['plan_sha256'][:12]}"
    receipt = docker_state.reset(plan_path, confirmation)
    assert receipt["removed"] == {"containers": 4, "volumes": 4, "networks": 1}
    assert calls[0][:3] == ["docker", "rm", "-f"]
    assert calls[1][:3] == ["docker", "volume", "rm"]
    assert calls[2][:3] == ["docker", "network", "rm"]


def test_reset_aborts_when_inventory_changes(monkeypatch, tmp_path):
    before = sample_inventory()
    monkeypatch.setattr(docker_state, "inventory", lambda _project: before)
    plan_path = tmp_path / "reset.json"
    plan = docker_state.create_reset_plan(before["project"], plan_path)
    after = json.loads(json.dumps(before))
    after["containers"][0]["id"] = "replacement-container"
    monkeypatch.setattr(docker_state, "inventory", lambda _project: after)
    calls = []
    monkeypatch.setattr(
        docker_state, "execute", lambda arguments, **_kwargs: calls.append(arguments) or ""
    )
    confirmation = f"RESET:{before['project']}:{plan['plan_sha256'][:12]}"
    with pytest.raises(docker_state.OperationError, match="cambió"):
        docker_state.reset(plan_path, confirmation)
    assert calls == []


def test_manifest_traversal_fails_even_with_matching_hash(tmp_path):
    root = tmp_path / "backup"
    root.mkdir()
    outside = tmp_path / "outside"
    outside.write_bytes(b"PGDMPoutside")
    manifest = {
        "schema_version": 1,
        "consistency": "quiesced",
        "migration": "0007",
        "components": {
            "../outside": {
                "path": "../outside",
                "sha256": docker_state.digest(outside),
                "size_bytes": outside.stat().st_size,
            }
        },
    }
    (root / "backup-manifest.json").write_text(json.dumps(manifest))
    with pytest.raises(docker_state.OperationError):
        docker_state.verify_backup(root)


def test_restore_validates_dump_before_checking_or_creating_target(monkeypatch, tmp_path):
    source = tmp_path / "backup"
    source.mkdir()
    calls = []
    monkeypatch.setattr(
        docker_state,
        "verify_backup",
        lambda _source: {"source_project": "trackvance-source-test"},
    )

    def reject_dump(_source):
        calls.append("dump")
        raise docker_state.OperationError("invalid dump")

    monkeypatch.setattr(docker_state, "validate_postgres_dump", reject_dump)
    monkeypatch.setattr(
        docker_state,
        "ensure_fresh_project",
        lambda _project: calls.append("target"),
    )

    with pytest.raises(docker_state.OperationError, match="invalid dump"):
        docker_state.restore(source, "trackvance-restore-test")
    assert calls == ["dump"]
