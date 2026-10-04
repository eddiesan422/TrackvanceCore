"""Edits retain persisted UTC anchors and live cursors without scheduling work."""

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import func, select
from test_automation_events import (  # noqa: F401 -- shared pytest fixtures
    automation_case,
    delivery_case,
    delivery_runtime,
)

from trackvance import automation
from trackvance.automation import AutomationSettings, next_slot, revision_for, save_automation
from trackvance.automation_models import (
    DeliveryAutomation,
    DeliveryAutomationVersion,
    DeliveryOccurrence,
)
from trackvance.models import Configuration, Job, User


def stored_automation(database, case, monkeypatch, *, mode="INTERVAL", anchor=None, timezone="America/Bogota"):
    anchor = anchor or datetime(2030, 1, 1, 15, 30, 17, 123456, tzinfo=UTC)
    monkeypatch.setattr(automation, "utcnow", lambda: anchor)
    settings = AutomationSettings(mode=mode, starts_at=anchor, timezone=timezone, interval_seconds=600)
    with database() as db:
        item = save_automation(db, db.get(User, "test-user"), case["config_id"], "Original", settings)
        now = anchor + timedelta(days=3)
        item.next_run_at = next_slot(settings, now) if mode != "ONCE" else None
        db.commit()
        identifier = item.id
    monkeypatch.setattr(automation, "utcnow", lambda: now)
    return identifier, anchor, now


def revision_body(detail, **changes):
    return {"configuration_id": detail["configuration_id"], "name": "Edited name",
            "enabled": detail["enabled"], "responsible_user_id": detail["responsible_user_id"],
            "settings": detail["settings"], "expected_version": detail["version"], **changes}


@pytest.mark.parametrize("mode", ["ONCE", "INTERVAL", "DAILY", "WEEKLY"])
def test_http_edit_preserves_historical_anchor_exact_cursor_and_reopen(database, authenticated, automation_case, monkeypatch, mode):  # noqa: F811 -- pytest injects the imported fixture by name
    identifier, anchor, _ = stored_automation(database, automation_case, monkeypatch, mode=mode)
    path = f"/api/v1/delivery/automations/{identifier}"
    original = authenticated.get(path).json()
    response = authenticated.post(f"{path}/versions", json=revision_body(original))
    assert response.status_code == 201, response.text
    updated = response.json()
    assert updated["settings"] == original["settings"]
    assert updated["settings"]["starts_at"] == original["settings"]["starts_at"]
    assert updated["next_run_at"] == original["next_run_at"]
    assert datetime.fromisoformat(updated["settings"]["starts_at"]) == anchor
    assert authenticated.get(path).json()["settings"] == updated["settings"]
    with database() as db:
        assert db.get(DeliveryAutomation, identifier).version == 2
        assert db.scalar(select(func.count()).select_from(DeliveryAutomationVersion)) == 2
        assert db.scalar(select(func.count()).select_from(DeliveryOccurrence)) == 0
        assert db.scalar(select(func.count()).select_from(Job)) == 0


def test_business_configuration_responsible_and_policy_edits_keep_cursor(database, authenticated, automation_case, monkeypatch):  # noqa: F811 -- pytest injects the imported fixture by name
    identifier, _, _ = stored_automation(database, automation_case, monkeypatch)
    path = f"/api/v1/delivery/automations/{identifier}"
    original = authenticated.get(path).json()
    with database() as db:
        actor = db.get(User, "test-user")
        responsible = User(name="Other administrator", email="other@example.test", password_hash="unused",
                           organization_id=actor.organization_id, role=actor.role, role_id=actor.role_id)
        old = db.get(Configuration, automation_case["config_id"])
        config = Configuration(name="Another published configuration", module="DELIVERY", status="PUBLISHED",
                               organization_id=old.organization_id, dataset_id=old.dataset_id, config=old.config)
        db.add_all([responsible, config])
        db.commit()
        responsible_id, configuration_id = responsible.id, config.id
    settings = {**original["settings"], "repeat_versions": True, "allow_empty": True}
    response = authenticated.post(f"{path}/versions", json=revision_body(original, settings=settings,
                                  configuration_id=configuration_id, responsible_user_id=responsible_id))
    assert response.status_code == 201, response.text
    updated = response.json()
    assert updated["configuration_id"] == configuration_id
    assert updated["responsible_user_id"] == responsible_id
    assert updated["next_run_at"] == original["next_run_at"]
    assert updated["settings"]["starts_at"] == original["settings"]["starts_at"]
    with database() as db:
        previous = db.scalar(select(DeliveryAutomationVersion).where(DeliveryAutomationVersion.version == 1))
        assert previous.configuration_id == automation_case["config_id"]
        assert previous.responsible_user_id == "test-user"


def test_new_past_anchor_rejected_for_creation_or_revision_and_stale_version_checked(database, authenticated, automation_case, monkeypatch):  # noqa: F811 -- pytest injects the imported fixture by name
    identifier, anchor, _ = stored_automation(database, automation_case, monkeypatch)
    path = f"/api/v1/delivery/automations/{identifier}"
    original = authenticated.get(path).json()
    settings = {**original["settings"], "starts_at": (anchor - timedelta(days=1)).isoformat()}
    for route in ("/api/v1/delivery/automations", f"{path}/versions"):
        response = authenticated.post(route, json=revision_body(original, settings=settings))
        assert response.status_code == 422 and response.json()["error"]["code"] == "SCHEDULE_IN_PAST"
    assert authenticated.get(path).json()["version"] == 1
    assert authenticated.post(f"{path}/versions", json=revision_body(original)).status_code == 201
    stale = authenticated.post(f"{path}/versions", json=revision_body(original, settings=settings))
    assert stale.status_code == 409 and stale.json()["error"]["code"] == "VERSION_CONFLICT"


def test_calendar_change_retains_existing_anchor_but_resolves_strictly_future_interval(database, authenticated, automation_case, monkeypatch):  # noqa: F811 -- pytest injects the imported fixture by name
    identifier, anchor, now = stored_automation(database, automation_case, monkeypatch)
    path = f"/api/v1/delivery/automations/{identifier}"
    original = authenticated.get(path).json()
    settings = {**original["settings"], "interval_seconds": 1200}
    response = authenticated.post(f"{path}/versions", json=revision_body(original, settings=settings))
    assert response.status_code == 201, response.text
    updated = response.json()
    next_at = datetime.fromisoformat(updated["next_run_at"])
    assert next_at > now and next_at != anchor
    assert next_at == next_slot(AutomationSettings.model_validate(settings), now)
    assert updated["settings"]["starts_at"] == original["settings"]["starts_at"]


@pytest.mark.parametrize(("anchor", "now", "expected"), [
    (datetime(2026, 3, 1, tzinfo=UTC), datetime(2026, 3, 8, tzinfo=UTC), datetime(2026, 3, 9, 6, 30, tzinfo=UTC)),
    (datetime(2026, 10, 1, tzinfo=UTC), datetime(2026, 11, 1, tzinfo=UTC), datetime(2026, 11, 1, 5, 30, tzinfo=UTC)),
])
def test_changed_calendar_retains_dst_gap_and_first_fold_policy(database, authenticated, automation_case, monkeypatch, anchor, now, expected):  # noqa: F811 -- pytest injects the imported fixture by name
    identifier, _, _ = stored_automation(database, automation_case, monkeypatch, mode="DAILY", anchor=anchor)
    monkeypatch.setattr(automation, "utcnow", lambda: now)
    path = f"/api/v1/delivery/automations/{identifier}"
    original = authenticated.get(path).json()
    local_time = "02:30" if now.month == 3 else "01:30"
    settings = {**original["settings"], "timezone": "America/New_York", "local_time": local_time}
    response = authenticated.post(f"{path}/versions", json=revision_body(original, settings=settings))
    assert response.status_code == 201, response.text
    assert datetime.fromisoformat(response.json()["next_run_at"]) == expected
    with database() as db:
        revision = revision_for(db, db.get(DeliveryAutomation, identifier))
        assert datetime.fromisoformat(revision.settings["starts_at"]) == anchor
        assert db.scalar(select(func.count()).select_from(DeliveryOccurrence)) == 0
