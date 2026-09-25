"""The operational-review migration is additive and actually enforces its FKs/CHECK."""

import os
import subprocess
import sys
from pathlib import Path


def test_review_migration_preserves_existing_rows_and_enforces_constraints(tmp_path):
    environment = {
        **os.environ,
        "DATABASE_URL": f"sqlite:///{(tmp_path / 'review-migration.db').as_posix()}",
        "TRACKVANCE_STORAGE_DIR": str(tmp_path / "storage"),
        "TRACKVANCE_STORAGE_ROOT": str(tmp_path / "storage"),
        "DEMO_SEED_ENABLED": "false",
    }
    script = '''
from datetime import UTC, datetime
from pathlib import Path
import sys
from alembic import command
from alembic.config import Config
from sqlalchemy import MetaData, inspect, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
from trackvance.db import engine
from trackvance.models import Dataset, DatasetVersion

sys.path.insert(0, str(Path.cwd().parent / "scripts"))
from check_postgres_migrations import seed_delivery_baseline

config = Config("alembic.ini")
command.upgrade(config, "0008_data_delivery")
before = MetaData()
before.reflect(bind=engine)
now = datetime.now(UTC)
with engine.begin() as connection:
    connection.execute(before.tables["users"].insert(), {
        "id": "reviewer", "organization_id": "historical", "created_at": now,
        "name": "Historical reviewer", "email": "reviewer@example.test",
        "password_hash": "test-only-no-password", "role": "Administrator", "active": True,
        "version": 1, "updated_at": now,
    })
    with Session(bind=connection) as session:
        session.add(Dataset(id="dataset", organization_id="historical", name="Historical source"))
        session.flush()
        session.add(DatasetVersion(
            id="version", organization_id="historical", dataset_id="dataset", version=1,
            filename="migration-fixture.csv", sha256="a" * 64, schema_hash="b" * 64,
            size_bytes=42, row_count=3, column_count=1, original_path="migration-fixture.csv",
            canonical_path="migration-fixture.parquet", schema_json=[], profile={},
        ))
        session.commit()
    seed_delivery_baseline(connection, {
        "users": "reviewer", "datasets": "dataset", "dataset_versions": "version",
    })
    snapshot = {name: connection.execute(select(table)).mappings().all()
                for name, table in before.tables.items() if name != "alembic_version"}
    assert {row["status"] for row in snapshot["delivery_attempts"]} == {"COMMITTED", "UNKNOWN"}
    assert len(snapshot["jobs"]) == 2 and len(snapshot["artifact_links"]) == 8
    assert all(row["rows_inserted"] is None for row in snapshot["delivery_attempts"])
command.upgrade(config, "0009_delivery_reviews")
schema = inspect(engine)
assert set(schema.get_table_names()) == set(before.tables) | {"delivery_reviews"}
assert {(tuple(item["constrained_columns"]), item["referred_table"])
        for item in schema.get_foreign_keys("delivery_reviews")} == {
    (("run_id",), "runs"), (("delivery_attempt_id",), "delivery_attempts"),
    (("reviewer_id",), "users"),
}
assert [item["name"] for item in schema.get_check_constraints("delivery_reviews")] == [
    "ck_delivery_review_outcome"]
after = MetaData()
after.reflect(bind=engine)
reviews = after.tables["delivery_reviews"]
with engine.connect() as connection:
    assert connection.execute(select(reviews)).all() == []
    for name, rows in snapshot.items():
        assert connection.execute(select(after.tables[name])).mappings().all() == rows
row = {"id": "review", "organization_id": "org", "created_at": now,
       "run_id": "missing-run", "delivery_attempt_id": "missing-attempt",
       "reviewer_id": "reviewer", "reviewer_name": "Historical reviewer",
       "outcome": "INCONCLUSIVE", "note": "Test", "verified_at": now}
for outcome, expected in [("INCONCLUSIVE", "FOREIGN KEY"), ("COMMITTED", "CHECK")]:
    try:
        with engine.begin() as connection:
            connection.execute(reviews.insert(), {**row, "outcome": outcome})
    except IntegrityError as error:
        assert expected in str(error.orig), str(error.orig)
    else:
        raise AssertionError("Migration failed to enforce " + expected)
command.downgrade(config, "0008_data_delivery")
assert "delivery_reviews" not in inspect(engine).get_table_names()
with engine.connect() as connection:
    for name, rows in snapshot.items():
        assert connection.execute(select(before.tables[name])).mappings().all() == rows
engine.dispose()
'''
    result = subprocess.run(
        [sys.executable, "-c", script], env=environment,
        cwd=Path(__file__).resolve().parents[1], capture_output=True,
        text=True, timeout=30, check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
