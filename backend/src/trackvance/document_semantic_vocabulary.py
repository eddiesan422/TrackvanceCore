"""Declared bilingual role vocabulary for opt-in verification V2 only.

These are meanings of captions, never values, layouts or provider identities.
V1 keeps its own immutable vocabulary and regeneration path.
"""
from __future__ import annotations

import re
from typing import Any

from .document_header_association import label_key
from .document_normalization import canonical_hash

V2_VERSION = "SEMANTIC_SOURCE_ASSOCIATION_V2"
ROLE_CAPTIONS: dict[str, list[str]] = {
    "invoice_number": ["invoice number", "invoice no", "invoice #", "número de factura", "numero factura", "factura no"],
    "issuer": ["issuer", "supplier", "vendor", "seller", "issued by", "emisor", "proveedor", "vendedor", "emitido por"],
    "buyer": ["buyer", "customer", "client", "bill to", "sold to", "comprador", "cliente", "adquirente", "facturar a"],
    "issue_date": ["issue date", "invoice date", "date issued", "fecha de emisión", "fecha de emision", "fecha factura"],
    "currency": ["currency", "moneda", "divisa"],
    "subtotal": ["subtotal", "sub total", "net amount", "net subtotal", "base imponible"],
    "tax": ["tax", "taxes", "vat", "iva", "impuesto", "impuestos"],
    "tax_rate": ["tax rate", "vat rate", "tipo impositivo", "porcentaje de impuesto"],
    "total": ["total", "total due", "amount due", "grand total", "total a pagar", "importe total", "total factura", "total facturado"],
    "description": ["description", "item description", "concept", "details", "descripción", "descripcion", "concepto", "detalle", "artículo", "articulo", "detalle del movimiento"],
    "item_code": ["item code", "product code", "code", "sku", "código", "codigo", "clave", "ref.", "referencia artículo"],
    "quantity": ["quantity", "qty", "cantidad", "cant", "unid."],
    "unit_price": ["unit price", "unit cost", "rate", "amount per unit", "precio unitario", "p. unitario", "importe unitario", "unitario", "precio", "tarifa"],
    "amount": ["amount", "line amount", "line total", "importe", "importe de línea", "importe con signo", "valor", "monto"],
    "account": ["account", "account number", "cuenta", "número de cuenta"],
    "holder": ["holder", "account holder", "titular"],
    "period": ["period", "statement period", "reporting period", "período", "periodo", "período del extracto"],
    "opening_balance": ["opening balance", "initial balance", "balance at start of period", "opening balance for period", "saldo inicial", "saldo al inicio del período", "saldo al inicio del periodo"],
    "closing_balance": ["closing balance", "ending balance", "balance at end of period", "closing balance for period", "saldo final", "saldo final del período", "saldo final del periodo"],
    "balance": ["balance", "saldo"],
    "date": ["date", "transaction date", "fecha", "fecha movimiento"],
    "certificate_id": ["certificate id", "certificate number", "identificador del certificado", "número de certificado"],
    "subject": ["subject", "certified subject", "persona o entidad certificada", "sujeto"],
    "statement": ["statement", "certification", "certifica", "afirmación certificada"],
}
SECTION_CAPTIONS = {"TOTALS": ["totals", "totales", "amount summary", "resumen de importes"]}
MONETARY_ROLES = ("subtotal", "tax", "total", "opening_balance", "closing_balance", "balance", "amount", "unit_price")
CAPTION_CURRENCY_CODES = ("USD", "COP", "EUR", "GBP", "CHF")
_V2_ENTRY_DESCRIPTOR: dict[str, Any] = {"version": V2_VERSION, "roles": ROLE_CAPTIONS,
    "sections": SECTION_CAPTIONS,
    "decorators": {"tax": "EXPLICIT_PERCENTAGE_CAPTION_V1"},
    "association": "WHOLE_CAPTION_OWNER_TYPED_INVALID_RETAINED_ALIAS_SPAN_OWNERSHIP_V2"}
V2_ENTRY_SHA256 = canonical_hash(_V2_ENTRY_DESCRIPTOR)
V2_SHA256 = canonical_hash({**_V2_ENTRY_DESCRIPTOR,
    "decorators": {**_V2_ENTRY_DESCRIPTOR["decorators"], "monetary_roles": MONETARY_ROLES,
        "currency_codes": CAPTION_CURRENCY_CODES, "currency_table_policy": "DECLARED_RIGHT_VALUE_SAME_CURRENCY_SCOPE"},
    "native_monetary_pairs": "MEASURED_COMPOUND_SAME_ROLE_CONTRADICTION",
    "structural_escalation": "UNPROVEN_PRINTED_SCOPE_WITHIN_PHYSICAL_CAPACITY"})
V2_PRE_06B_SHA256 = V2_SHA256
ROLE_CAPTION_EXTENSIONS = {
    "total": ["total a cobrar", "importe a cobrar", "total to collect"],
    "account": ["identificación de cuenta", "identificacion de cuenta", "account identification"],
    "holder": ["titular registrado", "registered holder"],
}
V2_SHA256 = canonical_hash({"previous_assessment_sha256": V2_PRE_06B_SHA256,
    "caption_extensions": ROLE_CAPTION_EXTENSIONS,
    "html_value_relation": "SINGLE_CAPTION_SINGLE_VALUE_ACROSS_EXPLICIT_EMPTY_CELLS_V1",
    "native_page_counter": "MEASURED_FOOTER_EXACT_RECEIVED_PAGE_COUNTER_V1"})


def role_captions(field: dict[str, Any]) -> list[str]:
    return list(dict.fromkeys(str(value) for value in [field.get("label"),
        str(field.get("name", "")).replace("_", " "),
        *ROLE_CAPTIONS.get(field.get("name", ""), []),
        *ROLE_CAPTION_EXTENSIONS.get(field.get("name", ""), []), *field.get("aliases", [])] if value))


def caption_currency_qualifier(text: str) -> tuple[str, str] | None:
    """Split a declared currency code from a caption with no value delimiter."""
    match = re.fullmatch(r"(.+?)(?:\s+([A-Za-z]{3})|\s*\(\s*([A-Za-z]{3})\s*\))", text.strip())
    if match is None:
        return None
    caption, code = match[1].rstrip(), (match[2] or match[3]).upper()
    if caption.endswith((":", "=")) or code not in CAPTION_CURRENCY_CODES:
        return None
    return caption, code


def caption_role(text: str) -> str | None:
    """Recognize a complete caption; embedded words never identify its owner."""
    if any(not (char.isalnum() or char.isspace() or char in ".:#()-_/%=") for char in text):
        return None  # Invalid value punctuation must not disappear as a caption.
    key = label_key(text.rstrip(" :="))
    roles = {role for role, labels in ROLE_CAPTIONS.items()
        if key in {label_key(label) for label in [*labels, *ROLE_CAPTION_EXTENSIONS.get(role, [])]}}
    qualifier = caption_currency_qualifier(text)
    if qualifier:
        base, _ = qualifier
        roles.update(role for role in MONETARY_ROLES
            if label_key(base) in {label_key(label) for label in [*ROLE_CAPTIONS[role], *ROLE_CAPTION_EXTENSIONS.get(role, [])]})
    # A percentage printed with a tax caption describes that caption, not money.
    tax = re.fullmatch(r"(.+?)\s*\(?\s*\d+(?:[.,]\d+)?\s*%\s*\)?", text.strip().rstrip(":="))
    if tax and label_key(tax[1]) in {label_key(label) for label in ROLE_CAPTIONS["tax"]}:
        roles.add("tax")
    return next(iter(roles)) if len(roles) == 1 else None


def section_role(text: str) -> str | None:
    role = caption_role(text)
    if role in {"issuer", "buyer"}:
        return role.upper()
    return next((name for name, labels in SECTION_CAPTIONS.items()
        if label_key(text) in {label_key(label) for label in labels}), None)
