"""Native publication and small module consumption; recognition is a CPU double.

An explicit test-agent review is never counted as automatic source verification.
"""
import copy

from sqlalchemy import func, select
from test_document_semantic_general_098 import cell, selection
from test_document_semantic_general_lifecycle_098 import (
    all_paths,
    install_reader_double,
    setup_profile,
    upload_fixture,
)

from trackvance.document_models import DocumentExtraction
from trackvance.document_worker import process_once as process_document
from trackvance.models import Configuration, DatasetVersion, Job, Run
from trackvance.services import enqueue
from trackvance.worker import process_once as process_module


def published_fixture(client, monkeypatch):
    family, template = setup_profile(client, monkeypatch)
    # Equal row values represent two different occurrences and must survive.
    rows = [{"description": cell("Repeated source row"), "amount": cell("5.00")},
        {"description": cell("Repeated source row"), "amount": cell("5.00")},
        {"description": cell("Third source row"), "amount": cell("2.50")}]
    calls = install_reader_double(monkeypatch, [selection(rows=rows)])
    original = upload_fixture(client, family, label="native-consumption-fixture")
    assert process_document("semantic-consumption-document")
    before = client.get(f"/api/v1/documents/originals/{original['id']}").json()
    assert before["status"] == "REVIEW_REQUIRED" and not before["publications"]
    response = client.post(f"/api/v1/documents/originals/{original['id']}/review", json={
        "expected_extraction_id": before["extraction"]["id"],
        "confirmed": all_paths(before["extraction"]), "semantic_table_confirmations": ["items"],
        "reason": "Authorized test-agent review and complete source-row coverage of a synthetic fixture. Not AUTO."})
    assert response.status_code == 200, response.text
    published = response.json()
    assert published["status"] == "PUBLISHED" and published["semantic_review"]["automatic_approval"] is False
    return family, template, original, before, published, calls


def test_semantic_native_dataset_manual_intake_consumes_three_rows_and_preserves_lineage(
        authenticated, database, monkeypatch):
    family, _template, original, automatic, published, calls = published_fixture(authenticated, monkeypatch)
    version_id = published["publications"][0]["versions"]["items"]
    profile = authenticated.get(f"/api/v1/dataset-versions/{version_id}/profile").json()
    assert profile["row_count"] == 3
    assert [item["amount"] for item in profile["sample"]] == ["5.00", "5.00", "2.50"]
    with database() as db:
        source = db.get(DatasetVersion, version_id)
        frozen = copy.deepcopy(source.ingestion_metadata)
        original_artifact = source.canonical_artifact_id
        configuration = Configuration(name="Manual small semantic Intake fixture", module="intake",
            dataset_id=source.dataset_id, config={"required_columns": ["description", "amount"],
                "positive_columns": ["amount"], "max_error_rate": 0})
        db.add(configuration)
        db.flush()
        run = enqueue(db, configuration, source, None, "Test User")
        run_id = run.id
        db.commit()
    assert process_module("semantic-manual-consumer")
    assert not process_module("semantic-manual-consumer-no-duplicate")
    with database() as db:
        run = db.get(Run, run_id)
        assert run.status == "SUCCESS" and run.decision == "APPROVED", run.error
        assert run.metrics["valid_rows"] == 3 and run.metrics["error_rows"] == 0
        output = db.get(DatasetVersion, run.output_version_id)
        assert output.parent_version_id == version_id and output.row_count == 3
        source = db.get(DatasetVersion, version_id)
        assert source.ingestion_metadata == frozen and source.canonical_artifact_id == original_artifact
        assert source.ingestion_metadata["document_publication"]["family_id"] == family
        assert source.ingestion_metadata["document_publication"]["automatic_processing_allowed"] is False
        assert db.get(DocumentExtraction, automatic["extraction"]["id"]).payload == automatic["extraction"]["payload"]
        job = db.scalar(select(Job).where(Job.run_id == run_id))
        assert job.status == "SUCCESS" and job.attempts == 1
    evidence = authenticated.get(f"/api/v1/runs/{run_id}/evidence")
    assert evidence.status_code == 200 and evidence.json()["inputs"][0]["dataset_version_id"] == version_id
    assert calls == ["QWEN_VISUAL"]
    assert all(item["document_id"] == original["id"] and
        item["extraction_id"] == published["extraction"]["id"] for item in profile["sample"])


def test_semantic_replay_requires_fresh_review_and_keeps_published_versions_immutable(
        authenticated, database, monkeypatch):
    _family, _template, original, _automatic, published, calls = published_fixture(authenticated, monkeypatch)
    old = copy.deepcopy(published["extraction"])
    old_versions = copy.deepcopy(published["publications"])
    with database() as db:
        before = {v.id: (v.sha256, copy.deepcopy(v.ingestion_metadata))
            for v in db.scalars(select(DatasetVersion)).all()}
        count = db.scalar(select(func.count()).select_from(DatasetVersion))
    response = authenticated.post(f"/api/v1/documents/originals/{original['id']}/replay", json={
        "expected_extraction_id": old["id"], "reason": "Replay preserved source observations without a new reader or model call."})
    assert response.status_code == 200, response.text
    replayed = response.json()
    assert calls == ["QWEN_VISUAL"] and replayed["status"] == "REVIEW_REQUIRED"
    assert replayed["extraction"]["previous_id"] == old["id"]
    assert not replayed["extraction"]["reviewed"]
    assert replayed["semantic_review"]["tables"][0]["review_required"] is True
    assert replayed["publications"] == old_versions
    stale = authenticated.post(f"/api/v1/documents/originals/{original['id']}/review", json={
        "expected_extraction_id": old["id"], "confirmed": all_paths(old),
        "reason": "Stale review cannot authorize a later extraction."})
    assert stale.status_code == 409
    with database() as db:
        assert db.scalar(select(func.count()).select_from(DatasetVersion)) == count
        assert db.get(DocumentExtraction, old["id"]).payload == old["payload"]
        assert {v.id: (v.sha256, v.ingestion_metadata) for v in db.scalars(select(DatasetVersion)).all()} == before
