"""Select complete functional regression, explicit local deep work, or docs checks."""
from __future__ import annotations

import argparse
import json
import os
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
        profile_groups,
        require,
        sha256,
    )
    from .executable_proof import (
        api_from_environment,
        executable_fingerprint,
        find_approval,
        human_document,
        validate_fingerprint,
    )
except ImportError:
    from common import (
        MANIFEST,
        ROOT,
        SHA,
        EvidenceError,
        load_json,
        load_manifest,
        profile_groups,
        require,
        sha256,
    )
    from executable_proof import (
        api_from_environment,
        executable_fingerprint,
        find_approval,
        human_document,
        validate_fingerprint,
    )


def impact(path: str) -> str:
    """Only known human documentation skips product tests; uncertainty runs functional."""
    require(isinstance(path, str) and path and "\\" not in path, "UNCERTAIN_CHANGED_PATH")
    parsed = PurePosixPath(path)
    require(not parsed.is_absolute() and ".." not in parsed.parts and ":" not in path, "UNCERTAIN_CHANGED_PATH")
    if path == "docs/development/permission-matrix.md":
        return "generated-permission-contract"
    if human_document(path):
        return "documentation"
    if path.startswith((".github/", "scripts/", "deploy/", "backend/src/", "backend/alembic/", "frontend/src/")):
        return "transverse-runtime-or-harness"
    if path.startswith("docs/"):
        return "uncertain-document"
    if any(token in path.lower() for token in ("docker", "migration", "lock", "package.json", "pyproject", "requirements", "openapi", "contract")):
        return "dependency-container-or-contract"
    if path.startswith(("backend/tests/", "frontend/tests/", "frontend/tests-e2e/")):
        return "test-impact-requires-functional"
    return "uncertain-source-path"


def select(mode: str, changed_files: list[str] | None, *, source_sha: str,
           manifest_path: Path = MANIFEST, diff_uncertain: bool = False,
           commit_message: str = "", cache_mode: str = "warm",
           verified_inheritance: dict[str, Any] | None = None,
           inheritance_reason: str = "unverified-executable-inheritance") -> dict[str, Any]:
    require(mode in {"auto", "functional", "deep", "fast", "full"}, "INVALID_SELECTION_MODE")
    require(bool(SHA.fullmatch(source_sha)), "INVALID_SOURCE_SHA")
    require(cache_mode in {"cold", "warm"}, "INVALID_CACHE_MODE")
    manifest = load_manifest(manifest_path)
    reasons: list[str] = []
    paths: list[dict[str, str]] = []
    # Legacy command aliases are explicit and never turn ordinary changes into
    # local volumetry. workflow_dispatch exposes only the functional profile.
    requested = {"fast": "functional", "full": "deep"}.get(mode, mode)
    if requested in {"functional", "deep"}:
        reasons.append("explicit-" + requested)
    if "[ci functional]" in commit_message.lower():
        reasons.append("explicit-functional")
    if diff_uncertain or changed_files is None:
        reasons.append("uncertain-diff")
    for path in changed_files or []:
        try:
            rule = impact(path)
        except EvidenceError:
            rule = "uncertain-source-path"
        paths.append({"path": path, "rule": rule})
        if rule != "documentation":
            reasons.append(rule)
    if not reasons and changed_files:
        if verified_inheritance is None:
            reasons.append(inheritance_reason)
        else:
            require(verified_inheritance.get("schema_version") == 1
                    and verified_inheritance.get("kind") == "GITHUB_FUNCTIONAL_INHERITANCE"
                    and verified_inheritance.get("source_sha") == source_sha
                    and isinstance(verified_inheritance.get("origin"), dict), "INVALID_VERIFIED_INHERITANCE")
            validate_fingerprint(verified_inheritance.get("executable_fingerprint"))
    selected_mode = "deep" if requested == "deep" else "functional" if reasons or not changed_files else "docs"
    profile = "deep" if selected_mode == "deep" else "functional"
    groups = profile_groups(manifest, profile) if selected_mode != "docs" else []
    priority = ["corrections-functional", "catalog-reports-functional", "connections-functional", "delivery",
                "compose-functional", "catalog-reports", "corrections-recovery", "corrections-acquisition", "corrections-dispatch",
                "corrections-browser", "async-volume-1024", "async-volume-100", "async-volume-500"]
    suites = [g for g in manifest["groups"] if g["id"] in groups and g["id"] not in {"backend", "frontend"}]
    suites.sort(key=lambda g: priority.index(g["id"]) if g["id"] in priority else len(priority))
    browser_groups = {"compose-functional", "compose-critical", "identity-sso", "connections-functional", "connections",
                      "delivery", "catalog-reports-functional", "catalog-reports", "corrections-browser", "async-volume-100"}
    matrix = {"include": [{"group": g["id"], "job_key": g["job_key"], "timeout_minutes": g["timeout_minutes"],
                           "python": g["id"].startswith("async-volume-"), "browser": g["id"] in browser_groups}
                          for g in suites]}
    return {"schema_version": 1, "mode": selected_mode, "profile": profile, "requested_mode": mode,
            "source_sha": source_sha, "certifies_final": False,
            "scope": "DOCUMENTATION_ONLY" if selected_mode == "docs" else profile.upper(),
            "functional_eligible": selected_mode == "functional", "deep_eligible": selected_mode == "deep",
            "final_eligible": selected_mode == "deep", "cache_mode": cache_mode,
            "reason": sorted(set(reasons)) or ["documentation-only"],
            "changed_files": paths, "groups": groups, "matrix": matrix,
            "manifest_sha256": sha256(manifest_path),
            "functional_inheritance": verified_inheritance if selected_mode == "docs" else None}


def changed_from_git(base: str, head: str) -> list[str] | None:
    if not base:
        return None
    try:
        result = subprocess.run(["git", "diff", "--name-only", "-z", base, head, "--"], cwd=ROOT,
                                capture_output=True, check=True, timeout=30)
        return [p.decode("utf-8", errors="strict") for p in result.stdout.split(b"\0") if p]
    except (OSError, UnicodeError, subprocess.SubprocessError):
        return None


def base_from_event(event: dict[str, Any]) -> str:
    """Synchronize uses the update delta; a new PR uses its initial base.

    The zero SHA on a new branch means there is no reliable prior revision and
    must not be treated as an empty documentation change.
    """
    require(isinstance(event, dict), "INVALID_EVENT_PAYLOAD")
    pull_request = event.get("pull_request", {})
    base = pull_request.get("base", {}) if isinstance(pull_request, dict) else {}
    candidates = [event.get("before"), base.get("sha") if isinstance(base, dict) else None]
    return next((value for value in candidates if isinstance(value, str)
                 and SHA.fullmatch(value) and value != "0" * 40), "")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profile", "--mode", dest="mode", choices=["auto", "functional", "deep", "fast", "full"], default="auto")
    parser.add_argument("--changed-file", action="append")
    parser.add_argument("--changed-files", type=Path)
    parser.add_argument("--base", default="")
    parser.add_argument("--head", required=True)
    parser.add_argument("--event", default="")
    parser.add_argument("--event-path", type=Path)
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
            base = base_from_event(load_json(args.event_path)) if args.event_path else args.base
            paths = changed_from_git(base, args.head)
        fingerprint = executable_fingerprint(args.head)
        proof, inheritance_reason = None, "unverified-executable-inheritance"
        try:
            docs_candidate = (args.mode == "auto" and paths and not args.diff_uncertain
                              and "[ci functional]" not in args.commit_message.lower()
                              and all(impact(path) == "documentation" for path in paths))
        except EvidenceError:
            docs_candidate = False
        if docs_candidate:
            try:
                repository = os.environ.get("GITHUB_REPOSITORY", "")
                api = api_from_environment(repository)
                proof, inheritance_reason = find_approval(api, repository=repository, branch=args.branch,
                    current_sha=args.head, fingerprint=fingerprint, manifest_path=args.manifest,
                    current_run_id=os.environ.get("GITHUB_RUN_ID", ""))
            except (EvidenceError, OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError):
                inheritance_reason = "executable-inheritance-unavailable"
        result = select(args.mode, paths, source_sha=args.head, manifest_path=args.manifest,
                        diff_uncertain=args.diff_uncertain, commit_message=args.commit_message,
                        cache_mode=args.cache_mode, verified_inheritance=proof, inheritance_reason=inheritance_reason)
        result.update(event=args.event, branch=args.branch, executable_fingerprint=fingerprint)
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
        if args.github_output:
            with args.github_output.open("a", encoding="utf-8") as stream:
                for key, value in {"mode": result["mode"], "profile": result["profile"], "certifies_final": "false",
                                   "final_eligible": str(result["final_eligible"]).lower(),
                                   "matrix": json.dumps(result["matrix"], separators=(",", ":")),
                                   "groups": json.dumps(result["groups"], separators=(",", ":")),
                                   "manifest_sha256": result["manifest_sha256"],
                                   "cache_mode": result["cache_mode"]}.items():
                    stream.write(f"{key}={value}\n")
        print(json.dumps({"mode": result["mode"], "reason": result["reason"], "certifies_final": False}))
        return 0
    except (EvidenceError, OSError, ValueError, subprocess.SubprocessError) as error:
        print(json.dumps({"status": "FAIL", "error_code": str(error) if isinstance(error, EvidenceError) else type(error).__name__}))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
