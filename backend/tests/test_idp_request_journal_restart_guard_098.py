"""Unproven resource release survives restart, TTL and marker IO failures."""
import copy
import hashlib
import json
from types import SimpleNamespace

import pytest
from test_idp_request_journal_098 import journal, recovery, request


def fingerprint(root):
    return {path.name: hashlib.sha256(path.read_bytes()).hexdigest() for path in root.iterdir()
        if path.suffix in {".json", ".data"} and path.name != "journal.quarantine"}


def save_unreleased(root, now, monkeypatch, *, status="TIMEOUT", release=False):
    store = journal.RequestJournal(root, clock=lambda: now[0], ttl_seconds=10)
    body = request()
    store.admit("/extract", body, SimpleNamespace(duration=30))
    page = store.checkpoint(body["request_id"], body["admission_id"], {"public_synthetic_checkpoint": True}, page=1)
    publish = store._publish

    def marker_failure(name, raw):
        if name == "journal.quarantine":
            raise OSError("INJECTED_MARKER_PUBLICATION_FAILURE")
        return publish(name, raw)

    monkeypatch.setattr(store, "_publish", marker_failure)
    try:
        with pytest.raises(OSError, match="INJECTED_MARKER_PUBLICATION_FAILURE"):
            store.finish(body["request_id"], body["admission_id"], status,
                released=None if release == "MISSING" else release, error="PUBLIC_FAILURE",
                original_error="PUBLIC_ORIGINAL", cleanup_error="PUBLIC_RELEASE_UNPROVEN")
        assert store.quarantined is True
        path = root / (body["request_id"] + ".json")
        metadata = json.loads(path.read_bytes())
        if release == "MISSING":
            metadata.pop("resources_released")
            path.write_bytes(journal.canonical(metadata))
        else:
            assert metadata["resources_released"] is release
        assert not (root / "journal.quarantine").exists()
    finally:
        store.close()
    return body, page, copy.deepcopy(metadata)


@pytest.mark.parametrize("status", sorted(journal.TERMINAL))
@pytest.mark.parametrize("release", [False, None, "MISSING"])
@pytest.mark.parametrize("restart_time", [100.0, 111.0])
def test_unreleased_terminal_latches_before_prune_and_preserves_exact_record(tmp_path, monkeypatch, status, release, restart_time):
    now = [100.0]
    root = tmp_path / "requests"
    body, page, original = save_unreleased(root, now, monkeypatch, status=status, release=release)
    before = fingerprint(root)
    now[0] = restart_time
    reopened = journal.RequestJournal(root, clock=lambda: now[0], ttl_seconds=10)
    try:
        assert reopened.available() is False
        observed = reopened.get(body["request_id"])
        assert observed["status"] == status
        assert observed.get("resources_released") is original.get("resources_released")
        assert observed["error_code"] == "PUBLIC_FAILURE"
        assert observed["original_error_code"] == "PUBLIC_ORIGINAL"
        assert observed["cleanup_error_code"] == "PUBLIC_RELEASE_UNPROVEN"
        assert (root / "journal.quarantine").is_file()
        now[0] = 999.0
        assert reopened.available() is False and reopened.get(body["request_id"]) is not None
        assert fingerprint(root) == before
        assert (root / page["file"]).is_file()
        for attempt in (body, request(2)):
            with pytest.raises(ValueError, match="REQUEST_JOURNAL_UNAVAILABLE"):
                reopened.admit("/extract", attempt, SimpleNamespace(duration=30))
        assert len(reopened.records) == 1 and fingerprint(root) == before
    finally:
        reopened.close()


def test_restart_marker_publication_fails_again_without_admission_or_deletion(tmp_path, monkeypatch):
    now = [100.0]
    root = tmp_path / "requests"
    body, _, _ = save_unreleased(root, now, monkeypatch, release=None)
    before = fingerprint(root)
    now[0] = 111.0
    reopened = journal.RequestJournal(root, clock=lambda: now[0], ttl_seconds=10)
    publish = reopened._publish

    def marker_failure(name, raw):
        if name == "journal.quarantine":
            raise OSError("INJECTED_MARKER_FAILURE_AGAIN")
        return publish(name, raw)

    monkeypatch.setattr(reopened, "_publish", marker_failure)
    try:
        assert reopened.available() is False and reopened.quarantined is True
        with pytest.raises(ValueError, match="REQUEST_JOURNAL_UNAVAILABLE"):
            reopened.admit("/extract", body, SimpleNamespace(duration=30))
        assert not (root / "journal.quarantine").exists() and fingerprint(root) == before
    finally:
        reopened.close()


@pytest.mark.parametrize("release", [False, None])
def test_live_unknown_release_cannot_be_pruned_when_marker_write_fails(tmp_path, monkeypatch, release):
    now = [100.0]
    root = tmp_path / "requests"
    store = journal.RequestJournal(root, clock=lambda: now[0], ttl_seconds=10)
    body = request()
    try:
        store.admit("/extract", body, SimpleNamespace(duration=30))
        store.checkpoint(body["request_id"], body["admission_id"], {"public_raw": True}, page=1)
        publish = store._publish

        def marker_failure(name, raw):
            if name == "journal.quarantine":
                raise OSError("INJECTED_LIVE_MARKER_FAILURE")
            return publish(name, raw)

        monkeypatch.setattr(store, "_publish", marker_failure)
        with pytest.raises(OSError, match="INJECTED_LIVE_MARKER_FAILURE"):
            store.finish(body["request_id"], body["admission_id"], "FAILED", released=release)
        before = fingerprint(root)
        now[0] = 111.0
        assert store.available() is False and store.quarantined is True
        assert fingerprint(root) == before and len(store.records) == 1
    finally:
        store.close()


def test_remove_defensive_guard_preserves_unreleased_checkpoint(tmp_path):
    root = tmp_path / "requests"
    store = journal.RequestJournal(root, clock=lambda: 100.0)
    body = request()
    try:
        store.admit("/extract", body, SimpleNamespace(duration=30))
        store.checkpoint(body["request_id"], body["admission_id"], {"public_raw": True}, page=1)
        store.finish(body["request_id"], body["admission_id"], "FAILED", released=False)
        before = fingerprint(root)
        with store.lock, pytest.raises(ValueError, match="REQUEST_JOURNAL_UNAVAILABLE"):
            store._remove(store.records[body["request_id"]])
        assert fingerprint(root) == before and store.quarantined is True
    finally:
        store.close()


@pytest.mark.parametrize("status", sorted(journal.TERMINAL))
def test_released_true_terminal_retains_ordinary_ttl_expiry(tmp_path, status):
    now = [100.0]
    root = tmp_path / "requests"
    store = journal.RequestJournal(root, clock=lambda: now[0], ttl_seconds=10)
    body = request()
    try:
        store.admit("/extract", body, SimpleNamespace(duration=30))
        store.checkpoint(body["request_id"], body["admission_id"], {"public_raw": True}, page=1)
        store.finish(body["request_id"], body["admission_id"], status, released=True)
    finally:
        store.close()
    reopened = journal.RequestJournal(root, clock=lambda: now[0], ttl_seconds=10)
    try:
        assert reopened.available() is True and reopened.get(body["request_id"]) is not None
        now[0] = 111.0
        assert reopened.get(body["request_id"]) is None and reopened.available() is True
        assert not list(root.glob("*.data")) and not (root / "journal.quarantine").exists()
    finally:
        reopened.close()


def test_released_complete_result_recovery_and_duplicate_binding_unchanged(tmp_path):
    now = [100.0]
    store = journal.RequestJournal(tmp_path / "requests", clock=lambda: now[0], ttl_seconds=10)
    body = request()
    value = {"public_complete_result": True}
    receipt = store.admit("/extract", body, SimpleNamespace(duration=30))
    try:
        store.finish(body["request_id"], body["admission_id"], "COMPLETE", released=True, result=value)
        assert store.recover(recovery(receipt))["result"] == value
        with pytest.raises(ValueError, match="DUPLICATE_REQUEST"):
            store.admit("/extract", body, SimpleNamespace(duration=30))
        assert store.available() is True
    finally:
        store.close()
