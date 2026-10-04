#!/usr/bin/env python3
"""Run the destructive Docker E2E cycle in an isolated Compose project.

The generated project name owns its PostgreSQL, ArtifactStore and secret volumes.
The runner removes only that project, even when a command fails, so the developer's
normal Trackvance installation and data are never selected for cleanup.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import secrets
import shutil
import socket
import subprocess
import sys
import time
import uuid
from pathlib import Path

from browser_evidence import run_browser
from isolation_profile import assert_main_unchanged, isolate_compose, main_inventory

ROOT = Path(__file__).resolve().parents[2]
PROJECT_PREFIX = "trackvance-v070-test-e2e-"


def available_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


def execute(
    arguments: list[str],
    *,
    environment: dict[str, str],
    cwd: Path = ROOT,
    capture: bool = False,
    input_text: str | None = None,
) -> str:
    print("+ " + " ".join(arguments), flush=True)
    result = subprocess.run(
        arguments,
        cwd=cwd,
        env=environment,
        check=False,
        text=True,
        capture_output=True,
        input=input_text,
        encoding="utf-8",
    )
    if result.returncode:
        raise subprocess.CalledProcessError(result.returncode, arguments)
    if any(value and value in result.stdout + result.stderr for key, value in environment.items()
           if key in {"POSTGRES_PASSWORD", "MOCK_OIDC_CLIENT_SECRET"}):
        raise RuntimeError("Se detectó una credencial en la salida; contenido suprimido.")
    return result.stdout if capture else ""


def storage_snapshot(compose: list[str], environment: dict[str, str]) -> str:
    """The API may be recreated on readiness; submit the verifier each time."""
    return execute([*compose, "exec", "-T", "api", "python", "-", "snapshot"],
                   environment=environment, capture=True,
                   input_text=(ROOT / "scripts/verify_storage.py").read_text(encoding="utf-8"))


def validated_project_name(value: str) -> str:
    if not re.fullmatch(r"trackvance-v070-test-e2e-[a-z0-9-]*[a-f0-9]{12}", value):
        raise ValueError(
            f"El proyecto aislado debe comenzar por {PROJECT_PREFIX!r} y usar minúsculas."
        )
    return value


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=0, help="Puerto web; 0 busca uno libre.")
    parser.add_argument("--skip-build", action="store_true")
    parser.add_argument("--clean-demo", action="store_true", help="Certifica acceso demo sin datos sembrados.")
    parser.add_argument("--skip-playwright", action="store_true")
    parser.add_argument("--evidence-dir", type=Path, help="Directorio para las instantáneas de persistencia.")
    parser.add_argument(
        "--project",
        default=f"{PROJECT_PREFIX}{uuid.uuid4().hex[:12]}",
    )
    options = parser.parse_args()
    try:
        project = validated_project_name(options.project)
    except ValueError as error:
        parser.error(str(error))
    port = options.port or available_port()
    if not 1 <= port <= 65535 or port == 3100:
        parser.error("--port debe estar entre 1 y 65535 y no seleccionar 3100")

    environment = os.environ.copy()
    base_url = f"http://127.0.0.1:{port}"
    environment.update(
        {
            "COMPOSE_PROJECT_NAME": project,
            "WEB_PORT": str(port),
            "TRACKVANCE_WEB_ORIGIN": base_url,
            "POSTGRES_PASSWORD": secrets.token_urlsafe(36),
            "DEMO_ACCESS_ENABLED": "true",
            "DEMO_SEED_ENABLED": "false" if options.clean_demo else "true",
            "TV_EXPECT_CLEAN_DEMO": "true" if options.clean_demo else "false",
            "TV_E2E_URL": base_url,
            "PLAYWRIGHT_BASE_URL": base_url,
        }
    )
    compose = ["docker", "compose", "-p", project, "-f", "compose.yml"]
    evidence = options.evidence_dir or ROOT / ".codex-local" / "v070" / project
    evidence.mkdir(parents=True, exist_ok=True)
    environment['TRACKVANCE_CERTIFICATION_USE_ISOLATED_IMAGES'] = 'true' if options.skip_build else 'false'
    compose = isolate_compose(compose, environment, evidence, project)
    started = False
    began = time.monotonic()
    outcome = 1
    result = {"version": "0.7.0", "status": "FAIL", "project": project, "port": port,
              "demo_seed_enabled": not options.clean_demo}
    inventory_run = lambda arguments: execute(arguments, environment=environment, capture=True)
    main_before = None
    try:
        execute(["docker", "info", "--format", "{{.OSType}}"], environment=environment)
        main_before = main_inventory(inventory_run)
        existing = execute(
            ["docker", "ps", "-aq", "--filter", f"label=com.docker.compose.project={project}"],
            environment=environment,
            capture=True,
        ).strip()
        volumes = execute(
            ["docker", "volume", "ls", "-q", "--filter", f"label=com.docker.compose.project={project}"],
            environment=environment,
            capture=True,
        ).strip()
        networks = execute(
            ["docker", "network", "ls", "-q", "--filter", f"label=com.docker.compose.project={project}"],
            environment=environment,
            capture=True,
        ).strip()
        if existing or volumes or networks:
            raise RuntimeError(f"El proyecto {project} ya tiene recursos; usa otro nombre aislado.")
        up = [*compose, "up", "-d", "--wait"]
        if not options.skip_build:
            up.insert(-2, "--build")
        else:
            up.insert(-2, "--no-build")
        started = True
        execute(up, environment=environment)
        execute([*compose, 'exec', '-T', 'api', 'python', '-c',
                 "import trackvance; assert trackvance.__version__ == '0.7.0'"], environment=environment)
        execute(
            [sys.executable, "scripts/doctor.py", "--base-url", base_url, "--docker", "--project", project],
            environment=environment,
        )
        execute(
            [*compose, "cp", "scripts/check_postgres_migrations.py", "api:/tmp/check_postgres_migrations.py"],
            environment=environment,
        )
        migrations = execute(
            [*compose, "exec", "-T", "api", "python", "/tmp/check_postgres_migrations.py"],
            environment=environment,
            capture=True,
        )
        (evidence / "migrations.json").write_text(migrations, encoding="utf-8")
        if not options.clean_demo:
            execute([sys.executable, "scripts/smoke_test.py", "--base-url", base_url],
                    environment=environment)
        if not options.skip_playwright:
            pnpm = shutil.which("pnpm")
            if not pnpm:
                raise RuntimeError("pnpm no está disponible para ejecutar Playwright.")
            arguments = ["tests-e2e/demo-access-clean.spec.ts"] if options.clean_demo else []
            browser = run_browser(pnpm, arguments, root=ROOT, project=project,
                                  environment=environment, evidence=evidence)
            result["browser"] = browser

        before = evidence / "before.json"
        after = evidence / "after.json"
        execute([*compose, 'stop', 'web', 'scheduler', 'events-notifications', 'events-chaining',
                 'worker', 'delivery-worker', 'acquisition-worker'], environment=environment)
        before.write_text(storage_snapshot(compose, environment), encoding="utf-8")
        execute([*compose, "restart", 'api', 'postgres'], environment=environment)
        execute([*compose, "up", "-d", "--wait", 'api', 'postgres'], environment=environment)
        after.write_text(storage_snapshot(compose, environment), encoding="utf-8")
        execute(
            [sys.executable, "scripts/verify_storage.py", "compare", str(before), str(after)],
            environment=environment,
        )

        container_ids = execute(
            [*compose, "ps", "-aq"], environment=environment, capture=True
        ).split()
        restart_policies = {
            execute(
                [
                    "docker",
                    "inspect",
                    "--format",
                    "{{.HostConfig.RestartPolicy.Name}}",
                    container_id,
                ],
                environment=environment,
                capture=True,
            ).strip()
            for container_id in container_ids
        }
        if not container_ids or restart_policies != {"no"}:
            raise RuntimeError(
                f"Políticas de reinicio inesperadas para {project}: {restart_policies}"
            )
        result.update(status="PASS", smoke="NOT_RUN_CLEAN_DEMO" if options.clean_demo else "PASS",
                      playwright="SKIPPED" if options.skip_playwright else "PASS",
                      migrations="PASS", persistence_after_restart="PASS",
                      persistence_components_quiesced=True,
                      restart_policies=sorted(restart_policies))
        print(
            f"OK: ciclo aislado, Playwright/smoke y persistencia tras reinicio en {project}.",
            flush=True,
        )
        outcome = 0
    except (OSError, RuntimeError, subprocess.CalledProcessError) as error:
        result["error_type"] = type(error).__name__
        print("ERROR: falló la certificación aislada; consultar el resumen saneado.", file=sys.stderr, flush=True)
    finally:
        if started:
            try:
                execute([*compose, "down", "-v", "--remove-orphans"], environment=environment)
                result["cleanup"] = "PASS"
            except (OSError, RuntimeError, subprocess.CalledProcessError):
                result.update(status="FAIL", cleanup="FAIL")
                outcome = 1
        else:
            result["cleanup"] = "NOT_STARTED"
        if main_before is not None:
            try:
                assert_main_unchanged(main_before, inventory_run)
                result['main_inventory'] = 'UNCHANGED'
            except (OSError, RuntimeError, ValueError, subprocess.SubprocessError):
                result.update(status='FAIL', main_inventory='CHANGED_OR_UNVERIFIABLE')
                outcome = 1
        result["duration_seconds"] = round(time.monotonic() - began, 3)
        (evidence / "result.json").write_text(json.dumps(result, indent=2), encoding="utf-8")
    return outcome


if __name__ == "__main__":
    raise SystemExit(main())
