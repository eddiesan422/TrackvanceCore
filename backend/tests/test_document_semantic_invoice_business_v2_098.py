"""Explicit synthetic invoice adjustments, with source truth supplied separately.

No model calls, corpus references, inferred zero or computed cell correction.
"""
from copy import deepcopy

import pytest
from pydantic import ValidationError

from trackvance.document_normalization import canonical_hash
from trackvance.document_schemas import DocumentDefinition
from trackvance.document_semantic_business import business_report, projection
from trackvance.document_semantic_general import (
    ActivateSemanticProfile,
    SemanticProfile,
    compile_profile,
)


def fixture(monkeypatch, *, negative_deductions=False, duplicate=False):
    monkeypatch.setenv("TRACKVANCE_SEMANTIC_GENERAL_ENABLED", "true")
    monkeypatch.setenv("TRACKVANCE_SEMANTIC_AUTO_ENABLED", "true")
    headers = ["gross", "discount", "net", "exempt", "vat10", "vat20", "total", "advance", "balance"]
    roles = ["quantity", "price", "discount_pct", "tax_pct", "line_net"]
    profile = {"id": "invoice-adjustments-unit", "name": "Declared adjustments", "revision": 1,
        "document_type": "INVOICE", "description": "Every printed adjustment and tax bucket is declared explicitly.",
        "fields": [{"name": name, "label": name, "logical_type": "DECIMAL", "critical": True} for name in headers],
        "tables": [{"name": "lines", "description": "Every physical row occurrence including legitimate duplicates.",
            "fields": [{"name": name, "label": name, "logical_type": "STRING" if name.endswith("pct") else "DECIMAL"}
                for name in roles]}]}
    controls = {"version": "SEMANTIC_INVOICE_CONTROLS_V2", "scope": "FULL_DECLARED_INVOICE_WITH_ADJUSTMENTS",
        "applicability": "ALL_DECLARED_ROLES_PRINTED", "invoice_controls": {
            "name": "declared_adjusted_invoice", "table": "lines", "quantity": "quantity", "unit_price": "price",
            "discount_percent": "discount_pct", "tax_percent": "tax_pct", "line_net": "line_net",
            "percentage_decimal_separator": ",", "tolerance": "0", "headers": {
                "gross_subtotal": "gross", "discount_total": "discount", "net_base": "net", "exempt_base": "exempt",
                "tax_totals": [{"rate": "10", "field": "vat10"}, {"rate": "20", "field": "vat20"}],
                "invoice_total": "total", "advance": "advance", "balance": "balance"}}}
    if negative_deductions:
        controls["invoice_controls"].update(discount_header_sign="NEGATIVE_DEDUCTION", advance_header_sign="NEGATIVE_DEDUCTION")
    definition = compile_profile(profile, automatic_verification="SEMANTIC_AUTO_V2", business_controls=controls)

    def cell(value):
        return {"observed": value, "value": value, "validity": "VALID", "reviewed": False}

    values = ["50.00", "-5.00" if negative_deductions else "5.00", "45.00", "5.00", "2.00", "4.00", "51.00",
        "-1.00" if negative_deductions else "1.00", "50.00"]
    rows = [["2", "10.00", "0 %", "10%", "20.00"], ["1", "25.00", "20,0 %", "20 %", "20.00"],
        ["1", "5.00", "0%", "0 %", "5.00"]]
    if duplicate:
        rows.append(rows[0][:])
        values = ["70.00", "5.00", "65.00", "5.00", "4.00", "4.00", "73.00", "1.00", "72.00"]
    payload = {"fields": dict(zip(headers, map(cell, values), strict=True)),
        "tables": {"lines": [dict(zip(roles, map(cell, row), strict=True)) for row in rows]}, "table_unparsed": {"lines": []}}
    paths = {"headers.0." + name for name in headers} | {
        f"lines.{index}.{name}" for index, row in enumerate(payload["tables"]["lines"]) for name in row}
    return profile, definition, payload, paths


def report(definition, payload, paths, coverage=True):
    return business_report(payload, definition, verified_paths=paths, table_coverage={"lines": coverage})


@pytest.mark.parametrize("variant", ["adjustments", "negative_deductions", "duplicate"])
def test_complete_printed_adjustments_use_native_calculator_and_preserve_occurrences(monkeypatch, variant):
    _, definition, payload, paths = fixture(monkeypatch,
        negative_deductions=variant == "negative_deductions", duplicate=variant == "duplicate")
    before = deepcopy(payload)
    result = report(definition, payload, paths)
    assert result["version"] == "SEMANTIC_INVOICE_CONTROLS_V2" and result["applicable"] is True
    assert result["issues"] == [] and all(check["status"] == "VALID" and check["source_inputs_verified"] for check in result["checks"])
    assert len([check for check in result["checks"] if check["name"].endswith(".line_net")]) == (4 if variant == "duplicate" else 3)
    assert payload == before
    assert definition["invoice_controls"] == definition["semantic_general"]["business_controls"]["invoice_controls"]
    assert definition["arithmetic"] == [] and "cross_totals" not in definition


@pytest.mark.parametrize("missing", ["inventory", "header_proof", "row_proof", "zero", "invalid", "unparsed", "unsupported_rate"])
def test_unverified_or_missing_adjustment_is_not_a_printed_zero_or_a_proven_contradiction(monkeypatch, missing):
    _, definition, payload, paths = fixture(monkeypatch)
    payload["fields"]["total"]["value"] = "999.00"
    if missing == "header_proof":
        paths.remove("headers.0.discount")
    elif missing == "row_proof":
        paths.remove("lines.1.tax_pct")
    elif missing == "zero":
        payload["tables"]["lines"][0]["discount_pct"]["value"] = None
    elif missing == "invalid":
        payload["fields"]["advance"]["validity"] = "AMBIGUOUS"
    elif missing == "unparsed":
        payload["table_unparsed"]["lines"] = ["unassigned printed line"]
    elif missing == "unsupported_rate":
        payload["tables"]["lines"][0]["tax_pct"]["value"] = "7%"
    before = deepcopy(payload)
    result = report(definition, payload, paths, coverage=missing != "inventory")
    assert result["checks"] and all(check["status"] == "UNVERIFIED" for check in result["checks"])
    assert not any("QUALITY_BUSINESS_DISCREPANCY" in issue for issue in result["issues"])
    assert payload == before


def test_source_proven_discrepancy_is_not_waived_by_review_or_duplicated_rows(monkeypatch):
    _, definition, payload, paths = fixture(monkeypatch, duplicate=True)
    payload["fields"]["total"]["value"] = "51.00"
    for value in [*payload["fields"].values(), *(cell for row in payload["tables"]["lines"] for cell in row.values())]:
        value["reviewed"] = True
    result = report(definition, payload, paths)
    assert any(check["status"] == "DISCREPANCY" and check["source_inputs_verified"] for check in result["checks"])
    assert any("QUALITY_BUSINESS_DISCREPANCY" in issue for issue in result["issues"])
    assert len(payload["tables"]["lines"]) == 4


@pytest.mark.parametrize("mutation", ["version", "missing_role", "wrong_role", "native_binding", "hash", "auto_v1"])
def test_v2_requires_explicit_complete_exact_native_controls_and_auto_v2(monkeypatch, mutation):
    profile, definition, _, _ = fixture(monkeypatch)
    declaration = deepcopy(definition["semantic_general"]["business_controls"])
    if mutation == "version":
        declaration.pop("version")
    elif mutation == "missing_role":
        declaration["invoice_controls"].pop("discount_percent")
    elif mutation == "wrong_role":
        declaration["invoice_controls"]["tax_percent"] = "unprinted_tax"
    if mutation in {"version", "missing_role", "wrong_role", "auto_v1"}:
        auto = "SEMANTIC_AUTO_V1" if mutation == "auto_v1" else "SEMANTIC_AUTO_V2"
        with pytest.raises(ValueError):
            compile_profile(profile, automatic_verification=auto, business_controls=declaration)
        with pytest.raises(ValidationError):
            if mutation == "wrong_role":
                DocumentDefinition.model_validate({**definition,
                    "invoice_controls": {**definition["invoice_controls"], "tax_percent": "unprinted_tax"}})
            else:
                ActivateSemanticProfile.model_validate({"profile": profile, "automatic_verification": auto,
                    "business_controls": declaration, "reason": "Explicit native controls"})
    else:
        if mutation == "native_binding":
            definition["invoice_controls"]["discount_header_sign"] = "NEGATIVE_DEDUCTION"
        else:
            definition["semantic_general"]["business_controls_sha256"] = "f" * 64
        with pytest.raises(ValidationError):
            DocumentDefinition.model_validate(definition)


def test_v1_projection_retains_the_preexisting_explicit_contract():
    declaration = {"version": "SEMANTIC_INVOICE_CONTROLS_V1", "scope": "FULL_INVOICE_NO_UNREPRESENTED_ADJUSTMENTS",
        "applicability": "ALL_DECLARED_ROLES_PRINTED", "table": "items", "line_amount": "amount", "subtotal": "subtotal",
        "tax": "tax", "total": "total", "quantity": None, "unit_price": None, "scale": 2, "tolerance": "0.01"}
    assert projection(declaration) == (declaration, [{"name": "semantic_invoice_subtotal_tax_total_v1", "table": None,
        "operation": "SUM", "terms": ["subtotal", "tax"], "subtract": [], "result": "total", "scale": 2, "tolerance": "0.01"}],
        [{"name": "semantic_invoice_detail_subtotal_v1", "table": "items", "column": "amount", "header": "subtotal", "scale": 2, "tolerance": "0.01"}])


def test_numeric_fragments_preserves_a_literal_preexisting_profile_contract_and_hash():
    old = {"schema_version": "SEMANTIC_GENERAL_V1", "id": "old-decimal-contract", "name": "Old decimal contract",
        "revision": 1, "document_type": "CUSTOM", "description": "Preexisting scalar-only profile serialization.",
        "fields": [{"name": "amount", "label": "Amount", "description": "", "logical_type": "DECIMAL", "required": True,
            "critical": False, "business_key": False, "date_format": "%Y-%m-%d", "decimal_separator": ".",
            "thousands_separator": None, "currency_symbols": [], "section": "DOCUMENT", "normalization": None}], "tables": []}
    assert SemanticProfile.model_validate(old).model_dump(mode="json") == old
    assert canonical_hash(old) == "0332d868358b5167e7bba1710115c1c6cf6886fd8debaf11542fab156f097f23"
    explicit_false = deepcopy(old)
    explicit_false["fields"][0]["numeric_fragments"] = False
    assert SemanticProfile.model_validate(explicit_false).model_dump(mode="json") == old
    assert compile_profile(old)["semantic_general"]["profile_sha256"] == canonical_hash(old)
    new = deepcopy(old)
    new["fields"][0]["numeric_fragments"] = True
    compiled = compile_profile(new)
    assert compiled["fields"][0]["numeric_fragments"] is True
    assert compiled["semantic_general"]["profile_sha256"] != canonical_hash(old)
    for logical_type in ["STRING", "INT64", "DATE", "BOOLEAN"]:
        new["fields"][0]["logical_type"] = logical_type
        with pytest.raises(ValidationError):
            SemanticProfile.model_validate(new)
