"""Automatic Sentinel uses complete publications and immutable execution metrics."""

from datetime import timedelta
from types import SimpleNamespace

import polars as pl
import pytest
from sqlalchemy import func, select

from trackvance.acquisition_models import AcquisitionRun, AcquisitionUpload
from trackvance.audit_context import Actor
from trackvance.db import utcnow
from trackvance.models import (
    Configuration,
    Dataset,
    Job,
    Role,
    RolePermission,
    Run,
    SentinelMetricHistory,
    User,
)
from trackvance.processing import profile_frame
from trackvance.publication import publish_version_event
from trackvance.scheduler import save_schedule
from trackvance.sentinel_auto import (
    backfill_automatic_monitors,
    consume_dataset_event,
    dispatch_daily,
    ensure_automatic_monitor,
)
from trackvance.sentinel_auto_models import (
    AutomaticMonitorRevision,
    SentinelLightReview,
    SentinelVersionEvaluation,
)
from trackvance.services import create_version, execute_run


def source_version(db, tmp_path, name="Auto", content="id,category,optional\n0001,A,\n0002,A,\n0002,A,\n"):
    dataset = Dataset(name=name)
    db.add(dataset)
    db.flush()
    path = tmp_path / f"{name}.csv"
    path.write_text(content, encoding="utf-8")
    version = create_version(db, dataset, path, path.name)
    automatic = ensure_automatic_monitor(db, dataset, "test-user")
    return dataset, version, automatic


def publication(source, identity="event-1", complete=True):
    return SimpleNamespace(id=identity, organization_id=source.organization_id, event_type="DATASET_VERSION_PUBLISHED",
        payload={"dataset_version_id": source.id, "responsible_user_id": "test-user",
                 "publication_status": "COMPLETE" if complete else "IN_PROGRESS"})


def acquisition_publication(db, source):
    upload = AcquisitionUpload(user_id="test-user", filename="source.csv", path="private/source.csv",
        sha256=source.sha256, size_bytes=source.size_bytes, source_format="CSV",
        expires_at=utcnow() + timedelta(hours=1))
    db.add(upload)
    db.flush()
    acquisition = AcquisitionRun(dataset_id=source.dataset_id, upload_id=upload.id,
        source_type="UPLOAD", filename=upload.filename, request_hash="a" * 64,
        initiated_by_id="test-user", initiated_by_name="Test User", status="RUNNING",
        stage="PUBLISHING", attempt_id="publication-attempt")
    db.add(acquisition)
    db.flush()
    source.ingestion_metadata = {**source.ingestion_metadata, "acquisition_id": acquisition.id,
                                "acquisition_attempt_id": acquisition.attempt_id}
    return acquisition


def test_worker_publication_uses_acquisition_requester_and_revalidates_revoked_permission(database, tmp_path):
    with database() as db:
        user = db.get(User, "test-user")
        role = Role(name="Acquisition monitor executor", normalized_name="acquisition monitor executor",
                    organization_id=user.organization_id)
        db.add(role)
        db.flush()
        user.role_id = role.id
        for code in ("sentinel:read", "sentinel:execute", "datasets:read"):
            db.add(RolePermission(role_id=role.id, permission_code=code))
        dataset = Dataset(name="Worker acquisition publication")
        db.add(dataset)
        db.flush()
        path = tmp_path / "source.csv"
        path.write_text("id\n001\n", encoding="utf-8")
        first = create_version(db, dataset, path, path.name)
        acquisition_publication(db, first)
        event = publish_version_event(db, first, Actor("WORKER", "worker-lease", "Acquisition worker"))
        assert first.source_run_id is None
        assert event.payload["responsible_user_id"] == user.id
        consume_dataset_event(db, event)
        evaluation = db.scalar(select(SentinelVersionEvaluation).where(
            SentinelVersionEvaluation.dataset_version_id == first.id))
        assert evaluation.status == "ENQUEUED"
        assert db.get(Run, evaluation.run_id).initiated_by_id == user.id
        db.delete(db.get(RolePermission, (role.id, "sentinel:execute")))
        db.flush()
        second = create_version(db, dataset, path, path.name)
        acquisition_publication(db, second)
        second_event = publish_version_event(db, second, Actor("WORKER", "worker-lease", "Acquisition worker"))
        assert second_event.payload["responsible_user_id"] == user.id
        consume_dataset_event(db, second_event)
        blocked = db.scalar(select(SentinelVersionEvaluation).where(
            SentinelVersionEvaluation.dataset_version_id == second.id))
        assert blocked.status == "BLOCKED" and blocked.reason_code == "SCHEDULE_PERMISSION_REVOKED"
        assert blocked.run_id is None
        assert db.scalar(select(func.count()).select_from(Job)) == 1


@pytest.mark.parametrize("mismatch", ["attempt", "dataset", "source_type"])
def test_worker_publication_does_not_adopt_an_unrelated_or_stale_acquisition(database, tmp_path, mismatch):
    with database() as db:
        dataset = Dataset(name="Unproven publication")
        db.add(dataset)
        db.flush()
        path = tmp_path / "source.csv"
        path.write_text("id\n001\n", encoding="utf-8")
        source = create_version(db, dataset, path, path.name)
        acquisition = acquisition_publication(db, source)
        if mismatch == "attempt":
            acquisition.attempt_id = "newer-attempt"
        elif mismatch == "dataset":
            unrelated = Dataset(name="Different acquisition dataset")
            db.add(unrelated)
            db.flush()
            acquisition.dataset_id = unrelated.id
        else:
            source.source_type = "REST"
        event = publish_version_event(db, source, Actor("WORKER", "worker-lease", "Acquisition worker"))
        assert event.payload["responsible_user_id"] is None
        consume_dataset_event(db, event)
        blocked = db.scalar(select(SentinelVersionEvaluation))
        assert blocked.status == "BLOCKED" and blocked.reason_code == "SCHEDULE_EXECUTOR_DISABLED"
        assert blocked.run_id is None


def test_missing_monitor_waits_and_backfill_is_idempotent(database):
    with database() as db:
        dataset = Dataset(name="No versions")
        db.add(dataset)
        db.flush()
        automatic = ensure_automatic_monitor(db, dataset, "test-user")
        assert automatic.responsible_user_id == "test-user"
        assert ensure_automatic_monitor(db, dataset).id == automatic.id
        assert backfill_automatic_monitors(db) == 0
        assert db.scalar(select(func.count()).select_from(Configuration)) == 1
        assert db.scalar(select(func.count()).select_from(AutomaticMonitorRevision)) == 1
        assert dispatch_daily(db, utcnow()) == 1
        assert db.scalar(select(SentinelLightReview)).status == "WAITING_FIRST_VERSION"
        assert db.scalar(select(func.count()).select_from(Job)) == 0


def test_existing_monitor_revisions_custom_rules_and_explicit_pause_are_preserved(database):
    with database() as db:
        dataset = Dataset(name="Customized")
        db.add(dataset)
        db.flush()
        config = Configuration(name="Explicit", module="sentinel", dataset_id=dataset.id,
                               config={"null_columns": ["important"], "max_null_rate": 0})
        db.add(config)
        db.flush()
        revision = Configuration(name=config.name, module="sentinel", dataset_id=dataset.id,
            version=2, previous_version_id=config.id, config={"max_age_hours": 999})
        db.add(revision)
        db.flush()
        user = db.get(User, "test-user")
        schedule = save_schedule(db, revision, user, interval_seconds=3600, enabled=False,
                                 starts_at=None, expected_version=None)
        automatic = ensure_automatic_monitor(db, dataset, user.id)
        assert automatic.monitor_root_id == config.id and automatic.enabled is False
        assert schedule.enabled is False and revision.config == {"max_age_hours": 999}
        assert db.scalar(select(func.count()).select_from(Configuration)) == 2


def test_revoked_explicit_legacy_executor_is_not_replaced_by_an_authorized_publisher(database, tmp_path):
    with database() as db:
        user = db.get(User, "test-user")
        role = Role(name="Existing monitor executor", normalized_name="existing monitor executor",
                    organization_id=user.organization_id)
        db.add(role)
        db.flush()
        user.role_id = role.id
        for code in ("sentinel:read", "sentinel:execute", "datasets:read"):
            db.add(RolePermission(role_id=role.id, permission_code=code))
        publisher = User(id="authorized-publisher", name="Publisher", email="publisher@example.test",
                         password_hash=user.password_hash)
        dataset = Dataset(name="Explicit legacy owner")
        db.add_all([publisher, dataset])
        db.flush()
        path = tmp_path / "legacy.csv"
        path.write_text("id\n001\n", encoding="utf-8")
        source = create_version(db, dataset, path, path.name)
        monitor = Configuration(name="Historical monitor", module="sentinel", dataset_id=dataset.id, config={})
        db.add(monitor)
        db.flush()
        save_schedule(db, monitor, user, interval_seconds=86400, enabled=True,
                      starts_at=None, expected_version=None)
        db.delete(db.get(RolePermission, (role.id, "sentinel:execute")))
        db.flush()
        automatic = ensure_automatic_monitor(db, dataset, publisher.id)
        assert automatic.responsible_user_id == user.id
        consume_dataset_event(db, publication(source))
        evaluation = db.scalar(select(SentinelVersionEvaluation))
        assert evaluation.status == "BLOCKED" and evaluation.reason_code == "SCHEDULE_PERMISSION_REVOKED"
        assert evaluation.run_id is None


def test_each_complete_version_and_replay_have_one_job(database, tmp_path):
    with database() as db:
        dataset, first, _ = source_version(db, tmp_path)
        consume_dataset_event(db, publication(first, complete=False))
        assert db.scalar(select(func.count()).select_from(SentinelVersionEvaluation)) == 0
        consume_dataset_event(db, publication(first))
        consume_dataset_event(db, publication(first, identity="replayed-event"))
        path = tmp_path / "v2.csv"
        path.write_text("id,category,optional\n0003,A,\n", encoding="utf-8")
        second = create_version(db, dataset, path, path.name)
        consume_dataset_event(db, publication(second, identity="event-2"))
        evaluations = db.scalars(select(SentinelVersionEvaluation).order_by(SentinelVersionEvaluation.created_at)).all()
        assert [item.dataset_version_id for item in evaluations] == [first.id, second.id]
        assert len({item.run_id for item in evaluations}) == 2
        assert db.scalar(select(func.count()).select_from(Job)) == 2
        run = db.get(Run, evaluations[0].run_id)
        assert run.execution_plan["sentinel_automatic"]["responsible_user_id"] == "test-user"
        assert run.initiated_by_type == "USER"


def test_profile_reuse_has_no_scan_and_categories_nulls_are_observations(database, tmp_path, monkeypatch):
    from trackvance import services
    with database() as db:
        _, source, _ = source_version(db, tmp_path)
        user = db.get(User, "test-user")
        role = Role(name="Sentinel runner", normalized_name="sentinel runner", organization_id=user.organization_id)
        db.add(role)
        db.flush()
        user.role_id = role.id
        for code in ("sentinel:read", "sentinel:execute", "datasets:read"):
            db.add(RolePermission(role_id=role.id, permission_code=code))
        db.flush()
        consume_dataset_event(db, publication(source))
        run = db.get(Run, db.scalar(select(SentinelVersionEvaluation)).run_id)
        db.commit()
        monkeypatch.setattr(services, "ensure_input", lambda *args: (_ for _ in ()).throw(AssertionError("New data scan")))
        execute_run(db, run)
        assert run.status == "SUCCESS"
        assert run.execution_plan["runtime"]["scans"] == 0
        assert run.metrics["profile_dataset_version_id"] == source.id
        assert run.metrics["valid_rows"] is None
        observations = [item for item in run.metrics["checks"] if item.get("kind") == "OBSERVATION"]
        assert all(item["status"] == "OBSERVED" for item in observations)
        assert run.metrics["health_score"] is None
        from trackvance.dashboard import aggregate_health, run_health_score
        assert run_health_score(run) is None and aggregate_health([run]) is None
        assert any(item["code"] == "BASE_NULLS" and item["actual"] == 3 for item in observations)
        assert source.profile["duplicate_row_count"] == 1
        assert any(item["code"] == "BASE_DUPLICATE_ROWS" and item["status"] == "FAIL"
                   and item["severity"] == "WARNING" for item in run.metrics["checks"])


def test_responsible_permissions_revalidated_before_compute(database, tmp_path):
    with database() as db:
        _, source, _ = source_version(db, tmp_path)
        user = db.get(User, "test-user")
        role = Role(name="Sentinel runner", normalized_name="sentinel runner", organization_id=user.organization_id)
        db.add(role)
        db.flush()
        user.role_id = role.id
        for code in ("sentinel:read", "sentinel:execute", "datasets:read"):
            db.add(RolePermission(role_id=role.id, permission_code=code))
        db.flush()
        consume_dataset_event(db, publication(source))
        run = db.get(Run, db.scalar(select(SentinelVersionEvaluation)).run_id)
        permission = db.get(RolePermission, (user.role_id, "sentinel:execute"))
        db.delete(permission)
        db.commit()
        execute_run(db, run)
        assert run.status == "FAILED_PRECONDITION" and run.error == "SCHEDULE_PERMISSION_REVOKED"
        assert not run.result_path


def test_historical_profile_missing_duplicates_is_processed_without_rewriting_version(database, tmp_path, monkeypatch):
    from trackvance import services
    calls = []
    ensure_input = services.ensure_input
    def capture(*args):
        calls.append(args[0].id)
        return ensure_input(*args)
    monkeypatch.setattr(services, "ensure_input", capture)
    with database() as db:
        _, source, _ = source_version(db, tmp_path)
        source.profile = {key: value for key, value in source.profile.items()
                          if key not in {"duplicate_row_count", "distinct_row_count", "row_duplicate_definition"}}
        db.flush()
        consume_dataset_event(db, publication(source))
        run = db.get(Run, db.scalar(select(SentinelVersionEvaluation)).run_id)
        execute_run(db, run)
        assert run.status == "SUCCESS"
        assert calls == [source.id]
        assert run.metrics["profile_snapshot"]["duplicate_row_count"] == 1
        assert "duplicate_row_count" not in source.profile


def test_no_executor_is_inferred_from_an_arbitrary_admin(database):
    with database() as db:
        dataset = Dataset(name="Historical unowned")
        db.add(dataset)
        db.flush()
        automatic = ensure_automatic_monitor(db, dataset)
        assert automatic.responsible_user_id is None
        assert dispatch_daily(db) == 1
        review = db.scalar(select(SentinelLightReview))
        assert review.status == "BLOCKED" and review.reason_code == "SCHEDULE_EXECUTOR_DISABLED"


def test_daily_review_is_metadata_only_coalesced_and_idempotent(database, tmp_path, monkeypatch):
    from trackvance import services
    with database() as db:
        _, source, automatic = source_version(db, tmp_path)
        start = utcnow()
        automatic.next_review_at = start
        source.created_at = start - timedelta(hours=49)
        db.flush()
        monkeypatch.setattr(services, "ensure_input", lambda *args: (_ for _ in ()).throw(AssertionError("New data scan")))
        assert dispatch_daily(db, start + timedelta(days=3)) == 1
        assert dispatch_daily(db, start + timedelta(days=3)) == 0
        review = db.scalar(select(SentinelLightReview))
        assert review.status == "STALE" and review.metrics["scans"] == 0
        assert review.dataset_version_id == source.id
        assert db.scalar(select(func.count()).select_from(Job)) == 0


def test_pause_preserves_new_version_identity_without_creating_work(database, tmp_path):
    with database() as db:
        _, source, automatic = source_version(db, tmp_path)
        automatic.enabled = False
        db.flush()
        consume_dataset_event(db, publication(source))
        evaluation = db.scalar(select(SentinelVersionEvaluation))
        assert evaluation.status == "PAUSED" and evaluation.run_id is None
        assert dispatch_daily(db) == 0


def test_exact_row_duplicates_include_nulls_and_preserve_text_identity():
    _, profile, _ = profile_frame(pl.DataFrame({"id": ["001", "001", "1", "001"],
                                               "value": [None, None, None, ""]}))
    assert profile["distinct_row_count"] == 3 and profile["duplicate_row_count"] == 1


def test_visual_summary_uses_selected_version_and_never_scans(authenticated, database, tmp_path, monkeypatch):
    from trackvance import services
    with database() as db:
        dataset, first, _ = source_version(db, tmp_path)
        consume_dataset_event(db, publication(first))
        first_run = db.get(Run, db.scalar(select(SentinelVersionEvaluation)).run_id)
        execute_run(db, first_run)
        path = tmp_path / "new.csv"
        path.write_text("id,category,optional\n0099,B,present\n", encoding="utf-8")
        second = create_version(db, dataset, path, path.name)
        consume_dataset_event(db, publication(second, identity="event-two"))
        second_run = db.get(Run, db.scalar(select(SentinelVersionEvaluation).where(
            SentinelVersionEvaluation.dataset_version_id == second.id)).run_id)
        execute_run(db, second_run)
        db.commit()
        first_id, first_version = first_run.id, first.id
    monkeypatch.setattr(services, "ensure_input", lambda *args: (_ for _ in ()).throw(AssertionError("New data scan")))
    response = authenticated.get(f"/api/v1/runs/{first_id}/visual-summary")
    assert response.status_code == 200, response.text
    result = response.json()
    assert result["dataset_version_id"] == first_version and result["indicators"]["row_count"] == 3
    assert result["indicators"]["duplicate_row_count"] == 1 and result["indicators"]["valid_rows"] is None
    assert result["scans"] == 0
    assert result["evolution"]
    row_series = next(item for item in result["evolution"] if item["metric_key"] == "row_count")
    assert any(point["run_id"] == first_id and point["exact_value"] == "3" for point in row_series["points"])
    assert all(point["dataset_version_id"] == first_version for series in result["evolution"] for point in series["points"])


@pytest.mark.parametrize("content,population,nulls", [("id,optional\n001,\n002,\n", 2, 2), ("id,optional\n", 0, 0)])
def test_visual_summary_does_not_present_zero_denominator_ratios_as_zero(
        authenticated, database, tmp_path, monkeypatch, content, population, nulls):
    from trackvance import services
    with database() as db:
        _, source, _ = source_version(db, tmp_path, name="Undefined ratios", content=content)
        consume_dataset_event(db, publication(source))
        run = db.get(Run, db.scalar(select(SentinelVersionEvaluation)).run_id)
        execute_run(db, run)
        assert run.status == "SUCCESS"
        original_profile = source.profile.copy()
        original_hash = source.sha256
        run_id, version_id = run.id, source.id
        db.commit()
    monkeypatch.setattr(services, "ensure_input", lambda *args: (_ for _ in ()).throw(AssertionError("New data scan")))
    response = authenticated.get(f"/api/v1/runs/{run_id}/visual-summary")
    assert response.status_code == 200, response.text
    result = response.json()
    column = next(item for item in result["columns"] if item["name"] == "optional")
    assert result["indicators"]["row_count"] == population
    assert column["null_count"] == nulls and column["distinct_count"] == 0
    assert column["distinct_denominator"] == 0
    assert column["distinct_rate"] is None and column["uniqueness_ratio"] is None
    assert column["null_rate"] == (1 if population else None)
    undefined = {"distinct_rate:optional", "uniqueness_ratio:optional"}
    if population == 0:
        undefined |= {"null_rate:optional", "null_rate:id", "distinct_rate:id", "uniqueness_ratio:id"}
    undefined_records = [item for item in result["exact_metrics"] if item["metric_key"] in undefined]
    assert {item["metric_key"] for item in undefined_records} == undefined
    assert all(item["exact_value"] is None and item["value"] is None
               and item["unavailable_reason"] == "UNDEFINED_DENOMINATOR"
               for item in undefined_records)
    assert not any(series["metric_key"] in undefined for series in result["evolution"])
    assert result["scans"] == 0
    with database() as db:
        restored = db.get(type(source), version_id)
        assert restored.sha256 == original_hash and restored.profile == original_profile


def test_automatic_settings_keep_revisions_and_reject_stale_write(authenticated, database):
    with database() as db:
        dataset = Dataset(name="Settings")
        db.add(dataset)
        db.flush()
        automatic = ensure_automatic_monitor(db, dataset, "test-user")
        monitor_id = automatic.monitor_root_id
        db.commit()
    body = {"enabled": False, "expected_version": 1, "review_interval_seconds": 86400}
    response = authenticated.post(f"/api/v1/monitors/{monitor_id}/automatic", json=body)
    assert response.status_code == 200, response.text
    assert response.json()["state"] == "PAUSED"
    assert authenticated.post(f"/api/v1/monitors/{monitor_id}/automatic", json=body).status_code == 409
    with database() as db:
        revisions = db.scalars(select(AutomaticMonitorRevision).order_by(AutomaticMonitorRevision.version)).all()
        assert [item.settings["enabled"] for item in revisions] == [True, False]


def test_historical_band_uses_revision_identity_not_an_unrelated_monitor_name(database, tmp_path):
    from trackvance.services import _sentinel_context
    with database() as db:
        dataset, source, automatic = source_version(db, tmp_path)
        monitor = db.get(Configuration, automatic.monitor_root_id)
        monitor.config = {**monitor.config, "rules": [{"type": "historical_band",
                           "parameters": {"metric": "row_count", "window": 10}}]}
        unrelated = Configuration(name=monitor.name, module="sentinel", dataset_id=dataset.id, config={})
        db.add(unrelated)
        db.flush()
        for config_id, value in ((monitor.id, "3"), (unrelated.id, "900")):
            previous = Run(name="Completed sample", module="sentinel", config_id=config_id,
                dataset_version_id=source.id, initiated_by="Test", status="SUCCESS", decision="HEALTHY")
            db.add(previous)
            db.flush()
            db.add(SentinelMetricHistory(organization_id=dataset.organization_id, monitor_id=config_id,
                run_id=previous.id, metric_key="row_count", dimensions={}, dimension_hash="0" * 64,
                numeric_value=value, method=source.profile["metric_method"],
                metric_definition_version=source.profile["metric_definition_version"], observed_at=utcnow()))
        current = Run(name="Current sample", module="sentinel", config_id=monitor.id,
                      dataset_version_id=source.id, initiated_by="Test")
        db.add(current)
        db.flush()
        assert [point["numeric_value"] for point in _sentinel_context(db, current, source, monitor)["history"]] == ["3"]
