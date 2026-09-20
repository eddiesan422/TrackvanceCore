"""Versioned declarative configuration. This is the only legacy adaptation boundary."""

from __future__ import annotations

import copy
import re
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from typing import Any, Literal
from uuid import uuid4

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
    "compound_unique",
    "length",
    "column_compare",
    "reference",
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
        if len(str(value)) > 4096 or not re.fullmatch(r"[+-]?\d+(?:\.\d+)?", str(value)):
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
    when: dict[str, Any] | None = None

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
            in (COLUMN_TYPES - {"compound_unique", "reference"})
            | {"schema_type", "distinct_count", "distinct_rate", "uniqueness_ratio"}
            and not self.column
        ):
            raise ValueError("La regla necesita una columna.")
        p = self.parameters
        if self.rule_id is not None and not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", self.rule_id):
            raise ValueError("rule_id debe ser una identidad estable de hasta 64 caracteres.")
        if self.when is not None:
            validate_condition(self.when)
            if self.type in METRIC_TYPES:
                raise ValueError("Las condiciones por registro no aplican a métricas agregadas.")
        if self.type in {"compound_unique", "reference"}:
            columns = p.get("columns", [self.column] if self.column else [])
            validate_column_list(columns)
            if self.type == "compound_unique" and len(columns) < 2:
                raise ValueError("La unicidad compuesta necesita al menos dos columnas.")
            p["columns"] = columns
        if self.type == "reference":
            if (
                not isinstance(p.get("dataset_version_id"), str)
                or not 1 <= len(p["dataset_version_id"]) <= 64
            ):
                raise ValueError("La referencia requiere un DatasetVersion inmutable.")
            validate_column_list(p.get("reference_columns"))
            if len(p["columns"]) != len(p["reference_columns"]):
                raise ValueError(
                    "Las columnas de origen y referencia deben corresponder una a una."
                )
        if self.type == "length":
            if not any(k in p for k in ("min", "max")):
                raise ValueError("length necesita min y/o max.")
            for bound in ("min", "max"):
                if bound in p and (
                    isinstance(p[bound], bool)
                    or not isinstance(p[bound], int)
                    or not 0 <= p[bound] <= 1000000
                ):
                    raise ValueError("Los límites de longitud deben ser enteros entre 0 y 1000000.")
            if p.get("min", 0) > p.get("max", 1000000):
                raise ValueError("La longitud mínima no puede superar la máxima.")
        if self.type == "column_compare":
            if (
                not isinstance(p.get("other_column"), str)
                or not p["other_column"]
                or len(p["other_column"]) > 240
            ):
                raise ValueError("column_compare requiere otra columna.")
            validate_comparison_operator(p)
        if not isinstance(p.get("null_policy", "ALLOW"), str) or p.get(
            "null_policy", "ALLOW"
        ) not in {"ALLOW", "FAIL", "IGNORE"}:
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
        if self.type == "type" and (
            not isinstance(p.get("logical_type"), str)
            or p.get("logical_type")
            not in {
                "STRING",
                "DECIMAL",
                "INT64",
                "DATE",
                "TIMESTAMP",
                "BOOLEAN",
            }
        ):
            raise ValueError("logical_type no soportado.")
        if (
            self.type == "schema_type"
            and p.get("expected_type") is not None
            and (
                not isinstance(p["expected_type"], str)
                or p["expected_type"]
                not in {"STRING", "DECIMAL", "INT64", "DATE", "TIMESTAMP", "BOOLEAN"}
            )
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
                if (
                    not isinstance(p.get("metric"), str)
                    or not p.get("metric")
                    or not isinstance(p.get("operator", "lte"), str)
                    or p.get("operator", "lte")
                    not in {
                        "gt",
                        "gte",
                        "lt",
                        "lte",
                        "eq",
                    }
                ):
                    raise ValueError("metric_threshold necesita métrica y operador válido.")
                if "threshold" in p:
                    decimal_parameter(p["threshold"], "threshold")
                elif not any(k in p for k in ("min", "max")):
                    raise ValueError("metric_threshold necesita un límite.")
            if self.type == "historical_band":
                if (
                    any(
                        isinstance(p.get(key, default), bool)
                        or not isinstance(p.get(key, default), int)
                        for key, default in (("min_history", 4), ("window", 10))
                    )
                    or not 2 <= p.get("min_history", 4) <= p.get("window", 10) <= 1000
                ):
                    raise ValueError("Histórico requiere 2 ≤ min_history ≤ window ≤ 1000.")
                if decimal_parameter(p.get("iqr_multiplier", "1.5")) < 0:
                    raise ValueError("iqr_multiplier no puede ser negativo.")
            if self.type in {"distinct_rate", "uniqueness_ratio"} and any(
                not 0 <= decimal_parameter(p[key]) <= 1 for key in ("min", "max") if key in p
            ):
                raise ValueError("Los límites de proporción deben estar entre 0 y 1.")
        return self


def validate_column_list(columns: Any) -> None:
    if (
        not isinstance(columns, list)
        or not 1 <= len(columns) <= 100
        or any(not isinstance(c, str) or not c or len(c) > 240 for c in columns)
        or len(columns) != len(set(columns))
    ):
        raise ConfigurationError("Declare de 1 a 100 columnas distintas y no vacías.")


def validate_comparison_operator(parameters: dict) -> None:
    if (
        not isinstance(parameters.get("operator", "eq"), str)
        or not isinstance(parameters.get("logical_type", "STRING"), str)
        or parameters.get("operator", "eq") not in {"eq", "ne", "lt", "lte", "gt", "gte"}
        or parameters.get("logical_type", "STRING")
        not in {"STRING", "DECIMAL", "DATE", "TIMESTAMP"}
    ):
        raise ConfigurationError(
            "Comparación requiere operador eq/ne/lt/lte/gt/gte y tipo explícito compatible."
        )


def validate_condition(condition: dict, depth: int = 0, nodes: list[int] | None = None) -> None:
    """Bounded declarative predicate tree; strings are literals, never code."""
    nodes = nodes if nodes is not None else [0]
    nodes[0] += 1
    if not isinstance(condition, dict) or depth > 4 or nodes[0] > 64:
        raise ConfigurationError("Condición inválida o demasiado anidada (máximo 4 niveles).")
    groups = set(condition) & {"all", "any"}
    if groups:
        if len(groups) != 1 or len(condition) != 1:
            raise ConfigurationError("Cada condición compuesta declara all o any.")
        children = condition[next(iter(groups))]
        if not isinstance(children, list) or not 1 <= len(children) <= 10:
            raise ConfigurationError("Una condición admite de 1 a 10 predicados.")
        for child in children:
            validate_condition(child, depth + 1, nodes)
        return
    if set(condition) - {"column", "operator", "value", "logical_type"}:
        raise ConfigurationError("Propiedades no soportadas en condición.")
    validate_column_list([condition.get("column")])
    operator = condition.get("operator", "eq")
    if not isinstance(operator, str):
        raise ConfigurationError("El operador de condición debe ser texto.")
    if operator in {"is_null", "not_null"}:
        return
    validate_comparison_operator(condition)
    if "value" not in condition or isinstance(condition["value"], (list, dict)):
        raise ConfigurationError("La condición requiere un valor literal escalar.")
    value, kind = condition["value"], condition.get("logical_type", "STRING")
    if value is None:
        raise ConfigurationError("Para comprobar null utilice is_null o not_null.")
    if kind == "DECIMAL":
        decimal_parameter(value, "Valor de la condición")
    elif kind == "STRING" and not isinstance(value, str):
        raise ConfigurationError("Una condición STRING requiere un literal de texto.")
    elif kind in {"DATE", "TIMESTAMP"}:
        try:
            if not isinstance(value, str):
                raise TypeError
            if kind == "DATE":
                if not re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", value):
                    raise ValueError
                date.fromisoformat(value)
            else:
                if not re.fullmatch(
                    r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(?:\.[0-9]{1,6})?(?:Z|[+-](?:[01][0-9]|2[0-3]):[0-5][0-9])",
                    value,
                ):
                    raise ValueError
                if datetime.fromisoformat(value).utcoffset() is None:
                    raise ValueError
        except (ValueError, TypeError):
            raise ConfigurationError(
                "El valor de la condición debe ser fecha ISO o timestamp con offset válido."
            ) from None


def rule_columns(rule: RuleDefinition) -> list[str]:
    columns = list(rule.parameters.get("columns", [rule.column] if rule.column else []))
    if rule.type == "column_compare":
        columns.append(rule.parameters["other_column"])

    def collect(condition: dict) -> None:
        if "column" in condition:
            columns.append(condition["column"])
        for key in ("all", "any"):
            for child in condition.get(key, []):
                collect(child)

    if rule.when:
        collect(rule.when)
    return list(dict.fromkeys(columns))


def validate_transforms(transforms: list) -> None:
    if not isinstance(transforms, list) or len(transforms) > 100:
        raise ConfigurationError("Declare hasta 100 transformaciones ordenadas.")
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
    for transform in transforms:
        if (
            not isinstance(transform, dict)
            or not isinstance(transform.get("type"), str)
            or transform.get("type") not in allowed
            or not transform.get("column")
        ):
            raise ConfigurationError("Transform no soportado o sin columna.")
        p, kind = transform.get("parameters", {}), transform["type"]
        if not isinstance(p, dict):
            raise ConfigurationError("Los parámetros de transformación deben ser un objeto.")
        validate_column_list([transform["column"]])
        for key in (
            "case",
            "form",
            "fill",
            "side",
            "characters",
            "decimal_separator",
            "thousands_separator",
        ):
            if key in p and (not isinstance(p[key], str) or len(p[key]) > 500):
                raise ConfigurationError(
                    "Los parámetros de texto deben tener hasta 500 caracteres."
                )
        if kind == "case" and p.get("case", "UPPER") not in {"UPPER", "LOWER"}:
            raise ConfigurationError("Transform case debe ser UPPER o LOWER.")
        if kind == "unicode_normalization" and p.get("form", "NFC") not in {"NFC", "NFKC"}:
            raise ConfigurationError("Transform Unicode debe ser NFC o NFKC.")
        if kind == "id_padding" and (
            isinstance(p.get("width"), bool)
            or not isinstance(p.get("width"), int)
            or not 1 <= p["width"] <= 1000
            or len(p.get("fill", "0")) != 1
            or p.get("side", "left") not in {"left", "right"}
        ):
            raise ConfigurationError(
                "id_padding requiere ancho 1..1000, carácter y lado left/right."
            )
        if kind == "date_parse" and (
            not isinstance(p.get("formats"), list)
            or not 1 <= len(p["formats"]) <= 10
            or any(not isinstance(fmt, str) or len(fmt) > 100 for fmt in p["formats"])
        ):
            raise ConfigurationError("date_parse requiere de 1 a 10 formatos explícitos.")
        if kind == "decimal_parse" and (
            len(p.get("decimal_separator", ".")) != 1
            or len(p.get("thousands_separator", ",")) > 1
            or p.get("decimal_separator", ".") == p.get("thousands_separator", ",")
        ):
            raise ConfigurationError("Separadores decimal/miles deben ser distintos.")


def _comparison(rule: dict) -> dict:
    r = copy.deepcopy(rule)
    if not isinstance(r.get("type", r.get("code", "")), str):
        raise ConfigurationError("El tipo de comparación debe ser texto.")
    kind = r.get("type", r.get("code", "")).lower()
    if kind == "exact_match":
        kind = "numeric_tolerance"
        r["legacy_alias"] = "EXACT_MATCH"
    if kind not in {"exact_compare", "numeric_tolerance", "date_tolerance"}:
        raise ConfigurationError("Comparación no soportada.")
    r["type"], r["code"] = kind, kind.upper()
    if any(
        not isinstance(r.get(key), str) or not r[key] or len(r[key]) > 240
        for key in ("source_column", "target_column")
    ):
        raise ConfigurationError("Cada comparación necesita source_column y target_column.")
    p = r.setdefault("parameters", {})
    if not isinstance(p, dict):
        raise ConfigurationError("Los parámetros de comparación deben ser un objeto.")
    p.setdefault("equal_nulls", False)
    if not isinstance(p["equal_nulls"], bool):
        raise ConfigurationError("equal_nulls debe ser booleano.")
    if p.get("null_policy") is not None and (
        not isinstance(p["null_policy"], str)
        or p["null_policy"] not in {"MATCH_NULLS", "MISMATCH", "INVALID"}
    ):
        raise ConfigurationError("Política de nulos no soportada.")
    if kind == "exact_compare":
        p["normalization"] = Normalization.model_validate(p.get("normalization", {})).model_dump()
    if kind == "numeric_tolerance":
        p.setdefault("abs", "0")
        p.setdefault("percent", None)
        p.setdefault("denominator", "SOURCE")
        p.setdefault("zero_denominator", "EXACT_ONLY")
        if (
            not isinstance(p["denominator"], str)
            or p["denominator"] not in {"SOURCE", "TARGET", "MAX_ABS"}
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
        result.setdefault("source_transforms", [])
        result.setdefault("target_transforms", [])
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
    # Only publication assigns identities. Readers never reinterpret historical rules.
    for rule in result["rules"]:
        rule.setdefault("rule_id", str(uuid4()))
    identities = [rule["rule_id"] for rule in result["rules"]]
    if len(identities) != len(set(identities)):
        raise ConfigurationError("Cada regla necesita una identidad distinta.")
    if module.upper() == "INTAKE":
        validate_transforms(result["transforms"])
        if any(rule["type"] in METRIC_TYPES for rule in result["rules"]):
            raise ConfigurationError("Las reglas de métricas agregadas pertenecen a Sentinel.")
        if not 0 <= float(result.get("max_error_rate", 0)) <= 1:
            raise ConfigurationError("max_error_rate debe estar entre 0 y 1.")
    if module.upper() == "RECON":
        for side in ("source", "target"):
            validate_transforms(result[f"{side}_transforms"])
        if not result.get("key_columns") or not result.get("comparison_rules"):
            raise ConfigurationError("Recon necesita claves y al menos una comparación.")
        aggregation = result.get("aggregation")
        if aggregation:
            if (
                not isinstance(aggregation, dict)
                or not isinstance(aggregation.get("side"), str)
                or not isinstance(aggregation.get("operation"), str)
            ):
                raise ConfigurationError("La agregación requiere lado y operación de texto.")
            if aggregation.get("side") not in {"SOURCE", "TARGET"} or aggregation.get(
                "operation"
            ) not in {"sum", "count"}:
                raise ConfigurationError("Agregación admite SOURCE/TARGET y sum/count.")
            if not aggregation.get("output_column") or (
                aggregation["operation"] == "sum" and not aggregation.get("column")
            ):
                raise ConfigurationError("Agregación requiere output_column y column para sum.")
        aggregations = result.get("aggregations") or ([aggregation] if aggregation else [])
        if not isinstance(aggregations, list) or len(aggregations) > 100:
            raise ConfigurationError("Declare hasta 100 agregaciones.")
        if aggregations:
            if any(
                not isinstance(a, dict)
                or not isinstance(a.get("side"), str)
                or not isinstance(a.get("operation"), str)
                for a in aggregations
            ):
                raise ConfigurationError("Cada agregación requiere lado y operación de texto.")
            if len({a.get("side") for a in aggregations}) != 1:
                raise ConfigurationError("Agrupe un solo lado: 1:N o N:1; N:M no está soportado.")
            names = set()
            for item in aggregations:
                if (
                    item.get("side") not in {"SOURCE", "TARGET"}
                    or item.get("operation") not in {"sum", "count"}
                    or not item.get("output_column")
                    or (item["operation"] == "sum" and not item.get("column"))
                ):
                    raise ConfigurationError(
                        "Agregación requiere SOURCE/TARGET, sum/count y columnas explícitas."
                    )
                validate_column_list([item["output_column"]])
                if item["operation"] == "sum":
                    validate_column_list([item["column"]])
                if item["output_column"] in names or item["output_column"] in result["key_columns"]:
                    raise ConfigurationError(
                        "Los resultados agregados deben tener nombres distintos a las claves."
                    )
                names.add(item["output_column"])
            side_field = "source_column" if aggregations[0]["side"] == "SOURCE" else "target_column"
            if any(
                rule[side_field] not in names | set(result["key_columns"])
                for rule in result["comparison_rules"]
            ):
                raise ConfigurationError(
                    "Cada columna comparada del lado agrupado debe ser clave o resultado de una agregación declarada."
                )
            result["aggregations"] = aggregations
    return result
