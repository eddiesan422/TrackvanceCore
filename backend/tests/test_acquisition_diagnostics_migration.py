"""The additive diagnostics migration preserves an actual populated v6 schema."""

import os
import subprocess
import sys
from pathlib import Path


def test_diagnostics_migration_preserves_all_42_tables_and_v6_fingerprint(tmp_path):
    environment = {**os.environ,
        "DATABASE_URL": f"sqlite:///{(tmp_path / 'diagnostics-migration.db').as_posix()}",
        "TRACKVANCE_STORAGE_DIR": str(tmp_path / "storage"),
        "TRACKVANCE_STORAGE_ROOT": str(tmp_path / "storage"),
        "DEMO_SEED_ENABLED": "false"}
    script = '''
from pathlib import Path
import sys
from alembic import command
from alembic.config import Config
from sqlalchemy import text
from trackvance.db import engine
sys.path.insert(0, str(Path.cwd().parent / "scripts"))
from check_postgres_migrations import verify_corrections_preservation
config = Config("alembic.ini")
with engine.connect() as connection:
    config.attributes["connection"] = connection
    command.upgrade(config, "head")
    connection.commit()
    result = verify_corrections_preservation(connection, config)
    assert result["status"] == "PASS" and result["tables"] == 42
    assert result["failed_acquisition_preserved"]
    assert result["historical_notification_read_at_preserved"]
    assert result["domain_options_numbering_limits_preserved"]
    assert result["physical_schema"]["tables"] == 56
    assert connection.scalar(text("SELECT version_num FROM alembic_version")) == "0019_governance_people"
engine.dispose()
'''
    result = subprocess.run([sys.executable, "-c", script], env=environment,
        cwd=Path(__file__).resolve().parents[1], capture_output=True,
        text=True, timeout=60, check=False)
    assert result.returncode == 0, result.stdout + result.stderr
