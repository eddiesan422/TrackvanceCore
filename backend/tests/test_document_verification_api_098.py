"""Native verify queue/worker boundaries with explicitly synthetic saved events.

The active family is SYNTHETIC_SETUP in the disposable database. It is not a
claim of native template acceptance or OCR accuracy; no model/HTTP service runs.
"""
import base64
import copy
import hashlib
import json
from datetime import timedelta

import pytest
from sqlalchemy import func, select
from test_document_lossless_098 import artifact, definition
from test_document_recognition_compatibility_098 import bundle

from trackvance.db import utcnow
from trackvance.document_lossless import adapt_saved_recognition
from trackvance.document_models import DocumentExtraction, DocumentFamily, DocumentOriginal
from trackvance.document_normalization import canonical_hash
from trackvance.document_providers import EngineConfiguration
from trackvance.document_recognition_store import latest_recognition_events, recognition_journal
from trackvance.document_worker import process_once
from trackvance.models import AuditEvent, Dataset, Job, Role, RolePermission, User


def snapshot(row):
    return {column.name: copy.deepcopy(getattr(row, column.name)) for column in row.__table__.columns}


def fixtures(source, schema, frozen, context, family, *, released=True):
    """Retarget the typed source-event fixture, retaining actual request fields."""
    result = bundle(frozen, schema, family)
    source_sha = hashlib.sha256(source.read_bytes()).hexdigest()
    for index, event in enumerate(result):
        event.update(source_sha256=source_sha, attempt_id=context["attempt_id"],
            request_id=f"synthetic-{family.lower()}-{context['attempt_id']}", sequence=index)
        for number, value in enumerate(event["payload"].get("artifacts", [])):
            if value["kind"] == "QWEN_REQUEST_JSON":
                request = json.loads(base64.b64decode(value["content_b64"]))
                request["source_sha256"] = source_sha
                event["payload"]["artifacts"][number] = artifact("QWEN_REQUEST_JSON", request)
        event["payload"]["synthetic_transport_fixture"] = True
        if event["kind"] == "TERMINAL":
            event["resources_released"] = released
        event["event_sha256"] = canonical_hash({key: value for key, value in event.items() if key != "event_sha256"})
    return result


def prepare(authenticated, database, tmp_path, monkeypatch, *, primary="PADDLE", released=True, policy=True):
    import trackvance.document_worker as worker

    schema = definition(critical=True)
    if not policy:
        schema.pop("validation_policy", None)
    response = authenticated.post("/api/v1/documents/families", json={"name": "Synthetic bounded verification"})
    assert response.status_code == 200, response.text
    family = response.json()
    response = authenticated.post(f"/api/v1/documents/families/{family['id']}/templates", json=schema)
    assert response.status_code == 200, response.text
    template = response.json()
    # SYNTHETIC_SETUP: an active, non-example API subject without fabricating a
    # human review or claiming this fixture as the native activation E2E.
    with database() as db:
        db.get(DocumentFamily, family["id"]).active_template_id = template["id"]
        db.commit()
    old_config = EngineConfiguration(provider="PADDLE_QWEN_LOCAL", extraction_strategy=(
        "PADDLE_STRUCTURED" if primary == "PADDLE" else "QWEN_VISUAL"), use_llm=primary == "QWEN").model_dump()
    response = authenticated.put(f"/api/v1/documents/engines?family_id={family['id']}", json={
        "expected_version": 0, "configuration": old_config, "reason": "Synthetic initial frozen attempt"})
    assert response.status_code == 200, response.text
    source = tmp_path / "synthetic-verification.pdf"
    source.write_bytes(b"%PDF-1.4\nSynthetic fixture; no model evidence.\n%%EOF")
    response = authenticated.post(f"/api/v1/documents/families/{family['id']}/originals",
        data={"template_id": template["id"], "is_example": "false"},
        files=[("files", (source.name, source.read_bytes(), "application/pdf"))])
    assert response.status_code == 200, response.text
    original = response.json()["items"][0]

    def forbidden(*_args, **_kwargs):
        raise AssertionError("The saved primary/probe must not be read or inferred again")

    monkeypatch.setattr(worker, "run_engine", forbidden)
    def initial_provider(path, current_definition, frozen, **kwargs):
        values = fixtures(path, current_definition, frozen, kwargs["attempt_context"], primary, released=released)
        for event in values:
            event["durable_refs"] = kwargs["on_event"](event)
        return adapt_saved_recognition(values, current_definition, frozen,
            source_sha256=hashlib.sha256(path.read_bytes()).hexdigest())

    with monkeypatch.context() as initial:
        initial.setattr(worker, "execute_provider", initial_provider)
        assert process_once(owner="synthetic-primary-owner")
    current = authenticated.get(f"/api/v1/documents/originals/{original['id']}").json()
    assert current["status"] == "REVIEW_REQUIRED" and current["extraction"]["reviewed"] is False
    with database() as db:
        item = db.get(DocumentOriginal, original["id"])
        first = db.get(DocumentExtraction, item.extraction_id)
        baseline = {"engine_snapshot": copy.deepcopy(item.engine_snapshot),
            "engine_snapshot_hash": item.engine_snapshot_hash, "old_extraction": snapshot(first),
            "event_hashes": [row["event"]["event_sha256"] for row in recognition_journal(db, item)]}
    return {"id": original["id"], "family": family["id"], "first": current["extraction"]["id"],
        "schema": schema, "old_config": old_config, "baseline": baseline}


def configure(client, item, **changes):
    config = {**item["old_config"], "automatic_verification": "BOUNDED_LOCAL_V1", "use_llm": True, **changes}
    response = client.put(f"/api/v1/documents/engines?family_id={item['family']}", json={
        "expected_version": 1, "configuration": config, "reason": "Synthetic explicit bounded verification configuration"})
    assert response.status_code == 200, response.text
    return config


def verify(client, item, **changes):
    return client.post(f"/api/v1/documents/originals/{item['id']}/verify", json={
        "expected_extraction_id": item["first"], "expected_settings_version": 2,
        "reason": "Synthetic native verification; no human review", **changes})


def test_verification_requires_declared_validation_policy_before_queue(authenticated, database, tmp_path, monkeypatch):
    item = prepare(authenticated, database, tmp_path, monkeypatch, policy=False)
    configure(authenticated, item)
    current = authenticated.get(f"/api/v1/documents/originals/{item['id']}").json()
    assert current["verification_action"]["available"] is False
    assert "política de validación" in current["verification_action"]["blocked_reason"]
    response = verify(authenticated, item)
    assert response.status_code == 409, response.text
    assert "política de validación" in response.text
    assert_preserved(database, item)
    with database() as db:
        assert not any(row["event"].get("stage") == "VERIFICATION_REQUEST"
            for row in recognition_journal(db, db.get(DocumentOriginal, item["id"])))
        assert db.scalar(select(Job).where(Job.document_id == item["id"])).status == "SUCCESS"


def assert_preserved(database, item, *, extraction_count=1):
    with database() as db:
        original = db.get(DocumentOriginal, item["id"])
        assert original.engine_snapshot == item["baseline"]["engine_snapshot"]
        assert original.engine_snapshot_hash == item["baseline"]["engine_snapshot_hash"]
        assert snapshot(db.get(DocumentExtraction, item["first"])) == item["baseline"]["old_extraction"]
        assert db.scalar(select(func.count()).select_from(DocumentExtraction)) == extraction_count
        hashes = {row["event"]["event_sha256"] for row in recognition_journal(db, original)}
        assert set(item["baseline"]["event_hashes"]) <= hashes
        assert original.is_example is False


def install_control(monkeypatch, calls, *, intervene=None):
    import trackvance.document_recognition_transport as transport

    def control(source, schema, frozen, *, strategy, on_event, attempt_context, check, **_kwargs):
        assert strategy == "QWEN_VISUAL", "A stored Paddle primary permits only the missing Qwen control"
        calls.append(strategy)
        values = [event for event in fixtures(source, schema, frozen, attempt_context, "QWEN")
            if event["stage"] != "NATIVE_SOURCE_PROBE"]
        for event in values:
            if intervene:
                intervene(event)
            on_event(event)
        check(95)
        return {"pages": [event for event in values if event["kind"] == "PAGE"], "terminal": values[-1]}

    monkeypatch.setattr(transport, "recognize_stream", control)


def test_verify_native_queue_freezes_new_settings_and_is_idempotent(authenticated, database, tmp_path, monkeypatch):
    item = prepare(authenticated, database, tmp_path, monkeypatch)
    configure(authenticated, item)
    first, repeated = verify(authenticated, item), verify(authenticated, item)
    assert first.status_code == repeated.status_code == 200, (first.text, repeated.text)
    assert first.json()["verification_queue"]["idempotent"] is False
    assert repeated.json()["verification_queue"]["idempotent"] is True
    assert first.json()["verification_queue"]["attempt_id"] == repeated.json()["verification_queue"]["attempt_id"]
    assert first.json()["status"] == "QUEUED" and first.json()["extraction_id"] == item["first"]
    with database() as db:
        original = db.get(DocumentOriginal, item["id"])
        rows = recognition_journal(db, original)
        requests = [row for row in rows if row["event"]["stage"] == "VERIFICATION_REQUEST"]
        assert len(requests) == 1
        request = requests[0]["event"]
        assert request["payload"]["configuration"]["use_llm"] is True
        assert request["payload"]["configuration"]["configuration_version"] == 2
        assert request["configuration_sha256"] != original.engine_snapshot_hash
        assert db.scalar(select(Job).where(Job.document_id == original.id)).attempts == 1
        assert db.scalar(select(func.count()).select_from(AuditEvent).where(AuditEvent.event_type == "DOCUMENT_VERIFICATION_QUEUED")) == 1
    assert_preserved(database, item)


def test_verify_worker_reuses_primary_and_appends_unreviewed_revision_once(authenticated, database, tmp_path, monkeypatch):
    item = prepare(authenticated, database, tmp_path, monkeypatch)
    configure(authenticated, item)
    calls = []
    install_control(monkeypatch, calls)
    queued = verify(authenticated, item)
    assert queued.status_code == 200, queued.text
    assert process_once(owner="synthetic-verification-owner")
    current = authenticated.get(f"/api/v1/documents/originals/{item['id']}").json()
    assert calls == ["QWEN_VISUAL"]
    assert current["extraction"]["revision"] == 2 and current["extraction"]["previous_id"] == item["first"]
    assert current["extraction"]["reviewed"] is False
    assert current["extraction"]["payload"]["fields"]["reference"]["value"] == "R-001"
    assert current["quality"]["status"] == "AUTO_VERIFIED", current.get("quality")
    engine = current["extraction"]["engine"]
    assert engine["verification_request"]["base_configuration_sha256"] == item["baseline"]["engine_snapshot_hash"]
    assert engine["verification"]["primary_reused"] is True
    assert len(engine["recognition_calls"]) == 1
    assert engine["recognition_sources"]["PADDLE_STRUCTURED"]["configuration_sha256"] == item["baseline"]["engine_snapshot_hash"]
    assert engine["recognition_sources"]["QWEN_VISUAL"]["configuration_sha256"] != item["baseline"]["engine_snapshot_hash"]
    assert_preserved(database, item, extraction_count=2)
    with database() as db:
        original = db.get(DocumentOriginal, item["id"])
        before = len(recognition_journal(db, original))
    repeated = verify(authenticated, item)
    assert repeated.status_code == 200 and repeated.json()["verification_queue"]["idempotent"] is True
    with database() as db:
        assert len(recognition_journal(db, db.get(DocumentOriginal, item["id"]))) == before
    assert calls == ["QWEN_VISUAL"]


@pytest.mark.parametrize("changes,code", [({"expected_extraction_id": "stale-extraction"}, "DOCUMENT_REVIEW_CONFLICT"),
    ({"expected_settings_version": 1}, "DOCUMENT_ENGINE_CONFIGURATION_CONFLICT")])
def test_verify_rejects_stale_fences_without_a_request(authenticated, database, tmp_path, monkeypatch, changes, code):
    item = prepare(authenticated, database, tmp_path, monkeypatch)
    configure(authenticated, item)
    response = verify(authenticated, item, **changes)
    assert response.status_code == 409 and response.json()["error"]["code"] == code
    assert_preserved(database, item)


@pytest.mark.parametrize("changes,code", [({"automatic_verification": "OFF"}, "DOCUMENT_VERIFICATION_DISABLED"),
    ({"use_llm": False}, "DOCUMENT_VERIFICATION_NOT_ACTIONABLE")])
def test_verify_requires_an_actionable_explicit_configuration(authenticated, database, tmp_path, monkeypatch, changes, code):
    item = prepare(authenticated, database, tmp_path, monkeypatch)
    configure(authenticated, item, **changes)
    response = verify(authenticated, item)
    assert response.status_code == 409 and response.json()["error"]["code"] == code
    assert_preserved(database, item)


def test_verify_hides_foreign_organization_subject(authenticated, database, tmp_path, monkeypatch):
    item = prepare(authenticated, database, tmp_path, monkeypatch)
    configure(authenticated, item)
    with database() as db:
        db.get(DocumentOriginal, item["id"]).organization_id = "other-synthetic-organization"
        db.commit()
    response = verify(authenticated, item)
    assert response.status_code == 404 and response.json()["error"]["code"] == "DOCUMENT_NOT_FOUND"


def test_verify_enforces_review_permission(authenticated, database, tmp_path, monkeypatch):
    item = prepare(authenticated, database, tmp_path, monkeypatch)
    configure(authenticated, item)
    with database() as db:
        user = db.get(User, "test-user")
        role = Role(name="Synthetic observer", normalized_name="synthetic observer", organization_id=user.organization_id)
        db.add(role)
        db.flush()
        db.add_all([RolePermission(role_id=role.id, permission_code=permission)
            for permission in ("documents:read", "documents:write")])
        user.role_id, user.role = role.id, role.name
        db.commit()
    response = verify(authenticated, item)
    assert response.status_code == 403


def test_verify_rejects_unreleased_original_resources_without_new_attempt(authenticated, database, tmp_path, monkeypatch):
    item = prepare(authenticated, database, tmp_path, monkeypatch, released=False)
    configure(authenticated, item)
    response = verify(authenticated, item)
    assert response.status_code == 409 and response.json()["error"]["code"] == "DOCUMENT_VERIFICATION_REUSE_UNAVAILABLE"
    assert_preserved(database, item)


def test_verify_rejects_changed_actual_qwen_input_context(authenticated, database, tmp_path, monkeypatch):
    item = prepare(authenticated, database, tmp_path, monkeypatch, primary="QWEN")
    changed_context = 16384 if item["old_config"]["context_tokens"] != 16384 else 8192
    assert changed_context != item["baseline"]["engine_snapshot"]["context_tokens"]
    configure(authenticated, item, context_tokens=changed_context)
    response = verify(authenticated, item)
    assert response.status_code == 409 and response.json()["error"]["code"] == "DOCUMENT_VERIFICATION_REUSE_UNAVAILABLE"
    assert_preserved(database, item)


def test_cancelled_verification_preserves_close_and_old_revision(authenticated, database, tmp_path, monkeypatch):
    item = prepare(authenticated, database, tmp_path, monkeypatch)
    configure(authenticated, item)
    calls = []
    def cancel_after_page(event):
        if event["kind"] == "TERMINAL":
            response = authenticated.post(f"/api/v1/documents/originals/{item['id']}/cancel")
            assert response.status_code == 200, response.text
    install_control(monkeypatch, calls, intervene=cancel_after_page)
    assert verify(authenticated, item).status_code == 200
    assert process_once(owner="synthetic-cancel-owner")
    assert calls == ["QWEN_VISUAL"]
    assert_preserved(database, item)
    with database() as db:
        original = db.get(DocumentOriginal, item["id"])
        assert original.status == "CANCELLED" and original.extraction_id == item["first"]
        events = latest_recognition_events(db, original, hydrate=True)
        assert any(event["kind"] == "TERMINAL" and event["stage"] == "QWEN_VISUAL"
            and event["resources_released"] is True for event in events)
        assert db.scalar(select(func.count()).select_from(Dataset)) == 0


def test_superseded_verification_cannot_append_late_control_page(authenticated, database, tmp_path, monkeypatch):
    item = prepare(authenticated, database, tmp_path, monkeypatch)
    configure(authenticated, item)
    calls = []
    def supersede(event):
        if event["kind"] == "PAGE":
            with database() as db:
                job = db.scalar(select(Job).where(Job.document_id == item["id"]))
                job.attempts += 1
                job.lease_owner, job.lease_until = "synthetic-successor", utcnow() + timedelta(minutes=1)
                db.commit()
    install_control(monkeypatch, calls, intervene=supersede)
    assert verify(authenticated, item).status_code == 200
    assert process_once(owner="synthetic-late-owner")
    assert calls == ["QWEN_VISUAL"]
    assert_preserved(database, item)
    with database() as db:
        original = db.get(DocumentOriginal, item["id"])
        job = db.scalar(select(Job).where(Job.document_id == item["id"]))
        assert original.extraction_id == item["first"] and job.lease_owner == "synthetic-successor"
        assert job.status == "RUNNING" and job.attempts == 3
        assert not any(event["kind"] == "PAGE" and event["stage"] == "QWEN_VISUAL"
            for event in latest_recognition_events(db, original, hydrate=True))
        assert db.scalar(select(func.count()).select_from(Dataset)) == 0
