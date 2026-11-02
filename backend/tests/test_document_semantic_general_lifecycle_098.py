"""Native API/storage/review/dataset transactions using explicit recognition doubles.

These fixtures prove lifecycle guards, not real model accuracy or scanned OCR.
"""
import copy
import hashlib

import pytest
from sqlalchemy import func, select
from test_document_semantic_general_098 import cell, fixture_events, profile, selection

from trackvance.document_engine import extract
from trackvance.document_models import DocumentExtraction, DocumentOriginal, DocumentTemplate
from trackvance.document_normalization import canonical_hash
from trackvance.document_semantic_general import compile_profile, profiles
from trackvance.document_worker import process_once
from trackvance.models import DatasetVersion, Job


def setup_profile(client, monkeypatch, *, table=True, collection=False):
    monkeypatch.setenv("TRACKVANCE_SEMANTIC_GENERAL_ENABLED", "true")
    response = client.post("/api/v1/documents/families", json={"name": "General semantic lifecycle fixture"})
    assert response.status_code == 200, response.text
    family = response.json()["id"]
    response = client.post(f"/api/v1/documents/families/{family}/semantic-profile", json={
        "profile": profile(table=table, collection=collection), "route": "QWEN_VISUAL",
        "reason": "Explicit test-only candidate opt-in; schema activation is not model approval."})
    assert response.status_code == 200, response.text
    return family, response.json()["template"]


def install_reader_double(monkeypatch, values):
    import trackvance.document_recognition_transport as transport
    import trackvance.document_worker as worker

    calls = []
    def native(_source, definition, **_kwargs):
        payload = extract([], definition)
        payload.update(pages=[1], native_incomplete_pages=[1])
        return {"payload": payload, "engine": {"pages": 1, "key": "SEMANTIC_TEST_NATIVE_DOUBLE"}}
    def recognize(source, definition, configuration, **kwargs):
        calls.append(kwargs["strategy"])
        items = fixture_events(values, definition, configuration, hashlib.sha256(source.read_bytes()).hexdigest())
        items = [item for item in items if item["stage"] != "NATIVE_SOURCE_PROBE"]
        for number, item in enumerate(items, 1):
            item.pop("event_sha256", None)
            item["sequence"] = number
            item["attempt_id"] = kwargs["attempt_context"]["attempt_id"]
            item["event_sha256"] = canonical_hash(item)
            kwargs["on_event"](item)
        return {"pages": items[:-1], "terminal": items[-1]}
    monkeypatch.setattr(worker, "run_engine", native)
    monkeypatch.setattr(transport, "recognize_stream", recognize)
    return calls


def upload_fixture(client, family, *, label="one"):
    data = b"%PDF-1.4\nsemantic lifecycle synthetic unit fixture " + label.encode() + b"\n%%EOF"
    response = client.post(f"/api/v1/documents/families/{family}/originals",
        files={"files": (label + ".pdf", data, "application/pdf")})
    assert response.status_code == 200, response.text
    return response.json()["items"][0]


def all_paths(extraction):
    payload = extraction["payload"]
    return [f"headers.0.{name}" for name in payload["fields"]] + [
        f"{name}.{index}.{field}" for name, rows in payload["tables"].items()
        for index, row in enumerate(rows) for field in row]


def test_disabled_capability_blocks_activation_before_writing_templates(authenticated, database, monkeypatch):
    monkeypatch.delenv("TRACKVANCE_SEMANTIC_GENERAL_ENABLED", raising=False)
    result = authenticated.get("/api/v1/documents/semantic-profiles")
    assert result.status_code == 200 and result.json()["enabled"] is False
    family = authenticated.post("/api/v1/documents/families", json={"name": "Disabled semantic candidate"}).json()["id"]
    response = authenticated.post(f"/api/v1/documents/families/{family}/semantic-profile", json={
        "profile_id": "invoice-v1", "reason": "Attempt while feature is disabled"})
    assert response.status_code == 409 and response.json()["error"]["code"] == "DOCUMENT_SEMANTIC_GENERAL_DISABLED"
    with database() as db:
        assert db.scalar(select(func.count()).select_from(DocumentTemplate)) == 0


def test_semantic_upload_native_review_publication_lineage_and_versions(authenticated, database, monkeypatch):
    family, template = setup_profile(authenticated, monkeypatch)
    rows = [{"description": cell("First item"), "amount": cell("5.00")},
        {"description": cell("Second item"), "amount": cell("7.50")}]
    calls = install_reader_double(monkeypatch, [selection(rows=rows)])
    original = upload_fixture(authenticated, family)
    assert not original["is_example"] and original["template_id"] == template["id"]
    assert process_once("semantic-lifecycle-test")
    detail = authenticated.get("/api/v1/documents/originals/" + original["id"]).json()
    assert detail["status"] == "REVIEW_REQUIRED" and calls == ["QWEN_VISUAL"]
    assert detail["extraction"]["payload"]["document_representation"]["pages"]
    assert {item["kind"] for item in detail["recognition_artifacts"]} == {"NATIVE_SOURCE_PROBE_JSON", "QWEN_SELECTION_JSON"}
    assert detail["publications"] == []
    assert detail["semantic_review"]["automatic_approval"] is False
    assert detail["engine_snapshot"]["extraction_mode"] == "SEMANTIC_GENERAL_V1"
    first = copy.deepcopy(detail["extraction"])
    response = authenticated.post(f"/api/v1/documents/originals/{original['id']}/review", json={
        "expected_extraction_id": first["id"], "confirmed": all_paths(first),
        "reason": "Authorized agent fixture review of declared synthetic source; not automatic validation."})
    assert response.status_code == 200, response.text
    reviewed = response.json()
    assert reviewed["status"] == "REVIEW_REQUIRED" and reviewed["publications"] == []
    assert "SEMANTIC_TABLE_COVERAGE_REVIEW_REQUIRED:items" in reviewed["issues"]
    response = authenticated.post(f"/api/v1/documents/originals/{original['id']}/review", json={
        "expected_extraction_id": reviewed["extraction"]["id"], "semantic_table_confirmations": ["items"],
        "reason": "Authorized agent explicitly checked all fixture rows; no model approval claimed."})
    assert response.status_code == 200, response.text
    published = response.json()
    assert published["status"] == "PUBLISHED" and len(published["publications"]) == 1
    assert published["extraction"]["reviewed"] and len(published["revisions"]) == 3
    assert published["semantic_review"]["tables"][0]["review_required"] is False
    assert published["semantic_review"]["tables"][0]["latest_confirmation"]["scope"] == "ALL_SOURCE_ROWS_OR_CONFIRMED_ABSENCE"
    versions = published["publications"][0]["versions"]
    assert set(versions) == {"headers", "items"}
    for structure, count in [("headers", 1), ("items", 2)]:
        profile_data = authenticated.get(f"/api/v1/dataset-versions/{versions[structure]}/profile").json()
        assert profile_data["row_count"] == count
        assert all(row["document_id"] == original["id"] and row["extraction_id"] == published["extraction"]["id"] for row in profile_data["sample"])
        metadata = profile_data["ingestion_metadata"]["document_publication"]
        assert metadata["structure"] == structure and metadata["family_id"] == family
        assert metadata["automatic_processing_allowed"] is False and metadata["automatic_delivery_allowed"] is False
    assert not process_once("semantic-lifecycle-test-no-new-job")
    with database() as db:
        assert db.get(DocumentExtraction, first["id"]).payload == first["payload"]
        current = db.get(DocumentOriginal, original["id"])
        assert current.engine_snapshot["extraction_mode"] == "SEMANTIC_GENERAL_V1"
        assert current.engine_snapshot["automatic_verification"] == "OFF"
        assert db.scalar(select(func.count()).select_from(DatasetVersion)) == 2
        assert db.scalar(select(func.count()).select_from(Job).where(Job.status == "RUNNING")) == 0


def test_contradiction_requires_correction_and_preserves_automatic_revision(authenticated, database, monkeypatch):
    family, _template = setup_profile(authenticated, monkeypatch, table=False)
    # Two source-page proposals contradict each other; these are explicit doubles.
    calls = install_reader_double(monkeypatch, [selection(amount="12.50"), selection(amount="13.50")])
    # The native source probe must report the same source page count as both pages.
    import trackvance.document_worker as worker
    def native(_source, definition, **_kwargs):
        payload = extract([], definition)
        payload.update(pages=[1, 2], native_incomplete_pages=[1, 2])
        return {"payload": payload, "engine": {"pages": 2, "key": "SEMANTIC_TEST_TWO_PAGE_NATIVE_DOUBLE"}}
    monkeypatch.setattr(worker, "run_engine", native)
    original = upload_fixture(authenticated, family)
    assert process_once("semantic-contradiction-fixture")
    detail = authenticated.get("/api/v1/documents/originals/" + original["id"]).json()
    assert detail["status"] == "REVIEW_REQUIRED" and detail["publications"] == [] and calls == ["QWEN_VISUAL"]
    first = detail["extraction"]
    amount = first["payload"]["fields"]["amount"]
    assert amount["validity"] == "AMBIGUOUS" and amount["value"] is None
    response = authenticated.post(f"/api/v1/documents/originals/{original['id']}/review", json={
        "expected_extraction_id": first["id"], "confirmed": all_paths(first), "reason": "Confirming alone must fail."})
    assert response.status_code == 422
    response = authenticated.post(f"/api/v1/documents/originals/{original['id']}/review", json={
        "expected_extraction_id": first["id"], "changes": {"headers.0.amount": "13.50"},
        "confirmed": all_paths(first), "reason": "Authorized fixture correction after reviewing both source pages; not AUTO."})
    assert response.status_code == 200, response.text
    current = response.json()
    assert current["status"] == "PUBLISHED" and current["extraction"]["reviewed"]
    cell_value = current["extraction"]["payload"]["fields"]["amount"]
    assert cell_value["value"] == "13.50" and cell_value["observed"] == "12.50"
    assert cell_value["corrected"] and cell_value["origin"] is None
    assert len(cell_value["candidate_alternatives"]) == 2
    with database() as db:
        assert db.get(DocumentExtraction, first["id"]).payload == first["payload"]


def test_empty_optional_collection_still_requires_explicit_source_absence_confirmation(authenticated, monkeypatch):
    family, _template = setup_profile(authenticated, monkeypatch, collection=True)
    install_reader_double(monkeypatch, [selection(rows=[])])
    original = upload_fixture(authenticated, family)
    assert process_once("semantic-empty-collection-fixture")
    detail = authenticated.get("/api/v1/documents/originals/" + original["id"]).json()
    response = authenticated.post(f"/api/v1/documents/originals/{original['id']}/review", json={
        "expected_extraction_id": detail["extraction"]["id"], "confirmed": all_paths(detail["extraction"]),
        "reason": "Checking header values does not prove collection absence."})
    assert response.status_code == 200 and response.json()["status"] == "REVIEW_REQUIRED"
    reviewed = response.json()
    assert "SEMANTIC_TABLE_COVERAGE_REVIEW_REQUIRED:items" in reviewed["issues"]
    response = authenticated.post(f"/api/v1/documents/originals/{original['id']}/review", json={
        "expected_extraction_id": reviewed["extraction"]["id"], "semantic_table_confirmations": ["items"],
        "reason": "Authorized fixture reviewer checked the complete source and found no eligible actions."})
    assert response.status_code == 200, response.text
    assert response.json()["status"] == "PUBLISHED"


def test_profile_revision_selection_does_not_rewrite_previous_original_or_dataset_version(authenticated, database, monkeypatch):
    family, first_template = setup_profile(authenticated, monkeypatch, table=False)
    install_reader_double(monkeypatch, [selection()])
    original = upload_fixture(authenticated, family)
    assert process_once("semantic-prior-original-fixture")
    detail = authenticated.get("/api/v1/documents/originals/" + original["id"]).json()
    original_snapshot = copy.deepcopy(detail)
    updated = profile()
    updated.update(revision=2, description="A new semantic schema revision, preserving previous bindings.")
    response = authenticated.post(f"/api/v1/documents/families/{family}/semantic-profile", json={
        "profile": updated, "reason": "Explicit later schema revision for new uploads only."})
    assert response.status_code == 200, response.text
    assert response.json()["template"]["id"] != first_template["id"]
    after = authenticated.get("/api/v1/documents/originals/" + original["id"]).json()
    assert after["template"] == original_snapshot["template"] and after["extraction"] == original_snapshot["extraction"]
    with database() as db:
        assert db.get(DocumentOriginal, original["id"]).template_id == first_template["id"]


@pytest.mark.parametrize("change", ["profile_hash", "route", "downgrade_to_legacy"])
def test_replay_cannot_relabel_old_semantic_observations_as_a_different_contract_or_route(authenticated, database, monkeypatch, change):
    family, first_template = setup_profile(authenticated, monkeypatch, table=False)
    calls = install_reader_double(monkeypatch, [selection()])
    original = upload_fixture(authenticated, family)
    assert process_once("semantic-replay-binding-fixture")
    detail = authenticated.get("/api/v1/documents/originals/" + original["id"]).json()
    if change == "downgrade_to_legacy":
        response = authenticated.post(f"/api/v1/documents/families/{family}/templates", json={
            "fields": first_template["definition"]["fields"], "publication": {"review_policy": "ALL"}})
        assert response.status_code == 200, response.text
        second_template = response.json()
    else:
        updated = profile()
        if change == "profile_hash":
            updated.update(revision=2, description="Changed requested meaning on a later profile revision.")
        response = authenticated.post(f"/api/v1/documents/families/{family}/semantic-profile", json={
            "profile": updated, "route": "NATIVE_FIRST" if change == "route" else "QWEN_VISUAL",
            "reason": "New explicit candidate contract for future documents."})
        assert response.status_code == 200, response.text
        second_template = response.json()["template"]
    response = authenticated.post(f"/api/v1/documents/originals/{original['id']}/replay", json={
        "expected_extraction_id": detail["extraction"]["id"], "template_id": second_template["id"],
        "reason": "Attempt to reinterpret immutable model observations with a different semantic input."})
    assert response.status_code == 409, response.text
    assert response.json()["error"]["code"] == "DOCUMENT_SEMANTIC_REPLAY_CONTRACT"
    assert calls == ["QWEN_VISUAL"]
    with database() as db:
        current = db.get(DocumentOriginal, original["id"])
        assert current.template_id == first_template["id"] and current.extraction_id == detail["extraction"]["id"]
        assert db.scalar(select(func.count()).select_from(DocumentExtraction)) == 1


def test_replay_allows_native_publication_revision_when_semantic_contract_and_route_are_identical(authenticated, database, monkeypatch):
    family, template = setup_profile(authenticated, monkeypatch, table=False)
    calls = install_reader_double(monkeypatch, [selection()])
    original = upload_fixture(authenticated, family)
    assert process_once("semantic-compatible-replay-fixture")
    detail = authenticated.get("/api/v1/documents/originals/" + original["id"]).json()
    definition = copy.deepcopy(template["definition"])
    definition["publication"]["wait_seconds"] += 1
    response = authenticated.post(f"/api/v1/documents/families/{family}/templates", json=definition)
    assert response.status_code == 200, response.text
    updated = response.json()
    response = authenticated.post(f"/api/v1/documents/originals/{original['id']}/replay", json={
        "expected_extraction_id": detail["extraction"]["id"], "template_id": updated["id"],
        "reason": "Native publication-only revision; the semantic model input is unchanged."})
    assert response.status_code == 200, response.text
    current = authenticated.get("/api/v1/documents/originals/" + original["id"]).json()
    assert current["template_id"] == updated["id"] and len(current["revisions"]) == 2
    assert current["extraction"]["engine"]["replay"]["recognition_model_calls"] == 0
    assert current["extraction"]["previous_id"] == detail["extraction"]["id"] and calls == ["QWEN_VISUAL"]
    with database() as db:
        assert db.get(DocumentExtraction, detail["extraction"]["id"]).payload == detail["extraction"]["payload"]


def test_profile_revision_is_immutable_across_families_and_shared_template_endpoint(authenticated, database, monkeypatch):
    _family, template = setup_profile(authenticated, monkeypatch, table=False)
    second_family = authenticated.post("/api/v1/documents/families", json={"name": "Same organization second semantic family"}).json()["id"]
    updated = profile()
    updated["description"] = "A different contract cannot overwrite this profile's first revision."
    response = authenticated.post(f"/api/v1/documents/families/{second_family}/semantic-profile", json={
        "profile": updated, "reason": "Conflicting profile revision must be refused."})
    assert response.status_code == 409 and response.json()["error"]["code"] == "DOCUMENT_SEMANTIC_PROFILE_REVISION_CONFLICT"
    response = authenticated.post(f"/api/v1/documents/families/{second_family}/templates", json=compile_profile(updated))
    assert response.status_code == 409 and response.json()["error"]["code"] == "DOCUMENT_SEMANTIC_PROFILE_REVISION_CONFLICT"
    same = authenticated.post(f"/api/v1/documents/families/{second_family}/semantic-profile", json={
        "profile": profile(), "reason": "The exact same versioned semantic contract is reusable."})
    assert same.status_code == 200, same.text
    updated["revision"] = 2
    response = authenticated.post(f"/api/v1/documents/families/{second_family}/semantic-profile", json={
        "profile": updated, "reason": "The changed semantic contract has its own revision."})
    assert response.status_code == 200, response.text
    assert response.json()["profile"]["revision"] == 2
    with database() as db:
        assert db.get(DocumentTemplate, template["id"]).definition == template["definition"]


def test_catalog_profile_identifiers_cannot_be_reused_by_custom_contracts(authenticated, monkeypatch):
    monkeypatch.setenv("TRACKVANCE_SEMANTIC_GENERAL_ENABLED", "true")
    family = authenticated.post("/api/v1/documents/families", json={"name": "Reserved catalog profile fixture"}).json()["id"]
    custom = next(value for value in profiles() if value["id"] == "invoice-v1")
    custom.update(revision=2, description="Trying to repurpose the catalog's identity.")
    response = authenticated.post(f"/api/v1/documents/families/{family}/semantic-profile", json={
        "profile": custom, "reason": "Catalog identity must remain reserved."})
    assert response.status_code == 409 and response.json()["error"]["code"] == "DOCUMENT_SEMANTIC_PROFILE_REVISION_CONFLICT"


@pytest.mark.parametrize("body", [
    {"profile_id": "invoice-v1", "profile": profile()},
    {"profile_id": "unknown-profile"},
    {"profile_id": "invoice-v1", "route": "SELECTIVE"},
])
def test_semantic_profile_input_rejects_unknown_or_unbounded_selection(authenticated, monkeypatch, body):
    monkeypatch.setenv("TRACKVANCE_SEMANTIC_GENERAL_ENABLED", "true")
    family = authenticated.post("/api/v1/documents/families", json={"name": "Invalid profile selection"}).json()["id"]
    response = authenticated.post(f"/api/v1/documents/families/{family}/semantic-profile", json={**body, "reason": "Invalid fixture selection"})
    assert response.status_code == 422
