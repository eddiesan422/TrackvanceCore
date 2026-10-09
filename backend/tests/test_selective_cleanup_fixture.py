"""Validate the synthetic fixture graph locally; this is not PostgreSQL certification."""
import importlib.util
from pathlib import Path

import pytest
from sqlalchemy import text

from trackvance import credential_store, delivery_credential_store
from trackvance.config import STORAGE_DIR
from trackvance.credential_store import EncryptedFileSecretStore
from trackvance.operational_cleanup import create_plan, population

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location("cleanup_fixture_test", ROOT / "scripts/tests/selective_cleanup_fixture.py")
fixture = importlib.util.module_from_spec(spec)
spec.loader.exec_module(fixture)


def test_fixture_refuses_habitual_project_before_application_import(monkeypatch):
    monkeypatch.setenv("TRACKVANCE_CERTIFICATION_PROJECT", "trackvance-certification")
    with pytest.raises(ValueError, match="exact owned"):
        fixture.build("trackvance-certification")


def test_fixture_requires_native_postgres(database):
    with database() as db, pytest.raises(ValueError, match="requires PostgreSQL"):
        fixture.require_native_database(db)


def test_fixture_populates_complete_referential_graph_without_remote_io(authenticated, database, monkeypatch, tmp_path):
    import sys
    sys.path.insert(0, str(ROOT / "scripts"))
    sys.path.insert(0, str(ROOT / "scripts/tests"))
    import selective_cleanup_recovery
    import verify_storage

    project = "trackvance-v080-test-restore085-012345abcdef"
    monkeypatch.setenv("TRACKVANCE_CERTIFICATION_PROJECT", project)
    monkeypatch.setattr(fixture, "require_native_database", lambda _db: None)
    monkeypatch.setattr(credential_store, "secret_store", EncryptedFileSecretStore(tmp_path / "source", tmp_path / "source-keys"))
    monkeypatch.setattr(delivery_credential_store, "destination_secret_store", EncryptedFileSecretStore(tmp_path / "destination", tmp_path / "destination-keys"))
    assert authenticated.post("/api/v1/governance/people", json={"name": "Fixture shared contact"}).status_code == 201
    result = fixture.build(project)
    with database() as db:
        db.execute(text("UPDATE alembic_version SET version_num='0019_governance_people'"))
        db.commit()
        rows, _schema, _edges = population(db)
        from trackvance.db import Base
        fks = [(name, fk.parent.name, fk.column.table.name, fk.column.name)
            for name, table in Base.metadata.tables.items() for fk in table.foreign_keys]
        assert verify_storage.validate_relationships({name: list(items.values()) for name, items in rows.items()}, fks) > 0
        plan = create_plan(db, project, result["organization_id"], result["dataset_ids"], STORAGE_DIR)
        selective_cleanup_recovery.assert_plan(plan, {"tables": plan["scope"]["state_tables"]}, result)
