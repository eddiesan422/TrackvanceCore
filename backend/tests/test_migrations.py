import os
import subprocess
import sys
from pathlib import Path


def test_initial_migration_roundtrip_and_model_parity(tmp_path):
    """Run migration imports in a fresh process with a separate disposable SQLite file."""
    environment = {
        **os.environ,
        "DATABASE_URL": f"sqlite:///{(tmp_path / 'migration.db').as_posix()}",
        "TRACKVANCE_STORAGE_DIR": str(tmp_path / "storage"),
        "TRACKVANCE_STORAGE_ROOT": str(tmp_path / "storage"),
        "DEMO_SEED_ENABLED": "false",
    }
    script = """
from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.config import Config
from alembic.migration import MigrationContext
from sqlalchemy import inspect
from trackvance import models
from trackvance.db import Base, engine

config = Config("alembic.ini")
command.upgrade(config, "head")
assert set(inspect(engine).get_table_names()) == set(Base.metadata.tables) | {"alembic_version"}
with engine.connect() as connection:
    differences = compare_metadata(MigrationContext.configure(connection), Base.metadata)
    assert differences == [], differences
command.downgrade(config, "base")
assert set(inspect(engine).get_table_names()) == {"alembic_version"}
command.upgrade(config, "head")
assert "runs" in inspect(engine).get_table_names()
engine.dispose()
"""
    result = subprocess.run([sys.executable, "-c", script], env=environment,
                            cwd=Path(__file__).resolve().parents[1], capture_output=True,
                            text=True, timeout=30, check=False)
    assert result.returncode == 0, result.stdout + result.stderr


def test_data_delivery_migration_preserves_jobs_and_backfills_default_lane(tmp_path):
    environment = {
        **os.environ,
        "DATABASE_URL": f"sqlite:///{(tmp_path / 'delivery-migration.db').as_posix()}",
        "TRACKVANCE_STORAGE_DIR": str(tmp_path / "storage"),
        "TRACKVANCE_STORAGE_ROOT": str(tmp_path / "storage"),
        "DEMO_SEED_ENABLED": "false",
    }
    script = r'''
from datetime import UTC, datetime

from alembic import command
from alembic.config import Config
from sqlalchemy import MetaData, Table, inspect, select
from trackvance.db import engine

config = Config("alembic.ini")
command.upgrade(config, "0007_monitor_scheduling")
metadata = MetaData()
metadata.reflect(bind=engine)
now = datetime.now(UTC)
common = {"organization_id": "org", "created_at": now}
with engine.begin() as connection:
    connection.execute(metadata.tables["datasets"].insert(), {
        **common, "id": "dataset", "name": "Historical delivery input", "description": "",
        "domain": "Ops", "owner": "Owner", "criticality": "HIGH", "status": "ACTIVE",
    })
    connection.execute(metadata.tables["configurations"].insert(), {
        **common, "id": "configuration", "name": "Historical configuration", "module": "intake",
        "version": 1, "dataset_id": "dataset", "target_dataset_id": None, "owner": "Owner",
        "description": "", "status": "PUBLISHED", "config": {}, "previous_version_id": None,
    })
    connection.execute(metadata.tables["dataset_versions"].insert(), {
        **common, "id": "dataset-version", "dataset_id": "dataset", "version": 1,
        "filename": "historical.csv", "source_type": "UPLOAD", "sha256": "a" * 64,
        "schema_hash": "b" * 64, "size_bytes": 10, "row_count": 1, "column_count": 1,
        "profile_status": "READY", "original_path": "historical.csv",
        "canonical_path": "historical.parquet", "schema_json": [], "profile": {},
        "parent_version_id": None, "original_artifact_id": None, "canonical_artifact_id": None,
        "source_run_id": None, "ingestion_metadata": {},
    })
    connection.execute(metadata.tables["runs"].insert(), {
        **common, "id": "run", "module": "intake", "name": "Historical configuration",
        "status": "QUEUED", "decision": None, "config_id": "configuration",
        "dataset_version_id": "dataset-version", "target_version_id": None,
        "initiated_by": "Legacy user", "initiated_by_type": "SYSTEM",
        "initiated_by_id": "system:legacy", "initiated_by_legacy": True,
        "progress_stage": "En cola", "progress_percent": 0, "metrics": {},
        "execution_plan": {}, "result_path": None, "evidence_path": None,
        "output_version_id": None, "error": None, "cancel_requested": False,
        "started_at": None, "finished_at": None,
    })
    connection.execute(metadata.tables["jobs"].insert(), {
        **common, "id": "job", "run_id": "run", "status": "QUEUED", "attempts": 0,
        "lease_owner": None, "lease_until": None, "last_error": None,
    })

command.upgrade(config, "0008_data_delivery")
inspector = inspect(engine)
assert {
    "delivery_destinations", "delivery_destination_versions", "delivery_attempts"
} <= set(inspector.get_table_names())
assert "lane" in {column["name"] for column in inspector.get_columns("jobs")}
assert {index["name"] for index in inspector.get_indexes("jobs")} >= {
    "ix_jobs_lane", "ix_jobs_lane_status_created_at"
}
assert any(
    constraint["column_names"] == ["destination_id", "version"]
    for constraint in inspector.get_unique_constraints("delivery_destination_versions")
)
assert any(
    constraint["column_names"] == ["run_id", "attempt_number"]
    for constraint in inspector.get_unique_constraints("delivery_attempts")
)
upgraded = MetaData()
upgraded.reflect(bind=engine)
with engine.connect() as connection:
    job = connection.execute(
        select(upgraded.tables["jobs"]).where(upgraded.tables["jobs"].c.id == "job")
    ).mappings().one()
    assert job["lane"] == "DEFAULT"
    assert job["run_id"] == "run" and job["status"] == "QUEUED" and job["attempts"] == 0

command.downgrade(config, "0007_monitor_scheduling")
inspector = inspect(engine)
assert not {
    "delivery_destinations", "delivery_destination_versions", "delivery_attempts"
} & set(inspector.get_table_names())
assert "lane" not in {column["name"] for column in inspector.get_columns("jobs")}
downgraded = MetaData()
downgraded.reflect(bind=engine)
with engine.connect() as connection:
    job = connection.execute(
        select(downgraded.tables["jobs"]).where(downgraded.tables["jobs"].c.id == "job")
    ).mappings().one()
    assert job["run_id"] == "run" and job["status"] == "QUEUED" and job["attempts"] == 0
engine.dispose()
'''
    result = subprocess.run(
        [sys.executable, "-c", script],
        env=environment,
        cwd=Path(__file__).resolve().parents[1],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_exception_migration_backfills_configuration_without_rewriting_history(tmp_path):
    environment = {
        **os.environ,
        "DATABASE_URL": f"sqlite:///{(tmp_path / 'historical.db').as_posix()}",
        "TRACKVANCE_STORAGE_DIR": str(tmp_path / "storage"),
        "TRACKVANCE_STORAGE_ROOT": str(tmp_path / "storage"),
        "DEMO_SEED_ENABLED": "false",
    }
    script = r'''
from datetime import UTC, datetime

from alembic import command
from alembic.config import Config
from sqlalchemy import MetaData, Table, select
from trackvance.db import engine

config = Config("alembic.ini")
command.upgrade(config, "0003_dataset_ingestion_metadata")
metadata = MetaData()
now = datetime.now(UTC)
with engine.begin() as connection:
    datasets = Table("datasets", metadata, autoload_with=connection)
    configurations = Table("configurations", metadata, autoload_with=connection)
    versions = Table("dataset_versions", metadata, autoload_with=connection)
    runs = Table("runs", metadata, autoload_with=connection)
    findings = Table("findings", metadata, autoload_with=connection)
    exceptions = Table("exceptions", metadata, autoload_with=connection)
    common = {"organization_id": "org", "created_at": now}
    connection.execute(datasets.insert(), {
        **common, "id": "dataset", "name": "Historical", "description": "", "domain": "Ops",
        "owner": "Owner", "criticality": "HIGH", "status": "ACTIVE",
    })
    connection.execute(configurations.insert(), {
        **common, "id": "config", "name": "Historical control", "module": "intake", "version": 1,
        "dataset_id": "dataset", "target_dataset_id": None, "owner": "Owner", "description": "",
        "status": "PUBLISHED", "config": {}, "previous_version_id": None,
    })
    connection.execute(versions.insert(), {
        **common, "id": "version", "dataset_id": "dataset", "version": 1, "filename": "old.csv",
        "source_type": "UPLOAD", "sha256": "a" * 64, "schema_hash": "b" * 64, "size_bytes": 1,
        "row_count": 1, "column_count": 1, "profile_status": "READY", "original_path": "old.csv",
        "canonical_path": "old.parquet", "schema_json": [], "profile": {}, "parent_version_id": None,
        "original_artifact_id": None, "canonical_artifact_id": None, "source_run_id": None,
        "ingestion_metadata": {},
    })
    connection.execute(runs.insert(), {
        **common, "id": "run", "module": "intake", "name": "Historical control", "status": "SUCCESS",
        "decision": "REJECTED", "config_id": "config", "dataset_version_id": "version",
        "target_version_id": None, "initiated_by": "Legacy user", "initiated_by_type": "SYSTEM",
        "initiated_by_id": "system:legacy", "initiated_by_legacy": True, "progress_stage": "Completado",
        "progress_percent": 100, "metrics": {}, "execution_plan": {}, "result_path": None,
        "evidence_path": None, "output_version_id": None, "error": None, "cancel_requested": False,
        "started_at": now, "finished_at": now,
    })
    connection.execute(findings.insert(), {
        **common, "id": "finding", "run_id": "run", "title": "Historical finding", "code": "REQUIRED:id",
        "severity": "HIGH", "fingerprint": "legacy-fingerprint", "details": {},
    })
    connection.execute(exceptions.insert(), {
        **common, "id": "case", "display_id": "EXC-LEGACY", "finding_id": "finding", "run_id": "run",
        "title": "Historical finding", "module": "intake", "severity": "HIGH", "state": "RESOLVED",
        "owner": "Owner", "root_cause": "Legacy cause", "resolution": "Legacy resolution", "version": 3,
        "updated_at": now, "events": [{"to_state": "RESOLVED"}],
    })
command.upgrade(config, "head")
metadata = MetaData()
with engine.connect() as connection:
    exceptions = Table("exceptions", metadata, autoload_with=connection)
    row = connection.execute(select(exceptions).where(exceptions.c.id == "case")).mappings().one()
    assert row["configuration_id"] == "config"
    assert row["state"] == "RESOLVED"
    assert row["version"] == 3
    assert row["validation_run_id"] is None
    assert row["validation_evidence"] == {}
    assert row["administrative_reason"] == ""
    assert row["priority"] == "HIGH"
    assert row["assigned_user_id"] is None
    assert row["auto_resolve_enabled"] is False
    assert row["due_at"] is None and row["reopened_at"] is None
    assert row["events"] == [{"to_state": "RESOLVED"}]
engine.dispose()
'''
    result = subprocess.run(
        [sys.executable, "-c", script],
        env=environment,
        cwd=Path(__file__).resolve().parents[1],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
