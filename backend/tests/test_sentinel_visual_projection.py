"""The visual API projects immutable evidence; it does not reinterpret rule outcomes."""

import pytest
from sqlalchemy import select

from trackvance.models import Configuration, Dataset, Run
from trackvance.sentinel_api import check_category, check_projection, null_threshold_checks
from trackvance.sentinel_auto import consume_dataset_event, ensure_automatic_monitor
from trackvance.sentinel_auto_models import SentinelVersionEvaluation
from trackvance.services import create_version, execute_run


@pytest.mark.parametrize("check,category", [
    ({"status": "PASS"}, "CONFORMING"),
    ({"status": "FAIL", "severity": "WARNING"}, "WARNING"),
    ({"status": "WARNING"}, "WARNING"),
    ({"status": "FAIL", "severity": "ERROR"}, "FAILED"),
    ({"status": "FAIL", "actual": None}, "FAILED"),
    ({"status": "UNAVAILABLE"}, "NOT_EVALUATED"),
    ({"status": "PASS", "kind": "OBSERVATION"}, "OBSERVATION"),
])
def test_check_category_preserves_actual_outcomes(check, category):
    assert check_category(check) == category


def test_thresholds_require_a_check_actually_recorded_for_the_column():
    checks = [check_projection(item, {}) for item in [
        {"code": "NULL_RATE_EMAIL", "name": "Nulos · email", "actual": 0.1, "expected": {"max": 0.05}, "status": "FAIL"},
        {"code": "R2", "metric_key": "null_rate:email", "expected": {"operator": "lte", "threshold": "0.1"}, "status": "PASS"},
        {"code": "BASE_NULLS", "name": "Nulos · email", "expected": "Observación exacta", "kind": "OBSERVATION"},
        {"code": "NULL_RATE_EMAIL", "name": "Unrelated custom rule", "expected": {"max": 999}, "status": "PASS"},
    ]]
    assert [item["expected"] for item in null_threshold_checks("email", checks)] == [
        {"max": 0.05}, {"operator": "lte", "threshold": "0.1"}]
    assert null_threshold_checks("EMAIL", checks) == []
    assert check_projection({"rule_id": "original-rule"}, {"rules": [
        {"rule_id": "original-rule", "type": "unique"}, {"rule_id": "new-rule", "type": "required"}]
    })["rule_type"] == "unique"


def test_visual_distribution_reconciles_and_retains_selected_threshold_evidence(authenticated, database, tmp_path, monkeypatch):
    from types import SimpleNamespace

    from trackvance import services

    with database() as db:
        dataset = Dataset(name="Visual immutable evidence")
        db.add(dataset)
        db.flush()
        path = tmp_path / "visual.csv"
        path.write_text("id,email\n001,a\n002,\n", encoding="utf-8")
        source = create_version(db, dataset, path, path.name)
        automatic = ensure_automatic_monitor(db, dataset, "test-user")
        consume_dataset_event(db, SimpleNamespace(id="visual-event", organization_id=source.organization_id,
            event_type="DATASET_VERSION_PUBLISHED", payload={"dataset_version_id": source.id,
                "responsible_user_id": "test-user", "publication_status": "COMPLETE"}))
        run = db.get(Run, db.scalar(select(SentinelVersionEvaluation)).run_id)
        execute_run(db, run)
        assert run.status == "SUCCESS"
        monitor = db.get(Configuration, automatic.monitor_root_id)
        run.metrics = {**run.metrics, "checks": [
            {"code": "SCHEMA_REQUIRED", "name": "Structure", "status": "PASS", "actual": [], "expected": []},
            {"code": "NULL_RATE_EMAIL", "name": "Nulos · email", "status": "FAIL", "severity": "WARNING", "actual": 0.5, "expected": {"max": 0.1}},
            {"code": "FRESHNESS", "name": "Actualización", "status": "FAIL", "actual": 62.13, "expected": {"max_hours": 48}},
            {"code": "UNAVAILABLE", "name": "No medido", "status": "UNAVAILABLE", "actual": None},
            {"code": "BASE_NULLS", "name": "Nulos", "status": "PASS", "kind": "OBSERVATION", "actual": 1},
        ], "total_checks": 4, "failed_checks": 2, "conforming_checks": 2}
        run_id, version_id, config_id, config_version = run.id, source.id, monitor.id, monitor.version
        config_hash = (run.execution_plan or {}).get("config_hash")
        original_metrics = run.metrics.copy()
        db.commit()
    monkeypatch.setattr(services, "ensure_input", lambda *args: (_ for _ in ()).throw(AssertionError("Unexpected dataset scan")))
    response = authenticated.get(f"/api/v1/runs/{run_id}/visual-summary")
    assert response.status_code == 200, response.text
    result = response.json()
    assert result["dataset_version_id"] == version_id and result["scans"] == 0
    assert result["evaluated_configuration"] == {"id": config_id, "name": monitor.name, "version": config_version, "config_hash": config_hash}
    assert result["control_distribution"] == {"conforming": 1, "warning": 1, "failed": 1, "not_evaluated": 1, "observations": 1, "total_controls": 4}
    assert result["indicators"]["conforming_checks"] == 1 and result["indicators"]["valid_rows"] is None
    email = next(item for item in result["columns"] if item["name"] == "email")
    assert email["null_denominator"] == 2 and email["null_count"] == 1
    assert [item["expected"] for item in email["null_threshold_checks"]] == [{"max": 0.1}]
    with database() as db:
        assert db.get(Run, run_id).metrics == original_metrics
