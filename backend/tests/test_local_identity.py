import json

import pytest
from argon2 import PasswordHasher
from sqlalchemy import select

from trackvance.models import AuditEvent, AuthSession, User

PASSWORD = "A local passphrase 2048!"


def create_local_user(client, **changes):
    return client.post("/api/v1/users", json={"name": "Local Analyst", "email": "analyst@example.test",
                                              "role": "Data Analyst", "password": PASSWORD, **changes})


def test_user_crud_effective_permissions_reset_and_revocation(authenticated, database):
    created = create_local_user(authenticated)
    assert created.status_code == 201, created.text
    user = created.json()
    assert user["active"] and user["version"] == 1
    assert "runs:execute" in user["permissions"] and "users:write" not in user["permissions"]
    assert PASSWORD not in created.text and "password_hash" not in created.text
    assert "token" not in created.text
    assert authenticated.get(f"/api/v1/users/{user['id']}").json() == user
    assert len(authenticated.get("/api/v1/users/roles").json()["items"]) == 5
    with database() as db:
        record = db.get(User, user["id"])
        assert PasswordHasher().verify(record.password_hash, PASSWORD)
        db.add(AuthSession(organization_id=record.organization_id, user_id=record.id, token_hash="d" * 64,
                           csrf_token="test-token", expires_at=record.created_at))
        db.commit()
    edited = authenticated.patch(f"/api/v1/users/{user['id']}", json={"version": 1, "role": "Operations", "name": "Renamed"})
    assert edited.status_code == 200, edited.text
    assert edited.json()["version"] == 2
    assert "runs:execute" not in edited.json()["permissions"]
    assert authenticated.patch(f"/api/v1/users/{user['id']}", json={"version": 1, "active": False}).status_code == 409
    reset = authenticated.post(f"/api/v1/users/{user['id']}/reset-password", json={"version": 2, "password": "Changed secret 4096!"})
    assert reset.status_code == 200 and reset.json()["version"] == 3
    with database() as db:
        assert PasswordHasher().verify(db.get(User, user["id"]).password_hash, "Changed secret 4096!")
        assert not db.scalar(select(AuthSession).where(AuthSession.user_id == user["id"]))
        records = db.scalars(select(AuditEvent).where(AuditEvent.subject_id == user["id"])).all()
        assert [event.event_type for event in records] == ["USER_CREATED", "USER_UPDATED", "USER_PASSWORD_RESET"]
        serialized = json.dumps([event.metadata_json for event in records])
        assert PASSWORD not in serialized and "Changed secret" not in serialized and "password_hash" not in serialized
    inactive = authenticated.patch(f"/api/v1/users/{user['id']}", json={"version": 3, "active": False})
    assert inactive.status_code == 200
    assert authenticated.post("/api/v1/auth/login", json={"email": user["email"], "password": "Changed secret 4096!"}).status_code == 401
    assert user["id"] not in [item["id"] for item in authenticated.get("/api/v1/users?active=true").json()["items"]]


@pytest.mark.parametrize("role,can_read,can_write", [
    ("Administrator", True, True), ("Data Owner / Lead", False, False),
    ("Data Analyst", False, False), ("Operations", False, False), ("Auditor", True, False),
])
def test_user_administration_permissions(authenticated, database, role, can_read, can_write):
    with database() as db:
        db.get(User, "test-user").role = role
        db.commit()
    assert authenticated.get("/api/v1/users").status_code == (200 if can_read else 403)
    assert authenticated.get("/api/v1/users/roles").status_code == (200 if can_read else 403)
    assert create_local_user(authenticated).status_code == (201 if can_write else 403)


def test_last_administrator_and_organization_isolation(authenticated, database):
    for body in ({"active": False}, {"role": "Data Analyst"}):
        response = authenticated.patch("/api/v1/users/test-user", json={"version": 1, **body})
        assert response.status_code == 422
        assert response.json()["error"]["code"] == "LAST_ADMINISTRATOR_REQUIRED"
    with database() as db:
        db.add(User(id="external-user", organization_id="other-org", name="Private", email="private@example.test",
                    role="Administrator", password_hash="never-return-this"))
        db.commit()
    for method, path, body in [("GET", "/external-user", None), ("PATCH", "/external-user", {"version": 1, "active": False}),
                              ("POST", "/external-user/reset-password", {"version": 1, "password": PASSWORD})]:
        response = authenticated.request(method, "/api/v1/users" + path, json=body)
        assert response.status_code == 404
        assert "never-return-this" not in response.text
    assert "external-user" not in authenticated.get("/api/v1/users").text


@pytest.mark.parametrize("values", [{"role": "Root"}, {"email": "bad"}, {"password": "short"},
                                      {"password_hash": "forged"}, {"organization_id": "other"}])
def test_user_validation_does_not_echo_credentials(authenticated, values):
    response = create_local_user(authenticated, **values)
    assert response.status_code == 422
    assert PASSWORD not in response.text


def test_user_mutations_require_csrf_and_duplicate_email_is_generic(authenticated):
    assert create_local_user(authenticated).status_code == 201
    duplicate = create_local_user(authenticated)
    assert duplicate.status_code == 409 and duplicate.json()["error"]["code"] == "USER_EMAIL_UNAVAILABLE"
    response = authenticated.post("/api/v1/users", headers={"X-CSRF-Token": "wrong"}, json={
        "name": "Bad CSRF", "email": "csrf@example.test", "password": PASSWORD,
    })
    assert response.status_code == 403
