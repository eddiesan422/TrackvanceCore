"""Real Git histories and trusted-API fixtures for documentation inheritance."""
import copy
import hashlib
import io
import json
import subprocess
import sys
import urllib.error
import zipfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from scripts.ci import common, executable_proof, final_gate, select_suites
from scripts.ci.check_documentation import validate_documentation
from scripts.ci.common import EvidenceError, load_manifest, sha256
from scripts.ci.executable_proof import (
    executable_fingerprint,
    find_approval,
    revalidate_inheritance,
    verify_origin,
)

REPOSITORY = "eddiesan422/TrackvanceCore"
BRANCH = "feat/local-prototype"


def git(root, *args, input_bytes=None):
    return subprocess.run(["git", *args], cwd=root, input=input_bytes, capture_output=True,
                          check=True, timeout=20).stdout.decode().strip()


def commit(root, name):
    git(root, "add", ".")
    git(root, "commit", "-qm", name)
    return git(root, "rev-parse", "HEAD")


class FakeAPI:
    def __init__(self, case):
        self.case = case
        self.calls = []

    def get(self, path):
        self.calls.append(path)
        if path == "actions/workflows/ci.yml":
            return copy.deepcopy(self.case["workflow"])
        if path.startswith("actions/workflows/123/runs?"):
            return {"workflow_runs": copy.deepcopy(self.case["runs"])}
        if path in self.case.get("fresh_runs", {}):
            return copy.deepcopy(self.case["fresh_runs"][path])
        if path == "actions/runs/111":
            return copy.deepcopy(self.case["run"])
        if path == "actions/runs/111/attempts/1/jobs?per_page=100":
            return {"jobs": copy.deepcopy(self.case["jobs"]), "total_count": len(self.case["jobs"])}
        if path == "actions/runs/111/artifacts?per_page=100":
            return {"artifacts": copy.deepcopy(self.case["artifacts"]), "total_count": len(self.case["artifacts"])}
        raise EvidenceError("UNEXPECTED_TEST_API_PATH")

    def download(self, identity):
        self.calls.append(("download", identity))
        assert identity == 333
        return self.case["archive"]


def repack(case):
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name in ("gate-result.json", "selection.json", "jobs.json"):
            archive.writestr(name, json.dumps(case["documents"][name]))
    case["archive"] = stream.getvalue()
    case["artifact"]["digest"] = "sha256:" + hashlib.sha256(case["archive"]).hexdigest()
    case["artifact"]["size_in_bytes"] = len(case["archive"])


@pytest.fixture
def proof_case(tmp_path):
    root = tmp_path / "checkout"
    root.mkdir()
    git(root, "init", "-q")
    git(root, "config", "user.name", "CI proof fixture")
    git(root, "config", "user.email", "ci-proof@example.invalid")
    git(root, "config", "core.autocrlf", "false")
    (root / "README.md").write_text("# Initial human documentation\n", encoding="utf-8")
    (root / "runtime.py").write_text("print('real source')\n", encoding="utf-8")
    (root / "requirements.lock").write_text("dependency==1.0.0\n", encoding="utf-8")
    manifest_path = root / "scripts" / "ci" / "scenarios.json"
    manifest_path.parent.mkdir(parents=True)
    manifest_path.write_text(json.dumps(load_manifest()), encoding="utf-8")
    origin_sha = commit(root, "Executable source approved")
    fingerprint = executable_fingerprint(origin_sha, root=root)
    (root / "README.md").write_text("# Current human documentation\n", encoding="utf-8")
    current_sha = commit(root, "Documentation follows approved code")
    assert executable_fingerprint(current_sha, root=root) == fingerprint
    manifest = load_manifest(manifest_path)
    groups = manifest["profiles"]["functional"]["groups"]
    group_index = {g["id"]: g for g in manifest["groups"]}
    run = {"id": 111, "head_sha": origin_sha, "head_branch": BRANCH, "event": "push",
           "status": "completed", "conclusion": "success", "run_attempt": 1,
           "workflow_id": 123, "path": ".github/workflows/ci.yml",
           "repository": {"id": 789, "full_name": REPOSITORY},
           "head_repository": {"id": 789, "full_name": REPOSITORY}}
    job_names = {"select", "backend", "frontend", "images", "gate"} | {group_index[g]["job_key"] for g in groups}
    jobs = [{"name": n, "run_id": 111, "head_sha": origin_sha, "run_attempt": 1,
             "status": "completed", "conclusion": "success"} for n in job_names]
    artifact = {"id": 333, "name": f"ci-gate-{origin_sha}-1", "expired": False,
                "expires_at": "2099-01-01T00:00:00Z", "workflow_run": {
                    "id": 111, "head_sha": origin_sha, "head_branch": BRANCH,
                    "repository_id": 789, "head_repository_id": 789}}
    gate = {"schema_version": 1, "status": "PASS", "scope": "CI_FUNCTIONAL_APPROVED",
            "profile": "functional", "functional_approved": True, "deep_approved": False,
            "certifies_final": False, "source_sha": origin_sha, "run_id": "111", "run_attempt": "1",
            "executable_fingerprint": fingerprint, "manifest_sha256": sha256(manifest_path),
            "groups": groups, "module_coverage": manifest["profiles"]["functional"]["module_coverage"],
            "receipts": [{"scenario_id": s["id"], "group": g, "sha256": "a" * 64,
                          "attachments": {"source.json": "b" * 64}}
                         for g in groups for s in group_index[g]["scenarios"]]}
    gate["scenario_count"] = len(gate["receipts"])
    selection = {"schema_version": 1, "mode": "functional", "profile": "functional", "source_sha": origin_sha,
                 "manifest_sha256": sha256(manifest_path), "groups": groups, "executable_fingerprint": fingerprint}
    snapshot = {"source_sha": origin_sha, "run_id": "111", "run_attempt": "1",
                "needs": {n: {"result": "success"} for n in ("select", "backend", "frontend", "images", "suites")}}
    case = {"root": root, "manifest_path": manifest_path, "origin_sha": origin_sha, "current_sha": current_sha,
            "fingerprint": fingerprint, "run": run, "runs": [copy.deepcopy(run)], "jobs": jobs,
            "artifact": artifact, "artifacts": [artifact], "workflow": {"id": 123, "path": ".github/workflows/ci.yml"},
            "documents": {"gate-result.json": gate, "selection.json": selection, "jobs.json": snapshot}}
    repack(case)
    case["api"] = FakeAPI(case)
    return case


def discover(case):
    return find_approval(case["api"], repository=REPOSITORY, branch=BRANCH, current_sha=case["current_sha"],
                         fingerprint=executable_fingerprint(case["current_sha"], root=case["root"]),
                         manifest_path=case["manifest_path"], root=case["root"])


def verify(case):
    return verify_origin(case["api"], case["run"], repository=REPOSITORY, branch=BRANCH,
        current_sha=case["current_sha"], fingerprint=case["fingerprint"],
        manifest_path=case["manifest_path"], expected_workflow_id=123, root=case["root"])


def test_approved_executable_followed_by_readme_is_fast_with_exact_origin(proof_case):
    proof, reason = discover(proof_case)
    assert reason == "verified-functional-inheritance"
    result = select_suites.select("auto", ["README.md"], source_sha=proof_case["current_sha"], verified_inheritance=proof)
    assert result["mode"] == "docs" and result["groups"] == []
    assert proof["origin"]["run_id"] == 111 and proof["origin"]["source_sha"] == proof_case["origin_sha"]
    assert proof["origin"]["artifact_digest"] == proof_case["artifact"]["digest"]
    assert len(proof_case["api"].calls) == 6


@pytest.mark.parametrize("state", ["failure", "cancelled", "queued", "in_progress"])
def test_failed_cancelled_or_pending_code_then_readme_cannot_hide_behind_older_green(proof_case, state):
    root = proof_case["root"]
    (root / "runtime.py").write_text("raise RuntimeError('new unvalidated code')\n", encoding="utf-8")
    changed_sha = commit(root, "Changed executable source")
    (root / "README.md").write_text("# Documentation follows pending source\n", encoding="utf-8")
    proof_case["current_sha"] = commit(root, "README after failed source")
    pending = copy.deepcopy(proof_case["run"])
    pending.update(id=222, head_sha=changed_sha, conclusion=state if state in {"failure", "cancelled"} else None,
                   status="completed" if state in {"failure", "cancelled"} else state)
    proof_case["runs"].insert(0, pending)
    proof, reason = discover(proof_case)
    result = select_suites.select("auto", ["README.md"], source_sha=proof_case["current_sha"],
                                 verified_inheritance=proof, inheritance_reason=reason)
    assert result["mode"] == "functional" and result["functional_inheritance"] is None
    assert result["groups"] == load_manifest()["profiles"]["functional"]["groups"]


def newer_listed_success(case, state):
    """Two same-content ancestors: the latest list entry is stale, the older is green."""
    latest_sha = case["current_sha"]
    (case["root"] / "README.md").write_text("# README after the latest execution\n", encoding="utf-8")
    case["current_sha"] = commit(case["root"], "README follows the latest execution")
    latest = copy.deepcopy(case["run"])
    latest.update(id=444, head_sha=latest_sha)
    case["runs"] = [latest, copy.deepcopy(case["run"])]
    fresh = copy.deepcopy(latest)
    fresh.update(status="completed" if state in {"failure", "cancelled"} else state,
                 conclusion=state if state in {"failure", "cancelled"} else None)
    case["fresh_runs"] = {"actions/runs/444": fresh}


@pytest.mark.parametrize("state", ["failure", "cancelled", "queued", "in_progress"])
def test_fresh_unapproved_run_stops_discovery_before_older_green_and_selects_functional(proof_case, state):
    newer_listed_success(proof_case, state)
    proof, reason = discover(proof_case)
    assert proof is None and reason == "inherited-executable-run-unapproved"
    selected = select_suites.select("auto", ["README.md"], source_sha=proof_case["current_sha"],
        verified_inheritance=proof, inheritance_reason=reason)
    assert selected["mode"] == "functional" and selected["functional_inheritance"] is None
    assert selected["reason"] == [reason]
    assert selected["groups"] == load_manifest()["profiles"]["functional"]["groups"]
    assert proof_case["api"].calls == ["actions/workflows/ci.yml",
        "actions/workflows/123/runs?branch=feat%2Flocal-prototype&per_page=100", "actions/runs/444"]


@pytest.mark.parametrize("state", ["failure", "cancelled", "queued", "in_progress"])
def test_gate_revalidation_stops_at_fresh_unapproved_candidate_despite_cached_older_green(
        proof_case, monkeypatch, state):
    newer_listed_success(proof_case, state)
    proof = verify(proof_case)
    args = gate_arguments(proof_case, proof, monkeypatch)
    proof_case["api"].calls.clear()
    with pytest.raises(EvidenceError, match="INHERITED_EXECUTABLE_NO_LONGER_APPROVED"):
        final_gate.evaluate(**args)
    assert proof_case["api"].calls == ["actions/workflows/ci.yml",
        "actions/workflows/123/runs?branch=feat%2Flocal-prototype&per_page=100", "actions/runs/444"]


@pytest.mark.parametrize("path", ["requirements.lock", "backend/uv.lock", "frontend/pnpm-lock.yaml",
    "backend/tests/fixture.md", "scripts/tests/data/input.svg", "docs/development/permission-matrix.md",
    "docs/specification/model_contract_0.8.0.json", "AGENTS.md", ".github/actions/dependencies/action.yml",
    "deploy/docker/frontend.Dockerfile", "backend/migrations/README.md"])
def test_test_build_dependency_contract_and_policy_changes_invalidate_inherited_tree(proof_case, path):
    root = proof_case["root"]
    target = root / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("changed executable dependency\n", encoding="utf-8")
    commit(root, "Changed tracked dependency")
    (root / "README.md").write_text("# Only the latest delta is README\n", encoding="utf-8")
    proof_case["current_sha"] = commit(root, "README after dependency")
    proof, _ = discover(proof_case)
    assert proof is None
    assert executable_fingerprint(proof_case["current_sha"], root=root) != proof_case["fingerprint"]


def test_git_modes_and_document_named_symlinks_are_in_the_fingerprint(proof_case):
    root = proof_case["root"]
    git(root, "update-index", "--chmod=+x", "runtime.py")
    git(root, "commit", "-qm", "Executable mode changes")
    assert executable_fingerprint(git(root, "rev-parse", "HEAD"), root=root) != proof_case["fingerprint"]
    target_oid = git(root, "hash-object", "-w", "--stdin", input_bytes=b"runtime.py")
    git(root, "update-index", "--cacheinfo", "120000", target_oid, "README.md")
    git(root, "commit", "-qm", "Document path is a symlink")
    linked = executable_fingerprint(git(root, "rev-parse", "HEAD"), root=root)
    assert linked["tracked_entries"] == proof_case["fingerprint"]["tracked_entries"] + 1


@pytest.mark.parametrize("damage,code", [
    (lambda c: c["run"].update(conclusion="failure"), "EXECUTABLE_RUN_NOT_APPROVED"),
    (lambda c: c["run"].update(run_attempt=2), "UNEXPECTED_TEST_API_PATH"),
    (lambda c: c["run"].update(workflow_id=456), "APPROVAL_WORKFLOW_MISMATCH"),
    (lambda c: c["run"].update(head_branch="another-branch"), "APPROVAL_BRANCH_OR_EVENT_MISMATCH"),
    (lambda c: c["run"]["head_repository"].update(full_name="other/repo"), "APPROVAL_REPOSITORY_MISMATCH"),
    (lambda c: c["artifacts"].clear(), "MISSING_OR_DUPLICATE_APPROVAL_GATE_ARTIFACT"),
    (lambda c: c["artifacts"].append(copy.deepcopy(c["artifact"])), "MISSING_OR_DUPLICATE_APPROVAL_GATE_ARTIFACT"),
    (lambda c: c["artifact"].update(expired=True), "APPROVAL_ARTIFACT_EXPIRED"),
    (lambda c: c["artifact"].update(expires_at="2000-01-01T00:00:00Z"), "APPROVAL_ARTIFACT_EXPIRED"),
    (lambda c: c["artifact"].update(digest="sha256:" + "f" * 64), "APPROVAL_ARTIFACT_DIGEST_MISMATCH"),
    (lambda c: c["artifact"]["workflow_run"].update(id=444), "APPROVAL_ARTIFACT_PROVENANCE_MISMATCH"),
    (lambda c: c["artifact"]["workflow_run"].update(repository_id=444), "APPROVAL_ARTIFACT_PROVENANCE_MISMATCH"),
    (lambda c: c["artifact"]["workflow_run"].update(head_branch="other"), "APPROVAL_ARTIFACT_PROVENANCE_MISMATCH"),
    (lambda c: c["jobs"][0].update(conclusion="cancelled"), "APPROVAL_JOB_FAILED_MISSING_OR_DIFFERENT_ATTEMPT"),
    (lambda c: c["jobs"][0].update(run_attempt=2), "APPROVAL_JOB_FAILED_MISSING_OR_DIFFERENT_ATTEMPT"),
    (lambda c: c["jobs"].pop(), "APPROVAL_JOB_FAILED_MISSING_OR_DIFFERENT_ATTEMPT"),
])
def test_origin_requires_genuine_same_workflow_branch_attempt_artifact_and_job_closure(proof_case, damage, code):
    damage(proof_case)
    with pytest.raises(EvidenceError, match=code):
        verify(proof_case)


@pytest.mark.parametrize("document,key,value,code", [
    ("gate-result.json", "scope", "DOCUMENTATION_APPROVED", "ANCESTOR_DID_NOT_APPROVE_FUNCTIONAL"),
    ("gate-result.json", "functional_approved", False, "ANCESTOR_DID_NOT_APPROVE_FUNCTIONAL"),
    ("gate-result.json", "deep_approved", True, "ANCESTOR_DID_NOT_APPROVE_FUNCTIONAL"),
    ("gate-result.json", "run_attempt", "2", "APPROVAL_GATE_IDENTITY_OR_FINGERPRINT_MISMATCH"),
    ("gate-result.json", "executable_fingerprint", None, "APPROVAL_GATE_IDENTITY_OR_FINGERPRINT_MISMATCH"),
    ("gate-result.json", "receipts", [], "APPROVAL_RECEIPT_CLOSURE_MISMATCH"),
    ("gate-result.json", "scenario_count", 29, "APPROVAL_FUNCTIONAL_COVERAGE_MISMATCH"),
    ("gate-result.json", "module_coverage", {}, "APPROVAL_FUNCTIONAL_COVERAGE_MISMATCH"),
    ("selection.json", "mode", "docs", "APPROVAL_SELECTION_MISMATCH"),
    ("jobs.json", "run_attempt", "2", "APPROVAL_NEEDS_SNAPSHOT_MISMATCH"),
])
def test_even_a_digest_matching_archive_must_prove_functional_execution_and_new_fingerprint(proof_case, document, key, value, code):
    proof_case["documents"][document][key] = value
    repack(proof_case)
    with pytest.raises(EvidenceError, match=code):
        verify(proof_case)


def test_unrelated_git_commit_cannot_approve_the_current_branch(proof_case):
    root = proof_case["root"]
    git(root, "checkout", "--orphan", "unrelated")
    foreign_sha = commit(root, "Unrelated tree with identical content")
    proof_case["run"]["head_sha"] = foreign_sha
    with pytest.raises(EvidenceError, match="APPROVAL_NOT_A_PRIOR_ANCESTOR"):
        verify(proof_case)


def gate_arguments(case, proof, monkeypatch):
    root = case["root"]
    monkeypatch.setattr(common, "ROOT", root)
    selected = select_suites.select("auto", ["README.md"], source_sha=case["current_sha"],
        manifest_path=case["manifest_path"], verified_inheritance=proof)
    selected["executable_fingerprint"] = executable_fingerprint(case["current_sha"], root=root)
    directory = root / ".codex-local" / "ci"
    directory.mkdir(parents=True)
    selection = directory / "selection.json"
    selection.write_text(json.dumps(selected), encoding="utf-8")
    jobs = directory / "jobs.json"
    needs = {n: {"result": "skipped"} for n in ("backend", "frontend", "images", "suites")}
    needs.update(select={"result": "success"}, documentation={"result": "success"})
    jobs.write_text(json.dumps({"source_sha": case["current_sha"], "run_id": "222", "run_attempt": "1", "needs": needs}), encoding="utf-8")
    docs = directory / "documentation.json"
    docs.write_text(json.dumps({"schema_version": 1, "kind": "DOCUMENTATION_EVIDENCE", "status": "PASS",
        "source_sha": case["current_sha"], "selection_sha256": sha256(selection),
        "ci": {"run_id": "222", "run_attempt": "1", "job_id": "documentation"},
        "documents": validate_documentation(root, ["README.md"])}), encoding="utf-8")
    return {"manifest_path": case["manifest_path"], "selection_path": selection, "jobs_path": jobs,
        "evidence_dir": directory / "absent-product-evidence", "image_manifest_path": directory / "absent-images.json",
        "source_sha": case["current_sha"], "run_id": "222", "run_attempt": "1", "documentation_path": docs,
        "repository": REPOSITORY, "branch": BRANCH, "approval_api": case["api"], "repository_root": root}


def test_final_gate_repeats_live_api_and_hashes_without_reexecuting_product(proof_case, monkeypatch):
    proof, _ = discover(proof_case)
    args = gate_arguments(proof_case, proof, monkeypatch)
    calls_before = len(proof_case["api"].calls)
    result = final_gate.evaluate(**args)
    assert len(proof_case["api"].calls) > calls_before
    assert result["scope"] == "DOCUMENTATION_APPROVED" and result["status"] == "PASS"
    assert result["inherited_executable_approved"] and not result["current_functional_executed"]
    assert result["functional_inheritance"] == proof
    assert result["scenario_count"] == 0 and not result["functional_approved"] and not result["deep_approved"]


@pytest.mark.parametrize("state", ["failure", "cancelled", "in_progress"])
def test_final_gate_cannot_use_selection_cached_before_origin_failure_or_cancel(proof_case, monkeypatch, state):
    proof, _ = discover(proof_case)
    args = gate_arguments(proof_case, proof, monkeypatch)
    proof_case["run"].update(conclusion=state, status="completed" if state != "in_progress" else state)
    proof_case["runs"] = [copy.deepcopy(proof_case["run"])]
    with pytest.raises(EvidenceError, match="INHERITED_EXECUTABLE_NO_LONGER_APPROVED"):
        final_gate.evaluate(**args)


def test_final_gate_rejects_manually_forged_missing_and_tampered_inheritance(proof_case, monkeypatch):
    proof, _ = discover(proof_case)
    args = gate_arguments(proof_case, proof, monkeypatch)
    selected = json.loads(args["selection_path"].read_text())
    selected["functional_inheritance"] = None
    args["selection_path"].write_text(json.dumps(selected), encoding="utf-8")
    with pytest.raises(EvidenceError, match="MISSING_OR_DIFFERENT_EXECUTABLE_INHERITANCE"):
        final_gate.evaluate(**args)
    selected["functional_inheritance"] = copy.deepcopy(proof)
    selected["functional_inheritance"]["origin"]["artifact_id"] = 999
    args["selection_path"].write_text(json.dumps(selected), encoding="utf-8")
    with pytest.raises(EvidenceError, match="ALTERED_OR_STALE_EXECUTABLE_INHERITANCE"):
        final_gate.evaluate(**args)


def test_final_gate_rejects_stale_artifact_after_successful_selection(proof_case):
    proof, _ = discover(proof_case)
    proof_case["artifact"]["digest"] = "sha256:" + "0" * 64
    with pytest.raises(EvidenceError, match="INHERITED_EXECUTABLE_NO_LONGER_APPROVED"):
        revalidate_inheritance(proof, repository=REPOSITORY, branch=BRANCH, current_sha=proof_case["current_sha"],
            fingerprint=proof_case["fingerprint"], manifest_path=proof_case["manifest_path"],
            api=proof_case["api"], root=proof_case["root"])


@pytest.mark.parametrize("state", ["failure", "cancelled"])
def test_final_gate_rejects_a_forged_document_skip_after_changed_code_failed(proof_case, monkeypatch, state):
    proof, _ = discover(proof_case)
    root = proof_case["root"]
    (root / "runtime.py").write_text("raise RuntimeError('new source failed')\n", encoding="utf-8")
    failed_sha = commit(root, "New executable source failed")
    (root / "README.md").write_text("# README follows failed new code\n", encoding="utf-8")
    proof_case["current_sha"] = commit(root, "Documentation cannot mask the failure")
    failed_run = copy.deepcopy(proof_case["run"])
    failed_run.update(id=222, head_sha=failed_sha, conclusion=state)
    proof_case["runs"].insert(0, failed_run)
    # A manually edited selection is insufficient even with the current tree
    # hash and an older authentic green artifact ID.
    proof["source_sha"] = proof_case["current_sha"]
    proof["executable_fingerprint"] = executable_fingerprint(proof_case["current_sha"], root=root)
    args = gate_arguments(proof_case, proof, monkeypatch)
    with pytest.raises(EvidenceError, match="INHERITED_EXECUTABLE_NO_LONGER_APPROVED"):
        final_gate.evaluate(**args)


def test_final_gate_rejects_current_selection_fingerprint_tampering(proof_case, monkeypatch):
    proof, _ = discover(proof_case)
    args = gate_arguments(proof_case, proof, monkeypatch)
    selected = json.loads(args["selection_path"].read_text())
    selected["executable_fingerprint"]["sha256"] = "0" * 64
    args["selection_path"].write_text(json.dumps(selected), encoding="utf-8")
    with pytest.raises(EvidenceError, match="SELECTION_EXECUTABLE_FINGERPRINT_MISMATCH"):
        final_gate.evaluate(**args)


def test_api_unavailable_or_untrusted_local_context_selects_functional(proof_case, monkeypatch):
    monkeypatch.setattr(select_suites, "executable_fingerprint", lambda _: proof_case["fingerprint"])
    monkeypatch.setattr(select_suites, "api_from_environment", lambda _: (_ for _ in ()).throw(OSError("API unavailable")))
    output = proof_case["root"] / "selection-output.json"
    monkeypatch.setattr(sys, "argv", ["select_suites", "--head", proof_case["current_sha"], "--changed-file", "README.md",
                                     "--branch", BRANCH, "--output", str(output)])
    assert select_suites.main() == 0
    selected = json.loads(output.read_text())
    assert selected["mode"] == "functional" and selected["reason"] == ["executable-inheritance-unavailable"]


def test_artifact_signed_storage_redirect_never_receives_the_api_token(monkeypatch):
    api = executable_proof.GitHubAPI(REPOSITORY, "private-test-token")
    requests = []
    class Response:
        def __enter__(self):
            return self
        def __exit__(self, *args):
            return False
        def read(self, maximum):
            return b"artifact bytes"
    class Opener:
        def open(self, request, timeout):
            requests.append(request)
            if len(requests) == 1:
                raise urllib.error.HTTPError(request.full_url, 302, "redirect", {"Location": "https://storage.example.invalid/signed"}, None)
            return Response()
    monkeypatch.setattr(api, "_opener", Opener())
    assert api.download(333) == b"artifact bytes"
    assert requests[0].get_header("Authorization") == "Bearer private-test-token"
    assert requests[1].get_header("Authorization") is None


def test_api_lookup_has_a_finite_total_budget_and_does_not_send_tokens_after_expiry(monkeypatch):
    api = executable_proof.GitHubAPI(REPOSITORY, "private-test-token")
    monkeypatch.setattr(executable_proof.time, "monotonic", lambda: api._deadline + 1)
    with pytest.raises(EvidenceError, match="APPROVAL_LOOKUP_BUDGET_EXPIRED"):
        api.get("actions/workflows/ci.yml")


def test_workflow_scopes_actions_read_token_to_selector_and_gate_and_fetches_ancestry():
    workflow = (common.ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8")
    assert "  actions: read" in workflow
    assert workflow.count("GH_TOKEN: ${{ github.token }}") == 2
    gate = workflow.split("  gate:\n", 1)[1]
    assert "fetch-depth: 0" in gate
    assert "--profile functional" in workflow
    assert "options: [functional]" in workflow
