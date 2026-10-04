"""Disposable proof runners must fail closed before deleting Docker resources."""

from __future__ import annotations

import hashlib
import importlib.util
import json
from pathlib import Path

import pytest

PROOF = Path(__file__).resolve().parents[2] / "scripts" / "tests" / "spark_docker_guard.py"
SPEC = importlib.util.spec_from_file_location("spark_proof_guards", PROOF)
assert SPEC is not None and SPEC.loader is not None
guards = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(guards)
OWNER = "trackvance-v070-test-spark-0123456789ab"


def test_evidence_stays_in_private_new_directory(tmp_path, monkeypatch):
    monkeypatch.setattr(guards, "ROOT", tmp_path)
    allowed = tmp_path / ".codex-local" / "v070" / "spark" / "proof"
    assert guards.prepare_evidence(allowed) == allowed
    with pytest.raises(FileExistsError):
        guards.prepare_evidence(allowed)
    with pytest.raises(ValueError):
        guards.prepare_evidence(tmp_path / "outside")
    with pytest.raises(ValueError):
        guards.prepare_evidence(allowed.parent)


@pytest.mark.parametrize("name,owner,labels", [
    ("trackvance-certification-api-1", OWNER, {guards.OWNER_LABEL: OWNER}),
    (OWNER + "-driver", "trackvance-certification", {}),
    (OWNER + "-driver", OWNER, None),
    (OWNER + "-driver", OWNER, {guards.OWNER_LABEL: "another-owner"}),
])
def test_cleanup_rejects_unowned_container_before_removing(name, owner, labels, monkeypatch):
    calls = []

    def docker(*arguments, **_kwargs):
        calls.append(arguments)
        return json.dumps([{"Config": {"Labels": labels}}])

    monkeypatch.setattr(guards, "docker", docker)
    with pytest.raises(RuntimeError):
        guards.remove_owned([name], None, None, owner)
    assert not any(call[0] == "rm" for call in calls)


@pytest.mark.parametrize("kind", ["network", "volume"])
def test_cleanup_rejects_resource_owner_mismatch(kind, monkeypatch):
    calls = []

    def docker(*arguments, **_kwargs):
        calls.append(arguments)
        return json.dumps([{"Labels": {guards.OWNER_LABEL: "another-owner"}}])

    monkeypatch.setattr(guards, "docker", docker)
    with pytest.raises(RuntimeError):
        guards.remove_owned([], OWNER + "-network" if kind == "network" else None,
                            OWNER + "-data" if kind == "volume" else None, OWNER)
    assert all(call[1] != "rm" for call in calls)


@pytest.mark.parametrize("failing_tag", ["skipped", "failure", "error"])
def test_dedicated_suite_rejects_skips_and_failures(tmp_path, failing_tag):
    report = tmp_path / "parity.xml"
    cases = "<testcase/>" * 34 + f"<testcase><{failing_tag}/></testcase>"
    report.write_text(f"<testsuite>{cases}</testsuite>", encoding="utf-8")
    with pytest.raises(RuntimeError):
        guards.assert_tests(report)
    report.write_text("<testsuite>" + "<testcase/>" * 35 + "</testsuite>", encoding="utf-8")
    assert guards.assert_tests(report) == {"passed": 35, "skipped": 0, "failed": 0}


def test_population_proof_requires_all_modules_and_real_executors(tmp_path):
    report = tmp_path / "evidence.json"
    fingerprint = {"method": "CANONICAL_JSON_ROW_SHA256_COUNT_SUM_XOR_V1", "rows": 1000000,
                   "sum_sha256": "1" * 64, "xor_sha256": "2" * 64,
                   "sha256": hashlib.sha256((1000000).to_bytes(8, "big")
                            + bytes.fromhex("1" * 64) + bytes.fromhex("2" * 64)).hexdigest()}
    evidence = {"status": "PASS", "input_population_rows": 1000000, "complete_value_verification": "PASS",
                "expected_logical_fingerprints": {name: fingerprint for name in ("intake", "recon", "sentinel", "intake_accepted")},
                "outcomes": {
        module: {"status": "PASS", "runtime": {"deployment_mode": "STANDALONE_CLIENT",
                 "executor_memory_status_entries": 3}, "results": {"logical_fingerprint": fingerprint},
                 "accepted": {"logical_fingerprint": fingerprint}} for module in ("intake", "recon", "sentinel")}}
    report.write_text(json.dumps(evidence), encoding="utf-8")
    assert guards.assert_population(report, 1000000, "STANDALONE_CLIENT") == evidence
    evidence["outcomes"]["intake"]["runtime"]["executor_memory_status_entries"] = 2
    report.write_text(json.dumps(evidence), encoding="utf-8")
    with pytest.raises(RuntimeError):
        guards.assert_population(report, 1000000, "STANDALONE_CLIENT")
    evidence["outcomes"]["intake"]["runtime"]["executor_memory_status_entries"] = 3
    evidence["outcomes"]["recon"]["results"]["logical_fingerprint"] = {**fingerprint, "sha256": "4" * 64}
    report.write_text(json.dumps(evidence), encoding="utf-8")
    with pytest.raises(RuntimeError, match="values differ"):
        guards.assert_population(report, 1000000, "STANDALONE_CLIENT")


def test_null_expected_and_missing_fingerprints_cannot_certify_population(tmp_path):
    report = tmp_path / "evidence.json"
    evidence = {"status": "PASS", "input_population_rows": 1000000, "complete_value_verification": "PASS",
                "expected_logical_fingerprints": {name: None for name in ("intake", "recon", "sentinel", "intake_accepted")},
                "outcomes": {module: {"status": "PASS", "runtime": {"deployment_mode": "LOCAL"}}
                             for module in ("intake", "recon", "sentinel")}}
    report.write_text(json.dumps(evidence), encoding="utf-8")
    with pytest.raises(RuntimeError, match="valid logical fingerprints"):
        guards.assert_population(report, 1000000, "LOCAL")
