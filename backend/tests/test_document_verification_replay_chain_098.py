"""Native replays retain the immutable verification configuration proof."""
import copy

from sqlalchemy import func, select
from test_document_verification_api_098 import configure, install_control, prepare, snapshot, verify

from trackvance.document_models import DocumentExtraction, DocumentOriginal
from trackvance.document_recognition_store import recognition_journal
from trackvance.document_worker import process_once
from trackvance.models import Dataset


def test_two_native_replays_after_verification_preserve_proof_and_never_read_models(
        authenticated, database, tmp_path, monkeypatch):
    item = prepare(authenticated, database, tmp_path, monkeypatch)
    configure(authenticated, item)
    calls = []
    install_control(monkeypatch, calls)
    queued = verify(authenticated, item)
    assert queued.status_code == 200, queued.text
    assert process_once(owner="synthetic-replay-chain-verification-owner")
    verified = authenticated.get(f"/api/v1/documents/originals/{item['id']}").json()
    assert calls == ["QWEN_VISUAL"]
    assert verified["quality"]["status"] == "AUTO_VERIFIED"
    expected_proof = copy.deepcopy(verified["extraction"]["engine"]["verification_request"])
    expected_configuration = copy.deepcopy(verified["extraction"]["engine"]["configuration"])
    with database() as db:
        original = db.get(DocumentOriginal, item["id"])
        previous_rows = {row.id: snapshot(row) for row in db.scalars(select(DocumentExtraction))}
        journal_before = copy.deepcopy(recognition_journal(db, original, hydrate=True))
        datasets_before = db.scalar(select(func.count()).select_from(Dataset))
    previous = verified["extraction"]
    revisions = []
    for reason in ("First synthetic replay without models", "Second synthetic replay without models"):
        response = authenticated.post(f"/api/v1/documents/originals/{item['id']}/replay", json={
            "expected_extraction_id": previous["id"], "reason": reason})
        assert response.status_code == 200, {"replay_number": len(revisions) + 1, "response": response.text}
        current = response.json()
        assert current["extraction"]["previous_id"] == previous["id"]
        assert current["extraction"]["revision"] == previous["revision"] + 1
        assert current["extraction"]["reviewed"] is False
        assert current["quality"]["status"] == "AUTO_VERIFIED"
        assert current["extraction"]["engine"]["configuration"] == expected_configuration
        assert current["extraction"]["engine"]["replay"]["recognition_model_calls"] == 0
        verification = current["extraction"]["engine"]["verification"]
        assert verification["replay_recomputed"] is True
        assert verification["recognition_model_calls_new"] == 0
        assert verification["status"] == "CONTROLS_COMPLETED"
        assert verification["remaining_reasons"] == []
        revisions.append(current["extraction"])
        previous = current["extraction"]
    # Check both POSTs before inspecting this inherited proof: the pre-fix
    # regression fails on the second POST with a real source-binding rejection.
    assert all(revision["engine"]["verification_request"] == expected_proof for revision in revisions)
    assert calls == ["QWEN_VISUAL"]
    with database() as db:
        original = db.get(DocumentOriginal, item["id"])
        assert original.engine_snapshot == item["baseline"]["engine_snapshot"]
        assert original.engine_snapshot_hash == item["baseline"]["engine_snapshot_hash"]
        assert recognition_journal(db, original, hydrate=True) == journal_before
        assert all(snapshot(db.get(DocumentExtraction, identity)) == row for identity, row in previous_rows.items())
        assert db.scalar(select(func.count()).select_from(Dataset)) == datasets_before
