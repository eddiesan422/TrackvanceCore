"""Real populated migration roundtrip; no application installation is opened."""
import os
import subprocess
import sys
from pathlib import Path


def test_populated_090_upgrade_and_roundtrip_preserves_all_native_history(tmp_path):
    environment = {**os.environ, "DATABASE_URL": f"sqlite:///{(tmp_path / 'core095-migration.db').as_posix()}",
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
from check_postgres_migrations import verify_corrections_preservation, verify_core095_preservation
config = Config("alembic.ini")
with engine.connect() as connection:
    config.attributes["connection"] = connection
    command.upgrade(config, "head")
    connection.commit()
    assert verify_corrections_preservation(connection, config)["status"] == "PASS"
    result = verify_core095_preservation(connection, config)
    assert result["status"] == "PASS" and result["tables_before"] == 65 and result["tables_after"] == 77
    assert result["historical_physical_schema"]["tables"] == 65
    assert result["physical_schema"]["tables"] == 77
    assert result["new_activity_rejected"]
    assert result["roundtrip"] == result["exact_historical_projection"] == "PASS"
    assert connection.scalar(text("SELECT version_num FROM alembic_version")) == "0025_comparison_095"
engine.dispose()
'''
    result = subprocess.run([sys.executable, "-c", program], env=environment,
        cwd=Path(__file__).resolve().parents[1], capture_output=True, text=True, timeout=90, check=False)
    assert result.returncode == 0, result.stdout + result.stderr
