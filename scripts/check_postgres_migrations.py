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


def seed_delivery_baseline(connection, legacy_ids: dict[str, str]) -> None:
    """Persist representative 0008 rows, not remote writes or credential files."""
    from sqlalchemy.orm import Session

    from trackvance.models import (
        Artifact,
        ArtifactLink,
        Configuration,
        DeliveryAttempt,
        DeliveryDestination,
        DeliveryDestinationVersion,
        Job,
        Run,
    )

    now = datetime(2026, 9, 11, 12, 34, 56, 123456, tzinfo=UTC)
    common = {"organization_id": "historical", "created_at": now}
    destination_id, revision_id = "0008-delivery-destination", "0008-delivery-revision"
    config_id = "0008-delivery-configuration"
    target = {"mode": "EXISTING_TABLE", "schema_name": "migration_fixture",
              "table_name": "records", "create_schema": False}
    configuration = {
        "schema_version": 1, "dataset_version_id": legacy_ids["dataset_versions"],
        "destination_id": destination_id, "destination_version_id": revision_id,
        "target": target, "write_strategy": "UPSERT", "upsert_keys": ["id"],
        "columns": [{"source_name": "id", "target_name": "id", "target_type": "INT64",
                     "ordinal": 0, "nullable": False, "length": None,
                     "precision": None, "scale": None}],
    }
    with Session(bind=connection) as session:
        session.add(DeliveryDestination(
            **common, id=destination_id, name="Historical delivery", sink_type="POSTGRESQL",
            enabled=False, deleted=False, version=1, updated_at=now,
        ))
        session.flush()
        session.add(DeliveryDestinationVersion(
            **common, id=revision_id, destination_id=destination_id, version=1,
            config={"host": "migration-fixture.invalid", "port": 5432,
                    "database": "fixture", "username": "fixture", "options": {}},
            secret_reference="migration-fixture-not-a-real-secret", config_hash="a" * 64,
        ))
        session.add(Configuration(
            **common, id=config_id, name="Historical immutable Delivery", module="DELIVERY",
            dataset_id=legacy_ids["datasets"], owner="Migration fixture", config=configuration,
        ))
        session.flush()
        for outcome in ("COMMITTED", "UNKNOWN"):
            run_id, attempt_id = f"0008-delivery-{outcome.lower()}", f"0008-attempt-{outcome.lower()}"
            metrics = {
                "rows_attempted": 3, "rows_written": 3 if outcome == "COMMITTED" else None,
                "rows_inserted": None, "rows_updated": None,
                "bytes_sent": 42 if outcome == "COMMITTED" else None,
                "delivery_attempt_id": attempt_id, "destination_id": destination_id,
                "destination_version_id": revision_id, "attempt_number": 1,
                "sink_type": "POSTGRESQL", "write_strategy": "UPSERT", "target": target,
            }
            session.add(Run(
                **common, id=run_id, module="DELIVERY", name=f"Historical {outcome}",
                status="SUCCESS" if outcome == "COMMITTED" else "UNKNOWN", decision=outcome,
                config_id=config_id, dataset_version_id=legacy_ids["dataset_versions"],
                initiated_by="Equipo Trackvance", initiated_by_type="USER",
                initiated_by_id=legacy_ids["users"], metrics=metrics,
                execution_plan={"engine": "DATA_SINK", "lane": "DELIVERY", **configuration},
                started_at=now, finished_at=now,
                error=None if outcome == "COMMITTED" else "Historical confirmation lost",
            ))
            session.flush()
            session.add(Job(
                **common, id=f"0008-job-{outcome.lower()}", run_id=run_id, lane="DELIVERY",
                status="SUCCESS" if outcome == "COMMITTED" else "UNKNOWN", attempts=1,
                last_error=None if outcome == "COMMITTED" else "Historical confirmation lost",
            ))
            session.add(DeliveryAttempt(
                **common, id=attempt_id, run_id=run_id, destination_version_id=revision_id,
                attempt_number=1, idempotency_key=("b" if outcome == "COMMITTED" else "c") * 64,
                status=outcome, target_locator="migration_fixture.records", rows_attempted=3,
                rows_written=metrics["rows_written"], rows_inserted=None, rows_updated=None,
                bytes_sent=metrics["bytes_sent"], started_at=now, finished_at=now,
                error_code=None if outcome == "COMMITTED" else "DESTINATION_COMMIT_UNKNOWN",
                error_message=None if outcome == "COMMITTED" else "Historical confirmation lost",
            ))
            edges = [
                ("DELIVERY_INPUT", "DATASET_VERSION", legacy_ids["dataset_versions"], "RUN", run_id),
                ("DELIVERED_TO", "RUN", run_id, "DELIVERY_DESTINATION_VERSION", revision_id),
            ]
            if outcome == "COMMITTED":
                for kind, artifact_id in (("DELIVERY_RECEIPT", "0008-receipt"),
                                          ("RUN_MANIFEST", "0008-manifest")):
                    session.add(Artifact(
                        **common, id=artifact_id, kind=kind, name=f"{artifact_id}.json",
                        path=f"/migration-fixture/{artifact_id}.json", sha256="d" * 64,
                        size_bytes=42, media_type="application/json",
                    ))
                    edges.append(("RUN_OUTPUT", "RUN", run_id, "ARTIFACT", artifact_id))
                edges.extend([
                    ("DELIVERY_RECEIPT", "RUN", run_id, "ARTIFACT", "0008-receipt"),
                    ("EVIDENCE_OF", "ARTIFACT", "0008-receipt", "DELIVERY_ATTEMPT", attempt_id),
                ])
                run = session.get(Run, run_id)
                run.metrics = {**metrics, "receipt_artifact_id": "0008-receipt"}
                run.evidence_path = "/migration-fixture/0008-manifest.json"
            for number, (relation, source_type, source_id, target_type, target_id) in enumerate(edges):
                session.add(ArtifactLink(
                    **common, id=f"0008-{outcome.lower()}-edge-{number}", relation=relation,
                    source_type=source_type, source_id=source_id,
                    target_type=target_type, target_id=target_id,
                ))
        session.commit()


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
            command.upgrade(config, "0008_data_delivery")
            connection.commit()
            seed_delivery_baseline(connection, ids)
            connection.commit()
            delivery_baseline = sa.MetaData()
            delivery_baseline.reflect(bind=connection)
            baseline_rows = {
                name: [dict(row) for row in connection.execute(sa.select(table).order_by(table.c.id)).mappings()]
                for name, table in delivery_baseline.tables.items()
                if name != "alembic_version"
            }
            connection.rollback()
            command.upgrade(config, "head")
            connection.commit()
            upgraded = sa.MetaData()
            upgraded.reflect(bind=connection)
            assert set(upgraded.tables) - set(delivery_baseline.tables) == {"delivery_reviews"}
            for name, historical in baseline_rows.items():
                actual = [dict(row) for row in connection.execute(
                    sa.select(upgraded.tables[name]).order_by(upgraded.tables[name].c.id)
                ).mappings()]
                assert actual == historical, f"0008→0009 changed {name}"
            assert connection.execute(sa.select(sa.func.count()).select_from(
                upgraded.tables["delivery_reviews"]
            )).scalar_one() == 0
            for name in tables:
                row = connection.execute(sa.select(upgraded.tables[name]).where(
                    upgraded.tables[name].c.id == ids[name])).mappings().one()
                assert all(row[key] == value for key, value in before[name].items()), name
            run = connection.execute(sa.select(upgraded.tables["runs"]).where(
                upgraded.tables["runs"].c.id == ids["runs"])).mappings().one()
            audit = connection.execute(sa.select(upgraded.tables["audit_events"]).where(
                upgraded.tables["audit_events"].c.id == ids["audit_events"])).mappings().one()
            assert (run["initiated_by_type"], run["initiated_by_id"]) == ("USER", ids["users"])
            assert (audit["actor_type"], audit["actor_id"], audit["run_id"]) == ("USER", ids["users"], ids["runs"])
            assert run["initiated_by_legacy"] and audit["actor_legacy"]
            differences = compare_metadata(MigrationContext.configure(connection), Base.metadata)
            assert not differences, differences
            connection.rollback()
            command.downgrade(config, "0008_data_delivery")
            connection.commit()
            assert "delivery_reviews" not in sa.inspect(connection).get_table_names()
            for name, historical in baseline_rows.items():
                actual = [dict(row) for row in connection.execute(
                    sa.select(delivery_baseline.tables[name]).order_by(delivery_baseline.tables[name].c.id)
                ).mappings()]
                assert actual == historical, f"0009→0008 changed {name}"
            connection.rollback()
            command.upgrade(config, "head")
            connection.commit()
            connection.rollback()
            command.downgrade(config, "base")
            connection.commit()
            assert sa.inspect(connection).get_table_names() == ["alembic_version"]
            connection.rollback()
            command.upgrade(config, "head")
            connection.commit()
        return {"status": "PASS", "historical_tables_preserved": len(tables),
                "actor_backfill": "PASS", "model_parity": "PASS", "roundtrip": "PASS",
                "0008_0009_roundtrip": "PASS", "0008_tables_preserved": len(baseline_rows),
                "0008_delivery_attempts_preserved": {"COMMITTED": 1, "UNKNOWN": 1},
                "0008_delivery_lineage_edges_preserved": 8}
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
