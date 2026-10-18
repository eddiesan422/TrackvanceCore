"""AUT-097: real REST worker, metadata-only scheduling and isolated occurrences."""
import json
from datetime import timedelta

import pytest
from sqlalchemy import event, func, select
from sqlalchemy.orm import Session
from test_acquisition_095 import _settings, completed, connection, initial
from test_acquisition_095 import remote as _remote_fixture

from trackvance import acquisition_schedule, component_health, dispatcher, scheduler
from trackvance.acquisition_models import AcquisitionRun
from trackvance.acquisition_schedule_models import (
    AcquisitionOccurrence,
    AcquisitionSourceConfiguration,
    AcquisitionSourceRevision,
)
from trackvance.automation_models import OutboxEvent
from trackvance.credential_store import SecretStoreError
from trackvance.db import utcnow
from trackvance.models import (
    AuditEvent,
    DatasetVersion,
    ExternalConnection,
    ExternalConnectionVersion,
    Job,
    User,
)

remote = _remote_fixture


def configured(client, remote, *, name="Scheduled dataset", **settings):
    source = connection(client, remote)
    first = completed(client, initial(client, source, name=name))
    state = _settings(client, source, first["dataset_id"], enabled=True, interval_seconds=30, publication_policy="EVERY_CUT", **settings)
    return source, first, state


def test_thirty_second_schedule_never_opens_source_or_resolves_secret(authenticated, database, remote, monkeypatch):
    source, first, state = configured(authenticated, remote)
    assert state["settings"]["interval_seconds"] == 30 and state["timezone"] == "America/Bogota"
    assert "_source" not in state["settings"] and state["next_run_at"].endswith("Z")
    remote.calls.clear()
    def forbidden(*_args, **_kwargs):
        raise AssertionError("Scheduler accessed external credentials/source")
    with monkeypatch.context() as metadata_only:
        metadata_only.setattr("trackvance.connections_service.secret_store.get", forbidden)
        metadata_only.setattr("trackvance.acquisition.source_registry.create", forbidden)
        with database() as db:
            assert acquisition_schedule.dispatch_due(db, utcnow() + timedelta(seconds=31)) == 1
            occurrence = db.scalar(select(AcquisitionOccurrence))
            run = db.get(AcquisitionRun, occurrence.acquisition_id)
            run_id = run.id
            assert occurrence.status == "ENQUEUED" and occurrence.dispatched_at
            assert run.source_snapshot["origin"] == "SCHEDULED"
            assert run.source_snapshot["occurrence_id"] == occurrence.id
            assert run.source_snapshot["source_configuration_version"] == 1
            assert run.source_snapshot["connection_version_id"] == source["connection_version_id"]
            assert run.initiated_by_id == "test-user"
            assert db.scalar(select(Job).where(Job.acquisition_id == run_id)).status == "QUEUED"
            db.commit()
    assert remote.calls == []
    final = completed(authenticated, {"id": run_id})
    assert final["status"] == "SUCCESS" and final["output_version_id"] != first["output_version_id"]
    assert remote.calls


@pytest.mark.parametrize("interval", [29, 0, 2678401, 30.5, True])
def test_interval_rejected_without_silent_conversion(authenticated, database, remote, interval):
    source, first, _ = configured(authenticated, remote)
    body = {"expected_version": 1, "connection_id": source["id"], "schema_name": "REST", "object_name": "collection",
        "responsible_user_id": "test-user", "interval_seconds": interval}
    assert authenticated.put(f"/api/v1/datasets/{first['dataset_id']}/source-settings", json=body).status_code == 422
    with database() as db:
        assert db.scalar(select(AcquisitionSourceConfiguration)).version == 1


def test_calendar_pins_connection_revision_and_selection_despite_later_mutation(authenticated, database, remote):
    source, first, _ = configured(authenticated, remote)
    with database() as db:
        connection_row = db.get(ExternalConnection, source["id"])
        original = db.get(ExternalConnectionVersion, source["connection_version_id"])
        db.add(ExternalConnectionVersion(organization_id=original.organization_id, connection_id=connection_row.id,
            version=2, host=original.host, port=original.port, database=original.database, username=original.username,
            options={**original.options, "url": "https://unreachable.invalid/later"}, secret_reference=original.secret_reference, config_hash="later-revision"))
        connection_row.version = 2
        db.commit()
    with database() as db:
        acquisition_schedule.dispatch_due(db, utcnow() + timedelta(seconds=31))
        occurrence = db.scalar(select(AcquisitionOccurrence))
        run = db.get(AcquisitionRun, occurrence.acquisition_id)
        assert run.connection_version_id == source["connection_version_id"]
        assert run.source_snapshot["schema_name"] == "REST" and run.source_snapshot["object_name"] == "collection"
        run_id = run.id
        db.commit()
    assert completed(authenticated, {"id": run_id})["status"] == "SUCCESS"
    assert authenticated.get(f"/api/v1/datasets/{first['dataset_id']}/acquisition-occurrences").json()["total"] == 1


def test_blocked_source_and_database_registration_error_do_not_rollback_other_source(authenticated, database, remote, monkeypatch):
    source, first, _ = configured(authenticated, remote)
    second = completed(authenticated, initial(authenticated, source, name="Independent valid dataset"))
    _settings(authenticated, source, second["dataset_id"], enabled=True, interval_seconds=30)
    real_enqueue = acquisition_schedule.enqueue_update
    def fail_one(db, dataset, user, **kwargs):
        if dataset.id == first["dataset_id"]:
            # A real failed insert poisons its savepoint, exercising recovery.
            db.add(Job(organization_id=user.organization_id, acquisition_id="missing-acquisition", lane="INVALID"))
            db.flush()
        return real_enqueue(db, dataset, user, **kwargs)
    monkeypatch.setattr(acquisition_schedule, "enqueue_update", fail_one)
    with database() as db:
        now = utcnow() + timedelta(seconds=31)
        assert acquisition_schedule.dispatch_due(db, now) == 2
        assert acquisition_schedule.dispatch_due(db, now) == 0
        db.commit()
        occurrences = db.scalars(select(AcquisitionOccurrence)).all()
        assert len(occurrences) == 2
        blocked = next(item for item in occurrences if item.status == "BLOCKED")
        queued = next(item for item in occurrences if item.status == "ENQUEUED")
        assert blocked.acquisition_id is None and blocked.reason_code == "ACQUISITION_DISPATCH_FAILED"
        assert blocked.error_details["reference"] == blocked.id
        assert queued.acquisition_id
        assert db.scalar(select(func.count()).select_from(Job).where(Job.status == "QUEUED")) == 1
        assert db.scalar(select(OutboxEvent).where(OutboxEvent.aggregate_id == blocked.id)).event_type == "ACQUISITION_SCHEDULE_BLOCKED"
    feed = authenticated.get(f"/api/v1/datasets/{first['dataset_id']}/acquisition-occurrences?limit=1").json()
    assert feed["items"][0]["status"] == "BLOCKED" and feed["items"][0]["reason_message"]


def test_responsible_revocation_connection_disable_and_recovery_are_explicit(authenticated, database, remote):
    source, _, _ = configured(authenticated, remote)
    with database() as db:
        config = db.scalar(select(AcquisitionSourceConfiguration))
        now = config.next_run_at.replace(tzinfo=utcnow().tzinfo)
        db.get(User, "test-user").active = False
        assert acquisition_schedule.dispatch_due(db, now) == 1
        db.commit()
        occurrence = db.scalar(select(AcquisitionOccurrence))
        assert occurrence.status == "BLOCKED" and occurrence.reason_code == "ACQUISITION_RESPONSIBLE_UNAVAILABLE"
        db.get(User, "test-user").active = True
        db.get(ExternalConnection, source["id"]).enabled = False
        assert acquisition_schedule.dispatch_due(db, now + timedelta(seconds=30)) == 1
        db.commit()
        assert db.scalar(select(AcquisitionOccurrence).order_by(AcquisitionOccurrence.planned_at.desc())).reason_code == "CONNECTION_DISABLED"
        db.get(ExternalConnection, source["id"]).enabled = True
        assert acquisition_schedule.dispatch_due(db, now + timedelta(seconds=150)) == 1
        assert acquisition_schedule.dispatch_due(db, now + timedelta(seconds=150)) == 0
        db.commit()
        final = db.scalar(select(AcquisitionOccurrence).order_by(AcquisitionOccurrence.planned_at.desc()))
        assert final.status == "ENQUEUED" and final.coalesced_intervals == 3
        assert db.scalar(select(func.count()).select_from(AcquisitionOccurrence)) == 3
        assert db.scalar(select(func.count()).select_from(DatasetVersion)) == 1
        assert acquisition_schedule.dispatch_due(db, now + timedelta(seconds=180)) == 1
        busy = db.scalar(select(AcquisitionOccurrence).order_by(AcquisitionOccurrence.planned_at.desc()))
        assert busy.status == "SKIPPED" and busy.reason_code == "ACQUISITION_DATASET_BUSY" and busy.acquisition_id is None


def test_frozen_schema_revalidated_in_worker_and_old_versions_preserved(authenticated, database, remote):
    _, first, _ = configured(authenticated, remote)
    with database() as db:
        revision = db.scalar(select(AcquisitionSourceRevision))
        revision.settings = {**revision.settings, "_source": {**revision.settings["_source"], "source_native_schema": []}}
        acquisition_schedule.dispatch_due(db, utcnow() + timedelta(seconds=31))
        run_id = db.scalar(select(AcquisitionOccurrence)).acquisition_id
        db.commit()
    result = completed(authenticated, {"id": run_id})
    assert result["status"] == "FAILED" and result["error_code"] == "SOURCE_SCHEMA_DRIFT"
    with database() as db:
        assert db.scalar(select(func.count()).select_from(DatasetVersion)) == 1
        assert db.get(DatasetVersion, first["output_version_id"])


@pytest.mark.parametrize("failure,code", [("CREDENTIAL", "ACQUISITION_CREDENTIAL_UNAVAILABLE"),
    ("PERMISSION", "ACQUISITION_PERMISSION_REVOKED"), ("CONNECTION", "CONNECTION_DISABLED")])
def test_worker_revalidates_access_after_metadata_registration(authenticated, database, remote, monkeypatch, failure, code):
    source, first, _ = configured(authenticated, remote)
    with database() as db:
        acquisition_schedule.dispatch_due(db, utcnow() + timedelta(seconds=31))
        run_id = db.scalar(select(AcquisitionOccurrence)).acquisition_id
        if failure == "PERMISSION":
            db.get(User, "test-user").role = "Auditor"
        elif failure == "CONNECTION":
            db.get(ExternalConnection, source["id"]).enabled = False
        db.commit()
    if failure == "CREDENTIAL":
        def inaccessible(*args):
            raise SecretStoreError("private-driver-path-that-must-not-escape")
        monkeypatch.setattr("trackvance.connections_service.secret_store.get", inaccessible)
    remote.calls.clear()
    result = completed(authenticated, {"id": run_id})
    assert result["status"] == "FAILED" and result["error_code"] == code
    assert "private-driver" not in json.dumps(result)
    assert remote.calls == []
    with database() as db:
        assert db.scalar(select(func.count()).select_from(DatasetVersion)) == 1
        assert db.get(DatasetVersion, first["output_version_id"])


def test_pause_is_available_without_secret_or_remote_source(authenticated, database, remote, monkeypatch):
    source, first, state = configured(authenticated, remote)
    with database() as db:
        db.get(ExternalConnection, source["id"]).enabled = False
        db.commit()
    def unavailable(*args):
        raise AssertionError("Pause must not need source credentials")
    monkeypatch.setattr("trackvance.connections_service.secret_store.get", unavailable)
    response = authenticated.put(f"/api/v1/datasets/{first['dataset_id']}/source-settings",
        json={**state["settings"], "expected_version": state["version"], "enabled": False})
    assert response.status_code == 200, response.text
    assert response.json()["enabled"] is False and response.json()["next_run_at"] is None


def test_repeated_blocked_cuts_keep_history_and_suppress_duplicate_notice(authenticated, database, remote):
    source, _, _ = configured(authenticated, remote)
    with database() as db:
        db.get(ExternalConnection, source["id"]).enabled = False
        now = utcnow() + timedelta(seconds=31)
        acquisition_schedule.dispatch_due(db, now)
        acquisition_schedule.dispatch_due(db, now + timedelta(seconds=30))
        db.commit()
        rows = db.scalars(select(AcquisitionOccurrence).order_by(AcquisitionOccurrence.planned_at)).all()
        assert len(rows) == 2 and all(item.status == "BLOCKED" and item.acquisition_id is None for item in rows)
        first = db.scalar(select(OutboxEvent).where(OutboxEvent.aggregate_id == rows[0].id))
        second = db.scalar(select(OutboxEvent).where(OutboxEvent.aggregate_id == rows[1].id))
        assert first.payload["suppress_notification"] is False
        assert second.payload["suppress_notification"] is True and second.payload["related_occurrence_id"] == rows[0].id


@pytest.mark.parametrize("failure", ["OCCURRENCE", "AUDIT"])
def test_complete_source_savepoint_isolates_occurrence_and_audit_failure(authenticated, database, remote, monkeypatch, failure):
    source, first, _ = configured(authenticated, remote)
    datasets = [first["dataset_id"]]
    for name in ("Middle invalid source", "Last valid source"):
        result = completed(authenticated, initial(authenticated, source, name=name))
        datasets.append(result["dataset_id"])
        _settings(authenticated, source, result["dataset_id"], enabled=True, interval_seconds=30)
    now = utcnow()
    with database() as db:
        configs = [db.scalar(select(AcquisitionSourceConfiguration).where(AcquisitionSourceConfiguration.dataset_id == identity)) for identity in datasets]
        for index, config in enumerate(configs):
            config.next_run_at = now - timedelta(seconds=3 - index)
        bad_id = configs[1].id
        db.commit()
    def bad_occurrence(session, *_):
        if failure == "OCCURRENCE" and any(isinstance(item, AcquisitionOccurrence) and item.configuration_id == bad_id for item in session.new):
            raise RuntimeError("private-insert-diagnostic")
    real_audit = acquisition_schedule.audit
    def bad_audit(db, action, kind, identity, *args, **kwargs):
        if failure == "AUDIT" and action == "ACQUISITION_SCHEDULE_DISPATCHED" and identity == datasets[1]:
            raise RuntimeError("private-audit-diagnostic")
        return real_audit(db, action, kind, identity, *args, **kwargs)
    monkeypatch.setattr(acquisition_schedule, "audit", bad_audit)
    event.listen(Session, "before_flush", bad_occurrence)
    try:
        with database() as db:
            count = acquisition_schedule.dispatch_due(db, now)
            assert count == 2 and count.successful_count == 2
            db.commit()
            assert {item.configuration_id for item in db.scalars(select(AcquisitionOccurrence))} == {configs[0].id, configs[2].id}
            assert db.scalar(select(func.count()).select_from(Job).where(Job.status == "QUEUED")) == 2
            diagnostic = db.get(AcquisitionSourceConfiguration, bad_id).dispatch_diagnostic
            assert diagnostic["code"] == "ACQUISITION_DISPATCH_TRANSACTION_FAILED"
            assert "private-" not in json.dumps(diagnostic)
        operation = authenticated.get(f"/api/v1/operations?grouped=true&process=acquisition_schedule:{bad_id}")
        assert operation.status_code == 200, operation.text
        assert operation.json()["items"][0]["functional_result"] == "NOT_DISPATCHED"
    finally:
        event.remove(Session, "before_flush", bad_occurrence)


def test_missing_revision_retains_one_visible_incident_and_never_invents_an_occurrence(authenticated, database, remote):
    _, first, _ = configured(authenticated, remote)
    with database() as db:
        config = db.scalar(select(AcquisitionSourceConfiguration))
        config.version = 99
        now = utcnow() + timedelta(seconds=31)
        assert acquisition_schedule.dispatch_due(db, now) == 0
        original_diagnostic = dict(config.dispatch_diagnostic)
        assert acquisition_schedule.dispatch_due(db, now + timedelta(seconds=5)) == 0
        assert config.dispatch_diagnostic == original_diagnostic
        assert db.scalar(select(func.count()).select_from(AuditEvent).where(AuditEvent.event_type == "ACQUISITION_SCHEDULE_METADATA_BLOCKED")) == 1
        assert db.scalar(select(func.count()).select_from(AcquisitionOccurrence)) == 0
        assert db.scalar(select(func.count()).select_from(Job).where(Job.status == "QUEUED")) == 0
        db.commit()
        config_id = config.id
    state = authenticated.get(f"/api/v1/datasets/{first['dataset_id']}/source-settings")
    assert state.status_code == 200 and state.json()["settings"] is None
    assert state.json()["health"]["state"] == "BLOCKED" and state.json()["dispatch_diagnostic"]["code"] == "SOURCE_REVISION_MISSING"
    operation = authenticated.get(f"/api/v1/operations?grouped=true&process=acquisition_schedule:{config_id}")
    assert operation.status_code == 200 and operation.json()["items"][0]["functional_result"] == "NOT_DISPATCHED"


def test_module_failure_keeps_independent_commits_and_real_health_evidence(database, tmp_path, monkeypatch):
    from trackvance.artifactstore import artifact_store
    monkeypatch.setattr(component_health, "STORAGE_DIR", tmp_path)
    monkeypatch.setattr(artifact_store, "root", tmp_path)
    seen = []
    def broken(db):
        raise RuntimeError("secret-do-not-report")
    def healthy(name):
        def dispatch(db):
            seen.append(name)
            db.add(User(id=f"independent-{name}", name=name, email=f"{name}@example.test", password_hash="fixture"))
            return 1
        return dispatch
    monkeypatch.setattr(scheduler, "dispatch_due", broken)
    monkeypatch.setattr("trackvance.intake_automation.dispatch_due", healthy("intake"))
    monkeypatch.setattr("trackvance.sentinel_auto.dispatch_daily", healthy("sentinel-auto"))
    monkeypatch.setattr(acquisition_schedule, "dispatch_due", healthy("acquisition"))
    monkeypatch.setattr("trackvance.document_service.dispatch_due", healthy("documents"))
    monkeypatch.setattr(dispatcher, "dispatch_due", lambda db, **kwargs: seen.append("delivery") or 0)
    dispatcher.write_heartbeat("scheduler", "test-owner")
    assert component_health.component_details("scheduler")["execution_status"] == "UNKNOWN"
    with pytest.raises(scheduler.SchedulerDispatchError):
        dispatcher.dispatch_tick()
    assert seen == ["intake", "sentinel-auto", "acquisition", "documents", "delivery"]
    with database() as db:
        assert db.scalar(select(func.count()).select_from(User).where(User.id.like("independent-%"))) == 4
    health = component_health.component_details("scheduler")
    assert health["status"] == "DEGRADED" and health["execution_status"] == "DEGRADED"
    assert health["consecutive_failures"] == 1 and health["last_successful_dispatch_at"] is None
    assert "secret-do-not-report" not in json.dumps(health)
    dispatcher.write_dispatch_evidence(count=1, errors=[])
    successful_evidence = component_health.component_details("scheduler")
    dispatcher.write_dispatch_evidence(count=0, errors=[])
    healthy_evidence = component_health.component_details("scheduler")
    assert healthy_evidence["execution_status"] == "HEALTHY" and healthy_evidence["consecutive_failures"] == 0
    assert healthy_evidence["last_successful_cycle_at"] and healthy_evidence["last_successful_dispatch_at"] == successful_evidence["last_successful_dispatch_at"]
