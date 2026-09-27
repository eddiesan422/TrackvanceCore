import hashlib
import json
import time
from urllib.parse import parse_qs, urlparse

import httpx
import pytest
from argon2 import PasswordHasher
from fastapi.testclient import TestClient
from joserfc import jwt
from joserfc.jwk import RSAKey
from sqlalchemy import select

from trackvance.api import app
from trackvance.models import AuditEvent, ExternalIdentity, OIDCLoginAttempt, User
from trackvance.sso_api import CONSUMER_TENANT, authoritative_email


@pytest.fixture
def oidc(monkeypatch):
    """Real OAuth client and signed ID tokens with deterministic transport only."""
    key = RSAKey.generate_key(2048)
    key.ensure_kid()
    state = {"profile": "gmail", "claims": {}, "requests": [], "code_used": False}
    providers = {"google": "http://oidc.test/google", "microsoft": "http://oidc.test/microsoft"}
    for provider, base in providers.items():
        prefix = f"TRACKVANCE_SSO_{provider.upper()}_"
        monkeypatch.setenv(prefix + "ENABLED", "true")
        monkeypatch.setenv(prefix + "CLIENT_ID", "client-id")
        monkeypatch.setenv(prefix + "CLIENT_SECRET", "never-log-client-secret")
        monkeypatch.setenv(prefix + "DISCOVERY_URL", base + "/.well-known/openid-configuration")
    monkeypatch.setenv("TRACKVANCE_SSO_TEST_MODE", "true")
    monkeypatch.setenv("TRACKVANCE_PUBLIC_URL", "http://localhost:3000")

    def handle(request):
        state["requests"].append((request.method, str(request.url)))
        provider = "microsoft" if "/microsoft/" in request.url.path else "google"
        base = providers[provider]
        if request.url.path.endswith("openid-configuration"):
            return httpx.Response(200, json={"issuer": base, "authorization_endpoint": base + "/authorize",
                "token_endpoint": base + "/token", "jwks_uri": base + "/jwks"})
        if request.url.path.endswith("jwks"):
            return httpx.Response(200, json={"keys": [key.as_dict(private=False)]})
        if request.url.path.endswith("token"):
            fields = parse_qs(request.content.decode())
            if state["code_used"] or fields["code"] != ["code-once"]:
                return httpx.Response(400, json={"error": "invalid_grant"})
            assert fields["client_secret"] == ["never-log-client-secret"]
            verifier = fields["code_verifier"][0]
            import base64
            challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
            assert challenge == state["authorization"]["code_challenge"][0]
            state["code_used"] = True
            claims = {"iss": base, "aud": "client-id", "sub": "stable-subject", "iat": int(time.time()),
                "exp": int(time.time()) + 300, "nonce": state["authorization"]["nonce"][0],
                "email": "tester@gmail.com", "email_verified": True}
            if provider == "microsoft":
                claims.update(email="tester@outlook.com", tid=CONSUMER_TENANT)
            if state["profile"] == "workspace":
                claims.update(email="tester@workspace.test", hd="workspace.test")
            if state["profile"] == "organizational":
                claims.update(email="tester@company.test", tid="c06f03d4-cf3a-4c18-bbd5-99c745bd7605", xms_edov=True)
            claims.update(state["claims"])
            signing_key = RSAKey.generate_key(2048) if state.get("bad_signature") else key
            token = jwt.encode({"alg": "RS256", "kid": key.kid}, claims, signing_key)
            state["id_token"] = token
            return httpx.Response(200, json={"access_token": "never-persist-access-token", "token_type": "Bearer", "id_token": token})
        return httpx.Response(404)

    transport = httpx.MockTransport(handle)
    original = httpx.Client.__init__

    def initialize(self, *args, **kwargs):
        if "transport" not in kwargs:  # Preserve FastAPI's in-process ASGI test transport.
            kwargs["transport"] = transport
        original(self, *args, **kwargs)

    monkeypatch.setattr(httpx.Client, "__init__", initialize)
    return state


def start(client, oidc, provider="google"):
    oidc["code_used"] = False
    response = client.get(f"/api/v1/auth/sso/{provider}/start", follow_redirects=False)
    assert response.status_code == 302, response.text
    fields = parse_qs(urlparse(response.headers["location"]).query)
    oidc["authorization"] = fields
    assert fields["response_type"] == ["code"] and fields["code_challenge_method"] == ["S256"]
    assert set(fields["scope"][0].split()) == {"openid", "email", "profile"}
    assert "?" not in fields["redirect_uri"][0]
    return fields["state"][0]


def finish(client, state, provider="google"):
    return client.get(f"/api/v1/auth/sso/{provider}/callback", params={"state": state, "code": "code-once"}, follow_redirects=False)


@pytest.mark.parametrize("provider,profile,email", [("google", "gmail", "tester@gmail.com"),
    ("google", "workspace", "tester@workspace.test"), ("microsoft", "personal", "tester@outlook.com"),
    ("microsoft", "organizational", "tester@company.test")])
def test_oidc_signed_code_pkce_link_and_stable_subject(database, oidc, provider, profile, email):
    oidc["profile"] = profile
    with database() as db:
        user = db.get(User, "test-user")
        user.email = email
        db.commit()
    with TestClient(app) as client:
        state = start(client, oidc, provider)
        result = finish(client, state, provider)
        assert result.headers["location"] == "/"
        identity = client.get("/api/v1/me").json()
        assert identity["user"]["id"] == "test-user"
        assert identity["user"]["last_login_at"]
        assert identity["user"]["external_identities"][0]["subject"] == "stable-subject"
        assert finish(client, state, provider).headers["location"].endswith("SSO_LOGIN_FAILED")
        with database() as db:
            link = db.scalar(select(ExternalIdentity))
            assert link.user_id == "test-user"
            serialized = json.dumps([event.metadata_json for event in db.scalars(select(AuditEvent))])
            for secret in ("never-log-client-secret", "never-persist-access-token", "code-once", oidc["id_token"]):
                assert secret not in serialized
            attempt = db.scalar(select(OIDCLoginAttempt))
            assert attempt.consumed_at and not attempt.code_verifier and not attempt.nonce
        oidc["claims"] = {"email": "changed@external.test", "email_verified": False, "hd": None}
        state = start(client, oidc, provider)
        assert finish(client, state, provider).headers["location"] == "/"


@pytest.mark.parametrize("claims", [{"exp": 1}, {"aud": "wrong"}, {"iss": "https://evil.test"},
    {"nonce": "wrong"}, {"email_verified": False}, {"email": "tester@external.test"},
    {"email": "missing@gmail.com"}, {"sub": ""}])
def test_oidc_rejects_invalid_claims_or_unprovisioned_identity(database, oidc, claims):
    with database() as db:
        db.get(User, "test-user").email = "tester@gmail.com"
        db.commit()
    oidc["claims"] = claims
    with TestClient(app) as client:
        result = finish(client, start(client, oidc))
        assert result.headers["location"].endswith("SSO_LOGIN_FAILED")
        assert client.get("/api/v1/me").status_code == 401
    with database() as db:
        assert not db.scalar(select(ExternalIdentity))
        assert len(db.scalars(select(User)).all()) == 1


def test_oidc_state_binding_signature_and_first_login_restriction(database, oidc):
    with database() as db:
        user = db.get(User, "test-user")
        user.email, user.must_change_password = "tester@gmail.com", True
        user.password_hash = PasswordHasher().hash("temporary value never exposed")
        db.commit()
    with TestClient(app) as client:
        state = start(client, oidc)
        assert finish(client, "wrong").headers["location"].endswith("SSO_LOGIN_FAILED")
        state = start(client, oidc)
        oidc["bad_signature"] = True
        assert finish(client, state).headers["location"].endswith("SSO_LOGIN_FAILED")
        oidc["bad_signature"] = False
        state = start(client, oidc)
        with TestClient(app) as other_browser:
            assert finish(other_browser, state).headers["location"].endswith("SSO_LOGIN_FAILED")
        assert finish(client, state).headers["location"] == "/"
        me = client.get("/api/v1/me").json()
        assert me["user"]["must_change_password"]
        assert client.get("/api/v1/datasets").status_code == 403
        client.headers["X-CSRF-Token"] = me["csrf_token"]
        changed = client.post("/api/v1/auth/first-login/change-password", json={"new_password": "Permanent local secret 12!"})
        assert changed.status_code == 200 and not changed.json()["user"]["must_change_password"]
        assert client.get("/api/v1/datasets").status_code == 200


@pytest.mark.parametrize("flag", ["active", "deleted"])
def test_oidc_inactive_or_deleted_user_cannot_authenticate(database, oidc, flag):
    with database() as db:
        user = db.get(User, "test-user")
        user.email = "tester@gmail.com"
        setattr(user, flag, flag == "deleted")
        db.commit()
    with TestClient(app) as client:
        assert finish(client, start(client, oidc)).headers["location"].endswith("SSO_LOGIN_FAILED")


def test_microsoft_first_link_requires_email_authority(monkeypatch):
    claims = {"email": "admin@victim.test", "tid": "untrusted"}
    assert authoritative_email("microsoft", claims) is None
    assert authoritative_email("microsoft", {**claims, "xms_edov": True}) == "admin@victim.test"
    monkeypatch.setenv("TRACKVANCE_SSO_MICROSOFT_TRUSTED_TENANTS", "trusted")
    assert authoritative_email("microsoft", {**claims, "tid": "trusted"}) == "admin@victim.test"
    assert authoritative_email("google", {"email": "admin@external.test", "email_verified": True}) is None
    assert authoritative_email("google", {"email": "admin@external.test", "email_verified": True, "hd": "wrong.test"}) is None


def test_first_link_rechecks_email_after_organization_lock(database, oidc, monkeypatch):
    import trackvance.sso_api as sso
    with database() as db:
        db.get(User, "test-user").email = "tester@gmail.com"
        db.commit()
    original = sso.lock_organization_identities

    def change_email(db, organization_id):
        from sqlalchemy import update
        original(db, organization_id)
        db.execute(update(User).where(User.id == "test-user").values(email="changed@gmail.com").execution_options(synchronize_session=False))

    monkeypatch.setattr(sso, "lock_organization_identities", change_email)
    with TestClient(app) as client:
        assert finish(client, start(client, oidc)).headers["location"].endswith("SSO_LOGIN_FAILED")
        assert client.get("/api/v1/me").status_code == 401
    with database() as db:
        assert db.scalar(select(ExternalIdentity)) is None


@pytest.mark.parametrize("issuer", ["accounts.google.com", "https://accounts.google.com"])
def test_official_google_issuer_alias_normalizes_stable_identity(oidc, issuer, monkeypatch):
    from trackvance.sso_api import Provider, verified_claims
    key = RSAKey.generate_key(2048)
    key.ensure_kid()
    original = httpx.Client.get

    def get_keys(client, url, **kwargs):
        if url == "https://google.test/jwks":
            return httpx.Response(200, json={"keys": [key.as_dict(private=False)]}, request=httpx.Request("GET", url))
        return original(client, url, **kwargs)

    monkeypatch.setattr(httpx.Client, "get", get_keys)
    claims = {"iss": issuer, "sub": "stable", "aud": "client", "nonce": "nonce", "iat": int(time.time()), "exp": int(time.time())+300}
    token = jwt.encode({"alg": "RS256", "kid": key.kid}, claims, key)
    config = Provider("google", "client", "secret", "https://accounts.google.com/discovery", "https://app.test/callback", False)
    result = verified_claims(config, {"issuer": "https://accounts.google.com", "jwks_uri": "https://google.test/jwks"}, {"id_token": token}, "nonce")
    assert result["iss"] == "https://accounts.google.com" and result["sub"] == "stable"


@pytest.mark.parametrize("provider,profile,email", [("google", "gmail", "tester@gmail.com"),
    ("google", "workspace", "tester@workspace.test"), ("microsoft", "personal", "tester@outlook.com"),
    ("microsoft", "organizational", "tester@company.test")])
def test_preprovisioned_one_time_credentials_support_sso_without_smtp(authenticated, database, oidc, monkeypatch, provider, profile, email):
    import smtplib

    from trackvance.models import NotificationDeliveryRecord

    def forbidden(*args, **kwargs):
        pytest.fail("SSO and local credentials must not depend on SMTP")

    monkeypatch.setattr(smtplib, "SMTP", forbidden)
    monkeypatch.setattr(smtplib, "SMTP_SSL", forbidden)
    monkeypatch.setenv("TRACKVANCE_SMTP_ENABLED", "true")
    role = next(row for row in authenticated.get("/api/v1/roles").json()["items"] if row["name"] == "Data Analyst")
    issued = authenticated.post("/api/v1/users", json={"first_name": "Federated", "last_name": "User",
        "username": "federated.user", "email": email, "role_id": role["id"]})
    assert issued.status_code == 201
    user = issued.json()["user"]
    temporary = issued.json()["temporary_credentials"]["temporary_password"]
    oidc["profile"] = profile
    with TestClient(app) as client:
        assert finish(client, start(client, oidc, provider), provider).headers["location"] == "/"
        me = client.get("/api/v1/me").json()
        assert me["user"]["id"] == user["id"] and me["user"]["must_change_password"]
        assert client.get("/api/v1/datasets").status_code == 403
        client.headers["X-CSRF-Token"] = me["csrf_token"]
        changed = client.post("/api/v1/auth/first-login/change-password", json={"new_password": "Federated local password 2026!"})
        assert changed.status_code == 200 and not changed.json()["user"]["must_change_password"]
        assert client.get("/api/v1/datasets").status_code == 200
        regenerated = authenticated.post(f"/api/v1/users/{user['id']}/regenerate-credentials",
            json={"version": changed.json()["user"]["version"]})
        assert regenerated.status_code == 200
        assert client.get("/api/v1/me").status_code == 401
        assert finish(client, start(client, oidc, provider), provider).headers["location"] == "/"
        assert client.get("/api/v1/me").json()["user"]["must_change_password"]
        assert client.get("/api/v1/datasets").status_code == 403
    with database() as db:
        assert db.scalar(select(NotificationDeliveryRecord)) is None
        assert db.scalar(select(ExternalIdentity).where(ExternalIdentity.user_id == user["id"])) is not None
        serialized = json.dumps([row.metadata_json for row in db.scalars(select(AuditEvent))])
        leaked = temporary in serialized or regenerated.json()["temporary_credentials"]["temporary_password"] in serialized
        assert not leaked


def test_sso_disabled_by_default_cannot_be_enabled_by_smtp(client, monkeypatch):
    monkeypatch.setenv("TRACKVANCE_SMTP_ENABLED", "true")
    for provider in ("GOOGLE", "MICROSOFT"):
        for field in ("ENABLED", "CLIENT_ID", "CLIENT_SECRET", "DISCOVERY_URL"):
            monkeypatch.delenv(f"TRACKVANCE_SSO_{provider}_{field}", raising=False)
    providers = client.get("/api/v1/auth/providers").json()
    assert providers["local_enabled"] is True
    assert providers["items"] == [] and providers["statuses"] == {"google": "DISABLED", "microsoft": "DISABLED"}
    for provider in ("google", "microsoft"):
        response = client.get(f"/api/v1/auth/sso/{provider}/start", follow_redirects=False)
        assert response.status_code == 404
