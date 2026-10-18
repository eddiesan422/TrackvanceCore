"""AUT-097 additive migration preserves an actual populated 0.9.5 occurrence."""
import os
import subprocess
import sys
from pathlib import Path


def test_occurrence_diagnostics_upgrade_and_roundtrip_preserve_identity(tmp_path):
    environment = {**os.environ, "DATABASE_URL": f"sqlite:///{(tmp_path / 'migration.db').as_posix()}",
        "TRACKVANCE_STORAGE_DIR": str(tmp_path / "storage"), "TRACKVANCE_STORAGE_ROOT": str(tmp_path / "storage"), "DEMO_SEED_ENABLED": "false"}
    script = '''
from alembic import command
from alembic.config import Config
from sqlalchemy import text
from sqlalchemy.orm import Session
from trackvance import identity_bootstrap
from trackvance.db import engine
from trackvance.models import Dataset, User
config = Config("alembic.ini")
command.upgrade(config, "0025_comparison_095")
with Session(engine) as db:
    db.add(User(id='actor', organization_id='org', name='Actor', email='actor@example.test', password_hash='synthetic'))
    db.add(Dataset(id='dataset', organization_id='org', name='Existing'))
    db.commit()
with engine.begin() as db:
    db.execute(text("INSERT INTO acquisition_source_configurations(id,organization_id,created_at,dataset_id,version,enabled,updated_at,next_run_at) VALUES('calendar','org','2026-10-18 00:00:00','dataset',1,1,'2026-10-18 00:00:00','2026-10-18 00:01:00')"))
    db.execute(text("INSERT INTO acquisition_source_revisions(id,organization_id,created_at,configuration_id,version,actor_id,settings) VALUES('revision','org','2026-10-18 00:00:00','calendar',1,'actor','{}')"))
    db.execute(text("INSERT INTO acquisition_occurrences(id,organization_id,created_at,configuration_id,revision_id,planned_at,status,reason_code) VALUES('occurrence','org','2026-10-18 00:00:00','calendar','revision','2026-10-18 00:01:00','BLOCKED','CONNECTION_DISABLED')"))
    before = dict(db.execute(text("SELECT * FROM acquisition_occurrences")).mappings().one())
command.upgrade(config, "0026_acquisition_097")
with engine.connect() as db:
    after = dict(db.execute(text("SELECT * FROM acquisition_occurrences")).mappings().one())
    assert {key: after[key] for key in before} == before
    assert after['coalesced_intervals'] == 0
    assert after['dispatched_at'] is after['reason_message'] is after['error_details'] is None
    assert not db.execute(text("PRAGMA foreign_key_check")).all()
command.downgrade(config, "0025_comparison_095")
with engine.connect() as db:
    assert dict(db.execute(text("SELECT * FROM acquisition_occurrences")).mappings().one()) == before
command.upgrade(config, "0026_acquisition_097")
with engine.begin() as db:
    db.execute(text("UPDATE acquisition_occurrences SET dispatched_at='2026-10-18 00:01:01'"))
try:
    command.downgrade(config, "0025_comparison_095")
except RuntimeError as error:
    assert str(error) == 'ACQUISITION_DIAGNOSTIC_HISTORY_PREVENTS_DOWNGRADE'
else:
    raise AssertionError('Downgrade erased new occurrence evidence')
with engine.begin() as db:
    db.execute(text("UPDATE acquisition_occurrences SET dispatched_at=NULL"))
    db.execute(text("UPDATE acquisition_source_configurations SET dispatch_diagnostic='{}'"))
try:
    command.downgrade(config, "0025_comparison_095")
except RuntimeError as error:
    assert str(error) == 'ACQUISITION_DIAGNOSTIC_HISTORY_PREVENTS_DOWNGRADE'
else:
    raise AssertionError('Downgrade erased configuration incident evidence')
engine.dispose()
'''
    result = subprocess.run([sys.executable, "-c", script], env=environment, cwd=Path(__file__).resolve().parents[1],
        capture_output=True, text=True, timeout=60, check=False)
    assert result.returncode == 0, result.stdout + result.stderr
