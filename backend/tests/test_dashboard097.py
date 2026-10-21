"""DASH-097-01 preserves absence, zero and the existing weighting contract."""
import polars as pl
import pytest
from test_dashboard import dataset_with_run

from trackvance.dashboard import aggregate_health, run_health_score
from trackvance.db import utcnow
from trackvance.models import Run
from trackvance.processing import intake, reconcile


@pytest.mark.parametrize("module,metrics", [
    ("intake", {}), ("intake", {"total_rows": 10}),
    ("recon", {"total_rows": 10}), ("sentinel", {"total_checks": 10}),
    ("sentinel", {"health_score": "NaN"}), ("intake", {"acceptance_rate": float("inf")}),
])
def test_missing_metrics_are_not_a_perfect_score(module, metrics):
    assert run_health_score(Run(module=module, status="SUCCESS", metrics=metrics)) is None


def test_zero_counts_and_automatic_monitor_exclusions_are_preserved():
    zero = Run(module="intake", status="SUCCESS", metrics={"total_rows": 10, "valid_rows": 0})
    absent = Run(module="intake", status="SUCCESS", metrics={"total_rows": 100})
    assert run_health_score(zero) == 0
    assert aggregate_health([zero, absent]) == 0
    assert run_health_score(Run(module="sentinel", status="SUCCESS", metrics={"health_score": 100},
        execution_plan={"sentinel_automatic": True})) is None


@pytest.mark.parametrize("module,count_key,score_key", [
    ("intake", "total_rows", "acceptance_rate"),
    ("recon", "total_rows", "match_rate"),
    ("sentinel", "total_checks", "health_score"),
])
@pytest.mark.parametrize("population", [None, 0, -1, float("nan"), float("inf"), True])
def test_successful_rate_requires_positive_evaluated_population(module, count_key, score_key, population):
    metrics = {score_key: 100, count_key: population}
    assert run_health_score(Run(module=module, status="SUCCESS", metrics=metrics)) is None


def test_empty_actual_processing_rates_are_not_dashboard_measurements():
    frame = pl.DataFrame(schema={"id": pl.String, "amount": pl.String})
    _, intake_metrics, _ = intake(frame, {"required_columns": ["id"], "max_error_rate": 0})
    _, recon_metrics = reconcile(frame, frame, {"key_columns": ["id"], "amount_column": "amount", "tolerance": "0.01"})
    # Processing keeps its historical empty-set rate; displaying evidence is a
    # Dashboard concern and must not change approval or processing semantics.
    assert intake_metrics["acceptance_rate"] == recon_metrics["match_rate"] == 100
    for module, metrics in [("intake", intake_metrics), ("recon", recon_metrics)]:
        assert metrics["total_rows"] == 0
        assert run_health_score(Run(module=module, status="SUCCESS", metrics=metrics)) is None


def test_empty_runs_stay_visible_but_do_not_bias_real_zero_measurements(authenticated, database):
    with database() as db:
        for identifier, module, metrics, decision in [
            ("empty-intake", "intake", {"total_rows": 0, "valid_rows": 0, "acceptance_rate": 100}, "APPROVED"),
            ("empty-recon", "recon", {"total_rows": 0, "matched": 0, "match_rate": 100}, "CONFORME"),
            ("measured-zero", "intake", {"total_rows": 10, "valid_rows": 0, "acceptance_rate": 0}, "REJECTED"),
        ]:
            dataset_with_run(db, identifier=identifier, name=identifier, criticality="LOW", module=module,
                metrics=metrics, decision=decision, created_at=utcnow())
        db.commit()
    general = authenticated.get("/api/v1/dashboard").json()
    assert general["stats"]["runs"] == 3
    assert general["stats"]["health_score"] == 0
    assert len(general["recent_runs"]) == 3
    point = general["health_history"][0]
    assert point["overall"] == point["intake"] == 0
    assert point["recon"] is None
    assert point["comparable_runs"] == {"overall": 1, "intake": 1, "recon": 0, "sentinel": 0}
    empty_recon = authenticated.get("/api/v1/dashboard?module=recon").json()
    assert empty_recon["stats"]["runs"] == 1
    assert empty_recon["stats"]["health_score"] is None


def test_history_reports_only_comparable_sample_counts_with_global_filters(authenticated, database):
    with database() as db:
        for identifier, module, metrics in [
            ("missing-health", "intake", {"total_rows": 100}),
            ("zero-health", "intake", {"total_rows": 10, "valid_rows": 0}),
            ("matched-health", "recon", {"total_rows": 10, "matched": 10}),
        ]:
            dataset_with_run(db, identifier=identifier, name=identifier, criticality="LOW", module=module,
                metrics=metrics, decision="APPROVED", created_at=utcnow())
        db.commit()
    general = authenticated.get("/api/v1/dashboard").json()
    assert general["stats"]["health_score"] == 50
    assert general["health_history"][0]["comparable_runs"] == {"overall": 2, "intake": 1, "recon": 1, "sentinel": 0}
    intake = authenticated.get("/api/v1/dashboard?module=intake").json()
    assert intake["stats"]["health_score"] == 0
    assert intake["health_history"][0]["recon"] is None


def test_technical_zero_is_identified_separately_from_observed_acceptance(authenticated, database):
    with database() as db:
        _, failed = dataset_with_run(db, identifier="technical-health", name="Failed query", criticality="LOW",
            module="intake", metrics={}, decision=None, created_at=utcnow())
        failed.status = "FAILED"
        db.commit()
    result = authenticated.get("/api/v1/dashboard").json()
    point = result["health_history"][0]
    assert point["overall"] == 0
    assert point["comparable_runs"]["intake"] == 1
    assert point["technical_failures"] == {"overall": 1, "intake": 1, "recon": 0, "sentinel": 0}
