import json
from datetime import timedelta

import pytest
from argon2 import PasswordHasher
from fastapi.testclient import TestClient
from sqlalchemy import select

from trackvance.api import app
from trackvance.db import utcnow
from trackvance.models import (
    AuditEvent,
    AuthSession,
    NotificationDeliveryRecord,
    RolePermission,
    User,
)
from trackvance.permissions import (
    CATALOG,
    ENDPOINT_MATRIX,
    NON_DELEGABLE,
    UNMAPPED,
    required_permission,
)

PASSWORD = "A new local passphrase 2048!"


def role_id(client, name="Data Analyst"):
    return next(row["id"] for row in client.get("/api/v1/users/roles").json()["items"] if row["name"] == name)


def create_local_user(client, **changes):
    return client.post("/api/v1/users", json={"first_name": "Local", "last_name": "Analyst",
        "username": "local.analyst", "email": "analyst@example.test", "role_id": role_id(client), **changes})


def login(client, username, password):
    response = client.post("/api/v1/auth/login", json={"username": username, "password": password})
    if response.status_code == 200:
        client.headers["X-CSRF-Token"] = response.json()["csrf_token"]
    return response


def test_generated_credentials_first_login_rotation_and_no_secret_persistence(authenticated, database):
    created = create_local_user(authenticated)
    assert created.status_code == 201, created.text
    user, temporary = created.json()["user"], created.json()["temporary_credentials"]["temporary_password"]
    assert len(temporary) >= 20 and user["must_change_password"]
    assert user["name"] == "Local Analyst" and "credential_delivery" not in user
    assert temporary not in json.dumps(user) and "password_hash" not in created.text
    with database() as db:
        assert PasswordHasher().verify(db.get(User, user["id"]).password_hash, temporary)
        assert temporary not in json.dumps([row.metadata_json for row in db.scalars(select(AuditEvent))])
        assert {column.name for column in NotificationDeliveryRecord.__table__.columns}.isdisjoint({"body", "password", "token"})
    with TestClient(app) as account:
        logged = login(account, "LOCAL.ANALYST", temporary)
        assert logged.status_code == 200 and logged.json()["user"]["must_change_password"]
        before_cookie, before_csrf = account.cookies.get("trackvance_session"), logged.json()["csrf_token"]
        for route in ("/datasets", "/connections", "/delivery/configurations", "/roles"):
            denied = account.get("/api/v1" + route)
            assert denied.status_code == 403 and denied.json()["error"]["code"] == "PASSWORD_CHANGE_REQUIRED"
        assert account.get("/api/v1/me").status_code == 200
        too_short = account.post("/api/v1/auth/first-login/change-password", json={"new_password": "short"})
        assert too_short.status_code == 422
        same = account.post("/api/v1/auth/first-login/change-password", json={"new_password": temporary})
        assert same.status_code == 422 and same.json()["error"]["code"] == "PASSWORD_MUST_DIFFER"
        changed = account.post("/api/v1/auth/first-login/change-password", json={"new_password": PASSWORD})
        assert changed.status_code == 200, changed.text
        assert not changed.json()["user"]["must_change_password"]
        assert changed.json()["csrf_token"] != before_csrf and account.cookies.get("trackvance_session") != before_cookie
        assert account.get("/api/v1/datasets").status_code == 200
        assert login(account, user["email"], temporary).status_code == 401
        assert login(account, user["email"].upper(), PASSWORD).status_code == 200


def test_credentials_regenerate_expire_and_revoke(authenticated, database):
    issued = create_local_user(authenticated).json()
    user, old_password = issued["user"], issued["temporary_credentials"]["temporary_password"]
    with TestClient(app) as account:
        assert login(account, user["username"], old_password).status_code == 200
        regenerated = authenticated.post(f"/api/v1/users/{user['id']}/regenerate-credentials", json={"version": user["version"]})
        assert regenerated.status_code == 200
        password = regenerated.json()["temporary_credentials"]["temporary_password"]
        assert password != old_password and account.get("/api/v1/me").status_code == 401
        assert login(account, user["username"], old_password).status_code == 401
        assert login(account, user["username"], password).status_code == 200
        with database() as db:
            db.get(User, user["id"]).temporary_password_expires_at = utcnow() - timedelta(seconds=1)
            db.commit()
        assert account.get("/api/v1/me").status_code == 401
        assert login(account, user["username"], password).status_code == 401
    with database() as db:
        assert db.get(User, user["id"])
        assert not db.scalar(select(NotificationDeliveryRecord))


def test_dynamic_role_changes_are_effective_without_session_recreation(authenticated, database):
    role = authenticated.post("/api/v1/roles", json={"name": "Custom", "permissions": ["datasets:read"]}).json()
    user = create_local_user(authenticated, role_id=role["id"]).json()["user"]
    with database() as db:
        record = db.get(User, user["id"])
        record.must_change_password = False
        record.password_hash = PasswordHasher().hash(PASSWORD)
        db.commit()
    with TestClient(app) as account:
        assert login(account, user["username"], PASSWORD).status_code == 200
        cookie = account.cookies.get("trackvance_session")
        assert account.get("/api/v1/datasets").status_code == 200
        edited = authenticated.patch(f"/api/v1/roles/{role['id']}", json={"version": 1, "permissions": ["audit:read"]})
        assert edited.status_code == 200, edited.text
        assert account.get("/api/v1/datasets").status_code == 403
        current = account.get("/api/v1/me").json()
        assert current["user"]["permissions"] == ["audit:read"] and current["user"]["role_version"] == 2
        assert account.cookies.get("trackvance_session") == cookie
        assigned = authenticated.patch(f"/api/v1/users/{user['id']}", json={"version": user["version"], "role_id": role_id(authenticated, "Operations")})
        assert assigned.status_code == 200 and account.get("/api/v1/me").status_code == 401


@pytest.mark.parametrize("grants,code", [(["root:all"], "UNKNOWN_PERMISSION"), (["delivery:overwrite"], "PERMISSION_DEPENDENCIES_REQUIRED"), (["roles:read", "roles:manage"], "NON_DELEGABLE_PERMISSION"), (["users:read", "users:manage"], "NON_DELEGABLE_PERMISSION")])
def test_role_unknown_dependency_and_escalation_rejected(authenticated, grants, code):
    response = authenticated.post("/api/v1/roles", json={"name": "Forbidden", "permissions": grants})
    assert response.status_code == 422 and response.json()["error"]["code"] == code


def test_protected_administrator_always_resolves_full_catalog(authenticated, database):
    identity = role_id(authenticated, "Administrator")
    for body in ({"name": "Renamed"}, {"active": False}, {"permissions": []}):
        response = authenticated.patch(f"/api/v1/roles/{identity}", json={"version": 1, **body})
        assert response.status_code == 422 and response.json()["error"]["code"] == "PROTECTED_ROLE"
    assert authenticated.request("DELETE", f"/api/v1/roles/{identity}", json={"version": 1}).status_code == 422
    with database() as db:
        assert not db.scalar(select(RolePermission).where(RolePermission.role_id == identity))
    assert set(authenticated.get("/api/v1/me").json()["user"]["permissions"]) == CATALOG


def test_retire_role_blocks_inactive_users_but_not_deleted_and_reserves_names(authenticated):
    role = authenticated.post("/api/v1/roles", json={"name": "Retire me", "permissions": []}).json()
    user = create_local_user(authenticated, role_id=role["id"], active=False).json()["user"]
    assert authenticated.patch(f"/api/v1/roles/{role['id']}", json={"version": 1, "active": False}).json()["error"]["code"] == "ROLE_HAS_USERS"
    assert authenticated.request("DELETE", f"/api/v1/roles/{role['id']}", json={"version": 1}).status_code == 422
    deleted = authenticated.request("DELETE", f"/api/v1/users/{user['id']}", json={"version": 1})
    assert deleted.status_code == 200 and deleted.json()["deleted"]
    retired = authenticated.request("DELETE", f"/api/v1/roles/{role['id']}", json={"version": 1})
    assert retired.status_code == 200 and retired.json()["deleted"]
    assert authenticated.post("/api/v1/roles", json={"name": "RETIRE ME", "permissions": []}).status_code == 409
    assert create_local_user(authenticated).status_code == 409


def test_user_disable_delete_revoke_and_last_administrator(authenticated, database):
    for body in ({"active": False}, {"role_id": role_id(authenticated)}):
        response = authenticated.patch("/api/v1/users/test-user", json={"version": 1, **body})
        assert response.status_code == 422 and response.json()["error"]["code"] == "LAST_ADMINISTRATOR_REQUIRED"
    assert authenticated.request("DELETE", "/api/v1/users/test-user", json={"version": 1}).status_code == 422
    issued = create_local_user(authenticated).json()
    user = issued["user"]
    with TestClient(app) as account:
        assert login(account, user["username"], issued["temporary_credentials"]["temporary_password"]).status_code == 200
        edited = authenticated.patch(f"/api/v1/users/{user['id']}", json={"version": 1, "active": False})
        assert edited.status_code == 200
        assert account.get("/api/v1/me").status_code == 401
        assert login(account, user["username"], issued["temporary_credentials"]["temporary_password"]).status_code == 401
    with database() as db:
        assert db.get(User, user["id"])
        assert not db.scalar(select(AuthSession).where(AuthSession.user_id == user["id"]))


def test_dynamic_rbac_no_string_fallback_and_non_delegable_storage_corruption(authenticated, database):
    analyst_id = role_id(authenticated)
    with database() as db:
        user = db.get(User, "test-user")
        user.role_id = analyst_id
        # Historical label deliberately says Administrator; authorization ignores it.
        db.add_all([RolePermission(role_id=analyst_id, permission_code=code) for code in NON_DELEGABLE])
        db.commit()
    assert authenticated.get("/api/v1/users").status_code == 403
    assert authenticated.post("/api/v1/roles", json={"name": "Escalate"}).status_code == 403
    assert not NON_DELEGABLE.intersection(authenticated.get("/api/v1/me").json()["user"]["permissions"])


def test_scoped_identity_csrf_validation_and_reservations(authenticated, database):
    with database() as db:
        db.add(User(id="external-user", organization_id="other-org", name="Private", email="private@example.test", password_hash="never-return-this"))
        db.commit()
    assert authenticated.get("/api/v1/users/external-user").status_code == 404
    assert authenticated.patch("/api/v1/users/external-user", json={"version": 1, "active": False}).status_code == 404
    assert authenticated.get("/api/v1/users").json()["total"] == 1
    for changes in ({"username": "bad space"}, {"password": "Should never be accepted"}, {"role": "Administrator"}, {"organization_id": "other"}):
        response = create_local_user(authenticated, **changes)
        assert response.status_code == 422 and "Should never" not in response.text
    assert create_local_user(authenticated).status_code == 201
    assert create_local_user(authenticated, username="LOCAL.ANALYST", email="new@example.test").status_code == 409
    response = authenticated.post("/api/v1/roles", json={"name": "CSRF"}, headers={"X-CSRF-Token": "wrong"})
    assert response.status_code == 403


def test_unknown_routes_fail_closed_and_every_route_has_explicit_matrix(authenticated):
    for path in ("/new-private-route", "/me/forged", "/auth/future-protected", "/delivery/configurations/forged"):
        assert required_permission(path, "GET") == UNMAPPED
        assert authenticated.get("/api/v1" + path).status_code == 403
    for path, methods in app.openapi()["paths"].items():
        for method in set(methods) & {"get", "post", "patch", "delete"}:
            assert required_permission(path.removeprefix("/api/v1"), method.upper()) != UNMAPPED, (method, path)
    assert all(permission in CATALOG for _, _, permission in ENDPOINT_MATRIX)


def test_shared_runs_dashboard_and_artifacts_require_module_access(authenticated, database, queued_intake):
    from trackvance.models import Artifact, Role
    from trackvance.worker import process_once
    assert process_once("identity-module-guard")
    run_id = queued_intake["run_id"]
    with database() as db:
        role = Role(name="Only delivery", normalized_name="only delivery")
        db.add(role)
        db.flush()
        db.add_all([RolePermission(role_id=role.id, permission_code=code) for code in
                    ("delivery:read", "runs:read", "exports:download", "artifacts:download")])
        db.get(User, "test-user").role_id = role.id
        artifact = db.scalar(select(Artifact).where(Artifact.kind == "EVIDENCE_MANIFEST"))
        artifact_id = artifact.id if artifact else None
        db.commit()
    assert authenticated.get("/api/v1/runs").json()["items"] == []
    assert authenticated.get("/api/v1/findings").json()["items"] == []
    dashboard = authenticated.get("/api/v1/dashboard").json()
    assert dashboard["recent_runs"] == [] and dashboard["stats"]["runs"] == 0
    assert dashboard["activity"] == []
    for suffix in ("", "/results", "/evidence", "/export.xlsx", "/diagnostics", "/execution-plan"):
        assert authenticated.get(f"/api/v1/runs/{run_id}{suffix}").status_code == 403
    if artifact_id:
        assert authenticated.get(f"/api/v1/artifacts/{artifact_id}/download").status_code == 403


def test_edit_same_renamed_long_role_preserves_role_reference(authenticated, database):
    role = authenticated.post("/api/v1/roles", json={"name": "Custom", "permissions": []}).json()
    user = create_local_user(authenticated, role_id=role["id"]).json()["user"]
    renamed = authenticated.patch(f"/api/v1/roles/{role['id']}", json={"version": 1, "name": "Custom " + "long " * 14})
    assert renamed.status_code == 200
    edited = authenticated.patch(f"/api/v1/users/{user['id']}", json={"version": 1, "role_id": role["id"], "first_name": "Edited"})
    assert edited.status_code == 200 and edited.json()["role_id"] == role["id"]


def test_first_login_revalidates_revoked_session_after_lock(authenticated, database, monkeypatch):
    import trackvance.identity_api as identity_module
    issued = create_local_user(authenticated).json()
    user = issued["user"]
    with TestClient(app) as account:
        assert login(account, user["username"], issued["temporary_credentials"]["temporary_password"]).status_code == 200
        original = identity_module.lock_organization_identities

        def revoke_before_lock(db, organization_id):
            from sqlalchemy import delete
            original(db, organization_id)
            db.execute(delete(AuthSession).where(AuthSession.user_id == user["id"]))
            db.commit()

        monkeypatch.setattr(identity_module, "lock_organization_identities", revoke_before_lock)
        response = account.post("/api/v1/auth/first-login/change-password", json={"new_password": PASSWORD})
        assert response.status_code == 401
        with database() as db:
            assert db.get(User, user["id"]).must_change_password


def test_https_public_url_sets_secure_session_cookie(client, monkeypatch):
    monkeypatch.setenv("TRACKVANCE_PUBLIC_URL", "https://trackvance.example.test")
    response = client.post("/api/v1/auth/login", json={"email": "tester@example.test", "password": "test-password"})
    assert response.status_code == 200 and "Secure" in response.headers["set-cookie"]
