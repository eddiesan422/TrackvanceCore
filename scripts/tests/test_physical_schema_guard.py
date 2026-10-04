"""Real SQLite catalogs validate the guard without touching application data."""

import importlib.util
from pathlib import Path

import pytest
from sqlalchemy import Column, ForeignKeyConstraint, Integer, MetaData, String, Table, create_engine
from sqlalchemy.orm import Session

SCRIPT = Path(__file__).resolve().parents[1] / "physical_schema_guard.py"
spec = importlib.util.spec_from_file_location("physical_guard_test", SCRIPT)
guard = importlib.util.module_from_spec(spec)
spec.loader.exec_module(guard)


def contract():
    metadata = MetaData()
    Table("parent", metadata, Column("id", Integer, primary_key=True), Column("alternate", String, unique=True))
    Table("child", metadata, Column("id", Integer, primary_key=True), Column("parent_id", Integer),
          Column("label", String), ForeignKeyConstraint(["parent_id"], ["parent.id"], name="model_fk"))
    return metadata


def database(*, child="id INTEGER PRIMARY KEY, parent_id INTEGER, label TEXT, FOREIGN KEY(parent_id) REFERENCES parent(id)"):
    engine = create_engine("sqlite+pysqlite:///:memory:")
    with engine.begin() as connection:
        connection.exec_driver_sql("CREATE TABLE parent (id INTEGER PRIMARY KEY, alternate TEXT UNIQUE)")
        connection.exec_driver_sql(f"CREATE TABLE child ({child})")
        connection.exec_driver_sql("CREATE TABLE alembic_version (version_num TEXT PRIMARY KEY)")
        connection.exec_driver_sql("INSERT INTO parent VALUES (1, 'preserved')")
    return engine


def test_normal_contract_accepts_alembic_and_preserves_data():
    engine = database()
    assert guard.validate_physical_schema(engine, contract()) == {"tables": 2, "columns": 5, "foreign_keys": 1}
    with engine.connect() as connection:
        assert connection.exec_driver_sql("SELECT alternate FROM parent").scalar_one() == "preserved"


@pytest.mark.parametrize("statement", ["CREATE TABLE unknown_private_table (id INTEGER)", "DROP TABLE child"])
def test_unknown_or_missing_tables_fail_closed(statement):
    engine = database()
    with engine.begin() as connection:
        connection.exec_driver_sql(statement)
    with pytest.raises(guard.PhysicalSchemaMismatch) as caught:
        guard.validate_physical_schema(engine, contract())
    assert caught.value.code == "PHYSICAL_TABLES_MISMATCH"
    assert "unknown_private_table" not in str(caught.value)


@pytest.mark.parametrize("child", [
    "id INTEGER PRIMARY KEY, parent_id INTEGER, label TEXT, unexpected TEXT, FOREIGN KEY(parent_id) REFERENCES parent(id)",
    "id INTEGER PRIMARY KEY, parent_id INTEGER, FOREIGN KEY(parent_id) REFERENCES parent(id)",
])
def test_extra_or_missing_columns_fail_without_changes(child):
    engine = database(child=child)
    with pytest.raises(guard.PhysicalSchemaMismatch) as caught:
        guard.validate_physical_schema(engine, contract())
    assert caught.value.code == "PHYSICAL_COLUMNS_MISMATCH"
    with engine.connect() as connection:
        assert connection.exec_driver_sql("SELECT alternate FROM parent").scalar_one() == "preserved"


@pytest.mark.parametrize("definition", [
    "",
    ", FOREIGN KEY(parent_id) REFERENCES parent(alternate)",
    ", FOREIGN KEY(parent_id) REFERENCES parent(id), FOREIGN KEY(label) REFERENCES parent(alternate)",
    ", FOREIGN KEY(parent_id) REFERENCES parent(id) ON DELETE CASCADE",
])
def test_missing_extra_or_divergent_foreign_keys_fail(definition):
    engine = database(child="id INTEGER PRIMARY KEY, parent_id INTEGER, label TEXT" + definition)
    with pytest.raises(guard.PhysicalSchemaMismatch) as caught:
        guard.validate_physical_schema(engine, contract())
    assert caught.value.code == "PHYSICAL_FOREIGN_KEYS_MISMATCH"


def test_constraint_names_and_sql_type_spelling_are_not_compared():
    engine = database(child="id INTEGER PRIMARY KEY, parent_id TEXT, label BLOB, "
                      "CONSTRAINT old_runtime_fk FOREIGN KEY(parent_id) REFERENCES parent(id) ON DELETE NO ACTION")
    assert guard.validate_physical_schema(engine, contract())["foreign_keys"] == 1


def test_composite_constraints_are_not_flattened_into_independent_links():
    metadata = MetaData()
    Table("parent", metadata, Column("id", Integer, primary_key=True), Column("alternate", String, primary_key=True))
    Table("child", metadata, Column("id", Integer, primary_key=True), Column("parent_id", Integer),
          Column("label", String), ForeignKeyConstraint(["parent_id", "label"], ["parent.id", "parent.alternate"]))
    engine = database(child="id INTEGER PRIMARY KEY, parent_id INTEGER, label TEXT, "
                      "FOREIGN KEY(parent_id) REFERENCES parent(id), FOREIGN KEY(label) REFERENCES parent(alternate)")
    with pytest.raises(guard.PhysicalSchemaMismatch, match="claves foráneas"):
        guard.validate_physical_schema(engine, metadata)


def test_installed_runtime_contract_and_snapshot_reject_physical_extra_column(monkeypatch):
    from trackvance.db import Base

    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    assert guard.validate_physical_schema(engine, Base.metadata)["tables"] == 42
    with engine.begin() as connection:
        connection.exec_driver_sql("CREATE TABLE alembic_version (version_num TEXT PRIMARY KEY)")
        connection.exec_driver_sql("INSERT INTO alembic_version VALUES ('0015_sentinel_execution_identity')")
        connection.exec_driver_sql("ALTER TABLE users ADD COLUMN undocumented_private_field TEXT")
    verifier_spec = importlib.util.spec_from_file_location("verifier_guard_test", SCRIPT.with_name("verify_storage.py"))
    verifier = importlib.util.module_from_spec(verifier_spec)
    verifier_spec.loader.exec_module(verifier)
    monkeypatch.setattr("trackvance.db.SessionLocal", lambda: Session(engine))
    with pytest.raises(ValueError, match="columnas desconocidas"):
        verifier._snapshot_inputs()

