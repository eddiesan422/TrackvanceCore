"""Disposable OIDC provider for browser certification, never a product service.

Keys and single-use authorization codes exist only in memory. The mock verifies
PKCE and client binding and signs real RS256 ID tokens; Trackvance still executes
its real discovery, exchange, signature and claim validation code.
"""
from __future__ import annotations

import base64
import hashlib
import html
import json
import os
import secrets
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlencode, urlsplit

from joserfc import jwt
from joserfc.jwk import RSAKey

KEY = RSAKey.generate_key(2048, parameters={"kid": "disposable-certification-key"})
CODES: dict[str, dict] = {}
LOCK = threading.Lock()
INTERNAL = os.environ.get("MOCK_OIDC_INTERNAL_URL", "http://mock-oidc:9000")
PUBLIC = os.environ.get("MOCK_OIDC_PUBLIC_URL", "http://127.0.0.1:9000")
CLIENT_ID = "trackvance-disposable-client"
CLIENT_SECRET = os.environ.get("MOCK_OIDC_CLIENT_SECRET", "disposable-not-production")
PERSONAL_TENANT = "9188040d-6c67-4c5b-b112-36a304b66dad"
ORG_TENANT = "11111111-2222-3333-4444-555555555555"


class Handler(BaseHTTPRequestHandler):
    def log_message(self, _format, *_args):
        # Standard HTTP logging would leak callback codes or authorization params.
        pass

    def respond(self, data, status=200, *, content_type="application/json"):
        payload = (json.dumps(data) if content_type == "application/json" else data).encode()
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def redirect(self, target):
        self.send_response(302)
        self.send_header("Location", target)
        self.send_header("Cache-Control", "no-store")
        self.end_headers()

    def do_GET(self):
        parsed = urlsplit(self.path)
        if parsed.path == "/health":
            return self.respond({"status": "ok", "test_only": True})
        parts = parsed.path.strip("/").split("/")
        provider = parts[0]
        if provider not in {"microsoft", "google"}:
            return self.respond({"error": "invalid_provider"}, 404)
        issuer = f"{INTERNAL}/{provider}"
        if parsed.path.endswith("/.well-known/openid-configuration"):
            return self.respond({
                "issuer": issuer,
                "authorization_endpoint": f"{PUBLIC}/{provider}/authorize",
                "token_endpoint": f"{issuer}/token", "jwks_uri": f"{issuer}/jwks",
                "response_types_supported": ["code"], "subject_types_supported": ["public"],
                "id_token_signing_alg_values_supported": ["RS256"],
                "token_endpoint_auth_methods_supported": ["client_secret_basic", "client_secret_post"],
                "code_challenge_methods_supported": ["S256"],
                "scopes_supported": ["openid", "profile", "email"],
            })
        if parsed.path.endswith("/jwks"):
            return self.respond({"keys": [KEY.as_dict(private=False)]})
        if parsed.path.endswith("/authorize"):
            query = {key: values[0] for key, values in parse_qs(parsed.query).items()}
            if (query.get("client_id") != CLIENT_ID or query.get("response_type") != "code"
                    or query.get("code_challenge_method") != "S256"
                    or not all(query.get(key) for key in ("code_challenge", "state", "nonce"))):
                return self.respond({"error": "invalid_authorization_request"}, 400)
            callback = urlsplit(query.get("redirect_uri", ""))
            if callback.hostname not in {"127.0.0.1", "localhost"}:
                return self.respond({"error": "unsafe_test_callback"}, 400)
            hidden = "".join(f'<input type="hidden" name="{html.escape(key)}" '
                             f'value="{html.escape(value, quote=True)}">'
                             for key, value in query.items())
            profiles = ("Microsoft personal", "Microsoft organizational") if provider == "microsoft" else (
                "Google Gmail", "Google Workspace", "Google external email")
            options = "".join(f"<option>{profile}</option>" for profile in profiles)
            modes = "".join(f"<option>{mode}</option>" for mode in (
                "valid", "expired", "wrong_audience", "wrong_nonce", "wrong_issuer", "wrong_state",
                "unverified_email", "invalid_signature"))
            return self.respond(
                '<!doctype html><html lang="en"><title>Disposable OIDC</title><body>'
                '<h1>Disposable OIDC provider</h1><p>Test environment only</p>'
                f'<form method="post" action="/{provider}/authorize">{hidden}'
                '<label>Email <input name="email" type="email" required></label>'
                '<label>Subject <input name="subject" value="stable-test-subject" required></label>'
                f'<label>Profile <select name="profile">{options}</select></label>'
                f'<label>Scenario <select name="scenario">{modes}</select></label>'
                '<button>Authorize test identity</button></form></body></html>',
                content_type="text/html; charset=utf-8",
            )
        return self.respond({"error": "not_found"}, 404)

    def do_POST(self):
        if int(self.headers.get("Content-Length", "0")) > 16384:
            return self.respond({"error": "request_too_large"}, 413)
        form = {key: values[0] for key, values in parse_qs(
            self.rfile.read(int(self.headers.get("Content-Length", "0"))).decode()
        ).items()}
        provider = self.path.strip("/").split("/")[0]
        if provider not in {"microsoft", "google"}:
            return self.respond({"error": "invalid_provider"}, 404)
        if self.path.endswith("/authorize"):
            callback = urlsplit(form.get("redirect_uri", ""))
            if (callback.hostname not in {"127.0.0.1", "localhost"}
                    or form.get("client_id") != CLIENT_ID
                    or form.get("code_challenge_method") != "S256"):
                return self.respond({"error": "invalid_authorization_request"}, 400)
            code = secrets.token_urlsafe(32)
            with LOCK:
                CODES[code] = {**form, "provider": provider, "expires": time.time() + 90}
            state = "incorrect-state" if form.get("scenario") == "wrong_state" else form["state"]
            return self.redirect(form["redirect_uri"] + "?" + urlencode({"code": code, "state": state}))
        if not self.path.endswith("/token"):
            return self.respond({"error": "not_found"}, 404)
        client, secret = form.get("client_id"), form.get("client_secret")
        if self.headers.get("Authorization", "").startswith("Basic "):
            try:
                client, secret = base64.b64decode(self.headers["Authorization"][6:]).decode().split(":", 1)
            except (ValueError, UnicodeError):
                return self.respond({"error": "invalid_client"}, 401)
        if client != CLIENT_ID or secret != CLIENT_SECRET:
            return self.respond({"error": "invalid_client"}, 401)
        with LOCK:
            grant = CODES.pop(form.get("code", ""), None)
        if not grant or grant["expires"] < time.time() or grant["provider"] != provider:
            return self.respond({"error": "invalid_grant"}, 400)
        challenge = base64.urlsafe_b64encode(hashlib.sha256(
            form.get("code_verifier", "").encode()).digest()).rstrip(b"=").decode()
        if (challenge != grant["code_challenge"] or form.get("redirect_uri") != grant["redirect_uri"]
                or form.get("grant_type") != "authorization_code"):
            return self.respond({"error": "invalid_grant"}, 400)
        now = int(time.time())
        mode = grant.get("scenario", "valid")
        claims = {
            "iss": f"{INTERNAL}/{provider}", "sub": grant["subject"], "aud": CLIENT_ID,
            "iat": now, "exp": now + 300, "nonce": grant["nonce"],
            "email": grant["email"], "email_verified": mode != "unverified_email",
            "name": "Disposable identity", "preferred_username": grant["email"],
        }
        if provider == "microsoft":
            claims.update(tid=PERSONAL_TENANT if grant["profile"] == "Microsoft personal" else ORG_TENANT,
                          xms_edov=mode != "unverified_email", oid=grant["subject"])
        elif grant["profile"] == "Google Workspace":
            claims["hd"] = grant["email"].rsplit("@", 1)[1]
        if mode == "expired":
            claims.update(iat=now - 3600, exp=now - 1800)
        for scenario, claim in (("wrong_audience", "aud"), ("wrong_nonce", "nonce"), ("wrong_issuer", "iss")):
            if mode == scenario:
                claims[claim] = "https://invalid.example"
        key = RSAKey.generate_key(2048) if mode == "invalid_signature" else KEY
        token = jwt.encode({"alg": "RS256", "kid": KEY.kid}, claims, key)
        return self.respond({"access_token": "disposable-unused-access-token", "token_type": "Bearer",
                             "expires_in": 300, "id_token": token})


if __name__ == "__main__":
    ThreadingHTTPServer(("0.0.0.0", 9000), Handler).serve_forever()
