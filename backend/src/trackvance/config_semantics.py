"""Versioned declarative configuration. This is the only legacy adaptation boundary."""

from __future__ import annotations

import copy
import re
from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

NO_NORMALIZATION = {"trim": False, "case": "NONE", "unicode_normalization": "NONE"}
COLUMN_TYPES = {
    "required",
    "not_null",
    "unique",
    "numeric",
    "positive",
    "type",
    "range",
    "allowed_values",
    "regex",
    "date_rule",
}
METRIC_TYPES = {
    "metric_threshold",
    "schema_type",
    "historical_band",
    "distinct_count",
    "distinct_rate",
    "uniqueness_ratio",
}


class ConfigurationError(ValueError):
    """An unsupported or ambiguous declaration cannot enter an execution plan."""


def portable_regex_pattern(parameters: dict) -> str:
    """Give shorthand classes their explicit ASCII meaning on Rust and RE2.

    Literal Unicode and Unicode property classes remain supported. Word boundary
    and complemented shorthand inside a character class are rejected because
    their Unicode interpretations differ between the two engines.
    """
    pattern = parameters["pattern"]
    classes = {"d": "0-9", "w": "A-Za-z0-9_", "s": r" \t\n\r\f"}
    result, in_class, index = [], False, 0
    while index < len(pattern):
        value = pattern[index]
        if value == "\\" and index + 1 < len(pattern):
            escaped = pattern[index + 1]
            if escaped in {"b", "B"}:
                raise ConfigurationError(
                    "Regex portable requiere límites explícitos; no admite \\b/\\B."
                )
            if escaped.lower() in classes:
                if in_class and escaped.isupper():
                    raise ConfigurationError(
                        "Regex portable no admite \\D/\\W/\\S dentro de una clase."
                    )
                content = classes[escaped.lower()]
                result.append(
                    content
                    if in_class
                    else "[" + ("^" if escaped.isupper() else "") + content + "]"
                )
            else:
                result.append(value + escaped)
            index += 2
            continue
        if value == "[":
            in_class = True
        elif value == "]":
            in_class = False
        result.append(value)
        index += 1
    flags = parameters.get("flags", "")
    return (f"(?{flags})" if flags else "") + "".join(result)


def decimal_parameter(value: Any, name: str = "tolerancia") -> Decimal:
    try:
        if not re.fullmatch(r"[+-]?\d+(?:\.\d+)?", str(value)):
            raise InvalidOperation
        number = Decimal(str(value))
        if not number.is_finite():
            raise InvalidOperation
        return number
    except (InvalidOperation, ValueError):
        raise ConfigurationError(f"{name} debe ser un decimal finito con punto.") from None


class Normalization(BaseModel):
    model_config = ConfigDict(extra="forbid")
    trim: bool = False
    case: Literal["NONE", "UPPER", "LOWER"] = "NONE"
    unicode_normalization: Literal["NONE", "NFC", "NFKC"] = "NONE"


class RuleDefinition(BaseModel):
    model_config = ConfigDict(extra="forbid")
    rule_id: str | None = None
    code: str = ""
    type: str
    scope: Literal["column", "dataset"] = "column"
    column: str | None = None
    severity: Literal["ERROR", "WARNING"] = "ERROR"
    enabled: bool = True
    parameters: dict[str, Any] = Field(default_factory=dict)
    message: str | None = None

    @model_validator(mode="after")
    def check(self) -> RuleDefinition:
        self.type = self.type.lower()
        if self.type not in COLUMN_TYPES | METRIC_TYPES:
            raise ValueError(f"Tipo de regla no soportado: {self.type}")
        if not self.code:
            self.code = self.type.upper()
        if not re.fullmatch(r"[A-Z][A-Z0-9_]{0,95}", self.code):
            raise ValueError("El code debe ser un reason code estable en mayúsculas.")
        if (
            self.type
            in COLUMN_TYPES | {"schema_type", "distinct_count", "distinct_rate", "uniqueness_ratio"}
            and not self.column
        ):
            raise ValueError("La regla necesita una columna.")
        p = self.parameters
        if p.get("null_policy", "ALLOW") not in {"ALLOW", "FAIL", "IGNORE"}:
            raise ValueError("null_policy debe ser ALLOW, FAIL o IGNORE.")
        if self.type == "range":
            limits = {k: decimal_parameter(p[k], k) for k in ("gt", "gte", "lt", "lte") if k in p}
            if not limits or ({"gt", "gte"} <= limits.keys()) or ({"lt", "lte"} <= limits.keys()):
                raise ValueError("range requiere límites inequívocos gt/gte y/o lt/lte.")
            lower, upper = limits.get("gt", limits.get("gte")), limits.get("lt", limits.get("lte"))
            if (
                lower is not None
                and upper is not None
                and (lower > upper or (lower == upper and ("gt" in limits or "lt" in limits)))
            ):
                raise ValueError("El mínimo no puede superar el máximo.")
        if self.type == "allowed_values":
            if not isinstance(p.get("values"), list) or len(p["values"]) > 1000:
                raise ValueError("allowed_values necesita una lista de hasta 1000 valores.")
            if any(isinstance(v, (dict, list)) for v in p["values"]):
                raise ValueError("Los valores permitidos deben ser escalares.")
        if self.type == "regex":
            pattern = p.get("pattern")
            flags = p.get("flags", "")
            if (
                not isinstance(pattern, str)
                or len(pattern) > 500
                or not isinstance(flags, str)
                or set(flags) - set("ims")
            ):
                raise ValueError("Regex admite patrón de hasta 500 caracteres y flags i/m/s.")
            # Native Rust/RE2 engines have linear-time matching; no Python regex execution.
            if re.search(r"\\[1-9]|\(\?[=!<]|\(\?P", pattern):
                raise ValueError("Regex portable no admite backreferences ni lookaround.")
            try:
                import duckdb
                import polars as pl

                portable = portable_regex_pattern(p)
                pl.Series([""]).str.contains(portable)
                with duckdb.connect(":memory:") as connection:
                    connection.execute("SELECT regexp_matches('', ?)", [portable])
            except Exception as exc:
                raise ValueError("Patrón regex inválido o no portable.") from exc
        if self.type == "date_rule":
            for bound in ("min", "max"):
                if bound in p:
                    try:
                        if not isinstance(p[bound], str) or not re.fullmatch(
                            r"[0-9]{4}-[0-9]{2}-[0-9]{2}", p[bound]
                        ):
                            raise ValueError
                        date.fromisoformat(p[bound])
                    except (ValueError, TypeError):
                        raise ValueError(
                            "Los límites date_rule deben ser fechas ISO YYYY-MM-DD."
                        ) from None
            if not any(k in p for k in ("not_future", "min", "max")):
                raise ValueError("date_rule requiere not_future y/o min/max.")
            if "not_future" in p and not isinstance(p["not_future"], bool):
                raise ValueError("not_future debe ser booleano.")
            if "min" in p and "max" in p and p["min"] > p["max"]:
                raise ValueError("La fecha mínima no puede superar la máxima.")
            if p.get("timezone", "UTC") != "UTC":
                raise ValueError(
                    "La política timezone disponible es UTC; timestamps requieren offset."
                )
        if self.type == "type" and p.get("logical_type") not in {
            "STRING",
            "DECIMAL",
            "INT64",
            "DATE",
            "TIMESTAMP",
            "BOOLEAN",
        }:
            raise ValueError("logical_type no soportado.")
        if (
            self.type == "schema_type"
            and p.get("expected_type") is not None
            and p["expected_type"]
            not in {"STRING", "DECIMAL", "INT64", "DATE", "TIMESTAMP", "BOOLEAN"}
        ):
            raise ValueError(
                "expected_type debe ser un tipo lógico válido; omítalo para comparar con la versión anterior."
            )
        if self.type in METRIC_TYPES - {"schema_type"}:
            for bound in ("min", "max"):
                if bound in p:
                    decimal_parameter(p[bound], bound)
            if (
                "min" in p
                and "max" in p
                and decimal_parameter(p["min"]) > decimal_parameter(p["max"])
            ):
                raise ValueError("El mínimo no puede superar el máximo.")
            if self.type == "metric_threshold":
                if not p.get("metric") or p.get("operator", "lte") not in {
                    "gt",
                    "gte",
                    "lt",
                    "lte",
                    "eq",
                }:
                    raise ValueError("metric_threshold necesita métrica y operador válido.")
                if "threshold" in p:
                    decimal_parameter(p["threshold"], "threshold")
                elif not any(k in p for k in ("min", "max")):
                    raise ValueError("metric_threshold necesita un límite.")
            if self.type == "historical_band":
                if not 2 <= p.get("min_history", 4) <= p.get("window", 10) <= 1000:
                    raise ValueError("Histórico requiere 2 ≤ min_history ≤ window ≤ 1000.")
                if decimal_parameter(p.get("iqr_multiplier", "1.5")) < 0:
                    raise ValueError("iqr_multiplier no puede ser negativo.")
        return self


def _comparison(rule: dict) -> dict:
    r = copy.deepcopy(rule)
    kind = r.get("type", r.get("code", "")).lower()
    if kind == "exact_match":
        kind = "numeric_tolerance"
        r["legacy_alias"] = "EXACT_MATCH"
    if kind not in {"exact_compare", "numeric_tolerance", "date_tolerance"}:
        raise ConfigurationError("Comparación no soportada.")
    r["type"], r["code"] = kind, kind.upper()
    if not r.get("source_column") or not r.get("target_column"):
        raise ConfigurationError("Cada comparación necesita source_column y target_column.")
    p = r.setdefault("parameters", {})
    p.setdefault("equal_nulls", False)
    if kind == "exact_compare":
        p["normalization"] = Normalization.model_validate(p.get("normalization", {})).model_dump()
    if kind == "numeric_tolerance":
        p.setdefault("abs", "0")
        p.setdefault("percent", None)
        p.setdefault("denominator", "SOURCE")
        p.setdefault("zero_denominator", "EXACT_ONLY")
        if (
            p["denominator"] not in {"SOURCE", "TARGET", "MAX_ABS"}
            or p["zero_denominator"] != "EXACT_ONLY"
        ):
            raise ConfigurationError("Política de denominador no soportada.")
        for key in ("abs", "percent"):
            if p[key] is not None:
                value = decimal_parameter(p[key])
                if value < 0:
                    raise ConfigurationError("La tolerancia no puede ser negativa.")
                p[key] = str(value)
    if kind == "date_tolerance":
        if "hours" in p and "days" in p:
            raise ConfigurationError("Declare hours o days, no ambos.")
        unit = "hours" if "hours" in p else "days"
        p[unit] = str(decimal_parameter(p.get(unit, "0")))
        if Decimal(p[unit]) < 0:
            raise ConfigurationError("La tolerancia de fecha no puede ser negativa.")
        p.setdefault("timezone", "UTC")
        if p["timezone"] != "UTC":
            raise ConfigurationError("Date tolerance requiere UTC/offset explícito.")
    return r


def effective_config(module: str, config: dict) -> dict:
    """Read adapter: does not mutate historical JSON or hashes."""
    module = module.upper()
    result = copy.deepcopy(config)
    legacy = result.get("schema_version", 1) == 1
    result.setdefault("schema_version", 1)
    result["semantics_version"] = 2
    result.setdefault("rules", [])
    if module == "INTAKE":
        result.setdefault("transforms", [])
        result["normalization_policy"] = "DECLARED_ONLY"
    if module == "RECON":
        result["key_normalization"] = Normalization.model_validate(
            result.get("key_normalization", {"trim": legacy})
        ).model_dump()
        result["key_normalization_policy"] = (
            "LEGACY_V1_TRIM_EMPTY_NULL"
            if legacy and "key_normalization" not in config
            else "EXPLICIT_V2"
        )
        if not result.get("comparison_rules") and result.get("amount_column"):
            result["comparison_rules"] = [
                {
                    "type": "numeric_tolerance",
                    "source_column": result["amount_column"],
                    "target_column": result["amount_column"],
                    "parameters": {"abs": result.get("tolerance", "0.01")},
                    "legacy_alias": "EXACT_MATCH",
                }
            ]
        result["comparison_rules"] = [_comparison(r) for r in result.get("comparison_rules", [])]
    return result


def validate_config(module: str, config: dict) -> dict:
    """Validate new publication, returning an explicit snapshot suitable for hashing."""
    result = copy.deepcopy(config)
    result["schema_version"] = 2
    result = effective_config(module, result)
    result["rules"] = [
        RuleDefinition.model_validate(r).model_dump(exclude_none=True) for r in result["rules"]
    ]
    if module.upper() == "INTAKE":
        if any(rule["type"] in METRIC_TYPES for rule in result["rules"]):
            raise ConfigurationError("Las reglas de métricas agregadas pertenecen a Sentinel.")
        if not 0 <= float(result.get("max_error_rate", 0)) <= 1:
            raise ConfigurationError("max_error_rate debe estar entre 0 y 1.")
        allowed = {
            "trim",
            "case",
            "empty_to_null",
            "unicode_normalization",
            "id_padding",
            "remove_characters",
            "decimal_parse",
            "date_parse",
        }
        for transform in result["transforms"]:
            if transform.get("type") not in allowed or not transform.get("column"):
                raise ConfigurationError("Transform no soportado o sin columna.")
            p, kind = transform.get("parameters", {}), transform["type"]
            if kind == "case" and p.get("case", "UPPER") not in {"UPPER", "LOWER"}:
                raise ConfigurationError("Transform case debe ser UPPER o LOWER.")
            if kind == "unicode_normalization" and p.get("form", "NFC") not in {"NFC", "NFKC"}:
                raise ConfigurationError("Transform Unicode debe ser NFC o NFKC.")
            if kind == "id_padding" and (
                not 1 <= p.get("width", 0) <= 1000
                or len(p.get("fill", "0")) != 1
                or p.get("side", "left") not in {"left", "right"}
            ):
                raise ConfigurationError(
                    "id_padding requiere ancho 1..1000, carácter y lado left/right."
                )
            if kind == "date_parse" and not p.get("formats"):
                raise ConfigurationError("date_parse requiere formatos explícitos.")
            if kind == "decimal_parse" and p.get("decimal_separator", ".") == p.get(
                "thousands_separator", ","
            ):
                raise ConfigurationError("Separadores decimal/miles deben ser distintos.")
    if module.upper() == "RECON":
        if not result.get("key_columns") or not result.get("comparison_rules"):
            raise ConfigurationError("Recon necesita claves y al menos una comparación.")
        aggregation = result.get("aggregation")
        if aggregation:
            if aggregation.get("side") not in {"SOURCE", "TARGET"} or aggregation.get(
                "operation"
            ) not in {"sum", "count"}:
                raise ConfigurationError("Agregación admite SOURCE/TARGET y sum/count.")
            if not aggregation.get("output_column") or (
                aggregation["operation"] == "sum" and not aggregation.get("column")
            ):
                raise ConfigurationError("Agregación requiere output_column y column para sum.")
    return result
