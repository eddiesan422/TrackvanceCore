"""Run selected deep certification groups serially in owned disposable stacks.

Example: python scripts/ci/run_local.py --groups benchmark-smoke --build-images
Full closure: python scripts/ci/run_local.py --all --max-memory-gib 8 --max-cpus 4.5
Use --plan for the exact historical sizes and capacity reservations without Docker.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import re
import shutil
import subprocess
import sys
import threading
import time
import zipfile
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))
from ci.common import load_manifest, profile_groups
from ci.host_resources import limit_cpu_affinity, set_cpu_affinity
from ci.image_bundle import inspect_image
from ci.local_resources import memory_bytes
from ci.run_suite import execute

GIB = 1024**3


def stamp():
    return datetime.now(UTC).isoformat()


def write(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8")


def command(*args):
    return subprocess.run(args, cwd=ROOT, capture_output=True, text=True, encoding="utf-8", check=True, timeout=90).stdout


def capacity(group):
    if group == "backend":
        return {"memory_bytes": 4 * GIB, "cpus": 2, "disk_bytes": 20 * GIB}
    if group == "benchmark-smoke":
        return {"memory_bytes": 4 * GIB, "cpus": 3, "disk_bytes": 20 * GIB}
    if group == "frontend":
        return {"memory_bytes": int(0.75 * GIB), "cpus": 1, "disk_bytes": GIB}
    if group.startswith("async-volume-"):
        tier = int(group.rsplit("-", 1)[1])
        return {"memory_bytes": 6 * GIB, "cpus": 2, "disk_bytes": tier * 1024**2 * 12 + 10 * GIB}
    if group.startswith("corrections-") or group == "catalog-reports":
        return {"memory_bytes": 6 * GIB, "cpus": 2, "disk_bytes": 20 * GIB}
    if group == "spark-standalone":
        return {"memory_bytes": 7 * GIB, "cpus": 4.5, "disk_bytes": 20 * GIB}
    if group == "spark-local":
        return {"memory_bytes": 3 * GIB, "cpus": 2, "disk_bytes": 20 * GIB}
    # Connector fixtures contain separate source/sink DBs with their own limits.
    if group in {"connections", "delivery", "backup-restore", "delivery-benchmark-smoke"}:
        return {"memory_bytes": 6 * GIB, "cpus": 2, "disk_bytes": 20 * GIB}
    return {"memory_bytes": 3 * GIB, "cpus": 1, "disk_bytes": 20 * GIB}


def plan(manifest, groups, historical_version=None):
    result = []
    for group in manifest["groups"]:
        if group["id"] in groups:
            result.append({"group": group["id"], "scenarios": [{key: scenario[key] for key in
                ("id", "rows", "variant", "tier_mib", "mode") if key in scenario} for scenario in group["scenarios"]],
                "minimum_capacity": capacity(group["id"])})
            if historical_version:
                result[-1].update(execution_scope="REPRESENTATIVE_HISTORICAL_RESTORE", full_group_approved=False,
                    scenarios=[{"id": scenario["id"], "version": scenario["version"],
                        "selection": "SELECTED" if scenario["version"] == historical_version else "NOT_SELECTED"}
                        for scenario in group["scenarios"]])
    return result


def protected_inventory():
    from ci.owned_cleanup import PROJECT
    result = {}
    projects = json.loads(command("docker", "compose", "ls", "--all", "--format", "json"))
    protected = {"trackvance-certification", "bikerwash"} | {row["Name"] for row in projects if not PROJECT.fullmatch(row["Name"])}
    for project in sorted(protected):
        ids = command("docker", "ps", "-aq", "--filter", "label=com.docker.compose.project=" + project).split()
        rows = json.loads(command("docker", "inspect", *ids)) if ids else []
        result[project] = sorted([{"id": r["Id"], "image": r["Image"], "status": r["State"]["Status"],
            "started_at": r["State"].get("StartedAt"),
            "config_sha256": hashlib.sha256(json.dumps(r["Config"], sort_keys=True, separators=(",", ":")).encode()).hexdigest(),
            "host_config_sha256": hashlib.sha256(json.dumps(r["HostConfig"], sort_keys=True, separators=(",", ":")).encode()).hexdigest(),
            "mounts": sorted([{"type": m["Type"], "source": m["Source"], "target": m["Destination"]} for m in r["Mounts"]],
                key=lambda m: (m["type"], m["source"], m["target"])),
            "memory_limit_bytes": r["HostConfig"].get("Memory", 0)} for r in rows], key=lambda r: r["id"])
    return result


def inventory_differences(before, after):
    differences = []
    for project in sorted(before.keys() | after.keys()):
        if project not in before or project not in after:
            differences.append({"project": project, "field": "project_presence", "before": project in before, "after": project in after})
        old = {row["id"]: row for row in before.get(project, [])}
        new = {row["id"]: row for row in after.get(project, [])}
        for identifier in sorted(old.keys() | new.keys()):
            if identifier not in old or identifier not in new:
                differences.append({"project": project, "container_id": identifier, "field": "container_presence",
                    "before": identifier in old, "after": identifier in new})
                continue
            for field in sorted(old[identifier].keys() | new[identifier].keys()):
                if old[identifier].get(field) != new[identifier].get(field):
                    differences.append({"project": project, "container_id": identifier, "field": field,
                        "before": old[identifier].get(field), "after": new[identifier].get(field)})
    return differences


def active_memory_reservation(inventory, minimum, total):
    active = [r for rows in inventory.values() for r in rows if r.get("status") in {"running", "restarting", "paused"}]
    if any(not r.get("memory_limit_bytes") for r in active):
        return total  # An active unbounded service cannot promise spare capacity.
    return max(minimum, sum(r["memory_limit_bytes"] for r in active))


class ResourceMonitor:
    def __init__(self, registry):
        self.registry, self.stop = registry, threading.Event()
        self.samples, self.peak_memory, self.peak_cpu, self.errors = 0, 0, 0.0, 0
        self.host_samples, self.host_peak_memory, self.host_peak_cpu = 0, 0, 0.0
        self.host_previous, self.host_previous_at, self.host_unavailable = {}, time.monotonic(), None
        self.thread = threading.Thread(target=self.collect, daemon=True)

    def collect(self):
        while not self.stop.is_set():
            try:
                from ci.host_resources import sample_process_tree
                sample = sample_process_tree(os.getpid())
                if sample["status"] == "PASS":
                    now = time.monotonic()
                    rows = sample["processes"]
                    cpu = sum(max(0, r["cpu_seconds"] - self.host_previous.get(r["pid"], r["cpu_seconds"])) for r in rows)
                    self.host_peak_memory = max(self.host_peak_memory, sum(r["rss_bytes"] for r in rows))
                    self.host_peak_cpu = max(self.host_peak_cpu, 100 * cpu / max(0.001, now - self.host_previous_at))
                    self.host_previous, self.host_previous_at = {r["pid"]: r["cpu_seconds"] for r in rows}, now
                    self.host_samples += 1
                else:
                    self.host_unavailable = sample.get("reason", "Host measurements unavailable")
            except (OSError, ValueError) as error:
                self.host_unavailable = type(error).__name__
            try:
                projects = json.loads(self.registry.read_text(encoding="utf-8"))
                ids = []
                for project in projects:
                    ids.extend(command("docker", "ps", "-q", "--filter", "label=com.docker.compose.project=" + project).split())
                    ids.extend(command("docker", "ps", "-q", "--filter", "label=io.trackvance.spark-proof-owner=" + project).split())
                if ids:
                    lines = command("docker", "stats", "--no-stream", "--format", "{{json .}}", *ids).splitlines()
                    rows = [json.loads(line) for line in lines]
                    used = sum(memory_bytes(r["MemUsage"].split(" / ")[0].replace("i", "")) for r in rows)
                    cpu = sum(float(r["CPUPerc"].rstrip("%")) for r in rows)
                    self.peak_memory, self.peak_cpu = max(self.peak_memory, used), max(self.peak_cpu, cpu)
                    self.samples += 1
            except (OSError, ValueError, subprocess.SubprocessError):
                self.errors += 1
            self.stop.wait(2)

    def finish(self):
        self.stop.set()
        self.thread.join(timeout=10)
        return {"sample_interval_seconds": 2, "samples": self.samples,
            "sampled_peak_memory_bytes": self.peak_memory if self.samples else None,
            "sampled_peak_cpu_percent": self.peak_cpu if self.samples else None,
            "measurement_errors": self.errors, "scope": "Owned running containers only; sampled peaks, not exact maxima",
            "host": {"status": "MEASURED" if self.host_samples else "UNAVAILABLE", "samples": self.host_samples,
                "sampled_peak_sum_rss_bytes": self.host_peak_memory if self.host_samples else None,
                "sampled_peak_cpu_percent": self.host_peak_cpu if self.host_samples else None,
                "unavailable_reason": self.host_unavailable,
                "scope": "This runner and descendants only; sum of process RSS can count shared pages more than once; sampled peaks"}}


def prepare_images(directory, sha, build, *, max_memory_bytes, max_cpus, backend_tests=False, environment=None):
    from ci.local_resources import (
        begin_image_build,
        initialize_image_registry,
        observe_image_builds,
    )
    execution = (environment or os.environ).get("TRACKVANCE_LOCAL_EXECUTION_ID", directory.name)
    image_registry = Path((environment or os.environ).get("TRACKVANCE_LOCAL_IMAGE_REGISTRY", directory / "owned-images.json"))
    image_command = lambda *arguments: command("docker", *arguments)
    initialize_image_registry(image_registry, execution, image_command)
    owner_project = "trackvance-v070-test-images-" + execution[-12:]
    images = {}
    write(directory / "builder-images.before.json", command("docker", "image", "ls", "-aq", "--no-trunc").split())
    builder = "tv-local-build-" + directory.name.removeprefix("local-")[:12]
    builder_created = False
    config = directory / "buildkit.toml"
    config.write_text('[worker.oci]\n  max-parallelism = 1\n  gc = true\n  reservedSpace = "512MB"\n  maxUsedSpace = "4GB"\n', encoding="utf-8")
    try:
        roles = [("backend", "backend/Dockerfile"), ("web", "deploy/docker/frontend.Dockerfile")]
        if backend_tests:
            roles.append(("backend-tests", "LocalBackend.Dockerfile"))
        for role, dockerfile in roles:
            identifiers = command("docker", "image", "ls", "-q", "--no-trunc", "--filter", "label=io.trackvance.local-proof=true",
                "--filter", "label=org.opencontainers.image.revision=" + sha, "--filter", "label=io.trackvance.local-role=" + role).split()
            row = inspect_image(identifiers[0], sha) if identifiers else None
            if row is None:
                if not build:
                    raise ValueError("Verified current-SHA local images are absent; pass --build-images once")
                if not builder_created:
                    if builder in command("docker", "buildx", "ls", "--format", "{{.Name}}").split():
                        raise ValueError("The owned builder namespace is not fresh")
                    registry_path = (environment or os.environ).get("TRACKVANCE_LOCAL_BUILDER_REGISTRY")
                    if registry_path:
                        registry = Path(registry_path)
                        names = json.loads(registry.read_text(encoding="utf-8")) if registry.exists() else []
                        write(registry, [*names, builder])
                    builder_created = True  # Attempt may partially create state before failing.
                    execute(["docker", "buildx", "create", "--name", builder, "--driver", "docker-container",
                        "--buildkitd-config", str(config), "--driver-opt", "memory=" + str(min(3 * GIB, max_memory_bytes)),
                        "--driver-opt", "memory-swap=" + str(min(3 * GIB, max_memory_bytes)),
                        "--driver-opt", "cpu-quota=" + str(int(min(2, max_cpus) * 100000)),
                        "--driver-opt", "cpu-period=100000", "--driver-opt", "restart-policy=no"],
                        directory, "build-builder-create", 180, environment=environment)
                context = ROOT
                if role == "backend-tests":
                    from ci.local_backend import test_context
                    context = test_context(ROOT, directory, sha, execute, environment)
                reference = f"trackvance-local-proof:{sha[:12]}-{directory.name[-12:]}-{role}"
                labels = begin_image_build(image_registry, execution, owner_project, sha,
                    "0.8.0", role, reference, image_command)
                execute(["docker", "buildx", "build", "--builder", builder, "--load", "--platform", "linux/amd64",
                    *[argument for key, value in labels.items() for argument in ("--label", key + "=" + value)],
                    "-t", reference, "-f", dockerfile, "."],
                    directory, "build-" + role, 1800, cwd=context, environment=environment)
                observe_image_builds(image_registry, execution, image_command)
                row = inspect_image(reference, sha)
            images[role] = row["Id"]
    finally:
        try:
            observe_image_builds(image_registry, execution, image_command)
        finally:
            try:
                if builder_created:
                    # The builder name is unique to this execution.
                    execute(["docker", "buildx", "rm", builder], directory, "build-builder-cleanup", 180,
                            environment={k: v for k, v in (environment or os.environ).items() if k != "TRACKVANCE_LOCAL_PROTECTED_INVENTORY"})
            finally:
                shutil.rmtree(directory / "backend-test-context", ignore_errors=True)
                write(directory / "builder-image-cleanup.json", cleanup_builder_images(directory))
    proof = directory / "local-images.json"
    write(proof, {"schema_version": 1, "kind": "LOCAL_IMAGE_PROOF", "status": "PASS", "source_sha": sha,
        "verified_at": stamp(), "images": {role: image for role, image in images.items() if role != "backend-tests"},
        **({"backend_tests_image": images["backend-tests"]} if backend_tests else {}),
        "builder_cpu_limit": min(2, max_cpus),
        "builder_memory_limit_bytes": min(3 * GIB, max_memory_bytes),
        "retention": "Ephemeral: remove verified unused local proof images after evidence and owned containers; retain only real container references"})
    return proof


def cleanup_builder_images(directory):
    before_path = directory / "builder-images.before.json"
    if not before_path.is_file():
        return {"status": "PASS", "removed": [], "skipped": []}
    before = set(json.loads(before_path.read_text(encoding="utf-8")))
    tag = "moby/buildkit:buildx-stable-1"
    identifiers = list(dict.fromkeys(command("docker", "image", "ls", "-q", "--no-trunc", tag).split()))
    removed, skipped = [], []
    for identifier in identifiers:
        if identifier in before:
            skipped.append({"image_id": identifier, "reason": "PREEXISTING_NOT_OWNED"})
            continue
        row = json.loads(command("docker", "image", "inspect", identifier))[0]
        if row.get("Id") != identifier or row.get("RepoTags") != [tag]:
            skipped.append({"image_id": identifier, "reason": "REFERENCES_UNCERTAIN"})
            continue
        if command("docker", "ps", "-aq", "--filter", "ancestor=" + identifier).strip():
            skipped.append({"image_id": identifier, "reason": "CONTAINER_CONSUMER"})
            continue
        check = json.loads(command("docker", "image", "inspect", identifier))[0]
        if check.get("Id") != identifier or check.get("RepoTags") != [tag]:
            raise ValueError("Builder image references changed before scoped removal")
        command("docker", "image", "rm", tag)
        removed.append({"image_id": identifier, "tags": [tag]})
    return {"status": "PASS", "removed": removed, "skipped": skipped, "purpose": "Newly pulled infrastructure for this owned builder only"}


def cleanup_fixture_images(source_sha, retain_for_seconds=0, *, registry=None, execution_id=None):
    if not re.fullmatch(r"[a-f0-9]{40}", source_sha) or not 0 <= retain_for_seconds <= 86400:
        raise ValueError("Image retention requires an exact SHA and an explicit bounded duration")
    from ci.local_resources import cleanup_registered_images
    if registry is None:
        raise ValueError("Scoped image cleanup requires a durable execution registry")
    path = Path(registry)
    execution = execution_id or json.loads(path.read_text(encoding="utf-8"))["execution_id"]
    return cleanup_registered_images(path, execution, retain_for_seconds=retain_for_seconds,
        command=lambda *arguments: command("docker", *arguments))


def retain_reports(base, current, count):
    candidates = [p for p in base.iterdir() if p.is_dir() and not p.is_symlink()
        and re.fullmatch(r"local-[a-f0-9]{32}", p.name) and (p / "local-summary.json").is_file()]
    candidates.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    removed = []
    for path in candidates[count:]:
        if path != current and path.resolve().parent == base.resolve():
            archive = base.parent / "local-deep-archives" / (path.name + ".zip")
            archive.parent.mkdir(parents=True, exist_ok=True)
            with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED) as zipped:
                for report in path.rglob("*"):
                    if report.is_file() and not report.is_symlink() and (report == path / "local-summary.json"
                            or report.name in {"protected-inventory.before.json", "protected-inventory.after.json"}
                            or "evidence" in report.relative_to(path).parts or report.name.endswith("-timing.json")):
                        zipped.write(report, report.relative_to(path))
            shutil.rmtree(path)
            removed.append(path.name)
    return removed


def main(arguments=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--groups", nargs="+", help="Any explicit deep profile group IDs")
    parser.add_argument("--all", action="store_true")
    parser.add_argument("--plan", action="store_true")
    parser.add_argument("--max-cpus", type=float, default=2)
    parser.add_argument("--max-memory-gib", type=float, default=4)
    parser.add_argument("--reserve-memory-gib", type=float, default=2)
    parser.add_argument("--concurrency", type=int, choices=[1], default=1, help="One stack at a time protects active certification")
    parser.add_argument("--build-images", action="store_true")
    parser.add_argument("--exercise-failure-cleanup", action="store_true", help="Also run an explicit owned exit-23 fault and verify Docker cleanup")
    parser.add_argument("--historical-source-version", choices=["0.5.1", "0.6.0", "0.6.1"],
        help="Run only one authentic historical restore; requires --groups backup-restore and remains partial")
    parser.add_argument("--retain-runs", type=int, default=7)
    args = parser.parse_args(arguments)
    manifest = load_manifest()
    available = profile_groups(manifest, "deep")
    if bool(args.groups) == args.all or (args.groups and not set(args.groups) <= set(available)):
        parser.error("Choose --all or --groups with valid deep group IDs")
    if not 1 <= args.max_cpus <= 8 or not 1 <= args.max_memory_gib <= 16 or args.reserve_memory_gib < 2 or not 1 <= args.retain_runs <= 30:
        parser.error("Resource budgets and retention must remain bounded; use at least one CPU and reserve at least 2 GiB")
    groups = available if args.all else list(dict.fromkeys(args.groups))
    if args.historical_source_version and (args.all or groups != ["backup-restore"]):
        parser.error("A representative historical restore requires only --groups backup-restore, without --all")
    selected = plan(manifest, groups, args.historical_source_version)
    if args.plan:
        print(json.dumps({"profile": "deep", "origin": "local", "groups": selected,
            "concurrency": 1, "max_cpus": args.max_cpus, "max_memory_gib": args.max_memory_gib}, indent=2))
        return 0
    sha = command("git", "rev-parse", "HEAD").strip()
    if command("git", "status", "--porcelain", "--untracked-files=all").strip():
        parser.error("Commit the tracked implementation first; deep evidence must describe a reproducible SHA")
    execution = "local-" + uuid4().hex
    base = ROOT / ".codex-local/local-deep"
    directory = base / execution
    directory.mkdir(parents=True, exist_ok=False)
    registry = directory / "owned-projects.json"
    builder_registry = directory / "owned-builders.json"
    image_registry = directory / "owned-images.json"
    write(registry, [])
    write(builder_registry, [])
    summary = {"schema_version": 1, "kind": "LOCAL_DEEP_EXECUTION", "profile": "deep", "origin": "local",
        "execution_id": execution, "source_sha": sha, "started_at": stamp(), "status": "RUNNING",
        "environment": {"platform": platform.platform(), "python": platform.python_version(), "host_cpu_count": os.cpu_count()},
        "limits": {"max_cpus": args.max_cpus, "max_memory_bytes": int(args.max_memory_gib * GIB),
            "concurrency": 1, "reserve_memory_bytes": int(args.reserve_memory_gib * GIB)},
        "plan": selected, "groups": [{"group": group, "status": "NOT_SELECTED"} for group in available if group not in groups]}
    if args.historical_source_version:
        summary.update(execution_scope="REPRESENTATIVE_HISTORICAL_RESTORE", full_group_approved=False,
            historical_source_version=args.historical_source_version)
    target = directory / "local-summary.json"
    write(target, summary)
    began = time.monotonic()
    before = owned_before = affinity = None
    try:
        affinity = limit_cpu_affinity(args.max_cpus)
        summary["environment"]["host_cpu_affinity"] = affinity
        before = protected_inventory()
        write(directory / "protected-inventory.before.json", before)
        summary["protected_inventory_proof"] = {"before": "protected-inventory.before.json", "after": "protected-inventory.after.json",
            "scope": "Container identity, image, lifecycle, canonical mounts and configuration hashes; excludes environment/configuration values"}
        from ci.owned_cleanup import snapshot
        owned_before = snapshot()
        docker_info = json.loads(command("docker", "info", "--format", "{{json .}}"))
        summary["environment"].update(docker_context=command("docker", "context", "show").strip(),
            docker_version=docker_info["ServerVersion"], docker_memory_bytes=docker_info["MemTotal"])
        reserved = active_memory_reservation(before, int(args.reserve_memory_gib * GIB), docker_info["MemTotal"])
        summary["limits"].update(protected_active_reservation_bytes=reserved,
            reservation_policy="Only running/restarting/paused protected containers reserve declared limits; minimum reserve remains; lifecycle changes abort owned work")
        permitted = min(int(args.max_memory_gib * GIB), max(0, docker_info["MemTotal"] - reserved))
        runnable = [g for g in groups if capacity(g)["memory_bytes"] <= permitted
            and capacity(g)["cpus"] <= args.max_cpus and capacity(g)["disk_bytes"] <= shutil.disk_usage(ROOT).free]
        environment = {key: value for key, value in os.environ.items() if not key.startswith(("GITHUB_", "CI_", "TRACKVANCE_LOCAL_", "TRACKVANCE_CI_"))}
        environment.update(TRACKVANCE_LOCAL_EXECUTION_ID=execution, TRACKVANCE_SOURCE_SHA=sha,
            TRACKVANCE_LOCAL_PROJECT_REGISTRY=str(registry), TRACKVANCE_LOCAL_MAX_CPUS=str(args.max_cpus),
            TRACKVANCE_LOCAL_BUILDER_REGISTRY=str(builder_registry),
            TRACKVANCE_LOCAL_IMAGE_REGISTRY=str(image_registry),
            TRACKVANCE_LOCAL_MAX_MEMORY_BYTES=str(permitted), PYTHONPATH=str(ROOT / "scripts"),
            TRACKVANCE_LOCAL_PROTECTED_INVENTORY=str(directory / "protected-inventory.before.json"))
        environment.update(NODE_OPTIONS="--max-old-space-size=384", UV_THREADPOOL_SIZE="1", OMP_NUM_THREADS="1",
            OPENBLAS_NUM_THREADS="1", POLARS_MAX_THREADS="1", GOMAXPROCS="1")
        need_backend_tests = "backend" in runnable and os.name == "nt"
        images = prepare_images(directory, sha, args.build_images, max_memory_bytes=permitted, max_cpus=args.max_cpus,
            backend_tests=need_backend_tests, environment=environment) if need_backend_tests or args.exercise_failure_cleanup or any(g not in {"backend", "frontend"} for g in runnable) else None
        if images:
            environment["TRACKVANCE_LOCAL_IMAGE_MANIFEST"] = str(images)
        for group in groups:
            row = {"group": group, "started_at": stamp(), "required_capacity": capacity(group)}
            summary["groups"].append(row)
            if group not in runnable:
                row.update(status="PENDING_CAPACITY", reason="Configured CPU/RAM, protected installation reservation or disk capacity is insufficient")
                write(target, summary)
                continue
            monitor = ResourceMonitor(registry)
            monitor.thread.start()
            group_start = time.monotonic()
            try:
                if args.historical_source_version:
                    version = args.historical_source_version
                    # The authentic 0.6.1 runner intentionally requires this
                    # private root. Preserve that guard and copy safe receipts.
                    output = ROOT / ".codex-local/v070" / (execution + "-historical")
                    row["historical_evidence_directory"] = str(output.relative_to(ROOT))
                    write(target, summary)
                    execute([sys.executable, str(ROOT / "scripts/tests/identity_legacy_restore_cycle.py"),
                        "--source-version", version, "--evidence-dir", str(output)], directory, group, 3600,
                        environment=environment | {"TRACKVANCE_LOCAL_GROUP": group})
                    from ci.validators import validate_content
                    spec = next(scenario for spec_group in manifest["groups"] if spec_group["id"] == group
                        for scenario in spec_group["scenarios"] if scenario.get("version") == version)
                    result_path = output / "result.json"
                    validate_content(spec, {"documents": {spec["source"]:
                        json.loads(result_path.read_text(encoding="utf-8"))}})
                    group_output = directory / group
                    group_output.mkdir(parents=True, exist_ok=False)
                    for safe_name in ("result.json", "local-historical-source-proof.json", "local-legacy-image-ownership.json"):
                        if (output / safe_name).is_file():
                            shutil.copyfile(output / safe_name, group_output / safe_name)
                    result_path = group_output / "result.json"
                    row.update(status="PARTIAL_PASS", full_group_approved=False,
                        execution_scope="REPRESENTATIVE_HISTORICAL_RESTORE",
                        historical_evidence_directory=str(output.relative_to(ROOT)),
                        scenarios=[{"id": scenario["id"], "status": "PASS" if scenario["selection"] == "SELECTED" else "NOT_SELECTED"}
                            for scenario in selected[0]["scenarios"]],
                        validated_result={"path": str(result_path.relative_to(directory)),
                            "sha256": hashlib.sha256(result_path.read_bytes()).hexdigest()})
                else:
                    execute([sys.executable, str(ROOT / "scripts/ci/run_suite.py"), "--profile", "deep", "--group", group,
                        "--output-dir", str(directory / group)], directory, group, 7200,
                        environment=environment | {"TRACKVANCE_LOCAL_GROUP": group})
                    row["status"] = "PASS"
            except (Exception, KeyboardInterrupt) as error:
                row.update(status="FAIL", error_type=type(error).__name__)
                if isinstance(error, KeyboardInterrupt):
                    raise
                if getattr(error, "record", {}).get("protected_state_changed") or type(error).__name__ == "ProtectedStateChanged":
                    raise
            finally:
                row.update(completed_at=stamp(), duration_seconds=round(time.monotonic() - group_start, 3), resources=monitor.finish())
                write(target, summary)
        if args.exercise_failure_cleanup:
            from ci.local_failure_probe import run as failure_probe
            summary["controlled_failure_cleanup"] = failure_probe(directory / "controlled-failure", environment)
        summary["status"] = "PASS" if all(r["status"] in {"PASS", "PARTIAL_PASS"} for r in summary["groups"] if r["status"] != "NOT_SELECTED") else "INCOMPLETE"
        summary["closure"] = "DEEP_CERTIFICATION_APPROVED" if summary["status"] == "PASS" and args.all else "PARTIAL_DEEP_EXECUTION"
    except (Exception, KeyboardInterrupt) as error:  # noqa: BLE001 -- persist setup failures and explicit unexecuted groups.
        summary.update(status="FAIL", error_type=type(error).__name__)
        for group in groups:
            if not any(row["group"] == group for row in summary["groups"]):
                protection_changed = getattr(error, "record", {}).get("protected_state_changed") or type(error).__name__ == "ProtectedStateChanged"
                summary["groups"].append({"group": group, "status": "NOT_RUN_PROTECTION_CHANGED" if protection_changed else "NOT_RUN_SETUP_FAILED"})
    finally:
        # Retain the result before removing only registered, newly created resources.
        write(target, summary)
        if owned_before is not None:
            try:
                from ci.owned_cleanup import cleanup
                summary["owned_cleanup"] = cleanup(owned_before, projects=set(json.loads(registry.read_text(encoding="utf-8"))))
            except (OSError, ValueError, subprocess.SubprocessError) as error:
                summary.update(status="FAIL", cleanup_error=type(error).__name__)
        try:
            for builder in json.loads(builder_registry.read_text(encoding="utf-8")):
                if re.fullmatch(r"tv-local-(?:legacy-[a-f0-9]{24}|build-[a-f0-9]{12})", builder) and builder in command("docker", "buildx", "ls", "--format", "{{.Name}}").split():
                    with (directory / "legacy-builder-cleanup.private.log").open("a", encoding="utf-8") as output:
                        subprocess.run(["docker", "buildx", "rm", builder], stdout=output, stderr=subprocess.STDOUT,
                            check=False, timeout=180)
        except (OSError, ValueError, subprocess.SubprocessError, KeyboardInterrupt) as error:
            summary.update(status="FAIL", builder_cleanup_error=type(error).__name__)
        try:
            summary["builder_image_cleanup"] = cleanup_builder_images(directory)
        except (OSError, ValueError, subprocess.SubprocessError) as error:
            summary.update(status="FAIL", builder_image_cleanup_error=type(error).__name__)
        if before is not None:
            try:
                after = protected_inventory()
                write(directory / "protected-inventory.after.json", after)
                summary["protected_inventory_differences"] = inventory_differences(before, after)
                summary["protected_inventory_unchanged"] = not summary["protected_inventory_differences"]
                if not summary["protected_inventory_unchanged"]:
                    summary["status"] = "FAIL"
            except Exception as error:  # noqa: BLE001 -- unavailable protection proof must prevent local approval.
                summary.update(status="FAIL", protection_check_error=type(error).__name__)
        if affinity is not None:
            try:
                set_cpu_affinity(affinity["original_logical_processors"])
                summary["host_cpu_affinity_restored"] = True
            except OSError as error:
                summary.update(status="FAIL", affinity_restore_error=type(error).__name__)
        summary.update(completed_at=stamp(), duration_seconds=round(time.monotonic() - began, 3))
        summary["closure"] = "DEEP_CERTIFICATION_APPROVED" if summary["status"] == "PASS" and args.all else "PARTIAL_DEEP_EXECUTION"
        write(target, summary)
        if summary.get("environment", {}).get("docker_memory_bytes"):
            try:
                summary["fixture_image_cleanup"] = cleanup_fixture_images(sha, registry=image_registry, execution_id=execution) if image_registry.exists() else {
                    "status": "PASS", "removed": [], "skipped": [], "reason": "NO_IMAGE_BUILD_ATTEMPT"}
            except (OSError, ValueError, subprocess.SubprocessError) as error:
                summary["retention_image_error"] = type(error).__name__
                summary.update(status="FAIL", closure="PARTIAL_DEEP_EXECUTION")
        summary["retention_removed_reports"] = retain_reports(base, directory, args.retain_runs)
        summary.update(completed_at=stamp(), duration_seconds=round(time.monotonic() - began, 3))
        write(target, summary)
        print(json.dumps({"status": summary["status"], "profile": "deep", "execution_id": execution, "report": str(target)}))
    return 0 if summary["status"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
