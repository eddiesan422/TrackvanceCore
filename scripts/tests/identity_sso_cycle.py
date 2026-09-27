"""Certify identity in a new disposable Docker project with Mailpit and real OIDC.

The signed mock tests our client, not Microsoft's or Google's live services.
Only sanitized aggregate results are retained; mail bodies, passwords and OAuth
tokens are neither printed nor published as artifacts.
"""
from __future__ import annotations

import http.cookiejar
import json
import os
import re
import secrets
import shutil
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[2]


def available_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


def validated_project(value: str) -> str:
    if not re.fullmatch(r"trackvance-identity-e2e-[a-z0-9-]+", value):
        raise ValueError("Se requiere un proyecto identity-e2e aislado.")
    return value


def storage_snapshot(run, compose: list[str]) -> dict:
    """Submit the verifier anew; a recreated API has no prior /tmp helper file."""
    return json.loads(run(
        [*compose, "exec", "-T", "api", "python", "-", "snapshot"], capture=True,
        input_text=(ROOT / "scripts/verify_storage.py").read_text(encoding="utf-8"),
        stage="storage_snapshot",
    ))


def main() -> int:
    project = validated_project(f"trackvance-identity-e2e-{uuid4().hex[:12]}")
    port, mail_port, oidc_port = (available_port() for _ in range(3))
    base_url = f"http://127.0.0.1:{port}"
    mail_url = f"http://127.0.0.1:{mail_port}"
    client_secret, database_password = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
    environment = {
        **os.environ, "PYTHONIOENCODING": "utf-8", "COMPOSE_PROJECT_NAME": project,
        "COMPOSE_FILE": "compose.yml", "WEB_PORT": str(port), "MAILPIT_PORT": str(mail_port),
        "MOCK_OIDC_PORT": str(oidc_port), "MOCK_OIDC_CLIENT_SECRET": client_secret,
        "POSTGRES_USER": "trackvance", "POSTGRES_DB": "trackvance",
        "POSTGRES_PASSWORD": database_password, "TRACKVANCE_PUBLIC_URL": base_url,
        "TRACKVANCE_WEB_ORIGIN": base_url, "DEMO_ACCESS_ENABLED": "true", "DEMO_SEED_ENABLED": "false",
        "TV_E2E_URL": base_url, "PLAYWRIGHT_BASE_URL": base_url,
        "TV_MAILPIT_URL": mail_url, "TV_IDENTITY_SSO_E2E": "true",
    }
    compose = ["docker", "compose", "-p", project, "-f", "compose.yml", "-f",
               "deploy/docker/compose.mailpit-test.yml", "-f", "deploy/docker/compose.identity-test.yml"]
    evidence = ROOT / ".codex-local" / "identity-sso-e2e" / project
    evidence.mkdir(parents=True, exist_ok=False)
    started = False
    began = time.monotonic()
    result: dict = {"version": "0.6.0", "project": project, "status": "FAIL",
                    "real_providers": "NOT_RUN_EXTERNAL_CREDENTIALS", "mock_provider": True}

    def run(arguments, *, cwd=ROOT, capture=False, input_text=None, stage=None):
        completed = subprocess.run(arguments, cwd=cwd, env=environment, text=True,
                                   encoding="utf-8", errors="replace", input=input_text,
                                   capture_output=True, check=False)
        # A failed browser assertion may embed credentials. Never echo raw output.
        if completed.returncode:
            result["failed_stage"] = stage or arguments[0]
            result["failed_exit_code"] = completed.returncode
            if "playwright" in arguments:
                try:
                    report = json.loads(completed.stdout[completed.stdout.index("{"):])
                    result["playwright"] = report.get("stats", {})
                    failures = []
                    def inspect(suite):
                        for spec in suite.get("specs", []):
                            for test in spec.get("tests", []):
                                if test.get("status") != "unexpected":
                                    continue
                                for attempt in test.get("results", []):
                                    error = attempt.get("error", {})
                                    failures.append({"test": spec.get("title"),
                                                     "status": attempt.get("status"),
                                                     "location": error.get("location", {}),
                                                     "duration_ms": attempt.get("duration")})
                        for nested in suite.get("suites", []):
                            inspect(nested)
                    for suite in report.get("suites", []):
                        inspect(suite)
                    result["browser_failures"] = failures
                except (ValueError, TypeError):
                    result["browser_failure_diagnostics"] = "UNAVAILABLE"
            raise RuntimeError(f"Falló el comando de certificación ({arguments[0]}), exit {completed.returncode}.")
        if stage == "restart_readiness":
            result["restart_api_recreated"] = bool(re.search(
                r"\b" + re.escape(project) + r"-api-1\s+Recreated\b",
                completed.stdout + completed.stderr,
            ))
        if not capture:
            print("PASS: " + " ".join(arguments[:4]), flush=True)
        return completed.stdout

    try:
        run(["docker", "info", "--format", "{{.OSType}}"])
        for kind in ("container", "volume", "network"):
            existing = run(["docker", kind, "ls", "-q", "--filter",
                            f"label=com.docker.compose.project={project}"], capture=True)
            if existing.strip():
                raise RuntimeError("El proyecto de prueba ya contiene recursos.")
        started = True
        run([*compose, "up", "--build", "-d", "--wait", "--wait-timeout", "300"])
        with urllib.request.urlopen(base_url + "/api/v1/health", timeout=10) as response:
            health = json.load(response)
        if health.get("version") != "0.6.0":
            raise RuntimeError("La API no ejecuta la versión objetivo.")
        run([sys.executable, "scripts/doctor.py", "--base-url", base_url, "--docker", "--project", project])
        pnpm = shutil.which("pnpm")
        if not pnpm:
            raise RuntimeError("pnpm no está disponible.")
        browser_output = run([pnpm, "exec", "playwright", "test", "tests-e2e/identity-sso.spec.ts",
                              "--reporter=json", "--output", str(evidence / "browser-results")],
                             cwd=ROOT / "frontend", capture=True)
        browser = json.loads(browser_output[browser_output.index("{"):])
        stats = browser.get("stats", {})
        if stats.get("unexpected", 0) or not stats.get("expected", 0) or stats.get("skipped", 0):
            raise RuntimeError("La suite Identity/SSO no quedó completamente ejecutada y aprobada.")
        result["playwright"] = stats
        admin_http = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))

        def api_request(method, path, body=None, *, csrf=None, authenticated=True):
            headers = {"Content-Type": "application/json"}
            if csrf:
                headers["X-CSRF-Token"] = csrf
            request = urllib.request.Request(base_url + "/api/v1" + path, method=method,
                                             data=json.dumps(body).encode() if body is not None else None,
                                             headers=headers)
            client = admin_http if authenticated else urllib.request.build_opener()
            try:
                with client.open(request, timeout=30) as response:
                    return response.status, json.load(response)
            except urllib.error.HTTPError as error:
                return error.code, json.load(error)

        _, administrator = api_request("POST", "/auth/demo", {})
        csrf = administrator["csrf_token"]
        _, roles = api_request("GET", "/roles")
        analyst = next(role["id"] for role in roles["items"] if role["name"] == "Data Analyst")
        probe_name = "expiry." + uuid4().hex[:16]
        probe_email = probe_name + "@example.test"
        status, probe = api_request("POST", "/users", {"first_name": "Expiry", "last_name": "Probe",
                                  "username": probe_name, "email": probe_email,
                                  "role_id": analyst, "active": True}, csrf=csrf)
        if status != 201 or probe["credential_delivery"]["status"] != "SENT":
            raise RuntimeError("No se pudo preparar la prueba de expiración por email.")
        with urllib.request.urlopen(mail_url + "/api/v1/messages", timeout=10) as response:
            messages = json.load(response).get("messages", [])
        if not messages:
            raise RuntimeError("Mailpit no recibió las credenciales de prueba.")
        sensitive = [client_secret]
        expired_password = None
        for message in messages:
            with urllib.request.urlopen(mail_url + "/api/v1/message/" + message["ID"], timeout=10) as response:
                body = json.load(response).get("Text", "")
            sensitive.extend(re.findall(r"(?:Contraseña temporal|Temporary password):\s*(\S+)", body))
            if any(recipient.get("Address") == probe_email for recipient in message.get("To", [])):
                expired_password = re.search(r"Contraseña temporal:\s*(\S+)", body).group(1)
        if len(sensitive) < 2:
            raise RuntimeError("No se pudo validar el contenido de credenciales recibido.")
        if not expired_password:
            raise RuntimeError("No llegó el email de la prueba de expiración.")
        # This fixture mutation runs only in the newly created isolated test project.
        fixture = ("from datetime import timedelta; from trackvance.db import SessionLocal, utcnow; "
                   "from trackvance.models import User; "
                   f"db=SessionLocal(); user=db.get(User,{probe['id']!r}); "
                   "user.temporary_password_expires_at=utcnow()-timedelta(seconds=1); db.commit(); db.close()")
        run([*compose, "exec", "-T", "api", "python", "-c", fixture], capture=True)
        status, _ = api_request("POST", "/auth/login", {"username": probe_name, "password": expired_password}, authenticated=False)
        if status != 401:
            raise RuntimeError("La contraseña temporal expirada no fue rechazada.")
        result["temporary_password_expiration"] = "PASS"

        run([*compose, "stop", "mailpit"])
        failed_name = "smtp.failure." + uuid4().hex[:16]
        failed_email = failed_name + "@example.test"
        status, failed_user = api_request("POST", "/users", {"first_name": "SMTP", "last_name": "Failure",
                                        "username": failed_name, "email": failed_email,
                                        "role_id": analyst, "active": True}, csrf=csrf)
        if status != 201 or failed_user["credential_delivery"]["status"] != "FAILED":
            raise RuntimeError("El fallo SMTP no conservó usuario creado y metadata FAILED.")
        run([*compose, "up", "-d", "--wait", "mailpit"])
        for user in (probe, failed_user):
            status, _ = api_request("POST", f"/users/{user['id']}/resend-credentials",
                                    {"version": user["version"]}, csrf=csrf)
            if status != 200:
                raise RuntimeError("No se pudo regenerar tras fallo SMTP/expiración.")
            _, current_user = api_request("GET", f"/users/{user['id']}")
            if current_user["credential_delivery"]["status"] != "SENT":
                raise RuntimeError("Las credenciales regeneradas no se enviaron tras recuperar SMTP.")
        status, _ = api_request("POST", "/auth/login", {"username": probe_name, "password": expired_password}, authenticated=False)
        if status != 401:
            raise RuntimeError("La regeneración no invalidó la credencial previa.")
        with urllib.request.urlopen(mail_url + "/api/v1/messages", timeout=10) as response:
            messages = json.load(response).get("messages", [])
        for message in messages:
            with urllib.request.urlopen(mail_url + "/api/v1/message/" + message["ID"], timeout=10) as response:
                body = json.load(response).get("Text", "")
            sensitive.extend(re.findall(r"Contraseña temporal:\s*(\S+)", body))
            if any(recipient.get("Address") == failed_email for recipient in message.get("To", [])):
                regenerated = re.search(r"Contraseña temporal:\s*(\S+)", body).group(1)
                status, restricted = api_request("POST", "/auth/login", {"username": failed_name, "password": regenerated}, authenticated=False)
                if status != 200 or not restricted["user"]["must_change_password"]:
                    raise RuntimeError("El envío recuperado no permite únicamente el primer acceso restringido.")
        result["smtp_failure_and_regeneration"] = "PASS"
        dump = run([*compose, "exec", "-T", "postgres", "sh", "-c",
                    'pg_dump --data-only --username="$POSTGRES_USER" --dbname="$POSTGRES_DB"'], capture=True)
        logs = run([*compose, "logs", "--no-color", "api", "web", "worker", "delivery-worker"], capture=True)
        if any(secret in dump or secret in logs for secret in sensitive):
            raise RuntimeError("Se detectó persistencia o log de material sensible.")
        result["plaintext_database_and_log_scan"] = "PASS"
        result["mailpit_messages"] = len(messages)
        before = storage_snapshot(run, compose)
        run([*compose, "restart"])
        run([*compose, "up", "-d", "--wait", "--wait-timeout", "120"], stage="restart_readiness")
        after = storage_snapshot(run, compose)
        if before != after:
            raise RuntimeError("El reinicio modificó el estado persistente de identidad.")
        result.update(status="PASS", restart="PASS", migration=after["migration"],
                      tables={name: len(rows) for name, rows in after["tables"].items()})
        return 0
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as error:
        result["error"] = str(error)
        print(str(error), file=sys.stderr)
        return 1
    finally:
        if started:
            # All names are generated here and proven fresh before creation.
            try:
                run([*compose, "down", "-v", "--remove-orphans"])
                result["cleanup"] = "PASS"
            except (OSError, RuntimeError):
                result["cleanup"] = "FAIL"
        result["duration_seconds"] = round(time.monotonic() - began, 3)
        (evidence / "result.json").write_text(json.dumps(result, indent=2), encoding="utf-8")
        print(json.dumps(result, indent=2), flush=True)


if __name__ == "__main__":
    raise SystemExit(main())
