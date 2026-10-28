"""Normal-flow source caching with explicit native-reader doubles, never model data."""
import copy
from datetime import timedelta

import pytest
from sqlalchemy import select
from test_document_lossless_lifecycle_098 import native, setup_document

from trackvance.db import utcnow
from trackvance.document_models import DocumentOriginal
from trackvance.document_normalization import canonical_hash
from trackvance.document_providers import EngineConfiguration, freeze_configuration
from trackvance.document_recognition_compatibility import approve_recognition_reuse
from trackvance.document_recognition_store import (
    latest_recognition_events,
    persist_recognition_event,
    recognition_journal,
    reusable_recognition_source,
)
from trackvance.document_worker import process_once
from trackvance.models import Job
from trackvance.operations_common import OperationError


def ready_source(client, database, tmp_path, monkeypatch):
    import trackvance.document_worker as worker

    source, schema = setup_document(client, tmp_path)
    settings = EngineConfiguration(provider="PADDLE_QWEN_LOCAL", extraction_strategy="NATIVE_FIRST",
        automatic_verification="BOUNDED_LOCAL_V1", timeout_seconds=600, context_tokens=32768).model_dump()
    response = client.put(f"/api/v1/documents/engines?family_id={source['family_id']}", json={
        "expected_version": 1, "configuration": settings, "reason": "Explicit synthetic source-cache policy fixture"})
    assert response.status_code == 200, response.text
    calls = []
    monkeypatch.setattr(worker, "run_engine", lambda *_args, **_kwargs: calls.append("native") or native(schema))
    assert process_once(owner="source-owner")
    first = client.get(f"/api/v1/documents/originals/{source['id']}").json()
    assert first["status"] == "AUTO_VERIFIED", first.get("issues")
    with database() as db:
        original = db.get(DocumentOriginal, source["id"])
        before = copy.deepcopy(latest_recognition_events(db, original, hydrate=True))
    return first, schema, calls, before


def upload_same_source(client, source, tmp_path, *, example=True):
    response = client.post(f"/api/v1/documents/families/{source['family_id']}/originals",
        data={"template_id": source["template_id"], "is_example": str(example).lower()},
        files=[("files", ("synthetic.pdf", (tmp_path / "synthetic.pdf").read_bytes(), "application/pdf"))])
    assert response.status_code == 200, response.text
    return response.json()["items"][0]


def receipt(db, target, schema):
    frozen = freeze_configuration(db, target)
    cached = reusable_recognition_source(db, target, frozen, schema)
    assert cached
    job = db.scalar(select(Job).where(Job.document_id == target.id))
    value = {"version": 1, "kind": "PHASE", "stage": "RECOVERY_REUSE", "sequence": 0,
        "request_id": "cache-receipt", "attempt_id": job.id + "-1", "source_sha256": target.sha256,
        "configuration_sha256": canonical_hash(frozen), "definition_sha256": canonical_hash(schema),
        "page_one_based": None, "expected_pages": 1,
        "payload": {key: cached[key] for key in ("recognition_bindings", "source_original_id",
            "source_administrative_event_hashes")}}
    value["payload"].update(reused_event_hashes=[event["event_sha256"] for event in cached["events"]],
        reused_attempt_ids=list(dict.fromkeys(event["attempt_id"] for event in cached["events"])))
    value["event_sha256"] = canonical_hash(value)
    persist_recognition_event(db, target, value, attempt_id=value["attempt_id"],
        configuration_sha256=value["configuration_sha256"], job_id=job.id, worker_attempt_number=1)
    db.commit()
    return frozen, cached, value


def test_active_operational_document_reuses_exact_saved_source_then_publishes_normally(
        authenticated, database, tmp_path, monkeypatch):
    source, _schema, calls, before = ready_source(authenticated, database, tmp_path, monkeypatch)
    activated = authenticated.post(
        f"/api/v1/documents/families/{source['family_id']}/templates/{source['template_id']}/activate",
        json={"test_extraction_id": source["extraction"]["id"]})
    assert activated.status_code == 200, activated.text
    target = upload_same_source(authenticated, source, tmp_path, example=False)
    assert process_once(owner="operational-cache-owner")
    response = authenticated.get(f"/api/v1/documents/originals/{target['id']}")
    assert response.status_code == 200, response.text
    current = response.json()
    assert calls == ["native"]
    assert current["is_example"] is False and current["duplicate_of_id"] is None
    assert current["status"] == "PUBLISHED", current.get("issues")
    assert current["quality"]["status"] == "AUTO_VERIFIED"
    assert current["extraction"]["reviewed"] is False
    assert current["extraction"]["engine"]["recovered_from_original_id"] == source["id"]
    assert current["extraction"]["engine"]["verification"]["recognition_calls"] == []
    assert current["extraction"]["engine"]["verification"]["primary_reused"] is True
    with database() as db:
        original = db.get(DocumentOriginal, source["id"])
        assert latest_recognition_events(db, original, hydrate=True) == before
        target_original = db.get(DocumentOriginal, target["id"])
        loaded = latest_recognition_events(db, target_original, hydrate=True)
        hashes = {event["event_sha256"] for event in before if event["kind"] in {"PAGE", "TERMINAL"}}
        assert hashes <= {event["event_sha256"] for event in loaded}
        own = recognition_journal(db, target_original)
        assert not any(row["event"]["kind"] == "PAGE" for row in own)
    source_raw = next(item for item in current["recognition_artifacts"] if item["kind"] == "NATIVE_PAGE_JSON")
    downloaded = authenticated.get(
        f"/api/v1/documents/originals/{target['id']}/recognition-artifacts/{source_raw['artifact_id']}/content")
    assert downloaded.status_code == 200


@pytest.mark.parametrize("mismatch", ["organization", "family", "source", "newer"])
def test_peer_receipt_revalidates_ownership_source_and_age_at_every_read(
        authenticated, database, tmp_path, monkeypatch, mismatch):
    source, schema, _calls, _before = ready_source(authenticated, database, tmp_path, monkeypatch)
    target = upload_same_source(authenticated, source, tmp_path)
    other_family = None
    if mismatch == "family":
        other_family = authenticated.post("/api/v1/documents/families", json={"name": "Different owner family"}).json()["id"]
    with database() as db:
        current, previous = db.get(DocumentOriginal, target["id"]), db.get(DocumentOriginal, source["id"])
        configuration, _cached, _value = receipt(db, current, schema)
        if mismatch == "organization":
            previous.organization_id = "different-organization"
        elif mismatch == "family":
            previous.family_id = other_family
        elif mismatch == "source":
            previous.sha256 = "f" * 64
        else:
            previous.created_at = utcnow() + timedelta(seconds=1)
        db.commit()
        assert reusable_recognition_source(db, current, configuration, schema) is None
        with pytest.raises(OperationError, match="DOCUMENT_RECOGNITION_INTEGRITY"):
            latest_recognition_events(db, current, hydrate=True)


def test_incompatible_new_configuration_and_closed_policy_never_use_source_cache(
        authenticated, database, tmp_path, monkeypatch):
    source, schema, _calls, _before = ready_source(authenticated, database, tmp_path, monkeypatch)
    target = upload_same_source(authenticated, source, tmp_path)
    with database() as db:
        current = db.get(DocumentOriginal, target["id"])
        configuration = freeze_configuration(db, current)
        assert reusable_recognition_source(db, current, configuration, schema)
        assert reusable_recognition_source(db, current, {**configuration, "prompt_version": "trackvance-idp-098-v1"}, schema) is None
        assert reusable_recognition_source(db, current, {**configuration, "automatic_verification": "OFF"}, schema) is None


def test_cache_never_chains_a_source_from_another_original(authenticated, database, tmp_path, monkeypatch):
    source, schema, _calls, _before = ready_source(authenticated, database, tmp_path, monkeypatch)
    middle = upload_same_source(authenticated, source, tmp_path)
    with database() as db:
        original = db.get(DocumentOriginal, middle["id"])
        _configuration, _cached, _value = receipt(db, original, schema)
        # An existing peer link cannot become the next source, even if this
        # disposable fixture labels its lifecycle complete.
        original.status, original.extraction_id = "EXTRACTED", source["extraction"]["id"]
        job = db.scalar(select(Job).where(Job.document_id == original.id))
        job.status = "SUCCESS"
        db.get(DocumentOriginal, source["id"]).cancel_requested = True
        db.commit()
    target = upload_same_source(authenticated, source, tmp_path)
    with database() as db:
        original = db.get(DocumentOriginal, target["id"])
        configuration = freeze_configuration(db, original)
        assert reusable_recognition_source(db, original, configuration, schema) is None


@pytest.mark.parametrize("status,released", [("PARTIAL", True), ("COMPLETE", False)])
def test_incomplete_or_unreleased_source_never_admits_a_cache_read(
        authenticated, database, tmp_path, monkeypatch, status, released):
    source, schema, _calls, before = ready_source(authenticated, database, tmp_path, monkeypatch)
    with database() as db:
        original = db.get(DocumentOriginal, source["id"])
        job = db.scalar(select(Job).where(Job.document_id == original.id))
        terminal = copy.deepcopy(next(event for event in before if event["kind"] == "TERMINAL"))
        terminal.pop("durable_refs")
        terminal.update(sequence=99, status=status, resources_released=released)
        terminal["event_sha256"] = canonical_hash({key: value for key, value in terminal.items() if key != "event_sha256"})
        persist_recognition_event(db, original, terminal, attempt_id=terminal["attempt_id"],
            configuration_sha256=terminal["configuration_sha256"], job_id=job.id, worker_attempt_number=1)
        db.commit()
    target = upload_same_source(authenticated, source, tmp_path)
    with database() as db:
        original = db.get(DocumentOriginal, target["id"])
        configuration = freeze_configuration(db, original)
        assert reusable_recognition_source(db, original, configuration, schema) is None


def test_self_verification_receipt_is_flattened_to_original_model_events_with_admin_trace(
        authenticated, database, tmp_path, monkeypatch):
    source, schema, _calls, before = ready_source(authenticated, database, tmp_path, monkeypatch)
    with database() as db:
        previous = db.get(DocumentOriginal, source["id"])
        configuration = previous.engine_snapshot
        bindings = approve_recognition_reuse(before, source_sha256=previous.sha256,
            configuration=configuration, definition=schema,
            configuration_snapshots={canonical_hash(configuration): configuration},
            definition_snapshots={canonical_hash(schema): schema})
        job = db.scalar(select(Job).where(Job.document_id == previous.id))
        # Explicit storage fixture of an administrative SELF link. It supplies
        # no approval authority and does not call or simulate a model result.
        link = {"version": 1, "kind": "PHASE", "stage": "VERIFICATION_REQUEST", "sequence": 0,
            "request_id": "self-request", "attempt_id": job.id + "-2", "source_sha256": previous.sha256,
            "configuration_sha256": canonical_hash(configuration), "definition_sha256": canonical_hash(schema),
            "page_one_based": None, "expected_pages": 1, "payload": {
                "recognition_bindings": bindings,
                "reused_event_hashes": [event["event_sha256"] for event in before],
                "reused_attempt_ids": list(dict.fromkeys(event["attempt_id"] for event in before))}}
        link["event_sha256"] = canonical_hash(link)
        persist_recognition_event(db, previous, link, attempt_id=link["attempt_id"],
            configuration_sha256=link["configuration_sha256"], job_id=job.id, worker_attempt_number=2)
        db.commit()
    target = upload_same_source(authenticated, source, tmp_path)
    with database() as db:
        original = db.get(DocumentOriginal, target["id"])
        _configuration, cached, _value = receipt(db, original, schema)
        assert link["event_sha256"] in cached["source_administrative_event_hashes"]
        assert not any(event["stage"] in {"VERIFICATION_REQUEST", "RECOVERY_REUSE", "AUTOMATIC_VERIFICATION"}
            for event in cached["events"])
        restored = latest_recognition_events(db, original, hydrate=True)
        source_hashes = {event["event_sha256"] for event in before if event["kind"] in {"PAGE", "TERMINAL"}}
        assert source_hashes <= {event["event_sha256"] for event in restored}
