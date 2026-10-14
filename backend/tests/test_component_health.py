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


@pytest.mark.parametrize("lane", sorted(component_health.WORKER_LANES))
@pytest.mark.parametrize("age,status", [(0, "RUNNING"), (29.999, "RUNNING"), (30, "OFFLINE")])
def test_worker_probe_preserves_exact_file_and_freshness_contract(tmp_path, monkeypatch, lane, age, status):
    monkeypatch.setattr(component_health, "STORAGE_DIR", tmp_path)
    last_seen = (NOW - timedelta(seconds=age)).isoformat()
    (tmp_path / f"worker-heartbeat-{lane.lower()}.json").write_text(
        json.dumps({"last_seen": last_seen, "lane": lane, "worker_id": "test-worker"}), encoding="utf-8")
    assert component_health.worker_status(lane, now=NOW) == {"status": status, "last_seen": last_seen, "lane": lane}


@pytest.mark.parametrize("data", ["not-json", "null", "{}", '{"last_seen":null}',
                                 '{"last_seen":"2026-10-03T00:00:00"}'])
def test_invalid_worker_heartbeat_fails_closed(tmp_path, monkeypatch, data):
    monkeypatch.setattr(component_health, "STORAGE_DIR", tmp_path)
    (tmp_path / "worker-heartbeat-delivery.json").write_text(data, encoding="utf-8")
    assert component_health.worker_status("DELIVERY", now=NOW) == {"status": "OFFLINE", "last_seen": None, "lane": "DELIVERY"}


def test_worker_lane_files_are_independent_and_unknown_lane_rejected(tmp_path, monkeypatch):
    monkeypatch.setattr(component_health, "STORAGE_DIR", tmp_path)
    (tmp_path / "worker-heartbeat-default.json").write_text(json.dumps({"last_seen": NOW.isoformat()}), encoding="utf-8")
    assert component_health.worker_status(" default ", now=NOW)["status"] == "RUNNING"
    for lane in ("DELIVERY", "ACQUISITION", "REPORT"):
        assert component_health.worker_status(lane, now=NOW)["status"] == "OFFLINE"
    with pytest.raises(RuntimeError, match="TRACKVANCE_WORKER_LANE"):
        component_health.worker_status("../default")


def test_worker_public_wrapper_uses_identical_heartbeat_contract(tmp_path, monkeypatch):
    from trackvance import worker

    monkeypatch.setattr(component_health, "STORAGE_DIR", tmp_path)
    monkeypatch.setattr(worker.artifact_store, "root", tmp_path)
    for lane in component_health.WORKER_LANES:
        for age in (0, 31):
            last_seen = (datetime.now(UTC) - timedelta(seconds=age)).isoformat()
            (tmp_path / f"worker-heartbeat-{lane.lower()}.json").write_text(json.dumps({"last_seen": last_seen, "lane": lane}), encoding="utf-8")
            assert worker.worker_status(lane) == component_health.worker_status(lane)


def test_worker_probe_has_no_worker_engine_imports_or_database_side_effects(tmp_path):
    source = Path(component_health.__file__).resolve().parents[1]
    environment = {**os.environ, "PYTHONPATH": str(source), "TRACKVANCE_STORAGE_DIR": str(tmp_path)}
    program = ("import sys; from trackvance.component_health import worker_status; "
               "assert all(worker_status(lane)['status'] == 'OFFLINE' for lane in ('DEFAULT','DELIVERY','ACQUISITION','REPORT')); "
               "assert not any(name.split('.')[0] in {'sqlalchemy','polars','duckdb','pyarrow','pyspark'} "
               "or name in {'trackvance.db','trackvance.models','trackvance.worker','trackvance.services',"
               "'trackvance.artifactstore','trackvance.execution','trackvance.processing'} for name in sys.modules)")
    result = subprocess.run([sys.executable, "-c", program], env=environment,
                            capture_output=True, text=True, check=False, timeout=15)
    assert result.returncode == 0, result.stderr
    assert not list(tmp_path.iterdir())
