"""Startup migrations must preserve already imported product diagnostics."""
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest


@pytest.mark.parametrize("startup", ["migrate", "lifespan"])
def test_real_startup_migration_preserves_report_channel_logging(tmp_path, startup):
    environment = {
        **os.environ,
        "DATABASE_URL": f"sqlite:///{(tmp_path / 'migration.db').as_posix()}",
        "TRACKVANCE_STORAGE_DIR": str(tmp_path / "storage"),
        "TRACKVANCE_STORAGE_ROOT": str(tmp_path / "storage"),
        "TRACKVANCE_BACKEND_DIR": str(Path(__file__).resolve().parents[1]),
        "DEMO_ACCESS_ENABLED": "false",
        "DEMO_SEED_ENABLED": "false",
    }
    script = '''
import json
from trackvance import report_executor
from trackvance.db import engine
from trackvance.migrate import migrate, migration_ready

before = report_executor.logger.disabled
if __import__("sys").argv[1] == "lifespan":
    from fastapi.testclient import TestClient
    from trackvance.api import app
    with TestClient(app):
        assert migration_ready()
else:
    migrate()
    migrate()  # An already applied head must preserve logging too.
assert migration_ready()
report_executor._log_channel_diagnostic(report_executor._channel_diagnostic(
    ValueError("SYNTHETIC_PRIVATE_EXCEPTION_DO_NOT_LOG"), None, "DOWNLOAD", 0,
    stop_requested=False))
print(json.dumps({"disabled_before": before, "disabled_after": report_executor.logger.disabled,
                  "migration_ready": migration_ready()}))
engine.dispose()
'''
    result = subprocess.run([sys.executable, "-c", script, startup], env=environment,
                            cwd=Path(__file__).resolve().parents[1], capture_output=True,
                            text=True, timeout=60, check=False)
    assert result.returncode == 0, result.stdout + result.stderr
    assert json.loads(result.stdout.strip().splitlines()[-1]) == {
        "disabled_before": False, "disabled_after": False, "migration_ready": True}
    assert "REPORT_CHANNEL_DIAGNOSTIC" in result.stderr
    assert "SYNTHETIC_PRIVATE_EXCEPTION_DO_NOT_LOG" not in result.stdout + result.stderr
