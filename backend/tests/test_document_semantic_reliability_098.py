"""Current-revision reliability, using source-attested synthetic V2 fixtures."""
import copy
import json
from types import SimpleNamespace

import pytest
import test_document_semantic_corroboration_v2_098 as fixtures

from trackvance.document_normalization import canonical_hash
from trackvance.document_semantic_evidence import semantic_review_dto
from trackvance.document_semantic_reliability import POLICY_SHA256
from trackvance.document_validation_policy import semantic_quality_report


@pytest.fixture(autouse=True)
def explicit_synthetic_activation(monkeypatch):
    monkeypatch.setenv("TRACKVANCE_SEMANTIC_GENERAL_ENABLED", "true")
    monkeypatch.setenv("TRACKVANCE_SEMANTIC_AUTO_ENABLED", "true")


def candidate(monkeypatch, **options):
    definition, configuration, events = fixtures.v2_fixture(monkeypatch, **options)
    data, _context, _proof = fixtures.assess(definition, configuration, events)
    return SimpleNamespace(payload=data["payload"], engine=data["engine"], reviewed=False), definition


def report(extraction, definition):
    before = copy.deepcopy(extraction.__dict__)
    quality = semantic_quality_report(extraction, definition)
    value = semantic_review_dto(extraction, definition)["reliability"]
    assert value["document"]["status"] == quality["status"]
    assert value["document"]["reasons"] == quality["issues"]
    assert extraction.__dict__ == before
    return value


@pytest.mark.parametrize("native,paddle", [(True, False), (False, True)])
def test_complete_independent_inventory_has_current_cells_rows_and_policy_binding(monkeypatch, native, paddle):
    extraction, definition = candidate(monkeypatch, native=native, paddle=paddle)
    result = report(extraction, definition)
    assert result["version"] == "SEMANTIC_RELIABILITY_V1"
    assert result["policy_sha256"] == POLICY_SHA256
    assert result["calibration"] == {"status": "PENDING"}
    assert result["binding"]["payload_sha256"] == canonical_hash(extraction.payload)
    assert result["binding"]["definition_sha256"] == canonical_hash(definition)
    assert result["binding"]["source_attestation_sha256"] == extraction.engine["validation"]["attestation_sha256"]
    assert result["document"]["status"] == "AUTO_VERIFIED"
    assert result["document"]["pending_cells"] == 0
    assert result["tables"][0]["verified_rows"] == 1
    assert result["tables"][0]["rows"][0]["source_occurrences"]
    assert not any(key in json.dumps(result).lower() for key in ("probability", "recognition_events", "content_b64"))


def test_changed_cell_retains_other_cells_but_cannot_inherit_old_row_alignment(monkeypatch):
    extraction, definition = candidate(monkeypatch)
    original = report(extraction, definition)
    extraction.payload["tables"]["items"][0]["amount"]["value"] = "999.00"
    result = report(extraction, definition)
    assert result["binding"]["payload_sha256"] != original["binding"]["payload_sha256"]
    assert result["document"]["status"] == "REVIEW_REQUIRED"
    assert result["document"]["verified_cells"] > 0
    changed = next(value for value in result["cells"] if value["path"] == "items.0.amount")
    assert changed["status"] == "PENDING" and not changed["source_refs"]
    table = result["tables"][0]
    assert not table["current_table_binding"] and table["pending_rows"] == 1
    assert table["rows"][0]["source_occurrences"] == []


@pytest.mark.parametrize("change", ["missing", "extra"])
def test_row_count_changes_do_not_inherit_coverage_or_create_approval(monkeypatch, change):
    extraction, definition = candidate(monkeypatch)
    rows = extraction.payload["tables"]["items"]
    if change == "missing":
        rows.clear()
    else:
        rows.append(copy.deepcopy(rows[0]))
    result = report(extraction, definition)
    assert result["document"]["status"] == "REVIEW_REQUIRED"
    assert result["tables"][0]["coverage_state"] == "PENDING"
    assert not result["tables"][0]["current_table_binding"]


def test_partial_source_keeps_verified_cells_and_blocks_the_document(monkeypatch):
    definition, configuration, events = fixtures.scalar_only_fixture(partial=True)
    data, _context, _proof = fixtures.assess(definition, configuration, events)
    extraction = SimpleNamespace(payload=data["payload"], engine=data["engine"], reviewed=False)
    result = report(extraction, definition)
    assert result["document"]["verified_cells"] == 2
    assert result["document"]["status"] == "REVIEW_REQUIRED"
    assert result["document"]["gates"]["text"]["status"] == "PARTIAL"


def test_unchanged_authorized_confirmation_preserves_source_and_row_binding(monkeypatch):
    extraction, definition = candidate(monkeypatch)
    extraction.reviewed = True
    extraction.payload["tables"]["items"][0]["amount"]["reviewed"] = True
    result = report(extraction, definition)
    assert result["document"]["status"] == "HUMAN_VERIFIED"
    assert result["document"]["reviewed_cells"] == 1
    assert result["tables"][0]["current_table_binding"]
    assert result["tables"][0]["reviewed_rows"] == 1
    assert result["tables"][0]["pending_rows"] == 0
    assert result["tables"][0]["rows"][0]["source_occurrences"]
    amount = next(value for value in result["cells"] if value["path"] == "items.0.amount")
    assert amount["source_verified"] and amount["association_verified"] and amount["source_refs"]


def test_retained_alternative_records_are_counted_once_without_erasing_provenance(monkeypatch):
    extraction, definition = candidate(monkeypatch)
    cell = extraction.payload["fields"]["invoice_number"]
    literal = {"observed": "OTHER-REFERENCE", "page": 1, "ambiguous": True}
    cell["alternatives"] = [literal, literal]
    cell["candidate_alternatives"] = [literal, {**literal, "page": 2}]
    result = report(extraction, definition)
    reference = next(value for value in result["cells"] if value["path"] == "headers.0.invoice_number")
    assert reference["alternatives_retained"] == 2
    assert not reference["association_verified"] and reference["source_refs"] == []


def test_optional_column_absence_is_exposed_without_becoming_a_required_value(monkeypatch):
    extraction, definition = candidate(monkeypatch)
    result = report(extraction, definition)
    optional = next(value for value in result["cells"] if value["path"] == "items.0.item_code")
    assert optional["required"] is False and optional["applicability"] == "OPTIONAL_NOT_OBSERVED"
    assert optional["status"] == "VERIFIED_AUTOMATICALLY" and not optional["review_required"]
    assert result["document"]["status"] == "AUTO_VERIFIED"


def test_native_api_exposes_pending_calibration_real_association_and_retained_alternatives(authenticated, monkeypatch):
    from test_document_semantic_verification_replay_v2_098 import activate_v2, v1_original

    _family, _template, original, _before, calls = v1_original(authenticated, monkeypatch,
        "reliability-api", activation=activate_v2)
    assert calls == ["QWEN_VISUAL"]
    response = authenticated.get(f"/api/v1/documents/originals/{original['id']}")
    assert response.status_code == 200
    detail = response.json()
    result = detail["semantic_review"]["reliability"]
    assert result["calibration"] == {"status": "PENDING"}
    assert result["document"]["status"] == detail["quality"]["status"]
    reference = next(value for value in result["cells"] if value["path"] == "headers.0.reference")
    assert reference["association_verified"] and reference["association_basis"] == ["PDF_NATIVE_LABEL_VALUE"]
    assert reference["source_refs"]
    stored = detail["extraction"]["payload"]["fields"]["reference"]
    retained = [*stored.get("alternatives", []), *stored.get("candidate_alternatives", [])]
    assert reference["alternatives_retained"] == len({canonical_hash(value) for value in retained})
    assert "content_b64" not in json.dumps(result)
