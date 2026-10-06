import json
import shutil
import sys
import xml.etree.ElementTree as ET
from datetime import UTC, datetime
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from scripts.ci.common import FUNCTIONAL_MODULES, EvidenceError, sha256
from scripts.ci.evidence import write_receipt
from scripts.ci.final_gate import evaluate
from scripts.ci.validators import SPARK_REAL_CASES, junit_document

SHA = "a" * 40


def save(path, value):
    path.write_text(json.dumps(value), encoding="utf-8")
    return path


@pytest.fixture
def gate_case(tmp_path):
    manifest = save(tmp_path / "manifest.json", {"schema_version": 1, "profiles": {
        "functional": {"groups": ["backend"], "module_coverage": {
            name: {"scenario_ids": ["backend-lint"]} for name in FUNCTIONAL_MODULES}},
        "deep": {"groups": ["backend"]}},
        "groups": [{"id": "backend", "job_key": "backend", "required_sources": [],
                    "scenarios": [{"id": "backend-lint", "validator": "backend-check", "check": "lint"}]}]})
    selection = save(tmp_path / "selection.json", {"schema_version": 1, "source_sha": SHA, "manifest_sha256": sha256(manifest),
        "mode": "functional", "profile": "functional", "groups": ["backend"]})
    jobs = save(tmp_path / "jobs.json", {"source_sha": SHA, "run_id": "123", "run_attempt": "1", "needs": {
        name: {"result": "success"} for name in ("select", "backend", "frontend", "images", "suites")}})
    images = save(tmp_path / "images.json", {"schema_version": 1, "source_sha": SHA, "status": "PASS",
        "digest_kind": "DOCKER_CONFIGURATION_SHA256", "images": {
            role: {"image_id": "sha256:" + digit * 64, "archive": role + ".tar", "archive_sha256": digit * 64, "archive_bytes": 1}
            for role, digit in (("backend", "1"), ("web", "2"))}})
    evidence = tmp_path / "evidence"
    now = datetime.now(UTC).isoformat()
    receipt = write_receipt(evidence, group="backend", scenario_id="backend-lint", source_sha=SHA,
        ci={"run_id": "123", "run_attempt": "1", "job_id": "backend"}, started_at=now, completed_at=now,
        duration_seconds=0, resources={"profile": "functional"},
        result={"documents": {"checks": {"checks": [{"name": "lint", "status": "PASS", "exit_code": 0, "duration_seconds": 0}]}}})
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
    assert result["status"] == "PASS" and result["functional_approved"] is True
    assert result["scope"] == "CI_FUNCTIONAL_APPROVED" and not result["deep_approved"] and not result["certifies_final"]
    assert result["scenario_count"] == 1 and result["receipts"][0]["sha256"]


@pytest.mark.parametrize("damage,code", [(None, None), ("missing", "SPARK_FUNCTIONAL_COVERAGE_MISSING"),
                                        ("skipped", "SPARK_FUNCTIONAL_CASE_SKIPPED"),
                                        ("failure", "TEST_FAILURE_OR_ERROR")])
def test_gate_independently_requires_actual_spark_cases_in_backend_original_junit(gate_case, damage, code):
    args, receipt = gate_case
    manifest = json.loads(args["manifest_path"].read_text())
    manifest["groups"][0]["scenarios"][0]["check"] = "unit-tests"
    save(args["manifest_path"], manifest)
    selection = json.loads(args["selection_path"].read_text())
    selection["manifest_sha256"] = sha256(args["manifest_path"])
    save(args["selection_path"], selection)
    suite = ET.Element("testsuite")
    for index in range(1800):
        ET.SubElement(suite, "testcase", classname="tests.ordinary", name=f"case_{index}")
    for module, name in sorted(SPARK_REAL_CASES):
        if damage == "missing" and module == "test_spark_service_e2e":
            continue
        case = ET.SubElement(suite, "testcase", classname="tests." + module, name=name)
        if damage and module == "test_spark_service_e2e":
            ET.SubElement(case, damage, message="NOT_RUN_OPT_IN: real local Spark publication suite")
    original = args["evidence_dir"] / "original-junit.xml"
    ET.ElementTree(suite).write(original, encoding="utf-8")
    data = json.loads(receipt.read_text())
    data["result"]["documents"]["checks"]["checks"][0]["name"] = "unit-tests"
    data["result"]["documents"]["junit"] = junit_document(original)
    save(args["evidence_dir"] / "original-checks.json", data["result"]["documents"]["checks"])
    save(args["evidence_dir"] / data["evidence"][0]["path"], data["result"])
    for ref in data["evidence"]:
        ref["sha256"] = sha256(args["evidence_dir"] / ref["path"])
    data["evidence"].append({"kind": "source-junit", "path": original.name, "sha256": sha256(original)})
    save(receipt, data)
    if code:
        with pytest.raises(EvidenceError, match=code):
            evaluate(**args)
    else:
        assert evaluate(**args)["functional_approved"]


def test_failure_diagnostic_cannot_replace_a_missing_scenario_or_failed_job(gate_case):
    args, receipt = gate_case
    save(args['evidence_dir'] / 'failure-catalog.json', {'kind': 'FAILURE_DIAGNOSTIC', 'status': 'FAIL',
                                                       'certifies_final': False})
    receipt.unlink()
    with pytest.raises(EvidenceError, match='MISSING_MANDATORY_SCENARIOS'):
        evaluate(**args)
    data = json.loads(args['jobs_path'].read_text())
    data['needs']['suites']['result'] = 'failure'; save(args['jobs_path'], data)
    with pytest.raises(EvidenceError, match='JOB_INCOMPLETE_FAILED_CANCELLED_OR_SKIPPED'):
        evaluate(**args)


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


@pytest.mark.parametrize("mode", ["fast", "full", "deep", None])
def test_github_gate_cannot_certify_fast_or_local_deep_work(gate_case, mode):
    args, _ = gate_case
    data = json.loads(args["selection_path"].read_text()); data["mode"] = mode; save(args["selection_path"], data)
    with pytest.raises(EvidenceError, match="NON_FUNCTIONAL_GITHUB_SELECTION"): evaluate(**args)
    with pytest.raises(EvidenceError, match="LEGACY_DEVELOPMENT_GATE_REMOVED"): evaluate(**args, development_only=True)


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
    for profile in manifest["profiles"].values():
        profile["groups"] = ["fixture-heavy"]
    save(args["manifest_path"], manifest)
    selection = json.loads(args["selection_path"].read_text())
    selection.update(groups=["fixture-heavy"], manifest_sha256=sha256(args["manifest_path"]))
    save(args["selection_path"], selection)
    data = json.loads(receipt.read_text())
    data.update(group="fixture-heavy", ci={"run_id": "123", "run_attempt": "1", "job_id": "suite-fixture-heavy"},
                resources={"profile": "functional", "image_bundle_sha256": sha256(args["image_manifest_path"]),
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


@pytest.mark.parametrize("resources", [{}, {"profile": "deep"}, {"profile": "docs"}, None])
def test_gate_requires_functional_provenance_even_for_static_checks(gate_case, resources):
    args, receipt = gate_case
    data = json.loads(receipt.read_text()); data["resources"] = resources; save(receipt, data)
    with pytest.raises(EvidenceError, match="SCENARIO_PROFILE_METADATA_MISMATCH"):
        evaluate(**args)


def test_local_receipts_cannot_be_relabelled_as_github_approval(gate_case):
    args, receipt = gate_case
    data = json.loads(receipt.read_text())
    data["execution"] = {"kind": "LOCAL", "execution_id": "local-" + "a" * 32, "group": "backend"}
    data["ci"] = None; save(receipt, data)
    with pytest.raises(EvidenceError, match="SCENARIO_RUN_ATTEMPT_OR_JOB_MISMATCH"):
        evaluate(**args)
    data["ci"] = {"run_id": "123", "run_attempt": "1", "job_id": "backend"}; save(receipt, data)
    with pytest.raises(EvidenceError, match="LOCAL_EVIDENCE_CANNOT_APPROVE_GITHUB"):
        evaluate(**args)


@pytest.mark.parametrize("job", ["select", "backend", "frontend", "images", "suites"])
def test_every_required_product_job_must_be_present(gate_case, job):
    args, _ = gate_case
    data = json.loads(args["jobs_path"].read_text()); data["needs"].pop(job); save(args["jobs_path"], data)
    with pytest.raises(EvidenceError, match="JOB_INCOMPLETE_FAILED_CANCELLED_OR_SKIPPED"):
        evaluate(**args)


@pytest.fixture
def documentation_case(gate_case, monkeypatch):
    from scripts.ci import common
    from scripts.ci.check_documentation import validate_documentation
    args, _ = gate_case
    root = args["selection_path"].parent
    (root / "README.md").write_text("# Documentation\n", encoding="utf-8")
    monkeypatch.setattr(common, "ROOT", root)
    selection = json.loads(args["selection_path"].read_text())
    selection.update(mode="docs", scope="DOCUMENTATION_ONLY", groups=[], changed_files=[{"path": "README.md", "rule": "documentation"}])
    save(args["selection_path"], selection)
    jobs = json.loads(args["jobs_path"].read_text())
    for job in ("backend", "frontend", "images", "suites"):
        jobs["needs"][job] = {"result": "skipped"}
    jobs["needs"]["documentation"] = {"result": "success"}; save(args["jobs_path"], jobs)
    args["documentation_path"] = save(root / "documentation.json", {
        "schema_version": 1, "kind": "DOCUMENTATION_EVIDENCE", "status": "PASS", "source_sha": SHA,
        "selection_sha256": sha256(args["selection_path"]),
        "ci": {"run_id": "123", "run_attempt": "1", "job_id": "documentation"},
        "documents": validate_documentation(root, ["README.md"])})
    # A documentation-only run creates neither images nor scenario evidence.
    shutil.rmtree(args["evidence_dir"]); args["image_manifest_path"].unlink()
    return args


def test_docs_only_gate_passes_without_product_artifacts(documentation_case):
    result = evaluate(**documentation_case)
    assert result["status"] == "PASS" and result["scope"] == "DOCUMENTATION_APPROVED"
    assert result["scenario_count"] == 0 and not result["functional_approved"] and not result["deep_approved"]


@pytest.mark.parametrize("status", ["failure", "cancelled", "skipped", None])
def test_docs_only_gate_rejects_missing_failed_or_cancelled_documentation(documentation_case, status):
    args = documentation_case
    jobs = json.loads(args["jobs_path"].read_text()); jobs["needs"]["documentation"] = {"result": status}; save(args["jobs_path"], jobs)
    with pytest.raises(EvidenceError, match="JOB_INCOMPLETE_FAILED_CANCELLED_OR_SKIPPED"):
        evaluate(**args)


def test_docs_only_status_pass_cannot_replace_content_verification(documentation_case):
    args = documentation_case
    data = json.loads(args["documentation_path"].read_text()); data["documents"] = []; save(args["documentation_path"], data)
    with pytest.raises(EvidenceError, match="DOCUMENTATION_CONTENT_OR_COVERAGE_MISMATCH"):
        evaluate(**args)
