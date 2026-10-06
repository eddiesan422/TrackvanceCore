"""Conservative fast/full selection; a fast success is never final certification."""
from __future__ import annotations

import argparse
import json
import subprocess
from pathlib import Path, PurePosixPath
from typing import Any

try:
    from .common import (
        MANIFEST,
        ROOT,
        SHA,
        EvidenceError,
        load_json,
        load_manifest,
        require,
        sha256,
    )
except ImportError:
    from common import MANIFEST, ROOT, SHA, EvidenceError, load_json, load_manifest, require, sha256


def impact(path: str) -> str:
    """Unknown/transverse changes fail closed to full, including test harnesses."""
    require(isinstance(path, str) and path and "\\" not in path, "UNCERTAIN_CHANGED_PATH")
    parsed = PurePosixPath(path)
    require(not parsed.is_absolute() and ".." not in parsed.parts and ":" not in path, "UNCERTAIN_CHANGED_PATH")
    if path.startswith((".github/", "scripts/", "deploy/", "backend/src/", "backend/alembic/", "frontend/src/")):
        return "transverse-runtime-or-harness"
    if any(token in path.lower() for token in ("docker", "migration", "lock", "package.json", "pyproject", "requirements", "openapi", "contract")):
        return "dependency-container-or-contract"
    if path.startswith("docs/"):
        if any(token in path.lower() for token in ("runtime", "architecture", "architectural", "adr", "operation", "security", "validation", "specification", "development")):
            return "runtime-documentation"
        if path.endswith((".md", ".txt")):
            return "fast-documentation"
        return "uncertain-document"
    if path in {"README.md", "LICENSE", ".gitignore", ".gitattributes"}:
        return "fast-documentation"
    if path.startswith(("backend/tests/", "frontend/tests/", "frontend/tests-e2e/")):
        return "test-impact-requires-full"
    return "uncertain-source-path"


def select(mode: str, changed_files: list[str] | None, *, source_sha: str,
           manifest_path: Path = MANIFEST, diff_uncertain: bool = False,
           commit_message: str = "", cache_mode: str = "warm") -> dict[str, Any]:
    require(mode in {"auto", "fast", "full"}, "INVALID_SELECTION_MODE")
    require(bool(SHA.fullmatch(source_sha)), "INVALID_SOURCE_SHA")
    require(cache_mode in {"cold", "warm"}, "INVALID_CACHE_MODE")
    manifest = load_manifest(manifest_path)
    reasons: list[str] = []
    paths: list[dict[str, str]] = []
    if mode == "full" or "[ci full]" in commit_message.lower():
        reasons.append("explicit-full")
    if diff_uncertain or changed_files is None:
        reasons.append("uncertain-diff")
    for path in changed_files or []:
        try:
            rule = impact(path)
        except EvidenceError:
            rule = "uncertain-source-path"
        paths.append({"path": path, "rule": rule})
        if rule != "fast-documentation":
            reasons.append(rule)
    selected_mode = "full" if reasons else "fast"
    groups = [g["id"] for g in manifest["groups"]] if selected_mode == "full" else manifest["fast_groups"]
    priority = ["catalog-reports", "corrections-recovery", "corrections-acquisition", "corrections-dispatch",
                "corrections-browser", "async-volume-1024", "async-volume-100", "async-volume-500"]
    suites = [g for g in manifest["groups"] if g["id"] in groups and g["id"] not in {"backend", "frontend"}]
    suites.sort(key=lambda g: priority.index(g["id"]) if g["id"] in priority else len(priority))
    matrix = {"include": [{"group": g["id"], "job_key": g["job_key"], "timeout_minutes": g["timeout_minutes"]}
                          for g in suites]}
    return {"schema_version": 1, "mode": selected_mode, "requested_mode": mode,
            "source_sha": source_sha, "certifies_final": False,
            "final_eligible": selected_mode == "full", "cache_mode": cache_mode,
            "reason": sorted(set(reasons)) or ["bounded-development-validation"],
            "changed_files": paths, "groups": groups, "matrix": matrix,
            "manifest_sha256": sha256(manifest_path)}


def changed_from_git(base: str, head: str) -> list[str] | None:
    if not base:
        return None
    try:
        result = subprocess.run(["git", "diff", "--name-only", "-z", base, head, "--"], cwd=ROOT,
                                capture_output=True, check=True, timeout=30)
        return [p.decode("utf-8", errors="strict") for p in result.stdout.split(b"\0") if p]
    except (OSError, UnicodeError, subprocess.SubprocessError):
        return None


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", choices=["auto", "fast", "full"], default="auto")
    parser.add_argument("--changed-file", action="append")
    parser.add_argument("--changed-files", type=Path)
    parser.add_argument("--base", default="")
    parser.add_argument("--head", required=True)
    parser.add_argument("--event", default="")
    parser.add_argument("--branch", default="")
    parser.add_argument("--commit-message", default="")
    parser.add_argument("--diff-uncertain", action="store_true")
    parser.add_argument("--cache-mode", choices=["cold", "warm"], default="warm")
    parser.add_argument("--manifest", type=Path, default=MANIFEST)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--github-output", type=Path)
    args = parser.parse_args()
    try:
        paths = args.changed_file
        if args.changed_files:
            require(paths is None, "AMBIGUOUS_CHANGED_FILES_INPUT")
            paths = load_json(args.changed_files)
            require(isinstance(paths, list) and all(isinstance(p, str) for p in paths), "INVALID_CHANGED_FILES")
        if paths is None:
            paths = changed_from_git(args.base, args.head)
        result = select(args.mode, paths, source_sha=args.head, manifest_path=args.manifest,
                        diff_uncertain=args.diff_uncertain, commit_message=args.commit_message,
                        cache_mode=args.cache_mode)
        result.update(event=args.event, branch=args.branch)
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
        if args.github_output:
            with args.github_output.open("a", encoding="utf-8") as stream:
                for key, value in {"mode": result["mode"], "certifies_final": "false",
                                   "final_eligible": str(result["final_eligible"]).lower(),
                                   "matrix": json.dumps(result["matrix"], separators=(",", ":")),
                                   "groups": json.dumps(result["groups"], separators=(",", ":")),
                                   "manifest_sha256": result["manifest_sha256"],
                                   "cache_mode": result["cache_mode"]}.items():
                    stream.write(f"{key}={value}\n")
        print(json.dumps({"mode": result["mode"], "reason": result["reason"], "certifies_final": False}))
        return 0
    except (EvidenceError, OSError, ValueError) as error:
        print(json.dumps({"status": "FAIL", "error_code": str(error) if isinstance(error, EvidenceError) else type(error).__name__}))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
