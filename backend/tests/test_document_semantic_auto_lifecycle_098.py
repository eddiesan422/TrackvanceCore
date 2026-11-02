"""Synthetic, native API/storage proof tests; no Paddle/Qwen model is invoked."""
import base64
import copy
import hashlib
import json
from pathlib import Path

import pytest
from sqlalchemy import func, select
from test_document_semantic_general_098 import artifact, fixture_events, gateway, profile, selection
from test_document_semantic_general_lifecycle_098 import upload_fixture

from trackvance.document_engine import extract
from trackvance.document_models import DocumentExtraction, DocumentOriginal, DocumentTemplate
from trackvance.document_normalization import canonical_hash
from trackvance.document_worker import process_once
from trackvance.models import DatasetVersion, Job


def activate(client, monkeypatch):
    monkeypatch.setenv("TRACKVANCE_SEMANTIC_GENERAL_ENABLED", "true")
    monkeypatch.setenv("TRACKVANCE_SEMANTIC_AUTO_ENABLED", "true")
    family = client.post("/api/v1/documents/families", json={"name": "Synthetic semantic AUTO lifecycle"}).json()["id"]
    response = client.post(f"/api/v1/documents/families/{family}/semantic-profile", json={
        "profile": profile(), "route": "QWEN_VISUAL", "automatic_verification": "SEMANTIC_AUTO_V1",
        "reason": "Explicit isolated unit fixture for worker-owned verification; no quality measurement"})
    assert response.status_code == 200, response.text
    return family, response.json()["template"]


def digital_reader(monkeypatch):
    import trackvance.document_recognition_transport as transport
    import trackvance.document_worker as worker

    calls = []

    def native(_source, definition, **_kwargs):
        lines = []
        for top, text in [(0.1, "Reference: R-001"), (0.2, "Printed amount: 12.50")]:
            words, offset = [], .1
            for word in text.split():
                width = len(word) * .012
                words.append({"text": word, "region": [offset, top, offset + width, top + .025]})
                offset += width + .008
            lines.append({"page": 1, "text": text, "region": [.1, top, offset, top + .025],
                "method": "DIGITAL", "words": words, "confidence": 100})
        geometry = {"1": {"width": 600, "height": 800, "unit": "PDF_POINT"}}
        payload = extract(lines, definition, geometry)
        payload.update(pages=[1], native_incomplete_pages=[])
        return {"payload": payload, "engine": {"pages": 1, "key": "SYNTHETIC_NATIVE_SOURCE"}}

    def recognize(source, definition, configuration, **kwargs):
        calls.append(kwargs["strategy"])
        assert kwargs["strategy"] == "QWEN_VISUAL", "A complete native source control must avoid another engine"
        items = independent_qwen_events(definition, configuration, hashlib.sha256(source.read_bytes()).hexdigest())
        for number, item in enumerate(items, 1):
            item.pop("event_sha256", None)
            item.update(sequence=number, attempt_id=kwargs["attempt_context"]["attempt_id"])
            item["event_sha256"] = canonical_hash(item)
            kwargs["on_event"](item)
        return {"pages": items[:-1], "terminal": items[-1]}

    monkeypatch.setattr(worker, "run_engine", native)
    monkeypatch.setattr(transport, "recognize_stream", recognize)
    return calls


def independent_qwen_events(definition, configuration, source_sha256, *, observations=None):
    """Capture exact existing image-only request structure, with synthetic bytes."""
    items = fixture_events([selection() if observations is None else observations], definition, configuration, source_sha256)
    page, terminal = [item for item in items if item["stage"] != "NATIVE_SOURCE_PROBE"]
    recognizer = gateway()
    image_bytes = b"synthetic image-only recognition unit input; no actual model call"
    image = {"kind": "PAGE_IMAGE_PNG", "media_type": "image/png", "bytes": len(image_bytes),
        "sha256": hashlib.sha256(image_bytes).hexdigest(), "content_b64": base64.b64encode(image_bytes).decode()}
    schema = recognizer.visual_contract(definition)
    prompt = (Path(__file__).resolve().parents[2] / "deploy/idp/prompt-visual-v1.txt").read_text(encoding="utf-8")
    prompt += "\n" + recognizer.VISUAL_SOURCE_INSTRUCTIONS + "\n" + recognizer.SEMANTIC_GENERAL_LITERAL_INSTRUCTIONS
    prompt += "\n" + recognizer.SEMANTIC_GENERAL_GUARD_INSTRUCTIONS
    body = {"model": configuration["llm_model"], "think": False, "keep_alive": 0, "format": schema,
        "truncate": False, "shift": False, "messages": [{"role": "system", "content": prompt}, {"role": "user",
            "content": json.dumps({"page": 1, "document_pages": 1, "definition": recognizer.semantic_definition(definition),
                "json_schema": schema, "targets": {"fields": [field["name"] for field in definition["fields"]],
                    "tables": [table["name"] for table in definition["tables"]]}}, ensure_ascii=False),
            "images": [image["content_b64"]]}], "options": {"num_ctx": 32768, "num_predict": 8192,
            "temperature": 0, "seed": 98, "presence_penalty": 0, "repeat_penalty": 1, "top_k": 20, "top_p": .95}}
    metadata = recognizer.visual_request_metadata(body, source_sha256=source_sha256,
        definition_sha256=canonical_hash(definition), configuration_sha256=canonical_hash(configuration),
        image=image, page={"page": 1, "page_geometry": page["payload"]["page_geometry"]})
    request = copy.deepcopy(page)
    request.update(kind="PHASE", stage="QWEN_GENERATION", payload={"artifacts": [artifact("QWEN_REQUEST_JSON", metadata)]})
    page["payload"]["artifacts"].insert(0, image)
    return [request, page, terminal]


def test_disabled_auto_activation_writes_no_template(authenticated, database, monkeypatch):
    monkeypatch.setenv("TRACKVANCE_SEMANTIC_GENERAL_ENABLED", "true")
    monkeypatch.delenv("TRACKVANCE_SEMANTIC_AUTO_ENABLED", raising=False)
    family = authenticated.post("/api/v1/documents/families", json={"name": "Disabled AUTO fixture"}).json()["id"]
    response = authenticated.post(f"/api/v1/documents/families/{family}/semantic-profile", json={
        "profile": profile(), "automatic_verification": "SEMANTIC_AUTO_V1", "reason": "Must not silently opt in"})
    assert response.status_code == 409 and response.json()["error"]["code"] == "DOCUMENT_SEMANTIC_AUTO_DISABLED"
    with database() as db:
        assert db.scalar(select(func.count()).select_from(DocumentTemplate)) == 0


def test_persisted_native_evidence_automatically_publishes_without_human_flags(authenticated, database, monkeypatch):
    family, template = activate(authenticated, monkeypatch)
    calls = digital_reader(monkeypatch)
    original = upload_fixture(authenticated, family, label="digital-proof-unit")
    assert process_once("semantic-auto-synthetic-worker")
    response = authenticated.get("/api/v1/documents/originals/" + original["id"])
    assert response.status_code == 200, response.text
    detail = response.json()
    assert calls == ["QWEN_VISUAL"]
    assert detail["status"] == "PUBLISHED", detail["issues"]
    assert detail["quality"]["status"] == "AUTO_VERIFIED"
    assert detail["semantic_review"]["automatic_approval"] is True
    assert all(value["state"] == "VERIFIED_AUTOMATICALLY" for value in detail["semantic_review"]["values"])
    assert not detail["extraction"]["reviewed"]
    assert all(not cell["reviewed"] for cell in detail["extraction"]["payload"]["fields"].values())
    visible_proof = detail["extraction"]["engine"]["validation"]
    assert "recognition_events" not in visible_proof["source_context"]
    assert "automatic_candidate" not in visible_proof
    assert visible_proof["proof_material"]["recognition_event_count"] > 0
    source_artifact = detail["recognition_artifacts"][0]["artifact_id"]
    raw = authenticated.get(f"/api/v1/documents/originals/{original['id']}/recognition-artifacts/{source_artifact}/content")
    assert raw.status_code == 200 and raw.content
    version = detail["publications"][0]["versions"]["headers"]
    dataset = authenticated.get(f"/api/v1/dataset-versions/{version}/profile").json()
    assert dataset["row_count"] == 1
    assert dataset["sample"][0]["document_id"] == original["id"]
    assert dataset["sample"][0]["extraction_id"] == detail["extraction"]["id"]
    before = detail["extraction"]
    replay = authenticated.post(f"/api/v1/documents/originals/{original['id']}/replay", json={
        "expected_extraction_id": before["id"], "reason": "Immutable raw-journal proof replay without models"})
    assert replay.status_code == 200, replay.text
    assert calls == ["QWEN_VISUAL"]
    with database() as db:
        assert db.get(DocumentExtraction, before["id"]).payload == before["payload"]
        persisted_proof = db.get(DocumentExtraction, before["id"]).engine["validation"]
        assert persisted_proof["automatic_candidate"]
        assert persisted_proof["source_context"]["recognition_events"]
        assert db.get(DocumentOriginal, original["id"]).template_id == template["id"]
        assert db.scalar(select(func.count()).select_from(DatasetVersion)) == 1
    import trackvance.document_service as service

    grants = service.effective_permissions
    monkeypatch.setattr(service, "effective_permissions", lambda db, user: set(grants(db, user)) - {"documents:download"})
    denied = authenticated.get(f"/api/v1/documents/originals/{original['id']}/recognition-artifacts/{source_artifact}/content")
    assert denied.status_code == 403


@pytest.mark.parametrize("original_status,job_status", [("QUEUED", "SUCCESS"), ("REVIEW_REQUIRED", "RUNNING")])
def test_auto_review_rejects_an_active_attempt_even_if_extraction_id_matches(authenticated, database, monkeypatch,
                                                                          original_status, job_status):
    family, _template = activate(authenticated, monkeypatch)
    digital_reader(monkeypatch)
    original = upload_fixture(authenticated, family, label="active-proof-unit")
    assert process_once("semantic-auto-active-fixture")
    with database() as db:
        item = db.get(DocumentOriginal, original["id"])
        current_id = item.extraction_id
        item.status = original_status
        job = db.scalar(select(Job).where(Job.document_id == item.id))
        job.status = job_status
        db.commit()
    response = authenticated.post(f"/api/v1/documents/originals/{original['id']}/review", json={
        "expected_extraction_id": current_id, "confirmed": ["headers.0.reference"],
        "reason": "Stale UI cannot review while a worker owns the next attempt"})
    assert response.status_code == 409 and response.json()["error"]["code"] == "DOCUMENT_REVIEW_RUNNING"
