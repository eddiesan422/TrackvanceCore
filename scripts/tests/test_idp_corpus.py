"""Metric regression: no shifted rows, duplicate identities or numeric coercion."""
from idp_corpus import evaluate, percentile, summarize


def fixture():
    definition = {"fields": [{"name": "invoice_number", "logical_type": "STRING"}], "tables": [
        {"name": "detalle", "fields": [{"name": "code", "logical_type": "STRING"},
            {"name": "cantidad", "logical_type": "DECIMAL"}, {"name": "descuento_pct", "logical_type": "STRING"}]}]}
    document = {"id": "original", "family_id": "F01", "kind": "digital", "page_count": 1,
        "visible_header_fields": ["invoice_number"], "visible_line_fields": ["code", "cantidad", "descuento_pct"],
        "expected": {"invoice_number": "0001", "lines": [{"code": "A", "quantity": "2.00", "discount_pct": 0},
                                                           {"code": "B", "quantity": "3", "discount_pct": 5}]}}
    def cell(value):
        return {"value": value, "validity": "VALID"}
    payload = {"fields": {"invoice_number": cell("0001")}, "tables": {"detalle": [
        {"code": cell("A"), "cantidad": cell("2"), "descuento_pct": cell("0%")},
        {"code": cell("B"), "cantidad": cell("3.0"), "descuento_pct": cell("5%")}]}, "table_unparsed": {"detalle": []}}
    return payload, document, definition


def test_identity_accuracy_distinguishes_reordered_rows_from_wrong_values():
    payload, document, definition = fixture()
    assert evaluate(payload, document, definition)["document_exact"]
    payload["tables"]["detalle"].reverse()
    result = evaluate(payload, document, definition)
    assert result["exact_rows"] == 2 and result["document_exact"]
    assert result["positional_detail_exact_cells"] == 0
    payload["tables"]["detalle"][1]["code"]["value"] = "B"
    result = evaluate(payload, document, definition)
    assert result["missing_rows"] == 1 and result["duplicate_rows"] == 1
    assert result["exact_rows"] == 0 and result["critical_errors"] > 0


def test_string_identifiers_and_percentages_do_not_receive_numeric_coercion():
    payload, document, definition = fixture()
    payload["fields"]["invoice_number"]["value"] = "1"
    payload["tables"]["detalle"][0]["descuento_pct"]["value"] = "0"
    result = evaluate(payload, document, definition)
    assert result["cells"] == 7 and result["exact_cells"] == 5
    assert result["critical_errors"] == 2


def test_failed_documents_are_explicit_and_count_as_zero_accuracy():
    assert percentile([1, 2, 3, 4], .95) == 4
    result = summarize([{"status": "FAILED", "latency_seconds": 2, "cells": 25, "expected_rows": 3, "pages": 4}], 3)
    assert result["failed"] == 1 and result["processed"] == 0
    assert result["cell_accuracy"] == 0 and result["auto_verified"] == 0
    assert result["unprocessed_expected_rows"] == 3
    assert result["publication_measured"] is False
    assert result["pages"] == 4 and result["processed_pages"] == 0 and result["pages_per_minute"] == 0


def test_unknown_unique_row_is_extra_and_does_not_inflate_duplicate_count():
    payload, document, definition = fixture()
    payload["tables"]["detalle"][0]["code"]["value"] = "UNKNOWN"
    result = evaluate(payload, document, definition)
    assert result["extra_rows"] == result["missing_rows"] == 1
    assert result["duplicate_rows"] == 0
