"""Windows launches the unchanged backend checks in an immutable Linux image."""
from __future__ import annotations

import json
import os
import shutil
from pathlib import Path


def test_context(root: Path, directory: Path, sha: str, execute, environment=None) -> Path:
    context = directory / "backend-test-context"
    execute(["git", "clone", "--no-local", "--depth", "1", "--no-hardlinks", str(root), str(context)],
            directory, "backend-test-source-clone", 180, environment=environment)
    execute(["git", "checkout", "--detach", sha], directory, "backend-test-source-checkout", 60,
            cwd=context, environment=environment)
    # Docker excludes .git by default. This is a fresh source-only clone, with no
    # worktree settings, secrets or ignored files copied from the working host.
    shutil.move(str(context / ".git"), context / ".ci-source-git")
    runtime = (context / "backend/Dockerfile").read_text(encoding="utf-8")
    marker = "FROM python:3.12-slim-bookworm"
    if runtime.count(marker) != 1:
        raise ValueError("The committed Linux runtime base must be unambiguous")
    fragment = (context / "deploy/docker/backend-tests.Dockerfile").read_text(encoding="utf-8")
    (context / "LocalBackend.Dockerfile").write_text(runtime.replace(marker, marker + " AS runtime") + "\n" + fragment,
                                                    encoding="utf-8")
    return context


def launch(directory: Path, environment: dict[str, str], execute) -> dict[str, str]:
    from ci.image_bundle import inspect_image

    document = json.loads(Path(environment["TRACKVANCE_LOCAL_IMAGE_MANIFEST"]).read_text(encoding="utf-8"))
    image = document.get("backend_tests_image", "")
    row = inspect_image(image, environment["TRACKVANCE_SOURCE_SHA"])
    if (row.get("Config", {}).get("Labels") or {}).get("io.trackvance.local-role") != "backend-tests":
        raise ValueError("Linux backend tests require their own verified immutable image")
    project = environment["TRACKVANCE_LOCAL_BACKEND_PROJECT"]
    container = project + "-checks"
    memory = int(environment["TRACKVANCE_LOCAL_MAX_MEMORY_BYTES"]) - 512 * 1024**2
    cpus = float(environment["TRACKVANCE_LOCAL_MAX_CPUS"]) - 0.5
    if memory < 2 * 1024**3 or cpus < 0.5:
        raise ValueError("Linux backend and its database exceed the aggregate budget")
    internal_database = environment["TRACKVANCE_LOCAL_BACKEND_DATABASE_URL"]
    values = {"DATABASE_URL": "sqlite:////tmp/unit.sqlite", "CI_MIGRATION_DATABASE_URL": internal_database,
              "TRACKVANCE_STORAGE_DIR": "/tmp/storage", "TRACKVANCE_STORAGE_ROOT": "/tmp/storage",
              "DEMO_SEED_ENABLED": "false", "OMP_NUM_THREADS": "1", "OPENBLAS_NUM_THREADS": "1",
              "POLARS_MAX_THREADS": "1", "PYTHONPATH": "/app/source/scripts"}
    for key in ("TRACKVANCE_SPARK_TESTS", "TRACKVANCE_SPARK_MASTER"):
        if key in environment:
            values[key] = environment[key]
    command = ["docker", "run", "--detach", "--name", container,
               "--label", "com.docker.compose.project=" + project,
               "--label", "io.trackvance.local-execution=" + environment["TRACKVANCE_LOCAL_EXECUTION_ID"],
               "--network", project + "_network", "--memory", str(memory), "--memory-swap", str(memory),
               "--cpus", str(cpus), "--pids-limit", "768",
               "--mount", f"type=bind,src={directory.resolve()},dst=/evidence"]
    command.extend(token for key, value in values.items() for token in ("--env", key + "=" + value))
    execute([*command, image], directory, "linux-backend-start", 180, environment=environment)
    return environment | {"TRACKVANCE_LOCAL_BACKEND_CONTAINER": container,
                          "CI_MIGRATION_DATABASE_URL": internal_database}


def commands(commands, root: Path, directory: Path, environment: dict[str, str]):
    result = []
    for phase, arguments, cwd in commands:
        converted = [argument.replace(str(directory), "/evidence").replace("\\", "/")
                     if str(directory) in argument else argument for argument in arguments]
        prefix = ["docker", "exec", "--workdir", "/app/source/" + cwd.relative_to(root).as_posix()]
        if phase.startswith("migration-"):
            prefix.extend(["--env", "DATABASE_URL=" + environment["CI_MIGRATION_DATABASE_URL"]])
        result.append((phase, [*prefix, environment["TRACKVANCE_LOCAL_BACKEND_CONTAINER"], *converted], root))
    return result


def requires_linux(environment: dict[str, str]) -> bool:
    return bool(environment.get("TRACKVANCE_LOCAL_EXECUTION_ID")) and os.name == "nt"
