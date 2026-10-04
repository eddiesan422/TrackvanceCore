"""Personal reading changes are idempotent and never create execution events."""

import pytest
from sqlalchemy import func, select, update

from trackvance import events
from trackvance.acquisition_models import AcquisitionRun
from trackvance.automation_models import EventConsumption, InternalNotification, OutboxEvent
from trackvance.db import utcnow
from trackvance.models import (
    AuditEvent,
    NotificationDeliveryRecord,
    Role,
    RolePermission,
    Run,
    User,
)


@pytest.fixture
def notification_id(database, queued_intake):
    with database() as db:
        run = db.get(Run, queued_intake["run_id"])
        run.initiated_by_type, run.initiated_by_id = "USER", "test-user"
        run.status, run.decision = "SUCCESS", "APPROVED"
        db.commit()
    assert events.consume_once("NOTIFICATIONS")
    with database() as db:
        return db.scalar(select(InternalNotification.id))


def business_counts(db):
    return {model.__tablename__: db.scalar(select(func.count()).select_from(model))
            for model in (Run, OutboxEvent, EventConsumption, InternalNotification,
                          NotificationDeliveryRecord, AuditEvent)}


def test_unread_roundtrip_repeat_counts_filters_and_persistence(database, authenticated, notification_id):
    path = f"/api/v1/notifications/inbox/{notification_id}"
    with database() as db:
        before = business_counts(db)
    assert authenticated.get("/api/v1/notifications/unread-count").json() == {"unread_count": 1}
    read = authenticated.post(f"{path}/read", json={})
    assert read.status_code == 200 and read.json()["read_at"]
    original_read_at = read.json()["read_at"]
    assert authenticated.post(f"{path}/read", json={}).json()["read_at"] == original_read_at
    assert authenticated.get("/api/v1/notifications/unread-count").json() == {"unread_count": 0}

    assert authenticated.get("/api/v1/notifications/inbox?read_state=READ").json()["total"] == 1
    unread = authenticated.post(f"{path}/unread", json={})
    repeated = authenticated.post(f"{path}/unread", json={})
    assert unread.status_code == repeated.status_code == 200
    assert unread.json() == repeated.json()
    assert unread.json()["read_at"] is None
    assert authenticated.get("/api/v1/notifications/unread-count").json() == {"unread_count": 1}
    assert authenticated.get("/api/v1/notifications/inbox?read_state=READ").json()["total"] == 0
    assert authenticated.get("/api/v1/notifications/inbox?read_state=UNREAD").json()["items"][0]["id"] == notification_id
    # A new database session observes durable state, independently of response/cache.
    with database() as db:
        assert db.get(InternalNotification, notification_id).read_at is None
        assert business_counts(db) == before
    assert authenticated.post(f"{path}/read", json={}).json()["read_at"]
    assert authenticated.get("/api/v1/notifications/unread-count").json() == {"unread_count": 0}


def test_unread_explicit_setter_overwrites_a_concurrent_read(database, notification_id):
    from trackvance.notifications_api import unread_notification

    with database() as first:
        user = first.get(User, "test-user")
        cached = first.get(InternalNotification, notification_id)
        assert cached.read_at is None
        first.commit()
        with database() as second:
            before = business_counts(second)
            second.execute(update(InternalNotification).where(
                InternalNotification.id == notification_id).values(read_at=utcnow()))
            second.commit()
            assert second.get(InternalNotification, notification_id).read_at is not None
        # SessionLocal intentionally does not expire entities on commit. The
        # setter must issue SQL even when its cached reading state is null.
        assert cached.read_at is None
        result = unread_notification(notification_id, first, user)
        assert result["read_at"] is None
    with database() as observer:
        assert observer.get(InternalNotification, notification_id).read_at is None
        assert business_counts(observer) == before


def test_read_explicit_setter_overwrites_a_concurrent_unread(database, notification_id):
    from trackvance.notifications_api import read_notification

    with database() as first:
        user = first.get(User, "test-user")
        cached = first.get(InternalNotification, notification_id)
        cached.read_at = utcnow()
        first.commit()
        with database() as second:
            before = business_counts(second)
            second.execute(update(InternalNotification).where(
                InternalNotification.id == notification_id).values(read_at=None))
            second.commit()
            assert second.get(InternalNotification, notification_id).read_at is None
        assert cached.read_at is not None
        result = read_notification(notification_id, first, user)
        assert result["read_at"] is not None
        assert read_notification(notification_id, first, user)["read_at"] == result["read_at"]
    with database() as observer:
        assert observer.get(InternalNotification, notification_id).read_at is not None
        assert business_counts(observer) == before


def test_unread_administrator_cannot_change_other_recipient_or_organization(database, authenticated, notification_id):
    with database() as db:
        own = db.get(InternalNotification, notification_id)
        identifiers = []
        for identifier, organization in (("another-recipient", own.organization_id), ("foreign-recipient", "foreign-org")):
            user = User(id=identifier, organization_id=organization, name=identifier,
                        email=f"{identifier}@example.test", password_hash="unused", role="Administrator")
            db.add(user)
            db.flush()
            item = InternalNotification(organization_id=organization, event_id=own.event_id,
                recipient_user_id=user.id, module=own.module, origin=own.origin, status=own.status,
                decision=own.decision, description=own.description, resource_type=own.resource_type,
                resource_id=own.resource_id, detail_url=own.detail_url, read_at=utcnow())
            db.add(item)
            db.flush()
            identifiers.append(item.id)
        db.commit()
    for identifier in [*identifiers, "missing-notification"]:
        response = authenticated.post(f"/api/v1/notifications/inbox/{identifier}/unread", json={})
        assert response.status_code == 404 and response.json()["error"]["code"] == "NOT_FOUND"
    with database() as db:
        assert all(db.get(InternalNotification, identifier).read_at is not None for identifier in identifiers)


@pytest.mark.parametrize("module_permission", [True, False])
def test_unread_requires_current_inbox_and_resource_permissions(database, authenticated, notification_id, module_permission):
    authenticated.post(f"/api/v1/notifications/inbox/{notification_id}/read", json={})
    with database() as db:
        role = Role(name="Scoped reader", normalized_name="scoped reader", active=True)
        db.add(role)
        db.flush()
        # One case can read the linked module but has no inbox grant; the other
        # retains only the inbox grant after the linked module grant is revoked.
        db.add(RolePermission(role_id=role.id, permission_code="intake:read" if module_permission else "notifications:read"))
        db.get(User, "test-user").role_id = role.id
        db.commit()
    response = authenticated.post(f"/api/v1/notifications/inbox/{notification_id}/unread", json={})
    assert response.status_code == (403 if module_permission else 404)
    with database() as db:
        assert db.get(InternalNotification, notification_id).read_at is not None


def test_unread_requires_csrf_and_does_not_change_state_on_rejection(database, authenticated, notification_id):
    authenticated.post(f"/api/v1/notifications/inbox/{notification_id}/read", json={})
    token = authenticated.headers.pop("X-CSRF-Token")
    response = authenticated.post(f"/api/v1/notifications/inbox/{notification_id}/unread", json={})
    assert response.status_code == 403
    authenticated.headers["X-CSRF-Token"] = token
    with database() as db:
        assert db.get(InternalNotification, notification_id).read_at is not None


def test_acquisition_notification_retains_public_diagnostic_and_historical_null(database, authenticated):
    received = authenticated.post('/api/v1/datasets/uploads/stage?filename=diagnostic.csv', content=b'id\n001\n').json()
    dataset = authenticated.post('/api/v1/datasets', json={'name': 'Diagnostic acquisition'}).json()
    acquisition = authenticated.post(f'/api/v1/datasets/{dataset["id"]}/acquisitions',
                                    json={'upload_id': received['upload']['id']}).json()
    message = 'La fuente supera el límite efectivo de 100,000 registros de datos. No se publicó una versión parcial.'
    diagnostic = {'code': 'ACQUISITION_ROW_LIMIT', 'message': message,
                  'details': {'limit': 'data_rows', 'maximum': 100000, 'observed': 100001},
                  'reference': acquisition['id']}
    with database() as db:
        run = db.get(AcquisitionRun, acquisition['id'])
        run.status, run.stage = 'FAILED', 'FAILED'
        run.error_code, run.error_message = diagnostic['code'], message
        run.error_details, run.error_reference = diagnostic['details'], diagnostic['reference']
        db.commit()
    assert events.consume_once('NOTIFICATIONS')
    response = authenticated.get('/api/v1/notifications/inbox?module=acquisition')
    assert response.status_code == 200
    item = response.json()['items'][0]
    assert item['description'] == message and item['error'] == diagnostic
    path = f'/api/v1/notifications/inbox/{item["id"]}'
    assert authenticated.post(f'{path}/read', json={}).json()['error'] == diagnostic
    assert authenticated.post(f'{path}/unread', json={}).json()['error'] == diagnostic
    # Emulate the stored payload of a notification predating structured fields.
    with database() as db:
        notification = db.get(InternalNotification, item['id'])
        event = db.get(OutboxEvent, notification.event_id)
        event.payload = {key: value for key, value in event.payload.items() if not key.startswith('error_')}
        db.commit()
    historical = authenticated.get('/api/v1/notifications/inbox?module=acquisition').json()['items'][0]
    assert historical['description'] == message and historical['error'] is None
