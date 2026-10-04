"""Build authentic historical installations and certify isolated 0.7.0 restores."""
from __future__ import annotations

import argparse
import json
import os
import secrets
import subprocess
import sys
import time
import urllib.request
import zipfile
from pathlib import Path
from uuid import uuid4

from docker_backup_cycle import (
    RecoveryApi,
    assert_no_secrets,
    available_port,
    notification_count,
    scan_backup_plaintext,
)
from isolation_profile import assert_main_unchanged, main_inventory

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import docker_state

ROOT = Path(__file__).resolve().parents[2]
SOURCES = {
    "0.5.1": ("4519ed354202ea8f220682758da234e07b6df3ed", "0009_delivery_reviews", 4),
    "0.6.0": ("587909bc4462683e87e403dd2ea29a1d6d4afe08", "0012_delivery_target_audit", 5),
    "0.6.1": ("6fac26b3648cb4a4b50c094ef12c1e103bc97ddd", "0012_delivery_target_audit", 5),
}
TARGET_VERSION = "0.7.0"


def health_version(port: int) -> str:
    with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/v1/health", timeout=30) as response:
        return str(json.load(response)["version"])


def seed_060_history(api: RecoveryApi, run, compose: list[str], password: str) -> None:
    """Historical notification is produced by the real 0.6.0 API; no mail server."""
    roles = api.json("GET", "/roles")["items"]
    role = next(item for item in roles if item["name"] == "Data Analyst")
    user = api.json("POST", "/users", {
        "first_name": "Legacy", "last_name": "Recovery", "username": "legacy.recovery",
        "email": "legacy.recovery@trackvance.test", "role_id": role["id"], "active": True,
    }, expected=201)
    if user["credential_delivery"]["status"] != "FAILED":
        raise ValueError("La fuente 0.6.0 no produjo la entrega histórica esperada.")
    destination = api.json("POST", "/delivery/destinations", {
        "name": "Legacy recovery policy fixture", "sink_type": "POSTGRESQL",
        "host": "postgres", "port": 5432, "database": "trackvance",
        "username": "trackvance", "password": password,
        "options": {"sslmode": "disable", "connect_timeout": 3, "query_timeout": 15},
    }, expected=201)
    # Explicit synthetic persistence fixtures, not provider authentication or SQL materialization.
    script = '''
import hashlib
import json
from datetime import timedelta
from trackvance.db import SessionLocal, utcnow
from trackvance.delivery_audit import target_identity
from trackvance.models import DeliveryTargetPolicy, ExternalIdentity, OIDCLoginAttempt, User
identity = json.loads(INPUT)
with SessionLocal() as db:
    user = db.get(User, identity["user_id"])
    now = utcnow()
    db.add(ExternalIdentity(organization_id=user.organization_id, user_id=user.id,
        provider="GOOGLE", issuer="https://accounts.google.com", subject="legacy-recovery-subject",
        email_at_link=user.email, linked_at=now, last_login_at=now))
    db.add(OIDCLoginAttempt(state_hash=hashlib.sha256(b"legacy-consumed-state").hexdigest(),
        browser_hash=hashlib.sha256(b"legacy-browser-binding").hexdigest(), provider="GOOGLE",
        nonce="", code_verifier="", expires_at=now + timedelta(minutes=10), consumed_at=now))
    fingerprint, locator = target_identity(user.organization_id, "POSTGRESQL",
        {"host": "postgres", "port": 5432, "database": "trackvance"},
        {"schema_name": "legacy_fixture", "table_name": "not_materialized"})
    db.add(DeliveryTargetPolicy(organization_id=user.organization_id,
        destination_id=identity["destination_id"], target_fingerprint=fingerprint,
        sink_type="POSTGRESQL", host_snapshot=locator["host"], port=locator["port"],
        database=locator["database"], schema_name=locator["schema_name"], table_name=locator["table_name"],
        audit_columns_required=True, enabled_at=now, enabled_by_user_id=user.id,
        enabled_by_username=user.username))
    db.commit()
'''.replace("INPUT", repr(json.dumps({"user_id": user["id"], "destination_id": destination["id"]})))
    run([*compose, "exec", "-T", "api", "python", "-"], input_text=script)


def certify_061_credentials(api: RecoveryApi, compose: list[str], environment: dict[str, str],
                            credentials: tuple[str, ...]) -> tuple[dict, tuple[str, ...]]:
    before = notification_count(compose, environment, credentials)
    roles = api.json("GET", "/roles")["items"]
    role = next(item for item in roles if item["name"] == "Data Analyst")
    issued = api.json("POST", "/users", {
        "first_name": "Restored", "last_name": "Current", "username": "restored.current",
        "email": "restored.current@trackvance.test", "role_id": role["id"], "active": True,
    }, expected=201)
    user = issued["user"]
    temporary = issued["temporary_credentials"]["temporary_password"]
    credentials += (temporary,)
    api.credentials = credentials
    replacement = api.json("POST", f"/users/{user['id']}/regenerate-credentials",
                           {"version": user["version"]})
    renewed = replacement["temporary_credentials"]["temporary_password"]
    if len(temporary) < 20 or len(renewed) < 20 or temporary == renewed:
        raise ValueError("La credencial efímera restaurada no cumple la rotación esperada.")
    credentials += (renewed,)
    api.credentials = credentials
    del issued, replacement, temporary, renewed
    api.json("GET", "/users")
    api.json("GET", f"/users/{user['id']}")
    api.json("GET", "/audit-events")
    after = notification_count(compose, environment, credentials)
    if before != after:
        raise ValueError("Alta o regeneración 0.6.1 modificó el número de notificaciones históricas.")
    return {"status": "PASS", "notifications_before": before, "notifications_after": after,
            "create_and_regenerate_without_email": True, "get_and_audit_no_plaintext": True}, credentials


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-version", choices=SOURCES, default="0.6.1")
    parser.add_argument("--evidence-dir", type=Path)
    options = parser.parse_args()
    if options.source_version == "0.6.1":
        from v070_recovery import authentic_061_cycle

        return authentic_061_cycle(SOURCES["0.6.1"][0], options.evidence_dir)
    from v070_recovery import compose_adapter, guarded_project, target_override

    baseline_commit, expected_migration, expected_state = SOURCES[options.source_version]
    suffix = uuid4().hex[:12]
    source = guarded_project(f"trackvance-v070-test-legacy{options.source_version.replace('.', '')}-src-{suffix}")
    target = guarded_project(f"trackvance-v070-test-legacy{options.source_version.replace('.', '')}-dst-{suffix}")
    evidence = (options.evidence_dir or ROOT / ".codex-local" / "recovery" / f"identity-legacy-{suffix}").resolve()
    evidence.mkdir(parents=True, exist_ok=False)
    baseline = evidence / "baseline"
    baseline.mkdir()
    archive, backup = evidence / "baseline.zip", evidence / "backup"
    port, target_port = available_port(), available_port()
    while target_port == port:
        target_port = available_port()
    environment = {
        **os.environ, "PYTHONIOENCODING": "utf-8", "POSTGRES_PASSWORD": secrets.token_urlsafe(32),
        "POSTGRES_USER": "tv_v070_test", "POSTGRES_DB": "tv_v070_test", "DEMO_ACCESS_ENABLED": "true",
        "DEMO_SEED_ENABLED": "true", "WEB_PORT": str(port), "TRACKVANCE_SMTP_ENABLED": "false",
        "TRACKVANCE_SSO_MICROSOFT_ENABLED": "false", "TRACKVANCE_SSO_GOOGLE_ENABLED": "false",
        "TRACKVANCE_WEB_ORIGIN": f"http://127.0.0.1:{port}", "COMPOSE_FILE": "compose.yml",
    }
    original_environment = dict(os.environ)
    os.environ.update(environment)
    credentials = (environment["POSTGRES_PASSWORD"],)
    source_env_file, target_env_file = evidence / "source.env", evidence / "restore.env"
    private_values = {key: environment[key] for key in ("POSTGRES_PASSWORD", "POSTGRES_USER", "POSTGRES_DB",
        "DEMO_ACCESS_ENABLED", "DEMO_SEED_ENABLED", "WEB_PORT", "TRACKVANCE_SMTP_ENABLED",
        "TRACKVANCE_SSO_MICROSOFT_ENABLED", "TRACKVANCE_SSO_GOOGLE_ENABLED", "TRACKVANCE_WEB_ORIGIN")}
    source_env_file.write_text("\n".join(f"{key}={value}" for key, value in private_values.items()) + "\n", encoding="utf-8")
    restored_values = {**private_values, "WEB_PORT": str(target_port), "DEMO_SEED_ENABLED": "false",
                       "DEMO_ACCESS_ENABLED": "false", "TRACKVANCE_WEB_ORIGIN": f"http://127.0.0.1:{target_port}"}
    target_env_file.write_text("\n".join(f"{key}={value}" for key, value in restored_values.items()) + "\n", encoding="utf-8")
    profile = evidence / "source-compose.json"
    source_services = {name: {"cpus": 1, "mem_limit": "1g", "pids_limit": 256}
                       for name in ("postgres", "api", "worker", "delivery-worker")}
    source_services["web"] = {"cpus": 0.25, "mem_limit": "128m", "pids_limit": 64}
    for name in ("api", "worker", "delivery-worker", "web"):
        source_services[name]["image"] = f'{source}:{"web" if name == "web" else "backend"}'
    profile.write_text(json.dumps({"services": source_services}), encoding="utf-8")
    override = target_override(evidence, target)
    compose = ["docker", "compose", "--env-file", str(source_env_file), "-p", source,
               "-f", str(baseline / "compose.yml"), "-f", str(profile)]
    target_compose = ["docker", "compose", "--env-file", str(target_env_file), "-p", target,
                      "-f", str(ROOT / "compose.yml"), "-f", str(override)]
    old_compose = docker_state.compose
    started = target_claimed = False
    began = time.monotonic()
    result = {"status": "FAIL", "baseline_commit": baseline_commit,
              "source_version": options.source_version, "target_version": TARGET_VERSION,
              "source_project": source, "target_project": target}
    stage = "freshness"
    main_before = None

    def run(arguments, *, cwd=ROOT, input_text=None):
        assert_no_secrets(" ".join(arguments), credentials)
        completed = subprocess.run(arguments, env=environment, cwd=cwd, capture_output=True,
                                   text=True, encoding="utf-8", errors="replace", check=False,
                                   timeout=1800, input=input_text)
        assert_no_secrets(completed.stdout + completed.stderr, credentials)
        if completed.returncode:
            raise RuntimeError(f"Comando de recovery falló con exit {completed.returncode}; salida omitida.")
        return completed.stdout

    try:
        main_before = main_inventory(run)
        docker_state.ensure_fresh_project(source)
        docker_state.ensure_fresh_project(target)
        stage = "authentic_source_build"
        run(["git", "archive", "--format=zip", "--output", str(archive), baseline_commit])
        with zipfile.ZipFile(archive) as bundle:
            for entry in bundle.infolist():
                if not (baseline / entry.filename).resolve().is_relative_to(baseline.resolve()):
                    raise ValueError("Ruta de baseline fuera del directorio privado.")
            bundle.extractall(baseline)
        started = True
        run([*compose, "up", "--build", "-d", "--wait", "--wait-timeout", "300"], cwd=baseline)
        if health_version(port) != options.source_version:
            raise ValueError("El runtime fuente no corresponde a la versión auténtica solicitada.")
        stage = "source_fixtures"
        run([sys.executable, str(baseline / "scripts/smoke_test.py"), "--base-url",
             f"http://127.0.0.1:{port}"], cwd=baseline)
        if options.source_version == "0.6.0":
            seed_060_history(RecoveryApi(port, credentials), run, compose, environment["POSTGRES_PASSWORD"])
            result["notification_fixture"] = "AUTHENTIC_060_CREATE_USER_API_FAILED_NO_PROVIDER_NO_SMTP"
            result["identity_policy_fixture"] = "SYNTHETIC_EXTERNAL_LINK_CONSUMED_OIDC_AND_UNMATERIALIZED_POLICY"
        stage = "source_backup"
        docker_state.backup(source, backup)
        manifest = docker_state.verify_backup(backup)
        before = json.loads((backup / "state.json").read_text(encoding="utf-8"))
        if manifest["migration"] != expected_migration or before["schema_version"] != expected_state:
            raise ValueError("El schema fuente no corresponde a su versión histórica.")
        result["source_backup_scan"] = scan_backup_plaintext(backup, compose, environment, credentials)
        if options.source_version == "0.6.0":
            for table in ("notification_deliveries", "external_identities", "oidc_login_attempts", "delivery_target_policies"):
                if not before["tables"][table]:
                    raise ValueError("La fixture histórica 0.6.0 está incompleta.")
        run([*compose, "down", "-v", "--remove-orphans"], cwd=baseline)
        started = False
        stage = "restore"
        docker_state.ensure_fresh_project(target)
        docker_state.compose = compose_adapter(target, target_env_file, override, restored_values)
        target_claimed = True
        receipt = docker_state.restore(backup, target, start=False, web_port=target_port)
        environment.update(restored_values)
        os.environ.update(environment)
        run([*target_compose, "up", "-d", "--wait", "api", "web"])
        if health_version(target_port) != TARGET_VERSION:
            raise ValueError("La restauración no ejecuta 0.7.0.")
        restored = docker_state.inventory(target)
        api_container = next(item for item in restored["containers"] if item["service"] == "api")
        after = docker_state._copy_snapshot(str(api_container["id"]), evidence / "restored-state.json")
        normalized = docker_state._copy_snapshot(str(api_container["id"]), evidence / "legacy-state.json",
            command="snapshot-legacy-v5" if expected_state == 5 else "snapshot-legacy-v4")
        if normalized != before:
            raise ValueError("El restore modificó los datos históricos.")
        result.update(source_state_sha256=docker_state.canonical_hash(before),
                      restored_legacy_sha256=docker_state.canonical_hash(normalized),
                      exact_historical_state="PASS", migration=after["migration"],
                      state_schema_version=after["schema_version"], artifacts=after["verified_artifacts"],
                      source_secrets=after["verified_source_secrets"], delivery_secrets=after["verified_delivery_secrets"],
                      restore=receipt["status"], roles=len(after["tables"]["roles"]),
                      users=len(after["tables"]["users"]),
                      historical_notifications=len(after["tables"]["notification_deliveries"]))
        # Restore deliberately disables demo access unless --smoke is selected.
        # Compare untouched historical state first, then enable access only in
        # this disposable target without running a mutating business smoke.
        stage = "enable_disposable_access"
        environment.update(WEB_PORT=str(target_port),
                           TRACKVANCE_WEB_ORIGIN=f"http://127.0.0.1:{target_port}",
                           DEMO_SEED_ENABLED="false", DEMO_ACCESS_ENABLED="true")
        os.environ.update(environment)
        run([*target_compose, "up", "-d", "--wait", "--wait-timeout", "180", "api", "web"])
        stage = "current_credentials"
        report, credentials = certify_061_credentials(
            RecoveryApi(target_port, credentials), target_compose, environment, credentials)
        result["current_credentials"] = report
        stage = "current_backup_privacy"
        current_backup = evidence / "post-061-backup"
        docker_state.backup(target, current_backup)
        docker_state.verify_backup(current_backup)
        result["current_backup_scan"] = scan_backup_plaintext(current_backup, target_compose, environment, credentials)
        run([*target_compose, "logs", "--no-color"])
        result.update(status="PASS", log_secret_scan="PASS", source_destroyed_before_restore=True,
                      automatic_processes_started=False)
    except (OSError, ValueError, RuntimeError, KeyError, subprocess.SubprocessError) as error:
        result.update(failed_stage=stage, error_type=type(error).__name__)
    finally:
        docker_state.compose = old_compose
        cleanup_failed = False
        for claimed, command, directory in ((started, compose, baseline), (target_claimed, target_compose, ROOT)):
            if claimed:
                try:
                    run([*command, "down", "-v", "--remove-orphans"], cwd=directory)
                except (OSError, RuntimeError, subprocess.SubprocessError):
                    cleanup_failed = True
        if cleanup_failed:
            result.update(status="FAIL", cleanup="FAIL")
        else:
            result["cleanup"] = "PASS"
        if main_before is not None:
            try:
                assert_main_unchanged(main_before, run)
                result["main_inventory"] = "UNCHANGED"
            except (OSError, ValueError, RuntimeError, subprocess.SubprocessError):
                result.update(status="FAIL", main_inventory="CHANGED_OR_UNVERIFIABLE")
        os.environ.clear()
        os.environ.update(original_environment)
        result["duration_seconds"] = round(time.monotonic() - began, 3)
        serialized = json.dumps(result, indent=2)
        assert_no_secrets(serialized, credentials)
        (evidence / "result.json").write_text(serialized, encoding="utf-8")
        print(serialized, flush=True)
    return 0 if result["status"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
