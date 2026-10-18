"""Populated real additive migration proof, isolated from every installation."""
import os
import subprocess
import sys
from pathlib import Path


def test_populated_095_upgrade_and_roundtrip_preserve_documents_occurrences_and_jobs(tmp_path):
    environment = {**os.environ, "DATABASE_URL": f"sqlite:///{(tmp_path / 'core097-migration.db').as_posix()}",
        "TRACKVANCE_STORAGE_DIR": str(tmp_path / "storage"), "TRACKVANCE_STORAGE_ROOT": str(tmp_path / "storage"),
        "DEMO_SEED_ENABLED": "false"}
    program = '''
from pathlib import Path
import sys
from alembic import command
from alembic.config import Config
from sqlalchemy import text
from trackvance.db import engine
sys.path.insert(0, str(Path.cwd().parent / "scripts"))
from check_postgres_migrations import verify_corrections_preservation, verify_core095_preservation, verify_core097_preservation
config = Config("alembic.ini")
with engine.connect() as connection:
    config.attributes["connection"] = connection
    command.upgrade(config, "head")
    connection.commit()
    assert verify_corrections_preservation(connection, config)["status"] == "PASS"
    assert verify_core095_preservation(connection, config)["status"] == "PASS"
    result = verify_core097_preservation(connection, config)
    assert result["status"] == "PASS" and result["tables_before"] == 77 and result["tables_after"] == 80
    assert result["historical_physical_schema"]["tables"] == 77 and result["physical_schema"]["tables"] == 80
    assert result["historical_document_extraction_preserved"] and result["historical_occurrence_and_jobs_preserved"]
    assert result["new_activity_rejected"] and result["roundtrip"] == result["exact_historical_projection"] == "PASS"
    assert connection.scalar(text("SELECT version_num FROM alembic_version")) == "0027_document_builder_097"
engine.dispose()
'''
    result = subprocess.run([sys.executable, "-c", program], env=environment,
        cwd=Path(__file__).resolve().parents[1], capture_output=True, text=True, timeout=120, check=False)
    assert result.returncode == 0, result.stdout + result.stderr
