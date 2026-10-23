"""Declared invoice arithmetic, never visual completeness.

ROW is a documented accounting choice: round row gross, then its discount,
subtract to obtain net, then round row tax. AGGREGATE retains exact row values
until each header aggregate is rounded; printed line nets are still compared at
the declared scale. Neither convention is inferred from a model or source.
No extracted cell is created, corrected or mutated by this module.
"""
from __future__ import annotations

import re
from decimal import ROUND_HALF_UP, Decimal, DecimalException, Inexact, localcontext
from types import SimpleNamespace

PRECISION_LIMIT = 4096
MAX_ROWS = 10000
IDENTIFIER = r"[A-Za-z][A-Za-z0-9_]{0,62}"
ROW_ROLES = ("quantity", "unit_price", "discount_percent", "tax_percent", "line_net")
HEADER_ROLES = ("gross_subtotal", "discount_total", "net_base", "exempt_base", "invoice_total", "advance", "balance")
DEFAULTS = {"rounding_scope": "ROW", "rounding_mode": "ROUND_HALF_UP", "scale": 2, "tolerance": "0.01",
    "percentage_decimal_separator": ".", "string_percentage_suffix": "REQUIRED",
    "discount_header_sign": "POSITIVE_DEDUCTION", "advance_header_sign": "POSITIVE_DEDUCTION"}


class ParsedHeaders(SimpleNamespace):
    def bound_names(self):
        return [*(getattr(self, name) for name in HEADER_ROLES), *(item.field for item in self.tax_totals)]


class ParsedControls(SimpleNamespace):
    def row_names(self):
        return [getattr(self, name) for name in ROW_ROLES]


def _identifier(value):
    if not isinstance(value, str) or not re.fullmatch(IDENTIFIER, value):
        raise ValueError("Invoice bindings require declared field identifiers.")
    return value


def parse_controls(raw):
    """Independently bound the child's untrusted JSON without app dependencies."""
    required = {"name", "table", "headers", *ROW_ROLES}
    if not isinstance(raw, dict) or not required <= raw.keys() or raw.keys() - required - DEFAULTS.keys():
        raise ValueError("Invoice controls contain missing or unknown keys.")
    if not isinstance(raw["name"], str) or not 1 <= len(raw["name"]) <= 100:
        raise ValueError("Invoice control names must be bounded.")
    values = {**DEFAULTS, **raw}
    for name in ("table", *ROW_ROLES):
        _identifier(values[name])
    if (type(values["scale"]) is not int or not 0 <= values["scale"] <= 12
            or values["rounding_scope"] not in ("ROW", "AGGREGATE")
            or values["rounding_mode"] != "ROUND_HALF_UP"
            or values["percentage_decimal_separator"] not in (".", ",")
            or values["string_percentage_suffix"] not in ("REQUIRED", "OPTIONAL")
            or any(values[name] not in ("POSITIVE_DEDUCTION", "NEGATIVE_DEDUCTION")
                   for name in ("discount_header_sign", "advance_header_sign"))):
        raise ValueError("Invoice arithmetic options are unsupported.")
    tolerance = values["tolerance"]
    if (not isinstance(tolerance, str) or not 1 <= len(tolerance) <= 96
            or not re.fullmatch(r"(?:0|[1-9][0-9]*)(?:\.[0-9]{1,12})?", tolerance)
            or Decimal(tolerance) > Decimal(1).scaleb(-values["scale"])):
        raise ValueError("Invoice tolerance exceeds its bounded declared scale.")
    headers = raw["headers"]
    if (not isinstance(headers, dict) or not set(HEADER_ROLES) <= headers.keys()
            or headers.keys() - set(HEADER_ROLES) - {"tax_totals"}):
        raise ValueError("Invoice header roles are required and bounded.")
    for name in HEADER_ROLES:
        _identifier(headers[name])
    tax_totals = headers.get("tax_totals", [])
    if not isinstance(tax_totals, list) or len(tax_totals) > 30:
        raise ValueError("Invoice rate buckets must be bounded.")
    taxes = []
    for tax in tax_totals:
        if not isinstance(tax, dict) or set(tax) != {"rate", "field"}:
            raise ValueError("Invoice rate buckets require explicit bindings.")
        rate = tax["rate"]
        if (not isinstance(rate, str) or not 1 <= len(rate) <= 96
                or not re.fullmatch(r"(?:0|[1-9][0-9]*)(?:\.[0-9]+)?", rate)
                or not 0 <= Decimal(rate) <= 100):
            raise ValueError("Invoice rates must be explicit percentage points0..100.")
        taxes.append(SimpleNamespace(rate=rate, field=_identifier(tax["field"])))
    values["headers"] = ParsedHeaders(**{**headers, "tax_totals": taxes})
    control = ParsedControls(**values)
    row_names = [name.casefold() for name in control.row_names()]
    header_names = [name.casefold() for name in control.headers.bound_names()]
    rates = [Decimal(item.rate) for item in taxes]
    if (control.table.casefold() == "headers" or len(set(row_names)) != len(row_names)
            or len(set(header_names)) != len(header_names) or len(set(rates)) != len(rates)):
        raise ValueError("Invoice arithmetic roles and numerical tax buckets must be distinct.")
    return control


def validated_invoice_controls(definition):
    raw = definition.get("invoice_controls")
    if raw is None:
        return None
    control = parse_controls(raw)
    table = next((table for table in definition["tables"] if table["name"] == control.table), None)
    rows = {field["name"]: field for field in table["fields"]} if table else {}
    headers = {field["name"]: field for field in definition["fields"]}
    numeric = {"DECIMAL", "INT64"}
    for name in (control.quantity, control.unit_price, control.line_net):
        if rows.get(name, {}).get("logical_type", "STRING") not in numeric:
            raise ValueError("Invoice numeric row bindings require declared numeric fields.")
    for name in (control.discount_percent, control.tax_percent):
        if rows.get(name, {}).get("logical_type", "STRING") not in numeric | {"STRING"} or name not in rows:
            raise ValueError("Invoice percentage bindings require declared numeric or STRING fields.")
    if any(headers.get(name, {}).get("logical_type", "STRING") not in numeric for name in control.headers.bound_names()):
        raise ValueError("Invoice header bindings require distinct declared numeric fields.")
    return control


def invoice_arithmetic_fields(definition):
    """Declared arithmetic roles only; this never establishes row coverage."""
    try:
        control = validated_invoice_controls(definition)
        if control is None:
            return set()
        return {(control.table, name) for name in control.row_names()} | {
            ("headers", name) for name in control.headers.bound_names()}
    except (AttributeError, KeyError, TypeError, ValueError):
        return set()


class ArithmeticUnavailable(ValueError):
    def __init__(self, reason, path=None):
        self.reason, self.path = reason, path


def _guard(values, scale, count=1):
    if not all(value.is_finite() for value in values):
        raise ArithmeticUnavailable("NON_FINITE_VALUE")
    high = max(0, *(value.adjusted() for value in values))
    low = min(-scale, *(value.as_tuple().exponent for value in values))
    if high - low + len(str(count)) + 3 > PRECISION_LIMIT:
        raise ArithmeticUnavailable("ARITHMETIC_PRECISION_LIMIT")


def _decimal(raw, scale):
    if type(raw) not in (str, int, Decimal):
        raise ArithmeticUnavailable("NON_DECIMAL_VALUE")
    text = str(raw)
    if len(text) > PRECISION_LIMIT:
        raise ArithmeticUnavailable("ARITHMETIC_PRECISION_LIMIT")
    if not re.fullmatch(r"[+-]?(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)(?:[eE][+-]?[0-9]+)?", text):
        raise ArithmeticUnavailable("NON_DECIMAL_VALUE")
    try:
        value = Decimal(text)
    except DecimalException:
        raise ArithmeticUnavailable("NON_DECIMAL_VALUE") from None
    _guard([value], scale)
    return value


def _cell(row, name, field_type, control, path, percentage=False):
    if not isinstance(row, dict):
        raise ArithmeticUnavailable("INVALID_OR_MISSING_ROW", path)
    cell = row.get(name)
    if not isinstance(cell, dict) or cell.get("validity") != "VALID" or cell.get("value") is None:
        raise ArithmeticUnavailable("INVALID_OR_MISSING_CELL", path)
    raw = cell["value"]
    if percentage and field_type == "STRING":
        if not isinstance(raw, str) or len(raw) > PRECISION_LIMIT:
            raise ArithmeticUnavailable("INVALID_PERCENTAGE_LITERAL", path)
        separator = re.escape(control.percentage_decimal_separator)
        suffix = r"\s*%" + ("?" if control.string_percentage_suffix == "OPTIONAL" else "")
        if not re.fullmatch(r"[+-]?[0-9]+(?:" + separator + r"[0-9]+)?" + suffix, raw.strip()):
            raise ArithmeticUnavailable("INVALID_PERCENTAGE_LITERAL", path)
        raw = raw.strip().removesuffix("%").strip().replace(control.percentage_decimal_separator, ".")
    try:
        value = _decimal(raw, control.scale)
        if field_type == "INT64" and (value != value.to_integral_value() or not -(2**63) <= value < 2**63):
            raise ArithmeticUnavailable("INVALID_INTEGER_VALUE", path)
        if percentage and not 0 <= value <= 100:
            raise ArithmeticUnavailable("PERCENTAGE_OUT_OF_RANGE", path)
        return value
    except ArithmeticUnavailable as error:
        error.path = error.path or path
        raise


def _product(values, scale):
    high = sum(value.adjusted() for value in values) + len(values) - 1
    low = sum(value.as_tuple().exponent for value in values)
    if (max(0, high) - min(-scale, low) + 3 > PRECISION_LIMIT
            or sum(len(value.as_tuple().digits) for value in values) > PRECISION_LIMIT):
        raise ArithmeticUnavailable("ARITHMETIC_PRECISION_LIMIT")
    result = Decimal(1)
    for value in values:
        result *= value
    _guard([result], scale)
    return result


def _sum(values, scale):
    _guard([Decimal(0), *values], scale, len(values))
    result = sum(values, Decimal(0))
    _guard([result], scale)
    return result


def _round(value, control):
    _guard([value], control.scale)
    with localcontext() as rounding:
        rounding.traps[Inexact] = False
        return value.quantize(Decimal(1).scaleb(-control.scale), rounding=ROUND_HALF_UP)


def invoice_checks(payload, definition):
    """Return arithmetic checks only. VALID never means complete or publishable."""
    if definition.get("invoice_controls") is None:
        return []
    base = {"operation": "INVOICE_CONTROLS", "name": "invoice_controls"}
    try:
        control = validated_invoice_controls(definition)
        base.update(name=control.name, table=control.table, scale=control.scale,
            rounding=control.rounding_mode, rounding_scope=control.rounding_scope)
        table = next(table for table in definition["tables"] if table["name"] == control.table)
        types = {field["name"]: field.get("logical_type", "STRING") for field in table["fields"]}
        header_types = {field["name"]: field.get("logical_type", "STRING") for field in definition["fields"]}
        rows = payload.get("tables", {}).get(control.table)
        if not isinstance(rows, list) or not 1 <= len(rows) <= MAX_ROWS:
            raise ArithmeticUnavailable("EMPTY_OR_UNBOUNDED_TABLE")
        unparsed = payload.get("table_unparsed", {}).get(control.table)
        if not isinstance(unparsed, list) or unparsed:
            raise ArithmeticUnavailable("UNPARSED_ROWS_PRESENT_OR_UNKNOWN")
        totals = control.headers
        with localcontext() as context:
            context.prec = PRECISION_LIMIT
            context.traps[Inexact] = True
            observed = {name: _cell(payload.get("fields", {}), name, header_types[name], control, "headers.0." + name)
                for name in totals.bound_names()}
            rates = {Decimal(item.rate): item.field for item in totals.tax_totals}
            gross_values, discounts, nets, exempt, checks = [], [], [], [], []
            taxes: dict[Decimal, list[Decimal]] = {rate: [] for rate in rates}
            tolerance = Decimal(control.tolerance)

            def compare(kind, expected, actual, **metadata):
                _guard([expected, actual, tolerance], control.scale)
                checks.append({**base, "name": control.name + "." + kind, **metadata,
                    "status": "VALID" if abs(actual - expected) <= tolerance else "DISCREPANCY",
                    "expected": str(expected), "observed": str(actual)})

            for index, row in enumerate(rows):
                values = {name: _cell(row, name, types[name], control, f"{control.table}.{index}.{name}",
                    name in {control.discount_percent, control.tax_percent}) for name in control.row_names()}
                rate, discount_rate = values[control.tax_percent], values[control.discount_percent]
                if rate != 0 and rate not in rates:
                    raise ArithmeticUnavailable("UNSUPPORTED_TAX_RATE", f"{control.table}.{index}.{control.tax_percent}")
                gross = _product([values[control.quantity], values[control.unit_price]], control.scale)
                if control.rounding_scope == "ROW":
                    gross = _round(gross, control)
                discount = _product([gross, discount_rate.scaleb(-2)], control.scale)
                if control.rounding_scope == "ROW":
                    discount = _round(discount, control)
                net = _sum([gross, -discount], control.scale)
                tax = _product([net, rate.scaleb(-2)], control.scale)
                if control.rounding_scope == "ROW":
                    tax = _round(tax, control)
                compare("line_net", _round(net, control), values[control.line_net], row=index, field=control.line_net)
                gross_values.append(gross)
                discounts.append(discount)
                nets.append(net)
                if rate == 0:
                    exempt.append(net)
                if rate in taxes:
                    taxes[rate].append(tax)
            expected = {totals.gross_subtotal: _round(_sum(gross_values, control.scale), control),
                totals.discount_total: _round(_sum(discounts, control.scale), control),
                totals.net_base: _round(_sum(nets, control.scale), control),
                totals.exempt_base: _round(_sum(exempt, control.scale), control),
                **{field: _round(_sum(taxes[rate], control.scale), control) for rate, field in rates.items()}}
            if control.discount_header_sign == "NEGATIVE_DEDUCTION":
                expected[totals.discount_total] = -expected[totals.discount_total]
            expected[totals.invoice_total] = _round(_sum([expected[totals.net_base], *(expected[field] for field in rates.values())], control.scale), control)
            advance = observed[totals.advance]
            if control.advance_header_sign == "NEGATIVE_DEDUCTION":
                advance = -advance
            expected[totals.balance] = _round(_sum([expected[totals.invoice_total], -advance], control.scale), control)
            for name, value in expected.items():
                compare("header", value, observed[name], field=name, rows=len(rows))
        return checks
    except ArithmeticUnavailable as error:
        return [{**base, "status": "NOT_EVALUATED", "reason": error.reason, "path": error.path}]
    except (Inexact, DecimalException):
        return [{**base, "status": "NOT_EVALUATED", "reason": "ARITHMETIC_PRECISION_LIMIT"}]
    except (AttributeError, KeyError, TypeError, ValueError):
        return [{**base, "status": "NOT_EVALUATED", "reason": "INVALID_INVOICE_BINDINGS_OR_PAYLOAD"}]
