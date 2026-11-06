"""Native transactions over explicit source/recognizer fixtures, never model accuracy.

The existing disposable database/StorageProvider fixtures isolate all writes;
no prior campaign, permanent service, or real model is contacted.
"""
import copy
import hashlib
import json
from collections import Counter

import pytest
from sqlalchemy import func, select
from test_document_semantic_auto_lifecycle_098 import activate, digital_reader
from test_document_semantic_general_098 import profile
from test_document_semantic_general_lifecycle_098 import upload_fixture

from trackvance.document_models import DocumentExtraction, DocumentOriginal
from trackvance.document_recognition_store import recognition_journal
from trackvance.document_representation import decode_artifact
from trackvance.document_worker import process_once
from trackvance.models import Artifact, DatasetVersion, Job


def activate_v2(client, monkeypatch, family=None, *, acquisition_profile=None):
    monkeypatch.setenv("TRACKVANCE_SEMANTIC_GENERAL_ENABLED", "true")
    monkeypatch.setenv("TRACKVANCE_SEMANTIC_AUTO_ENABLED", "true")
    if family is None:
        result = client.post("/api/v1/documents/families", json={"name": "Synthetic V2 transaction fixture"})
        assert result.status_code == 200, result.text
        family = result.json()["id"]
    result = client.post(f"/api/v1/documents/families/{family}/semantic-profile", json={
        "profile": acquisition_profile or profile(), "route": "QWEN_VISUAL",
        "automatic_verification": "SEMANTIC_AUTO_V2",
        "reason": "Synthetic CPU-only verification contract; no model acquisition or real-world AUTO claim"})
    assert result.status_code == 200, result.text
    return family, result.json()["template"]


def v1_original(client, monkeypatch, label, *, activation=activate):
    family, template = activation(client, monkeypatch)
    import test_document_semantic_auto_lifecycle_098 as fixture

    original_events = fixture.independent_qwen_events

    def runtime_bound_events(*args, **kwargs):
        items = original_events(*args, **kwargs)
        # The older broad lifecycle double uses a fixed 32768-token request.
        # This stricter reuse fixture records its actual declared configuration.
        configuration = args[1]
        for item in items:
            for index, stored in enumerate(item.get("payload", {}).get("artifacts", [])):
                if stored["kind"] == "QWEN_REQUEST_JSON":
                    from test_document_semantic_general_098 import artifact, gateway

                    request = json.loads(decode_artifact(stored))
                    body = request["request_without_image_bytes"]
                    body["options"]["num_ctx"] = configuration["context_tokens"]
                    body["messages"][-1]["images"] = [next(a["content_b64"] for event in items
                        for a in event.get("payload", {}).get("artifacts", []) if a["kind"] == "PAGE_IMAGE_PNG")]
                    request["request_sha256"] = gateway().digest(body)
                    body["messages"][-1].pop("images")
                    item["payload"]["artifacts"][index] = artifact("QWEN_REQUEST_JSON", request)
        for item in items:
            if item["kind"] in {"PAGE", "TERMINAL"}:
                item["payload"].setdefault("engine", {})["model_lock_sha256"] = hashlib.sha256(
                    b"explicit synthetic runtime lock; no real model acquisition"
                ).hexdigest()
        return items

    monkeypatch.setattr(fixture, "independent_qwen_events", runtime_bound_events)
    calls = digital_reader(monkeypatch)
    original = upload_fixture(client, family, label=label)
    assert process_once("v2-native-acquisition-fixture")
    detail = client.get("/api/v1/documents/originals/" + original["id"]).json()
    assert detail["status"] == "PUBLISHED" and calls == ["QWEN_VISUAL"]
    return family, template, original, detail, calls


def counts(database):
    with database() as db:
        return {model.__tablename__: db.scalar(select(func.count()).select_from(model))
            for model in [Artifact, DocumentExtraction, DatasetVersion, Job]}


def canonical_rows(rows):
    return Counter(json.dumps(row, sort_keys=True, ensure_ascii=False) for row in rows)


def test_native_upgrade_reuses_exact_acquisition_and_keeps_old_proof_and_publication(authenticated, database, monkeypatch):
    family, old_template, original, before, calls = v1_original(authenticated, monkeypatch, "v2-reuse-exact")
    with database() as db:
        old = db.get(DocumentExtraction, before["extraction"]["id"])
        old_payload, old_engine = copy.deepcopy(old.payload), copy.deepcopy(old.engine)
        source = db.get(DocumentOriginal, original["id"])
        old_events = [copy.deepcopy(row["event"]) for row in recognition_journal(db, source, hydrate=True)]
        job_before = db.scalar(select(Job).where(Job.document_id == source.id)).attempts
    _, target = activate_v2(authenticated, monkeypatch, family)
    assert target["definition"]["semantic_general"]["profile_sha256"] == old_template["definition"]["semantic_general"]["profile_sha256"]
    result = authenticated.post(f"/api/v1/documents/originals/{original['id']}/replay", json={
        "expected_extraction_id": before["extraction"]["id"], "template_id": target["id"],
        "reason": "Explicit CPU verification V2; keep original acquisition contract and immutable outputs"})
    assert result.status_code == 200, result.text
    after = result.json()
    assert after["extraction"]["previous_id"] == before["extraction"]["id"]
    assert calls == ["QWEN_VISUAL"] and after["publications"] == before["publications"]
    assert not after["extraction"]["reviewed"]
    with database() as db:
        current = db.get(DocumentExtraction, after["extraction"]["id"])
        assert current.engine["validation"]["validator_version"] == "TRACKVANCE_SEMANTIC_EVIDENCE_098_V2"
        receipt = current.engine["semantic_verification_replay"]
        assert receipt["recognition_model_calls_new"] == 0
        assert receipt["acquisition_profile_sha256"] == old_template["definition"]["semantic_general"]["profile_sha256"]
        assert receipt["verification_contract_sha256"] == target["definition"]["semantic_general"]["verification_contract_sha256"]
        assert db.get(DocumentExtraction, before["extraction"]["id"]).payload == old_payload
        assert db.get(DocumentExtraction, before["extraction"]["id"]).engine == old_engine
        events = [row["event"] for row in recognition_journal(db, db.get(DocumentOriginal, original["id"]), hydrate=True)]
        by_hash = {event["event_sha256"]: event for event in events}
        assert all(by_hash[event["event_sha256"]] == event for event in old_events)
        assert len(events) == len(old_events) + 1 and events[-1]["stage"] == "RECOVERY_REUSE"
        assert db.scalar(select(Job).where(Job.document_id == original["id"])).attempts == job_before
        assert db.scalar(select(func.count()).select_from(DatasetVersion)) == 1
    sizes = counts(database)
    repeated = authenticated.post(f"/api/v1/documents/originals/{original['id']}/replay", json={
        "expected_extraction_id": before["extraction"]["id"], "template_id": target["id"], "reason": "Stale duplicate"})
    assert repeated.status_code == 409 and counts(database) == sizes


def test_upgrade_rejects_a_changed_acquisition_profile_without_registering_reuse(authenticated, database, monkeypatch):
    family, _, original, before, calls = v1_original(authenticated, monkeypatch, "v2-reuse-changed-profile")
    changed = profile()
    changed["revision"] = 2
    changed["fields"][0]["label"] = "Different reference"
    _, target = activate_v2(authenticated, monkeypatch, family, acquisition_profile=changed)
    sizes = counts(database)
    response = authenticated.post(f"/api/v1/documents/originals/{original['id']}/replay", json={
        "expected_extraction_id": before["extraction"]["id"], "template_id": target["id"],
        "reason": "A different recognizer input cannot be passed off as verification-only replay"})
    assert response.status_code == 409 and response.json()["error"]["code"] == "DOCUMENT_SEMANTIC_REPLAY_CONTRACT"
    assert counts(database) == sizes and calls == ["QWEN_VISUAL"]


def test_entry_v2_replay_uses_saved_acquisition_under_current_v2(authenticated, database, monkeypatch):
    from trackvance import document_semantic_general as semantic
    from trackvance.document_semantic_corroboration import trusted_semantic_proof
    from trackvance.document_semantic_vocabulary import V2_ENTRY_SHA256

    current_descriptor = semantic.verification_contract_v2()
    entry_descriptor = {**current_descriptor, "vocabulary_sha256": V2_ENTRY_SHA256}
    assert entry_descriptor != current_descriptor
    with monkeypatch.context() as entry:
        entry.setattr(semantic, "verification_contract_v2", lambda: entry_descriptor)
        family, source_template, original, before, calls = v1_original(
            authenticated, monkeypatch, "saved-entry-v2-replay", activation=activate_v2)
    with database() as db:
        source = db.get(DocumentOriginal, original["id"])
        old_events = [copy.deepcopy(row["event"]) for row in recognition_journal(db, source, hydrate=True)]
        old_extraction = db.get(DocumentExtraction, before["extraction"]["id"])
        old_payload, old_engine = copy.deepcopy(old_extraction.payload), copy.deepcopy(old_extraction.engine)
        assert trusted_semantic_proof(old_extraction, source_template["definition"]) is None
        old_attempts = db.scalar(select(Job).where(Job.document_id == source.id)).attempts
    _, target = activate_v2(authenticated, monkeypatch, family)
    target_metadata = target["definition"]["semantic_general"]
    old_metadata = source_template["definition"]["semantic_general"]
    assert old_metadata["profile_sha256"] == target_metadata["profile_sha256"]
    assert old_metadata["verification_contract_sha256"] != target_metadata["verification_contract_sha256"]
    result = authenticated.post(f"/api/v1/documents/originals/{original['id']}/replay", json={
        "expected_extraction_id": before["extraction"]["id"], "template_id": target["id"],
        "reason": "Assess under current V2 using the unchanged persisted reader request"})
    assert result.status_code == 200, result.text
    after = result.json()
    assert after["extraction"]["previous_id"] == before["extraction"]["id"]
    assert after["publications"] == before["publications"] and calls == ["QWEN_VISUAL"]
    with database() as db:
        current = db.get(DocumentExtraction, after["extraction"]["id"])
        retained = db.get(DocumentExtraction, before["extraction"]["id"])
        assert retained.payload == old_payload and retained.engine == old_engine
        assert current.engine["validation"]["association_contract_sha256"] == target_metadata["verification_contract_sha256"]
        assert not current.engine["validation"]["global_issues"]
        assert trusted_semantic_proof(current, target["definition"]) is not None
        receipt = current.engine["semantic_verification_replay"]
        assert receipt["recognition_model_calls_new"] == 0
        assert receipt["acquisition_profile_sha256"] == old_metadata["profile_sha256"]
        assert receipt["verification_contract_sha256"] == target_metadata["verification_contract_sha256"]
        events = [row["event"] for row in recognition_journal(db, db.get(DocumentOriginal, original["id"]), hydrate=True)]
        assert len(events) == len(old_events) + 1 and events[-1]["stage"] == "RECOVERY_REUSE"
        by_hash = {event["event_sha256"]: event for event in events}
        assert all(by_hash[event["event_sha256"]] == event for event in old_events)
        assert db.scalar(select(Job).where(Job.document_id == original["id"])).attempts == old_attempts


@pytest.mark.parametrize("changed", ["old_hash", "old_descriptor", "new_hash", "new_descriptor",
    "profile", "fields", "tables", "route", "business", "configuration", "downgrade"])
def test_entry_v2_replay_rejects_an_unbound_transition(monkeypatch, changed):
    from trackvance import document_semantic_general as semantic
    from trackvance.document_normalization import canonical_hash
    from trackvance.document_semantic_vocabulary import V2_ENTRY_SHA256
    from trackvance.operations_common import OperationError

    monkeypatch.setenv("TRACKVANCE_SEMANTIC_GENERAL_ENABLED", "true")
    monkeypatch.setenv("TRACKVANCE_SEMANTIC_AUTO_ENABLED", "true")
    target = semantic.compile_profile(profile(), automatic_verification=semantic.AUTO_VERSION_V2)
    original = copy.deepcopy(target)
    before, after = original["semantic_general"], target["semantic_general"]
    before["verification_contract"]["vocabulary_sha256"] = V2_ENTRY_SHA256
    before["verification_contract_sha256"] = canonical_hash(before["verification_contract"])
    configuration = semantic.configuration_for(target, {})
    if changed == "old_hash":
        before["verification_contract_sha256"] = after["verification_contract_sha256"]
    elif changed == "new_hash":
        after["verification_contract_sha256"] = before["verification_contract_sha256"]
    elif changed in {"old_descriptor", "new_descriptor"}:
        meta = before if changed == "old_descriptor" else after
        meta["verification_contract"]["vocabulary_sha256"] = "b" * 64
        meta["verification_contract_sha256"] = canonical_hash(meta["verification_contract"])
    elif changed in {"profile", "route", "business"}:
        key = {"profile": "profile_sha256", "route": "route", "business": "business_controls_sha256"}[changed]
        after[key] = "b" * 64
    elif changed == "fields":
        target["fields"].clear()
    elif changed == "tables":
        target["tables"].append({"name": "new_table"})
    elif changed == "configuration":
        configuration["semantic_profile_sha256"] = "b" * 64
    elif changed == "downgrade":
        original, target = target, original
    with pytest.raises(OperationError, match="contrato|configuración"):
        semantic.assert_replay_binding(original, target, configuration)


def test_upgrade_requires_configuration_permission_and_preserves_all_prior_records(authenticated, database, monkeypatch):
    family, _, original, before, calls = v1_original(authenticated, monkeypatch, "v2-reuse-permission")
    _, target = activate_v2(authenticated, monkeypatch, family)
    import trackvance.document_service as service

    grants = service.effective_permissions
    monkeypatch.setattr(service, "effective_permissions", lambda db, user: set(grants(db, user)) - {"documents:configure"})
    sizes = counts(database)
    result = authenticated.post(f"/api/v1/documents/originals/{original['id']}/replay", json={
        "expected_extraction_id": before["extraction"]["id"], "template_id": target["id"], "reason": "Denied contract change"})
    assert result.status_code == 403 and counts(database) == sizes and calls == ["QWEN_VISUAL"]


def test_upgrade_does_not_start_while_an_existing_reader_is_active(authenticated, database, monkeypatch):
    family, _, original, before, calls = v1_original(authenticated, monkeypatch, "v2-reuse-active")
    _, target = activate_v2(authenticated, monkeypatch, family)
    with database() as db:
        job = db.scalar(select(Job).where(Job.document_id == original["id"]))
        job.status = "RUNNING"
        db.commit()
    sizes = counts(database)
    result = authenticated.post(f"/api/v1/documents/originals/{original['id']}/replay", json={
        "expected_extraction_id": before["extraction"]["id"], "template_id": target["id"], "reason": "Do not overlap acquisition"})
    assert result.status_code == 409 and result.json()["error"]["code"] == "DOCUMENT_REPLAY_RUNNING"
    assert counts(database) == sizes and calls == ["QWEN_VISUAL"]


def test_v2_synthetic_native_auto_publication_and_small_intake_are_idempotent(authenticated, database, monkeypatch):
    family, _ = activate_v2(authenticated, monkeypatch)
    calls = digital_reader(monkeypatch)
    original = upload_fixture(authenticated, family, label="v2-native-intake-synthetic")
    assert process_once("v2-native-publication-fixture")
    detail = authenticated.get("/api/v1/documents/originals/" + original["id"]).json()
    assert detail["status"] == "PUBLISHED" and detail["quality"]["status"] == "AUTO_VERIFIED", detail["issues"]
    assert calls == ["QWEN_VISUAL"] and not detail["extraction"]["reviewed"]
    assert all(not v["review_required"] for v in detail["semantic_review"]["values"])
    publication = detail["publications"][0]
    source_id = publication["versions"]["headers"]
    source = authenticated.get(f"/api/v1/dataset-versions/{source_id}/profile").json()
    assert source["row_count"] == 1 and source["sample"][0]["document_id"] == original["id"]
    for _ in range(2):
        published = authenticated.post(f"/api/v1/documents/batches/{publication['batch_id']}/publish")
        assert published.status_code == 200 and published.json()["published_versions"]["headers"] == source_id
    contract = authenticated.post("/api/v1/intake/contracts", json={
        "name": "Synthetic V2 preserved header transport", "dataset_id": source["dataset_id"], "owner": "Test User",
        "description": "CPU fixture only, not model or financial-source certification",
        "config": {"schema_version": 2, "required_columns": ["reference", "amount", "document_id", "extraction_id", "batch_id"],
            "numeric_columns": ["amount"], "unique_columns": [], "positive_columns": [], "max_error_rate": 0}})
    assert contract.status_code == 201, contract.text
    body = {"contract_id": contract.json()["id"], "dataset_version_id": source_id, "requested_engine": "POLARS"}
    headers = {"Idempotency-Key": "semantic-v2-native-intake-once"}
    first = authenticated.post("/api/v1/intake/runs", json=body, headers=headers)
    second = authenticated.post("/api/v1/intake/runs", json=body, headers=headers)
    assert first.status_code == second.status_code == 202 and first.json()["id"] == second.json()["id"]
    from trackvance import worker

    assert worker.process_once("v2-native-manual-intake-fixture")
    run = authenticated.get("/api/v1/runs/" + first.json()["id"]).json()
    assert run["status"] == "SUCCESS" and run["metrics"]["valid_rows"] == 1, json.dumps(run)
    output = authenticated.get(f"/api/v1/dataset-versions/{run['output_version_id']}/profile").json()
    assert output["parent_version_id"] == source_id and output["source_run_id"] == first.json()["id"]
    assert output["schema"] == source["schema"]
    assert canonical_rows(output["sample"]) == canonical_rows(source["sample"])
    repeated = authenticated.post("/api/v1/intake/runs", json=body, headers=headers)
    assert repeated.status_code == 202 and repeated.json()["id"] == first.json()["id"]
    with database() as db:
        assert db.scalar(select(func.count()).select_from(DatasetVersion)) == 2
        assert not list(db.scalars(select(Job).where(Job.status.in_(["QUEUED", "RUNNING", "RETRY_WAIT"]))))
