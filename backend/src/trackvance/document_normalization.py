"""Declared, reproducible scalar normalization; observed source text is immutable."""
from __future__ import annotations

import hashlib
import json
import re
from decimal import ROUND_HALF_EVEN, ROUND_HALF_UP, Decimal, InvalidOperation, localcontext
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_serializer, model_validator


def canonical_hash(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
        separators=(",", ":"), allow_nan=False).encode()).hexdigest()


class DocumentNormalization(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    version: Literal[1] = 1
    numeric_semantics: Literal["SIGNED", "DEDUCTION_MAGNITUDE"] = "SIGNED"
    decimal_scale: int | None = Field(default=None, ge=0, le=18)
    rounding: Literal["EXACT", "HALF_UP", "HALF_EVEN"] = "EXACT"
    currency_equivalences: dict[str, list[str]] = Field(default_factory=dict, max_length=30)
    text_linebreaks: Literal["DECLARED_MULTILINE_V1"] | None = None

    @model_serializer(mode="wrap")
    def preserve_existing_contract(self, handler):
        result = handler(self)
        if self.text_linebreaks is None:
            result.pop("text_linebreaks", None)
        return result

    @model_validator(mode="after")
    def unambiguous_aliases(self):
        seen: dict[str, str] = {}
        for code, aliases in self.currency_equivalences.items():
            if len(code) != 3 or not code.isascii() or not code.isalpha() or code != code.upper():
                raise ValueError("La moneda requiere un código de tres letras mayúsculas.")
            if not aliases or len(aliases) > 30:
                raise ValueError("Declara entre una y treinta equivalencias por moneda.")
            for alias in [code, *aliases]:
                if not isinstance(alias, str) or not alias.strip() or len(alias) > 160:
                    raise ValueError("La equivalencia de moneda está vacía o es demasiado larga.")
                if not any(character.isalnum() for character in alias):
                    raise ValueError("Un símbolo aislado no identifica una moneda sin contexto verificable.")
                key = " ".join(alias.casefold().split())
                if key in seen and seen[key] != code:
                    raise ValueError("Una representación no puede identificar dos monedas.")
                seen[key] = code
        return self


def normalize_observation(observed, field, *, evidence_refs=None):
    """Return value, validity and an auditable trace, never a calculated source value."""
    from .document_engine import validate_value
    policy = field.get("normalization")
    policy = DocumentNormalization.model_validate(policy).model_dump() if policy is not None else None
    rules: list[str] = []
    trace = {"version": 1, "rule": "IDENTITY", "observed": observed,
        "normalized": None, "configuration_sha256": canonical_hash(policy),
        "evidence_refs": list(evidence_refs or []), "applied_rules": rules}
    value, validity = validate_value(observed, field)
    if isinstance(observed, str) and not observed.strip():
        value, validity = None, "MISSING" if field.get("required", True) else "VALID"
        trace.update(rule="EMPTY_SOURCE_CELL", normalized=None)
        return value, validity, trace
    if observed is None or not policy:
        trace["normalized"] = value
        return value, validity, trace
    if policy.get("text_linebreaks"):
        if field["logical_type"] != "STRING" or field.get("business_key") or policy["currency_equivalences"]:
            return observed, "INVALID", trace
        # Exactly the declared documentary separator is interpreted. This does
        # not apply unicode_escape decoding or repair identifiers/path strings.
        value = re.sub(r"[ \t]*(?:(?:\r\n|\n|(?<!\\)\\n)[ \t]*)+", " ", value)
        validity = "VALID" if value else "MISSING" if field.get("required", True) else "VALID"
        rules.append("DECLARED_MULTILINE_V1")
    elif policy["currency_equivalences"]:
        if field["logical_type"] != "STRING":
            return observed, "INVALID", trace
        aliases = {" ".join(alias.casefold().split()): code
            for code, names in policy["currency_equivalences"].items() for alias in [code, *names]}
        value = aliases.get(" ".join(observed.casefold().split()))
        validity = "VALID" if value is not None else "AMBIGUOUS"
        rules.append("DECLARED_CURRENCY_EQUIVALENCE")
    elif field["logical_type"] == "DECIMAL" and validity == "VALID":
        try:
            with localcontext() as context:
                context.prec = 64
                number = Decimal(value)
                if policy["numeric_semantics"] == "DEDUCTION_MAGNITUDE":
                    number = abs(number)
                    rules.append("DEDUCTION_MAGNITUDE")
                else:
                    rules.append("DECIMAL_SIGNED")
                if policy["decimal_scale"] is not None:
                    quantum = Decimal(1).scaleb(-policy["decimal_scale"])
                    rounding = ROUND_HALF_UP if policy["rounding"] == "HALF_UP" else ROUND_HALF_EVEN
                    rounded = number.quantize(quantum, rounding=rounding)
                    if policy["rounding"] == "EXACT" and rounded != number:
                        return observed, "INVALID", trace
                    number = rounded
                    rules.append("DECIMAL_SCALE_" + policy["rounding"])
                value = format(number, "f")
        except (InvalidOperation, ValueError, OverflowError):
            return observed, "INVALID", trace
    trace.update(rule=rules[0] if rules else "IDENTITY", normalized=value)
    return value, validity, trace


def normalization_matches(cell, field):
    try:
        trace = cell.get("normalization")
        if not isinstance(trace, dict) or trace.get("observed") != cell.get("observed"):
            return False
        value, status, expected = normalize_observation(cell.get("observed"), field,
            evidence_refs=cell.get("evidence_refs", []))
        return status == cell.get("validity") and value == cell.get("value") and trace == expected
    except (TypeError, ValueError, KeyError):
        return False
