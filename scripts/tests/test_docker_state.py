import importlib.util
import io
import json
import os
import subprocess
import sys
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


def archive_bytes(name="evidence.txt", content=b"binary\x00evidence"):
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
        member = tarfile.TarInfo(name)
        member.size = len(content)
        archive.addfile(member, io.BytesIO(content))
    return buffer.getvalue()


def test_archive_streams_to_private_host_file_without_writable_bind(monkeypatch, tmp_path):
    (tmp_path / "volumes").mkdir()
    payload = archive_bytes()
    calls = []

    def run(arguments, **kwargs):
        calls.append(arguments)
        assert kwargs["stdin"] == subprocess.DEVNULL
        assert kwargs["stdout"] != subprocess.PIPE
        assert kwargs["stderr"] == subprocess.PIPE
        kwargs["stdout"].write(payload)
        return subprocess.CompletedProcess(arguments, 0)

    monkeypatch.setattr(docker_state.subprocess, "run", run)
    result = docker_state._archive_volume("disposable-volume", "trackvance_data", "image", tmp_path)
    target = tmp_path / result["path"]
    assert target.read_bytes() == payload
    assert result["entries"]["evidence.txt"]["size_bytes"] == len(b"binary\x00evidence")
    assert "type=volume,src=disposable-volume,dst=/source,readonly" in calls[0]
    assert not any("type=bind" in value for value in calls[0])
    assert "--user" not in calls[0]
    if os.name != "nt":
        assert target.stat().st_mode & 0o777 == 0o600


def test_extract_streams_private_host_file_to_unprivileged_container(monkeypatch, tmp_path):
    archive = tmp_path / "snapshot.tar.gz"
    payload = archive_bytes()
    archive.write_bytes(payload)
    calls = []

    def run(arguments, **kwargs):
        calls.append(arguments)
        assert kwargs["stdin"].read() == payload
        assert kwargs["stdout"] == subprocess.DEVNULL
        return subprocess.CompletedProcess(arguments, 0)

    monkeypatch.setattr(docker_state.subprocess, "run", run)
    docker_state._extract_volume(tmp_path, archive.name, "fresh-volume", "image")
    assert "--interactive" in calls[0]
    assert "type=volume,src=fresh-volume,dst=/target" in calls[0]
    assert not any("type=bind" in value for value in calls[0])
    assert "--user" not in calls[0]


def test_stream_failure_never_exposes_subprocess_stderr(monkeypatch):
    monkeypatch.setattr(docker_state.subprocess, "run", lambda arguments, **_kwargs:
        subprocess.CompletedProcess(arguments, 1, stderr=b"credential-must-remain-private"))
    with pytest.raises(docker_state.OperationError) as captured:
        docker_state.execute_stream(["docker", "run"])
    assert "credential-must-remain-private" not in str(captured.value)
    assert "transferencia" in str(captured.value)


def test_archive_program_round_trip_streams_binary_and_preserves_mode(tmp_path):
    source = tmp_path / "source"
    target = tmp_path / "target"
    source.mkdir()
    target.mkdir()
    (source / "nested").mkdir()
    (source / "nested" / "record.bin").write_bytes(b"record\x00\xffdata")
    (source / "nested" / "record.bin").chmod(0o600)
    archive_program = docker_state.ARCHIVE_PROGRAM.replace("root = '/source'", f"root = {str(source)!r}")
    extract_program = docker_state.EXTRACT_PROGRAM.replace("root = '/target'", f"root = {str(target)!r}")
    archive = tmp_path / "stream.tar.gz"
    with archive.open("wb") as output:
        subprocess.run([sys.executable, "-c", archive_program], stdout=output, check=True)
    with archive.open("rb") as stream:
        subprocess.run([sys.executable, "-c", extract_program], stdin=stream, check=True)
    assert (target / "nested" / "record.bin").read_bytes() == b"record\x00\xffdata"
    if os.name != "nt":
        assert (target / "nested" / "record.bin").stat().st_mode & 0o777 == 0o600


@pytest.mark.parametrize("name", ["../outside", "/outside", "nested/../../outside", "..\\outside"])
def test_stream_extraction_rejects_traversal(tmp_path, name):
    target = tmp_path / "target"
    target.mkdir()
    program = docker_state.EXTRACT_PROGRAM.replace("root = '/target'", f"root = {str(target)!r}")
    result = subprocess.run([sys.executable, "-c", program], input=archive_bytes(name), capture_output=True, check=False)
    assert result.returncode != 0
    assert b"unsafe archive path" in result.stderr
    assert list(target.iterdir()) == []


def test_new_backup_directory_is_private_on_posix(tmp_path):
    path = docker_state._new_directory(tmp_path / "private-backup")
    if os.name != "nt":
        assert path.stat().st_mode & 0o777 == 0o700
