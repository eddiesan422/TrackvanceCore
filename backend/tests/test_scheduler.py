import json
from datetime import timedelta
from pathlib import Path

import pytest
from sqlalchemy import func, select

from trackvance.db import iso, utcnow
from trackvance.models import (
    AuditEvent,
    Configuration,
    Dataset,
    Finding,
    Job,
    MonitorOccurrence,
    MonitorSchedule,
    MonitorScheduleVersion,
    Run,
    SentinelMetricHistory,
    User,
)
from trackvance.scheduler import ScheduleError, dispatch_due, save_schedule
from trackvance.services import create_version, execute_run


def make_monitor(db, tmp_path, *, with_version=True, organization_id=None):
    user = db.get(User, "test-user")
    organization_id = organization_id or user.organization_id
    dataset = Dataset(name=f"Scheduled {utcnow().timestamp()}", organization_id=organization_id)
    db.add(dataset)
    db.flush()
    version = None
    if with_version:
        source = tmp_path / "schedule.csv"
        source.write_text("id,email\n1,a@example.test\n2,\n", encoding="utf-8")
        version = create_version(db, dataset, source, source.name)
    config = Configuration(name="Scheduled monitor", module="sentinel", dataset_id=dataset.id,
                           organization_id=organization_id,
                           config={"required_columns": ["email"], "null_columns": ["email"], "max_null_rate": 0})
    db.add(config)
    db.flush()
    return user, config, version


def test_tick_is_idempotent_captures_snapshot_actor_and_manifest(database, tmp_path):
    now = utcnow()
    with database() as db:
        user, config, version = make_monitor(db, tmp_path)
        schedule = save_schedule(db, config, user, interval_seconds=60, enabled=True, starts_at=now, expected_version=None)
        assert dispatch_due(db, now) == 1
        assert dispatch_due(db, now) == 0
        occurrence = db.scalar(select(MonitorOccurrence))
        run = db.get(Run, occurrence.run_id)
        assert run.dataset_version_id == version.id
        assert (run.initiated_by_type, run.initiated_by_id) == ("SYSTEM", "trackvance:local-scheduler")
        assert db.scalar(select(func.count()).select_from(Job)) == 1
        assert occurrence.schedule_version_id == db.scalar(select(MonitorScheduleVersion.id))
        assert run.execution_plan["schedule"]["planned_at"] == iso(now)
        execute_run(db, run)
        db.commit()
        assert run.status == "SUCCESS"
        assert run.decision == "ALERT"
        manifest = json.loads(Path(run.evidence_path).read_text(encoding="utf-8"))
        assert manifest["processing"]["schedule"]["schedule_id"] == schedule.id
        assert manifest["processing"]["schedule"]["occurrence_id"] == occurrence.id
        assert manifest["initiated_by"]["type"] == "SYSTEM"
        events = db.scalars(select(AuditEvent).where(AuditEvent.event_type == "MONITOR_SCHEDULE_DISPATCHED")).all()
        assert len(events) == 1
        assert events[0].metadata_json["schedule_id"] == schedule.id
        assert db.scalar(select(func.count()).select_from(SentinelMetricHistory)) > 0


def test_late_intervals_coalesce_and_busy_runs_are_not_duplicated(database, tmp_path):
    now = utcnow()
    with database() as db:
        user, config, _ = make_monitor(db, tmp_path)
        save_schedule(db, config, user, interval_seconds=60, enabled=True, starts_at=now, expected_version=None)
        assert dispatch_due(db, now + timedelta(minutes=10, seconds=5)) == 1
        first = db.scalar(select(MonitorOccurrence))
        assert first.coalesced_intervals == 10
        assert dispatch_due(db, now + timedelta(minutes=11)) == 1
        occurrences = db.scalars(select(MonitorOccurrence).order_by(MonitorOccurrence.planned_at)).all()
        assert [row.status for row in occurrences] == ["ENQUEUED", "SKIPPED"]
        assert occurrences[-1].reason_code == "MONITOR_BUSY"
        assert db.scalar(select(func.count()).select_from(Job)) == 1


def test_pause_revision_and_missing_dataset_are_evidenced(database, tmp_path):
    now = utcnow()
    with database() as db:
        user, config, _ = make_monitor(db, tmp_path, with_version=False)
        schedule = save_schedule(db, config, user, interval_seconds=60, enabled=False, starts_at=now, expected_version=None)
        assert dispatch_due(db, now) == 0
        save_schedule(db, config, user, interval_seconds=120, enabled=True, starts_at=now, expected_version=1)
        assert dispatch_due(db, now) == 1
        assert db.scalar(select(MonitorOccurrence)).reason_code == "NO_DATASET_VERSION"
        assert db.scalar(select(func.count()).select_from(MonitorScheduleVersion)) == 2
        assert db.scalar(select(MonitorScheduleVersion).where(MonitorScheduleVersion.version == 1)).enabled is False
        with pytest.raises(ScheduleError) as caught:
            save_schedule(db, config, user, interval_seconds=60, enabled=False, starts_at=now, expected_version=1)
        assert caught.value.code == "SCHEDULE_CONFLICT"
        assert schedule.version == 2


def test_dispatch_transaction_rolls_back_cursor_occurrence_and_job(database, tmp_path):
    now = utcnow()
    with database() as db:
        user, config, _ = make_monitor(db, tmp_path)
        save_schedule(db, config, user, interval_seconds=60, enabled=True, starts_at=now, expected_version=None)
        db.commit()
        assert dispatch_due(db, now) == 1
        db.rollback()
        assert db.scalar(select(func.count()).select_from(MonitorOccurrence)) == 0
        assert dispatch_due(db, now) == 1
        db.commit()
    with database() as restarted:
        assert dispatch_due(restarted, now) == 0
        assert restarted.scalar(select(func.count()).select_from(Job)) == 1


def test_stale_cursor_cannot_dispatch_twice(database, tmp_path):
    now = utcnow()
    with database() as db:
        user, config, _ = make_monitor(db, tmp_path)
        save_schedule(db, config, user, interval_seconds=60, enabled=True, starts_at=now, expected_version=None)
        db.commit()
    with database() as first, database() as second:
        stale = second.scalar(select(MonitorSchedule))
        second.commit()
        assert dispatch_due(first, now) == 1
        first.commit()
        assert dispatch_due(second, now) == 0
        assert stale.id


def test_revision_reusing_recorded_slot_advances_without_duplicate(database, tmp_path):
    now = utcnow()
    with database() as db:
        user, config, _ = make_monitor(db, tmp_path, with_version=False)
        save_schedule(db, config, user, interval_seconds=60, enabled=True, starts_at=now, expected_version=None)
        assert dispatch_due(db, now) == 1
        original = db.scalar(select(MonitorOccurrence))
        original_revision = original.schedule_version_id
        save_schedule(db, config, user, interval_seconds=120, enabled=True, starts_at=now, expected_version=1)
        assert dispatch_due(db, now) == 0
        assert db.scalar(select(func.count()).select_from(MonitorOccurrence)) == 1
        assert original.schedule_version_id == original_revision
        assert dispatch_due(db, now + timedelta(seconds=120)) == 1
        assert db.scalar(select(func.count()).select_from(MonitorOccurrence)) == 2


def test_invalid_monitor_does_not_block_other_schedules_or_leave_partial_jobs(database, tmp_path, monkeypatch):
    from trackvance import scheduler
    from trackvance.processing import ProcessingError

    original_enqueue = scheduler.enqueue

    def fail_after_partial_enqueue(db, config, source, target, actor):
        run = original_enqueue(db, config, source, target, actor)
        if config.name == "Broken monitor":
            db.flush()
            raise ProcessingError("Untrusted configuration value that must not enter errors")
        return run

    monkeypatch.setattr(scheduler, "enqueue", fail_after_partial_enqueue)
    now = utcnow()
    with database() as db:
        user, broken, _ = make_monitor(db, tmp_path)
        broken.name = "Broken monitor"
        _, healthy, _ = make_monitor(db, tmp_path)
        for monitor in (broken, healthy):
            save_schedule(db, monitor, user, interval_seconds=60, enabled=True, starts_at=now, expected_version=None)
        assert dispatch_due(db, now) == 2
        db.commit()
        failed = db.scalar(select(MonitorOccurrence).where(MonitorOccurrence.monitor_id == broken.id))
        assert failed.run_id is None
        assert failed.status == "FAILED_PRECONDITION"
        assert failed.reason_code == "INVALID_MONITOR_CONFIGURATION"
        assert db.scalar(select(func.count()).select_from(Run)) == 1
        assert db.scalar(select(func.count()).select_from(Job)) == 1
        assert db.scalar(select(Run)).config_id == healthy.id


def test_planner_rejection_has_explicit_occurrence_status(database, tmp_path):
    now = utcnow()
    with database() as db:
        user, config, version = make_monitor(db, tmp_path)
        version.row_count = 1_000_000_000
        save_schedule(db, config, user, interval_seconds=60, enabled=True, starts_at=now, expected_version=None)
        assert dispatch_due(db, now) == 1
        occurrence = db.scalar(select(MonitorOccurrence))
        assert occurrence.status == "FAILED_PRECONDITION"
        assert occurrence.reason_code
        assert db.get(Run, occurrence.run_id).status == "FAILED_PRECONDITION"


def test_schedule_api_validation_rbac_csrf_and_organization(authenticated, database, tmp_path):
    with database() as db:
        _, config, _ = make_monitor(db, tmp_path)
        _, foreign, _ = make_monitor(db, tmp_path, organization_id="other-org", with_version=False)
        config_id, foreign_id = config.id, foreign.id
        db.commit()
    url = f"/api/v1/monitors/{config_id}/schedule"
    assert authenticated.get(url).json() is None
    for payload in [{"interval_seconds": 0}, {"interval_seconds": 60.5}, {"interval_seconds": 60, "starts_at": "2026-01-01T12:00:00"}, {"interval_seconds": 60, "python": "anything"}]:
        assert authenticated.post(url, json=payload).status_code == 422
    created = authenticated.post(url, json={"interval_seconds": 60})
    assert created.status_code == 200, created.text
    assert created.json()["source_policy"] == "LATEST_REGISTERED_SNAPSHOT"
    assert authenticated.post(url, json={"interval_seconds": 120}).status_code == 409
    resources = ("schedule", "occurrences", "series", "alerts")
    for resource in resources:
        assert authenticated.get(f"/api/v1/monitors/{foreign_id}/{resource}").status_code == 404
    assert authenticated.post(f"/api/v1/monitors/{foreign_id}/schedule",
                              json={"interval_seconds": 60}).status_code == 404
    csrf = authenticated.headers.pop("X-CSRF-Token")
    assert authenticated.post(url, json={"interval_seconds": 120, "expected_version": 1}).status_code == 403
    authenticated.headers["X-CSRF-Token"] = csrf
    for role in ("Auditor", "Operations"):
        with database() as db:
            db.get(User, "test-user").role = role
            db.commit()
        for resource in resources:
            assert authenticated.get(f"/api/v1/monitors/{config_id}/{resource}").status_code == 200
        for enabled in (True, False):
            assert authenticated.post(url, json={"interval_seconds": 120, "expected_version": 1,
                                                 "enabled": enabled}).status_code == 403
    authenticated.cookies.clear()
    for resource in resources:
        assert authenticated.get(f"/api/v1/monitors/{config_id}/{resource}").status_code == 401
    assert authenticated.post(url, json={"interval_seconds": 120, "expected_version": 1}).status_code == 401


def test_series_separates_method_version_and_organization(authenticated, database, tmp_path):
    with database() as db:
        user, config, _ = make_monitor(db, tmp_path)
        now = utcnow()
        save_schedule(db, config, user, interval_seconds=60, enabled=True, starts_at=now, expected_version=None)
        dispatch_due(db, now)
        run = db.get(Run, db.scalar(select(MonitorOccurrence.run_id)))
        execute_run(db, run)
        for index, (method, definition) in enumerate([("SAMPLE", 2), ("EXACT_OBSERVED", 1)]):
            db.add(SentinelMetricHistory(organization_id=user.organization_id, monitor_id=config.id,
                run_id=run.id, metric_key="test_metric", dimension_hash=str(index), dimensions={},
                numeric_value=index, method=method, metric_definition_version=definition, observed_at=now))
        # Deliberately malformed cross-organization edges exercise both sides of
        # the join. Such records cannot be created by the public API; readers must
        # still exclude them rather than trust only the monitor's ownership.
        foreign_run = Run(organization_id="other-org", name="Private foreign execution",
            module="sentinel", status="SUCCESS", config_id=config.id,
            dataset_version_id=run.dataset_version_id, initiated_by="Other organization")
        db.add(foreign_run)
        db.flush()
        for organization_id, metric_run_id, suffix in (
            ("other-org", run.id, "foreign-row"),
            (user.organization_id, foreign_run.id, "foreign-run"),
        ):
            db.add(SentinelMetricHistory(organization_id=organization_id, monitor_id=config.id,
                run_id=metric_run_id, metric_key=f"PRIVATE_METRIC_{suffix}", dimension_hash=suffix,
                dimensions={"private": suffix}, numeric_value="987654321",
                method="EXACT_OBSERVED", metric_definition_version=1, observed_at=now))
            db.add(Finding(organization_id=organization_id, run_id=metric_run_id,
                title=f"PRIVATE_ALERT_{suffix}", code="NULL_RATE", severity="HIGH",
                fingerprint=suffix, details={"private": suffix}))
        db.commit()
        config_id = config.id
    response = authenticated.get(f"/api/v1/monitors/{config_id}/series")
    assert response.status_code == 200, response.text
    assert "PRIVATE_METRIC" not in response.text
    assert foreign_run.id not in response.text
    test_groups = [row for row in response.json()["items"] if row["metric_key"] == "test_metric"]
    assert len(test_groups) == 2
    assert all(row["points"][0]["dataset_version_id"] for row in test_groups)
    row_series = next(row for row in response.json()["items"] if row["metric_key"] == "row_count")
    assert row_series["points"][0]["value"] == 2
    assert row_series["points"][0]["exact_value"] == "2"
    alert_response = authenticated.get(f"/api/v1/monitors/{config_id}/alerts")
    assert alert_response.status_code == 200
    assert "PRIVATE_ALERT" not in alert_response.text
    assert foreign_run.id not in alert_response.text
    alerts = alert_response.json()["items"]
    assert alerts and all(row["run_id"] == run.id for row in alerts)
    occurrence = authenticated.get(f"/api/v1/monitors/{config_id}/occurrences").json()["items"][0]
    assert occurrence["status"] == "SUCCESS" and occurrence["decision"] == "ALERT"
    assert occurrence["started_at"] and occurrence["finished_at"]
