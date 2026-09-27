"""Server-side OIDC Authorization Code + PKCE, without provider role mapping."""
import hashlib
import os
import secrets
from dataclasses import dataclass, field
from datetime import timedelta
from typing import cast
from urllib.parse import urlparse
from uuid import UUID

import httpx
from authlib.integrations.httpx_client import OAuth2Client
from authlib.oidc.core import CodeIDToken
from fastapi import APIRouter, Depends, Request
from fastapi.responses import RedirectResponse
from joserfc import jwt
from joserfc.jwk import KeySet
from pydantic import BaseModel
from sqlalchemy import delete, func, select, update
from sqlalchemy.engine import CursorResult
from sqlalchemy.orm import Session

from .audit_context import Actor
from .config import ORG_ID, WEB_ORIGIN
from .db import get_db, utcnow
from .identity_api import lock_organization_identities
from .models import ExternalIdentity, OIDCLoginAttempt, User
from .operations_common import OperationError
from .services import audit

router = APIRouter(prefix="/api/v1/auth", tags=["Autenticación OIDC"])
CONSUMER_TENANT = "9188040d-6c67-4c5b-b112-36a304b66dad"
DISCOVERY = {
    "microsoft": "https://login.microsoftonline.com/common/v2.0/.well-known/openid-configuration",
    "google": "https://accounts.google.com/.well-known/openid-configuration",
}


@dataclass(frozen=True)
class Provider:
    name: str
    client_id: str
    client_secret: str = field(repr=False)
    discovery_url: str
    redirect_uri: str
    test_mode: bool

    @property
    def key(self) -> str:
        return "MICROSOFT_ENTRA" if self.name == "microsoft" else "GOOGLE"


def digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def provider_config(name: str) -> Provider | None:
    if name not in DISCOVERY:
        return None
    prefix = f"TRACKVANCE_SSO_{name.upper()}_"
    if os.getenv(prefix + "ENABLED", "false").casefold() != "true":
        return None
    client_id, secret = os.getenv(prefix + "CLIENT_ID", ""), os.getenv(prefix + "CLIENT_SECRET", "")
    base = os.getenv("TRACKVANCE_PUBLIC_URL", WEB_ORIGIN).rstrip("/")
    parsed = urlparse(base)
    if (not client_id or not secret or parsed.query or parsed.fragment or parsed.username
        or parsed.path not in {"", "/"} or not parsed.hostname
        or (parsed.scheme != "https" and not (parsed.scheme == "http" and parsed.hostname in {"localhost", "127.0.0.1", "::1"}))):
        return None
    test_mode = os.getenv("TRACKVANCE_SSO_TEST_MODE", "false").casefold() == "true"
    discovery = os.getenv(prefix + "DISCOVERY_URL", DISCOVERY[name]) if test_mode else DISCOVERY[name]
    return Provider(name, client_id, secret, discovery, f"{base}/api/v1/auth/sso/{name}/callback", test_mode)


def metadata(provider: Provider) -> dict:
    with httpx.Client(timeout=10, follow_redirects=False) as client:
        response = client.get(provider.discovery_url)
        response.raise_for_status()
        data = response.json()
    for key in ("authorization_endpoint", "token_endpoint", "jwks_uri", "issuer"):
        parsed = urlparse(data[key])
        if not parsed.hostname or parsed.username or parsed.fragment:
            raise ValueError("INVALID_PROVIDER_METADATA")
        if not provider.test_mode and parsed.scheme != "https":
            raise ValueError("INVALID_PROVIDER_METADATA")
    return data


class ProviderResponse(BaseModel):
    id: str
    name: str
    start_url: str


class ProvidersResponse(BaseModel):
    items: list[ProviderResponse]
    total: int
    statuses: dict[str, str]
    local_enabled: bool


@router.get("/providers", response_model=ProvidersResponse)
def providers():
    items, statuses = [], {}
    for name in DISCOVERY:
        configured = provider_config(name)
        is_enabled = os.getenv(f"TRACKVANCE_SSO_{name.upper()}_ENABLED", "false").casefold() == "true"
        statuses[name] = "ENABLED" if configured else "NOT_CONFIGURED" if is_enabled else "DISABLED"
        if configured:
            items.append({"id": name, "name": "Microsoft" if name == "microsoft" else "Google", "start_url": f"/api/v1/auth/sso/{name}/start"})
    return {"items": items, "total": len(items), "statuses": statuses, "local_enabled": True}


@router.get("/sso/{provider}/start")
def start(provider: str, request: Request, db: Session = Depends(get_db)):
    config = provider_config(provider)
    if config is None:
        raise OperationError(404, "SSO_UNAVAILABLE", "El proveedor no está configurado.")
    try:
        data = metadata(config)
        state, nonce, verifier, browser = (secrets.token_urlsafe(32) for _ in range(4))
        with OAuth2Client(config.client_id, config.client_secret, scope="openid email profile",
                          redirect_uri=config.redirect_uri, code_challenge_method="S256") as client:
            url, _ = client.create_authorization_url(data["authorization_endpoint"], state=state,
                                                      nonce=nonce, code_verifier=verifier)
        db.execute(delete(OIDCLoginAttempt).where(OIDCLoginAttempt.expires_at < utcnow()))
        db.add(OIDCLoginAttempt(state_hash=digest(state), provider=config.key, browser_hash=digest(browser),
            nonce=nonce, code_verifier=verifier, expires_at=utcnow() + timedelta(minutes=10)))
        db.commit()
        response = RedirectResponse(url, status_code=302)
        response.set_cookie(f"trackvance_oidc_{provider}", browser, httponly=True,
                            secure=config.redirect_uri.startswith("https:"), samesite="lax", max_age=600,
                            path=f"/api/v1/auth/sso/{provider}")
        return response
    except Exception:  # noqa: BLE001 - redact all provider metadata/network errors.
        db.rollback()
        raise OperationError(503, "SSO_UNAVAILABLE", "No se pudo iniciar la autenticación externa.") from None


def verified_claims(config: Provider, data: dict, token: dict, nonce: str) -> dict:
    with httpx.Client(timeout=10, follow_redirects=False) as client:
        response = client.get(data["jwks_uri"])
        response.raise_for_status()
        raw_keys = response.json()
    decoded = jwt.decode(token["id_token"], KeySet.import_key_set(raw_keys), algorithms=["RS256"])
    claims = decoded.claims
    issuer = data["issuer"]
    if config.name == "microsoft" and not config.test_mode:
        tenant = str(UUID(claims.get("tid", "")))
        issuer = f"https://login.microsoftonline.com/{tenant}/v2.0"
        for key in raw_keys["keys"]:
            if (key.get("kid") == decoded.header.get("kid") and key.get("issuer")
                and key["issuer"].replace("{tenantid}", tenant) != issuer):
                raise ValueError("INVALID_SIGNING_KEY_ISSUER")
    options = {"iss": {"essential": True, "value": issuer},
               "aud": {"essential": True, "value": config.client_id},
               "exp": {"essential": True}, "iat": {"essential": True},
               "sub": {"essential": True}, "nonce": {"essential": True}}
    if config.name == "google" and not config.test_mode:
        options["iss"] = {"essential": True, "values": ["https://accounts.google.com", "accounts.google.com"]}
    # Authlib implements OIDC claim semantics (audience/azp, nonce, times, at_hash);
    # joserfc performs JOSE signature/algorithm/key validation. No handwritten JWT.
    validated = CodeIDToken(claims, decoded.header, options=options,
        params={"nonce": nonce, "client_id": config.client_id, "access_token": token.get("access_token")})
    validated.validate(leeway=30)
    if not isinstance(claims.get("sub"), str) or not 1 <= len(claims["sub"]) <= 255:
        raise ValueError("INVALID_SUBJECT")
    result = dict(validated)
    if config.name == "google" and not config.test_mode:
        result["iss"] = "https://accounts.google.com"
    return result


def authoritative_email(provider: str, claims: dict) -> str | None:
    email = claims.get("email")
    if provider == "microsoft":
        email = email or claims.get("preferred_username")
    if not isinstance(email, str) or email.count("@") != 1 or len(email) > 200:
        return None
    email = email.strip().casefold()
    domain = email.rsplit("@", 1)[1]
    if provider == "google":
        verified = claims.get("email_verified") in (True, "true")
        hosted = claims.get("hd")
        if verified and (domain == "gmail.com" or (isinstance(hosted, str) and hosted.casefold() == domain)):
            return email
        return None
    tenant = claims.get("tid")
    if tenant == CONSUMER_TENANT and domain in {"outlook.com", "hotmail.com", "live.com", "msn.com"}:
        return email
    trusted = {item.strip().casefold() for item in os.getenv("TRACKVANCE_SSO_MICROSOFT_TRUSTED_TENANTS", "").split(",") if item.strip()}
    if claims.get("xms_edov") is True or (isinstance(tenant, str) and tenant.casefold() in trusted):
        return email
    return None


def link_user(db: Session, config: Provider, claims: dict) -> User:
    email = None
    existing = db.scalar(select(ExternalIdentity).where(ExternalIdentity.provider == config.key,
        ExternalIdentity.issuer == claims["iss"], ExternalIdentity.subject == claims["sub"]))
    if existing:
        user = db.get(User, existing.user_id)
    else:
        email = authoritative_email(config.name, claims)
        user = db.scalar(select(User).where(func.lower(User.email) == email)) if email else None
    if not user or not user.active or user.deleted:
        raise ValueError("IDENTITY_UNAVAILABLE")
    lock_organization_identities(db, user.organization_id)
    db.refresh(user)
    if not user.active or user.deleted:
        raise ValueError("IDENTITY_UNAVAILABLE")
    if existing is None and (email is None or user.email.casefold() != email):
        raise ValueError("PREPROVISIONED_IDENTIFIER_CHANGED")
    if existing and existing.organization_id != user.organization_id:
        raise ValueError("IDENTITY_UNAVAILABLE")
    if existing is None:
        if db.scalar(select(ExternalIdentity.id).where(ExternalIdentity.user_id == user.id, ExternalIdentity.provider == config.key)):
            raise ValueError("IDENTITY_ALREADY_LINKED")
        existing = ExternalIdentity(organization_id=user.organization_id, user_id=user.id,
            provider=config.key, issuer=claims["iss"], subject=claims["sub"], email_at_link=user.email)
        db.add(existing)
        audit(db, "SSO_LINKED", "user", user.id, "Identidad externa vinculada", Actor("USER", user.id, user.name),
              user.organization_id, {"provider": config.key})
    existing.last_login_at = utcnow()
    audit(db, "SSO_LOGIN_SUCCESS", "user", user.id, "Autenticación externa completada", Actor("USER", user.id, user.name),
          user.organization_id, {"provider": config.key})
    return user


@router.get("/sso/{provider}/callback")
def callback(provider: str, request: Request, db: Session = Depends(get_db)):
    config = provider_config(provider)
    if config is None:
        raise OperationError(404, "SSO_UNAVAILABLE", "El proveedor no está configurado.")
    response = RedirectResponse("/login?sso_error=SSO_LOGIN_FAILED", status_code=302)
    response.delete_cookie(f"trackvance_oidc_{provider}", path=f"/api/v1/auth/sso/{provider}")
    try:
        state, code = request.query_params.get("state", ""), request.query_params.get("code", "")
        browser = request.cookies.get(f"trackvance_oidc_{provider}", "")
        if not state or not code or not browser or request.query_params.get("error"):
            raise ValueError("INVALID_CALLBACK")
        attempt = db.scalar(select(OIDCLoginAttempt).where(OIDCLoginAttempt.state_hash == digest(state),
            OIDCLoginAttempt.provider == config.key, OIDCLoginAttempt.expires_at > utcnow(),
            OIDCLoginAttempt.consumed_at.is_(None)))
        if attempt is None or not secrets.compare_digest(attempt.browser_hash, digest(browser)):
            raise ValueError("INVALID_STATE")
        nonce, verifier = attempt.nonce, attempt.code_verifier
        consumed = db.execute(update(OIDCLoginAttempt).where(OIDCLoginAttempt.state_hash == attempt.state_hash,
            OIDCLoginAttempt.consumed_at.is_(None)).values(consumed_at=utcnow(), code_verifier="", nonce=""))
        if cast(CursorResult, consumed).rowcount != 1:
            raise ValueError("CALLBACK_REPLAY")
        db.commit()  # Consume before network exchange; concurrent or retried callbacks cannot reuse code.
        data = metadata(config)
        with OAuth2Client(config.client_id, config.client_secret, redirect_uri=config.redirect_uri,
                          token_endpoint_auth_method="client_secret_post", timeout=15) as client:
            token = client.fetch_token(data["token_endpoint"], code=code, code_verifier=verifier,
                                       grant_type="authorization_code")
        claims = verified_claims(config, data, token, nonce)
        token.clear()
        user = link_user(db, config, claims)
        from .api import establish_session
        response.headers["location"] = "/"
        establish_session(request, response, db, user, config.key)
        return response
    except Exception:  # noqa: BLE001 - OAuth/JWT exceptions may contain credentials or tokens.
        db.rollback()
        audit(db, "SSO_LOGIN_FAILED", "authentication", "sso", "Autenticación externa rechazada",
              Actor("SYSTEM", "oidc-auth", "Autenticación"), ORG_ID, {"provider": config.key})
        db.commit()
        response.headers["location"] = "/login?sso_error=SSO_LOGIN_FAILED"
        return response
