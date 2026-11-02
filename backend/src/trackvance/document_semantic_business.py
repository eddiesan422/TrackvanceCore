"""Declared business coherence for new semantic AUTO invoices, never extraction.

These controls reuse the native arithmetic evaluators. They neither establish
source truth nor create an unprinted subtotal, tax, quantity or adjustment.
"""
from __future__ import annotations

from decimal import Decimal
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .document_normalization import canonical_hash

VERSION: Literal["SEMANTIC_INVOICE_CONTROLS_V1"] = "SEMANTIC_INVOICE_CONTROLS_V1"
Identifier = Annotated[str, Field(pattern=r"^[A-Za-z][A-Za-z0-9_]{0,62}$")]


class SemanticInvoiceControls(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    version: Literal["SEMANTIC_INVOICE_CONTROLS_V1"] = VERSION
    scope: Literal["FULL_INVOICE_NO_UNREPRESENTED_ADJUSTMENTS"]
    applicability: Literal["ALL_DECLARED_ROLES_PRINTED"]
    table: Identifier
    line_amount: Identifier
    subtotal: Identifier
    tax: Identifier
    total: Identifier
    quantity: Identifier | None = None
    unit_price: Identifier | None = None
    scale: int = Field(default=2, ge=0, le=12)
    tolerance: str = Field(default="0.01", min_length=1, max_length=96,
        pattern=r"^(?:0|[1-9][0-9]*)(?:\.[0-9]{1,12})?$")

    @model_validator(mode="after")
    def coherent_roles(self):
        if ((self.quantity is None) != (self.unit_price is None) or self.table == "headers"
                or len({self.subtotal, self.tax, self.total}) != 3
                or len({value for value in (self.line_amount, self.quantity, self.unit_price) if value is not None})
                    != 1 + (2 if self.quantity is not None else 0)
                or Decimal(self.tolerance) > Decimal(1).scaleb(-self.scale)):
            raise ValueError("Declare distinct invoice roles, paired quantity/price and a bounded rounding tolerance.")
        return self


def projection(controls):
    value = SemanticInvoiceControls.model_validate(controls).model_dump(mode="json")
    arithmetic = [{"name": "semantic_invoice_subtotal_tax_total_v1", "table": None,
        "operation": "SUM", "terms": [value["subtotal"], value["tax"]], "subtract": [],
        "result": value["total"], "scale": value["scale"], "tolerance": value["tolerance"]}]
    if value["quantity"] is not None:
        arithmetic.append({"name": "semantic_invoice_row_product_v1", "table": value["table"],
            "operation": "PRODUCT", "terms": [value["quantity"], value["unit_price"]], "subtract": [],
            "result": value["line_amount"], "scale": value["scale"], "tolerance": value["tolerance"]})
    totals = [{"name": "semantic_invoice_detail_subtotal_v1", "table": value["table"],
        "column": value["line_amount"], "header": value["subtotal"], "scale": value["scale"], "tolerance": value["tolerance"]}]
    return value, arithmetic, totals


def proposal(profile):
    """Offer a declaration tied to a compatible schema; never activate its scope."""
    if profile.get("document_type") != "INVOICE":
        return None
    headers = {field["name"]: field for field in profile["fields"]}
    table = next((table for table in profile.get("tables", []) if table["name"] == "items"), None)
    rows = {field["name"]: field for field in table["fields"]} if table else {}
    required = [(headers, name) for name in ("subtotal", "tax", "total")]
    required.extend((rows, name) for name in ("amount", "quantity", "unit_price"))
    if any(name not in fields or fields[name]["logical_type"] not in {"DECIMAL", "INT64"} for fields, name in required):
        return None
    controls, _, _ = projection({"version": VERSION, "scope": "FULL_INVOICE_NO_UNREPRESENTED_ADJUSTMENTS",
        "applicability": "ALL_DECLARED_ROLES_PRINTED", "table": "items", "line_amount": "amount",
        "subtotal": "subtotal", "tax": "tax", "total": "total", "quantity": "quantity", "unit_price": "unit_price"})
    return {"profile_sha256": canonical_hash(profile), "controls": controls,
        "controls_sha256": canonical_hash(controls), "explicit_scope_confirmation_required": True}


def validate_business_metadata(definition):
    """Validate the immutable declaration and its exact native rule projection."""
    metadata = definition.semantic_general or {}
    controls = metadata.get("business_controls")
    if controls is None:
        return
    value, arithmetic, totals = projection(controls)
    if (metadata.get("automatic_verification") not in {"SEMANTIC_AUTO_V1", "SEMANTIC_AUTO_V2"}
            or metadata.get("contract", {}).get("document_type") != "INVOICE"
            or metadata.get("business_controls_sha256") != canonical_hash(value)):
        raise ValueError("Invoice business controls require their versioned semantic AUTO contract and hash.")
    table = next((item for item in definition.tables if item.name == value["table"]), None)
    headers = {field.name: field for field in definition.fields}
    rows = {field.name: field for field in table.fields} if table is not None else {}
    roles = [(headers, value[name]) for name in ("subtotal", "tax", "total")]
    roles.extend((rows, value[name]) for name in ("line_amount", "quantity", "unit_price") if value[name] is not None)
    if any(name not in fields or fields[name].logical_type not in {"DECIMAL", "INT64"} for fields, name in roles):
        raise ValueError("Invoice business roles must bind declared numeric fields in their exact scopes.")
    if (value["quantity"] is None and all(name in rows and rows[name].logical_type in {"DECIMAL", "INT64"}
            for name in ("quantity", "unit_price"))):
        raise ValueError("The declared invoice quantity and unit price require their row-product control.")
    actual_arithmetic = [item.model_dump(mode="json") for item in definition.arithmetic]
    actual_totals = [item.model_dump(mode="json") for item in definition.cross_totals]
    if any(rule not in actual_arithmetic for rule in arithmetic) or any(rule not in actual_totals for rule in totals):
        raise ValueError("The invoice business declaration requires its unchanged native arithmetic controls.")


def business_report(payload, definition, *, verified_paths, table_coverage):
    """A sum is source-confirmed only after its inputs and inventory are proven."""
    from .document_engine import arithmetic_checks, cross_table_checks

    metadata = definition.get("semantic_general") or {}
    invoice = metadata.get("contract", {}).get("document_type") == "INVOICE"
    if metadata.get("automatic_verification") not in {"SEMANTIC_AUTO_V1", "SEMANTIC_AUTO_V2"} or not invoice:
        return {"version": VERSION, "required": False, "applicable": True, "checks": [], "issues": []}
    controls = metadata.get("business_controls")
    if controls is None:
        return {"version": VERSION, "required": True, "applicable": False, "checks": [],
            "issues": ["QUALITY_BUSINESS_SCOPE_REQUIRED"]}
    try:
        value, expected_arithmetic, expected_totals = projection(controls)
        if metadata.get("business_controls_sha256") != canonical_hash(value):
            raise ValueError("BUSINESS_CONTROL_HASH")
        if (any(rule not in definition.get("arithmetic", []) for rule in expected_arithmetic)
                or any(rule not in definition.get("cross_totals", []) for rule in expected_totals)):
            raise ValueError("BUSINESS_CONTROL_PROJECTION")
    except (ValueError, TypeError):
        return {"version": VERSION, "required": True, "applicable": False, "checks": [],
            "issues": ["QUALITY_BUSINESS_CONTRACT_REQUIRED"]}
    checks = []
    findings = []
    # Native evaluators report a computed result even for a partial table. A
    # partial inventory must be labelled UNVERIFIED, never a source contradiction.
    arithmetic = arithmetic_checks(payload, definition)
    totals = cross_table_checks(payload, definition)
    arithmetic_rules = {rule["name"]: rule for rule in definition.get("arithmetic", [])}
    total_rules = {rule["name"]: rule for rule in definition.get("cross_totals", [])}
    for check in [*arithmetic, *totals]:
        name = check["name"]
        if name in total_rules:
            rule = total_rules[name]
            rows = payload.get("tables", {}).get(rule["table"], [])
            required = {"headers.0." + rule["header"], *(
                f"{rule['table']}.{index}.{rule['column']}" for index in range(len(rows)))}
            source_ready = bool(table_coverage.get(rule["table"]) and rows and required <= verified_paths)
        else:
            rule = arithmetic_rules.get(name)
            if rule is None:
                # Invoice-controls results have their own native declaration.
                required = set()
                source_ready = False
            else:
                scope = rule.get("table") or "headers"
                row = check.get("row", 0) if scope != "headers" else 0
                required = {f"{scope}.{row}.{field}" for field in [rule["result"], *rule["terms"], *rule.get("subtract", [])]}
                source_ready = required <= verified_paths
        result = {**check, "computed_status": check["status"], "source_inputs_verified": source_ready,
            "status": check["status"] if source_ready else "UNVERIFIED"}
        checks.append(result)
        if result["status"] != "VALID":
            findings.append("QUALITY_BUSINESS_" + result["status"] + ":" + name + ":" + str(check.get("row", "ALL")))
    if not checks:
        findings.append("QUALITY_BUSINESS_SOURCE_SCOPE_UNVERIFIED")
    return {"version": VERSION, "required": True, "scope": value["scope"],
        "controls_sha256": canonical_hash(value), "applicable": bool(checks) and all(check["source_inputs_verified"] for check in checks),
        "checks": checks, "issues": findings}
