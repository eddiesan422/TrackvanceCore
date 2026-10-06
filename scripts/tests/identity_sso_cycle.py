"""Certify identity in a new disposable Docker project with one-time credentials and signed mock OIDC.

The signed mock tests our client, not Microsoft's or Google's live services.
Only sanitized aggregate results are retained; passwords and OAuth
tokens are neither printed nor published as artifacts.
"""
from __future__ import annotations

import argparse
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

from browser_evidence import run_browser
from credential_leak_probe import scan as scan_credentials
from isolation_profile import (
    assert_main_unchanged,
    isolate_compose,
    main_inventory,
    runtime_diagnostics,
)

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))
import docker_state


def available_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


def validated_project(value: str) -> str:
    if not re.fullmatch(r"trackvance-v070-test-identity-[a-f0-9]{12}", value):
        raise ValueError("Se requiere un proyecto identity-e2e aislado.")
    return value


def storage_snapshot(run, compose: list[str]) -> dict:
    """Submit the verifier anew; a recreated API has no prior /tmp helper file."""
    return json.loads(run(
        [*compose, "exec", "-T", "api", "python", "-", "snapshot"], capture=True,
        input_text=docker_state.snapshot_stdin_source(),
        stage="storage_snapshot",
    ))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--skip-build', action='store_true')
    options = parser.parse_args(argv)
    project = validated_project(f"trackvance-v070-test-identity-{uuid4().hex[:12]}")
    port, oidc_port = (available_port() for _ in range(2))
    while port == oidc_port or port == 3100 or oidc_port == 3100:
        port, oidc_port = (available_port() for _ in range(2))
    base_url = f"http://127.0.0.1:{port}"
    client_secret, database_password = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
    environment = {
        **os.environ, "PYTHONIOENCODING": "utf-8", "COMPOSE_PROJECT_NAME": project,
        "COMPOSE_FILE": "compose.yml", "WEB_PORT": str(port),
        "MOCK_OIDC_PORT": str(oidc_port), "MOCK_OIDC_CLIENT_SECRET": client_secret,
        "POSTGRES_USER": "trackvance", "POSTGRES_DB": "trackvance",
        "POSTGRES_PASSWORD": database_password, "TRACKVANCE_PUBLIC_URL": base_url,
        "TRACKVANCE_WEB_ORIGIN": base_url, "DEMO_ACCESS_ENABLED": "true", "DEMO_SEED_ENABLED": "false",
        "TV_E2E_URL": base_url, "PLAYWRIGHT_BASE_URL": base_url,
        "TV_IDENTITY_SSO_E2E": "true",
    }
    environment['TRACKVANCE_CERTIFICATION_USE_ISOLATED_IMAGES'] = 'true' if options.skip_build else 'false'
    compose = ["docker", "compose", "-p", project, "-f", "compose.yml", "-f",
               "deploy/docker/compose.identity-test.yml"]
    evidence = ROOT / ".codex-local" / "v070" / project
    evidence.mkdir(parents=True, exist_ok=False)
    compose = isolate_compose(compose, environment, evidence, project)
    started = False
    began = time.monotonic()
    result: dict = {"version": "0.8.0", "project": project, "status": "FAIL",
                    "real_providers": "NOT_RUN_EXTERNAL_CREDENTIALS", "mock_provider": True}
    main_before, outcome = None, 1

    def run(arguments, *, cwd=ROOT, capture=False, input_text=None, stage=None):
        completed = subprocess.run(arguments, cwd=cwd, env=environment, text=True,
                                   encoding="utf-8", errors="replace", input=input_text,
                                   capture_output=True, check=False)
        # A failed browser assertion may embed credentials. Never echo raw output.
        if completed.returncode:
            result["failed_stage"] = stage or arguments[0]
            result["failed_exit_code"] = completed.returncode
            if arguments[:2] == ['docker', 'compose']:
                failed_output = (completed.stdout + completed.stderr).lower()
                result['compose_failure'] = ('CONTAINER_UNHEALTHY' if 'unhealthy' in failed_output else
                                             'BUILD_FAILED' if 'failed to solve' in failed_output else
                                             'PORT_BIND_FAILED' if 'port is already allocated' in failed_output else
                                             'OTHER_COMPOSE_FAILURE')
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
        main_before = main_inventory(lambda arguments: run(arguments, capture=True))
        for kind in ("container", "volume", "network"):
            existing = run(["docker", kind, "ls", "-q", *(['-a'] if kind == 'container' else []), "--filter",
                            f"label=com.docker.compose.project={project}"], capture=True)
            if existing.strip():
                raise RuntimeError("El proyecto de prueba ya contiene recursos.")
        started = True
        run([*compose, "up", "--no-build" if options.skip_build else "--build",
             "-d", "--wait", "--wait-timeout", "300"])
        with urllib.request.urlopen(base_url + "/api/v1/health", timeout=10) as response:
            health = json.load(response)
        if health.get("version") != "0.8.0":
            raise RuntimeError("La API no ejecuta la versión objetivo.")
        run([sys.executable, "scripts/doctor.py", "--base-url", base_url, "--docker", "--project", project])
        pnpm = shutil.which("pnpm")
        if not pnpm:
            raise RuntimeError("pnpm no está disponible.")
        browser = run_browser(pnpm, ["tests-e2e/identity-sso.spec.ts"], root=ROOT,
                              project=project, environment=environment, evidence=evidence)
        if browser.get("unexpected", 0) or not browser.get("expected", 0) or browser.get("skipped", 0):
            raise RuntimeError("La suite Identity/SSO no quedó completamente ejecutada y aprobada.")
        result["playwright"] = browser
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
        if status != 201 or not probe.get("temporary_credentials"):
            raise RuntimeError("El alta no devolvió la credencial efímera.")
        issued = probe["temporary_credentials"]
        probe = probe["user"]
        expired_password = issued["temporary_password"]
        sensitive = [client_secret, expired_password]
        if len(expired_password) < 20 or not issued["must_change_password"]:
            raise RuntimeError("La credencial temporal no cumple el contrato.")
        for path in ("/users", f"/users/{probe['id']}", "/audit-events"):
            status, public = api_request("GET", path)
            if status != 200 or expired_password in json.dumps(public):
                raise RuntimeError("La lectura pública recuperó la credencial o falló.")
        # Fixture only in this newly created disposable project; never the user's data.
        fixture = ("from datetime import timedelta; from trackvance.db import SessionLocal, utcnow; "
                   "from trackvance.models import User; "
                   f"db=SessionLocal(); user=db.get(User,{probe['id']!r}); "
                   "user.temporary_password_expires_at=utcnow()-timedelta(seconds=1); db.commit(); db.close()")
        run([*compose, "exec", "-T", "api", "python", "-c", fixture], capture=True)
        status, _ = api_request("POST", "/auth/login", {"username": probe_name, "password": expired_password}, authenticated=False)
        if status != 401:
            raise RuntimeError("La contraseña temporal expirada no fue rechazada.")
        result["temporary_password_expiration"] = "PASS"
        status, regenerated = api_request("POST", f"/users/{probe['id']}/regenerate-credentials",
                                          {"version": probe["version"]}, csrf=csrf)
        if status != 200:
            raise RuntimeError("No se pudo regenerar la credencial expirada.")
        new_password = regenerated["temporary_credentials"]["temporary_password"]
        sensitive.append(new_password)
        if new_password == expired_password:
            raise RuntimeError("La regeneración no produjo una credencial nueva.")
        status, _ = api_request("POST", "/auth/login", {"username": probe_name, "password": expired_password}, authenticated=False)
        if status != 401:
            raise RuntimeError("La regeneración no invalidó la credencial previa.")
        status, restricted = api_request("POST", "/auth/login", {"username": probe_name, "password": new_password}, authenticated=False)
        if status != 200 or not restricted["user"]["must_change_password"]:
            raise RuntimeError("La nueva temporal no produce una sesión de primer acceso.")
        result["one_time_issue_and_regeneration"] = "PASS"
        result["credential_privacy"] = scan_credentials(project, sensitive)
        result["plaintext_database_and_log_scan"] = "PASS"
        before = storage_snapshot(run, compose)
        if before["tables"].get("notification_deliveries"):
            raise RuntimeError("El alta o regeneración creó registros de entrega de email.")
        result["no_notification_records_created"] = "PASS"
        run([*compose, "restart"])
        run([*compose, "up", "-d", "--wait", "--wait-timeout", "120"], stage="restart_readiness")
        after = storage_snapshot(run, compose)
        if before != after:
            raise RuntimeError("El reinicio modificó el estado persistente de identidad.")
        result.update(status="PASS", restart="PASS", migration=after["migration"],
                      tables={name: len(rows) for name, rows in after["tables"].items()})
        outcome = 0
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as error:
        result["error_type"] = type(error).__name__
        if started:
            def diagnostic_run(arguments):
                completed = subprocess.run(arguments, cwd=ROOT, env=environment, text=True,
                                           encoding='utf-8', errors='replace', capture_output=True,
                                           check=False, timeout=15)
                if completed.returncode:
                    raise RuntimeError('Diagnóstico Docker no disponible.')
                return completed.stdout
            try:
                result['runtime_diagnostics'] = runtime_diagnostics(project, diagnostic_run)
            except (OSError, ValueError, RuntimeError, subprocess.SubprocessError):
                result['runtime_diagnostics_status'] = 'UNAVAILABLE'
        print('ERROR: falló la certificación Identity/SSO; diagnóstico saneado.', file=sys.stderr)
        outcome = 1
    finally:
        if started:
            # All names are generated here and proven fresh before creation.
            try:
                run([*compose, "down", "-v", "--remove-orphans"])
                result["cleanup"] = "PASS"
            except (OSError, RuntimeError):
                result.update(status='FAIL', cleanup='FAIL')
                outcome = 1
        if main_before is not None:
            try:
                assert_main_unchanged(main_before, lambda arguments: run(arguments, capture=True))
                result['main_inventory'] = 'UNCHANGED'
            except (OSError, ValueError, RuntimeError, subprocess.SubprocessError):
                result.update(status='FAIL', main_inventory='CHANGED_OR_UNVERIFIABLE')
                outcome = 1
        result["duration_seconds"] = round(time.monotonic() - began, 3)
        (evidence / "result.json").write_text(json.dumps(result, indent=2), encoding="utf-8")
        print(json.dumps(result, indent=2), flush=True)
    return outcome


if __name__ == "__main__":
    raise SystemExit(main())
