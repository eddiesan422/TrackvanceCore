"""Real coordinator/storage/API transactions with explicit native-reader doubles."""
import copy
from datetime import timedelta

from sqlalchemy import func, select

from trackvance.db import utcnow
from trackvance.document_engine import extract
from trackvance.document_models import DocumentExtraction, DocumentOriginal
from trackvance.document_providers import EngineConfiguration
from trackvance.document_recognition_store import (
    latest_recognition_events,
    persist_recognition_event,
)
from trackvance.document_schemas import DocumentDefinition
from trackvance.document_worker import process_once
from trackvance.models import Dataset, Job


def setup_document(client, tmp_path, *, definition=None):
    definition = DocumentDefinition.model_validate(definition or {"fields": [{"name": "color", "label": "Color", "pattern": "(.+)"}],
        "validation_policy": {"version": "EVIDENCE_V2", "allow_noncritical_source_bound": True},
        "publication": {"review_policy": "AUTO_VERIFIED"}}).model_dump(mode="json")
    family_response = client.post("/api/v1/documents/families", json={"name": "Lossless lifecycle"})
    assert family_response.status_code == 200, family_response.text
    family = family_response.json()
    response = client.post(f"/api/v1/documents/families/{family['id']}/templates", json=definition)
    assert response.status_code == 200, response.text
    template = response.json()
    configuration = EngineConfiguration(provider="PADDLE_QWEN_LOCAL", extraction_strategy="NATIVE_FIRST",
        timeout_seconds=600, context_tokens=32768).model_dump()
    response = client.put(f"/api/v1/documents/engines?family_id={family['id']}", json={"expected_version": 0,
        "configuration": configuration, "reason": "Prueba de coordinación con lector nativo simulado"})
    assert response.status_code == 200, response.text
    source = tmp_path / "synthetic.pdf"
    source.write_bytes(b"%PDF-1.4\nsynthetic unit input, not model evidence\n%%EOF")
    response = client.post(f"/api/v1/documents/families/{family['id']}/originals",
        data={"template_id": template["id"], "is_example": "true"},
        files=[("files", (source.name, source.read_bytes(), "application/pdf"))])
    assert response.status_code == 200, response.text
    return response.json()["items"][0], definition


def native(definition, pages=1, *, observed="blue"):
    lines = [{"text": "Color: " + observed, "page": number, "region": [0.1, 0.1, 0.8, 0.2],
        "method": "DIGITAL", "confidence": None} for number in range(1, pages + 1)]
    payload = extract(lines, definition)
    payload["page_geometry"] = {str(i): {"width": 600, "height": 800, "unit": "PDF_POINT"} for i in range(1, pages + 1)}
    payload["native_incomplete_pages"] = []
    return {"payload": payload, "engine": {"key": "EXPLICIT_TEST_NATIVE_READER_DOUBLE", "pages": pages}}


def test_manual_correction_uses_declared_normalization_and_keeps_source_observation(authenticated, database, tmp_path, monkeypatch):
    import trackvance.document_worker as worker

    original, definition = setup_document(authenticated, tmp_path, definition={"fields": [{"name": "color",
        "label": "Color", "pattern": "(.+)", "normalization": {"currency_equivalences": {"COP": ["pesos colombianos", "COP colombiano"]}}}]})
    monkeypatch.setattr(worker, "run_engine", lambda *_args, **_kwargs: native(definition, observed="pesos colombianos"))
    assert process_once(owner="normalization-owner")
    first = authenticated.get(f"/api/v1/documents/originals/{original['id']}").json()["extraction"]
    response = authenticated.post(f"/api/v1/documents/originals/{original['id']}/review", json={
        "expected_extraction_id": first["id"], "changes": {"headers.0.color": "COP colombiano"},
        "confirmed": ["headers.0.color"], "reason": "Revisión automatizada de fixture sintético; equivalencia declarada"})
    assert response.status_code == 200, response.text
    current = response.json()["extraction"]
    cell = current["payload"]["fields"]["color"]
    assert cell["value"] == "COP" and cell["observed"] == "pesos colombianos" and cell["origin"] is None
    assert cell["correction_input"] == "COP colombiano"
    assert cell["correction_normalization"]["rule"] == "DECLARED_CURRENCY_EQUIVALENCE"
    assert current["previous_id"] == first["id"] and cell["reviewed"] is True
    with database() as db:
        assert db.get(DocumentExtraction, first["id"]).payload == first["payload"]


def test_local_strategy_parameter_cannot_change_the_legacy_provider_worker_lane(authenticated, tmp_path, monkeypatch):
    import trackvance.document_worker as worker

    original, definition = setup_document(authenticated, tmp_path)
    response = authenticated.put(f"/api/v1/documents/engines?family_id={original['family_id']}", json={
        "expected_version": 1, "configuration": EngineConfiguration(provider="TESSERACT_LEGACY",
            extraction_strategy="NATIVE_FIRST").model_dump(), "reason": "Prueba sintética de selección explícita del proveedor histórico"})
    assert response.status_code == 200, response.text
    calls = []
    monkeypatch.setattr(worker, "run_engine", lambda *_args, **_kwargs: calls.append("legacy") or native(definition))
    assert process_once(owner="legacy-provider-owner")
    current = authenticated.get(f"/api/v1/documents/originals/{original['id']}").json()
    assert calls == ["legacy", "legacy"]  # Existing extraction and independent validation.
    assert current["extraction"]["engine"]["provider"] == "TESSERACT_LEGACY"
    assert "lossless_contract_version" not in current["extraction"]["engine"]
    assert current["recognition_artifacts"] == [] and "execution" not in current


def test_worker_persists_then_replay_creates_revision_without_calling_any_reader(authenticated, database, tmp_path, monkeypatch):
    import trackvance.document_worker as worker

    original, definition = setup_document(authenticated, tmp_path)
    calls = []
    monkeypatch.setattr(worker, "run_engine", lambda *_args, **_kwargs: calls.append("native") or native(definition))
    assert process_once(owner="owner-1")
    response = authenticated.get(f"/api/v1/documents/originals/{original['id']}")
    assert response.status_code == 200, response.text
    current = response.json()
    assert calls == ["native"]
    assert current["execution"]["recognition_complete"] is True
    assert current["status"] == "AUTO_VERIFIED", current.get("quality")
    assert current["extraction"]["reviewed"] is False and current["issues"] == []
    assert current["recognition_artifacts"]
    first = copy.deepcopy(current["extraction"])
    artifact_id = current["recognition_artifacts"][0]["artifact_id"]
    download = authenticated.get(f"/api/v1/documents/originals/{original['id']}/recognition-artifacts/{artifact_id}/content")
    assert download.status_code == 200
    assert download.headers["content-disposition"].startswith("attachment")
    assert download.headers["x-content-type-options"] == "nosniff"
    replay = authenticated.post(f"/api/v1/documents/originals/{original['id']}/replay", json={
        "expected_extraction_id": first["id"], "reason": "Reproducción de prueba sin nueva lectura"})
    assert replay.status_code == 200, replay.text
    second = replay.json()["extraction"]
    assert calls == ["native"] and second["revision"] == 2 and second["previous_id"] == first["id"]
    assert second["payload"]["fields"]["color"]["value"] == "blue"
    assert second["engine"]["replay"]["recognition_model_calls"] == 0
    with database() as db:
        preserved = db.get(DocumentExtraction, first["id"])
        assert preserved.payload_hash == first["sha256"] and preserved.payload == first["payload"]
        assert db.scalar(select(func.count()).select_from(Dataset)) == 0
    stale = authenticated.post(f"/api/v1/documents/originals/{original['id']}/replay", json={
        "expected_extraction_id": first["id"], "reason": "Debe rechazar revisión obsoleta"})
    assert stale.status_code == 409


def test_restart_recovers_durable_partial_without_repeating_recognition(authenticated, database, tmp_path, monkeypatch):
    import trackvance.document_worker as worker
    from trackvance.document_lossless import _native_events
    from trackvance.document_providers import freeze_configuration, hash_value

    original, definition = setup_document(authenticated, tmp_path)
    with database() as db:
        item = db.get(DocumentOriginal, original["id"])
        job = db.scalar(select(Job).where(Job.document_id == item.id))
        configuration = freeze_configuration(db, item)
        attempt = job.id + "-1"
        events = _native_events(native(definition, 2), item.sha256, hash_value(configuration), {"attempt_id": attempt},
            definition_sha256=hash_value(definition))
        persist_recognition_event(db, item, events[0], attempt_id=attempt,
            configuration_sha256=hash_value(configuration), job_id=job.id, worker_attempt_number=1)
        item.status, job.status, job.attempts, job.lease_owner = "RUNNING", "RUNNING", 1, "lost-owner"
        job.lease_until = utcnow() - timedelta(seconds=1)
        db.commit()
    def forbidden(*_args, **_kwargs):
        raise AssertionError("Recovery must use the durable raw, not rerun recognition")
    monkeypatch.setattr(worker, "run_engine", forbidden)
    assert process_once(owner="recovery-owner")
    response = authenticated.get(f"/api/v1/documents/originals/{original['id']}")
    assert response.status_code == 200, response.text
    current = response.json()
    assert current["status"] == "REVIEW_REQUIRED"
    assert current["execution"]["emitted_pages"] == [1] and current["execution"]["pending_pages"] == [2]
    assert current["execution"]["recognition_complete"] is False
    assert "RECOGNITION_INCOMPLETE" in current["issues"]
    assert current["extraction"]["engine"]["recovered_from_attempt"] == attempt
    with database() as db:
        item = db.get(DocumentOriginal, original["id"])
        assert len(latest_recognition_events(db, item, hydrate=True)) == 1
        assert db.scalar(select(func.count()).select_from(Dataset)) == 0
