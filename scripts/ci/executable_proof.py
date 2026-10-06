"""Verify inherited executable content against a genuine successful Actions gate."""
from __future__ import annotations

import hashlib
import io
import json
import os
import re
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

try:
    from .common import (
        DIGEST,
        ROOT,
        SHA,
        EvidenceError,
        _pairs,
        load_manifest,
        profile_groups,
        require,
        sha256,
    )
except ImportError:
    from common import (
        DIGEST,
        ROOT,
        SHA,
        EvidenceError,
        _pairs,
        load_manifest,
        profile_groups,
        require,
        sha256,
    )

WORKFLOW = ".github/workflows/ci.yml"
FINGERPRINT_ALGORITHM = "GIT_TRACKED_EXECUTABLE_TREE_V1"
# Code keeps these human documents and pointers. Everything else, including
# Markdown test/build assets, permissions, AGENTS and symlinks, remains hashed.
HUMAN_DOCUMENTS = frozenset({
    "README.md", "CHANGELOG.md", "backend/API_CONTRACT.md", "docs/README.md",
    "docs/specification/README.md", "LICENSE-or-proprietary-notice.txt",
})
MAX_GATE_BYTES = 2 * 1024**2
MAX_API_BYTES = 8 * 1024**2
MAX_CANDIDATES = 10
MAX_LOOKUP_SECONDS = 30


def human_document(path: str) -> bool:
    return path in HUMAN_DOCUMENTS


def executable_fingerprint(source_sha: str, *, root: Path = ROOT) -> dict[str, Any]:
    """Hash committed paths/modes/object IDs, independently of checkout newlines."""
    require(isinstance(source_sha, str) and SHA.fullmatch(source_sha), "INVALID_FINGERPRINT_SHA")
    result = subprocess.run(["git", "ls-tree", "-r", "-z", "--full-tree", source_sha, "--"],
                            cwd=root, capture_output=True, check=True, timeout=30)
    require(0 < len(result.stdout) <= 16 * 1024**2, "INVALID_EXECUTABLE_TREE")
    entries = []
    for record in result.stdout.split(b"\0"):
        if not record:
            continue
        descriptor, encoded_path = record.split(b"\t", 1)
        mode, kind, oid = descriptor.decode("ascii").split(" ")
        path = encoded_path.decode("utf-8", errors="strict")
        require(mode in {"100644", "100755", "120000", "160000"} and kind in {"blob", "commit"}
                and SHA.fullmatch(oid), "INVALID_EXECUTABLE_TREE_ENTRY")
        # A symlink with a document's name can change what build tools read.
        if human_document(path) and mode == "100644" and kind == "blob":
            continue
        entries.append({"path": path, "mode": mode, "type": kind, "oid": oid})
    require(entries, "EMPTY_EXECUTABLE_TREE")
    payload = json.dumps(sorted(entries, key=lambda e: e["path"]), sort_keys=True,
                         separators=(",", ":"), ensure_ascii=True).encode("ascii")
    return {"algorithm": FINGERPRINT_ALGORITHM, "sha256": hashlib.sha256(payload).hexdigest(),
            "tracked_entries": len(entries)}


def validate_fingerprint(value: Any) -> None:
    require(isinstance(value, dict) and set(value) == {"algorithm", "sha256", "tracked_entries"}
            and value.get("algorithm") == FINGERPRINT_ALGORITHM
            and isinstance(value.get("sha256"), str) and DIGEST.fullmatch(value["sha256"])
            and type(value.get("tracked_entries")) is int and value["tracked_entries"] > 0,
            "INVALID_EXECUTABLE_FINGERPRINT")


def strict_json(raw: bytes) -> Any:
    return json.loads(raw.decode("utf-8-sig"), object_pairs_hook=_pairs,
                      parse_constant=lambda _: (_ for _ in ()).throw(EvidenceError("NONFINITE_JSON")))


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class GitHubAPI:
    """Read-only API; never forward authorization to signed artifact storage."""
    def __init__(self, repository: str, token: str):
        require(isinstance(repository, str) and re.fullmatch(r"[\w.-]+/[\w.-]+", repository), "INVALID_PROOF_REPOSITORY")
        require(isinstance(token, str) and bool(token), "MISSING_ACTIONS_READ_TOKEN")
        self.repository = repository
        self._token = token
        self._opener = urllib.request.build_opener(NoRedirect)
        self._deadline = time.monotonic() + MAX_LOOKUP_SECONDS

    def _request(self, url: str, *, authenticated: bool, maximum: int) -> bytes:
        headers = {"User-Agent": "trackvance-ci-inheritance", "Accept": "application/vnd.github+json"}
        if authenticated:
            require(url.startswith("https://api.github.com/repos/" + self.repository + "/"), "UNTRUSTED_API_URL")
            headers.update(Authorization="Bearer " + self._token, **{"X-GitHub-Api-Version": "2022-11-28"})
        remaining = self._deadline - time.monotonic()
        require(remaining > 0, "APPROVAL_LOOKUP_BUDGET_EXPIRED")
        with self._opener.open(urllib.request.Request(url, headers=headers), timeout=min(8, remaining)) as response:
            raw = response.read(maximum + 1)
        require(len(raw) <= maximum, "OVERSIZED_APPROVAL_RESPONSE")
        return raw

    def get(self, suffix: str) -> dict[str, Any]:
        require(suffix.startswith("actions/") and ".." not in suffix and "#" not in suffix, "UNTRUSTED_API_PATH")
        raw = self._request("https://api.github.com/repos/" + self.repository + "/" + suffix,
                            authenticated=True, maximum=MAX_API_BYTES)
        value = strict_json(raw)
        require(isinstance(value, dict), "INVALID_APPROVAL_API_RESPONSE")
        return value

    def download(self, artifact_id: int) -> bytes:
        require(type(artifact_id) is int and artifact_id > 0, "INVALID_APPROVAL_ARTIFACT_ID")
        endpoint = f"https://api.github.com/repos/{self.repository}/actions/artifacts/{artifact_id}/zip"
        try:
            # The API returns a redirect, intercepted before urllib can copy headers.
            return self._request(endpoint, authenticated=True, maximum=MAX_GATE_BYTES)
        except urllib.error.HTTPError as error:
            require(error.code in {301, 302, 303, 307}, "APPROVAL_DOWNLOAD_UNAVAILABLE")
            location = error.headers.get("Location", "")
        parsed = urllib.parse.urlsplit(location)
        require(parsed.scheme == "https" and bool(parsed.hostname) and not parsed.username and not parsed.password,
                "UNSAFE_APPROVAL_DOWNLOAD_REDIRECT")
        return self._request(location, authenticated=False, maximum=MAX_GATE_BYTES)


def api_from_environment(repository: str) -> GitHubAPI:
    require(os.environ.get("GITHUB_ACTIONS") == "true"
            and os.environ.get("GITHUB_REPOSITORY") == repository
            and os.environ.get("GITHUB_API_URL", "https://api.github.com") == "https://api.github.com",
            "UNTRUSTED_APPROVAL_CONTEXT")
    return GitHubAPI(repository, os.environ.get("GH_TOKEN", ""))


def ancestor(source_sha: str, current_sha: str, *, root: Path = ROOT) -> bool:
    require(SHA.fullmatch(source_sha) and SHA.fullmatch(current_sha), "INVALID_APPROVAL_SHA")
    return subprocess.run(["git", "merge-base", "--is-ancestor", source_sha, current_sha],
                          cwd=root, capture_output=True, check=False, timeout=30).returncode == 0


def validate_run(run: dict, *, repository: str, branch: str, workflow_id: int) -> None:
    require(run.get("repository", {}).get("full_name") == repository
            and run.get("head_repository", {}).get("full_name") == repository
            and type(run.get("repository", {}).get("id")) is int
            and run["repository"]["id"] == run.get("head_repository", {}).get("id"), "APPROVAL_REPOSITORY_MISMATCH")
    require(run.get("workflow_id") == workflow_id and run.get("path", "").split("@")[0] == WORKFLOW,
            "APPROVAL_WORKFLOW_MISMATCH")
    require(run.get("head_branch") == branch and run.get("event") in {"push", "pull_request", "workflow_dispatch"},
            "APPROVAL_BRANCH_OR_EVENT_MISMATCH")
    require(type(run.get("id")) is int and run["id"] > 0
            and type(run.get("run_attempt")) is int and run["run_attempt"] > 0
            and isinstance(run.get("head_sha"), str) and SHA.fullmatch(run["head_sha"]), "INVALID_APPROVAL_RUN_IDENTITY")
    require(run.get("status") == "completed" and run.get("conclusion") == "success", "EXECUTABLE_RUN_NOT_APPROVED")


def workflow_id(api: GitHubAPI) -> int:
    workflow = api.get("actions/workflows/ci.yml")
    require(workflow.get("path") == WORKFLOW and type(workflow.get("id")) is int and workflow["id"] > 0,
            "APPROVAL_WORKFLOW_MISMATCH")
    return workflow["id"]


def verify_origin(api: GitHubAPI, run: dict, *, repository: str, branch: str, current_sha: str,
                  fingerprint: dict, manifest_path: Path, expected_workflow_id: int,
                  root: Path = ROOT) -> dict[str, Any]:
    validate_run(run, repository=repository, branch=branch, workflow_id=expected_workflow_id)
    sha, identity, attempt = run["head_sha"], run["id"], run["run_attempt"]
    require(sha != current_sha and ancestor(sha, current_sha, root=root), "APPROVAL_NOT_A_PRIOR_ANCESTOR")
    require(executable_fingerprint(sha, root=root) == fingerprint, "INHERITED_EXECUTABLE_CONTENT_CHANGED")
    manifest = load_manifest(manifest_path)
    groups = profile_groups(manifest, "functional")
    indexed = {g["id"]: g for g in manifest["groups"]}
    expected_scenarios = {s["id"]: g for g in groups for s in indexed[g]["scenarios"]}
    required_jobs = {"select", "backend", "frontend", "images", "gate"} | {indexed[g]["job_key"] for g in groups}
    jobs_response = api.get(f"actions/runs/{identity}/attempts/{attempt}/jobs?per_page=100")
    jobs = jobs_response.get("jobs")
    require(isinstance(jobs, list) and jobs_response.get("total_count") == len(jobs) <= 100, "INCOMPLETE_APPROVAL_JOBS")
    for name in required_jobs:
        matching = [j for j in jobs if j.get("name") == name]
        require(len(matching) == 1 and matching[0].get("run_id") == identity and matching[0].get("head_sha") == sha
                and matching[0].get("status") == "completed" and matching[0].get("conclusion") == "success"
                and matching[0].get("run_attempt", attempt) == attempt, "APPROVAL_JOB_FAILED_MISSING_OR_DIFFERENT_ATTEMPT")
    listing = api.get(f"actions/runs/{identity}/artifacts?per_page=100")
    artifacts = listing.get("artifacts")
    require(isinstance(artifacts, list) and listing.get("total_count") == len(artifacts) <= 100, "INCOMPLETE_APPROVAL_ARTIFACTS")
    matching = [a for a in artifacts if a.get("name") == f"ci-gate-{sha}-{attempt}"]
    require(len(matching) == 1, "MISSING_OR_DUPLICATE_APPROVAL_GATE_ARTIFACT")
    artifact = matching[0]
    provenance = artifact.get("workflow_run", {})
    require(provenance.get("id") == identity and provenance.get("head_sha") == sha
            and provenance.get("head_branch") == branch and provenance.get("repository_id") == run["repository"]["id"]
            and provenance.get("head_repository_id") == run["repository"]["id"], "APPROVAL_ARTIFACT_PROVENANCE_MISMATCH")
    try:
        expires = datetime.fromisoformat(artifact["expires_at"])
        require(artifact.get("expired") is False and expires.tzinfo is not None and expires > datetime.now(UTC),
                "APPROVAL_ARTIFACT_EXPIRED")
    except (KeyError, TypeError, ValueError) as error:
        if isinstance(error, EvidenceError):
            raise
        raise EvidenceError("APPROVAL_ARTIFACT_EXPIRY_MISSING") from error
    require(type(artifact.get("id")) is int and artifact["id"] > 0
            and type(artifact.get("size_in_bytes")) is int and 0 < artifact["size_in_bytes"] <= MAX_GATE_BYTES,
            "INVALID_APPROVAL_ARTIFACT_SIZE")
    raw = api.download(artifact["id"])
    digest = "sha256:" + hashlib.sha256(raw).hexdigest()
    require(artifact.get("digest") == digest and len(raw) == artifact["size_in_bytes"], "APPROVAL_ARTIFACT_DIGEST_MISMATCH")
    with zipfile.ZipFile(io.BytesIO(raw)) as archive:
        members = archive.infolist()
        require(len(members) == 3 and {m.filename for m in members} == {"gate-result.json", "selection.json", "jobs.json"}
                and sum(m.file_size for m in members) <= 8 * 1024**2, "INVALID_APPROVAL_GATE_ARCHIVE")
        gate_bytes = archive.read("gate-result.json")
        gate, selection, snapshot = (strict_json(archive.read(n)) for n in ("gate-result.json", "selection.json", "jobs.json"))
    require(all(isinstance(v, dict) for v in (gate, selection, snapshot)), "INVALID_APPROVAL_GATE_CONTENT")
    require(gate.get("schema_version") == 1 and gate.get("status") == "PASS" and gate.get("scope") == "CI_FUNCTIONAL_APPROVED"
            and gate.get("profile") == "functional" and gate.get("functional_approved") is True
            and gate.get("deep_approved") is False and gate.get("certifies_final") is False,
            "ANCESTOR_DID_NOT_APPROVE_FUNCTIONAL")
    require(gate.get("source_sha") == sha and str(gate.get("run_id")) == str(identity)
            and str(gate.get("run_attempt")) == str(attempt) and gate.get("manifest_sha256") == sha256(manifest_path)
            and gate.get("executable_fingerprint") == fingerprint, "APPROVAL_GATE_IDENTITY_OR_FINGERPRINT_MISMATCH")
    require(gate.get("groups") == groups and gate.get("module_coverage") == manifest["profiles"]["functional"]["module_coverage"]
            and gate.get("scenario_count") == len(expected_scenarios), "APPROVAL_FUNCTIONAL_COVERAGE_MISMATCH")
    receipts = gate.get("receipts")
    require(isinstance(receipts, list) and len(receipts) == len(expected_scenarios)
            and {r.get("scenario_id") for r in receipts if isinstance(r, dict)} == set(expected_scenarios),
            "APPROVAL_RECEIPT_CLOSURE_MISMATCH")
    for receipt in receipts:
        require(receipt.get("group") == expected_scenarios[receipt["scenario_id"]]
                and isinstance(receipt.get("sha256"), str) and DIGEST.fullmatch(receipt["sha256"])
                and isinstance(receipt.get("attachments"), dict) and bool(receipt["attachments"])
                and all(isinstance(h, str) and DIGEST.fullmatch(h) for h in receipt["attachments"].values()),
                "INVALID_APPROVAL_RECEIPT_DESCRIPTOR")
    require(selection.get("schema_version") == 1 and selection.get("mode") == selection.get("profile") == "functional" and selection.get("source_sha") == sha
            and selection.get("groups") == groups and selection.get("manifest_sha256") == sha256(manifest_path)
            and selection.get("executable_fingerprint") == fingerprint, "APPROVAL_SELECTION_MISMATCH")
    require(snapshot.get("source_sha") == sha and str(snapshot.get("run_id")) == str(identity)
            and str(snapshot.get("run_attempt")) == str(attempt) and isinstance(snapshot.get("needs"), dict)
            and all(snapshot["needs"].get(name, {}).get("result") == "success"
                    for name in ("select", "backend", "frontend", "images", "suites")), "APPROVAL_NEEDS_SNAPSHOT_MISMATCH")
    return {"schema_version": 1, "kind": "GITHUB_FUNCTIONAL_INHERITANCE", "repository": repository,
            "branch": branch, "source_sha": current_sha, "executable_fingerprint": fingerprint,
            "origin": {"source_sha": sha, "run_id": identity, "run_attempt": attempt,
                       "artifact_id": artifact["id"], "artifact_digest": digest,
                       "gate_sha256": hashlib.sha256(gate_bytes).hexdigest()}}


def find_approval(api: GitHubAPI, *, repository: str, branch: str, current_sha: str,
                  fingerprint: dict, manifest_path: Path, current_run_id: str = "",
                  root: Path = ROOT) -> tuple[dict | None, str]:
    """A finite lookup; all uncertainty selects fresh functional execution."""
    deadline = time.monotonic() + MAX_LOOKUP_SECONDS
    identity = workflow_id(api)
    listing = api.get(f"actions/workflows/{identity}/runs?branch={urllib.parse.quote(branch, safe='')}&per_page=100")
    candidates = listing.get("workflow_runs")
    require(isinstance(candidates, list), "INVALID_APPROVAL_RUN_LIST")
    inspected = 0
    for candidate in candidates:
        if time.monotonic() >= deadline:
            return None, "executable-inheritance-lookup-budget-exhausted"
        if not isinstance(candidate, dict) or str(candidate.get("id")) == current_run_id:
            continue
        sha = candidate.get("head_sha")
        if not isinstance(sha, str) or not SHA.fullmatch(sha) or sha == current_sha or not ancestor(sha, current_sha, root=root):
            continue
        if executable_fingerprint(sha, root=root) != fingerprint:
            continue
        # Do not use an older green run to hide a later failure or cancellation
        # of the same inherited executable content.
        if candidate.get("status") != "completed" or candidate.get("conclusion") != "success":
            return None, "inherited-executable-run-unapproved"
        inspected += 1
        if inspected > MAX_CANDIDATES:
            break
        try:
            fresh = api.get(f"actions/runs/{candidate['id']}")
            return verify_origin(api, fresh, repository=repository, branch=branch, current_sha=current_sha,
                                 fingerprint=fingerprint, manifest_path=manifest_path,
                                 expected_workflow_id=identity, root=root), "verified-functional-inheritance"
        except (EvidenceError, OSError, ValueError, KeyError, TypeError, zipfile.BadZipFile):
            continue
    return None, "no-verifiable-functional-ancestor"


def revalidate_inheritance(proof: Any, *, repository: str, branch: str, current_sha: str,
                           fingerprint: dict, manifest_path: Path, api: GitHubAPI | None = None,
                           root: Path = ROOT) -> dict:
    require(isinstance(proof, dict) and proof.get("kind") == "GITHUB_FUNCTIONAL_INHERITANCE"
            and proof.get("schema_version") == 1 and proof.get("repository") == repository
            and proof.get("branch") == branch and proof.get("source_sha") == current_sha
            and proof.get("executable_fingerprint") == fingerprint, "MISSING_OR_DIFFERENT_EXECUTABLE_INHERITANCE")
    origin = proof.get("origin")
    require(isinstance(origin, dict) and type(origin.get("run_id")) is int and origin["run_id"] > 0,
            "INVALID_INHERITED_RUN_IDENTITY")
    api = api or api_from_environment(repository)
    # Repeat discovery as well as the artifact verification. A failure or rerun
    # observed after selection cannot be hidden by the earlier cached origin.
    verified, _ = find_approval(api, repository=repository, branch=branch, current_sha=current_sha,
        fingerprint=fingerprint, manifest_path=manifest_path,
        current_run_id=os.environ.get("GITHUB_RUN_ID", ""), root=root)
    require(verified is not None, "INHERITED_EXECUTABLE_NO_LONGER_APPROVED")
    require(verified == proof, "ALTERED_OR_STALE_EXECUTABLE_INHERITANCE")
    return verified
