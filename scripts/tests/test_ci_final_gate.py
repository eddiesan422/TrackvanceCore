import json
import shutil
import sys
from datetime import UTC, datetime
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from scripts.ci.common import EvidenceError, sha256
from scripts.ci.evidence import write_receipt
from scripts.ci.final_gate import evaluate

SHA = "a" * 40


def save(path, value):
    path.write_text(json.dumps(value), encoding="utf-8")
    return path


@pytest.fixture
def gate_case(tmp_path):
    manifest = save(tmp_path / "manifest.json", {"schema_version": 1, "fast_groups": ["backend"],
        "groups": [{"id": "backend", "job_key": "backend", "required_sources": [],
                    "scenarios": [{"id": "backend-lint", "validator": "backend-check", "check": "lint"}]}]})
    selection = save(tmp_path / "selection.json", {"schema_version": 1, "source_sha": SHA, "manifest_sha256": sha256(manifest),
        "mode": "full", "groups": ["backend"]})
    jobs = save(tmp_path / "jobs.json", {"source_sha": SHA, "run_id": "123", "run_attempt": "1", "needs": {
        name: {"result": "success"} for name in ("backend", "frontend", "images", "suites")}})
    images = save(tmp_path / "images.json", {"schema_version": 1, "source_sha": SHA, "status": "PASS",
        "digest_kind": "DOCKER_CONFIGURATION_SHA256", "images": {
            role: {"image_id": "sha256:" + digit * 64, "archive": role + ".tar", "archive_sha256": digit * 64, "archive_bytes": 1}
            for role, digit in (("backend", "1"), ("web", "2"))}})
    evidence = tmp_path / "evidence"
    now = datetime.now(UTC).isoformat()
    receipt = write_receipt(evidence, group="backend", scenario_id="backend-lint", source_sha=SHA,
        ci={"run_id": "123", "run_attempt": "1", "job_id": "backend"}, started_at=now, completed_at=now,
        duration_seconds=0, result={"documents": {"checks": {"checks": [{"name": "lint", "status": "PASS", "exit_code": 0, "duration_seconds": 0}]}}})
    # Original command output is hashed separately from the normalized result.
    content = json.loads(receipt.read_text())
    source = save(evidence / "original-checks.json", content["result"]["documents"]["checks"])
    content["evidence"].append({"kind": "source-checks", "path": source.name, "sha256": sha256(source)})
    save(receipt, content)
    return {"manifest_path": manifest, "selection_path": selection, "evidence_dir": evidence,
            "jobs_path": jobs, "image_manifest_path": images, "source_sha": SHA, "run_id": "123", "run_attempt": "1"}, receipt


def test_complete_same_commit_gate_passes_and_binds_every_receipt(gate_case):
    args, _ = gate_case
    result = evaluate(**args)
    assert result["status"] == "PASS" and result["certifies_final"] is True
    assert result["scenario_count"] == 1 and result["receipts"][0]["sha256"]


@pytest.mark.parametrize("field,value,code", [
    ("status", "RUNNING", "SCENARIO_FAILED_CANCELLED_SKIPPED_OR_RUNNING"),
    ("status", "FAIL", "SCENARIO_FAILED_CANCELLED_SKIPPED_OR_RUNNING"),
    ("status", "CANCELLED", "SCENARIO_FAILED_CANCELLED_SKIPPED_OR_RUNNING"),
    ("status", "SKIP", "SCENARIO_FAILED_CANCELLED_SKIPPED_OR_RUNNING"),
    ("source_sha", "b" * 40, "SCENARIO_COMMIT_MISMATCH"),
    ("group", "frontend", "WRONG_SCENARIO_GROUP"),
    ("rows", 1, "SCENARIO_ROWS_OR_VARIANT_MISMATCH"),
    ("completed_at", None, "MISSING_OR_INVALID_SCENARIO_TIMING"),
    ("duration_seconds", -1, "INVALID_SCENARIO_TIMING"),
    ("evidence", [], "MISSING_EVIDENCE_ATTACHMENTS"),
    ("kind", "ALIEN_PASS", "UNKNOWN_RECEIPT_KIND"),
])
def test_gate_rejects_incomplete_different_commit_skip_and_unknown_receipts(gate_case, field, value, code):
    args, receipt = gate_case
    data = json.loads(receipt.read_text()); data[field] = value; save(receipt, data)
    with pytest.raises(EvidenceError, match=code): evaluate(**args)


@pytest.mark.parametrize("key,value", [("run_id", "124"), ("run_attempt", "2"), ("job_id", "frontend")])
def test_gate_rejects_wrong_run_attempt_and_job(gate_case, key, value):
    args, receipt = gate_case
    data = json.loads(receipt.read_text()); data["ci"][key] = value; save(receipt, data)
    with pytest.raises(EvidenceError, match="SCENARIO_RUN_ATTEMPT_OR_JOB_MISMATCH"): evaluate(**args)


def test_gate_rejects_missing_and_duplicate_scenarios(gate_case):
    args, receipt = gate_case
    copied = receipt.with_name("scenario-duplicate.json"); shutil.copyfile(receipt, copied)
    with pytest.raises(EvidenceError, match="DUPLICATE_SCENARIO"): evaluate(**args)
    copied.unlink(); receipt.unlink()
    with pytest.raises(EvidenceError, match="MISSING_MANDATORY_SCENARIOS"): evaluate(**args)


@pytest.mark.parametrize("result", ["failure", "cancelled", "skipped", "running", None])
def test_gate_does_not_inherit_green_from_cancelled_or_unfinished_jobs(gate_case, result):
    args, _ = gate_case
    data = json.loads(args["jobs_path"].read_text()); data["needs"]["suites"]["result"] = result; save(args["jobs_path"], data)
    with pytest.raises(EvidenceError, match="JOB_INCOMPLETE_FAILED_CANCELLED_OR_SKIPPED"): evaluate(**args)


def test_gate_rejects_missing_altered_and_escaping_attachments(gate_case):
    args, receipt = gate_case
    data = json.loads(receipt.read_text()); attachment = args["evidence_dir"] / data["evidence"][0]["path"]
    attachment.write_text("{}")
    with pytest.raises(EvidenceError, match="ALTERED_EVIDENCE_ATTACHMENT"): evaluate(**args)
    attachment.unlink()
    with pytest.raises(EvidenceError, match="MISSING_EVIDENCE_ATTACHMENT"): evaluate(**args)
    data["evidence"][0]["path"] = "../outside.json"; save(receipt, data)
    with pytest.raises(EvidenceError, match="EVIDENCE_PATH_ESCAPE"): evaluate(**args)


def test_status_pass_with_missing_real_command_content_is_not_evidence(gate_case):
    args, receipt = gate_case
    data = json.loads(receipt.read_text()); data["result"] = {"status": "PASS"}
    snapshot = args["evidence_dir"] / data["evidence"][0]["path"]; save(snapshot, data["result"])
    data["evidence"][0]["sha256"] = sha256(snapshot); save(receipt, data)
    with pytest.raises(EvidenceError, match="COMMAND_CHECK_MISSING_OR_FAILED"): evaluate(**args)


def test_fast_is_only_green_as_development_and_never_final(gate_case):
    args, _ = gate_case
    data = json.loads(args["selection_path"].read_text()); data["mode"] = "fast"; save(args["selection_path"], data)
    with pytest.raises(EvidenceError, match="FAST_IS_NOT_FINAL_CERTIFICATION"): evaluate(**args)
    result = evaluate(**args, development_only=True)
    assert result["status"] == "PASS" and result["scope"] == "DEVELOPMENT_ONLY" and not result["certifies_final"]
    data["mode"] = "full"; save(args["selection_path"], data)
    with pytest.raises(EvidenceError, match="DEVELOPMENT_FLAG_WITH_FULL_MODE"): evaluate(**args, development_only=True)


def test_manifest_and_image_metadata_are_commit_bound(gate_case):
    args, _ = gate_case
    data = json.loads(args["image_manifest_path"].read_text()); data["source_sha"] = "b" * 40; save(args["image_manifest_path"], data)
    with pytest.raises(EvidenceError, match="IMAGE_MANIFEST_SHA_OR_STATUS"): evaluate(**args)


def test_duplicate_json_keys_are_never_silently_last_wins(gate_case):
    args, receipt = gate_case
    receipt.write_text('{"kind":"SCENARIO_EVIDENCE","kind":"XLSX_SCENARIO"}')
    with pytest.raises(EvidenceError, match="DUPLICATE_JSON_KEY"): evaluate(**args)


@pytest.mark.parametrize("changed", ["image_bundle_sha256", "backend", "web"])
def test_every_compose_receipt_binds_the_verified_image_bundle_and_both_roles(gate_case, changed):
    args, receipt = gate_case
    manifest = json.loads(args["manifest_path"].read_text())
    manifest["groups"][0].update(id="fixture-heavy", job_key="suite-fixture-heavy")
    manifest["fast_groups"] = ["fixture-heavy"]
    save(args["manifest_path"], manifest)
    selection = json.loads(args["selection_path"].read_text())
    selection.update(groups=["fixture-heavy"], manifest_sha256=sha256(args["manifest_path"]))
    save(args["selection_path"], selection)
    data = json.loads(receipt.read_text())
    data.update(group="fixture-heavy", ci={"run_id": "123", "run_attempt": "1", "job_id": "suite-fixture-heavy"},
                resources={"image_bundle_sha256": sha256(args["image_manifest_path"]),
                           "runtime_images": {"backend": "sha256:" + "1" * 64, "web": "sha256:" + "2" * 64}})
    save(receipt, data)
    assert evaluate(**args)["status"] == "PASS"
    if changed == "image_bundle_sha256":
        data["resources"][changed] = "3" * 64
    else:
        data["resources"]["runtime_images"][changed] = "sha256:" + "3" * 64
    save(receipt, data)
    with pytest.raises(EvidenceError, match="SCENARIO_IMAGE_BUNDLE_MISMATCH"):
        evaluate(**args)


def test_downloaded_artifact_subdirectories_keep_receipt_relative_attachment_paths(gate_case):
    args, _ = gate_case
    base = args["evidence_dir"].parent / "downloaded"
    nested = base / "artifact-suite" / "ci" / "evidence"
    nested.parent.mkdir(parents=True)
    shutil.move(str(args["evidence_dir"]), str(nested))
    args["evidence_dir"] = base
    assert evaluate(**args)["receipts"][0]["path"].startswith("artifact-suite/ci/evidence/")
