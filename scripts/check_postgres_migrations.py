"""Verify PostgreSQL v1 upgrade and historical preservation in a new temporary DB.

Run with the backend environment (DATABASE_URL already configured). This creates
and removes only a random, namespaced test database; it never migrates or drops
the application database. It does not print database URLs or credentials.
"""

import json
import sys
from datetime import UTC, datetime
from uuid import uuid4

from sqlalchemy.exc import SQLAlchemyError


def check() -> dict:
    import sqlalchemy as sa
    from alembic import command
    from alembic.autogenerate import compare_metadata
    from alembic.config import Config
    from alembic.migration import MigrationContext
    from sqlalchemy.engine import make_url
    from trackvance import models  # noqa: F401
    from trackvance.config import BACKEND_DIR, DATABASE_URL
    from trackvance.db import Base

    url = make_url(DATABASE_URL)
    if not url.drivername.startswith("postgresql"):
        raise ValueError("Esta comprobación requiere PostgreSQL.")
    database_name = "trackvance_migration_check_" + uuid4().hex
    admin = sa.create_engine(url, isolation_level="AUTOCOMMIT")
    test_engine = None
    created = False
    try:
        with admin.connect() as connection:
            # Name is generated here; no user-provided identifier is interpolated.
            connection.exec_driver_sql(f'CREATE DATABASE "{database_name}"')
            created = True
        test_engine = sa.create_engine(url.set(database=database_name))
        config = Config(str(BACKEND_DIR / "alembic.ini"))
        config.set_main_option("script_location", str(BACKEND_DIR / "migrations"))
        tables = ["users", "datasets", "configurations", "dataset_versions", "runs", "audit_events"]
        ids = {name: f"legacy-{name}" for name in tables}
        with test_engine.connect() as connection:
            if sa.inspect(connection).get_table_names():
                raise ValueError("La base temporal debe estar vacía.")
            connection.rollback()
            config.attributes["connection"] = connection
            command.upgrade(config, "0001_initial")
            connection.commit()
            metadata = sa.MetaData()
            metadata.reflect(bind=connection)
            for name in tables:
                table = metadata.tables[name]
                values = {}
                for column in table.columns:
                    if column.name == "id":
                        value = ids[name]
                    elif column.nullable:
                        value = None
                    elif column.foreign_keys:
                        value = ids[next(iter(column.foreign_keys)).column.table.name]
                    elif isinstance(column.type, sa.DateTime):
                        value = datetime(2026, 9, 11, tzinfo=UTC)
                    elif isinstance(column.type, sa.Boolean):
                        value = True
                    elif isinstance(column.type, sa.Integer):
                        value = 1
                    elif isinstance(column.type, sa.JSON):
                        value = {"historical": True}
                    else:
                        value = "historical"
                    values[column.name] = value
                if name == "users":
                    values.update(name="Equipo Trackvance", email="migration@trackvance.local")
                if name == "configurations":
                    values.update(config={"key_columns": ["id"], "amount_column": "amount", "tolerance": "0.01"})
                if name == "runs":
                    values.update(initiated_by="Equipo Trackvance", status="SUCCESS", metrics={"match_rate": 91.67})
                if name == "audit_events":
                    values.update(actor="Equipo Trackvance", subject_type="run", subject_id=ids["runs"])
                connection.execute(table.insert().values(**values))
            connection.commit()
            before = {name: dict(connection.execute(sa.select(metadata.tables[name])).mappings().one())
                      for name in tables}
            connection.rollback()
            command.upgrade(config, "head")
            connection.commit()
            upgraded = sa.MetaData()
            upgraded.reflect(bind=connection)
            for name in tables:
                row = connection.execute(sa.select(upgraded.tables[name])).mappings().one()
                assert all(row[key] == value for key, value in before[name].items()), name
            run = connection.execute(sa.select(upgraded.tables["runs"])).mappings().one()
            audit = connection.execute(sa.select(upgraded.tables["audit_events"])).mappings().one()
            assert (run["initiated_by_type"], run["initiated_by_id"]) == ("USER", ids["users"])
            assert (audit["actor_type"], audit["actor_id"], audit["run_id"]) == ("USER", ids["users"], ids["runs"])
            assert run["initiated_by_legacy"] and audit["actor_legacy"]
            differences = compare_metadata(MigrationContext.configure(connection), Base.metadata)
            assert not differences, differences
            connection.rollback()
            command.downgrade(config, "base")
            connection.commit()
            assert sa.inspect(connection).get_table_names() == ["alembic_version"]
            connection.rollback()
            command.upgrade(config, "head")
            connection.commit()
        return {"status": "PASS", "historical_tables_preserved": len(tables),
                "actor_backfill": "PASS", "model_parity": "PASS", "roundtrip": "PASS"}
    finally:
        if test_engine is not None:
            test_engine.dispose()
        if created:
            with admin.connect() as connection:
                connection.exec_driver_sql(f'DROP DATABASE "{database_name}"')
        admin.dispose()


if __name__ == "__main__":
    try:
        print(json.dumps(check(), indent=2))
    except (AssertionError, OSError, ValueError, SQLAlchemyError) as error:
        print(f"FAIL: {type(error).__name__}: {error}", file=sys.stderr)
        sys.exit(1)
