"""A module with unfinished work must not report a completed healthy state."""
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from trackvance import dashboard
from trackvance.models import Run


class DashboardRows:
    """Read-only query result provider; no database or artifact processing."""
    def __init__(self, runs):
        self.runs = runs

    def scalars(self, query):
        rows = self.runs if query.column_descriptions[0]["entity"] is Run else []
        return SimpleNamespace(all=lambda: rows)


def run(identity, module="intake", status="RUNNING", decision=None, metrics=None):
    now = datetime.now(UTC)
    return Run(id=identity, module=module, name=identity, config_id=identity,
               dataset_version_id=None, target_version_id=None, status=status,
               decision=decision, metrics=metrics or {}, execution_plan={},
               created_at=now, started_at=now,
               finished_at=now + timedelta(seconds=1) if status == "SUCCESS" else None)


def summary(monkeypatch, runs, module="intake", status=None):
    # Enrichment is unrelated to the module state and normally reads run/job
    # details. Keep that boundary read-free while exercising build_dashboard.
    monkeypatch.setattr(dashboard, "run_dto", lambda db, item: {"id": item.id, "name": item.name})
    result = dashboard.build_dashboard(DashboardRows(runs), "own-org",
        dashboard.DashboardFilters(module=module, status=status))
    return result, next(item for item in result["module_status"] if item["module"] == module)


@pytest.mark.parametrize("module", ["intake", "recon", "sentinel"])
@pytest.mark.parametrize("state", ["QUEUED", "RUNNING"])
def test_unfinished_module_without_population_is_in_progress(monkeypatch, module, state):
    result, item = summary(monkeypatch, [run("unfinished", module, state)], module)
    assert item["status"] == "IN_PROGRESS"
    assert item["runs"] == 1 and item["issues"] == item["processed_records"] == 0
    assert item["health_score"] is result["stats"]["health_score"] is None
    assert result["recent_runs"][0]["operational_status"] == "IN_PROGRESS"


@pytest.mark.parametrize("module,metrics", [
    ("intake", {"total_rows": 10, "valid_rows": 10}),
    ("recon", {"total_rows": 10, "matched": 10}),
    ("sentinel", {"total_checks": 10, "failed_checks": 0}),
])
def test_running_and_healthy_completed_work_remain_in_progress(monkeypatch, module, metrics):
    values = [run("unfinished", module), run("completed", module, "SUCCESS", "APPROVED", metrics)]
    result, item = summary(monkeypatch, values, module)
    assert item["status"] == "IN_PROGRESS" and item["runs"] == 2
    assert item["health_score"] == result["stats"]["health_score"] == 100
    filtered, current = summary(monkeypatch, values, module, "IN_PROGRESS")
    assert current["status"] == "IN_PROGRESS" and current["runs"] == 1
    assert current["health_score"] is filtered["stats"]["health_score"] is None


@pytest.mark.parametrize("state,decision", [("FAILED", None), ("SUCCESS", "REJECTED")])
def test_pending_work_does_not_hide_attention(monkeypatch, state, decision):
    _, item = summary(monkeypatch, [run("unfinished"), run("problem", status=state, decision=decision)])
    assert item["status"] == "ATTENTION" and item["issues"] == 1


@pytest.mark.parametrize("metrics,expected_health,expected_state", [
    ({}, None, "NO_DATA"),
    ({"total_rows": 0, "valid_rows": 0}, None, "NO_DATA"),
    ({"total_rows": 10}, None, "NO_DATA"),
    ({"total_rows": 10, "valid_rows": 0}, 0, "HEALTHY"),
    ({"total_rows": 10, "valid_rows": 10}, 100, "HEALTHY"),
])
def test_completed_module_requires_a_comparable_population(monkeypatch, metrics, expected_health, expected_state):
    _, item = summary(monkeypatch, [run("completed", status="SUCCESS", decision="APPROVED", metrics=metrics)])
    assert item["status"] == expected_state and item["health_score"] == expected_health


def test_empty_module_is_no_data(monkeypatch):
    _, item = summary(monkeypatch, [])
    assert item["status"] == "NO_DATA" and item["runs"] == 0 and item["health_score"] is None
