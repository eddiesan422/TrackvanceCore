"""Synthetic adversarial controls only; no corpus/oracle or inference input."""
import hashlib
import json
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from trackvance import document_engine
from trackvance.document_builder_executor import _valid_result
from trackvance.document_builder_workspace import processor_identity
from trackvance.document_invoice_controls import invoice_arithmetic_fields, invoice_checks
from trackvance.document_quality import _arithmetic_fields, certify_candidate, quality_report
from trackvance.document_schemas import DocumentDefinition, DocumentDraftDefinition


def cell(value):
    return {"value": value, "raw": value, "validity": "VALID", "reviewed": False}


def fixture():
    header_names = ["gross", "discount", "net", "exempt", "vat5", "vat19", "total", "advance", "balance"]
    row_names = ["quantity", "price", "discount_pct", "tax_pct", "line_net"]
    definition = {"fields": [{"name": name, "logical_type": "DECIMAL"} for name in header_names],
        "tables": [{"name": "items", "row_pattern": " ".join("(?P<" + name + ">.+)" for name in row_names),
            "fields": [{"name": name, "logical_type": "STRING" if name.endswith("pct") else "DECIMAL"}
                       for name in row_names]}],
        "invoice_controls": {"name": "independent_invoice_math", "table": "items", "quantity": "quantity",
            "unit_price": "price", "discount_percent": "discount_pct", "tax_percent": "tax_pct", "line_net": "line_net",
            "tolerance": "0", "headers": {"gross_subtotal": "gross", "discount_total": "discount", "net_base": "net",
                "exempt_base": "exempt", "tax_totals": [{"rate": "5", "field": "vat5"}, {"rate": "19", "field": "vat19"}],
                "invoice_total": "total", "advance": "advance", "balance": "balance"}}}
    payload = {"fields": {name: cell(value) for name, value in zip(header_names,
        ["22.00", "2.00", "20.00", "2.00", "0.90", "0.00", "20.90", "0.90", "20.00"], strict=True)},
        "tables": {"items": [dict(zip(row_names, map(cell, values), strict=True)) for values in (
            ["2", "10", "10 %", "5%", "18.00"], ["1", "2", "0%", "0 %", "2.00"])]},
        "table_unparsed": {"items": []}}
    return payload, definition


def valid(payload, definition):
    result = invoice_checks(payload, definition)
    return bool(result) and all(item["status"] == "VALID" for item in result)


def test_synthetic_invoice_exact_math_preserves_every_cell_and_literal():
    payload, definition = fixture()
    before = deepcopy(payload)
    assert valid(payload, definition)
    assert payload == before
    assert payload["tables"]["items"][0]["discount_pct"]["value"] == "10 %"
    assert {("items", "price"), ("headers", "net"), ("headers", "balance")} <= invoice_arithmetic_fields(definition)
    assert not any("coverage" in key or "quality" in key for item in invoice_checks(payload, definition) for key in item)
    declared = DocumentDefinition.model_validate(definition).model_dump(mode="json")
    assert valid(payload, declared)


@pytest.mark.parametrize("name", ["gross", "discount", "net", "exempt", "vat5", "vat19", "total", "balance"])
def test_independently_recomputed_headers_detect_changed_observation(name):
    payload, definition = fixture()
    payload["fields"][name] = cell("999")
    assert not valid(payload, definition)


@pytest.mark.parametrize("value", [None, True, 1.0, "NaN", "Infinity", "1_000", "1e100000", "9" * 4097])
def test_unavailable_or_unsafe_numeric_observations_fail_closed(value):
    payload, definition = fixture()
    payload["tables"]["items"][0]["price"] = cell(value)
    assert invoice_checks(payload, definition)[0]["status"] == "NOT_EVALUATED"


@pytest.mark.parametrize("value", ["-1%", "101%", "0.05", "NaN%", "5%%", "5,5%", 5])
def test_percentage_source_strings_are_strict_points_not_fractions(value):
    payload, definition = fixture()
    payload["tables"]["items"][0]["tax_pct"] = cell(value)
    assert not valid(payload, definition)


@pytest.mark.parametrize("case", ["missing", "invalid", "missing_unparsed", "unparsed", "unsupported_rate", "malformed_row", "malformed_tables"])
def test_missing_unknown_or_unparsed_inputs_are_never_valid(case):
    payload, definition = fixture()
    if case == "missing":
        del payload["tables"]["items"][0]["line_net"]
    elif case == "invalid":
        payload["fields"]["total"]["validity"] = "INVALID"
    elif case == "missing_unparsed":
        payload.pop("table_unparsed")
    elif case == "unparsed":
        payload["table_unparsed"]["items"] = ["unparsed observation"]
    elif case == "unsupported_rate":
        payload["tables"]["items"][0]["tax_pct"] = cell("7%")
    elif case == "malformed_row":
        payload["tables"]["items"][0] = None
    else:
        payload["tables"] = []
    assert invoice_checks(payload, definition)[0]["status"] == "NOT_EVALUATED"


@pytest.mark.parametrize("case", ["alias", "header_alias", "missing_binding", "wrong_type", "duplicate_rate", "unsupported_rounding", "excess_tolerance"])
def test_tautological_or_invalid_declarations_never_grant_arithmetic_coverage(case):
    payload, definition = fixture()
    controls = definition["invoice_controls"]
    if case == "alias":
        controls["unit_price"] = controls["line_net"]
    elif case == "header_alias":
        controls["headers"]["invoice_total"] = controls["headers"]["net_base"]
    elif case == "missing_binding":
        controls["quantity"] = "not_declared"
    elif case == "wrong_type":
        definition["tables"][0]["fields"][0]["logical_type"] = "STRING"
    elif case == "duplicate_rate":
        controls["headers"]["tax_totals"].append({"rate": "5.00", "field": "another_vat"})
    elif case == "unsupported_rounding":
        controls["rounding_mode"] = "FLOAT"
    else:
        controls["tolerance"] = "1000"
    assert invoice_arithmetic_fields(definition) == set()
    assert invoice_checks(payload, definition)[0]["status"] == "NOT_EVALUATED"
    with pytest.raises((ValidationError, ValueError)):
        DocumentDefinition.model_validate(definition)


def test_deduction_sign_is_explicit_and_never_absolutized():
    payload, definition = fixture()
    payload["fields"]["discount"] = cell("-2.00")
    payload["fields"]["advance"] = cell("-0.90")
    assert not valid(payload, definition)
    definition["invoice_controls"].update(discount_header_sign="NEGATIVE_DEDUCTION", advance_header_sign="NEGATIVE_DEDUCTION")
    assert valid(payload, definition)


def test_coherent_negative_credit_rows_preserve_signed_arithmetic():
    payload, definition = fixture()
    for row in payload["tables"]["items"]:
        row["quantity"]["value"] = "-" + row["quantity"]["value"]
        row["line_net"]["value"] = "-" + row["line_net"]["value"]
    for observed in payload["fields"].values():
        observed["value"] = "-" + observed["value"]
    assert valid(payload, definition)
    payload["tables"]["items"][0]["price"]["value"] = "-10"
    assert not valid(payload, definition)


def test_one_missing_row_cannot_pass_unchanged_totals():
    payload, definition = fixture()
    payload["tables"]["items"].pop()
    assert not valid(payload, definition)


def test_percentage_format_is_an_explicit_checking_choice():
    payload, definition = fixture()
    definition["invoice_controls"]["percentage_decimal_separator"] = ","
    payload["tables"]["items"][0]["discount_pct"]["value"] = "10,00 %"
    assert valid(payload, definition)
    payload["tables"]["items"][0]["discount_pct"]["value"] = "10,00"
    assert not valid(payload, definition)
    definition["invoice_controls"]["string_percentage_suffix"] = "OPTIONAL"
    assert valid(payload, definition)


def test_valid_finite_inputs_with_excessive_product_span_fail_closed():
    payload, definition = fixture()
    payload["tables"]["items"][0]["quantity"] = cell("1e3000")
    payload["tables"]["items"][0]["price"] = cell("1e3000")
    checks = invoice_checks(payload, definition)
    assert checks[0]["status"] == "NOT_EVALUATED"
    assert checks[0]["reason"] == "ARITHMETIC_PRECISION_LIMIT"


def test_declared_integer_bindings_reject_fractional_or_out_of_range_cells():
    payload, definition = fixture()
    definition["tables"][0]["fields"][0]["logical_type"] = "INT64"
    assert valid(payload, definition)
    for value in ("1.5", str(2**63)):
        payload["tables"]["items"][0]["quantity"] = cell(value)
        assert invoice_checks(payload, definition)[0]["status"] == "NOT_EVALUATED"


def test_declared_rounding_stage_changes_the_expected_totals():
    payload, definition = fixture()
    base = {"quantity": cell("1"), "price": cell("0.005"), "discount_pct": cell("0%"),
            "tax_pct": cell("5%"), "line_net": cell("0.01")}
    payload["tables"]["items"] = [deepcopy(base) for _ in range(4)]
    for name in payload["fields"]:
        payload["fields"][name] = cell("0.00")
    for name in ("gross", "net", "total", "balance"):
        payload["fields"][name] = cell("0.04")
    assert valid(payload, definition)
    definition["invoice_controls"]["rounding_scope"] = "AGGREGATE"
    assert not valid(payload, definition)
    for name in ("gross", "net", "total", "balance"):
        payload["fields"][name] = cell("0.02")
    assert valid(payload, definition)


@pytest.mark.parametrize("kind", ["discount", "tax"])
def test_row_half_up_ties_for_discount_and_tax(kind):
    payload, definition = fixture()
    row = {"quantity": cell("1"), "price": cell("0.10"), "discount_pct": cell("5%" if kind == "discount" else "0%"),
           "tax_pct": cell("0%" if kind == "discount" else "5%"), "line_net": cell("0.09" if kind == "discount" else "0.10")}
    payload["tables"]["items"] = [row]
    values = {"gross": "0.10", "discount": "0.01" if kind == "discount" else "0.00",
              "net": "0.09" if kind == "discount" else "0.10", "exempt": "0.09" if kind == "discount" else "0.00",
              "vat5": "0.00" if kind == "discount" else "0.01", "vat19": "0.00",
              "total": "0.09" if kind == "discount" else "0.11", "advance": "0.00",
              "balance": "0.09" if kind == "discount" else "0.11"}
    payload["fields"] = {name: cell(value) for name, value in values.items()}
    assert valid(payload, definition)
    changed = "discount" if kind == "discount" else "vat5"
    payload["fields"][changed] = cell("0.00")
    assert not valid(payload, definition)


def test_math_cannot_prove_completeness_when_opposing_rows_are_lost():
    payload, definition = fixture()
    positive = {"quantity": cell("1"), "price": cell("10"), "discount_pct": cell("0%"),
                "tax_pct": cell("0%"), "line_net": cell("10.00")}
    negative = deepcopy(positive)
    negative["quantity"], negative["line_net"] = cell("-1"), cell("-10.00")
    payload["tables"]["items"].extend([positive, negative])
    assert valid(payload, definition)
    # The omitted pair cancels every sum. Independent measured row coverage is
    # still required by the quality gate; arithmetic has no completeness status.
    payload["tables"]["items"] = payload["tables"]["items"][:-2]
    assert valid(payload, definition)
    assert "row_coverage" not in str(invoice_checks(payload, definition))


@pytest.mark.parametrize("draft", [False, True])
def test_absent_or_null_optional_controls_preserve_historical_serialization(draft):
    payload, definition = fixture()
    definition.pop("invoice_controls")
    candidate = DocumentDraftDefinition if draft else DocumentDefinition
    before = candidate.model_validate(definition).model_dump(mode="json")
    # Fixed before invoice-controls integration using this synthetic fixture;
    # the optional field must not change historical canonical definition bytes.
    serialized = json.dumps(before, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()
    assert hashlib.sha256(serialized).hexdigest() == "5495993658c10d0a86ac5ab276edcf446d1998dcede2826db9737c35d4ab8527"
    assert candidate.model_validate(dict(definition, invoice_controls=None)).model_dump(mode="json") == before
    assert "invoice_controls" not in before
    assert invoice_checks(payload, definition) == []


def test_empty_draft_hash_and_native_structures_remain_historical():
    definition = DocumentDraftDefinition(invoice_controls=None).model_dump(mode="json")
    serialized = json.dumps(definition, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()
    assert hashlib.sha256(serialized).hexdigest() == "fff66e49c49773f650956756544757cc09d87c2aa555f45de700b8a0e33fb497"
    _, raw = fixture()
    before = DocumentDefinition.model_validate({key: value for key, value in raw.items() if key != "invoice_controls"}).model_dump(mode="json")
    after = DocumentDefinition.model_validate(raw).model_dump(mode="json")
    assert after["fields"] == before["fields"] and after["tables"] == before["tables"]
    assert after["publication"] == before["publication"]


def source_candidate(payload):
    payload = deepcopy(payload)
    payload["lines"], payload["pages"] = [], [1]
    for row in [payload["fields"], *payload["tables"]["items"]]:
        for observed in row.values():
            origin = {"page": 1, "region": [0.1, 0.1, 0.9, 0.9], "method": "DIGITAL"}
            observed.update(observed=observed["value"], origin=origin, confidence=None)
            payload["lines"].append({"text": observed["value"], **origin})
    return {"payload": payload, "engine": {"key": "SYNTHETIC_INDEPENDENT_TEST"}}


def test_common_arithmetic_reaches_both_worker_and_quality_report_without_coverage_claim():
    payload, raw = fixture()
    definition = DocumentDefinition.model_validate(raw).model_dump(mode="json")
    data, independent = source_candidate(payload), source_candidate(payload)
    proof = certify_candidate(data, independent, definition, "a" * 64)
    assert proof["arithmetic"]
    assert not proof["table_coverage"]["items"]
    assert "QUALITY_ROW_COVERAGE:items" in proof["issues"]
    assert not any("ARITHMETIC_UNDECLARED" in issue for issue in proof["issues"])
    data["engine"]["validation"] = proof
    item = SimpleNamespace(**data, reviewed=False)
    assert quality_report(item, definition)["checks"]["arithmetic"]
    assert quality_report(item, definition)["status"] == "REVIEW_REQUIRED"
    payload["fields"]["vat5"] = cell("999")
    data, independent = source_candidate(payload), source_candidate(payload)
    data["engine"]["validation"] = certify_candidate(data, independent, definition, "a" * 64)
    assert not data["engine"]["validation"]["arithmetic"]
    assert not quality_report(SimpleNamespace(**data, reviewed=False), definition)["checks"]["arithmetic"]
    assert document_engine.arithmetic_checks(payload, definition) == invoice_checks(payload, definition)


def test_invalid_raw_bindings_cannot_extend_quality_arithmetic_fields():
    payload, definition = fixture()
    definition["invoice_controls"]["unit_price"] = "line_net"
    assert _arithmetic_fields(definition) == set()
    assert document_engine.arithmetic_checks(payload, definition)[0]["status"] == "NOT_EVALUATED"


@pytest.mark.parametrize("key,value", [("scale", True), ("scale", "2"), ("scale", 13),
    ("rounding_scope", "UNDECLARED"), ("rounding_mode", "HALF_EVEN"), ("tolerance", 0.01),
    ("percentage_decimal_separator", ";"), ("string_percentage_suffix", False),
    ("discount_header_sign", "ABS"), ("advance_header_sign", "ABS"), ("unknown_option", True)])
def test_api_and_confined_raw_declarations_fail_closed_for_the_same_invalid_options(key, value):
    payload, definition = fixture()
    definition["invoice_controls"][key] = value
    with pytest.raises(ValidationError):
        DocumentDefinition.model_validate(definition)
    assert invoice_arithmetic_fields(definition) == set()
    assert invoice_checks(payload, definition)[0]["status"] == "NOT_EVALUATED"


def test_invoice_controls_cannot_replace_the_independent_cross_total_coverage_guard():
    payload, raw = fixture()
    raw["tables"][0]["visual"] = {"page": 1, "region": [0, 0, 1, 0.1],
        "header_labels": ["Quantity", "Price", "Discount", "Tax", "Net"],
        "column_bounds": [0, 0.2, 0.4, 0.6, 0.8, 1], "flowing": True,
        "row_start": {"column": 0, "segments": [
            {"alphabet": "ASCII_DIGIT", "minimum": 1, "maximum": 2},
            {"alphabet": "ASCII_DIGIT", "minimum": 1, "maximum": 2}]}}
    definition = DocumentDefinition.model_validate(raw).model_dump(mode="json")
    data, independent = source_candidate(payload), source_candidate(payload)
    proof = certify_candidate(data, independent, definition, "a" * 64)
    assert proof["arithmetic"] and not definition.get("cross_totals")
    assert proof["table_coverage"]["items"] is False
    assert "QUALITY_ROW_COVERAGE:items" in proof["issues"]


def test_arithmetic_source_is_part_of_engine_and_builder_processing_identity():
    path = Path(document_engine.__file__).with_name("document_invoice_controls.py")
    expected = hashlib.sha256(path.read_bytes()).hexdigest()
    assert document_engine.PROCESSOR_HASHES["document_invoice_controls.py"] == expected
    assert processor_identity()["document_invoice_controls.py"] == expected


@pytest.mark.parametrize("mutation", ["missing", "invented_result", "wrong_status"])
def test_builder_protocol_recomputes_new_controls_before_accepting_child_results(mutation):
    payload, definition = fixture()
    definition = DocumentDefinition.model_validate(definition).model_dump(mode="json")
    data = source_candidate(payload)
    payload = data["payload"]
    payload["arithmetic"] = document_engine.arithmetic_checks(payload, definition)
    request = {"operation": "EXTRACT", "references": [{"id": "reference", "lines": payload["lines"]}], "definition": definition}
    assert _valid_result({"reference": payload}, request)
    if mutation == "missing":
        payload.pop("arithmetic")
    elif mutation == "invented_result":
        payload["arithmetic"][0]["expected"] = "fabricated"
    else:
        payload["arithmetic"][0]["status"] = "NOT_EVALUATED"
    assert not _valid_result({"reference": payload}, request)
