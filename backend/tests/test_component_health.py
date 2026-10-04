"""Health probes preserve freshness while avoiding engine/database imports."""

import json
import os
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from trackvance import component_health

NOW = datetime(2026, 10, 3, tzinfo=UTC)


@pytest.mark.parametrize("age,status", [(0, "RUNNING"), (29.999, "RUNNING"),
                                      (30, "OFFLINE"), (-0.001, "OFFLINE")])
def test_exact_freshness_contract(tmp_path, monkeypatch, age, status):
    monkeypatch.setattr(component_health, "STORAGE_DIR", tmp_path)
    (tmp_path / "scheduler-heartbeat.json").write_text(
        json.dumps({"updated_at": (NOW - timedelta(seconds=age)).isoformat()}), encoding="utf-8")
    assert component_health.component_status("scheduler", now=NOW) == status


@pytest.mark.parametrize("data", ["not-json", "null", "{}", '{"updated_at":null}',
                                 '{"updated_at":"2026-10-03T00:00:00"}'])
def test_invalid_heartbeat_is_offline(tmp_path, monkeypatch, data):
    monkeypatch.setattr(component_health, "STORAGE_DIR", tmp_path)
    (tmp_path / "events-chaining-heartbeat.json").write_text(data, encoding="utf-8")
    assert component_health.component_status("events-chaining", now=NOW) == "OFFLINE"


def test_missing_file_and_unknown_component(tmp_path, monkeypatch):
    monkeypatch.setattr(component_health, "STORAGE_DIR", tmp_path)
    assert component_health.component_status("events-notifications", now=NOW) == "OFFLINE"
    with pytest.raises(ValueError, match="Componente desconocido"):
        component_health.component_status("../scheduler")


def test_symlink_cannot_read_a_heartbeat_outside_storage(tmp_path, monkeypatch):
    root = tmp_path / 'storage'
    root.mkdir()
    outside = tmp_path / 'outside.json'
    outside.write_text(json.dumps({'updated_at': NOW.isoformat()}), encoding='utf-8')
    try:
        (root / 'scheduler-heartbeat.json').symlink_to(outside)
    except OSError:
        pytest.skip('The host does not grant symlink creation.')
    monkeypatch.setattr(component_health, 'STORAGE_DIR', root)
    assert component_health.component_status('scheduler', now=NOW) == 'OFFLINE'


def test_probe_has_no_engine_imports_or_database_side_effects(tmp_path):
    source = Path(component_health.__file__).resolve().parents[1]
    environment = {**os.environ, "PYTHONPATH": str(source), "TRACKVANCE_STORAGE_DIR": str(tmp_path)}
    program = ("import sys; from trackvance.component_health import component_status; "
               "assert component_status('scheduler') == 'OFFLINE'; "
               "assert not any(name.split('.')[0] in {'sqlalchemy','polars','duckdb','pyarrow','pyspark'} "
               "or name in {'trackvance.db','trackvance.models','trackvance.dispatcher'} for name in sys.modules)")
    result = subprocess.run([sys.executable, "-c", program], env=environment,
                            capture_output=True, text=True, check=False)
    assert result.returncode == 0, result.stderr
    assert not list(tmp_path.iterdir())
