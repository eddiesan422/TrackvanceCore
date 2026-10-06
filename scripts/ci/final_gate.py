"""Fail closed on incomplete certification; fast validation cannot certify a release."""
from __future__ import annotations

import argparse
import json
import math
import re
from datetime import datetime
from pathlib import Path
from typing import Any

try:
    from .common import (
        DIGEST,
        MANIFEST,
        SHA,
        EvidenceError,
        load_json,
        load_manifest,
        relative_file,
        require,
        sha256,
    )
    from .validators import junit_document, validate_content
except ImportError:
    from common import (
        DIGEST,
        MANIFEST,
        SHA,
        EvidenceError,
        load_json,
        load_manifest,
        relative_file,
        require,
        sha256,
    )
    from validators import junit_document, validate_content


def validate_images(path: Path, source_sha: str) -> dict[str, str]:
    data = load_json(path)
    require(data.get("schema_version") == 1 and data.get("source_sha") == source_sha
            and data.get("status") == "PASS" and data.get("digest_kind") == "DOCKER_CONFIGURATION_SHA256",
            "IMAGE_MANIFEST_SHA_OR_STATUS")
    require(set(data.get("images", {})) == {"backend", "web"}, "INCOMPLETE_IMAGE_ROLES")
    roles = {}
    for role, descriptor in data["images"].items():
        require(re.fullmatch(r"sha256:[0-9a-f]{64}", descriptor.get("image_id", "")), "INVALID_IMAGE_DIGEST")
        require(descriptor.get("archive") == role + ".tar" and DIGEST.fullmatch(descriptor.get("archive_sha256", ""))
                and type(descriptor.get("archive_bytes")) is int and 0 < descriptor["archive_bytes"] <= 8 * 1024**3,
                "INVALID_IMAGE_ARCHIVE_DESCRIPTOR")
        roles[role] = descriptor["image_id"]
    return roles


def validate_jobs(jobs: dict[str, Any], source_sha: str, run_id: str, run_attempt: str) -> None:
    require(jobs.get("source_sha") == source_sha and str(jobs.get("run_id")) == run_id
            and str(jobs.get("run_attempt")) == run_attempt, "JOBS_COMMIT_OR_RUN_MISMATCH")
    require(isinstance(jobs.get("needs"), dict), "MISSING_NEEDS_SNAPSHOT")
    for name in ("backend", "frontend", "images", "suites"):
        entry = jobs["needs"].get(name)
        require(isinstance(entry, dict) and entry.get("result") == "success", "JOB_INCOMPLETE_FAILED_CANCELLED_OR_SKIPPED")


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
             run_id: str, run_attempt: str, development_only: bool = False) -> dict[str, Any]:
    require(bool(SHA.fullmatch(source_sha)) and run_id.isdigit() and int(run_id) > 0
            and run_attempt.isdigit() and int(run_attempt) > 0, "INVALID_EXPECTED_CI_IDENTITY")
    manifest = load_manifest(manifest_path)
    manifest_hash = sha256(manifest_path)
    selection = load_json(selection_path)
    require(selection.get("schema_version") == 1 and selection.get("source_sha") == source_sha
            and selection.get("manifest_sha256") == manifest_hash, "SELECTION_COMMIT_OR_MANIFEST_MISMATCH")
    mode = selection.get("mode")
    if development_only:
        require(mode == "fast", "DEVELOPMENT_FLAG_WITH_FULL_MODE")
        groups = manifest["fast_groups"]
    else:
        require(mode == "full", "FAST_IS_NOT_FINAL_CERTIFICATION")
        groups = [g["id"] for g in manifest["groups"]]
    require(selection.get("groups") == groups, "INCOMPLETE_OR_DUPLICATE_SELECTION_GROUPS")
    validate_jobs(load_json(jobs_path), source_sha, run_id, run_attempt)
    roles = validate_images(image_manifest_path, source_sha)
    image_hash = sha256(image_manifest_path)
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
        require(record.get("status") == "PASS" and not record.get("error"), "SCENARIO_FAILED_CANCELLED_SKIPPED_OR_RUNNING")
        require(record.get("rows") == spec.get("rows") and record.get("variant") == spec.get("variant"), "SCENARIO_ROWS_OR_VARIANT_MISMATCH")
        validate_timing(record)
        resources = record.get("resources")
        require(isinstance(resources, dict), "SCENARIO_RESOURCE_METADATA_MISSING")
        if group["id"] not in {"backend", "frontend"}:
            require(resources.get("image_bundle_sha256") == image_hash and resources.get("runtime_images") == roles,
                    "SCENARIO_IMAGE_BUNDLE_MISMATCH")
        attachments = validate_attachments(record, path)
        validate_content(spec, record["result"])
        found[scenario_id] = record
        receipts.append({"scenario_id": scenario_id, "group": group["id"], "path": path.relative_to(evidence_dir).as_posix(),
                         "sha256": sha256(path), "rows": record.get("rows"),
                         "duration_seconds": record["duration_seconds"], "attachments": attachments})
    missing = sorted(set(expected) - found.keys())
    require(not missing, "MISSING_MANDATORY_SCENARIOS")
    return {"schema_version": 1, "status": "PASS", "scope": "DEVELOPMENT_ONLY" if development_only else "FULL_CERTIFICATION",
            "certifies_final": not development_only, "source_sha": source_sha,
            "run_id": run_id, "run_attempt": run_attempt, "manifest_sha256": manifest_hash,
            "image_manifest_sha256": image_hash, "runtime_images": roles, "groups": groups,
            "scenario_count": len(found), "receipts": receipts}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, default=MANIFEST)
    parser.add_argument("--selection", required=True, type=Path)
    parser.add_argument("--evidence-dir", required=True, type=Path)
    parser.add_argument("--jobs", required=True, type=Path)
    parser.add_argument("--image-manifest", required=True, type=Path)
    parser.add_argument("--sha", required=True)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--run-attempt", required=True)
    parser.add_argument("--development-only", action="store_true")
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    try:
        result = evaluate(manifest_path=args.manifest, selection_path=args.selection, evidence_dir=args.evidence_dir,
                          jobs_path=args.jobs, image_manifest_path=args.image_manifest, source_sha=args.sha,
                          run_id=args.run_id, run_attempt=args.run_attempt, development_only=args.development_only)
        exit_code = 0
    except (EvidenceError, OSError, ValueError, KeyError, TypeError) as error:
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
