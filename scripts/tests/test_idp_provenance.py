"""Benchmark evidence binds actual code bytes without implying OCI certification."""
import json

import idp_benchmark
import pytest
from idp_corpus import implementation_record


@pytest.fixture
def source(tmp_path):
    files = {"backend/src/trackvance/__init__.py": '__version__ = "0.9.8"\n',
        "backend/src/trackvance/engine.py": 'MODEL = "synthetic"\n',
        "backend/pyproject.toml": '[project]\nversion = "0.9.8"\n', "backend/uv.lock": "version = 1\n",
        "scripts/idp_corpus.py": "# corpus runner\n", "scripts/idp_benchmark.py": "# host runner\n",
        "scripts/idp_capacity.py": "# capacity estimator\n"}
    for relative, content in files.items():
        path = tmp_path / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    return tmp_path


def test_implementation_hashes_actual_backend_and_lock_bytes_without_oracle_data(source):
    record = implementation_record(source, "a" * 40)
    assert record["version"] == "0.9.8" and record["source_sha"] == "a" * 40
    assert record["source_sha_origin"] == "CALLER_DECLARED" and not record["source_sha_verified_in_workload"]
    assert len(record["files"]) == 7 and all(not item["path"].startswith("/") for item in record["files"])
    (source / "oracle.private.json").write_text('{"sensitive_expected": "not source code"}', encoding="utf-8")
    assert implementation_record(source)["manifest_sha256"] == record["manifest_sha256"]
    (source / "backend/src/trackvance/engine.py").write_text('MODEL = "changed"\n', encoding="utf-8")
    assert implementation_record(source)["manifest_sha256"] != record["manifest_sha256"]


def test_source_sha_is_explicitly_unavailable_without_caller_declaration(source):
    record = implementation_record(source)
    assert record["source_sha"] is None and record["source_sha_origin"] == "UNAVAILABLE"
    with pytest.raises(ValueError, match="FULL_SOURCE_SHA_REQUIRED"):
        implementation_record(source, "short-sha")


def test_incomplete_implementation_manifest_cannot_claim_provenance(source):
    (source / "backend/uv.lock").unlink()
    with pytest.raises(ValueError, match="REGULAR_SOURCE_FILES_REQUIRED"):
        implementation_record(source)


@pytest.mark.parametrize(("dirty", "sha"), [(" M scripts/idp_corpus.py", "a" * 40), ("", "short")])
def test_wrapper_rejects_dirty_or_abbreviated_host_git_source(source, monkeypatch, dirty, sha):
    def command(arguments):
        if "--show-toplevel" in arguments:
            return str(source)
        if "HEAD" in arguments:
            return sha
        return dirty
    monkeypatch.setattr(idp_benchmark, "command", command)
    with pytest.raises(ValueError, match="CLEAN_EXACT_HOST_GIT_SOURCE_REQUIRED"):
        idp_benchmark.verified_host_source(source)


def test_wrapper_verifies_host_git_but_does_not_relabel_workload_sha_as_verified(source, monkeypatch):
    monkeypatch.setattr(idp_benchmark, "command", lambda arguments: str(source) if "--show-toplevel" in arguments
        else "a" * 40 if "HEAD" in arguments else "")
    host = idp_benchmark.verified_host_source(source)
    assert host["clean_git_verified_on_host"] and not host["oci_content_certified"]
    assert not host["implementation"]["source_sha_verified_in_workload"]
    actual = {**host["implementation"], "source_unchanged_during_run": True, "runtime_version": "0.9.8"}
    result = source / "result.json"
    result.write_text(json.dumps({"implementation": actual}), encoding="utf-8")
    proof = idp_benchmark.workload_provenance(result, host)
    assert proof["actual_bytes_match_verified_host"] and not proof["oci_content_certified"]
    actual["source_unchanged_during_run"] = False
    result.write_text(json.dumps({"implementation": actual}), encoding="utf-8")
    assert not idp_benchmark.workload_provenance(result, host)["actual_bytes_match_verified_host"]


def test_wrapper_rejects_same_declared_sha_with_different_actual_lock_bytes(source):
    old = implementation_record(source, "a" * 40)
    host = {"source_sha": "a" * 40, "implementation": old}
    (source / "backend/uv.lock").write_text("version = 2\n", encoding="utf-8")
    actual = {**implementation_record(source, "a" * 40), "source_unchanged_during_run": True, "runtime_version": "0.9.8"}
    result = source / "result.json"
    result.write_text(json.dumps({"implementation": actual}), encoding="utf-8")
    assert not idp_benchmark.workload_provenance(result, host)["actual_bytes_match_verified_host"]


def test_retry_report_preserves_failed_attempt_and_existing_legacy_matrix(tmp_path):
    legacy = tmp_path / "benchmark-matrix.json"
    legacy.write_text('{"status":"FAIL","attempt":"legacy"}', encoding="utf-8")
    first = idp_benchmark.save_matrix(tmp_path, {"status": "FAIL", "attempt": 1})
    first_bytes = first.read_bytes()
    second = idp_benchmark.save_matrix(tmp_path, {"status": "PASS", "attempt": 2})
    assert first != second and first.read_bytes() == first_bytes
    assert json.loads(first_bytes)["status"] == "FAIL"
    assert json.loads(second.read_bytes())["status"] == "PASS"
    assert json.loads(legacy.read_bytes()) == {"status": "FAIL", "attempt": "legacy"}
