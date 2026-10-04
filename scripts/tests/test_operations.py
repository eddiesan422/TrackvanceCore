import importlib.util
import json
import sqlite3
from contextlib import closing
from pathlib import Path

import pytest

from trackvance.credential_store import EncryptedFileSecretStore

SCRIPT = Path(__file__).resolve().parents[1] / "backup_local.py"
spec = importlib.util.spec_from_file_location("backup_local", SCRIPT)
backup_module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(backup_module)


@pytest.fixture
def runtime(tmp_path):
    root = tmp_path / "runtime"
    storage = root / "storage"
    storage.mkdir(parents=True)
    original, canonical = storage / "original.csv", storage / "canonical.parquet"
    original.write_bytes(b"customer_id\n001234567\n")
    canonical.write_bytes(b"test artifact bytes")
    with sqlite3.connect(root / "trackvance.db") as connection:
        connection.execute("CREATE TABLE dataset_versions (id TEXT PRIMARY KEY, original_path TEXT, canonical_path TEXT)")
        connection.execute("INSERT INTO dataset_versions VALUES (?, ?, ?)", ("immutable-version", str(original), str(canonical)))
        connection.commit()
    return root


def add_connection_secret(runtime, secret="private-connection-password"):
    store = EncryptedFileSecretStore(runtime / "credentials", runtime / "keys/master.key")
    reference = store.put("org-a", secret)
    with sqlite3.connect(runtime / "trackvance.db") as connection:
        connection.execute(
            "CREATE TABLE external_connection_versions "
            "(id TEXT PRIMARY KEY, organization_id TEXT, secret_reference TEXT)"
        )
        connection.execute(
            "INSERT INTO external_connection_versions VALUES (?, ?, ?)",
            ("connection-version", "org-a", reference),
        )
        connection.commit()
    return reference


def add_delivery_secret(runtime, secret="private-delivery-password"):
    store = EncryptedFileSecretStore(
        runtime / "delivery_credentials", runtime / "delivery_keys/master.key"
    )
    reference = store.put("org-a", secret)
    with sqlite3.connect(runtime / "trackvance.db") as connection:
        connection.execute(
            "CREATE TABLE delivery_destination_versions "
            "(id TEXT PRIMARY KEY, organization_id TEXT, secret_reference TEXT)"
        )
        connection.execute(
            "INSERT INTO delivery_destination_versions VALUES (?, ?, ?)",
            ("destination-version", "org-a", reference),
        )
        connection.commit()
    return reference


def test_backup_restore_preserves_identity_and_bytes(runtime, tmp_path):
    backup, restored = tmp_path / "backup", tmp_path / "restored"
    original_hash = backup_module.digest(runtime / "trackvance.db")
    backup_module.backup(runtime, backup)
    backup_module.restore(backup, restored)
    with sqlite3.connect(restored / "trackvance.db") as connection:
        identifier, original, canonical = connection.execute("SELECT * FROM dataset_versions").fetchone()
    assert identifier == "immutable-version"
    assert Path(original).is_relative_to(restored)
    assert Path(canonical).read_bytes() == (runtime / "storage/canonical.parquet").read_bytes()
    assert backup_module.digest(runtime / "trackvance.db") == original_hash
    assert json.loads((restored / "restoration.json").read_text())["database_sha256"]


def add_multipart_dataset(runtime):
    storage = runtime / 'storage'
    part = storage / 'datasets/part-00000.parquet'
    part.parent.mkdir()
    part.write_bytes(b'immutable parquet partition')
    descriptor = storage / 'datasets/canonical.parquet-set.json'
    descriptor.write_text(json.dumps({'schema_version': 1, 'kind': 'PARQUET_DATASET', 'schema': [],
        'row_count': 2, 'data_size_bytes': part.stat().st_size, 'parts': [{'ordinal': 0,
        'artifact_id': 'part', 'path': 'datasets/part-00000.parquet', 'row_count': 2,
        'sha256': backup_module.digest(part), 'size_bytes': part.stat().st_size}]}), encoding='utf-8')
    with sqlite3.connect(runtime / 'trackvance.db') as db:
        db.execute('CREATE TABLE artifacts (id TEXT PRIMARY KEY, organization_id TEXT, path TEXT, sha256 TEXT, size_bytes INTEGER, media_type TEXT)')
        db.executemany('INSERT INTO artifacts VALUES (?,?,?,?,?,?)', [
            ('part', 'org', str(part), backup_module.digest(part), part.stat().st_size, 'application/vnd.apache.parquet'),
            ('set', 'org', str(descriptor), backup_module.digest(descriptor), descriptor.stat().st_size, 'application/vnd.trackvance.parquet-set+json')])
        db.execute('UPDATE dataset_versions SET canonical_path=?', (str(descriptor),))
        db.commit()
    return descriptor, part


def test_multipart_restore_relocates_metadata_preserving_descriptor_and_partition_bytes(runtime, tmp_path):
    descriptor, part = add_multipart_dataset(runtime)
    original_descriptor, original_part = descriptor.read_bytes(), part.read_bytes()
    backup, restored = tmp_path / 'backup', tmp_path / 'portable'
    backup_module.backup(runtime, backup)
    backup_module.restore(backup, restored)
    assert (restored / 'storage/datasets/canonical.parquet-set.json').read_bytes() == original_descriptor
    assert (restored / 'storage/datasets/part-00000.parquet').read_bytes() == original_part
    with sqlite3.connect(restored / 'trackvance.db') as db:
        assert all(Path(row[0]).is_relative_to(restored / 'storage') for row in db.execute('SELECT path FROM artifacts'))
        backup_module.validate_multipart(db, restored / 'storage')


@pytest.mark.parametrize('damage', ['bytes', 'organization', 'missing_registration', 'row_total'])
def test_multipart_backup_rejects_corruption_before_creating_manifest(runtime, tmp_path, damage):
    descriptor, part = add_multipart_dataset(runtime)
    with sqlite3.connect(runtime / 'trackvance.db') as db:
        if damage == 'bytes':
            part.write_bytes(b'corrupt')
        elif damage == 'organization':
            db.execute("UPDATE artifacts SET organization_id='foreign' WHERE id='part'")
        elif damage == 'missing_registration':
            db.execute("DELETE FROM artifacts WHERE id='part'")
        else:
            payload = json.loads(descriptor.read_text())
            payload['row_count'] = 3
            descriptor.write_text(json.dumps(payload), encoding='utf-8')
            db.execute("UPDATE artifacts SET sha256=?,size_bytes=? WHERE id='set'", (backup_module.digest(descriptor), descriptor.stat().st_size))
        db.commit()
    backup = tmp_path / 'backup'
    with pytest.raises(ValueError, match='Parquet'):
        backup_module.backup(runtime, backup)
    assert not (backup / 'backup-manifest.json').exists()


def test_backup_restore_preserves_connection_secret_and_default_locations(
    runtime, tmp_path, capsys
):
    secret = "private-connection-password"
    reference = add_connection_secret(runtime, secret)
    backup, restored = tmp_path / "backup", tmp_path / "restored"

    backup_module.backup(runtime, backup)
    manifest = backup_module.verify(backup)
    backup_module.restore(backup, restored)

    assert manifest["schema_version"] == 3
    assert "keys/master.key" in manifest["files"]
    assert any(name.startswith("credentials/") for name in manifest["files"])
    restored_store = EncryptedFileSecretStore(
        restored / "credentials", restored / "keys/master.key"
    )
    assert restored_store.get("org-a", reference) == secret
    output = capsys.readouterr()
    assert secret not in output.out
    assert secret not in output.err


def test_backup_restore_preserves_separate_delivery_secret_store(
    runtime, tmp_path, capsys
):
    secret = "private-delivery-password"
    reference = add_delivery_secret(runtime, secret)
    backup, restored = tmp_path / "backup", tmp_path / "restored"

    backup_module.backup(runtime, backup)
    manifest = backup_module.verify(backup)
    backup_module.restore(backup, restored)

    assert "delivery_keys/master.key" in manifest["files"]
    assert any(
        name.startswith("delivery_credentials/") for name in manifest["files"]
    )
    restored_store = EncryptedFileSecretStore(
        restored / "delivery_credentials", restored / "delivery_keys/master.key"
    )
    assert restored_store.get("org-a", reference) == secret
    output = capsys.readouterr()
    assert secret not in output.out
    assert secret not in output.err


def test_corrupt_backup_fails_before_creating_restore_destination(runtime, tmp_path):
    backup, restored = tmp_path / "backup", tmp_path / "restored"
    backup_module.backup(runtime, backup)
    (backup / "storage/original.csv").write_bytes(b"corrupt")
    with pytest.raises(ValueError, match="Integridad"):
        backup_module.restore(backup, restored)
    assert not restored.exists()


def test_restore_rejects_source_mutation_between_verify_and_copy(
    runtime, tmp_path, monkeypatch
):
    backup, restored = tmp_path / "backup", tmp_path / "restored"
    backup_module.backup(runtime, backup)
    real_verify = backup_module.verify

    def verify_then_mutate(source):
        manifest = real_verify(source)
        with sqlite3.connect(source / "trackvance.db") as connection:
            connection.execute(
                "UPDATE dataset_versions SET id='mutated-after-verify'"
            )
            connection.commit()
        return manifest

    monkeypatch.setattr(backup_module, "verify", verify_then_mutate)

    with pytest.raises(ValueError, match="Integridad.*copia"):
        backup_module.restore(backup, restored)

    assert not (restored / "restoration.json").exists()


@pytest.mark.parametrize("material", ["credential", "key"])
def test_corrupt_connection_material_fails_before_restore(runtime, tmp_path, material):
    add_connection_secret(runtime)
    backup, restored = tmp_path / "backup", tmp_path / "restored"
    backup_module.backup(runtime, backup)
    manifest = json.loads((backup / "backup-manifest.json").read_text())
    if material == "key":
        relative = "keys/master.key"
    else:
        relative = next(name for name in manifest["files"] if name.startswith("credentials/"))
    (backup / relative).write_bytes(b"corrupt")

    with pytest.raises(ValueError, match="Integridad"):
        backup_module.restore(backup, restored)
    assert not restored.exists()


@pytest.mark.parametrize("material", ["credential", "key"])
def test_missing_delivery_material_fails_before_restore(runtime, tmp_path, material):
    add_delivery_secret(runtime)
    backup, restored = tmp_path / "backup", tmp_path / "restored"
    backup_module.backup(runtime, backup)
    manifest_path = backup / "backup-manifest.json"
    manifest = json.loads(manifest_path.read_text())
    if material == "key":
        removed_names = ["delivery_keys/master.key"]
    else:
        removed_names = [
            name
            for name in manifest["files"]
            if name.startswith("delivery_credentials/")
        ]
    for name in removed_names:
        (backup / name).unlink()
        del manifest["files"][name]
    manifest_path.write_text(json.dumps(manifest))

    with pytest.raises(ValueError, match="Data Delivery"):
        backup_module.restore(backup, restored)
    assert not restored.exists()


def test_existing_or_nested_destinations_are_rejected(runtime, tmp_path):
    with pytest.raises(ValueError, match="dentro"):
        backup_module.backup(runtime, runtime / "backup")
    existing = tmp_path / "existing"
    existing.mkdir()
    with pytest.raises(ValueError, match="ya existe"):
        backup_module.backup(runtime, existing)

    backup = tmp_path / "backup"
    backup_module.backup(runtime, backup)
    with pytest.raises(ValueError, match="ya existe"):
        backup_module.restore(backup, existing)


def test_manifest_path_traversal_is_rejected(runtime, tmp_path):
    backup = tmp_path / "backup"
    backup_module.backup(runtime, backup)
    manifest_path = backup / "backup-manifest.json"
    manifest = json.loads(manifest_path.read_text())
    outside = tmp_path / "outside.csv"
    outside.write_bytes(b"outside")
    manifest["files"]["../outside.csv"] = {"sha256": backup_module.digest(outside), "size_bytes": 7}
    manifest_path.write_text(json.dumps(manifest))
    with pytest.raises(ValueError, match="Ruta"):
        backup_module.verify(backup)


def test_connection_reference_cannot_traverse_credentials(runtime, tmp_path):
    with sqlite3.connect(runtime / "trackvance.db") as connection:
        connection.execute(
            "CREATE TABLE external_connection_versions "
            "(id TEXT PRIMARY KEY, organization_id TEXT, secret_reference TEXT)"
        )
        connection.execute(
            "INSERT INTO external_connection_versions VALUES (?, ?, ?)",
            ("connection-version", "org-a", "local:../../master.key"),
        )
        connection.commit()

    with pytest.raises(ValueError, match="referencia"):
        backup_module.backup(runtime, tmp_path / "backup")


def test_schema_one_without_connections_remains_restorable(runtime, tmp_path):
    backup, restored = tmp_path / "backup", tmp_path / "restored"
    backup_module.backup(runtime, backup)
    manifest_path = backup / "backup-manifest.json"
    manifest = json.loads(manifest_path.read_text())
    manifest["schema_version"] = 1
    manifest_path.write_text(json.dumps(manifest))

    assert backup_module.verify(backup)["schema_version"] == 1
    backup_module.restore(backup, restored)
    assert (restored / "credentials").is_dir()
    assert (restored / "keys").is_dir()
    assert (restored / "delivery_credentials").is_dir()
    assert (restored / "delivery_keys").is_dir()


@pytest.mark.parametrize("missing", ["credential", "key"])
def test_schema_one_with_connection_refs_requires_credentials_and_key(runtime, tmp_path, missing):
    add_connection_secret(runtime)
    backup, restored = tmp_path / "backup", tmp_path / "restored"
    backup_module.backup(runtime, backup)
    manifest_path = backup / "backup-manifest.json"
    manifest = json.loads(manifest_path.read_text())
    manifest["schema_version"] = 1
    if missing == "key":
        removed_names = ["keys/master.key"]
    else:
        removed_names = [
            name for name in manifest["files"] if name.startswith("credentials/")
        ]
    for name in removed_names:
        (backup / name).unlink()
        del manifest["files"][name]
    manifest_path.write_text(json.dumps(manifest))

    with pytest.raises(ValueError, match="credenciales y la clave"):
        backup_module.restore(backup, restored)
    assert not restored.exists()


def test_backup_of_live_wal_database_is_portable_and_restores(runtime, tmp_path):
    backup, restored = tmp_path / "wal-backup", tmp_path / "wal-restored"
    with closing(sqlite3.connect(runtime / "trackvance.db")) as writer:
        writer.execute("PRAGMA journal_mode=WAL")
        writer.execute("UPDATE dataset_versions SET id='committed-in-wal'")
        writer.commit()
        assert (runtime / "trackvance.db-wal").exists()
        backup_module.backup(runtime, backup)
        manifest = backup_module.verify(backup)
        assert not any(name.endswith(("-wal", "-shm")) for name in manifest["files"])
        backup_module.restore(backup, restored)
        with closing(sqlite3.connect(restored / "trackvance.db")) as connection:
            assert connection.execute("SELECT id FROM dataset_versions").fetchone()[0] == "committed-in-wal"
            assert connection.execute("PRAGMA journal_mode").fetchone()[0] == "delete"
