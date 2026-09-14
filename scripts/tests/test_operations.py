import importlib.util
import json
import sqlite3
from contextlib import closing
from pathlib import Path

import pytest

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


def test_corrupt_backup_fails_before_creating_restore_destination(runtime, tmp_path):
    backup, restored = tmp_path / "backup", tmp_path / "restored"
    backup_module.backup(runtime, backup)
    (backup / "storage/original.csv").write_bytes(b"corrupt")
    with pytest.raises(ValueError, match="Integridad"):
        backup_module.restore(backup, restored)
    assert not restored.exists()


def test_existing_or_nested_destinations_are_rejected(runtime, tmp_path):
    with pytest.raises(ValueError, match="dentro"):
        backup_module.backup(runtime, runtime / "backup")
    existing = tmp_path / "existing"
    existing.mkdir()
    with pytest.raises(ValueError, match="ya existe"):
        backup_module.backup(runtime, existing)


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
