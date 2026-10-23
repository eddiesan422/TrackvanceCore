"""Metric regression: no shifted rows, duplicate identities or numeric coercion."""
import pytest
from idp_corpus import evaluate, failed_evaluation, percentile, summarize


def fixture():
    definition = {"fields": [{"name": "invoice_number", "logical_type": "STRING"}], "tables": [
        {"name": "detalle", "fields": [{"name": "code", "logical_type": "STRING"},
            {"name": "cantidad", "logical_type": "DECIMAL"}, {"name": "descuento_pct", "logical_type": "STRING"}]}]}
    document = {"id": "original", "family_id": "F01", "kind": "digital", "page_count": 1, "sha256": "a" * 64,
        "visible_header_fields": ["invoice_number"], "visible_line_fields": ["code", "cantidad", "descuento_pct"],
        "expected": {"invoice_number": "0001", "lines": [{"code": "A", "quantity": "2.00", "discount_pct": 0},
                                                           {"code": "B", "quantity": "3", "discount_pct": 5}]}}
    def cell(value):
        return {"value": value, "validity": "VALID"}
    payload = {"fields": {"invoice_number": cell("0001")}, "tables": {"detalle": [
        {"code": cell("A"), "cantidad": cell("2"), "descuento_pct": cell("0%")},
        {"code": cell("B"), "cantidad": cell("3.0"), "descuento_pct": cell("5%")}]}, "table_unparsed": {"detalle": []}}
    literals = [{"invoice_id": "original", "source_sha256": "a" * 64, "row_number": index,
        "code_observed_in_source": code, "descuento_pct": value}
        for index, code, value in [(1, "A", "0%"), (2, "B", "5%")]]
    return payload, document, definition, literals


def test_identity_accuracy_distinguishes_reordered_rows_from_wrong_values():
    payload, document, definition, literals = fixture()
    assert evaluate(payload, document, definition, literals)["document_exact"]
    payload["tables"]["detalle"].reverse()
    result = evaluate(payload, document, definition, literals)
    assert result["exact_rows"] == 2 and result["document_exact"]
    assert result["positional_detail_exact_cells"] == 0
    payload["tables"]["detalle"][1]["code"]["value"] = "B"
    result = evaluate(payload, document, definition, literals)
    assert result["missing_rows"] == 1 and result["duplicate_rows"] == 1
    assert result["exact_rows"] == 0 and result["critical_errors"] > 0


def test_string_identifiers_and_percentages_do_not_receive_numeric_coercion():
    payload, document, definition, literals = fixture()
    payload["fields"]["invoice_number"]["value"] = "1"
    payload["tables"]["detalle"][0]["descuento_pct"]["value"] = "0"
    result = evaluate(payload, document, definition, literals)
    assert result["cells"] == 7 and result["exact_cells"] == 5
    assert result["critical_errors"] == 2
    assert result["semantic_exact_cells"] == 6


def test_failed_documents_are_explicit_and_count_as_zero_accuracy():
    assert percentile([1, 2, 3, 4], .95) == 4
    result = summarize([{"status": "FAILED", "latency_seconds": 2, "cells": 25, "expected_rows": 3, "pages": 4}], 3)
    assert result["failed"] == 1 and result["processed"] == 0
    assert result["cell_accuracy"] == 0 and result["auto_verified"] == 0
    assert result["unprocessed_expected_rows"] == 3
    assert result["publication_measured"] is False
    assert result["pages"] == 4 and result["processed_pages"] == 0 and result["pages_per_minute"] == 0


def test_raw_definition_default_does_not_mask_failure_or_drop_its_denominators():
    _, document, definition, _ = fixture()
    document["line_count"] = 2
    del definition["tables"][0]["fields"][2]["logical_type"]
    row = failed_evaluation(document, definition, TimeoutError("Real inference timeout"))
    row["latency_seconds"] = 3
    assert row["status"] == "FAILED" and row["error_type"] == "TimeoutError"
    assert row["cells"] == 7 and row["expected_rows"] == row["literal_expected_cells"] == 2
    summary = summarize([row], 3)
    assert summary["cells"] == 7 and summary["literal_expected_cells"] == 2
    assert summary["cell_accuracy"] == 0 and summary["unprocessed_expected_rows"] == 2


def test_unknown_unique_row_is_extra_and_does_not_inflate_duplicate_count():
    payload, document, definition, literals = fixture()
    payload["tables"]["detalle"][0]["code"]["value"] = "UNKNOWN"
    result = evaluate(payload, document, definition, literals)
    assert result["extra_rows"] == result["missing_rows"] == 1
    assert result["duplicate_rows"] == 0


def test_independent_source_literal_spacing_is_preserved_separately_from_semantics():
    payload, document, definition, literals = fixture()
    literals[0]["descuento_pct"] = "0 %"
    result = evaluate(payload, document, definition, literals)
    assert result["exact_cells"] == 6 and result["semantic_exact_cells"] == 7
    assert result["primary_reference_complete"] and not result["document_exact"]
    assert result["semantic_document_exact"]
    payload["tables"]["detalle"][0]["descuento_pct"]["value"] = "0 %"
    assert evaluate(payload, document, definition, literals)["document_exact"]


def test_missing_literal_evidence_is_unverified_without_inventing_source_spelling():
    payload, document, definition, _ = fixture()
    result = evaluate(payload, document, definition)
    assert result["literal_reference_missing_cells"] == 2
    assert result["exact_cells"] == 5 and result["semantic_exact_cells"] == 7
    assert not result["document_exact"] and not result["primary_reference_complete"]
    assert result["semantic_document_exact"]


@pytest.mark.parametrize(("field", "value"), [
    ("invoice_id", "other"), ("source_sha256", "b" * 64),
    ("row_number", 2), ("code_observed_in_source", "other"),
])
def test_literal_evidence_must_match_original_document_and_row(field, value):
    payload, document, definition, literals = fixture()
    literals[0][field] = value
    with pytest.raises(ValueError, match="CORPUS_LITERAL_SOURCE_BINDING_MISMATCH"):
        evaluate(payload, document, definition, literals)


def test_incomplete_literal_row_coverage_is_rejected():
    payload, document, definition, literals = fixture()
    with pytest.raises(ValueError, match="CORPUS_LITERAL_ROW_COUNT_MISMATCH"):
        evaluate(payload, document, definition, literals[:1])


def test_source_scan_literal_evidence_can_bind_by_hash_and_row_without_claiming_code_observation():
    payload, document, definition, literals = fixture()
    for record in literals:
        del record["code_observed_in_source"]
    assert evaluate(payload, document, definition, literals)["document_exact"]


def test_wrong_description_blocks_all_cells_of_the_business_row_identity():
    payload, document, definition, literals = fixture()
    document["visible_line_fields"].append("description")
    for index, row in enumerate(document["expected"]["lines"]):
        row["description"] = "Description " + str(index)
        payload["tables"]["detalle"][index]["description"] = {"value": row["description"], "validity": "VALID"}
    payload["tables"]["detalle"][0]["description"]["value"] = "Another product"
    result = evaluate(payload, document, definition, literals)
    assert result["cells"] == 9 and result["exact_cells"] == 5
    assert result["code_aligned_exact_cells"] == result["positional_exact_cells"] == 8
    assert result["identity_failed_rows"] == 1 and result["exact_rows"] == 1


def test_multiple_unidentified_rows_are_unknown_without_invented_duplicate_identity():
    payload, document, definition, literals = fixture()
    for row in payload["tables"]["detalle"]:
        row["code"]["value"] = None
    result = evaluate(payload, document, definition, literals)
    assert result["unknown_rows"] == result["missing_rows"] == result["extra_rows"] == 2
    assert result["duplicate_rows"] == 0 and result["exact_rows"] == 0


def test_known_duplicate_unknown_code_is_counted_as_duplicate():
    payload, document, definition, literals = fixture()
    for row in payload["tables"]["detalle"]:
        row["code"]["value"] = "UNKNOWN"
    result = evaluate(payload, document, definition, literals)
    assert result["extra_rows"] == 2 and result["duplicate_rows"] == 1 and result["unknown_rows"] == 0


def test_declared_string_value_requires_string_and_invalid_cells_block_document_exact():
    payload, document, definition, literals = fixture()
    document["expected"]["invoice_number"] = "1"
    payload["fields"]["invoice_number"]["value"] = 1
    assert evaluate(payload, document, definition, literals)["exact_cells"] == 6
    payload["fields"]["invoice_number"]["value"] = "1"
    payload["fields"]["invoice_number"]["validity"] = "INVALID"
    result = evaluate(payload, document, definition, literals)
    assert result["exact_cells"] == 7 and result["invalid_cells"] == 1 and not result["document_exact"]
    assert result["table_exact"] and result["invalid_header_cells"] == 1


@pytest.mark.parametrize("value", ["NaN", "Infinity", "-Infinity", True, "2%"])
def test_declared_numeric_fields_reject_non_finite_and_non_numeric_values(value):
    payload, document, definition, literals = fixture()
    payload["tables"]["detalle"][0]["cantidad"]["value"] = value
    assert evaluate(payload, document, definition, literals)["exact_cells"] == 6


def test_failed_and_successful_documents_share_all_accuracy_denominators():
    payload, document, definition, literals = fixture()
    successful = evaluate(payload, document, definition, literals)
    successful.update(status="PROCESSED", latency_seconds=1, auto_verified=True)
    failed = {"status": "FAILED", "latency_seconds": 2, "cells": 7, "header_fields": 1,
        "expected_rows": 2, "pages": 1}
    result = summarize([successful, failed], 2)
    assert result["cells"] == 14 and result["cell_accuracy"] == result["semantic_oracle_cell_accuracy"] == .5
    assert result["header_accuracy"] == result["row_accuracy"] == .5
    assert result["correct_auto_verified"] == 1 and result["document_exact"] == 1
    assert not result["primary_reference_complete"]


@pytest.mark.parametrize("policy", [None, "AUTO_VERIFIED"])
def test_independent_eligibility_never_claims_native_approval_under_either_policy(policy):
    payload, document, definition, literals = fixture()
    if policy is not None:
        definition["publication"] = {"review_policy": policy}
    effective = policy or "ALL"
    row = evaluate(payload, document, definition, literals)
    row.update(status="PROCESSED", latency_seconds=1, auto_approval_eligible=True)
    assert row["policy_effective"] == effective and not row["native_approval_measured"]
    assert row["native_auto_approved"] is None
    report = summarize([row], 1)
    assert report["auto_approval_eligible"] == report["correct_auto_approval_eligible"] == 1
    assert report["policy_effective_counts"] == {effective: 1}
    assert not report["native_approval_measured"] and report["native_auto_approved"] is None
    assert report["auto_verified"] == 1 and report["deprecated_eligibility_aliases"]["auto_verified"] == "auto_approval_eligible"
    assert not report["publication_measured"]
