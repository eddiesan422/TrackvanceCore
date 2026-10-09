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


def seed_pre_corrections_baseline(connection) -> None:
    """Populate only an isolated 0015 migration fixture through its real tables.

    Reflection avoids inserting 0016 columns into the authentic old schema.
    This helper neither reads artifacts nor resolves its deliberately inert
    credential references. Its purpose is exact SQL preservation, not a live run.
    """
    import sqlalchemy as sa

    from trackvance.db import Base

    metadata = sa.MetaData()
    metadata.reflect(bind=connection)
    if "error_details" in metadata.tables["acquisition_runs"].c:
        raise ValueError("La fixture histórica requiere exactamente la revisión 0015.")
    for name, table in metadata.tables.items():
        if name in Base.metadata.tables:
            for column in table.columns:
                column.default = Base.metadata.tables[name].c[column.name].default
    now = datetime(2026, 9, 11, 12, 34, 56, 123456, tzinfo=UTC)
    common = {"organization_id": "corrections-fixture", "created_at": now}

    def insert(table_name, **values):
        connection.execute(metadata.tables[table_name].insert().values(**common, **values))

    insert("roles", id="c05-role", name="Historical role", normalized_name="historical role")
    connection.execute(metadata.tables["role_permissions"].insert().values(
        role_id="c05-role", permission_code="notifications:read"))
    insert("users", id="c05-user", role_id="c05-role", username="historical.corrections",
           name="Nombre histórico íntegro", email="corrections@example.test",
           password_hash="historical-unchanged-password-hash", version=7,
           password_changed_at=now, last_login_at=now)
    insert("sessions", id="c05-session", user_id="c05-user", token_hash="f" * 64,
           csrf_token="historical-csrf", authentication_method="LOCAL", expires_at=now)
    insert("datasets", id="c05-dataset", name="Historical diagnostic source",
           domain="Riesgo / área histórica", owner="Historical owner", description="No rewrite")
    insert("external_connections", id="c05-connection", name="Historical offline source",
           source_type="POSTGRESQL", enabled=False)
    insert("external_connection_versions", id="c05-source-revision", connection_id="c05-connection", version=1,
           host="migration-fixture.invalid", port=5432, database="historical", username="fixture_reader",
           options={"sslmode": "require", "application_name": "histórico"},
           secret_reference="fixture-opaque-source-reference", config_hash="a" * 64)
    for kind, identifier, media in (("ORIGINAL_UPLOAD", "c05-original", "text/csv"),
                                    ("CANONICAL_PARQUET", "c05-canonical", "application/vnd.apache.parquet")):
        insert("artifacts", id=identifier, kind=kind, name=identifier,
               path=f"migration-fixture/{identifier}", sha256="b" * 64,
               size_bytes=42, media_type=media)
    insert("dataset_versions", id="c05-version", dataset_id="c05-dataset", version=1,
           filename="historical.csv", source_type="POSTGRESQL", sha256="b" * 64, schema_hash="c" * 64,
           size_bytes=42, row_count=3, column_count=2, profile_status="READY",
           original_path="migration-fixture/c05-original", canonical_path="migration-fixture/c05-canonical",
           original_artifact_id="c05-original", canonical_artifact_id="c05-canonical",
           schema_json=[{"name": "identifier", "logical_type": "STRING"},
                        {"name": "amount", "logical_type": "DECIMAL"}],
           profile={"row_count": 3, "null_count": 1, "metric_method": "FULL_DATASET"},
           ingestion_metadata={"row_numbering": "PHYSICAL_LINE", "canonical_size_bytes": 42,
               "reader_options": {"delimiter": ";", "encoding": "utf-8", "header": True},
               "source": {"connection_id": "c05-connection", "connection_version_id": "c05-source-revision",
                          "config_hash": "a" * 64, "schema_name": "historical", "table_name": "records"}})
    insert("artifact_links", id="c05-lineage", relation="CANONICAL_OF", source_type="DATASET_VERSION",
           source_id="c05-version", target_type="ARTIFACT", target_id="c05-canonical")
    insert("acquisition_uploads", id="c05-upload", user_id="c05-user", filename="historical-failure.xlsx",
           path="migration-fixture/expired-upload", sha256="d" * 64, size_bytes=12345,
           source_format="XLSX", status="EXPIRED", expires_at=now)
    insert("acquisition_runs", id="c05-acquisition", dataset_id="c05-dataset", upload_id="c05-upload",
           source_type="UPLOAD", filename="historical-failure.xlsx", request_hash="e" * 64,
           idempotency_key="historical-acquisition-failure", initiated_by_id="c05-user",
           initiated_by_name="Nombre histórico íntegro", status="FAILED", stage="READING",
           attempt_id="historical-attempt", attempts=1, processed_rows=17, processed_bytes=12345,
           total_rows=400000, total_bytes=12345,
           source_snapshot={"upload_id": "c05-upload", "sha256": "d" * 64, "size_bytes": 12345,
                            "sheet_name": "Histórico", "row_numbering": "WORKSHEET_ROW"},
           reader_options={"sheet_name": "Histórico", "header_row": 2, "identifier_columns": ["identifier"]},
           column_overrides={"identifier": {"logical_type": "STRING", "semantic_tag": "IDENTIFIER"}},
           effective_limits={"max_upload_bytes": 1073741824, "max_rows": 5000000,
                             "batch_rows": 1000, "batch_bytes": 8388608},
           error_code="HISTORICAL_XLSX_LIMIT", error_message="Fallo histórico conservado sin nueva interpretación.",
           started_at=now, finished_at=now)
    insert("jobs", id="c05-job", run_id=None, acquisition_id="c05-acquisition", lane="ACQUISITION",
           status="FAILED", attempts=1, last_error="HISTORICAL_XLSX_LIMIT")
    insert("outbox_events", id="c05-event", dedupe_key="historical:c05-acquisition", event_type="ACQUISITION_COMPLETED",
           aggregate_type="ACQUISITION", aggregate_id="c05-acquisition", module="ACQUISITION",
           payload={"recipient_user_id": "c05-user", "status": "FAILED", "error_code": "HISTORICAL_XLSX_LIMIT",
                    "error_message": "Fallo histórico conservado sin nueva interpretación."})
    insert("event_consumptions", id="c05-consumption", event_id="c05-event", consumer="NOTIFICATIONS",
           status="DONE", attempts=1, available_at=now, completed_at=now)
    insert("internal_notifications", id="c05-notification", event_id="c05-event", recipient_user_id="c05-user",
           module="ACQUISITION", origin="MANUAL", status="FAILED", description="Aviso histórico leído",
           resource_type="ACQUISITION", resource_id="c05-acquisition", detail_url="/datasets/acquisitions/c05-acquisition",
           read_at=now)
    insert("notification_deliveries", id="c05-email", event_type="TEMPORARY_PASSWORD_ISSUED",
           template_key="temporary_password", recipient_user_id="c05-user", recipient_email_snapshot="corrections@example.test",
           status="SENT", sent_at=now)


def corrections_rows(connection):
    """Return every application column/row and FK, including empty tables."""
    import sqlalchemy as sa

    metadata = sa.MetaData()
    metadata.reflect(bind=connection)
    rows = {name: [dict(row) for row in connection.execute(sa.select(table)).mappings()]
            for name, table in metadata.tables.items() if name != "alembic_version"}
    foreign_keys = [(name, fk.parent.name, fk.column.table.name, fk.column.name)
                    for name, table in metadata.tables.items() if name != "alembic_version"
                    for fk in table.foreign_keys]
    return rows, foreign_keys


def verify_corrections_preservation(connection, config) -> dict:
    """Exercise 0015→head→0015→head only in the checker's disposable DB."""
    import sqlalchemy as sa
    import verify_storage
    from alembic import command
    from physical_schema_guard import validate_physical_schema

    from trackvance.db import Base

    connection.rollback()
    command.downgrade(config, verify_storage.PRE_CORRECTIONS_MIGRATION)
    connection.commit()
    seed_pre_corrections_baseline(connection)
    connection.commit()
    before, before_fks = corrections_rows(connection)
    assert len(before) == 42
    before_hashes = verify_storage._table_hashes(before)
    relationships = verify_storage.validate_relationships(before, before_fks)
    connection.rollback()
    command.upgrade(config, verify_storage.CURRENT_MIGRATION)
    connection.commit()
    after, after_fks = corrections_rows(connection)
    report = verify_storage.legacy_v6_report(after, after_fks,
        current_migration=verify_storage.CURRENT_MIGRATION,
        verified_artifacts=len(before["artifacts"]),
        verified_source_secrets=len(before["external_connection_versions"]),
        verified_delivery_secrets=len(before["delivery_destination_versions"]))
    assert report["tables"] == before_hashes
    assert report["validated_relationships"] == relationships
    assert all(row["error_details"] is None and row["error_reference"] is None
               for row in after["acquisition_runs"])
    assert {column["name"] for column in sa.inspect(connection).get_columns("acquisition_runs")
            if column["name"] not in before["acquisition_runs"][0]} == {"error_details", "error_reference"}
    physical = validate_physical_schema(connection, Base.metadata)
    connection.rollback()
    command.downgrade(config, verify_storage.PRE_CORRECTIONS_MIGRATION)
    connection.commit()
    restored, restored_fks = corrections_rows(connection)
    assert verify_storage._table_hashes(restored) == before_hashes
    assert verify_storage.validate_relationships(restored, restored_fks) == relationships
    connection.rollback()
    command.upgrade(config, verify_storage.CURRENT_MIGRATION)
    connection.commit()
    final, final_fks = corrections_rows(connection)
    corrected, corrected_fks = verify_storage.project_catalog_upgrade(final, final_fks)
    projected, projected_fks = verify_storage.project_corrections_upgrade(corrected, corrected_fks)
    assert verify_storage._table_hashes(projected) == before_hashes
    assert verify_storage.validate_relationships(projected, projected_fks) == relationships
    return {"status": "PASS", "tables": 42, "rows": sum(map(len, before.values())),
            "relationships": relationships, "physical_schema": physical,
            "failed_acquisition_preserved": True, "historical_notification_read_at_preserved": True,
            "domain_options_numbering_limits_preserved": True}


def verify_catalog_preservation(connection, config) -> dict:
    """Prove 0016 preservation, including non-NULL correction diagnostics."""
    import sqlalchemy as sa
    import verify_storage
    from alembic import command
    from physical_schema_guard import validate_physical_schema

    from trackvance.db import Base

    connection.rollback()
    command.downgrade(config, verify_storage.CORRECTIONS_MIGRATION)
    connection.commit()
    metadata = sa.MetaData()
    metadata.reflect(bind=connection)
    acquisition = metadata.tables["acquisition_runs"]
    connection.execute(acquisition.update().values(
        error_details={"limit": 1000000, "observed": 1000001}, error_reference="historical-070-diagnostic"))
    connection.commit()
    before, before_fks = corrections_rows(connection)
    before_hashes = verify_storage._table_hashes(before)
    connection.rollback()
    command.upgrade(config, "head")
    connection.commit()
    after, after_fks = corrections_rows(connection)
    report = verify_storage.legacy_v7_report(
        after, after_fks, current_migration=verify_storage.CURRENT_MIGRATION,
        verified_artifacts=len(before["artifacts"]),
        verified_source_secrets=len(before["external_connection_versions"]),
        verified_delivery_secrets=len(before["delivery_destination_versions"]))
    assert report["tables"] == before_hashes
    assert report["validated_relationships"] == verify_storage.validate_relationships(before, before_fks)
    assert all(not after[table] for table in verify_storage.CATALOG_TABLES)
    physical = validate_physical_schema(connection, Base.metadata)
    connection.rollback()
    command.downgrade(config, verify_storage.CORRECTIONS_MIGRATION)
    connection.commit()
    restored, _ = corrections_rows(connection)
    assert verify_storage._table_hashes(restored) == before_hashes
    connection.rollback()
    command.upgrade(config, "head")
    connection.commit()
    return {"status": "PASS", "tables_before": len(before), "tables_after": len(after),
            "legacy_area_preserved": True, "diagnostics_preserved": True,
            "custom_roles_unchanged": True, "no_automatic_classification": True,
            "physical_schema": physical}


def verify_people_preservation(connection, config) -> dict:
    """Prove populated authentic 0017→0019→0017 on PostgreSQL, with frozen history."""
    import sqlalchemy as sa
    import verify_storage
    from alembic import command
    from physical_schema_guard import validate_physical_schema

    from trackvance.db import Base

    connection.rollback()
    command.downgrade(config, verify_storage.CATALOG_MIGRATION)
    connection.commit()
    metadata = sa.MetaData()
    metadata.reflect(bind=connection)
    for name, table in metadata.tables.items():
        if name in Base.metadata.tables:
            for column in table.columns:
                column.default = Base.metadata.tables[name].c[column.name].default
    datasets, history = metadata.tables["datasets"], metadata.tables["governance_history"]
    connection.execute(datasets.update().where(datasets.c.id == "c05-dataset").values(
        business_owner_id="c05-user", steward_id="c05-user", owner="Texto sin identidad verificable"))
    snapshot = {"schema_version": 1, "business_owner_id": "c05-user", "name": "Gobierno histórico íntegro"}
    connection.execute(history.insert().values(id="c08-governance", organization_id="corrections-fixture",
        dataset_id="c05-dataset", version=1, actor_id="c05-user", snapshot=snapshot))
    connection.execute(metadata.tables["role_permissions"].insert().values(role_id="c05-role", permission_code="datasets:read"))
    connection.commit()
    before, _before_fks = corrections_rows(connection)
    assert len(before) == 55 and before["datasets"] and before["users"] and before["governance_history"]
    before_hashes = verify_storage._table_hashes(before)
    connection.rollback()
    command.upgrade(config, "head")
    connection.commit()
    after, after_fks = corrections_rows(connection)
    projection = verify_storage.legacy_v8_report(after, after_fks, current_migration=verify_storage.CURRENT_MIGRATION,
        verified_artifacts=len(before["artifacts"]), verified_source_secrets=len(before["external_connection_versions"]),
        verified_delivery_secrets=len(before["delivery_destination_versions"]))
    assert projection["tables"] == before_hashes
    assert len(after["governance_people"]) == 1
    person = after["governance_people"][0]
    dataset = next(row for row in after["datasets"] if row["id"] == "c05-dataset")
    assert dataset["business_owner_person_id"] == dataset["steward_person_id"] == person["id"]
    assert dataset["technical_custodian_person_id"] is None and person["user_id"] == "c05-user"
    assert after["governance_history"] == before["governance_history"]
    physical = validate_physical_schema(connection, Base.metadata)
    connection.rollback()
    command.downgrade(config, verify_storage.CATALOG_MIGRATION)
    connection.commit()
    restored, _ = corrections_rows(connection)
    assert verify_storage._table_hashes(restored) == before_hashes
    connection.rollback()
    command.upgrade(config, "head")
    connection.commit()
    return {"status": "PASS", "source_version": "0.8.0", "target_version": "0.8.5",
        "tables_before": 55, "tables_after": 56, "exact_historical_projection": "PASS", "roundtrip": "PASS",
        "explicit_user_links_reused": True, "frozen_governance_history_preserved": True, "physical_schema": physical}


def seed_delivery_baseline(connection, legacy_ids: dict[str, str]) -> None:
    """Persist representative 0008 rows, not remote writes or credential files."""
    from sqlalchemy import MetaData
    from sqlalchemy.orm import Session, registry

    from trackvance import models

    # A migration fixture must use the actual old schema, not INSERT new ORM
    # columns into 0008. Retain only defaults for columns present at that revision.
    metadata = MetaData()
    metadata.reflect(bind=connection)
    mapper = registry()
    mapped = []
    for name in ("Artifact", "ArtifactLink", "Configuration", "DeliveryAttempt",
                 "DeliveryDestination", "DeliveryDestinationVersion", "Job", "Run", "MonitorSchedule", "MonitorScheduleVersion"):
        current = getattr(models, name)
        table = metadata.tables[current.__tablename__]
        for column in table.columns:
            column.default = current.__table__.c[column.name].default
        historical = type("Historical" + name, (), {})
        mapper.map_imperatively(historical, table)
        mapped.append(historical)
    (Artifact, ArtifactLink, Configuration, DeliveryAttempt, DeliveryDestination,
     DeliveryDestinationVersion, Job, Run, MonitorSchedule, MonitorScheduleVersion) = mapped

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
        # Historical actor_id is NOT NULL but has no User FK: an old SYSTEM
        # marker is intentionally not a provable executor.
        for label, actor_id in (("unresolved", "SYSTEM"), ("verified", legacy_ids["users"])):
            monitor_id, schedule_id = f"0008-monitor-{label}", f"0008-schedule-{label}"
            session.add(Configuration(**common, id=monitor_id, name=f"Historical monitor {label}",
                module="sentinel", dataset_id=legacy_ids["datasets"], owner="Migration fixture", config={}))
            session.flush()
            session.add(MonitorSchedule(**common, id=schedule_id, monitor_id=monitor_id,
                version=1, enabled=True, next_run_at=now, updated_at=now))
            session.flush()
            session.add(MonitorScheduleVersion(**common, id=f"0008-schedule-revision-{label}",
                schedule_id=schedule_id, version=1, interval_seconds=3600, enabled=True,
                starts_at=now, actor_id=actor_id))
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
                    values.update(name="Equipo Trackvance", email="migration@trackvance.local",
                                  role="Administrator")
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
            command.upgrade(config, "0012_delivery_target_audit")
            connection.commit()
            identity_baseline = sa.MetaData()
            identity_baseline.reflect(bind=connection)
            identity_rows = {name: [dict(row) for row in connection.execute(sa.select(table).order_by(*table.primary_key.columns)).mappings()]
                             for name, table in identity_baseline.tables.items() if name != "alembic_version"}
            connection.rollback()
            command.upgrade(config, "head")
            connection.commit()
            upgraded = sa.MetaData()
            upgraded.reflect(bind=connection)
            assert set(upgraded.tables) - set(delivery_baseline.tables) == {
                "delivery_reviews", "roles", "role_permissions", "external_identities",
                "oidc_login_attempts", "notification_deliveries", "delivery_target_policies",
                "acquisition_uploads", "acquisition_runs", "delivery_automations", "delivery_automation_versions",
                "delivery_occurrences", "delivery_input_claims", "delivery_target_guards", "delivery_target_decisions",
                "outbox_events", "event_consumptions", "internal_notifications",
                "macro_domains", "data_domains", "governance_history", "glossary_terms",
                "column_documentation", "glossary_associations", "dataset_blocks",
                "dataset_security_dependencies", "strict_approvals", "report_definitions",
                "report_revisions", "report_contexts", "report_executions",
                "governance_people",
            }
            for name, historical in identity_rows.items():
                actual = [dict(row) for row in connection.execute(
                    sa.select(*(upgraded.tables[name].c[column.name] for column in identity_baseline.tables[name].columns))
                    .order_by(*identity_baseline.tables[name].primary_key.columns)).mappings()]
                if name == "monitor_schedules":
                    for row in actual:
                        if row["id"] == "0008-schedule-unresolved":
                            assert row["enabled"] is False
                            row["enabled"] = True  # Only the documented legacy safety pause is projected.
                if name == "role_permissions":
                    actual = [row for row in actual if row["permission_code"] != "people:read"]
                assert actual == historical, f"0012→0016 changed {name}"
            schedule_table, revision_table = upgraded.tables["monitor_schedules"], upgraded.tables["monitor_schedule_versions"]
            unresolved = connection.execute(sa.select(schedule_table).where(schedule_table.c.id == "0008-schedule-unresolved")).mappings().one()
            assert unresolved["legacy_enabled_before_identity"] is True and unresolved["enabled"] is False
            verified_revision = connection.execute(sa.select(revision_table).where(revision_table.c.id == "0008-schedule-revision-verified")).mappings().one()
            legacy_revision = connection.execute(sa.select(revision_table).where(revision_table.c.id == "0008-schedule-revision-unresolved")).mappings().one()
            assert verified_revision["responsible_user_id"] == ids["users"] and legacy_revision["responsible_user_id"] is None
            for name, historical in baseline_rows.items():
                actual = [dict(row) for row in connection.execute(
                    sa.select(*(upgraded.tables[name].c[column.name]
                                for column in delivery_baseline.tables[name].columns))
                    .order_by(upgraded.tables[name].c.id)
                ).mappings()]
                if name == "monitor_schedules":
                    for row in actual:
                        if row["id"] == "0008-schedule-unresolved":
                            assert row["enabled"] is False
                            row["enabled"] = True
                assert actual == historical, f"0008→0016 changed {name}"
            migrated_user = connection.execute(sa.select(upgraded.tables["users"]).where(
                upgraded.tables["users"].c.id == ids["users"])).mappings().one()
            assert migrated_user["username"] and migrated_user["role_id"]
            assert migrated_user["first_name"] is None and migrated_user["last_name"] is None
            assert not migrated_user["deleted"] and not migrated_user["must_change_password"]
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
                assert actual == historical, f"0016→0008 changed {name}"
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
            corrections = verify_corrections_preservation(connection, config)
            catalog = verify_catalog_preservation(connection, config)
            people = verify_people_preservation(connection, config)
        return {"status": "PASS", "historical_tables_preserved": len(tables),
                "actor_backfill": "PASS", "model_parity": "PASS", "roundtrip": "PASS",
                "0008_0016_roundtrip": "PASS", "0012_0016_preservation": "PASS",
                "sentinel_verified_executor": "PASS", "sentinel_legacy_safety_pause": "PASS",
                "0012_tables_preserved": len(identity_rows), "0008_tables_preserved": len(baseline_rows),
                "0008_delivery_attempts_preserved": {"COMMITTED": 1, "UNKNOWN": 1},
                "0008_delivery_lineage_edges_preserved": 8,
                "0015_0017_preservation": corrections,
                "0016_0017_preservation": catalog, "0017_0019_preservation": people}
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
