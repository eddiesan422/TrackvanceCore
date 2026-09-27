"""Credentials are disclosed once to an authorized administrator, never persisted."""
import json
import os
import smtplib
from datetime import datetime, timedelta
from pathlib import Path

import pytest
from argon2 import PasswordHasher
from fastapi.testclient import TestClient
from sqlalchemy import select

from trackvance.api import app
from trackvance.db import utcnow
from trackvance.identity_api import UserCredentialIssueResponse
from trackvance.models import AuditEvent, AuthSession, NotificationDeliveryRecord, Role, User


@pytest.fixture(autouse=True)
def smtp_forbidden(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("Credential issuance must never connect to SMTP")

    monkeypatch.setattr(smtplib, "SMTP", forbidden)
    monkeypatch.setattr(smtplib, "SMTP_SSL", forbidden)
    for key, value in {"ENABLED": "true", "HOST": "legacy.invalid", "PORT": "587",
                       "USERNAME": "legacy", "PASSWORD": "old-provider-secret",
                       "FROM_ADDRESS": "legacy@example.test", "SECURITY": "STARTTLS"}.items():
        monkeypatch.setenv("TRACKVANCE_SMTP_" + key, value)


def create_user(client, **changes):
    role = next(row for row in client.get("/api/v1/roles").json()["items"] if row["name"] == "Data Analyst")
    return client.post("/api/v1/users", json={"first_name": "One", "last_name": "Time",
        "username": "one.time", "email": "one.time@example.test", "role_id": role["id"], **changes})


def assert_no_disclosure(values, secrets):
    leaked = any(secret in value for secret in secrets for value in values)
    assert not leaked, "Sensitive credentials appeared outside their issuance response"


def assert_normal_user(user):
    forbidden = {"password", "password_hash", "temporary_password", "temporary_credentials", "credential_delivery"}
    assert not forbidden.intersection(user)


def test_issuance_envelope_expiry_and_no_store(authenticated, database):
    before = utcnow()
    created = create_user(authenticated)
    after = utcnow()
    assert created.status_code == 201
    body = created.json()
    assert set(body) == {"user", "temporary_credentials"}
    credentials = body["temporary_credentials"]
    assert set(credentials) == {"username", "temporary_password", "expires_at", "must_change_password"}
    assert credentials["username"] == body["user"]["username"]
    assert len(credentials["temporary_password"]) == 32
    assert credentials["must_change_password"] is True
    expiry = datetime.fromisoformat(credentials["expires_at"])
    assert before + timedelta(hours=24) <= expiry <= after + timedelta(hours=24)
    assert datetime.fromisoformat(body["user"]["temporary_password_expires_at"]) == expiry
    assert created.headers["cache-control"] == "no-store"
    assert created.headers["pragma"] == "no-cache"
    assert_normal_user(body["user"])
    dto = UserCredentialIssueResponse.model_validate(body)
    assert_no_disclosure([repr(dto), str(dto), json.dumps(body["user"])], [credentials["temporary_password"]])
    with database() as db:
        user = db.get(User, body["user"]["id"])
        assert user.password_hash.startswith("$argon2")
        assert PasswordHasher().verify(user.password_hash, credentials["temporary_password"])
        assert db.scalar(select(NotificationDeliveryRecord)) is None


@pytest.mark.parametrize("endpoint", ["regenerate-credentials", "resend-credentials", "reset-password"])
def test_regeneration_cas_aliases_and_all_session_revocation(authenticated, database, endpoint):
    issued = create_user(authenticated).json()
    user, initial = issued["user"], issued["temporary_credentials"]["temporary_password"]
    with TestClient(app) as account, TestClient(app) as second:
        for client in (account, second):
            logged = client.post("/api/v1/auth/login", json={"username": user["username"], "password": initial})
            assert logged.status_code == 200
            client.headers["X-CSRF-Token"] = logged.json()["csrf_token"]
        changed = account.post("/api/v1/auth/first-login/change-password", json={"new_password": "Defined password for rotation 2026!"})
        assert changed.status_code == 200
        user = changed.json()["user"]
        defined = "Defined password for rotation 2026!"
        assert second.post("/api/v1/auth/login", json={"username": user["username"], "password": defined}).status_code == 200
        regenerated = authenticated.post(f"/api/v1/users/{user['id']}/{endpoint}", json={"version": user["version"]})
        assert regenerated.status_code == 200
        body = regenerated.json()
        current = body["temporary_credentials"]["temporary_password"]
        assert len({initial, defined, current}) == 3
        assert body["user"]["version"] == user["version"] + 1
        assert body["user"]["must_change_password"] is True
        assert regenerated.headers["cache-control"] == "no-store" and regenerated.headers["pragma"] == "no-cache"
        for client in (account, second):
            assert client.get("/api/v1/me").status_code == 401
        for password in (initial, defined):
            assert account.post("/api/v1/auth/login", json={"username": user["username"], "password": password}).status_code == 401
        stale = authenticated.post(f"/api/v1/users/{user['id']}/{endpoint}", json={"version": user["version"]})
        assert stale.status_code == 409
        assert_no_disclosure([stale.text], [initial, defined, current])
        assert "temporary_credentials" not in stale.json()
        with database() as db:
            assert db.scalar(select(AuthSession).where(AuthSession.user_id == user["id"])) is None
            assert PasswordHasher().verify(db.get(User, user["id"]).password_hash, current)
            assert db.scalar(select(NotificationDeliveryRecord)) is None
        logged = account.post("/api/v1/auth/login", json={"username": user["username"], "password": current})
        assert logged.status_code == 200 and logged.json()["user"]["must_change_password"]


def test_get_edit_delete_audit_database_backup_logs_and_storage_never_disclose(authenticated, database, caplog, capsys):
    issued = create_user(authenticated).json()
    user, first = issued["user"], issued["temporary_credentials"]["temporary_password"]
    with database() as db:
        original_hash = db.get(User, user["id"]).password_hash
    regenerated = authenticated.post(f"/api/v1/users/{user['id']}/regenerate-credentials", json={"version": user["version"]}).json()
    secret = regenerated["temporary_credentials"]["temporary_password"]
    values = []
    for path in ("/users", "/users/" + user["id"], "/me", "/roles", "/roles/permissions", "/audit-events",
                 "/notifications/status", "/notifications/deliveries"):
        response = authenticated.get("/api/v1" + path)
        assert response.status_code == 200
        values.append(response.text)
        assert "temporary_credentials" not in response.text and "password_hash" not in response.text
    edited = authenticated.patch(f"/api/v1/users/{user['id']}", json={"version": regenerated["user"]["version"], "first_name": "Edited"})
    assert edited.status_code == 200
    assert_normal_user(edited.json())
    deleted = authenticated.request("DELETE", f"/api/v1/users/{user['id']}", json={"version": edited.json()["version"]})
    assert deleted.status_code == 200
    assert_normal_user(deleted.json())
    values += [edited.text, deleted.text, json.dumps(dict(authenticated.cookies)), caplog.text]
    captured = capsys.readouterr()
    values += [captured.out, captured.err]
    with database() as db:
        assert_no_disclosure(values, [original_hash, db.get(User, user["id"]).password_hash])
        dump = "\n".join(db.connection().connection.driver_connection.iterdump())
        values.append(dump)
        audit_rows = db.scalars(select(AuditEvent)).all()
        values.append(json.dumps([row.metadata_json for row in audit_rows]))
        assert not any(row.event_type.startswith("NOTIFICATION_") for row in audit_rows)
        assert db.scalar(select(NotificationDeliveryRecord)) is None
        assert len(db.scalars(select(User)).all()) == 2
    for path in Path(os.environ["TRACKVANCE_STORAGE_DIR"]).rglob("*"):
        if path.is_file():
            values.append(path.read_bytes().decode("utf-8", errors="replace"))
    assert_no_disclosure(values, [first, secret])


def test_historical_notification_metadata_is_unchanged_and_always_disabled(authenticated, database):
    with database() as db:
        db.add(NotificationDeliveryRecord(id="historical", organization_id=db.get(User, "test-user").organization_id,
            event_type="USER_TEMPORARY_CREDENTIALS", template_key="USER_TEMPORARY_CREDENTIALS",
            recipient_user_id="test-user", recipient_email_snapshot="tester@example.test",
            attempt_number=4, status="FAILED", error_code="NO_PROVIDER", failed_at=utcnow()))
        db.commit()
    before = authenticated.get("/api/v1/notifications/deliveries").json()
    assert before["total"] == 1
    issued = create_user(authenticated).json()
    assert authenticated.post(f"/api/v1/users/{issued['user']['id']}/regenerate-credentials",
        json={"version": issued["user"]["version"]}).status_code == 200
    assert authenticated.get("/api/v1/notifications/deliveries").json() == before
    status = authenticated.get("/api/v1/notifications/status").json()
    assert status == {"enabled": False, "configured": False, "provider": "NONE", "security": "NONE",
                      "from_address": "", "from_name": "", "availability": "HISTORICAL_ONLY"}
    paths = app.openapi()["paths"]
    for path in ("/notifications/status", "/notifications/deliveries"):
        assert paths["/api/v1" + path]["get"]["deprecated"] is True
    for alias in ("resend-credentials", "reset-password"):
        assert paths[f"/api/v1/users/{{user_id}}/{alias}"]["post"]["deprecated"] is True


def test_regeneration_requires_csrf_permission_scope_and_live_user(authenticated, database):
    issued = create_user(authenticated).json()
    user = issued["user"]
    path = f"/api/v1/users/{user['id']}/regenerate-credentials"
    with database() as db:
        initial_hash = db.get(User, user["id"]).password_hash
        db.add(User(id="other-org-user", organization_id="other-org", name="Other", email="other@example.test", password_hash="unusable"))
        db.commit()
    assert authenticated.post(path, json={"version": 1}, headers={"X-CSRF-Token": "bad"}).status_code == 403
    assert authenticated.post("/api/v1/users/other-org-user/regenerate-credentials", json={"version": 1}).status_code == 404
    with TestClient(app) as anonymous:
        assert anonymous.post(path, json={"version": 1}).status_code == 401
    with database() as db:
        analyst = db.scalar(select(Role).where(Role.name == "Data Analyst", Role.organization_id == db.get(User, "test-user").organization_id))
        db.get(User, "test-user").role_id = analyst.id
        db.commit()
    assert authenticated.post(path, json={"version": 1}).status_code == 403
    with database() as db:
        assert db.get(User, user["id"]).password_hash == initial_hash
        admin = db.scalar(select(Role).where(Role.system_key == "ADMINISTRATOR", Role.organization_id == db.get(User, "test-user").organization_id))
        db.get(User, "test-user").role_id = admin.id
        db.commit()
    deleted = authenticated.request("DELETE", f"/api/v1/users/{user['id']}", json={"version": 1})
    assert deleted.status_code == 200
    assert authenticated.post(path, json={"version": deleted.json()["version"]}).status_code == 404


def test_expired_temporary_cannot_change_password_but_regeneration_recovers(authenticated, database):
    issued = create_user(authenticated).json()
    user, password = issued["user"], issued["temporary_credentials"]["temporary_password"]
    with TestClient(app) as account:
        logged = account.post("/api/v1/auth/login", json={"username": user["username"], "password": password})
        assert logged.status_code == 200
        account.headers["X-CSRF-Token"] = logged.json()["csrf_token"]
        with database() as db:
            db.get(User, user["id"]).temporary_password_expires_at = utcnow() - timedelta(seconds=1)
            db.commit()
        changed = account.post("/api/v1/auth/first-login/change-password", json={"new_password": "Permanent but expired 2026!"})
        assert changed.status_code == 401
        regenerated = authenticated.post(f"/api/v1/users/{user['id']}/regenerate-credentials", json={"version": 1})
        assert regenerated.status_code == 200
        recovered = account.post("/api/v1/auth/login", json={"username": user["username"],
            "password": regenerated.json()["temporary_credentials"]["temporary_password"]})
        assert recovered.status_code == 200 and recovered.json()["user"]["must_change_password"]


@pytest.mark.parametrize("operation", ["create", "regenerate"])
@pytest.mark.parametrize("failure", ["hash", "audit"])
def test_unexpected_issuance_errors_are_sanitized_and_rolled_back(authenticated, database, monkeypatch, caplog, capsys, operation, failure):
    import trackvance.identity_api as identity

    user = create_user(authenticated).json()["user"] if operation == "regenerate" else None
    with database() as db:
        original_hash = db.get(User, user["id"]).password_hash if user else None
        count_before = len(db.scalars(select(User)).all())
    sensitive = []

    def hash_failure(self, password, *args, **kwargs):
        sensitive.append(password)
        raise RuntimeError(password)

    def audit_failure(db, *args, **kwargs):
        record = db.scalar(select(User).where(User.username == "one.time"))
        sensitive.append(record.password_hash)
        raise RuntimeError(record.password_hash)

    if failure == "hash":
        monkeypatch.setattr(identity.PasswordHasher, "hash", hash_failure)
    else:
        monkeypatch.setattr(identity, "audit", audit_failure)
    if user:
        response = authenticated.post(f"/api/v1/users/{user['id']}/regenerate-credentials", json={"version": user["version"]})
    else:
        response = create_user(authenticated)
    assert response.status_code == 500
    assert response.json()["error"]["code"] == "CREDENTIAL_ISSUE_FAILED"
    assert response.headers["cache-control"] == "no-store"
    captured = capsys.readouterr()
    assert_no_disclosure([response.text, caplog.text, captured.out, captured.err], sensitive)
    with database() as db:
        assert len(db.scalars(select(User)).all()) == count_before
        assert db.scalar(select(NotificationDeliveryRecord)) is None
        if user:
            unchanged = db.get(User, user["id"])
            assert unchanged.version == user["version"] and unchanged.password_hash == original_hash


def test_first_login_hash_failure_never_logs_submitted_password(authenticated, database, monkeypatch, caplog, capsys):
    issued = create_user(authenticated).json()
    user = issued["user"]
    with TestClient(app, raise_server_exceptions=False) as account:
        logged = account.post("/api/v1/auth/login", json={"username": user["username"],
            "password": issued["temporary_credentials"]["temporary_password"]})
        account.headers["X-CSRF-Token"] = logged.json()["csrf_token"]

        def fail_hash(self, password, *args, **kwargs):
            raise RuntimeError(password)

        monkeypatch.setattr(PasswordHasher, "hash", fail_hash)
        proposed = "Synthetic failed password change 2026!"
        response = account.post("/api/v1/auth/first-login/change-password", json={"new_password": proposed})
        captured = capsys.readouterr()
        assert_no_disclosure([response.text, caplog.text, captured.out, captured.err], [proposed])
        assert response.status_code == 500 and response.json()["error"]["code"] == "PASSWORD_CHANGE_FAILED"
        with database() as db:
            unchanged = db.get(User, user["id"])
            assert unchanged.must_change_password and unchanged.version == user["version"]
            assert PasswordHasher().verify(unchanged.password_hash, issued["temporary_credentials"]["temporary_password"])
