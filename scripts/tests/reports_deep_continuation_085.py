"""Retry only API1m, then owned downloads/OPFS, HTTP observation and recovery.

Original Catalog tiers are immutable references to another SHA. This receipt
never grants a full Catalog approval or inherits approval for the new image.
Candidate images must already have same-HEAD OCI proof; only the existing
diagnostic/historical runners may build their registered bounded images.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import sys
import time
import traceback
from pathlib import Path
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "scripts/tests"))
import certification_v080 as guard
import reports_download_085 as downloads
import reports_ephemeral_http as observation
from ci import owned_cleanup, run_local, validators
from ci.host_resources import limit_cpu_affinity, set_cpu_affinity
from ci.local_resources import cleanup_registered_images, initialize_image_registry
from ci.run_suite import execute, now

GIB = 1024**3
ROWS = [120, 400000, 1000000]
STAGES = ("api_1m_retry", "mass_download_opfs", "http_17_cases", "recovery_both")
GLOBAL_DEADLINE = 10800
BUILDER = re.compile(r"tv-local-(?:legacy-[a-f0-9]{24}|build-[a-f0-9]{12})")


def require(condition, code):
    if not condition:
        raise ValueError(code)


def read(path):
    return json.loads(path.read_text(encoding="utf-8"))


def file_reference(path):
    require(path.is_file() and not path.is_symlink(), "CONTINUATION_REGULAR_EVIDENCE_REQUIRED")
    return {"path": str(path.resolve()), "sha256": downloads.file_hash(path), "bytes": path.stat().st_size}


def original_tiers(directory, source_sha):
    """Reference actual receipts without copying or reinterpreting approval."""
    require(re.fullmatch(r"[a-f0-9]{40}", source_sha), "CONTINUATION_PARENT_SHA")
    directory = directory.resolve(strict=True)
    require(directory.parent.name == "v080" and directory.parent.parent.name == ".codex-local",
            "CONTINUATION_PARENT_PRIVATE_DIRECTORY")
    context = read(directory / "isolation.json")
    require(guard.PROJECT.fullmatch(context["project"]) and context["project"] != context["main_project"],
            "CONTINUATION_PARENT_OWNERSHIP")
    aggregate = read(directory / "result.json")
    require(aggregate.get("source_sha") == source_sha and aggregate.get("source_tree_dirty") is False
            and aggregate.get("status") in {"PASS", "FAIL"}, "CONTINUATION_PARENT_TERMINAL_SOURCE")
    files = [file_reference(directory / "isolation.json"), file_reference(directory / "result.json")]
    tiers = []
    for rows in ROWS:
        path = directory / f"reports-{rows}.json"
        value = read(path)
        require(value.get("rows_per_source") == rows and (value.get("status") == "PASS"
                or rows == 1000000 and value.get("status") == "FAIL"),
                "CONTINUATION_PARENT_TIER_NOT_COMPLETE")
        reference = file_reference(path)
        files.append(reference)
        tiers.append({"rows_per_source": rows, "original_status": value["status"], **reference})
    return {"source_sha": source_sha, "project": context["project"], "aggregate_original_status": aggregate["status"],
            "tiers": tiers, "files": files, "scope": "ORIGINAL_SHA_EVIDENCE_REFERENCES_ONLY",
            "new_sha_approval_inherited": False, "original_receipts_modified": False}


def verify_originals(proof):
    require(all(file_reference(Path(item["path"])) == item for item in proof["files"]),
            "CONTINUATION_ORIGINAL_EVIDENCE_CHANGED")


def source_provenance(original_sha, current_sha):
    guard.command(["git", "merge-base", "--is-ancestor", original_sha, current_sha])
    return {"original_source_tree": guard.command(["git", "rev-parse", original_sha + "^{tree}"]).strip(),
            "continuation_source_tree": guard.command(["git", "rev-parse", current_sha + "^{tree}"]).strip(),
            "changed_paths": guard.command(["git", "diff", "--name-only", original_sha, current_sha]).splitlines(),
            "runtime_application_changed_paths": guard.command([
                "git", "diff", "--name-only", original_sha, current_sha, "--", "backend/src", "frontend/src"]).splitlines(),
            "approval_inheritance": "NOT_GRANTED"}


def capacity():
    """Reserve all currently running workloads, including other UUID projects."""
    info = json.loads(guard.command(["docker", "info", "--format", "{{json .}}"], timeout=90))
    ids = guard.command(["docker", "ps", "-q", "--no-trunc"]).split()
    active = json.loads(guard.command(["docker", "inspect", *ids])) if ids else []
    reserve, cpu = 0, 0.0
    for row in active:
        config = row["HostConfig"]
        memory = config.get("Memory", 0)
        quota, period = config.get("CpuQuota", 0), config.get("CpuPeriod", 0)
        cores = config.get("NanoCpus", 0) / 10**9 or (quota / period if quota > 0 and period > 0 else 0)
        require(memory > 0 and cores > 0, "PENDING_CAPACITY_UNBOUNDED_ACTIVE_WORKLOAD")
        reserve += memory
        cpu += cores
    active_memory, infrastructure = reserve, 2 * GIB
    reserve = active_memory + infrastructure
    require(info["MemTotal"] >= reserve + 10 * GIB and info["NCPU"] >= cpu + 4
            and shutil.disk_usage(ROOT).free >= 20 * GIB, "PENDING_CAPACITY_CONTINUATION_4_CPU_10_GIB")
    return {"status": "PASS", "docker_memory_bytes": info["MemTotal"], "docker_cpus": info["NCPU"],
            "active_declared_memory_bytes": active_memory, "infrastructure_reservation_bytes": infrastructure,
            "other_workload_memory_reservation_bytes": reserve, "other_workload_cpu_reservation": cpu,
            "runner_memory_budget_bytes": 10 * GIB, "runner_affinity_processors": 4,
            "mass_docker_cpu_budget": 3, "mass_client_cpu_ceiling": 1,
            "controller_monitor_infrastructure_excluded_from_client_budget": True}


def stop_mass(directory, context):
    guard.preflight(directory, context)
    guard.command([*guard.compose_args(directory, context), "stop"], timeout=180)
    ids = guard.command(["docker", "ps", "-q", "--filter",
                         "label=com.docker.compose.project=" + context["project"]]).split()
    require(not ids, "CONTINUATION_POPULATION_SERVICES_STILL_RUNNING")
    return {"status": "PASS", "project": context["project"], "running_containers": 0}


def cleanup(directory, execution, before, baseline_builders):
    """Read final registries after the last restore; never clean by global prefix."""
    result = {"status": "PASS"}
    projects = set()

    def attempt(name, operation):
        try:
            result[name] = operation()
        except (Exception, KeyboardInterrupt) as error:  # noqa: BLE001 - attempt every owned cleanup after failure.
            result.update(status="FAIL")
            result[name] = {**result.get(name, {}), "status": "FAIL", "error_type": type(error).__name__}

    def registered_projects():
        registered = set(read(directory / "owned-projects.json"))
        require(all(owned_cleanup.PROJECT.fullmatch(project) for project in registered), "CONTINUATION_CLEANUP_OWNERSHIP")
        projects.update(registered)
        return {"status": "PASS", "projects": sorted(projects)}

    attempt("project_registry", registered_projects)
    attempt("resources", lambda: owned_cleanup.cleanup(before, projects=projects))

    def builders():
        registered = read(directory / "owned-builders.json")
        require(all(BUILDER.fullmatch(name) and name not in baseline_builders for name in registered),
                "CONTINUATION_BUILDER_OWNERSHIP")
        current = set(guard.command(["docker", "buildx", "ls", "--format", "{{.Name}}"]).split())
        removed = []
        for name in registered:
            if name in current:
                guard.command(["docker", "buildx", "rm", name], timeout=180)
                removed.append(name)
        require(not set(registered) & set(guard.command(["docker", "buildx", "ls", "--format", "{{.Name}}"]).split()),
                "CONTINUATION_REGISTERED_BUILDER_REMAINS")
        return {"status": "PASS", "removed": removed}

    attempt("builders", builders)
    def images():
        proof = cleanup_registered_images(directory / "owned-images.json", execution, projects=projects)
        result["images"] = proof
        require(not proof.get("skipped"), "CONTINUATION_OWNED_IMAGES_NOT_FULLY_RETIRED")
        return proof

    attempt("images", images)
    def builder_images():
        proof = run_local.cleanup_builder_images(directory)
        result["builder_images"] = proof
        require(all(item.get("reason") == "PREEXISTING_NOT_OWNED" for item in proof.get("skipped", [])),
                "CONTINUATION_NEW_INFRASTRUCTURE_IMAGE_REMAINS")
        return proof

    attempt("builder_images", builder_images)
    return result


def run_scopes(directory, summary, environment, deadline, port, main_project):
    """Reuse mandatory gates; retain receipts independently of child exit status."""
    def stage(index, command, receipt_path, validator):
        item = summary["stages"][index]
        item.update(status="RUNNING", started_at=now())
        downloads.write_json(directory / "result.json", summary)
        try:
            execute(command, directory, item["scope"], deadline - time.monotonic(), environment=environment)
            path = receipt_path()
            value = read(path)
            item["receipt"] = file_reference(path)
            validator(value)
            item.update(status="PASS", completed_at=now())
            return value
        except (Exception, KeyboardInterrupt):
            item.update(status="FAIL", completed_at=now())
            path = receipt_path()
            if path is not None and path.is_file():
                item["receipt"] = file_reference(path)
            raise
        finally:
            downloads.write_json(directory / "result.json", summary)

    mass_dir, context = downloads.prepare(port, main_project, 1000000)
    summary["mass_context"] = str(mass_dir)
    summary["mass_project"] = context["project"]
    require(context["project"] != summary["original_evidence"]["project"], "CONTINUATION_FRESH_CONTEXT_REQUIRED")
    timeout = min(7200, int(deadline - time.monotonic()))
    require(timeout >= 60, "CONTINUATION_DEADLINE")
    execute([*guard.compose_args(mass_dir, context), "up", "--no-build", "--detach", "--wait", "--wait-timeout", "240",
             "postgres", "api", "worker", "acquisition-worker", "report-worker", "web"],
            directory, "api_1m_startup", deadline - time.monotonic(), environment=environment)
    summary["api_1m_effective_cgroups"] = downloads.effective_cgroups(
        read(mass_dir / "compose.json"), context["project"], set(downloads.SCOPES) | {"report-worker"})
    # This fixed entry point invokes the existing tier runner in a child whose
    # process tree/deadline/protected inventory are handled by execute().
    tier_entry = ("from pathlib import Path; import sys; "
                  "from scripts.tests.catalog_reports_cycle import report_tier; "
                  "import certification_v080 as guard; "
                  "directory,context=guard.load_context(Path(sys.argv[1])); "
                  "report_tier(directory,context,1000000,{})")
    stage(0, [sys.executable, "-c", tier_entry, str(mass_dir)],
          lambda: mass_dir / "reports-1000000.json", lambda value: validators.catalog_population(value, 1000000))
    timeout = min(7200, int(deadline - time.monotonic()))
    require(timeout >= 60, "CONTINUATION_DEADLINE")
    mass_value = stage(1, [sys.executable, str(ROOT / "scripts/tests/reports_download_085.py"),
        "--context", str(mass_dir), "--start", "--keep", "--rows", *map(str, ROWS),
        "--limit", "1000000", "--with-browser", "--source-sha", summary["source_sha"], "--timeout", str(timeout)],
        lambda: mass_dir / "reports-download-085.json",
        lambda value: validators.catalog_download_receipt(value, ROWS, summary["source_sha"]))
    summary["population_stopped_before_other_scopes"] = stop_mass(mass_dir, context)
    http_dir, _ = observation.prepare(port + 1, main_project)
    stage(2, [sys.executable, str(ROOT / "scripts/tests/reports_ephemeral_http.py"), "--context", str(http_dir)],
          lambda: http_dir / "reports-ephemeral-http.json", validators.ephemeral_http_observation)

    def recovery_path():
        paths = list(mass_dir.glob("catalog-recovery-*/result.json"))
        require(len(paths) <= 1, "CONTINUATION_AMBIGUOUS_RECOVERY_RECEIPT")
        return paths[0] if paths else None

    def recovery_valid(value):
        require(value.get("status") == "PASS" and value.get("mode") == "both"
                and all(value.get(scope, {}).get("status") == "PASS" for scope in ("native", "legacy", "legacy080")),
                "CONTINUATION_RECOVERY_BOTH_INCOMPLETE")
        # Adapt the existing recovery gate's envelope locally. Its `tiers` key
        # supplies DOWNLOAD result sizes; this adapter is not published as a
        # Catalog receipt and does not certify API120/API400k on the new SHA.
        envelope = {"status": value["status"], "main_unchanged": mass_value["main_unchanged"],
                    "source_tree_dirty": False, "source_sha": summary["source_sha"],
                    "host_download_085": mass_value, "recovery": value,
                    "tiers": [{"rows_per_source": rows} for rows in mass_value["requested_result_rows"]]}
        for mode in ("native", "legacy", "legacy080"):
            validators.validate_content({"validator": "catalog-recovery", "mode": mode}, envelope)

    stage(3, [sys.executable, str(ROOT / "scripts/tests/catalog_reports_recovery.py"), "--context", str(mass_dir), "--mode", "both"],
          recovery_path, recovery_valid)


def main(arguments=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--original-context", type=Path, required=True)
    parser.add_argument("--original-source-sha", required=True)
    parser.add_argument("--source-sha", required=True)
    parser.add_argument("--image-manifest", type=Path, required=True)
    parser.add_argument("--main-project", required=True)
    parser.add_argument("--port", type=int, default=32088)
    parser.add_argument("--max-cpus", type=int, choices=(4,), default=4)
    parser.add_argument("--max-memory-gib", type=int, choices=(10,), default=10)
    parser.add_argument("--timeout", type=int, default=GLOBAL_DEADLINE)
    parser.add_argument("--plan", action="store_true")
    args = parser.parse_args(arguments)
    require(not os.getenv("GITHUB_ACTIONS") and 32000 <= args.port <= 32998
            and 60 <= args.timeout <= GLOBAL_DEADLINE, "CONTINUATION_LOCAL_FINITE_SCOPE")
    proof = original_tiers(args.original_context, args.original_source_sha)
    plan = {"kind": "REPORTS_DEEP_CONTINUATION_085", "source_sha": args.source_sha, "original_evidence": proof,
            "full_catalog_approved": False, "automatic_approval_inheritance": False,
            "stages": [{"scope": name, "status": "PENDING"} for name in STAGES],
            "scope_not_executed": ["CATALOG_API_120", "CATALOG_API_400K", "STANDARD_CATALOG_BROWSER", "POSTGRES_SNAPSHOT", "BACKEND", "DELIVERY", "NATIVE_EXCEL"],
            "limits": {"max_cpus": 4, "max_memory_bytes": 10 * GIB, "deadline_seconds": args.timeout,
                       "mass_docker_cpus": 3, "mass_client_cpus": 1, "concurrency": 1},
            "candidate_images_built": False, "internal_image_builds": "EXISTING_REGISTERED_DIAGNOSTIC_AND_AUTHENTIC_HISTORICAL_RUNNERS"}
    if args.plan:
        print(json.dumps(plan, ensure_ascii=False, indent=2))
        return 0
    execution = "local-" + uuid4().hex
    directory = ROOT / ".codex-local/deep-continuation" / execution
    directory.mkdir(parents=True, exist_ok=False)
    for name in ("owned-projects.json", "owned-builders.json"):
        downloads.write_json(directory / name, [])
    summary = {**plan, "execution_id": execution, "status": "RUNNING", "started_at": now()}
    target = directory / "result.json"
    downloads.write_json(target, summary)
    os.environ.update(TRACKVANCE_SOURCE_SHA=args.source_sha, TRACKVANCE_LOCAL_EXECUTION_ID=execution,
        TRACKVANCE_LOCAL_IMAGE_MANIFEST=str(args.image_manifest.resolve()), TRACKVANCE_LOCAL_GROUP="catalog-reports",
        TRACKVANCE_LOCAL_PROJECT_REGISTRY=str(directory / "owned-projects.json"),
        TRACKVANCE_LOCAL_BUILDER_REGISTRY=str(directory / "owned-builders.json"),
        TRACKVANCE_LOCAL_IMAGE_REGISTRY=str(directory / "owned-images.json"),
        TRACKVANCE_LOCAL_MAX_CPUS="4", TRACKVANCE_LOCAL_MAX_MEMORY_BYTES=str(10 * GIB),
        TRACKVANCE_LOCAL_PROTECTED_INVENTORY=str(directory / "protected-inventory.before.json"),
        NODE_OPTIONS="--max-old-space-size=384", UV_THREADPOOL_SIZE="1", OMP_NUM_THREADS="1",
        OPENBLAS_NUM_THREADS="1", POLARS_MAX_THREADS="1", GOMAXPROCS="1")
    began = time.monotonic()
    before = protected = affinity = monitor = None
    baseline_builders = set()
    try:
        summary["verified_images"] = downloads.verify_code(args.source_sha)
        summary["source_provenance"] = source_provenance(args.original_source_sha, args.source_sha)
        summary["image_manifest"] = file_reference(args.image_manifest)
        summary["capacity"] = capacity()
        protected = run_local.protected_inventory()
        downloads.write_json(directory / "protected-inventory.before.json", protected)
        before = owned_cleanup.snapshot()
        baseline_builders = set(guard.command(["docker", "buildx", "ls", "--format", "{{.Name}}"]).split())
        downloads.write_json(directory / "builder-images.before.json",
            guard.command(["docker", "image", "ls", "-q", "--no-trunc", "moby/buildkit:buildx-stable-1"]).split())
        initialize_image_registry(directory / "owned-images.json", execution)
        affinity = limit_cpu_affinity(4)
        summary["runner_affinity"] = affinity
        monitor = run_local.ResourceMonitor(directory / "owned-projects.json")
        monitor.thread.start()
        run_scopes(directory, summary, dict(os.environ), began + args.timeout, args.port, args.main_project)
        summary["status"] = "PASS"
    except (Exception, KeyboardInterrupt) as error:  # noqa: BLE001 - private diagnostics and complete final cleanup.
        summary.update(status="FAIL", error_type=type(error).__name__)
        (directory / "failure.private.log").write_text(traceback.format_exc(), encoding="utf-8")
    finally:
        if monitor:
            summary["resources"] = monitor.finish()
        if before is not None:
            summary["cleanup"] = cleanup(directory, execution, before, baseline_builders)
            if summary["cleanup"]["status"] != "PASS":
                summary["status"] = "FAIL"
        try:
            verify_originals(proof)
            summary["original_evidence_unchanged"] = True
            if protected is not None:
                after = run_local.protected_inventory()
                downloads.write_json(directory / "protected-inventory.after.json", after)
                summary["protected_inventory_unchanged"] = protected == after
                require(protected == after, "CONTINUATION_PROTECTED_STATE_CHANGED")
        except (Exception, KeyboardInterrupt) as error:  # noqa: BLE001 - missing final evidence cannot grant approval.
            summary.update(status="FAIL", final_proof_error=type(error).__name__)
        if affinity:
            try:
                set_cpu_affinity(affinity["original_logical_processors"])
                summary["runner_affinity_restored"] = True
            except OSError as error:
                summary.update(status="FAIL", affinity_restore_error=type(error).__name__)
        summary.update(completed_at=now(), duration_seconds=round(time.monotonic() - began, 3),
                       closure="SCOPED_DEEP_EXECUTION_PASS" if summary["status"] == "PASS" else "SCOPED_DEEP_EXECUTION_INCOMPLETE")
        downloads.write_json(target, summary)
        print(json.dumps({"status": summary["status"], "scope": "REPORTS_DEEP_CONTINUATION_ONLY", "evidence": str(target)}))
    return 0 if summary["status"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
