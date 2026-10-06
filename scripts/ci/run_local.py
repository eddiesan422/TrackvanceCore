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


def plan(manifest, groups):
    result = []
    for group in manifest["groups"]:
        if group["id"] in groups:
            result.append({"group": group["id"], "scenarios": [{key: scenario[key] for key in
                ("id", "rows", "variant", "tier_mib", "mode") if key in scenario} for scenario in group["scenarios"]],
                "minimum_capacity": capacity(group["id"])})
    return result


def protected_inventory():
    result = {}
    projects = json.loads(command("docker", "compose", "ls", "--all", "--format", "json"))
    protected = {"trackvance-certification", "bikerwash"} | {row["Name"] for row in projects if row["Name"].startswith("bikerwash")}
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


def prepare_images(directory, sha, build, *, max_memory_bytes, max_cpus):
    images = {}
    builder = "tv-local-build-" + directory.name.removeprefix("local-")[:12]
    builder_created = False
    config = directory / "buildkit.toml"
    config.write_text('[worker.oci]\n  max-parallelism = 1\n  gc = true\n  reservedSpace = "512MB"\n  maxUsedSpace = "4GB"\n', encoding="utf-8")
    try:
        for role, dockerfile in (("backend", "backend/Dockerfile"), ("web", "deploy/docker/frontend.Dockerfile")):
            identifiers = command("docker", "image", "ls", "-q", "--no-trunc", "--filter", "label=io.trackvance.local-proof=true",
                "--filter", "label=org.opencontainers.image.revision=" + sha, "--filter", "label=io.trackvance.local-role=" + role).split()
            row = inspect_image(identifiers[0], sha) if identifiers else None
            if row is None:
                if not build:
                    raise ValueError("Verified current-SHA local images are absent; pass --build-images once")
                if not builder_created:
                    execute(["docker", "buildx", "create", "--name", builder, "--driver", "docker-container",
                        "--buildkitd-config", str(config), "--driver-opt", "memory=" + str(min(3 * GIB, max_memory_bytes)),
                        "--driver-opt", "memory-swap=" + str(min(3 * GIB, max_memory_bytes)),
                        "--driver-opt", "cpu-quota=" + str(int(min(2, max_cpus) * 100000)),
                        "--driver-opt", "cpu-period=100000", "--driver-opt", "restart-policy=no"],
                        directory, "build-builder-create", 180)
                    builder_created = True
                reference = f"trackvance-local-proof:{sha[:12]}-{directory.name[-12:]}-{role}"
                execute(["docker", "buildx", "build", "--builder", builder, "--load", "--platform", "linux/amd64",
                    "--label", "org.opencontainers.image.revision=" + sha, "--label", "io.trackvance.local-proof=true",
                    "--label", "org.opencontainers.image.version=0.8.0",
                    "--label", "io.trackvance.local-role=" + role, "-t", reference, "-f", dockerfile, "."],
                    directory, "build-" + role, 1800)
                row = inspect_image(reference, sha)
            images[role] = row["Id"]
    finally:
        if builder_created:
            # The builder name is unique to this local execution; no current/default builder is changed.
            execute(["docker", "buildx", "rm", builder], directory, "build-builder-cleanup", 180)
    proof = directory / "local-images.json"
    write(proof, {"schema_version": 1, "kind": "LOCAL_IMAGE_PROOF", "status": "PASS", "source_sha": sha,
        "verified_at": stamp(), "images": images, "builder_cpu_limit": min(2, max_cpus),
        "builder_memory_limit_bytes": min(3 * GIB, max_memory_bytes),
        "retention": "Keep the most recent three SHA pairs; retain current referenced images; no global image pruning"})
    return proof


def retain_images(current_sha, pairs=3):
    identifiers = list(dict.fromkeys(command("docker", "image", "ls", "-q", "--no-trunc", "--filter",
        "label=io.trackvance.local-proof=true").split()))
    rows = json.loads(command("docker", "image", "inspect", *identifiers)) if identifiers else []
    candidates = []
    for row in rows:
        labels = row.get("Config", {}).get("Labels") or {}
        sha = labels.get("org.opencontainers.image.revision", "")
        tags = row.get("RepoTags") or []
        if (labels.get("io.trackvance.local-proof") == "true" and re.fullmatch(r"[a-f0-9]{40}", sha)
                and tags and all(re.fullmatch(r"trackvance-local-proof:[a-f0-9]{12}-[a-f0-9]{12}-(?:backend|web)", tag) for tag in tags)):
            candidates.append(row)
    revisions = []
    for row in sorted(candidates, key=lambda value: value["Created"], reverse=True):
        sha = row["Config"]["Labels"]["org.opencontainers.image.revision"]
        if sha not in revisions:
            revisions.append(sha)
    retained = {current_sha, *revisions[:pairs]}
    removed = []
    for row in candidates:
        if row["Config"]["Labels"]["org.opencontainers.image.revision"] in retained:
            continue
        if command("docker", "ps", "-aq", "--filter", "ancestor=" + row["Id"]).strip():
            continue
        check = json.loads(command("docker", "image", "inspect", row["Id"]))[0]
        if check.get("RepoTags") == row.get("RepoTags") and check.get("Id") == row["Id"]:
            command("docker", "image", "rm", *row["RepoTags"])
            removed.append(row["Id"])
    return removed


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
    parser.add_argument("--retain-runs", type=int, default=7)
    args = parser.parse_args(arguments)
    manifest = load_manifest()
    available = profile_groups(manifest, "deep")
    if bool(args.groups) == args.all or (args.groups and not set(args.groups) <= set(available)):
        parser.error("Choose --all or --groups with valid deep group IDs")
    if not 1 <= args.max_cpus <= 8 or not 1 <= args.max_memory_gib <= 16 or args.reserve_memory_gib < 2 or not 1 <= args.retain_runs <= 30:
        parser.error("Resource budgets and retention must remain bounded; use at least one CPU and reserve at least 2 GiB")
    groups = available if args.all else list(dict.fromkeys(args.groups))
    selected = plan(manifest, groups)
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
    write(registry, [])
    write(builder_registry, [])
    summary = {"schema_version": 1, "kind": "LOCAL_DEEP_EXECUTION", "profile": "deep", "origin": "local",
        "execution_id": execution, "source_sha": sha, "started_at": stamp(), "status": "RUNNING",
        "environment": {"platform": platform.platform(), "python": platform.python_version(), "host_cpu_count": os.cpu_count()},
        "limits": {"max_cpus": args.max_cpus, "max_memory_bytes": int(args.max_memory_gib * GIB),
            "concurrency": 1, "reserve_memory_bytes": int(args.reserve_memory_gib * GIB)},
        "plan": selected, "groups": [{"group": group, "status": "NOT_SELECTED"} for group in available if group not in groups]}
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
        reserved = max(int(args.reserve_memory_gib * GIB), sum(r["memory_limit_bytes"] for rows in before.values() for r in rows))
        permitted = min(int(args.max_memory_gib * GIB), max(0, docker_info["MemTotal"] - reserved))
        runnable = [g for g in groups if capacity(g)["memory_bytes"] <= permitted
            and capacity(g)["cpus"] <= args.max_cpus and capacity(g)["disk_bytes"] <= shutil.disk_usage(ROOT).free]
        images = prepare_images(directory, sha, args.build_images, max_memory_bytes=permitted, max_cpus=args.max_cpus) if any(g not in {"backend", "frontend"} for g in runnable) else None
        environment = {key: value for key, value in os.environ.items() if not key.startswith(("GITHUB_", "CI_", "TRACKVANCE_LOCAL_", "TRACKVANCE_CI_"))}
        environment.update(TRACKVANCE_LOCAL_EXECUTION_ID=execution, TRACKVANCE_SOURCE_SHA=sha,
            TRACKVANCE_LOCAL_PROJECT_REGISTRY=str(registry), TRACKVANCE_LOCAL_MAX_CPUS=str(args.max_cpus),
            TRACKVANCE_LOCAL_BUILDER_REGISTRY=str(builder_registry),
            TRACKVANCE_LOCAL_MAX_MEMORY_BYTES=str(permitted), PYTHONPATH=str(ROOT / "scripts"))
        environment.update(NODE_OPTIONS="--max-old-space-size=384", UV_THREADPOOL_SIZE="1", OMP_NUM_THREADS="1",
            OPENBLAS_NUM_THREADS="1", POLARS_MAX_THREADS="1", GOMAXPROCS="1")
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
                execute([sys.executable, str(ROOT / "scripts/ci/run_suite.py"), "--profile", "deep", "--group", group,
                    "--output-dir", str(directory / group)], directory, group, 7200,
                    environment=environment | {"TRACKVANCE_LOCAL_GROUP": group})
                row["status"] = "PASS"
            except (Exception, KeyboardInterrupt) as error:
                row.update(status="FAIL", error_type=type(error).__name__)
                if isinstance(error, KeyboardInterrupt):
                    raise
            finally:
                row.update(completed_at=stamp(), duration_seconds=round(time.monotonic() - group_start, 3), resources=monitor.finish())
                write(target, summary)
        summary["status"] = "PASS" if all(r["status"] == "PASS" for r in summary["groups"] if r["status"] != "NOT_SELECTED") else "INCOMPLETE"
        summary["closure"] = "DEEP_CERTIFICATION_APPROVED" if summary["status"] == "PASS" and args.all else "PARTIAL_DEEP_EXECUTION"
    except (Exception, KeyboardInterrupt) as error:  # noqa: BLE001 -- persist setup failures and explicit unexecuted groups.
        summary.update(status="FAIL", error_type=type(error).__name__)
        for group in groups:
            if not any(row["group"] == group for row in summary["groups"]):
                summary["groups"].append({"group": group, "status": "NOT_RUN_SETUP_FAILED"})
    finally:
        # Retain the result before removing only registered, newly created resources.
        write(target, summary)
        if owned_before is not None:
            try:
                from ci.owned_cleanup import cleanup
                summary["owned_cleanup"] = cleanup(owned_before, projects=set(json.loads(registry.read_text(encoding="utf-8"))))
            except (OSError, ValueError, subprocess.SubprocessError) as error:
                summary.update(status="FAIL", cleanup_error=type(error).__name__)
        for builder in json.loads(builder_registry.read_text(encoding="utf-8")):
            if re.fullmatch(r"tv-local-legacy-[a-f0-9]{24}", builder):
                with (directory / "legacy-builder-cleanup.private.log").open("a", encoding="utf-8") as output:
                    subprocess.run(["docker", "buildx", "rm", builder], stdout=output, stderr=subprocess.STDOUT,
                        check=False, timeout=180)
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
                summary["retention_removed_images"] = retain_images(sha)
            except (OSError, ValueError, subprocess.SubprocessError) as error:
                summary["retention_image_error"] = type(error).__name__
        summary["retention_removed_reports"] = retain_reports(base, directory, args.retain_runs)
        write(target, summary)
        print(json.dumps({"status": summary["status"], "profile": "deep", "execution_id": execution, "report": str(target)}))
    return 0 if summary["status"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
