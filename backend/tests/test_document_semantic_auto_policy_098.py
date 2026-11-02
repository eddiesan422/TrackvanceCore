"""Synthetic policy/lifecycle checks; these do not measure OCR/model quality."""
import copy
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from pydantic import ValidationError
from test_document_semantic_general_098 import profile

from trackvance.document_normalization import canonical_hash
from trackvance.document_schemas import DocumentDefinition
from trackvance.document_semantic_evidence import (
    record_table_reviews,
    semantic_review_dto,
    semantic_review_issues,
)
from trackvance.document_semantic_general import (
    assert_replay_binding,
    catalog,
    compile_profile,
    configuration_for,
)
from trackvance.document_service import extraction_dto, issues
from trackvance.document_validation_policy import cell_hash, lossless_quality_report
from trackvance.operations_common import OperationError


def auto_definition(monkeypatch, *, table=False):
    monkeypatch.setenv("TRACKVANCE_SEMANTIC_GENERAL_ENABLED", "true")
    monkeypatch.setenv("TRACKVANCE_SEMANTIC_AUTO_ENABLED", "true")
    return compile_profile(profile(table=table), automatic_verification="SEMANTIC_AUTO_V1")


def normalized(value):
    return {"observed": value, "value": value, "validity": "VALID", "reviewed": False,
        "origin": {"page": 1, "region": [0.1, 0.1, 0.3, 0.2]}, "confidence": 99,
        "evidence_refs": [{"sha256": "b" * 64}]}


def policy_fixture(monkeypatch, *, table=False, verified=None, coverage=True):
    definition = auto_definition(monkeypatch, table=table)
    payload = {"fields": {"reference": normalized("R-001"), "amount": normalized("12.50")},
        "tables": {"items": [{"description": normalized("Service"), "amount": normalized("12.50")}]} if table else {},
        "table_unparsed": {"items": []} if table else {}}
    extraction = SimpleNamespace(payload=payload, engine={"source_sha256": "a" * 64,
        "lossless_contract_version": 1, "execution": {"recognition_complete": True,
            "resources_released": True, "pending_pages": []}}, reviewed=False)
    all_cells = {f"headers.0.{name}": cell for name, cell in payload["fields"].items()}
    all_cells.update({f"items.{index}.{name}": cell for index, row in enumerate(payload["tables"].get("items", []))
        for name, cell in row.items()})
    selected = set(all_cells) if verified is None else set(verified)
    proof = {"validator_version": "TRACKVANCE_SEMANTIC_EVIDENCE_098_V1", "durable_source_bound": True,
        "source_sha256": "a" * 64, "definition_sha256": canonical_hash(definition),
        "independent_extraction": True, "attestation_sha256": "c" * 64, "global_issues": [],
        "verified_cells": {path: cell_hash(cell) for path, cell in all_cells.items() if path in selected},
        "table_coverage": {"items": coverage} if table else {},
        "table_hashes": {"items": canonical_hash([{name: {key: value for key, value in cell.items() if key != "reviewed"}
            for name, cell in row.items()} for row in payload["tables"]["items"]])} if table else {},
        "corroboration": {path: {"status": "VERIFIED", "reasons": ["MEASURED_SOURCE_ASSOCIATION"],
            "mechanisms": ["QWEN_VISUAL", "NATIVE"], "source_refs": [], "association": "LABEL_VALUE"}
            for path in selected}}
    # This test isolates policy overlays after the authenticator boundary. The
    # actual registry/blob/forgery checks are exercised by corroboration tests.
    import trackvance.document_semantic_corroboration as corroboration

    monkeypatch.setattr(corroboration, "trusted_semantic_proof", lambda *_: copy.deepcopy(proof))
    return definition, extraction, proof


def test_auto_requires_separate_explicit_opt_in_and_preserves_legacy_hashes(monkeypatch):
    monkeypatch.setenv("TRACKVANCE_SEMANTIC_GENERAL_ENABLED", "true")
    monkeypatch.delenv("TRACKVANCE_SEMANTIC_AUTO_ENABLED", raising=False)
    legacy = compile_profile(profile())
    assert catalog()["automatic_verification_enabled"] is False
    assert "automatic_verification" not in legacy["semantic_general"]
    assert "association_contract" not in legacy["semantic_general"]
    assert "validation_policy" not in legacy
    before = canonical_hash(legacy)
    with pytest.raises(OperationError, match="activación explícita"):
        compile_profile(profile(), automatic_verification="SEMANTIC_AUTO_V1")
    automatic = auto_definition(monkeypatch)
    assert automatic["validation_policy"]["version"] == "SEMANTIC_EVIDENCE_V1"
    assert automatic["publication"]["review_policy"] == "AUTO_VERIFIED"
    assert automatic["semantic_general"]["profile_sha256"] == legacy["semantic_general"]["profile_sha256"]
    assert canonical_hash(compile_profile(profile())) == before
    configuration = configuration_for(automatic, {})
    assert configuration["automatic_verification"] == "BOUNDED_LOCAL_V1"
    assert configuration["max_retries"] == 0
    with pytest.raises(OperationError) as rejected:
        assert_replay_binding(legacy, automatic, configuration)
    assert rejected.value.code == "DOCUMENT_SEMANTIC_REPLAY_CONTRACT"


@pytest.mark.parametrize("change", ["metadata", "policy", "source_bound_shortcut"])
def test_auto_cannot_be_forged_by_relabeling_an_old_contract(monkeypatch, change):
    value = auto_definition(monkeypatch)
    if change == "metadata":
        value["semantic_general"].pop("automatic_verification")
    elif change == "policy":
        value["validation_policy"]["version"] = "EVIDENCE_V2"
    else:
        value["validation_policy"]["allow_noncritical_source_bound"] = True
    with pytest.raises(ValidationError):
        DocumentDefinition.model_validate(value)


def test_valid_but_uncorroborated_value_stays_pending_by_path_and_human_only_reviews_exception(monkeypatch):
    definition, extraction, _proof = policy_fixture(monkeypatch, verified={"headers.0.reference"})
    assert issues(extraction, definition) == ["REVIEW_REQUIRED:headers.0.amount"]
    dto = semantic_review_dto(extraction, definition)
    assert dto["values"][0]["state"] == "VERIFIED_AUTOMATICALLY"
    assert dto["values"][0]["verified"] == "R-001"
    assert dto["values"][1]["review_required"] is True
    assert dto["automatic_approval"] is False
    extraction.reviewed = True
    extraction.payload["fields"]["amount"]["reviewed"] = True
    assert issues(extraction, definition) == []
    assert not extraction.payload["fields"]["reference"]["reviewed"]
    assert lossless_quality_report(extraction, definition)["status"] == "HUMAN_VERIFIED"
    assert semantic_review_dto(extraction, definition)["automatic_approval"] is False


def test_changing_a_previously_proven_value_invalidates_its_proof(monkeypatch):
    definition, extraction, _proof = policy_fixture(monkeypatch)
    assert lossless_quality_report(extraction, definition)["status"] == "AUTO_VERIFIED"
    extraction.payload["fields"]["amount"]["value"] = "99.00"
    assert "REVIEW_REQUIRED:headers.0.amount" in issues(extraction, definition)
    assert semantic_review_dto(extraction, definition)["values"][1]["state"] != "VERIFIED_AUTOMATICALLY"


def test_human_correction_labels_prior_corroboration_as_invalidated_without_mutating_proof(monkeypatch):
    definition, extraction, proof = policy_fixture(monkeypatch)
    proof["candidate_sha256"] = canonical_hash(extraction.payload)
    proof["corroboration"]["headers.0.amount"]["source_refs"] = [{"artifact_sha256": "e" * 64, "page": 1}]
    original_proof = copy.deepcopy(proof)
    unchanged = semantic_review_dto(extraction, definition)["values"][1]
    assert unchanged["corroboration"]["status"] == "VERIFIED"
    cell = extraction.payload["fields"]["amount"]
    cell.update(value="99.00", reviewed=True, corrected=True)
    extraction.reviewed = True
    current = semantic_review_dto(extraction, definition)["values"][1]
    assert current["state"] == "AUTHORIZED_CORRECTION" and current["verified"] == "99.00"
    evidence = current["corroboration"]
    assert evidence["status"] == "INVALIDATED"
    assert "CURRENT_CELL_DIFFERS_FROM_ATTESTED_VALUE" in evidence["reasons"]
    assert evidence["prior_proof"]["candidate_sha256"] == original_proof["candidate_sha256"]
    assert evidence["prior_proof"]["cell_sha256"] == original_proof["verified_cells"]["headers.0.amount"]
    assert evidence["source_refs"][0]["proof_scope"] == "PRIOR_ATTESTED_CANDIDATE"
    assert evidence["source_refs"][0]["artifact_sha256"] == "e" * 64
    assert proof == original_proof


def test_table_edit_invalidates_coverage_and_mixed_receipt_requires_only_unproven_cell(monkeypatch):
    definition, extraction, _proof = policy_fixture(monkeypatch, table=True)
    assert semantic_review_dto(extraction, definition)["tables"][0]["coverage_state"] == "AUTOMATICALLY_VERIFIED"
    row = extraction.payload["tables"]["items"][0]
    row["description"].update(value="Corrected service", observed="Corrected service", reviewed=True)
    extraction.reviewed = True
    assert "SEMANTIC_TABLE_COVERAGE_REVIEW_REQUIRED:items" in issues(extraction, definition)
    from trackvance.document_semantic_evidence import semantic_verification_state

    state = semantic_verification_state(extraction, definition)
    record_table_reviews(extraction.payload, definition, ["items"], source_sha256="a" * 64,
        actor_id="reviewer", reason="Checked changed row occurrence and complete source", created_at="2026-11-02T00:00:00Z",
        automatic_verification=state)
    assert not row["amount"]["reviewed"]
    assert semantic_review_issues(extraction.payload, definition, reviewed=True, source_sha256="a" * 64,
        automatic_verification=state) == []
    assert issues(extraction, definition) == []
    assert semantic_review_dto(extraction, definition)["tables"][0]["coverage_state"] == "AUTHORIZED_REVIEW"


def test_human_flags_cannot_waive_a_global_integrity_failure(monkeypatch):
    definition, extraction, proof = policy_fixture(monkeypatch)
    proof["global_issues"] = ["SEMANTIC_SOURCE_INTEGRITY"]
    extraction.reviewed = True
    for cell in extraction.payload["fields"].values():
        cell["reviewed"] = True
    assert "SEMANTIC_SOURCE_INTEGRITY" in issues(extraction, definition)
    assert lossless_quality_report(extraction, definition)["status"] == "REVIEW_REQUIRED"


def test_public_detail_keeps_proof_identifiers_and_preserves_private_source_material():
    engine = {"validation": {"attestation_sha256": "a" * 64,
        "automatic_candidate": {"payload": {"private": "full immutable candidate"}},
        "source_context": {"source_sha256": "b" * 64, "recognition_events": [
            {"event_sha256": "c" * 64, "payload": {"artifacts": [{"content_b64": "private"}]}}]}}}
    item = SimpleNamespace(id="extraction", revision=1, previous_id=None, payload={}, payload_hash="d" * 64,
        engine=engine, reviewed=False, actor_id="worker", reason="Source validation", created_at=datetime.now(UTC),
        artifact_id="immutable-extraction-artifact")
    original = copy.deepcopy(engine)
    dto = extraction_dto(item)
    proof = dto["engine"]["validation"]
    assert "automatic_candidate" not in proof and "recognition_events" not in proof["source_context"]
    assert proof["attestation_sha256"] == "a" * 64
    assert proof["proof_material"]["extraction_artifact_id"] == "immutable-extraction-artifact"
    assert proof["proof_material"]["recognition_event_hashes"] == ["c" * 64]
    assert item.engine == original
    legacy = copy.deepcopy(item)
    legacy.engine = {"validation": {"validator_version": "TRACKVANCE_DOCUMENT_EVIDENCE_098_V2", "issues": []}}
    assert extraction_dto(legacy)["engine"] == legacy.engine
