"""Run unchanged certification scenarios with deadlines and incremental diagnostics."""
from __future__ import annotations

import argparse
import json
import os
import secrets
import shutil
import signal
import socket
import subprocess
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))
from ci.common import load_manifest, profile_groups
from ci.evidence import wrap_group
from ci.failure_diagnostics import publish_failure

DEADLINES = {"backend": 1200, "frontend": 600, "compose-critical": 1500,
    "compose-functional": 1500, "connections-functional": 1500, "backup-basic": 1200,
    "catalog-reports-functional": 1500, "corrections-functional": 1800,
    "identity-sso": 1200, "connections": 1500, "delivery": 1800,
    "backup-restore": 2100, "catalog-reports": 6300, "benchmark-smoke": 900,
    "delivery-benchmark-smoke": 1200, "spark-local": 1500, "spark-standalone": 1800,
    "async-volume-100": 4500, "async-volume-500": 4500, "async-volume-1024": 4500,
    "corrections-acquisition": 3900, "corrections-browser": 3900,
    "corrections-dispatch": 2700, "corrections-recovery": 3600}


def now() -> str:
    return datetime.now(UTC).isoformat()


class PhaseFailed(RuntimeError):
    def __init__(self, record: dict):
        super().__init__("Certification phase failed; sanitized diagnostic retained.")
        self.record = record


def execute(command: list[str], directory: Path, phase: str, remaining: float,
            *, cwd: Path = ROOT, environment: dict[str, str] | None = None) -> dict:
    if remaining <= 0:
        raise TimeoutError("Group deadline expired before the next phase.")
    began, stamp = time.monotonic(), now()
    log = directory / (phase + ".private.log")
    with log.open("wb") as output:
        process = subprocess.Popen(command, cwd=cwd, env=environment, stdout=output,
            stderr=subprocess.STDOUT, start_new_session=os.name != "nt")
        timed_out = False
        interruption = None
        try:
            code = process.wait(timeout=remaining)
        except (subprocess.TimeoutExpired, KeyboardInterrupt) as error:
            timed_out = isinstance(error, subprocess.TimeoutExpired)
            interruption = error if not timed_out else None
            if os.name == "nt":
                if getattr(process, "args", None):
                    subprocess.run(["taskkill", "/PID", str(process.pid), "/T", "/F"],
                        stdout=output, stderr=subprocess.STDOUT, check=False, timeout=30)
                else:
                    process.terminate()
            else:
                os.killpg(process.pid, signal.SIGTERM)
            try:
                code = process.wait(timeout=30)
            except subprocess.TimeoutExpired:
                if os.name == "nt":
                    process.kill()
                else:
                    os.killpg(process.pid, signal.SIGKILL)
                code = process.wait(timeout=10)
    record = {"name": phase, "status": "PASS" if code == 0 and not timed_out and not interruption else "FAIL",
        "exit_code": code, "timed_out": timed_out, "started_at": stamp,
        "completed_at": now(), "duration_seconds": round(time.monotonic() - began, 3)}
    (directory / (phase + ".json")).write_text(json.dumps(record, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(record), flush=True)
    if interruption:
        raise interruption
    if record["status"] != "PASS":
        raise PhaseFailed(record)
    return record


def commands_for(group: str, directory: Path, *, profile: str = "deep") -> list[tuple[str, list[str], Path]]:
    python = sys.executable
    tests = "scripts/tests/"
    if group == "backend":
        uv = ["uv", "run"]
        return [
            ("runtime-isolation", [*uv, "python", "../scripts/tests/reports_runtime_probe.py"], ROOT / "backend"),
            ("unit-tests", [*uv, "pytest", "tests", "../scripts/tests", "-q", "-ra",
                             "--junitxml=" + str(directory / "pytest-junit.xml")], ROOT / "backend"),
            ("lint", [*uv, "ruff", "check", "src", "tests", "../scripts"], ROOT / "backend"),
            ("types", [*uv, "mypy", "src/trackvance", "--check-untyped-defs", "--ignore-missing-imports"], ROOT / "backend"),
            ("contracts", [*uv, "python", "../scripts/export_contracts.py"], ROOT / "backend"),
            ("contracts-diff", ["git", "diff", "--exit-code", "--", "backend/openapi.json",
                "docs/specification/model_contract_0.8.0.json", "docs/specification/permission_contract_0.8.0.json",
                "docs/development/permission-matrix.md"], ROOT),
            *[("migration-" + name, [*uv, "alembic", *args], ROOT / "backend")
              for name, args in (("upgrade", ["upgrade", "head"]), ("check", ["check"]),
                                ("downgrade", ["downgrade", "base"]), ("reupgrade", ["upgrade", "head"]))],
            ("migration-verify", [*uv, "python", "../scripts/check_postgres_migrations.py"], ROOT / "backend")]
    if group == "frontend":
        pnpm = shutil.which("pnpm") or "pnpm"
        workers = ["--maxWorkers=1", "--minWorkers=1"] if os.environ.get("TRACKVANCE_LOCAL_EXECUTION_ID") else []
        build = [pnpm, "run", "build"]
        if os.environ.get("TRACKVANCE_CI_FRONTEND_BUILD_PROOF_MANIFEST") and not os.environ.get("TRACKVANCE_LOCAL_EXECUTION_ID"):
            build = [python, str(ROOT / "scripts/ci/frontend_build_proof.py"), "--manifest",
                     os.environ["TRACKVANCE_CI_FRONTEND_BUILD_PROOF_MANIFEST"], "--sha", os.environ["CI_SOURCE_SHA"]]
        return [("lint", [pnpm, "run", "lint"], ROOT / "frontend"),
            ("types", [pnpm, "run", "typecheck"], ROOT / "frontend"),
            ("unit-tests", [pnpm, "run", "test", "--reporter=default", "--reporter=json",
                            "--outputFile=" + str(directory / "vitest-results.json"), *workers], ROOT / "frontend"),
            ("build", build, ROOT / "frontend")]
    suites = {
        "compose-critical": [("clean-demo", [python, tests + "docker_e2e_cycle.py", "--skip-build", "--clean-demo"]),
                             ("browser-persistence", [python, tests + "docker_e2e_cycle.py", "--skip-build"])],
        "identity-sso": [("identity", [python, tests + "identity_sso_cycle.py", "--skip-build"])],
        "connections": [("connections", [python, tests + "connections_cycle.py", "--skip-build", "--full-playwright"])],
        "delivery": [("delivery", [python, tests + "delivery_cycle.py", "--skip-build"])],
        "backup-restore": [("native", [python, tests + "docker_backup_cycle.py", "--skip-build"]),
            *[("legacy" + version.replace(".", ""), [python, tests + "identity_legacy_restore_cycle.py", "--source-version", version])
              for version in ("0.5.1", "0.6.0", "0.6.1")]],
        "catalog-reports": [("catalog", [python, tests + "catalog_reports_cycle.py", "--reuse-images",
            "--rows", "120", "400000", "1000000", "--main-project", "trackvance-ci-protected",
            "--with-browser", "--with-ephemeral-observation", "--with-recovery"])],
        "benchmark-smoke": [("benchmark", [python, tests + "benchmark_cycle.py", "--skip-build", "--target-mib", "1",
            "--rows", "1000", "--file-only", "--max-wall-seconds", "600"])],
        "delivery-benchmark-smoke": [("delivery-benchmark", [python, tests + "delivery_benchmark_cycle.py",
            "--skip-build", "--smoke", "--max-wall-seconds", "1200"])],
        "spark-local": [("spark", [python, tests + "spark_local_docker_cycle.py", "--evidence",
            ".codex-local/v070/spark/ci-local", "--rows", "1000000"])],
        "spark-standalone": [("spark", [python, tests + "spark_docker_cycle.py", "--evidence",
            ".codex-local/v070/spark/ci-standalone", "--rows", "1000000"])],
    }
    functional_specs = ("advanced-rules", "corrections", "exception-validation", "local-identity-exceptions",
                        "multiformat-navigation", "rule-builders", "sentinel-schedule")
    suites["compose-functional"] = [
        ("clean-demo", [python, tests + "docker_e2e_cycle.py", "--skip-build", "--clean-demo"]),
        ("browser-persistence", [python, tests + "docker_e2e_cycle.py", "--skip-build",
            *[token for name in functional_specs for token in ("--browser-spec", f"tests-e2e/{name}.spec.ts")]])]
    suites["connections-functional"] = [("connections", [python, tests + "connections_cycle.py", "--skip-build", "--streaming-rows", "1028"])]
    suites["backup-basic"] = [("native", [python, tests + "docker_backup_cycle.py", "--skip-build"])]
    suites["catalog-reports-functional"] = [("catalog", [python, tests + "catalog_reports_cycle.py", "--reuse-images",
        "--rows", "120", "--main-project", "trackvance-ci-protected", "--with-browser", "--with-ephemeral-observation"])]
    # Historic CLIs accept these prefixes. Use a complete UUID ownership suffix
    # rather than their legacy PID/short-suffix defaults in new CI invocations.
    prefixes = {"connections": "trackvance-connections-e2e-", "connections-functional": "trackvance-connections-e2e-", "delivery": "trackvance-delivery-e2e-",
                "benchmark-smoke": "trackvance-bench-", "delivery-benchmark-smoke": "trackvance-delivery-bench-"}
    if group in prefixes:
        suites[group][0][1].extend(["--project", prefixes[group] + uuid4().hex[:12]])
    if group.startswith("async-volume-"):
        tier = group.removeprefix("async-volume-")
        extra = ["--with-browser", "--with-recovery"] if tier == "100" else []
        suites[group] = [("volume", ["uv", "run", "--project", "backend", "python", tests + "v070_cycle.py",
            "--reuse-images", "--tier-mib", tier, *extra])]
    if group.startswith("corrections-"):
        suites[group] = [("corrections", [python, tests + "corrections_cycle.py", "--reuse-images",
            "--group", group.removeprefix("corrections-")])]
    if os.environ.get("TRACKVANCE_LOCAL_EXECUTION_ID"):
        for _, command in suites.get(group, []):
            if "--main-project" in command:
                command[command.index("--main-project") + 1] = "trackvance-certification"
        if group.startswith("spark-"):
            evidence = ROOT / ".codex-local/v070/spark" / os.environ["TRACKVANCE_LOCAL_EXECUTION_ID"] / group
            suites[group][0][1][suites[group][0][1].index("--evidence") + 1] = str(evidence)
    return [(phase, command, ROOT) for phase, command in suites[group]]


def local_backend_database(directory: Path, environment: dict[str, str]) -> dict[str, str]:
    """Actual PostgreSQL migrations use an owned DB, never an ambient URL."""
    from ci.local_resources import register_project
    project = "trackvance-v070-test-unitdb-" + uuid4().hex[:12]
    register_project(project)
    port = None
    for candidate in range(32100, 32999):
        try:
            with socket.socket() as probe:
                probe.bind(("127.0.0.1", candidate))
            port = candidate
            break
        except OSError:
            continue
    if port is None:
        raise ValueError("No exclusive local migration database port is available")
    password = secrets.token_urlsafe(32)
    volume, network, container = project + "_postgres", project + "_network", project + "-postgres"
    label = "com.docker.compose.project=" + project
    for phase, command in (
        ("unitdb-volume", ["docker", "volume", "create", "--label", label, volume]),
        ("unitdb-network", ["docker", "network", "create", "--label", label, network]),
        ("unitdb-start", ["docker", "run", "--detach", "--name", container, "--label", label,
            "--network", network, "--publish", f"127.0.0.1:{port}:5432", "--memory", "512m", "--cpus", "0.5",
            "--pids-limit", "128", "--mount", f"type=volume,src={volume},dst=/var/lib/postgresql/data",
            "-e", "POSTGRES_USER=tv_local_test", "-e", "POSTGRES_DB=tv_local_test", "-e", "POSTGRES_PASSWORD=" + password,
            "postgres:16-alpine"])):
        execute(command, directory, phase, 180, environment=environment)
    for _attempt in range(60):
        ready = subprocess.run(["docker", "exec", container, "pg_isready", "-U", "tv_local_test", "-d", "tv_local_test"],
            capture_output=True, check=False, timeout=15)
        if ready.returncode == 0:
            return {**environment, "CI_MIGRATION_DATABASE_URL": f"postgresql+psycopg://tv_local_test:{password}@127.0.0.1:{port}/tv_local_test",
                "DATABASE_URL": "sqlite:///" + str(directory / "unit.sqlite"), "DEMO_SEED_ENABLED": "false",
                "TRACKVANCE_STORAGE_DIR": str(directory / "storage"), "TRACKVANCE_STORAGE_ROOT": str(directory / "storage")}
        time.sleep(1)
    raise TimeoutError("Owned PostgreSQL migration database did not become ready")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--group", choices=sorted(DEADLINES), required=True)
    parser.add_argument("--profile", choices=("functional", "deep"), default="functional")
    parser.add_argument("--output-dir", type=Path, default=ROOT / ".codex-local/ci")
    args = parser.parse_args()
    if args.group not in profile_groups(load_manifest(), args.profile):
        parser.error("The requested group does not belong to the selected profile")
    # CI uses a head SHA rather than GitHub's synthetic PR merge commit.
    local = bool(os.environ.get("TRACKVANCE_LOCAL_EXECUTION_ID"))
    sha = os.environ.get("TRACKVANCE_SOURCE_SHA") if local else os.environ.get("CI_SOURCE_SHA", os.environ.get("GITHUB_SHA", ""))
    actual = subprocess.run(["git", "rev-parse", "HEAD"], cwd=ROOT, capture_output=True,
                            text=True, check=True).stdout.strip()
    if actual != sha or len(sha) != 40:
        raise ValueError("Checked-out source differs from the requested CI commit.")
    directory = args.output_dir.resolve() / "diagnostics" / args.group
    directory.mkdir(parents=True, exist_ok=False)
    began, stamp, records = time.monotonic(), now(), []
    environment = {**os.environ, "PYTHONPATH": str(ROOT / "scripts") + os.pathsep + os.environ.get("PYTHONPATH", "")}
    if args.group == "backend" and args.profile == "functional":
        environment.update(TRACKVANCE_SPARK_TESTS="1", TRACKVANCE_SPARK_MASTER="local[2]")
    before = {path.resolve() for path in (ROOT / ".codex-local").rglob("*.json")}
    from ci.owned_cleanup import cleanup, snapshot
    owned_before = snapshot() if environment.get("TRACKVANCE_CI_IMAGE_MANIFEST") or local else None
    checks_path = directory / (args.group + "-checks.json")
    ci = {"kind": "LOCAL", "execution_id": os.environ["TRACKVANCE_LOCAL_EXECUTION_ID"], "group": args.group} if local else {"run_id": os.environ["GITHUB_RUN_ID"], "run_attempt": os.environ["GITHUB_RUN_ATTEMPT"],
          "job_id": os.environ.get("CI_JOB_ID", os.environ.get("GITHUB_JOB", ""))}
    phase = 'source-collection'
    try:
        if local and args.group == "backend":
            environment = local_backend_database(directory, environment)
        for phase, command, cwd in commands_for(args.group, directory, profile=args.profile):
            phase_environment = environment
            if phase.startswith('migration-'):
                phase_environment = {**environment, 'DATABASE_URL': environment['CI_MIGRATION_DATABASE_URL'],
                                     'DEMO_SEED_ENABLED': 'false'}
            record = execute(command, directory, phase, DEADLINES[args.group] - (time.monotonic() - began),
                             cwd=cwd, environment=phase_environment)
            records.append(record)
            checks_path.write_text(json.dumps({"status": "RUNNING", "checks": records}, indent=2) + "\n", encoding="utf-8")
        phase = 'source-collection'
        sources = collect_sources(args.group, directory, before)
        if args.group in {"backend", "frontend"}:
            checks = normalize_checks(args.group, records)
            checks_path.write_text(json.dumps({"status": "PASS", "checks": checks}, indent=2) + "\n", encoding="utf-8")
            sources["checks"] = checks_path
        resources = {"profile": args.profile, "heavy_stacks_per_runner": 1, "group_deadline_seconds": DEADLINES[args.group]}
        if local and args.group not in {"backend", "frontend"}:
            from ci_images import verified_images
            resources["runtime_images"] = verified_images(environment)
        if args.group not in {"backend", "frontend"} and not local:
            proof = json.loads((args.output_dir / "image-load.json").read_text(encoding="utf-8"))
            if proof.get("status") != "PASS" or proof.get("source_sha") != sha:
                raise ValueError("Missing actual same-commit image load proof.")
            resources.update(image_bundle_sha256=proof["manifest_sha256"],
                runtime_images={row["role"]: row["image_id"] for row in proof["measurements"]})
        phase = 'evidence-validation'
        wrap_group(args.group, sources, args.output_dir / "evidence", source_sha=sha,
            ci=ci,
            started_at=stamp, completed_at=now(), duration_seconds=round(time.monotonic() - began, 3),
            resources=resources)
    except Exception as error:
        if isinstance(error, PhaseFailed):
            records.append(error.record)
        checks_path.write_text(json.dumps({"status": "FAIL", "checks": records}, indent=2) + "\n", encoding="utf-8")
        if not local:
            publish_failure(args.group, args.output_dir, source_sha=sha, ci=ci, phases=records,
                            error=error, phase=phase, before=before)
        raise
    finally:
        try:
            if owned_before is not None:
                registry = Path(environment.get("TRACKVANCE_LOCAL_PROJECT_REGISTRY", ""))
                projects = set(json.loads(registry.read_text(encoding="utf-8"))) if local and registry.is_file() else set()
                cleanup_result = cleanup(owned_before, projects=projects if local else None)
                (directory / "owned-cleanup.json").write_text(json.dumps(cleanup_result, indent=2) + "\n", encoding="utf-8")
        except Exception as error:
            if not local:
                publish_failure(args.group, args.output_dir, source_sha=sha, ci=ci, phases=records,
                                error=error, phase='owned-cleanup', before=before)
            raise
        finally:
            (directory / "group-timing.json").write_text(json.dumps({"group": args.group,
                "source_sha": sha, "started_at": stamp, "completed_at": now(),
                "duration_seconds": round(time.monotonic() - began, 3), "phases": records}, indent=2) + "\n", encoding="utf-8")


def normalize_checks(group: str, records: list[dict]) -> list[dict]:
    if group == "frontend":
        return records
    result = [row for row in records if row["name"] not in {"contracts-diff"} and not row["name"].startswith("migration-")]
    migrations = [row for row in records if row["name"].startswith("migration-")]
    result.append({"name": "migrations", "status": "PASS", "exit_code": 0,
                   "duration_seconds": sum(row["duration_seconds"] for row in migrations)})
    return result


def one(paths: list[Path]) -> Path:
    if len(paths) != 1:
        raise ValueError("Expected exactly one new, complete scenario evidence document.")
    return paths[0]


def collect_sources(group: str, directory: Path, before: set[Path]) -> dict[str, Path]:
    if group == "backend":
        raw = (directory / "runtime-isolation.private.log").read_text(encoding="utf-8")
        probe = json.loads(raw[raw.index("{"):])
        if probe.get("status") != "PASS":
            raise ValueError("The actual confined runtime probe did not pass.")
        target = directory / "runtime-probe.json"
        target.write_text(json.dumps(probe, indent=2) + "\n", encoding="utf-8")
        return {"junit": directory / "pytest-junit.xml", "runtime-probe": target}
    if group == "frontend":
        return {"unit-results": directory / "vitest-results.json"}
    # Discovery is restricted to newly-created documents in this clean job.
    fresh = [path for path in (ROOT / ".codex-local").rglob("*.json")
             if path.resolve() not in before and "ci" not in path.relative_to(ROOT / ".codex-local").parts
             and "baseline" not in path.relative_to(ROOT / ".codex-local").parts]
    registry = os.environ.get("TRACKVANCE_LOCAL_PROJECT_REGISTRY")
    if registry:
        owned_projects = set(json.loads(Path(registry).read_text(encoding="utf-8")))
        fresh = [path for path in fresh if owned_projects & set(path.relative_to(ROOT / ".codex-local").parts)
                 or any(project in path.parent.name for project in owned_projects)]
    if group.startswith("corrections-"):
        return {"result": one([p for p in fresh if p.name == "corrections-evidence.json"])}
    if group in {"backup-restore", "backup-basic"}:
        result = {}
        for path in [p for p in fresh if p.name == "result.json"]:
            document = json.loads(path.read_text(encoding="utf-8"))
            version = document.get("source_version")
            key = {"0.5.1": "legacy051", "0.6.0": "legacy060", "0.6.1": "legacy061"}.get(version, "native")
            if key in result:
                raise ValueError("Duplicate native or historical recovery evidence.")
            result[key] = path
        return result
    if group.startswith("spark-"):
        base = ROOT / ".codex-local/v070/spark" / ("ci-local" if group == "spark-local" else "ci-standalone")
        if os.environ.get("TRACKVANCE_LOCAL_EXECUTION_ID"):
            base = ROOT / ".codex-local/v070/spark" / os.environ["TRACKVANCE_LOCAL_EXECUTION_ID"] / group
        return {"result": one(list(base.glob("*summary.json"))), "parity": base / "parity.xml",
                "million": one(list(base.glob("*evidence.json")))}
    if group.startswith("async-volume-"):
        result = {"result": one([p for p in fresh if p.name == "cycle-summary.json"]),
                  "volume": one([p for p in fresh if p.name == "volume-evidence.json"]),
                  "automation": one([p for p in fresh if p.name == "automation-results.json"])}
        if group.endswith("-100"):
            result.update(browser=one([p for p in fresh if p.name == "browser-summary.json"]),
                          **{"browser-integrity": one([p for p in fresh if p.name == "browser-volume-integrity.json"]),
                             "recovery": one([p for p in fresh if p.name == "result.json"])})
        return result
    if group in {"compose-critical", "compose-functional"}:
        contexts = sorted({p.parent for p in fresh if p.name == "result.json"
                           and p.parent.name.startswith("trackvance-v070-test-e2e-")})
        results = [(p, json.loads((p / "result.json").read_text(encoding="utf-8"))) for p in contexts]
        clean = one([p / "result.json" for p, doc in results if doc["demo_seed_enabled"] is False])
        seeded = one([p / "result.json" for p, doc in results if doc["demo_seed_enabled"] is True])
        return {"clean-demo": clean, "result": seeded, "browser": seeded.parent / "browser-summary.json",
                **{key: seeded.parent / (key + ".json") for key in ("before", "after", "migrations")}}
    if group in {"catalog-reports", "catalog-reports-functional"}:
        return {"result": one([p for p in fresh if p.name == "result.json" and p.parent.name.startswith("trackvance-v080-test-catalog-reports-")])}
    candidates = [p for p in fresh if p.name == "result.json"]
    result = {"result": one(candidates)}
    browsers = [p for p in fresh if p.name == "browser-summary.json"]
    if browsers:
        result["browser"] = one(browsers)
    return result


if __name__ == "__main__":
    main()
