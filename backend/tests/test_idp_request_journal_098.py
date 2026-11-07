"""Durable request identity, bounded retention and recovery without inference."""
import hashlib
import importlib.util
import json
import os
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[2] / "deploy/idp"
SPEC = importlib.util.spec_from_file_location("request_journal_test", ROOT / "idp_request_journal.py")
journal = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(journal)


def request(number=1):
    return {"request_id": f"{number:032x}", "admission_id": f"{number + 100:032x}",
        "rpc_contract": journal.VERSION, "source_sha256": hashlib.sha256(b"synthetic source").hexdigest(),
        "configuration": {"timeout_seconds": 30}, "definition": {"fields": [], "tables": []}}


def recovery(receipt):
    return {key: receipt[key] for key in ("request_id", "admission_id", "request_sha256", "configuration_sha256")}


@pytest.fixture
def store(tmp_path):
    value = journal.RequestJournal(tmp_path / "requests")
    yield value
    value.close()


def test_complete_result_is_durable_and_recoverable_after_gateway_restart(store):
    body = request()
    receipt = store.admit("/extract", body, SimpleNamespace(duration=30))
    store.update(body["request_id"], body["admission_id"], expected_pages=2)
    original = {"source_sha256": body["source_sha256"], "page": 1, "original": {"table": "synthetic raw"}}
    page = store.checkpoint(body["request_id"], body["admission_id"], original, page=1)
    result = {"source_sha256": body["source_sha256"], "lines": [], "engine": {}}
    store.finish(body["request_id"], body["admission_id"], "COMPLETE", released=True, result=result)
    root = store.root
    store.close()
    reopened = journal.RequestJournal(root)
    try:
        terminal = reopened.get(body["request_id"])
        assert terminal["status"] == "COMPLETE" and terminal["resources_released"] is True
        assert terminal["completed_pages"] == [1] and terminal["pending_pages"] == [2]
        assert "file" not in terminal["page_refs"][0]
        recovered = reopened.recover(recovery(receipt))
        assert recovered["result"] == result
        assert recovered["result_sha256"] == journal.digest(result)
        assert json.loads((root / page["file"]).read_bytes()) == original
    finally:
        reopened.close()


@pytest.mark.parametrize("key", ["admission_id", "request_sha256", "configuration_sha256"])
def test_recovery_rejects_wrong_nonce_or_input_configuration_binding(store, key):
    body = request()
    receipt = store.admit("/extract", body, SimpleNamespace(duration=30))
    store.finish(body["request_id"], body["admission_id"], "COMPLETE", released=True,
                 result={"source_sha256": body["source_sha256"], "engine": {}})
    attempt = recovery(receipt)
    attempt[key] = "0" * len(attempt[key])
    with pytest.raises(ValueError, match="REQUEST_BINDING"):
        store.recover(attempt)


def test_incomplete_result_cannot_be_recovered_as_success(store):
    body = request()
    receipt = store.admit("/extract", body, SimpleNamespace(duration=30))
    store.checkpoint(body["request_id"], body["admission_id"], {"page": 1}, page=1)
    store.finish(body["request_id"], body["admission_id"], "TIMEOUT", released=True,
                 error="INFERENCE_TIMEOUT", original_error="INFERENCE_TIMEOUT")
    with pytest.raises(ValueError, match="REQUEST_BINDING"):
        store.recover(recovery(receipt))
    assert store.get(body["request_id"])["completed_pages"] == [1]


def test_restart_marks_unfinished_admission_interrupted_and_quarantines(store):
    body = request()
    store.admit("/extract", body, SimpleNamespace(duration=30))
    page = store.checkpoint(body["request_id"], body["admission_id"], {"original": "synthetic"}, page=1)
    root = store.root
    before = (root / page["file"]).read_bytes()
    store.close()
    reopened = journal.RequestJournal(root)
    try:
        assert reopened.available() is False
        terminal = reopened.get(body["request_id"])
        assert terminal["status"] == "INTERRUPTED" and terminal["resources_released"] is False
        assert terminal["error_code"] == "GATEWAY_INTERRUPTED"
        with pytest.raises(ValueError, match="REQUEST_JOURNAL_UNAVAILABLE"):
            reopened.admit("/extract", request(2), SimpleNamespace(duration=30))
        assert (root / page["file"]).read_bytes() == before
        assert (root / "journal.quarantine").is_file()
    finally:
        reopened.close()


def test_terminal_capacity_never_evicts_an_unexpired_receipt(tmp_path, monkeypatch):
    monkeypatch.setattr(journal, "MAX_RECORDS", 2)
    observed = [100.0]
    store = journal.RequestJournal(tmp_path / "requests", clock=lambda: observed[0], ttl_seconds=10)
    try:
        for number in (1, 2):
            body = request(number)
            store.admit("/extract", body, SimpleNamespace(duration=30))
            store.finish(body["request_id"], body["admission_id"], "FAILED", released=True, error="PADDLE_FAILURE")
        with pytest.raises(ValueError, match="REQUEST_JOURNAL_LIMIT"):
            store.admit("/extract", request(3), SimpleNamespace(duration=30))
        assert store.get(request(1)["request_id"])["status"] == "FAILED"
        observed[0] = 111.0
        assert store.get(request(1)["request_id"]) is None
        assert store.admit("/extract", request(3), SimpleNamespace(duration=30))["status"] == "QUEUED"
    finally:
        store.close()


def test_active_and_terminal_request_ids_are_not_reexecuted(store):
    body = request()
    store.admit("/extract", body, SimpleNamespace(duration=30))
    with pytest.raises(ValueError, match="DUPLICATE_REQUEST"):
        store.admit("/extract", body, SimpleNamespace(duration=30))
    store.finish(body["request_id"], body["admission_id"], "COMPLETE", released=True)
    with pytest.raises(ValueError, match="DUPLICATE_REQUEST"):
        store.admit("/extract", {**body, "admission_id": "f" * 32}, SimpleNamespace(duration=30))


def test_modified_result_bytes_are_not_recovered(store):
    body = request()
    receipt = store.admit("/extract", body, SimpleNamespace(duration=30))
    store.finish(body["request_id"], body["admission_id"], "COMPLETE", released=True, result={"engine": {}})
    ref = store.records[body["request_id"]]["result_ref"]
    (store.root / ref["file"]).write_bytes(b'{"forged":true}')
    with pytest.raises(ValueError, match="REQUEST_JOURNAL_INTEGRITY"):
        store.recover(recovery(receipt))


def test_recovery_rejects_changed_durable_metadata_even_with_ram_cache(store):
    body = request()
    receipt = store.admit("/extract", body, SimpleNamespace(duration=30))
    store.finish(body["request_id"], body["admission_id"], "COMPLETE", released=True, result={"engine": {}})
    path = store.root / (body["request_id"] + ".json")
    path.write_bytes(b'{"forged":true}')
    with pytest.raises(ValueError, match="REQUEST_JOURNAL_INTEGRITY"):
        store.recover(recovery(receipt))
    assert store.quarantined
    assert path.read_bytes() == b'{"forged":true}'


def test_shared_page_result_blob_is_deleted_once_on_expiry(tmp_path):
    now = [100.0]
    store = journal.RequestJournal(tmp_path / "requests", clock=lambda: now[0], ttl_seconds=10)
    try:
        body = request()
        store.admit("/extract", body, SimpleNamespace(duration=30))
        value = {"source_sha256": body["source_sha256"], "engine": {}}
        store.checkpoint(body["request_id"], body["admission_id"], value, page=1)
        store.finish(body["request_id"], body["admission_id"], "COMPLETE", released=True, result=value)
        assert len(list(store.root.glob("*.data"))) == 1
        now[0] = 111.0
        assert store.get(body["request_id"]) is None
        assert not list(store.root.glob("*.data")) and not store.quarantined
    finally:
        store.close()


def test_incomplete_page_is_retained_and_emitted_without_becoming_completed(store):
    body = request()
    store.admit("/recognize", body, SimpleNamespace(duration=30))
    store.update(body["request_id"], body["admission_id"], expected_pages=1)
    store.checkpoint(body["request_id"], body["admission_id"], {"recognition_status": "INCOMPLETE"}, page=1, complete=False)
    terminal = store.finish(body["request_id"], body["admission_id"], "FAILED", released=True)
    assert terminal["emitted_pages"] == [1]
    assert terminal["completed_pages"] == [] and terminal["pending_pages"] == [1]
    assert terminal["page_refs"][0]["page"] == 1 and terminal["page_refs"][0]["complete"] is False


def test_byte_limit_preserves_preceding_checkpoint_without_claiming_more(store, monkeypatch):
    body = request()
    store.admit("/extract", body, SimpleNamespace(duration=30))
    first = store.checkpoint(body["request_id"], body["admission_id"], {"page": 1}, page=1)
    before = (store.root / first["file"]).read_bytes()
    monkeypatch.setattr(journal, "MAX_BYTES", journal.MAX_METADATA + 2048)
    with pytest.raises(ValueError, match="REQUEST_JOURNAL_LIMIT"):
        store.checkpoint(body["request_id"], body["admission_id"], {"page": 2, "text": "x" * 4096}, page=2)
    assert store.get(body["request_id"])["completed_pages"] == [1]
    assert (store.root / first["file"]).read_bytes() == before


def test_terminal_expires_without_another_request(tmp_path):
    store = journal.RequestJournal(tmp_path / "requests", ttl_seconds=1)
    try:
        body = request()
        store.admit("/extract", body, SimpleNamespace(duration=30))
        store.finish(body["request_id"], body["admission_id"], "FAILED", released=True)
        deadline = time.monotonic() + 3
        while store.records and time.monotonic() < deadline:
            time.sleep(.02)
        assert not store.records
        assert sorted(path.name for path in store.root.iterdir()) == ["journal.lock"]
    finally:
        store.close()


@pytest.mark.skipif(os.name != "posix", reason="POSIX directory ownership and symlink contract")
def test_symlink_root_is_rejected_and_foreign_bytes_preserved(tmp_path):
    foreign = tmp_path / "foreign"
    foreign.mkdir()
    (foreign / "keep").write_bytes(b"foreign evidence")
    link = tmp_path / "requests"
    link.symlink_to(foreign, target_is_directory=True)
    store = journal.RequestJournal(link)
    try:
        assert store.available() is False
        assert (foreign / "keep").read_bytes() == b"foreign evidence"
    finally:
        store.close()
