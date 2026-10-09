"""Fail closed on missing functional evidence; document and deep approvals differ."""
from __future__ import annotations

import argparse
import json
import math
import os
import re
import subprocess
import zipfile
from datetime import datetime
from pathlib import Path
from typing import Any

try:
    from .common import (
        DIGEST,
        MANIFEST,
        ROOT,
        SHA,
        EvidenceError,
        load_json,
        load_manifest,
        profile_groups,
        relative_file,
        require,
        sha256,
    )
    from .executable_proof import GitHubAPI, executable_fingerprint, revalidate_inheritance
    from .validators import junit_document, validate_content
except ImportError:
    from common import (
        DIGEST,
        MANIFEST,
        ROOT,
        SHA,
        EvidenceError,
        load_json,
        load_manifest,
        profile_groups,
        relative_file,
        require,
        sha256,
    )
    from executable_proof import GitHubAPI, executable_fingerprint, revalidate_inheritance
    from validators import junit_document, validate_content


def validate_images(path: Path, source_sha: str) -> dict[str, str]:
    data = load_json(path)
    require(data.get("schema_version") == 1 and data.get("source_sha") == source_sha
            and data.get("status") == "PASS" and data.get("digest_kind") in {
                "DOCKER_CONFIGURATION_SHA256", "DOCKER_ENGINE_IMAGE_ID"},
            "IMAGE_MANIFEST_SHA_OR_STATUS")
    require(set(data.get("images", {})) == {"backend", "web"}, "INCOMPLETE_IMAGE_ROLES")
    roles = {}
    for role, descriptor in data["images"].items():
        require(re.fullmatch(r"sha256:[0-9a-f]{64}", descriptor.get("image_id", "")), "INVALID_IMAGE_DIGEST")
        require(descriptor.get("archive") == role + ".tar" and DIGEST.fullmatch(descriptor.get("archive_sha256", ""))
                and type(descriptor.get("archive_bytes")) is int and 0 < descriptor["archive_bytes"] <= 8 * 1024**3,
                "INVALID_IMAGE_ARCHIVE_DESCRIPTOR")
        if data["digest_kind"] == "DOCKER_ENGINE_IMAGE_ID":
            require(descriptor.get("image_id_kind") in {"CONFIGURATION_SHA256", "OCI_TARGET_SHA256"}
                    and re.fullmatch(r"sha256:[a-f0-9]{64}", descriptor.get("configuration_digest", ""))
                    and isinstance(descriptor.get("rootfs_diff_ids"), list)
                    and len(descriptor["rootfs_diff_ids"]) <= 256
                    and all(isinstance(value, str) and re.fullmatch(r"sha256:[a-f0-9]{64}", value)
                            for value in descriptor["rootfs_diff_ids"]), "INVALID_IMAGE_CONTENT_IDENTITY")
        roles[role] = descriptor["image_id"]
    return roles


def validate_jobs(jobs: dict[str, Any], source_sha: str, run_id: str, run_attempt: str,
                  *, documentation_only: bool = False) -> None:
    require(jobs.get("source_sha") == source_sha and str(jobs.get("run_id")) == run_id
            and str(jobs.get("run_attempt")) == run_attempt, "JOBS_COMMIT_OR_RUN_MISMATCH")
    require(isinstance(jobs.get("needs"), dict), "MISSING_NEEDS_SNAPSHOT")
    required = ("select", "documentation") if documentation_only else ("select", "backend", "frontend", "images", "suites")
    for name in required:
        entry = jobs["needs"].get(name)
        require(isinstance(entry, dict) and entry.get("result") == "success", "JOB_INCOMPLETE_FAILED_CANCELLED_OR_SKIPPED")
    if documentation_only:
        require(all(jobs["needs"].get(name, {}).get("result") == "skipped"
                    for name in ("backend", "frontend", "images", "suites")), "PRODUCT_JOBS_IN_DOCUMENTATION_ONLY_RUN")


def validate_documentation_evidence(path: Path, selection: dict[str, Any], selection_path: Path,
                                    source_sha: str, run_id: str, run_attempt: str) -> list[dict]:
    try:
        from .check_documentation import validate_documentation
        from .common import ROOT
    except ImportError:
        from check_documentation import validate_documentation
        from common import ROOT
    data = load_json(path)
    require(data.get("schema_version") == 1 and data.get("kind") == "DOCUMENTATION_EVIDENCE"
            and data.get("status") == "PASS" and not data.get("error_code"), "DOCUMENTATION_FAILED_OR_MISSING")
    require(data.get("source_sha") == source_sha and data.get("selection_sha256") == sha256(selection_path)
            and data.get("ci") == {"run_id": run_id, "run_attempt": run_attempt, "job_id": "documentation"},
            "DOCUMENTATION_COMMIT_OR_RUN_MISMATCH")
    expected = validate_documentation(ROOT, [item["path"] for item in selection["changed_files"]])
    require(data.get("documents") == expected, "DOCUMENTATION_CONTENT_OR_COVERAGE_MISMATCH")
    return expected


def validate_timing(record: dict[str, Any]) -> None:
    try:
        started = datetime.fromisoformat(record["started_at"])
        finished = datetime.fromisoformat(record["completed_at"])
        duration = record["duration_seconds"]
        require(started.tzinfo is not None and finished.tzinfo is not None, "TIMING_TIMEZONE_MISSING")
        elapsed = (finished - started).total_seconds()
        require(type(duration) in {float, int} and math.isfinite(duration) and duration >= 0 and elapsed >= 0,
                "INVALID_SCENARIO_TIMING")
        require(abs(elapsed - duration) <= max(5, elapsed * 0.1), "SCENARIO_TIMING_INCONSISTENT")
    except (KeyError, TypeError, ValueError) as error:
        if isinstance(error, EvidenceError):
            raise
        raise EvidenceError("MISSING_OR_INVALID_SCENARIO_TIMING") from error


def validate_attachments(record: dict[str, Any], receipt: Path) -> dict[str, str]:
    attachments = record.get("evidence")
    require(isinstance(attachments, list) and attachments, "MISSING_EVIDENCE_ATTACHMENTS")
    seen: set[tuple[str, str]] = set()
    results = []
    sources: dict[str, Any] = {}
    observed: dict[str, str] = {}
    for entry in attachments:
        require(isinstance(entry, dict) and set(entry) == {"path", "sha256", "kind"}, "INVALID_ATTACHMENT_DESCRIPTOR")
        require(DIGEST.fullmatch(entry.get("sha256", "")) and isinstance(entry.get("kind"), str), "INVALID_ATTACHMENT_DIGEST")
        identity = (entry["path"], entry["kind"])
        require(identity not in seen, "DUPLICATE_ATTACHMENT")
        seen.add(identity)
        path = relative_file(receipt.parent, entry["path"])
        require(sha256(path) == entry["sha256"], "ALTERED_EVIDENCE_ATTACHMENT")
        observed[entry["path"]] = entry["sha256"]
        if entry["kind"] == "result":
            results.append(load_json(path))
        if entry["kind"].startswith("source-"):
            name = entry["kind"][7:]
            require(name not in sources, "DUPLICATE_SOURCE_ATTACHMENT")
            sources[name] = junit_document(path) if path.suffix.lower() == ".xml" else load_json(path)
    require(len(results) == 1 and results[0] == record.get("result"), "RESULT_SNAPSHOT_MISSING_OR_DIFFERENT")
    if "documents" in record.get("result", {}):
        require(sources == record["result"]["documents"], "ORIGINAL_SUITE_ATTACHMENT_MISMATCH")
    return observed


def evaluate(*, manifest_path: Path, selection_path: Path, evidence_dir: Path,
             jobs_path: Path, image_manifest_path: Path, source_sha: str,
             run_id: str, run_attempt: str, development_only: bool = False,
             documentation_path: Path | None = None, repository: str = "", branch: str = "",
             approval_api: GitHubAPI | None = None, repository_root: Path | None = None) -> dict[str, Any]:
    require(bool(SHA.fullmatch(source_sha)) and run_id.isdigit() and int(run_id) > 0
            and run_attempt.isdigit() and int(run_attempt) > 0, "INVALID_EXPECTED_CI_IDENTITY")
    manifest = load_manifest(manifest_path)
    manifest_hash = sha256(manifest_path)
    selection = load_json(selection_path)
    require(selection.get("schema_version") == 1 and selection.get("source_sha") == source_sha
            and selection.get("manifest_sha256") == manifest_hash, "SELECTION_COMMIT_OR_MANIFEST_MISMATCH")
    fingerprint = executable_fingerprint(source_sha, root=repository_root or ROOT)
    require(selection.get("executable_fingerprint") == fingerprint, "SELECTION_EXECUTABLE_FINGERPRINT_MISMATCH")
    mode = selection.get("mode")
    require(not development_only, "LEGACY_DEVELOPMENT_GATE_REMOVED")
    if mode == "docs":
        require(selection.get("groups") == [] and selection.get("scope") == "DOCUMENTATION_ONLY",
                "INVALID_DOCUMENTATION_ONLY_SELECTION")
        inheritance = revalidate_inheritance(selection.get("functional_inheritance"),
            repository=repository, branch=branch, current_sha=source_sha, fingerprint=fingerprint,
            manifest_path=manifest_path, api=approval_api, root=repository_root or ROOT)
        validate_jobs(load_json(jobs_path), source_sha, run_id, run_attempt, documentation_only=True)
        require(documentation_path is not None, "MISSING_DOCUMENTATION_EVIDENCE")
        documents = validate_documentation_evidence(documentation_path, selection, selection_path,
                                                   source_sha, run_id, run_attempt)
        return {"schema_version": 1, "status": "PASS", "scope": "DOCUMENTATION_APPROVED", "profile": "functional",
                "certifies_final": False, "functional_approved": False, "deep_approved": False,
                "current_functional_executed": False, "inherited_executable_approved": True,
                "functional_inheritance": inheritance, "executable_fingerprint": fingerprint,
                "source_sha": source_sha, "run_id": run_id, "run_attempt": run_attempt,
                "manifest_sha256": manifest_hash, "groups": [], "scenario_count": 0, "documents": documents}
    # Deep certification is local and must use its own execution identity. A
    # GitHub status cannot approve intensive work that was not executed there.
    require(mode == "functional" and selection.get("profile") == "functional", "NON_FUNCTIONAL_GITHUB_SELECTION")
    require(selection.get("functional_inheritance") is None, "FUNCTIONAL_CANNOT_REUSE_ANCESTOR_RECEIPTS")
    groups = profile_groups(manifest, "functional")
    require(selection.get("groups") == groups, "INCOMPLETE_OR_DUPLICATE_SELECTION_GROUPS")
    validate_jobs(load_json(jobs_path), source_sha, run_id, run_attempt)
    roles = validate_images(image_manifest_path, source_sha)
    image_hash = sha256(image_manifest_path)
    image_document = load_json(image_manifest_path)
    host_roles_by_group = {}
    expected: dict[str, tuple[dict[str, Any], dict[str, Any]]] = {}
    for group in manifest["groups"]:
        if group["id"] in groups:
            for scenario in group["scenarios"]:
                expected[scenario["id"]] = (group, scenario)
    require(evidence_dir.is_dir() and not evidence_dir.is_symlink(), "MISSING_EVIDENCE_DIRECTORY")
    found: dict[str, dict[str, Any]] = {}
    receipts: list[dict[str, Any]] = []
    for path in sorted(evidence_dir.rglob("scenario-*.json")):
        # Original XLSX progress/prerequisites are diagnostics. Only independently
        # normalized receipts count; a missing normalized terminal receipt fails.
        record = load_json(path)
        kind = record.get("kind")
        if kind in {"XLSX_SCENARIO", "XLSX_PREREQUISITE", "SCENARIO_PROGRESS", "XLSX_PROGRESS"}:
            continue
        require(kind == "SCENARIO_EVIDENCE", "UNKNOWN_RECEIPT_KIND")
        require(record.get("schema_version") == 1, "INVALID_RECEIPT_SCHEMA")
        scenario_id = record.get("scenario_id")
        require(isinstance(scenario_id, str) and scenario_id in expected, "UNEXPECTED_SCENARIO")
        require(scenario_id not in found, "DUPLICATE_SCENARIO")
        group, spec = expected[scenario_id]
        require(record.get("group") == group["id"], "WRONG_SCENARIO_GROUP")
        require(record.get("source_sha") == source_sha, "SCENARIO_COMMIT_MISMATCH")
        require(record.get("ci") == {"run_id": run_id, "run_attempt": run_attempt, "job_id": group["job_key"]},
                "SCENARIO_RUN_ATTEMPT_OR_JOB_MISMATCH")
        require(not record.get("execution"), "LOCAL_EVIDENCE_CANNOT_APPROVE_GITHUB")
        require(record.get("status") == "PASS" and not record.get("error"), "SCENARIO_FAILED_CANCELLED_SKIPPED_OR_RUNNING")
        require(record.get("rows") == spec.get("rows") and record.get("variant") == spec.get("variant"), "SCENARIO_ROWS_OR_VARIANT_MISMATCH")
        validate_timing(record)
        resources = record.get("resources")
        require(isinstance(resources, dict) and resources.get("profile") == "functional", "SCENARIO_PROFILE_METADATA_MISMATCH")
        attachments = validate_attachments(record, path)
        if group["id"] not in {"backend", "frontend"}:
            require(resources.get("image_bundle_sha256") == image_hash, "SCENARIO_IMAGE_BUNDLE_MISMATCH")
            mapping_refs = [entry for entry in record["evidence"] if entry["kind"] == "source-image_host_mapping"]
            if mapping_refs:
                require(len(mapping_refs) == 1 and resources.get("host_image_mapping_sha256") == mapping_refs[0]["sha256"],
                        "SCENARIO_HOST_MAPPING_HASH_MISMATCH")
                try:
                    from ci.image_bundle import mapping_images

                    mapped = mapping_images(image_document, image_hash, source_sha,
                        load_json(relative_file(path.parent, mapping_refs[0]["path"])))
                except (ValueError, KeyError, TypeError, AttributeError) as error:
                    raise EvidenceError("SCENARIO_HOST_MAPPING_CONTENT_MISMATCH") from error
            else:
                require(image_document["digest_kind"] == "DOCKER_CONFIGURATION_SHA256"
                        and not resources.get("host_image_mapping_sha256"), "SCENARIO_HOST_MAPPING_MISSING")
                mapped = roles
            require(resources.get("runtime_images") == mapped, "SCENARIO_IMAGE_BUNDLE_MISMATCH")
            previous = host_roles_by_group.setdefault(group["id"], mapped)
            require(previous == mapped, "SCENARIO_HOST_IMAGE_DRIFT")
        validate_content(spec, record["result"], execution_profile="functional")
        found[scenario_id] = record
        receipts.append({"scenario_id": scenario_id, "group": group["id"], "path": path.relative_to(evidence_dir).as_posix(),
                         "sha256": sha256(path), "rows": record.get("rows"),
                         "duration_seconds": record["duration_seconds"], "attachments": attachments})
    missing = sorted(set(expected) - found.keys())
    require(not missing, "MISSING_MANDATORY_SCENARIOS")
    return {"schema_version": 1, "status": "PASS", "scope": "CI_FUNCTIONAL_APPROVED", "profile": "functional",
            "certifies_final": False, "functional_approved": True, "deep_approved": False, "source_sha": source_sha,
            "run_id": run_id, "run_attempt": run_attempt, "manifest_sha256": manifest_hash,
            "executable_fingerprint": fingerprint, "current_functional_executed": True,
            "inherited_executable_approved": False,
            "image_manifest_sha256": image_hash, "source_images": roles,
            "runtime_images": (next(iter(host_roles_by_group.values())) if host_roles_by_group
                and len({tuple(sorted(value.items())) for value in host_roles_by_group.values()}) == 1
                else roles if not host_roles_by_group else None),
            "runtime_images_by_group": host_roles_by_group, "groups": groups,
            "scenario_count": len(found), "module_coverage": manifest["profiles"]["functional"]["module_coverage"], "receipts": receipts}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, default=MANIFEST)
    parser.add_argument("--selection", required=True, type=Path)
    parser.add_argument("--evidence-dir", required=True, type=Path)
    parser.add_argument("--jobs", required=True, type=Path)
    parser.add_argument("--image-manifest", type=Path, default=Path(".codex-local/ci/images/images.json"))
    parser.add_argument("--documentation-evidence", type=Path)
    parser.add_argument("--sha", required=True)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--run-attempt", required=True)
    parser.add_argument("--repository", default=os.environ.get("GITHUB_REPOSITORY", ""))
    parser.add_argument("--branch", default=os.environ.get("CI_BRANCH", ""))
    parser.add_argument("--development-only", action="store_true")
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    try:
        result = evaluate(manifest_path=args.manifest, selection_path=args.selection, evidence_dir=args.evidence_dir,
                          jobs_path=args.jobs, image_manifest_path=args.image_manifest, source_sha=args.sha,
                          run_id=args.run_id, run_attempt=args.run_attempt, development_only=args.development_only,
                          documentation_path=args.documentation_evidence, repository=args.repository, branch=args.branch)
        exit_code = 0
    except (EvidenceError, OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError, zipfile.BadZipFile) as error:
        result = {"schema_version": 1, "status": "FAIL", "certifies_final": False, "source_sha": args.sha,
                  "run_id": args.run_id, "run_attempt": args.run_attempt,
                  "error_code": str(error) if isinstance(error, EvidenceError) else type(error).__name__}
        exit_code = 1
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({key: result[key] for key in ("status", "certifies_final", "source_sha")}))
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
