"""Real worker/store/API fencing with explicitly synthetic transport events."""
import base64
import copy
import hashlib
import json
from datetime import timedelta

import pytest
from sqlalchemy import func, select
from test_document_lossless_lifecycle_098 import native, setup_document

from trackvance.db import utcnow
from trackvance.document_inference_completion import canonical_hash
from trackvance.document_models import (
    DocumentExtraction,
    DocumentFamily,
    DocumentOriginal,
    DocumentTemplate,
)
from trackvance.document_recognition_store import latest_recognition_events
from trackvance.document_service import open_batch
from trackvance.document_worker import process_once
from trackvance.models import Dataset, Job, User
from trackvance.operations_common import OperationError


def synthetic_events(source, definition, configuration, context, *, status="COMPLETE", released=True):
    """No model output: a labelled synthetic structured recognition contract."""
    selection = {"fields": {"color": {"observed": "blue", "ambiguous": False, "alternatives": []}}, "tables": {}}
    raw = json.dumps(selection).encode()
    binding = {"version": 1, "request_id": "a" * 32, "attempt_id": context["attempt_id"],
        "source_sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
        "configuration_sha256": canonical_hash(configuration), "definition_sha256": canonical_hash(definition),
        "stage": "QWEN_VISUAL", "expected_pages": 1}
    page = {**binding, "kind": "PAGE", "sequence": 1, "page_one_based": 1,
        "payload": {"artifacts": [{"kind": "QWEN_SELECTION_JSON", "media_type": "application/json",
            "sha256": hashlib.sha256(raw).hexdigest(), "bytes": len(raw), "content_b64": base64.b64encode(raw).decode()}],
            "page_geometry": {"width": 600, "height": 800, "unit": "PDF_POINT"},
            "engine": {"synthetic_transport_fixture": True}}}
    terminal = {**binding, "kind": "TERMINAL", "sequence": 2, "page_one_based": None,
        "status": status, "resources_released": released, "emitted_pages": [1],
        "completed_pages": [1] if status == "COMPLETE" else [],
        "pending_pages": [] if status == "COMPLETE" else [1], "payload": {"engine": {"synthetic_transport_fixture": True}}}
    for event in (page, terminal):
        event["event_sha256"] = canonical_hash(event)
    return page, terminal


def require_recognition(monkeypatch, definition):
    import trackvance.document_worker as worker

    def insufficient(*_args, **_kwargs):
        result = native(definition)
        result["payload"]["native_incomplete_pages"] = [1]
        return result
    monkeypatch.setattr(worker, "run_engine", insufficient)


def assert_native_publication_blocked(authenticated, database, original_id):
    # The reusable fixture uploads a configuration example. This isolated
    # publication setup explicitly makes that synthetic row operational before
    # reserving it through the native composition service.
    with database() as db:
        item = db.get(DocumentOriginal, original_id)
        item.is_example = False
        batch = open_batch(db, db.get(DocumentFamily, item.family_id), db.get(DocumentTemplate, item.template_id),
            db.get(User, item.uploaded_by_id), "synthetic-authority-publication", document_ids=[item.id])
        batch.wait_until = utcnow() - timedelta(seconds=1)
        db.commit()
        batch_id = batch.id
    response = authenticated.post(f"/api/v1/documents/batches/{batch_id}/publish")
    assert response.status_code == 200, response.text
    assert response.json()["status"] == "BLOCKED"
    assert not response.json()["published_versions"]
    with database() as db:
        assert db.scalar(select(func.count()).select_from(Dataset)) == 0


@pytest.mark.parametrize("remote_status", ["CANCELLED", "COMPLETE"])
def test_cancel_after_page_keeps_terminal_but_never_creates_extraction_or_dataset(
        authenticated, database, tmp_path, monkeypatch, remote_status):
    import trackvance.document_recognition_transport as transport

    original, definition = setup_document(authenticated, tmp_path)
    require_recognition(monkeypatch, definition)
    saved = []
    def recognition(source, schema, configuration, *, strategy, check, budget, on_event, attempt_context):
        assert strategy == "QWEN_VISUAL" and budget is not None
        page, terminal = synthetic_events(source, schema, configuration, attempt_context, status=remote_status)
        on_event(page)
        saved.append(copy.deepcopy(page))
        response = authenticated.post(f"/api/v1/documents/originals/{original['id']}/cancel")
        assert response.status_code == 200, response.text
        # The production transport persists the close proof through its cleanup
        # sink even after the work checkpoint rejects cancellation.
        on_event(terminal)
        saved.append(copy.deepcopy(terminal))
        check(95)
        raise AssertionError("A cancelled authority must reject a late completion")
    monkeypatch.setattr(transport, "recognize_stream", recognition)
    assert process_once(owner="cancel-owner")
    with database() as db:
        item = db.get(DocumentOriginal, original["id"])
        events = latest_recognition_events(db, item, hydrate=True)
        assert [event["kind"] for event in events] == ["PHASE", "PAGE", "TERMINAL"]
        assert events[0]["stage"] == "NATIVE_SOURCE_PROBE"
        assert events[-1]["status"] == remote_status and events[-1]["resources_released"] is True
        assert item.status == "CANCELLED" and item.extraction_id is None
        assert db.scalar(select(func.count()).select_from(DocumentExtraction)) == 0
        assert db.scalar(select(func.count()).select_from(Dataset)) == 0
    assert_native_publication_blocked(authenticated, database, original["id"])
    assert len(saved) == 2


def test_superseded_attempt_cannot_append_raw_page_or_overwrite_successor(
        authenticated, database, tmp_path, monkeypatch):
    import trackvance.document_recognition_transport as transport

    original, definition = setup_document(authenticated, tmp_path)
    require_recognition(monkeypatch, definition)
    def recognition(source, schema, configuration, *, strategy, check, budget, on_event, attempt_context):
        page, _ = synthetic_events(source, schema, configuration, attempt_context)
        with database() as db:
            job = db.scalar(select(Job).where(Job.document_id == original["id"]))
            job.lease_owner, job.attempts = "successor-owner", job.attempts + 1
            job.lease_until = utcnow() + timedelta(minutes=1)
            db.commit()
        on_event(page)
        raise AssertionError("A superseded attempt must not accept a page")
    monkeypatch.setattr(transport, "recognize_stream", recognition)
    assert process_once(owner="superseded-owner")
    with database() as db:
        item = db.get(DocumentOriginal, original["id"])
        job = db.scalar(select(Job).where(Job.document_id == item.id))
        events = latest_recognition_events(db, item)
        # The source probe belongs to the authority held before succession.
        # The late model PAGE must add no result or recognition artifact.
        assert len(events) == 1 and events[0]["kind"] == "PHASE"
        assert events[0]["stage"] == "NATIVE_SOURCE_PROBE"
        assert {artifact["kind"] for artifact in events[0]["payload"]["artifacts"]} == {"NATIVE_SOURCE_PROBE_JSON"}
        assert item.extraction_id is None and item.status == "RUNNING"
        assert job.lease_owner == "successor-owner" and job.status == "RUNNING" and job.attempts == 2
        assert db.scalar(select(func.count()).select_from(Dataset)) == 0


def test_saved_visual_results_replay_without_reader_or_model_and_preserve_revision(
        authenticated, database, tmp_path, monkeypatch):
    import trackvance.document_recognition_transport as transport
    import trackvance.document_worker as worker

    original, definition = setup_document(authenticated, tmp_path)
    require_recognition(monkeypatch, definition)
    calls = []
    def recognition(source, schema, configuration, *, strategy, check, budget, on_event, attempt_context):
        calls.append(strategy)
        page, terminal = synthetic_events(source, schema, configuration, attempt_context)
        on_event(page)
        on_event(terminal)
        return {"pages": [page], "terminal": terminal, "engine": {"synthetic_transport_fixture": True}}
    monkeypatch.setattr(transport, "recognize_stream", recognition)
    assert process_once(owner="visual-owner")
    first = authenticated.get(f"/api/v1/documents/originals/{original['id']}").json()["extraction"]
    assert first["payload"]["fields"]["color"]["observed"] == "blue"
    assert calls == ["QWEN_VISUAL"]
    def forbidden(*_args, **_kwargs):
        raise AssertionError("Replay may not invoke any reader or model transport")
    monkeypatch.setattr(worker, "run_engine", forbidden)
    monkeypatch.setattr(transport, "recognize_stream", forbidden)
    response = authenticated.post(f"/api/v1/documents/originals/{original['id']}/replay", json={
        "expected_extraction_id": first["id"], "reason": "Reproducción sintética sin otra inferencia"})
    assert response.status_code == 200, response.text
    replay = response.json()["extraction"]
    assert replay["previous_id"] == first["id"] and replay["revision"] == 2
    assert replay["engine"]["replay"]["recognition_model_calls"] == 0
    with database() as db:
        preserved = db.get(DocumentExtraction, first["id"])
        assert preserved.payload == first["payload"] and preserved.payload_hash == first["sha256"]
        assert db.scalar(select(func.count()).select_from(Dataset)) == 0


def test_unverified_close_survives_replay_as_publication_blocker(
        authenticated, database, tmp_path, monkeypatch):
    import trackvance.document_recognition_transport as transport

    original, definition = setup_document(authenticated, tmp_path)
    require_recognition(monkeypatch, definition)
    def recognition(source, schema, configuration, *, strategy, check, budget, on_event, attempt_context):
        page, terminal = synthetic_events(source, schema, configuration, attempt_context, status="FAILED", released=False)
        on_event(page)
        on_event(terminal)
        raise OperationError(503, "DOCUMENT_LOCAL_CLEANUP_UNVERIFIED", "Synthetic unknown model closure")
    monkeypatch.setattr(transport, "recognize_stream", recognition)
    assert process_once(owner="unknown-close-owner")
    current = authenticated.get(f"/api/v1/documents/originals/{original['id']}").json()
    assert current["status"] == "FAILED" and current["extraction"] is None
    response = authenticated.post(f"/api/v1/documents/originals/{original['id']}/replay", json={
        "expected_extraction_id": None, "reason": "Examinar parcial conservando bloqueo de cierre"})
    assert response.status_code == 200, response.text
    replay = response.json()
    assert replay["status"] == "REVIEW_REQUIRED"
    assert "RECOGNITION_CLOSURE_UNVERIFIED" in replay["issues"]
    assert_native_publication_blocked(authenticated, database, original["id"])
