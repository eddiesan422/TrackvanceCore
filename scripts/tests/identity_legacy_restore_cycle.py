"""Build an authentic 0.5.1 installation and certify its isolated 0.6.0 restore."""
from __future__ import annotations

import json
import os
import secrets
import subprocess
import sys
import time
import zipfile
from pathlib import Path
from uuid import uuid4

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import docker_state

ROOT = Path(__file__).resolve().parents[2]
BASELINE = "4519ed354202ea8f220682758da234e07b6df3ed"


def main() -> int:
    from identity_sso_cycle import available_port

    suffix = uuid4().hex[:12]
    source, target = f"trackvance-recovery-src-051-{suffix}", f"trackvance-recovery-dst-060-{suffix}"
    evidence = ROOT / ".codex-local" / "recovery" / f"identity-legacy-{suffix}"
    evidence.mkdir(parents=True, exist_ok=False)
    baseline = evidence / "baseline"
    baseline.mkdir()
    archive = evidence / "baseline.zip"
    backup = evidence / "backup"
    port, target_port = available_port(), available_port()
    environment = {
        **os.environ, "PYTHONIOENCODING": "utf-8", "POSTGRES_PASSWORD": secrets.token_urlsafe(32),
        "POSTGRES_USER": "trackvance", "POSTGRES_DB": "trackvance", "DEMO_ACCESS_ENABLED": "true",
        "DEMO_SEED_ENABLED": "true", "WEB_PORT": str(port),
        "TRACKVANCE_WEB_ORIGIN": f"http://127.0.0.1:{port}", "COMPOSE_FILE": "compose.yml",
    }
    # Operational tooling inherits this process environment for its fresh projects.
    os.environ.update(environment)
    compose = ["docker", "compose", "-p", source, "-f", str(baseline / "compose.yml")]
    started = False
    target_claimed = False
    began = time.monotonic()
    result = {"status": "FAIL", "baseline_commit": BASELINE, "source_version": "0.5.1",
              "target_version": "0.6.0"}

    def run(arguments, *, cwd=ROOT):
        completed = subprocess.run(arguments, env=environment, cwd=cwd, capture_output=True,
                                   text=True, encoding="utf-8", errors="replace", check=False)
        if completed.returncode:
            # Fixed disposable credential is redacted before reporting diagnostic text.
            detail = (completed.stdout + completed.stderr).replace(environment["POSTGRES_PASSWORD"], "[REDACTED]")
            raise RuntimeError(detail[-5000:])
        return completed.stdout

    try:
        docker_state.ensure_fresh_project(source)
        docker_state.ensure_fresh_project(target)
        run(["git", "archive", "--format=zip", "--output", str(archive), BASELINE])
        with zipfile.ZipFile(archive) as bundle:
            for entry in bundle.infolist():
                if not (baseline / entry.filename).resolve().is_relative_to(baseline.resolve()):
                    raise ValueError("Ruta de baseline fuera del directorio privado.")
            bundle.extractall(baseline)
        started = True
        run([*compose, "up", "--build", "-d", "--wait", "--wait-timeout", "300"], cwd=baseline)
        run([sys.executable, str(baseline / "scripts/smoke_test.py"), "--base-url",
             f"http://127.0.0.1:{port}"], cwd=baseline)
        docker_state.backup(source, backup)
        manifest = docker_state.verify_backup(backup)
        before = json.loads((backup / "state.json").read_text(encoding="utf-8"))
        if manifest["migration"] != "0009_delivery_reviews" or before["schema_version"] != 4:
            raise ValueError("La fuente no es una instalación auténtica 0.5.1.")
        # Remove ONLY this freshly created source, proving recovery is independent.
        run([*compose, "down", "-v", "--remove-orphans"], cwd=baseline)
        started = False
        docker_state.ensure_fresh_project(target)
        target_claimed = True
        receipt = docker_state.restore(backup, target, start=True, web_port=target_port)
        restored = docker_state.inventory(target)
        api = next(item for item in restored["containers"] if item["service"] == "api")
        after = docker_state._copy_snapshot(str(api["id"]), evidence / "restored-state.json")
        normalized = docker_state._copy_snapshot(str(api["id"]), evidence / "legacy-state.json",
                                                 command="snapshot-legacy-v4")
        if normalized != before:
            raise ValueError("El restore modificó datos históricos de 0.5.1.")
        result.update(status="PASS", source_state_sha256=docker_state.canonical_hash(before),
                      restored_legacy_sha256=docker_state.canonical_hash(normalized),
                      exact_historical_state="PASS", migration=after["migration"],
                      artifacts=after["verified_artifacts"], source_secrets=after["verified_source_secrets"],
                      delivery_secrets=after["verified_delivery_secrets"], restore=receipt["status"],
                      roles=len(after["tables"]["roles"]), users=len(after["tables"]["users"]))
        return 0
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as error:
        result["error"] = str(error)
        return 1
    finally:
        if started:
            run([*compose, "down", "-v", "--remove-orphans"], cwd=baseline)
        # Names are generated by this runner and rejected up front when preexisting.
        if target_claimed:
            run(["docker", "compose", "-p", target, "-f", str(ROOT / "compose.yml"),
                 "down", "-v", "--remove-orphans"])
        result["duration_seconds"] = round(time.monotonic() - began, 3)
        (evidence / "result.json").write_text(json.dumps(result, indent=2), encoding="utf-8")
        print(json.dumps(result, indent=2), flush=True)


if __name__ == "__main__":
    raise SystemExit(main())
