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
            for service in ("postgres", "api", "worker", "delivery-worker", "web")
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


def test_backup_inventory_requires_delivery_secret_volumes():
    state = sample_inventory()
    state["volumes"] = [
        item
        for item in state["volumes"]
        if item["logical_name"] != "delivery_keys"
    ]
    with pytest.raises(docker_state.OperationError, match="persistente completo"):
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
    assert receipt["removed"] == {"containers": 5, "volumes": 6, "networks": 1}
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
        "stage_verified_backup",
        lambda _source, _destination: {
            "schema_version": 2,
            "source_project": "trackvance-source-test",
        },
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


def write_legacy_backup(
    root: Path,
    *,
    migration: str = "0007_monitor_scheduling",
    state_schema_version: int = 2,
):
    root.mkdir()
    (root / "volumes").mkdir()
    tables = {table: {} for table in docker_state.LEGACY_STATE_TABLES}
    tables["jobs"] = {"job": "1" * 64}
    state = {
        "schema_version": state_schema_version,
        "migration": migration,
        "tables": tables,
        "verified_artifacts": 0,
        "verified_secrets": 0,
        "validated_relationships": 0,
    }
    (root / "state.json").write_text(json.dumps(state), encoding="utf-8")
    (root / "postgres.dump").write_bytes(b"PGDMPlegacy-test")
    components = {}
    for relative in ("state.json", "postgres.dump"):
        path = root / relative
        components[relative] = {
            "path": relative,
            "sha256": docker_state.digest(path),
            "size_bytes": path.stat().st_size,
        }
    for logical in docker_state.LEGACY_ARCHIVED_VOLUMES:
        relative = f"volumes/{logical}.tar.gz"
        path = root / relative
        path.write_bytes(archive_bytes(f"{logical}.txt", logical.encode()))
        components[relative] = {
            "path": relative,
            "sha256": docker_state.digest(path),
            "size_bytes": path.stat().st_size,
            "entries": docker_state.inspect_archive(path),
        }
    manifest = {
        "schema_version": 1,
        "consistency": "quiesced",
        "migration": migration,
        "components": components,
    }
    (root / "backup-manifest.json").write_text(
        json.dumps(manifest), encoding="utf-8"
    )
    return manifest, state


def rewrite_legacy_state(root: Path, manifest: dict, state: dict) -> None:
    path = root / "state.json"
    path.write_text(json.dumps(state), encoding="utf-8")
    manifest["components"]["state.json"] = {
        "path": "state.json",
        "sha256": docker_state.digest(path),
        "size_bytes": path.stat().st_size,
    }
    (root / "backup-manifest.json").write_text(
        json.dumps(manifest), encoding="utf-8"
    )


def write_delivery_backup(root: Path, *, current: bool = False):
    manifest, state = write_legacy_backup(root)
    manifest["schema_version"] = 2
    manifest["migration"] = "0009_delivery_reviews" if current else "0008_data_delivery"
    state["schema_version"] = 4 if current else 3
    state["migration"] = manifest["migration"]
    for name in docker_state.DELIVERY_TABLES:
        state["tables"][name] = {}
    if current:
        state["tables"]["delivery_reviews"] = {}
    state["tables"]["delivery_destinations"] = {"destination": "a" * 64}
    state["tables"]["delivery_destination_versions"] = {"revision": "b" * 64}
    state["tables"]["delivery_attempts"] = {"committed": "c" * 64, "unknown": "d" * 64}
    state["verified_source_secrets"] = 0
    state["verified_delivery_secrets"] = state["verified_secrets"] = 1
    for logical in set(docker_state.ARCHIVED_VOLUMES) - set(docker_state.LEGACY_ARCHIVED_VOLUMES):
        relative = f"volumes/{logical}.tar.gz"
        path = root / relative
        path.write_bytes(archive_bytes(f"{logical}.txt", b"ciphertext-fixture"))
        manifest["components"][relative] = {
            "path": relative, "sha256": docker_state.digest(path),
            "size_bytes": path.stat().st_size, "entries": docker_state.inspect_archive(path),
        }
    rewrite_legacy_state(root, manifest, state)
    return manifest, state


@pytest.mark.parametrize("current", [False, True])
def test_verify_backup_accepts_frozen_complete_delivery_format(tmp_path, current):
    root = tmp_path / "backup"
    manifest, _state = write_delivery_backup(root, current=current)
    assert docker_state.verify_backup(root) == manifest


@pytest.mark.parametrize("current", [False, True])
@pytest.mark.parametrize("damage", [
    "missing_table", "extra_table", "invalid_hash", "boolean_counter",
    "source_count", "delivery_count", "total_count", "artifact_count",
])
def test_verify_backup_rejects_incomplete_native_fingerprint_before_restore(tmp_path, current, damage):
    root = tmp_path / "backup"
    manifest, state = write_delivery_backup(root, current=current)
    if damage == "missing_table":
        state["tables"].pop("delivery_attempts")
    elif damage == "extra_table":
        state["tables"]["unexpected"] = {}
    elif damage == "invalid_hash":
        state["tables"]["delivery_attempts"]["unknown"] = "not-a-sha256"
    elif damage == "boolean_counter":
        state["verified_artifacts"] = False
    elif damage == "source_count":
        state["verified_source_secrets"] = 1
    elif damage == "delivery_count":
        state["verified_delivery_secrets"] = 0
    elif damage == "total_count":
        state["verified_secrets"] = 2
    else:
        state["verified_artifacts"] = 1
    rewrite_legacy_state(root, manifest, state)
    with pytest.raises(docker_state.OperationError, match="inventario|hashes|contadores"):
        docker_state.verify_backup(root)


@pytest.mark.parametrize("command", ["snapshot", "snapshot-legacy-v2", "snapshot-legacy-v3"])
def test_copy_snapshot_allows_each_supported_real_command(monkeypatch, tmp_path, command):
    calls = []
    expected = {"schema_version": 3, "migration": "0008_data_delivery", "tables": {}}

    def execute(arguments, **kwargs):
        calls.append(arguments)
        return json.dumps(expected) if arguments[:2] == ["docker", "exec"] else ""

    monkeypatch.setattr(docker_state, "execute", execute)
    destination = tmp_path / "state.json"
    assert docker_state._copy_snapshot("fixture-api", destination, command=command) == expected
    assert json.loads(destination.read_text()) == expected
    assert calls[-1] == ["docker", "exec", "fixture-api", "python", "/tmp/verify_storage.py", command]


def test_verify_backup_accepts_exact_041_format_and_routes_only_legacy_volumes(
    tmp_path,
):
    root = tmp_path / "backup"
    manifest, _ = write_legacy_backup(root)

    assert docker_state.verify_backup(root) == manifest
    assert docker_state.archived_volumes_for_backup(1) == (
        "trackvance_data",
        "connection_credentials",
        "connection_keys",
    )
    assert set(docker_state.archived_volumes_for_backup(2)) == {
        "trackvance_data",
        "connection_credentials",
        "connection_keys",
        "delivery_credentials",
        "delivery_keys",
    }


def test_stage_verified_backup_is_private_and_read_only(tmp_path):
    source = tmp_path / "source"
    manifest, _ = write_legacy_backup(source)
    staged = tmp_path / "private-stage"

    assert docker_state.stage_verified_backup(source, staged) == manifest
    assert docker_state.verify_backup(staged) == manifest
    assert all(
        path.stat().st_mode & 0o222 == 0
        for path in staged.rglob("*")
        if path.is_file()
    )
    if os.name != "nt":
        assert staged.stat().st_mode & 0o777 == 0o700


@pytest.mark.parametrize(
    ("migration", "state_schema_version", "message"),
    [
        ("0006_local_identity_exceptions", 2, "baseline 0.4.1"),
        ("0007_monitor_scheduling", 3, "huella persistente"),
    ],
)
def test_verify_backup_rejects_v1_outside_exact_041_baseline(
    tmp_path, migration, state_schema_version, message
):
    root = tmp_path / "backup"
    write_legacy_backup(
        root,
        migration=migration,
        state_schema_version=state_schema_version,
    )

    with pytest.raises(docker_state.OperationError, match=message):
        docker_state.verify_backup(root)


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        ("missing_table", "inventario"),
        ("extra_table", "inventario"),
        ("malformed_hash", "hashes"),
        ("invalid_counter", "contadores"),
    ],
)
def test_verify_backup_rejects_incomplete_or_malformed_v1_state(
    tmp_path, mutation, message
):
    root = tmp_path / "backup"
    manifest, state = write_legacy_backup(root)
    if mutation == "missing_table":
        state["tables"].pop("users")
    elif mutation == "extra_table":
        state["tables"]["delivery_attempts"] = {}
    elif mutation == "malformed_hash":
        state["tables"]["jobs"]["job"] = "not-a-sha256"
    else:
        state["verified_artifacts"] = True
    rewrite_legacy_state(root, manifest, state)

    with pytest.raises(docker_state.OperationError, match=message):
        docker_state.verify_backup(root)


def test_verify_backup_rejects_delivery_component_in_v1_even_with_valid_hash(tmp_path):
    root = tmp_path / "backup"
    manifest, _ = write_legacy_backup(root)
    relative = "volumes/delivery_credentials.tar.gz"
    path = root / relative
    path.write_bytes(archive_bytes("unexpected.secret", b"ciphertext"))
    manifest["components"][relative] = {
        "path": relative,
        "sha256": docker_state.digest(path),
        "size_bytes": path.stat().st_size,
        "entries": docker_state.inspect_archive(path),
    }
    (root / "backup-manifest.json").write_text(json.dumps(manifest), encoding="utf-8")

    with pytest.raises(docker_state.OperationError, match="componentes"):
        docker_state.verify_backup(root)


def restored_current_state():
    return {
        "schema_version": docker_state.VERIFY_SCHEMA_VERSION,
        "migration": docker_state.CURRENT_MIGRATION,
        "tables": {table: {} for table in docker_state.DELIVERY_TABLES | {"delivery_reviews"}},
        "verified_secrets": 0,
        "verified_source_secrets": 0,
        "verified_delivery_secrets": 0,
    }


def test_validate_restored_state_accepts_exact_normalized_041_upgrade(tmp_path):
    manifest, expected = write_legacy_backup(tmp_path / "backup")

    docker_state.validate_restored_state(
        manifest,
        expected,
        restored_current_state(),
        normalized_legacy_state=expected,
    )


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        ("migration", "migración segura"),
        ("delivery_rows", "migración segura"),
        ("delivery_secrets", "migración segura"),
        ("normalized_job", "normalizada"),
    ],
)
def test_validate_restored_state_rejects_incomplete_or_changed_041_upgrade(
    tmp_path, mutation, message
):
    manifest, expected = write_legacy_backup(tmp_path / "backup")
    restored = restored_current_state()
    normalized = json.loads(json.dumps(expected))
    if mutation == "migration":
        restored["migration"] = "0007_monitor_scheduling"
    elif mutation == "delivery_rows":
        restored["tables"]["delivery_attempts"] = {"attempt": "hash"}
    elif mutation == "delivery_secrets":
        restored["verified_delivery_secrets"] = 1
        restored["verified_secrets"] = 2
    else:
        normalized["tables"]["jobs"]["job"] = "changed-job-hash"

    with pytest.raises(docker_state.OperationError, match=message):
        docker_state.validate_restored_state(
            manifest,
            expected,
            restored,
            normalized_legacy_state=normalized,
        )


def test_restore_v1_uses_legacy_archives_and_post_migration_normalization(
    monkeypatch, tmp_path
):
    source = tmp_path / "backup"
    manifest, expected = write_legacy_backup(source)
    manifest["source_project"] = "trackvance-source-test"
    (source / "backup-manifest.json").write_text(
        json.dumps(manifest), encoding="utf-8"
    )
    state = sample_inventory("trackvance-restore-test")
    extracted = []
    snapshot_commands = []

    monkeypatch.setattr(docker_state, "validate_postgres_dump", lambda _source: None)
    monkeypatch.setattr(docker_state, "ensure_fresh_project", lambda _project: None)
    monkeypatch.setattr(
        docker_state,
        "compose_services",
        lambda _project, _environment: list(docker_state.PRIMARY_SERVICES),
    )
    monkeypatch.setattr(docker_state, "compose", lambda *args, **kwargs: "")
    monkeypatch.setattr(docker_state, "inventory", lambda _project: state)
    monkeypatch.setattr(docker_state, "execute", lambda *args, **kwargs: "")
    monkeypatch.setattr(
        docker_state,
        "_extract_volume",
        lambda _root, archive, _volume, _image: extracted.append(archive),
    )

    def snapshot(_api, _destination, *, command="snapshot"):
        snapshot_commands.append(command)
        return restored_current_state() if command == "snapshot" else expected

    monkeypatch.setattr(docker_state, "_copy_snapshot", snapshot)

    receipt = docker_state.restore(source, "trackvance-restore-test")

    assert receipt["status"] == "STOPPED_VERIFIED"
    assert snapshot_commands == ["snapshot", "snapshot-legacy-v2"]
    assert extracted == [
        f"volumes/{logical}.tar.gz"
        for logical in docker_state.LEGACY_ARCHIVED_VOLUMES
    ]
    assert not any("delivery_" in archive for archive in extracted)


def test_restore_050_uses_real_snapshot_command_routing_and_all_delivery_volumes(monkeypatch, tmp_path):
    source = tmp_path / "backup"
    manifest, expected = write_delivery_backup(source)
    manifest["source_project"] = "trackvance-source-test"
    rewrite_legacy_state(source, manifest, expected)
    restored = json.loads(json.dumps(expected))
    restored.update(schema_version=4, migration="0009_delivery_reviews")
    restored["tables"]["delivery_reviews"] = {}
    state = sample_inventory("trackvance-restore-test")
    extracted, snapshot_commands = [], []
    monkeypatch.setattr(docker_state, "validate_postgres_dump", lambda _source: None)
    monkeypatch.setattr(docker_state, "ensure_fresh_project", lambda _project: None)
    monkeypatch.setattr(docker_state, "compose_services", lambda *_args: list(docker_state.PRIMARY_SERVICES))
    monkeypatch.setattr(docker_state, "compose", lambda *args, **kwargs: "")
    monkeypatch.setattr(docker_state, "inventory", lambda _project: state)
    monkeypatch.setattr(docker_state, "_extract_volume",
                        lambda _root, archive, _volume, _image: extracted.append(archive))

    def execute(arguments, **kwargs):
        if arguments[:2] == ["docker", "exec"] and arguments[-1] in {"snapshot", "snapshot-legacy-v3"}:
            snapshot_commands.append(arguments[-1])
            return json.dumps(restored if arguments[-1] == "snapshot" else expected)
        return ""

    # Keep _copy_snapshot real so its allowlist and actual command construction
    # are exercised, not hidden behind the orchestration fixture.
    monkeypatch.setattr(docker_state, "execute", execute)
    receipt = docker_state.restore(source, "trackvance-restore-test")
    assert receipt["status"] == "STOPPED_VERIFIED"
    assert snapshot_commands == ["snapshot", "snapshot-legacy-v3"]
    assert extracted == [f"volumes/{name}.tar.gz" for name in docker_state.ARCHIVED_VOLUMES]


def test_validate_restored_050_keeps_delivery_and_projects_only_empty_review_table():
    manifest = {"schema_version": 2, "migration": "0008_data_delivery"}
    expected = restored_current_state()
    expected["schema_version"] = 3
    expected["migration"] = "0008_data_delivery"
    expected["tables"].pop("delivery_reviews")
    expected["tables"]["delivery_attempts"] = {"committed": "immutable-hash"}
    restored = restored_current_state()
    restored["tables"]["delivery_attempts"] = expected["tables"]["delivery_attempts"].copy()
    docker_state.validate_restored_state(manifest, expected, restored, expected)
    restored["tables"]["delivery_reviews"]["unexpected"] = "hash"
    with pytest.raises(docker_state.OperationError, match="0.5.0 normalizada"):
        docker_state.validate_restored_state(manifest, expected, restored, expected)


@pytest.mark.parametrize("mutation", ["migration", "schema", "normalization"])
def test_validate_restored_050_rejects_partial_or_changed_upgrade(mutation):
    manifest = {"schema_version": 2, "migration": "0008_data_delivery"}
    expected = {"schema_version": 3, "migration": "0008_data_delivery", "tables": {}}
    restored = restored_current_state()
    normalized = dict(expected)
    if mutation == "migration":
        restored["migration"] = "0008_data_delivery"
    elif mutation == "schema":
        restored["schema_version"] = 3
    else:
        normalized["verified_artifacts"] = 99
    with pytest.raises(docker_state.OperationError, match="0.5.0 normalizada"):
        docker_state.validate_restored_state(manifest, expected, restored, normalized)


def test_restore_uses_only_private_stage_after_source_changes(monkeypatch, tmp_path):
    source = tmp_path / "backup"
    manifest, expected = write_legacy_backup(source)
    manifest["source_project"] = "trackvance-source-test"
    (source / "backup-manifest.json").write_text(
        json.dumps(manifest), encoding="utf-8"
    )
    original_state_sha256 = docker_state.digest(source / "state.json")
    target_state = sample_inventory("trackvance-restore-test")
    validated_roots = []
    extraction_roots = []
    dump_sources = []

    def mutate_original_after_staging(staged_root):
        validated_roots.append(staged_root)
        assert staged_root.resolve() != source.resolve()
        swapped = json.loads(json.dumps(expected))
        swapped["tables"]["jobs"]["job"] = "2" * 64
        (source / "state.json").write_text(json.dumps(swapped), encoding="utf-8")

    def execute(arguments, **_kwargs):
        if arguments[:2] == ["docker", "cp"] and str(arguments[2]).endswith(
            "postgres.dump"
        ):
            dump_sources.append(Path(arguments[2]))
        return ""

    monkeypatch.setattr(
        docker_state, "validate_postgres_dump", mutate_original_after_staging
    )
    monkeypatch.setattr(docker_state, "ensure_fresh_project", lambda _project: None)
    monkeypatch.setattr(
        docker_state,
        "compose_services",
        lambda _project, _environment: list(docker_state.PRIMARY_SERVICES),
    )
    monkeypatch.setattr(docker_state, "compose", lambda *args, **kwargs: "")
    monkeypatch.setattr(docker_state, "inventory", lambda _project: target_state)
    monkeypatch.setattr(docker_state, "execute", execute)
    monkeypatch.setattr(
        docker_state,
        "_extract_volume",
        lambda root, _archive, _volume, _image: extraction_roots.append(root),
    )
    monkeypatch.setattr(
        docker_state,
        "_copy_snapshot",
        lambda _api, _destination, *, command="snapshot": (
            restored_current_state() if command == "snapshot" else expected
        ),
    )

    receipt = docker_state.restore(source, "trackvance-restore-test")

    assert receipt["status"] == "STOPPED_VERIFIED"
    assert docker_state.digest(source / "state.json") != original_state_sha256
    assert receipt["verified_state_sha256"] == original_state_sha256
    assert len(validated_roots) == 1
    assert extraction_roots and set(extraction_roots) == {validated_roots[0]}
    assert len(dump_sources) == 1
    assert dump_sources[0].parent == validated_roots[0]


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
