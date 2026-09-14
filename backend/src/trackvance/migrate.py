"""Alembic bootstrap with verified adoption of the original unversioned prototype."""

import importlib.util
from pathlib import Path

from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.config import Config
from alembic.migration import MigrationContext
from alembic.operations import Operations
from alembic.script import ScriptDirectory
from sqlalchemy import create_engine, inspect

from .config import BACKEND_DIR
from .db import Base, engine


def alembic_config() -> Config:
    config = Config(str(BACKEND_DIR / "alembic.ini"))
    config.set_main_option("script_location", str(BACKEND_DIR / "migrations"))
    return config


def schema_signature(connection) -> dict:
    inspector = inspect(connection)
    return {
        name: {
            "columns": {c["name"]: (str(c["type"]).upper(), c["nullable"]) for c in inspector.get_columns(name)},
            "primary_key": inspector.get_pk_constraint(name)["constrained_columns"],
        }
        for name in inspector.get_table_names() if name != "alembic_version"
    }


def original_signature() -> dict:
    path = Path(BACKEND_DIR / "migrations/versions/0001_initial.py")
    spec = importlib.util.spec_from_file_location("trackvance_initial_migration", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    scratch = create_engine("sqlite://")
    with scratch.begin() as connection, Operations.context(MigrationContext.configure(connection)):
        module.upgrade()
        signature = schema_signature(connection)
    scratch.dispose()
    return signature


def migrate() -> None:
    from . import models  # noqa: F401

    config = alembic_config()
    with engine.connect() as connection:
        inspector = inspect(connection)
        tables = set(inspector.get_table_names())
        revision = MigrationContext.configure(connection).get_current_revision()
        if tables - {"alembic_version"} and revision is None:
            differences = compare_metadata(MigrationContext.configure(connection), Base.metadata)
            if not differences:
                baseline = "head"
            elif engine.dialect.name == "sqlite" and schema_signature(connection) == original_signature():
                baseline = "0001_initial"
            else:
                raise RuntimeError("Schema sin versión no reconocido. Se requiere revisar la base antes de migrar.")
            connection.rollback()
            config.attributes["connection"] = connection
            command.stamp(config, baseline)
            connection.commit()
        config.attributes["connection"] = connection
        command.upgrade(config, "head")
        connection.commit()


def migration_ready() -> bool:
    with engine.connect() as connection:
        actual = MigrationContext.configure(connection).get_current_revision()
    return actual == ScriptDirectory.from_config(alembic_config()).get_current_head()


if __name__ == "__main__":
    migrate()
