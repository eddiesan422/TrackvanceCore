#!/usr/bin/env python3
"""Run the destructive Docker E2E cycle in an isolated Compose project.

The generated project name owns its PostgreSQL and ArtifactStore volumes. The
runner removes only that project, even when a command fails, so the developer's
normal Trackvance installation and data are never selected for cleanup.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
PROJECT_PREFIX = "trackvance-e2e-"


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
) -> str:
    print("+ " + " ".join(arguments), flush=True)
    result = subprocess.run(
        arguments,
        cwd=cwd,
        env=environment,
        check=True,
        text=True,
        stdout=subprocess.PIPE if capture else None,
    )
    return result.stdout if capture else ""


def validated_project_name(value: str) -> str:
    if not re.fullmatch(r"trackvance-e2e-[a-z0-9-]+", value):
        raise ValueError(
            f"El proyecto aislado debe comenzar por {PROJECT_PREFIX!r} y usar minúsculas."
        )
    return value


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=0, help="Puerto web; 0 busca uno libre.")
    parser.add_argument("--skip-build", action="store_true")
    parser.add_argument("--skip-playwright", action="store_true")
    parser.add_argument("--evidence-dir", type=Path, help="Directorio para las instantáneas de persistencia.")
    parser.add_argument(
        "--project",
        default=f"{PROJECT_PREFIX}{os.getpid()}-{uuid.uuid4().hex[:6]}",
    )
    options = parser.parse_args()
    try:
        project = validated_project_name(options.project)
    except ValueError as error:
        parser.error(str(error))
    port = options.port or available_port()
    if not 1 <= port <= 65535:
        parser.error("--port debe estar entre 1 y 65535")

    environment = os.environ.copy()
    base_url = f"http://127.0.0.1:{port}"
    environment.update(
        {
            "COMPOSE_PROJECT_NAME": project,
            "WEB_PORT": str(port),
            "TRACKVANCE_WEB_ORIGIN": base_url,
            "POSTGRES_PASSWORD": "trackvance-isolated-e2e",
            "DEMO_ACCESS_ENABLED": "true",
            "DEMO_SEED_ENABLED": "true",
            "TV_E2E_URL": base_url,
        }
    )
    compose = ["docker", "compose", "-p", project]
    evidence = options.evidence_dir or ROOT / ".codex-local" / "architecture-e2e" / project
    evidence.mkdir(parents=True, exist_ok=True)
    started = False
    try:
        execute(["docker", "info", "--format", "{{.OSType}}"], environment=environment)
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
        if existing or volumes:
            raise RuntimeError(f"El proyecto {project} ya tiene recursos; usa otro nombre aislado.")
        up = [*compose, "up", "-d", "--wait"]
        if not options.skip_build:
            up.insert(-2, "--build")
        started = True
        execute(up, environment=environment)
        execute(
            [sys.executable, "scripts/doctor.py", "--base-url", base_url, "--docker"],
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
        execute(
            [sys.executable, "scripts/smoke_test.py", "--base-url", base_url],
            environment=environment,
        )
        if not options.skip_playwright:
            pnpm = shutil.which("pnpm")
            if not pnpm:
                raise RuntimeError("pnpm no está disponible para ejecutar Playwright.")
            execute(
                [pnpm, "test:e2e"],
                environment=environment,
                cwd=ROOT / "frontend",
            )

        before = evidence / "before.json"
        after = evidence / "after.json"
        execute(
            [*compose, "cp", "scripts/verify_storage.py", "api:/tmp/verify_storage.py"],
            environment=environment,
        )
        snapshot = [*compose, "exec", "-T", "api", "python", "/tmp/verify_storage.py", "snapshot"]
        before.write_text(execute(snapshot, environment=environment, capture=True), encoding="utf-8")
        execute([*compose, "restart"], environment=environment)
        execute([*compose, "up", "-d", "--wait"], environment=environment)
        after.write_text(execute(snapshot, environment=environment, capture=True), encoding="utf-8")
        execute(
            [sys.executable, "scripts/verify_storage.py", "compare", str(before), str(after)],
            environment=environment,
        )

        container_ids = execute(
            [*compose, "ps", "-q"], environment=environment, capture=True
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
        (evidence / "result.json").write_text(
            json.dumps({"status": "PASS", "project": project, "port": port,
                        "smoke": "PASS", "playwright": "SKIPPED" if options.skip_playwright else "PASS",
                        "migrations": "PASS", "persistence_after_restart": "PASS",
                        "restart_policies": sorted(restart_policies)}, indent=2),
            encoding="utf-8",
        )
        print(
            f"OK: ciclo aislado, Playwright/smoke y persistencia tras reinicio en {project}.",
            flush=True,
        )
        return 0
    except (OSError, RuntimeError, subprocess.CalledProcessError) as error:
        print(f"ERROR: {error}", file=sys.stderr, flush=True)
        return 1
    finally:
        if started:
            execute(
                [*compose, "down", "-v", "--remove-orphans"],
                environment=environment,
            )


if __name__ == "__main__":
    raise SystemExit(main())
