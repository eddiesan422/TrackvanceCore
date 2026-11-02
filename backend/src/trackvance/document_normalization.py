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
    decimal_formats: Literal["UNAMBIGUOUS_LOCALE_V1"] | None = None
    date_formats: list[str] | None = Field(default=None, min_length=1, max_length=12)

    @model_serializer(mode="wrap")
    def preserve_existing_contract(self, handler):
        result = handler(self)
        for key in ("text_linebreaks", "decimal_formats", "date_formats"):
            if getattr(self, key) is None:
                result.pop(key, None)
        return result

    @model_validator(mode="after")
    def unambiguous_aliases(self):
        if self.date_formats is not None:
            supported = {"%Y-%m-%d", "%Y/%m/%d", "%Y.%m.%d", "%d/%m/%Y", "%m/%d/%Y",
                "%d-%m-%Y", "%m-%d-%Y", "%d.%m.%Y", "%m.%d.%Y"}
            if len(set(self.date_formats)) != len(self.date_formats) or set(self.date_formats) - supported:
                raise ValueError("Declara formatos de fecha completos, compatibles y sin duplicados.")
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


def _declared_scalar_formats(observed, field, policy, validate_value):
    """Enumerate declared grammars; accept only a single distinct scalar meaning.

    Format order is never a preference. A lexical 1,234 or 01/02/2026 can retain
    two meanings and must not be resolved from document identity or an oracle.
    """
    candidates = []
    if field["logical_type"] == "DECIMAL" and policy.get("decimal_formats"):
        lexical = observed.replace("\u00a0", " ").replace("\u202f", " ").strip()
        sign = ""
        if lexical.startswith(("-", "+")):
            sign, lexical = lexical[0], lexical[1:].strip()
        symbols = sorted(field.get("currency_symbols", []), key=len, reverse=True)
        prefix = next((symbol for symbol in symbols if lexical.startswith(symbol)), None)
        suffix = next((symbol for symbol in symbols if lexical.endswith(symbol)), None)
        affix = None
        if prefix and not suffix:
            lexical = lexical[len(prefix):].strip()
            affix = {"position": "PREFIX", "literal": prefix}
        elif suffix and not prefix:
            lexical = lexical[:-len(suffix)].strip()
            affix = {"position": "SUFFIX", "literal": suffix}
        lexical = sign + lexical
        formats = [{"decimal_separator": decimal, "thousands_separator": thousands}
            for decimal, thousands in [(".", None), (",", None), (".", ","), (",", "."), (".", " "), (",", " ")]]
        for grammar in formats:
            value, status = validate_value(lexical, {**field, **grammar, "currency_symbols": [], "decimal_format_explicit": True,
                "numeric_fragments": False})
            if status == "VALID":
                candidates.append({"value": value, "format": {**grammar,
                    **({"currency_affix": affix} if affix else {})}})
        meanings = {Decimal(item["value"]) for item in candidates}
    elif field["logical_type"] == "DATE" and policy.get("date_formats"):
        for grammar in policy["date_formats"]:
            value, status = validate_value(observed, {**field, "date_format": grammar})
            if status == "VALID":
                candidates.append({"value": value, "format": {"date_format": grammar}})
        meanings = {item["value"] for item in candidates}
    else:
        return None
    if len(meanings) == 1:
        value = format(next(iter(meanings)), "f") if field["logical_type"] == "DECIMAL" else next(iter(meanings))
        return value, "VALID", candidates
    return observed, "AMBIGUOUS" if meanings else "INVALID", candidates


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
    scalar_formats = _declared_scalar_formats(observed, field, policy, validate_value)
    if scalar_formats is not None:
        value, validity, candidates = scalar_formats
        trace["format_candidates"] = candidates
        rules.append("DECLARED_DECIMAL_FORMAT_CONSENSUS_V1" if field["logical_type"] == "DECIMAL"
            else "DECLARED_DATE_FORMAT_CONSENSUS_V1")
        if validity != "VALID":
            trace.update(rule=rules[0], normalized=value)
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
