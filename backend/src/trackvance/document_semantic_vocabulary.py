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
V2_SHA256 = canonical_hash({"version": V2_VERSION, "roles": ROLE_CAPTIONS,
    "sections": SECTION_CAPTIONS,
    "decorators": {"tax": "EXPLICIT_PERCENTAGE_CAPTION_V1"},
    "association": "WHOLE_CAPTION_OWNER_TYPED_INVALID_RETAINED_V2"})


def role_captions(field: dict[str, Any]) -> list[str]:
    return list(dict.fromkeys(str(value) for value in [field.get("label"),
        str(field.get("name", "")).replace("_", " "),
        *ROLE_CAPTIONS.get(field.get("name", ""), []), *field.get("aliases", [])] if value))


def caption_role(text: str) -> str | None:
    """Recognize a complete caption; embedded words never identify its owner."""
    if any(not (char.isalnum() or char.isspace() or char in ".:#()-_/%=") for char in text):
        return None  # Invalid value punctuation must not disappear as a caption.
    key = label_key(text.rstrip(" :="))
    roles = {role for role, labels in ROLE_CAPTIONS.items()
        if key in {label_key(label) for label in labels}}
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
