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
