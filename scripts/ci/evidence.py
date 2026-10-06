"""Create unique incremental receipts without changing historical suite artifacts."""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

try:
    from .common import (
        IDENTIFIER,
        ROOT,
        SHA,
        EvidenceError,
        group_spec,
        load_json,
        load_manifest,
        relative_file,
        require,
        sha256,
    )
    from .validators import junit_document, validate_content
except ImportError:
    from common import (
        IDENTIFIER,
        ROOT,
        SHA,
        EvidenceError,
        group_spec,
        load_json,
        load_manifest,
        relative_file,
        require,
        sha256,
    )
    from validators import junit_document, validate_content


def utc_now() -> str:
    return datetime.now(UTC).isoformat()


def ci_context() -> dict[str, str]:
    if os.environ.get("TRACKVANCE_LOCAL_EXECUTION_ID"):
        return {"kind": "LOCAL", "execution_id": os.environ["TRACKVANCE_LOCAL_EXECUTION_ID"],
                "group": os.environ.get("TRACKVANCE_LOCAL_GROUP", "standalone")}
    result = {"run_id": os.environ.get("GITHUB_RUN_ID", ""),
              "run_attempt": os.environ.get("GITHUB_RUN_ATTEMPT", ""),
              "job_id": os.environ.get("CI_JOB_ID") or os.environ.get("GITHUB_JOB", "")}
    require(all(result.values()), "MISSING_CI_IDENTITY")
    return result


def source_head() -> str:
    result = subprocess.run(["git", "rev-parse", "HEAD"], cwd=ROOT,
                            capture_output=True, text=True, check=True)
    value = result.stdout.strip()
    require(bool(SHA.fullmatch(value)), "INVALID_SOURCE_SHA")
    return value


def artifact_ref(path: Path, base: Path, kind: str = "result") -> dict[str, str]:
    path, base = path.resolve(), base.resolve()
    require(path.is_relative_to(base) and path.is_file(), "ATTACHMENT_OUTSIDE_EVIDENCE_ROOT")
    require(bool(IDENTIFIER.fullmatch(kind)), "INVALID_ATTACHMENT_KIND")
    return {"path": path.relative_to(base).as_posix(), "sha256": sha256(path), "kind": kind}


def _write_exclusive(path: Path, value: Any) -> None:
    encoded = json.dumps(value, ensure_ascii=True, allow_nan=False, indent=2) + "\n"
    with path.open("x", encoding="utf-8") as stream:
        stream.write(encoded)


def write_receipt(output_dir: Path, *, group: str, scenario_id: str,
                  result: dict[str, Any], attachments: list[dict[str, str]] | None = None,
                  rows: int | None = None, variant: str | None = None, status: str = "PASS",
                  started_at: str | None = None, completed_at: str | None = None,
                  duration_seconds: float | None = None, resources: dict[str, Any] | None = None,
                  source_sha: str | None = None, ci: dict[str, Any] | None = None,
                  kind: str = "SCENARIO_EVIDENCE", error: dict[str, str] | None = None) -> Path:
    """Write a new UUID receipt plus its immutable result attachment.

    This is a recorder, not an assertion of certification. The gate independently
    validates result contents and every attachment. Callers must not reuse PASS.
    """
    require(bool(IDENTIFIER.fullmatch(group)) and bool(IDENTIFIER.fullmatch(scenario_id)), "INVALID_RECEIPT_ID")
    require(status in {"RUNNING", "PASS", "FAIL"}, "INVALID_SCENARIO_STATUS")
    require(isinstance(result, dict), "INVALID_SCENARIO_RESULT")
    source_sha = source_sha or source_head()
    require(bool(SHA.fullmatch(source_sha)), "INVALID_SOURCE_SHA")
    ci = ci or ci_context()
    local = ci.get("kind") == "LOCAL"
    require((set(ci) == {"kind", "execution_id", "group"} if local else
             set(ci) == {"run_id", "run_attempt", "job_id"}) and all(str(v) for v in ci.values()), "INVALID_CI_IDENTITY")
    output_dir.mkdir(parents=True, exist_ok=True)
    token = uuid4().hex
    result_path = output_dir / f"result-{scenario_id}-{token}.json"
    _write_exclusive(result_path, result)
    now = utc_now()
    start = started_at or now
    finish = completed_at or (now if status != "RUNNING" else None)
    elapsed = duration_seconds if duration_seconds is not None else (datetime.fromisoformat(finish) - datetime.fromisoformat(start)).total_seconds() if finish else None
    record = {"schema_version": 1, "kind": kind, "group": group, "scenario_id": scenario_id,
              "source_sha": source_sha, "ci": None if local else {key: str(value) for key, value in ci.items()},
              **({"execution": ci} if local else {}),
              "status": status, "rows": rows, "variant": variant, "started_at": start,
              "completed_at": finish, "duration_seconds": elapsed, "resources": resources or {},
              "result": result, "evidence": [artifact_ref(result_path, output_dir), *(attachments or [])]}
    if error:
        require(set(error) <= {"type", "code"} and all(IDENTIFIER.fullmatch(v) for v in error.values()), "UNSAFE_ERROR_DETAIL")
        record["error"] = error
    target = output_dir / f"scenario-{scenario_id}-{token}.json"
    _write_exclusive(target, record)
    return target


def copy_attachment(path: Path, output_dir: Path, kind: str) -> dict[str, str]:
    """Copy explicit safe artifacts; never glob static baseline archives."""
    require(path.is_file() and not path.is_symlink(), "MISSING_SUITE_ATTACHMENT")
    destination = output_dir / "attachments" / f"{uuid4().hex}-{path.name}"
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(path, destination)
    return artifact_ref(destination, output_dir, kind)


def wrap_group(group: str, sources: dict[str, Path], output_dir: Path, *, source_sha: str,
               ci: dict[str, Any], started_at: str, completed_at: str,
               duration_seconds: float, resources: dict[str, Any] | None = None,
               manifest: dict[str, Any] | None = None) -> list[Path]:
    """Validate explicit current outputs, then snapshot mandatory scenarios.

    Parent runners select outputs created by their own invocation. Baseline
    archive paths are never discovered here or accepted through a broad glob.
    """
    manifest = manifest or load_manifest()
    spec = group_spec(manifest, group)
    require(set(spec["required_sources"]) <= sources.keys(), "MISSING_REQUIRED_SUITE_SOURCE")
    output_dir.mkdir(parents=True, exist_ok=True)
    documents = {name: junit_document(path) if path.suffix.lower() == ".xml" else load_json(path)
                 for name, path in sources.items()}
    original = documents.get("result", {})
    refs = [copy_attachment(path, output_dir, "source-" + name) for name, path in sources.items()]
    results: dict[str, dict[str, Any]] = {}
    original_meta: dict[str, dict[str, Any]] = {}
    if isinstance(original, dict) and original.get("kind") == "XLSX_GROUP":
        require(original.get("group") == group and original.get("source_sha") == source_sha
                and original.get("status") == "PASS" and original.get("xlsx_test_limit_overrides") is (group == "corrections-functional"),
                "INVALID_XLSX_GROUP")
        identity = original.get("execution") if ci.get("kind") == "LOCAL" else original.get("ci")
        require({k: str(v) for k, v in (identity or {}).items()} == {k: str(v) for k, v in ci.items()}, "XLSX_CI_IDENTITY_MISMATCH")
        required = {s["id"] for s in spec["scenarios"]}
        entries = original.get("scenario_results", [])
        require(len(entries) == len(required) and {e.get("scenario_id") for e in entries} == required,
                "XLSX_MISSING_OR_DUPLICATE_SCENARIO")
        require(set(original.get("required_scenarios", [])) == required, "XLSX_MANIFEST_COVERAGE_MISMATCH")
        for entry in entries:
            path = relative_file(sources["result"].parent, entry["path"])
            require(sha256(path) == entry["sha256"], "XLSX_RECEIPT_HASH_MISMATCH")
            record = load_json(path)
            require(record.get("kind") == "XLSX_SCENARIO" and record.get("scenario_id") == entry["scenario_id"]
                    and record.get("group") == group and record.get("source_sha") == source_sha
                    and record.get("status") == "PASS", "INVALID_XLSX_SCENARIO")
            expected_scenario = next(s for s in spec["scenarios"] if s["id"] == entry["scenario_id"])
            require(record.get("rows") == expected_scenario.get("rows")
                    and record.get("variant") == expected_scenario.get("variant"), "XLSX_ROWS_OR_VARIANT_MISMATCH")
            identity = record.get("execution") if ci.get("kind") == "LOCAL" else record.get("ci")
            require({k: str(v) for k, v in (identity or {}).items()} == {k: str(v) for k, v in ci.items()}, "XLSX_CI_IDENTITY_MISMATCH")
            attachments = record.get("evidence", [])
            require(isinstance(attachments, list) and attachments, "XLSX_ATTACHMENT_MISSING")
            for attachment in attachments:
                attached = relative_file(path.parent, attachment["path"])
                require(sha256(attached) == attachment["sha256"], "XLSX_ATTACHMENT_HASH_MISMATCH")
            results[entry["scenario_id"]] = record["result"]
            original_meta[entry["scenario_id"]] = record
            refs.append(copy_attachment(path, output_dir, "original-" + entry["scenario_id"]))
    else:
        for doc in documents.values():
            if isinstance(doc, dict) and "source_sha" in doc:
                require(doc["source_sha"] == source_sha, "SUITE_SOURCE_SHA_MISMATCH")
            if isinstance(doc, dict) and "source_tree_dirty" in doc:
                require(doc["source_tree_dirty"] is False, "SUITE_DIRTY_SOURCE_TREE")
        results = {scenario["id"]: {"documents": documents} for scenario in spec["scenarios"]}
    paths = []
    for scenario in spec["scenarios"]:
        result = results[scenario["id"]]
        validate_content(scenario, result, execution_profile=(resources or {}).get("profile"))
        meta = original_meta.get(scenario["id"], {})
        paths.append(write_receipt(output_dir, group=group, scenario_id=scenario["id"], result=result,
            attachments=refs, rows=scenario.get("rows"), variant=scenario.get("variant"),
            source_sha=source_sha, ci=ci, started_at=meta.get("started_at", started_at),
            completed_at=meta.get("completed_at", completed_at),
            duration_seconds=meta.get("duration_seconds", duration_seconds), resources=resources))
    return paths


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--group", required=True)
    parser.add_argument("--scenario", required=True)
    parser.add_argument("--result-file", required=True, type=Path)
    parser.add_argument("--output-dir", required=True, type=Path)
    parser.add_argument("--source-sha")
    parser.add_argument("--rows", type=int)
    parser.add_argument("--variant")
    parser.add_argument("--started-at")
    parser.add_argument("--completed-at")
    parser.add_argument("--duration-seconds", type=float)
    parser.add_argument("--status", choices=["PASS", "FAIL", "RUNNING"], default="PASS")
    args = parser.parse_args()
    try:
        target = write_receipt(args.output_dir, group=args.group, scenario_id=args.scenario,
            result=load_json(args.result_file), rows=args.rows, variant=args.variant, status=args.status,
            source_sha=args.source_sha, started_at=args.started_at, completed_at=args.completed_at,
            duration_seconds=args.duration_seconds)
        print(json.dumps({"receipt": str(target), "sha256": sha256(target), "status": args.status}))
        return 0
    except (EvidenceError, OSError, ValueError) as error:
        print(json.dumps({"status": "FAIL", "error_code": str(error) if isinstance(error, EvidenceError) else type(error).__name__}))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
