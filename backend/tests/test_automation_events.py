"""Durable automation, rollback, resource privacy and independent consumer recovery."""

from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError
from sqlalchemy import func, select
from test_delivery_service_api import delivery_case, delivery_runtime  # noqa: F401

from trackvance import events
from trackvance.automation import (
    AutomationError,
    AutomationSettings,
    authorize_automated_run,
    claim_delivery_target,
    dispatch_due,
    dispatch_occurrence,
    next_slot,
    release_unknown_target,
    revision_for,
    save_automation,
)
from trackvance.automation_models import (
    DeliveryAutomationVersion,
    DeliveryInputClaim,
    DeliveryOccurrence,
    DeliveryTargetGuard,
    EventConsumption,
    InternalNotification,
    OutboxEvent,
)
from trackvance.db import utcnow
from trackvance.delivery_schemas import DeliveryDraft
from trackvance.models import (
    Configuration,
    Dataset,
    DatasetVersion,
    DeliveryAttempt,
    DeliveryOperationalReview,
    Job,
    MonitorScheduleVersion,
    Role,
    RolePermission,
    Run,
    User,
)
from trackvance.notifications_api import visible_query
from trackvance.scheduler import ScheduleError, authorize_scheduled_run, save_schedule
from trackvance.services import create_version, enqueue, execute_run


@pytest.fixture
def automation_case(database, delivery_case):  # noqa: F811 -- pytest injects the imported fixture by its name
    with database() as db:
        config = Configuration(name="Automatic publication", module="DELIVERY",
            dataset_id=delivery_case.source["dataset_id"], config=DeliveryDraft.model_validate(delivery_case.draft).snapshot())
        db.add(config)
        db.commit()
        return {"config_id": config.id, "source_id": delivery_case.source["version_id"],
                "destination_version_id": delivery_case.destination["destination_version_id"],
                "runtime": delivery_case.runtime}


def settings(now=None, **values):
    return AutomationSettings(starts_at=now or utcnow(), **values)


def make_automation(db, case, **values):
    return save_automation(db, db.get(User, "test-user"), case["config_id"], "Publish accepted data", settings(**values))


def test_calendar_obeys_zone_dst_and_rejects_naive_or_unknown_zone():
    daily = settings(datetime(2026, 3, 7, tzinfo=UTC), mode="DAILY", timezone="America/New_York", local_time="02:30")
    assert next_slot(daily, datetime(2026, 3, 8, 0, tzinfo=UTC)) == datetime(2026, 3, 9, 6, 30, tzinfo=UTC)
    weekly = settings(datetime(2026, 10, 1, tzinfo=UTC), mode="WEEKLY", timezone="America/New_York", local_time="01:30", weekdays=[6])
    assert next_slot(weekly, datetime(2026, 11, 1, 0, tzinfo=UTC)) == datetime(2026, 11, 1, 5, 30, tzinfo=UTC)
    with pytest.raises(ValidationError):
        settings(datetime(2026, 1, 1))  # noqa: DTZ001 -- verifies rejection of a naive scheduling instant
    with pytest.raises(ValidationError):
        settings(timezone="Invented/Zone")


def test_cursor_occurrence_input_claim_and_job_commit_together(database, automation_case):
    now = utcnow()
    with database() as db:
        automation = make_automation(db, automation_case, now=now, interval_seconds=60)
        db.commit()
        assert dispatch_due(db, now + timedelta(minutes=10)) == 1
        occurrence = db.scalar(select(DeliveryOccurrence))
        assert occurrence.coalesced_intervals == 10
        assert occurrence.dataset_version_id == automation_case["source_id"]
        assert occurrence.status == "ENQUEUED"
        assert len(automation_case["runtime"].calls) == 0  # Metadata dispatch never calls SQL sink.
        db.rollback()
        assert db.scalar(select(func.count()).select_from(DeliveryOccurrence)) == 0
        assert dispatch_due(db, now + timedelta(minutes=10)) == 1
        db.commit()
        assert dispatch_due(db, now + timedelta(minutes=10)) == 0
        assert db.scalar(select(func.count()).select_from(Job)) == 1
        assert db.scalar(select(func.count()).select_from(DeliveryInputClaim)) == 1
        original = revision_for(db, automation)
        save_automation(db, db.get(User, "test-user"), automation_case["config_id"], "Paused", settings(),
                        automation=automation, expected_version=1, enabled=False)
        db.commit()
        assert original.enabled is True
        assert db.scalar(select(func.count()).select_from(DeliveryAutomationVersion)) == 2


def test_no_repeat_is_separate_from_occurrence_identity_and_deliberate_repeat(database, automation_case):
    with database() as db:
        automation = make_automation(db, automation_case)
        revision = revision_for(db, automation)
        first = dispatch_occurrence(db, automation, revision, "slot:1", utcnow())
        first_run = db.get(Run, first.run_id)
        first_run.status = "SUCCESS"
        db.flush()
        assert dispatch_occurrence(db, automation, revision, "slot:1", utcnow()) is None
        skipped = dispatch_occurrence(db, automation, revision, "slot:2", utcnow())
        assert skipped.run_id is None and skipped.reason_code == "VERSION_ALREADY_PROCESSED"
        repeated = dispatch_occurrence(db, automation, revision, "slot:3", utcnow(), repeat=True)
        assert repeated.run_id != first.run_id
        assert db.get(Run, repeated.run_id).execution_plan["automation"]["repeat_deliberate"] is True
        db.commit()


@pytest.mark.parametrize("failure", ["user_disabled", "permission_revoked", "destination_disabled"])
def test_current_identity_permissions_and_destination_revalidated(database, automation_case, failure):
    from trackvance.models import DeliveryDestination

    with database() as db:
        automation = make_automation(db, automation_case)
        occurrence = dispatch_occurrence(db, automation, revision_for(db, automation), "slot", utcnow())
        run, user = db.get(Run, occurrence.run_id), db.get(User, "test-user")
        if failure == "user_disabled":
            user.active = False
        elif failure == "permission_revoked":
            user.role = "Operations"
        else:
            config = db.get(Configuration, run.config_id)
            db.get(DeliveryDestination, config.config["destination_id"]).enabled = False
        db.flush()
        with pytest.raises(AutomationError):
            authorize_automated_run(db, run)


def test_unknown_blocks_manual_and_auto_until_explicit_conclusive_review(database, automation_case):
    with database() as db:
        automation = make_automation(db, automation_case)
        occurrence = dispatch_occurrence(db, automation, revision_for(db, automation), "first", utcnow())
        run = db.get(Run, occurrence.run_id)
        claim_delivery_target(db, run)
        run.status, run.decision = "UNKNOWN", "UNKNOWN"
        db.flush()
        guard = db.scalar(select(DeliveryTargetGuard))
        assert guard.unknown_run_id == run.id and guard.active_run_id is None
        later = Run(name="Manual follow-up", module="DELIVERY", config_id=run.config_id, dataset_version_id=run.dataset_version_id,
                    initiated_by="Test User")
        db.add(later)
        db.flush()
        with pytest.raises(AutomationError, match="target"):
            claim_delivery_target(db, later)
        blocked = dispatch_occurrence(db, automation, revision_for(db, automation), "second", utcnow(), repeat=True)
        assert blocked.reason_code == "TARGET_UNKNOWN_BLOCKED"
        attempt = DeliveryAttempt(run_id=run.id, destination_version_id=automation_case["destination_version_id"],
            attempt_number=1, idempotency_key="unknown", target_locator="sales.orders", status="UNKNOWN")
        db.add(attempt)
        db.flush()
        review = DeliveryOperationalReview(run_id=run.id, delivery_attempt_id=attempt.id,
            reviewer_id="test-user", reviewer_name="Test User", outcome="INCONCLUSIVE", note="Investigated", verified_at=utcnow())
        db.add(review)
        db.flush()
        with pytest.raises(AutomationError) as error:
            release_unknown_target(db, db.get(User, "test-user"), run.id, review.id, "Explicit resumption")
        assert error.value.code == "REVIEW_INCONCLUSIVE"
        review.outcome = "REMOTE_COMMIT_OBSERVED"
        db.flush()
        release_unknown_target(db, db.get(User, "test-user"), run.id, review.id, "Remote effect reviewed, authorize new deliveries")
        db.flush()
        claim_delivery_target(db, later)
        assert run.status == "UNKNOWN" and attempt.status == "UNKNOWN"
        assert guard.active_run_id == later.id


def test_outbox_rollback_consumer_dedupe_and_failures_are_independent(database, queued_intake, monkeypatch):
    with database() as db:
        run = db.get(Run, queued_intake["run_id"])
        run.initiated_by_type, run.initiated_by_id = "USER", "test-user"
        db.commit()
        run.status, run.decision = "SUCCESS", "REJECTED"
        db.flush()
        assert db.scalar(select(func.count()).select_from(OutboxEvent)) == 1
        db.rollback()
        assert db.scalar(select(func.count()).select_from(OutboxEvent)) == 0
        run.status, run.decision = "SUCCESS", "REJECTED"
        db.commit()
        run_id = run.id
    assert events.consume_once("NOTIFICATIONS")
    with database() as db:
        notification = db.scalar(select(InternalNotification))
        assert notification.status == "SUCCESS" and notification.decision == "REJECTED"
        assert "rechazado" in notification.description
        consumed = db.scalar(select(EventConsumption).where(EventConsumption.consumer == "NOTIFICATIONS"))
        consumed.status = "PENDING"
        db.commit()
    assert events.consume_once("NOTIFICATIONS")
    with database() as db:
        assert db.scalar(select(func.count()).select_from(InternalNotification)) == 1
    def failure(*_):
        raise RuntimeError("driver password=do-not-persist")
    monkeypatch.setattr("trackvance.automation.consume_intake_event", failure)
    assert events.consume_once("CHAINING")
    with database() as db:
        delivery = db.scalar(select(EventConsumption).where(EventConsumption.consumer == "CHAINING"))
        assert delivery.status == "PENDING" and delivery.error_code == "EVENT_CONSUMER_FAILED"
        assert "password" not in str(delivery.__dict__)
        assert db.get(Run, run_id).status == "SUCCESS"
        assert db.scalar(select(EventConsumption).where(EventConsumption.consumer == "NOTIFICATIONS")).status == "DONE"


def test_inbox_filters_owner_org_permissions_and_revocation(database, tmp_path):
    from trackvance.config import ORG_ID
    from trackvance.models import Role

    with database() as db:
        user = db.get(User, "test-user")
        other = User(id="other", name="Other", email="other@example.test", role="Administrator", password_hash="unused")
        foreign = User(id="foreign", organization_id="other-org", name="Foreign", email="foreign@example.test", password_hash="unused")
        db.add_all([other, foreign])
        db.commit()
        for owner, org in [(user.id, ORG_ID), (other.id, ORG_ID), (foreign.id, "other-org")]:
            dataset = Dataset(organization_id=org, name=f"Owned input {owner}")
            db.add(dataset)
            db.flush()
            path = tmp_path / f"{owner}.csv"
            path.write_text("id\n1\n", encoding="utf-8")
            version = create_version(db, dataset, path, path.name)
            config = Configuration(organization_id=org, name="Owned validation", module="intake", dataset_id=dataset.id, config={})
            db.add(config)
            db.flush()
            run = Run(name="Validation", organization_id=org, module="intake", config_id=config.id,
                dataset_version_id=version.id, initiated_by="User", initiated_by_type="USER", initiated_by_id=owner)
            db.add(run)
            db.flush()
            run.status, run.decision = "SUCCESS", "APPROVED"
            db.flush()
        db.commit()
    while events.consume_once("NOTIFICATIONS"):
        pass
    with database() as db:
        user = db.get(User, "test-user")
        assert len(db.scalars(visible_query(db, user)).all()) == 1
        assert len(db.scalars(visible_query(db, db.get(User, "other"))).all()) == 1
        role = Role(name="Inbox only", normalized_name="inbox only", active=True)
        db.add(role)
        db.flush()
        db.add(RolePermission(role_id=role.id, permission_code="notifications:read"))
        user.role_id = role.id
        db.commit()
        assert db.scalars(visible_query(db, user)).all() == []


def test_inbox_read_filter_persists_and_revalidates_module_permission(
    database, authenticated, queued_intake
):
    from trackvance.models import Role

    with database() as db:
        run = db.get(Run, queued_intake['run_id'])
        run.initiated_by_type, run.initiated_by_id = 'USER', 'test-user'
        run.status, run.decision = 'SUCCESS', 'REJECTED'
        db.commit()
    assert events.consume_once('NOTIFICATIONS')
    first = authenticated.get('/api/v1/notifications/inbox').json()['items'][0]['id']
    marked = authenticated.post(f'/api/v1/notifications/inbox/{first}/read', json={})
    assert marked.status_code == 200 and marked.json()['read_at']
    with database() as db:
        initial = db.get(Run, queued_intake['run_id'])
        another = Run(name='Another validation', organization_id=initial.organization_id,
            module=initial.module, config_id=initial.config_id, dataset_version_id=initial.dataset_version_id,
            initiated_by='Test User', initiated_by_type='USER', initiated_by_id='test-user')
        db.add(another)
        db.flush()
        another.status, another.decision = 'SUCCESS', 'APPROVED'
        db.commit()
    assert events.consume_once('NOTIFICATIONS')
    read = authenticated.get('/api/v1/notifications/inbox?read_state=READ').json()
    unread = authenticated.get('/api/v1/notifications/inbox?read_state=UNREAD').json()
    assert read['total'] == unread['total'] == 1
    assert read['items'][0]['id'] == first and read['items'][0]['read_at']
    assert unread['items'][0]['id'] != first and unread['items'][0]['read_at'] is None
    assert authenticated.get('/api/v1/notifications/inbox?read_state=ALL').json()['total'] == 2
    assert authenticated.get('/api/v1/notifications/inbox?unread=true').json()['total'] == 1
    assert authenticated.get('/api/v1/notifications/inbox?read_state=UNKNOWN').status_code == 422
    with database() as db:
        user = db.get(User, 'test-user')
        role = Role(name='Personal inbox without module', normalized_name='personal inbox without module', active=True)
        db.add(role)
        db.flush()
        db.add(RolePermission(role_id=role.id, permission_code='notifications:read'))
        user.role_id = role.id
        db.commit()
    for state in ['READ', 'UNREAD', 'ALL']:
        response = authenticated.get(f'/api/v1/notifications/inbox?read_state={state}')
        assert response.status_code == 200 and response.json()['total'] == 0
    assert authenticated.get('/api/v1/notifications/unread-count').json()['unread_count'] == 0
    assert authenticated.post(f'/api/v1/notifications/inbox/{first}/read', json={}).status_code == 404


def test_chain_pins_exact_published_intake_output(database, automation_case):
    with database() as db:
        source = db.get(DatasetVersion, automation_case["source_id"])
        intake = Configuration(name="Intake accepted", module="intake", dataset_id=source.dataset_id,
            config={"required_columns": ["tenant_id"]})
        db.add(intake)
        db.flush()
        automation = make_automation(db, automation_case, now=utcnow() - timedelta(seconds=1), mode="CHAINED",
            source_policy="INTAKE_OUTPUT", intake_configuration_id=intake.id)
        db.commit()
        run = enqueue(db, intake, source, None, "Test User")
        db.commit()
        execute_run(db, run)
        db.commit()
        assert run.status == "SUCCESS" and run.decision == "APPROVED"
        assert run.output_version_id and run.output_version_id != source.id
        output_id, run_id, automation_id = run.output_version_id, run.id, automation.id
    while events.consume_once("CHAINING"):
        pass
    with database() as db:
        occurrence = db.scalar(select(DeliveryOccurrence).where(DeliveryOccurrence.automation_id == automation_id))
        assert occurrence.source_run_id == run_id
        assert occurrence.dataset_version_id == output_id
        assert db.get(Run, occurrence.run_id).dataset_version_id == output_id
        event = db.scalar(select(OutboxEvent).where(OutboxEvent.aggregate_id == run_id))
        from trackvance.automation import consume_intake_event
        consume_intake_event(db, event)
        assert db.scalar(select(func.count()).select_from(DeliveryOccurrence)) == 1


def test_sentinel_revalidates_executor_on_runtime_and_requires_legacy_assignment(database):
    with database() as db:
        user = db.get(User, "test-user")
        dataset = Dataset(name="Monitored source")
        db.add(dataset)
        db.flush()
        monitor = Configuration(name="Monitor", module="sentinel", dataset_id=dataset.id, config={})
        db.add(monitor)
        db.flush()
        schedule = save_schedule(db, monitor, user, interval_seconds=60, enabled=True,
                                 starts_at=None, expected_version=None)
        revision = db.scalar(select(MonitorScheduleVersion))
        run = Run(organization_id=user.organization_id, module="sentinel", execution_plan={"schedule": {"responsible_user_id": user.id}})
        authorize_scheduled_run(db, run)
        user.active = False
        db.flush()
        with pytest.raises(ScheduleError):
            authorize_scheduled_run(db, run)
        user.active = True
        revision.responsible_user_id = None
        db.flush()
        with pytest.raises(ScheduleError) as error:
            save_schedule(db, monitor, user, interval_seconds=60, enabled=True, starts_at=None,
                          expected_version=schedule.version)
        assert error.value.code == "EXECUTOR_ASSIGNMENT_REQUIRED"
        save_schedule(db, monitor, user, interval_seconds=60, enabled=True, starts_at=None,
                      expected_version=schedule.version, responsible_user_id=user.id)


def test_request_idempotency_conflict_remains_distinct_from_no_repeat(authenticated, automation_case):
    created = authenticated.post('/api/v1/delivery/automations', json={
        'configuration_id': automation_case['config_id'], 'name': 'Versioned automatic publication',
        'settings': settings().model_dump(mode='json')})
    assert created.status_code == 201, created.text
    path = f"/api/v1/delivery/automations/{created.json()['id']}/dispatch"
    body = {'request_key': 'stable-key', 'repeat': False}
    first = authenticated.post(path, json=body)
    assert first.status_code == 202, first.text
    repeated = authenticated.post(path, json=body)
    assert repeated.json()['id'] == first.json()['id']
    conflict = authenticated.post(path, json={**body, 'repeat': True})
    assert conflict.status_code == 409
    assert conflict.json()['error']['code'] == 'IDEMPOTENCY_CONFLICT'


def test_expired_consumer_lease_recovers_and_attempts_stop_at_five(database, queued_intake, monkeypatch):
    with database() as db:
        run = db.get(Run, queued_intake['run_id'])
        run.initiated_by_type, run.initiated_by_id = 'USER', 'test-user'
        run.status, run.decision = 'SUCCESS', 'REJECTED'
        db.commit()
        delivery = db.scalar(select(EventConsumption).where(EventConsumption.consumer == 'NOTIFICATIONS'))
        delivery.status, delivery.lease_owner, delivery.lease_until = 'RUNNING', 'lost-process', utcnow() - timedelta(seconds=1)
        db.commit()
    assert events.consume_once('NOTIFICATIONS')
    with database() as db:
        assert db.scalar(select(func.count()).select_from(InternalNotification)) == 1
        delivery = db.scalar(select(EventConsumption).where(EventConsumption.consumer == 'CHAINING'))
        delivery.attempts = 4
        db.commit()
    def fail(*_):
        raise RuntimeError('sanitized')
    monkeypatch.setattr('trackvance.automation.consume_intake_event', fail)
    assert events.consume_once('CHAINING')
    with database() as db:
        delivery = db.scalar(select(EventConsumption).where(EventConsumption.consumer == 'CHAINING'))
        assert delivery.status == 'DEAD' and delivery.attempts == 5
        assert db.get(Run, queued_intake['run_id']).status == 'SUCCESS'
    assert not events.consume_once('CHAINING')


def test_unverifiable_published_bytes_never_dispatch_a_delivery(database, automation_case):
    from trackvance.artifactstore import storage_provider
    from trackvance.models import Artifact

    with database() as db:
        automation = make_automation(db, automation_case)
        source = db.get(DatasetVersion, automation_case['source_id'])
        artifact = db.get(Artifact, source.canonical_artifact_id)
        path = storage_provider.materialize(artifact)
        path.write_bytes(b'corrupt')
        occurrence = dispatch_occurrence(db, automation, revision_for(db, automation), 'corrupt-output', utcnow())
        assert occurrence.reason_code == 'OUTPUT_INTEGRITY_FAILED' and occurrence.run_id is None
        assert not automation_case['runtime'].calls


def test_target_guard_serializes_queued_manual_and_automated_runs(database, automation_case):
    with database() as db:
        automation = make_automation(db, automation_case)
        occurrence = dispatch_occurrence(db, automation, revision_for(db, automation), 'automated', utcnow())
        automatic = db.get(Run, occurrence.run_id)
        manual = Run(name='Manual target competitor', module='DELIVERY', config_id=automatic.config_id,
                     dataset_version_id=automatic.dataset_version_id, initiated_by='Test User')
        db.add(manual)
        db.commit()
        automatic_id, manual_id = automatic.id, manual.id
        claim_delivery_target(db, automatic)
        db.commit()
    with database() as competing:
        with pytest.raises(AutomationError) as error:
            claim_delivery_target(competing, competing.get(Run, manual_id))
        assert error.value.code == 'TARGET_BUSY'
    with database() as settled:
        run = settled.get(Run, automatic_id)
        run.status = 'FAILED'
        settled.commit()
    with database() as competing:
        guard = claim_delivery_target(competing, competing.get(Run, manual_id))
        assert guard.active_run_id == manual_id


@pytest.mark.parametrize('decision', ['PASS', 'FAIL'])
def test_preflight_notification_has_personal_detail_and_validation_semantics(database, queued_intake, decision):
    with database() as db:
        run = db.get(Run, queued_intake['run_id'])
        run.module, run.initiated_by_type, run.initiated_by_id = 'DELIVERY_PREFLIGHT', 'USER', 'test-user'
        run.status, run.decision = 'SUCCESS', decision
        db.commit()
    assert events.consume_once('NOTIFICATIONS')
    with database() as db:
        notification = db.scalar(select(InternalNotification))
        assert notification.module == 'DELIVERY'
        assert notification.detail_url == f"/delivery/validation/{queued_intake['run_id']}"
        assert 'validación de la entrega' in notification.description.lower()
        assert ('no aprobó' in notification.description) is (decision == 'FAIL')
        assert 'publicó' not in notification.description


def test_outbox_payload_finishes_atomically_across_flushes_and_is_frozen_after_commit(database, queued_intake):
    with database() as db:
        run = db.get(Run, queued_intake['run_id'])
        run.status, run.decision = 'SUCCESS', None
        db.flush()
        event = db.scalar(select(OutboxEvent))
        assert event.payload['output_version_id'] is None
        run.output_version_id, run.decision = queued_intake['version_id'], 'APPROVED'
        run.initiated_by_type, run.initiated_by_id = 'USER', 'test-user'
        db.commit()
        db.refresh(event)
        assert event.payload['output_version_id'] == queued_intake['version_id']
        assert event.payload['recipient_user_id'] == 'test-user'
        assert event.payload['decision'] == 'APPROVED'
        assert db.scalar(select(func.count()).select_from(OutboxEvent)) == 1
        assert db.scalar(select(func.count()).select_from(EventConsumption)) == 2
        original_payload = dict(event.payload)
        run.output_version_id = None
        db.commit()
        db.refresh(event)
        assert event.payload == original_payload


def test_blocked_sentinel_occurrence_notifies_only_its_currently_authorized_owner(database, tmp_path):
    from test_scheduler import make_monitor

    from trackvance.scheduler import dispatch_due as dispatch_sentinel

    now = utcnow()
    with database() as db:
        user, monitor, _ = make_monitor(db, tmp_path)
        role = Role(name='Sentinel executor', normalized_name='sentinel executor', organization_id=user.organization_id)
        db.add(role)
        db.flush()
        user.role_id = role.id
        for code in ['sentinel:read', 'sentinel:execute', 'datasets:read', 'notifications:read']:
            db.add(RolePermission(role_id=role.id, permission_code=code))
        db.flush()
        save_schedule(db, monitor, user, interval_seconds=60, enabled=True, starts_at=now, expected_version=None)
        db.delete(db.scalar(select(RolePermission).where(RolePermission.role_id == user.role_id,
                                                       RolePermission.permission_code == 'sentinel:execute')))
        db.flush()
        dispatch_sentinel(db, now)
        db.commit()
    assert events.consume_once('NOTIFICATIONS')
    with database() as db:
        user = db.get(User, 'test-user')
        notification = db.scalar(visible_query(db, user))
        assert notification.resource_type == 'MONITOR_OCCURRENCE' and notification.status == 'BLOCKED'
        assert notification.recipient_user_id == user.id and notification.detail_url.startswith('/sentinel?monitor=')
        assert 'permisos revocados' in notification.description
