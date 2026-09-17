"""Bounded, deterministic local processing. Monetary values never pass through float."""

import csv
import hashlib
import io
import json
import re
import unicodedata
from collections import Counter, defaultdict
from datetime import UTC, date, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

import polars as pl

from .config import MAX_ROWS
from .config_semantics import METRIC_TYPES, ConfigurationError, RuleDefinition, effective_config
from .portable_engine import (
    TIMESTAMP_PATTERN,
    PolarsCompiler,
    compile_metric,
    compile_rule,
    exact_decimal_context,
)


class ProcessingError(ValueError):
    pass


LOGICAL_SCHEMA_TYPES = frozenset(
    {"STRING", "DECIMAL", "INT64", "DATE", "TIMESTAMP", "BOOLEAN"}
)
COLUMN_OVERRIDE_FIELDS = frozenset({"logical_type", "semantic_tag"})


def validate_column_overrides(overrides: dict, columns: list[str]) -> dict[str, dict]:
    """Validate and normalize the public upload override contract.

    The API accepts the historical short form ``{"amount": "DECIMAL"}`` and
    the structured form emitted by the upload UI.  Keeping validation here
    also protects service callers that bypass the HTTP boundary.
    """
    if not isinstance(overrides, dict):
        raise ProcessingError("Los overrides de columnas deben ser un objeto.")
    if len(overrides) > len(columns):
        raise ProcessingError("Hay más overrides que columnas disponibles.")
    if any(not isinstance(name, str) for name in overrides):
        raise ProcessingError("Los nombres de columna de los overrides deben ser texto.")
    unknown_columns = set(overrides) - set(columns)
    if unknown_columns:
        raise ProcessingError(
            f"Override sobre columnas ausentes: {', '.join(sorted(unknown_columns))}"
        )

    normalized: dict[str, dict] = {}
    for column, raw_override in overrides.items():
        if isinstance(raw_override, str):
            override = {"logical_type": raw_override}
        elif isinstance(raw_override, dict):
            override = dict(raw_override)
        else:
            raise ProcessingError(
                f"El override de {column} debe ser un objeto con logical_type y/o semantic_tag."
            )

        if any(not isinstance(name, str) for name in override):
            raise ProcessingError(f"Las opciones del override de {column} deben ser texto.")
        unknown_fields = set(override) - COLUMN_OVERRIDE_FIELDS
        if unknown_fields:
            raise ProcessingError(
                f"Opciones de override no soportadas para {column}: "
                f"{', '.join(sorted(unknown_fields))}."
            )

        logical_type = override.get("logical_type")
        if logical_type is not None and (
            not isinstance(logical_type, str) or logical_type not in LOGICAL_SCHEMA_TYPES
        ):
            raise ProcessingError(f"Tipo de override no soportado para {column}.")
        semantic_tag = override.get("semantic_tag")
        if semantic_tag is not None and semantic_tag != "IDENTIFIER":
            raise ProcessingError(f"Etiqueta semántica no soportada para {column}.")
        if semantic_tag == "IDENTIFIER" and logical_type not in {None, "STRING"}:
            raise ProcessingError(
                f"La columna identificadora {column} debe usar el tipo lógico STRING."
            )
        normalized[column] = override
    return normalized


def money(value) -> Decimal | None:
    if value is None or not re.fullmatch(r"[+-]?[0-9]+(?:\.[0-9]+)?", str(value)):
        return None
    try:
        result = Decimal(str(value))
        return result if result.is_finite() else None
    except InvalidOperation:
        return None


def normalize(frame: pl.DataFrame) -> pl.DataFrame:
    """Legacy utility only. Profiling and new execution never call implicit normalization."""
    return frame.with_columns(
        [
            pl.col(c).cast(pl.String).str.strip_chars().replace("", None).alias(c)
            for c in frame.columns
        ]
    )


def read_csv(path: Path) -> pl.DataFrame:
    try:
        with path.open(encoding="utf-8-sig", newline="") as source:
            sample = source.read(8192)
        if "\x00" in sample:
            raise ProcessingError("El archivo contiene bytes inválidos; se admite CSV UTF-8.")
        first_line = sample.splitlines()[0] if sample.splitlines() else ""
        delimiter = ";" if first_line.count(";") > first_line.count(",") else ","
        header = next(csv.reader(io.StringIO(sample), delimiter=delimiter), [])
        if not header or len(header) != len(set(header)):
            raise ProcessingError(
                "El archivo necesita encabezados únicos; hay nombres de columnas duplicados."
            )
        frame = pl.read_csv(
            path, separator=delimiter, infer_schema=False, encoding="utf8", n_rows=MAX_ROWS + 1
        )
    except (UnicodeDecodeError, pl.exceptions.PolarsError, IndexError) as exc:
        raise ProcessingError(
            "No se pudo leer el CSV UTF-8. Revise encabezados y delimitador."
        ) from exc
    if frame.height > MAX_ROWS:
        raise ProcessingError(f"Este prototipo admite hasta {MAX_ROWS:,} filas por archivo.")
    if not frame.width or any(not c.strip() or c.startswith("__tv_") for c in frame.columns):
        raise ProcessingError(
            "El archivo necesita encabezados válidos, sin prefijo reservado __tv_."
        )
    if frame.width > 100:
        raise ProcessingError("Este prototipo admite hasta 100 columnas.")
    return frame


def csv_record_lines(path: Path) -> list[int]:
    """Physical starting lines for parsed CSV records, excluding the header.

    The map is separate from the canonical business columns. Callers only use
    this map for a verified original CSV; derived Parquet records have no
    original physical file line unless their lineage records one explicitly.
    """
    with path.open(encoding="utf-8-sig", newline="") as source:
        sample = source.read(8192)
        first_line = sample.splitlines()[0] if sample.splitlines() else ""
        delimiter = ";" if first_line.count(";") > first_line.count(",") else ","
        source.seek(0)
        records = csv.reader(source, delimiter=delimiter)
        next(records, None)
        lines = []
        previous_line = records.line_num
        for _ in records:
            lines.append(previous_line + 1)
            previous_line = records.line_num
    return lines


def _record_lines(frame: pl.DataFrame, row_numbers: list[int] | None) -> list[int]:
    if row_numbers is None:
        return list(range(2, frame.height + 2))
    if len(row_numbers) != frame.height or any(n < 1 for n in row_numbers):
        raise ProcessingError("El mapa de líneas no coincide con los registros del dataset.")
    return row_numbers


def iso_date(value) -> date | None:
    if not isinstance(value, str) or not re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", value):
        return None
    try:
        return date.fromisoformat(value)
    except ValueError:
        return None


def iso_timestamp(value) -> datetime | None:
    if not isinstance(value, str) or not re.fullmatch(TIMESTAMP_PATTERN, value):
        return None
    try:
        return datetime.fromisoformat(value).astimezone(UTC)
    except ValueError:
        return None


def logical_type_from_native(value: str | None) -> str | None:
    """Map connector-native schema names to the portable logical type catalog."""
    normalized = (value or "").upper().replace(" ", "")
    base = normalized.split("(", 1)[0]
    if base.startswith(("INT", "UINT")) or base in {
        "BYTE",
        "SHORT",
        "LONG",
        "BIGINT",
        "SMALLINT",
    }:
        return "INT64"
    if base.startswith(("FLOAT", "DECIMAL")) or base in {
        "DOUBLE",
        "REAL",
        "NUMERIC",
        "NUMBER",
    }:
        return "DECIMAL"
    if base in {"BOOL", "BOOLEAN"}:
        return "BOOLEAN"
    if base == "DATE":
        return "DATE"
    if base.startswith(("DATETIME", "TIMESTAMP")):
        return "TIMESTAMP"
    return None


def profile_frame(
    frame: pl.DataFrame,
    column_overrides: dict | None = None,
    *,
    overrides: dict | None = None,
    native_types: dict[str, str] | None = None,
) -> tuple[list, dict, str]:
    """Observe exact values. CSV unquoted empty is null; quoted empty remains empty text.

    Distinct excludes only null, never whitespace/case/Unicode variants. Identifier
    inference is metadata: canonical values remain unchanged even under an override.
    """
    overrides = overrides if overrides is not None else column_overrides or {}
    native_types = native_types or {}
    overrides = validate_column_overrides(overrides, frame.columns)
    columns, schema = [], []
    for c in frame.columns:
        series = frame[c]
        values = series.drop_nulls().to_list()
        override = overrides.get(c, {})
        identifier = override.get(
            "semantic_tag", "IDENTIFIER" if c.lower() == "id" or c.lower().endswith("_id") else None
        )
        logical = "STRING"
        native_logical = logical_type_from_native(native_types.get(c))
        if identifier != "IDENTIFIER" and native_logical:
            logical = native_logical
        elif values and identifier != "IDENTIFIER":
            if all(iso_date(v) is not None for v in values):
                logical = "DATE"
            elif all(iso_timestamp(v) is not None for v in values):
                logical = "TIMESTAMP"
            elif all(money(v) is not None for v in values):
                logical = "DECIMAL"
        if override.get("logical_type"):
            logical = override["logical_type"]
            if logical != "STRING":
                expression = compile_rule(
                    {"type": "type", "column": c, "parameters": {"logical_type": logical}},
                    datetime.now(UTC),
                )
                if not all(PolarsCompiler().validate(frame, expression)):
                    raise ProcessingError(f"Los valores de {c} no cumplen el override {logical}.")
        null_count = series.null_count()
        distinct = series.drop_nulls().n_unique()
        unique_rows = sum(count == 1 for count in Counter(values).values())
        item = {
            "name": c,
            "logical_type": logical,
            "null_count": null_count,
            "null_rate": round(null_count / frame.height, 4) if frame.height else 0,
            "distinct_count": distinct,
            "distinct_rate": distinct / len(values) if values else 0,
            "uniqueness_ratio": unique_rows / len(values) if values else 0,
            "semantic_tag": identifier,
            "inference_method": (
                "EXPLICIT_OVERRIDE"
                if override
                else "SOURCE_SCHEMA_V1"
                if native_logical and identifier != "IDENTIFIER"
                else "OBSERVED_V2"
            ),
        }
        if logical in {"DECIMAL", "INT64"} and values:
            decimals = [number for v in values if (number := money(v)) is not None]
            item["parse_error_count"] = len(values) - len(decimals)
            if decimals:
                with exact_decimal_context(*decimals):
                    item.update(
                        min=str(min(decimals)),
                        max=str(max(decimals)),
                        mean=str(sum(decimals) / len(decimals)),
                    )
        elif logical in {"DATE", "TIMESTAMP"} and values:
            parsed = [
                observed_date
                for v in values
                if (observed_date := iso_date(v) if logical == "DATE" else iso_timestamp(v))
                is not None
            ]
            item["parse_error_count"] = len(values) - len(parsed)
            if parsed:
                item.update(min=min(parsed).isoformat(), max=max(parsed).isoformat())
        elif values:
            item.update(
                min_length=min(len(str(v)) for v in values),
                max_length=max(len(str(v)) for v in values),
            )
        columns.append(item)
        # Null spikes change observed metadata, not the structural schema identity.
        schema_item = {
            "name": c,
            "logical_type": logical,
            "nullable": null_count > 0,
            "semantic_tag": identifier,
        }
        if native_types.get(c) and native_types[c] not in {"String", "JSON scalar"}:
            schema_item["native_type"] = native_types[c]
        schema.append(schema_item)
    signature = [{"name": s["name"], "logical_type": s["logical_type"]} for s in schema]
    schema_hash = hashlib.sha256(json.dumps(signature, sort_keys=True).encode()).hexdigest()
    return (
        schema,
        {
            "row_count": frame.height,
            "column_count": frame.width,
            "columns": columns,
            "profiling_policy": "OBSERVED_EXACT_V2",
            "metric_method": "EXACT_OBSERVED",
            "metric_definition_version": 2,
            "null_policy": "NULL_EXCLUDED_EMPTY_PRESERVED",
        },
        schema_hash,
    )


def normalized_value(value, policy: dict):
    if value is None:
        return None
    value = str(value)
    if policy.get("unicode_normalization", "NONE") != "NONE":
        value = unicodedata.normalize(policy["unicode_normalization"], value)
    if policy.get("trim"):
        value = value.strip()
    if policy.get("case") == "UPPER":
        value = value.upper()
    if policy.get("case") == "LOWER":
        value = value.lower()
    return value


def apply_transforms(frame: pl.DataFrame, transforms: list[dict]) -> pl.DataFrame:
    """Apply the ordered contract declarations; never modify the input frame."""
    result = frame.clone()
    for transform in transforms:
        column, kind, p = transform["column"], transform["type"], transform.get("parameters", {})
        if column not in result.columns:
            raise ProcessingError(f"Columna de transformación ausente: {column}")

        def convert(value, kind=kind, p=p):
            if value is None:
                return None
            value = str(value)
            if kind == "trim":
                return value.strip()
            if kind == "empty_to_null":
                return None if value == "" else value
            if kind == "case":
                return value.upper() if p.get("case", "UPPER") == "UPPER" else value.lower()
            if kind == "unicode_normalization":
                return unicodedata.normalize(p.get("form", "NFC"), value)
            if kind == "id_padding":
                return (
                    value.rjust(p["width"], p.get("fill", "0"))
                    if p.get("side", "left") == "left"
                    else value.ljust(p["width"], p.get("fill", "0"))
                )
            if kind == "remove_characters":
                return value.translate(str.maketrans("", "", p.get("characters", "")))
            if kind == "decimal_parse":
                sep = p.get("thousands_separator", ",")
                cleaned = value.replace(sep, "") if sep else value
                parsed = money(cleaned.replace(p.get("decimal_separator", "."), "."))
                return str(parsed) if parsed is not None else value
            if kind == "date_parse":
                for fmt in p["formats"]:
                    try:
                        # This declared transform extracts a calendar date; UTC
                        # makes the intermediate datetime independent of host TZ.
                        return datetime.strptime(value, fmt).replace(tzinfo=UTC).date().isoformat()
                    except ValueError:
                        continue
                return value
            raise ProcessingError(f"Transformación no soportada: {kind}")

        result = result.with_columns(
            pl.Series(column, [convert(v) for v in result[column]], dtype=pl.String)
        )
    return result


def configured_rules(config: dict) -> list[RuleDefinition]:
    rules: list[RuleDefinition] = []
    for code, key in (
        ("REQUIRED", "required_columns"),
        ("UNIQUE", "unique_columns"),
        ("NUMERIC", "numeric_columns"),
        ("POSITIVE", "positive_columns"),
    ):
        rules.extend(
            RuleDefinition(
                type=code.lower(),
                code=code,
                column=c,
                # Published v1 shorthand rejected missing numeric/positive values.
                parameters={"null_policy": "FAIL"} if code in {"NUMERIC", "POSITIVE"} else {},
            )
            for c in config.get(key, [])
        )
    rules.extend(RuleDefinition.model_validate(r) for r in config.get("rules", []))
    return [r for r in rules if r.enabled]


def intake(
    frame: pl.DataFrame,
    config: dict,
    observed_at: datetime | None = None,
    input_row_numbers: list[int] | None = None,
) -> tuple[list[dict], dict, pl.DataFrame]:
    config = effective_config("INTAKE", config)
    transformed = apply_transforms(frame, config["transforms"])
    rules = configured_rules(config)
    missing = sorted({r.column for r in rules if r.column} - set(transformed.columns))
    if missing:
        raise ProcessingError(f"Columnas del contrato ausentes: {', '.join(missing)}")
    errors: list[dict] = []
    bad_rows: set[int] = set()
    warning_rows: set[int] = set()
    summaries: list[dict] = []
    observed = observed_at or datetime.now(UTC)
    original = frame.to_dicts()
    input_lines = _record_lines(frame, input_row_numbers)
    for rule in rules:
        assert rule.column is not None
        passed = PolarsCompiler().validate(transformed, compile_rule(rule, observed))
        failed_indices = [i for i, valid in enumerate(passed) if not valid]
        summaries.append(
            {
                "code": rule.code,
                "column": rule.column,
                "severity": rule.severity,
                "evaluated_count": transformed.height,
                "failed_count": len(failed_indices),
                "status": "FAIL" if failed_indices else "PASS",
                "rule_id": rule.rule_id,
            }
        )
        for i in failed_indices:
            errors.append(
                {
                    "original_row_number": input_lines[i],
                    "rule_code": rule.code,
                    "column": rule.column,
                    "received_value": original[i][rule.column],
                    "severity": rule.severity,
                    "message": rule.message or f"{rule.column}: incumple la regla {rule.code}",
                    "classification": rule.severity,
                }
            )
            (bad_rows if rule.severity == "ERROR" else warning_rows).add(i)
    errors.sort(key=lambda r: (r["original_row_number"], r["rule_code"], r["column"]))
    total, failed = frame.height, len(bad_rows)
    decision = (
        "REJECTED"
        if total and failed / total > config.get("max_error_rate", 0)
        else "APPROVED_WITH_WARNINGS"
        if failed or warning_rows
        else "APPROVED"
    )
    metrics = {
        "total_rows": total,
        "valid_rows": total - failed,
        "error_rows": failed,
        "warning_rows": len(warning_rows),
        "error_count": sum(r["severity"] == "ERROR" for r in errors),
        "acceptance_rate": round(100 * (total - failed) / total, 2) if total else 100,
        "rules": summaries,
        "decision": decision,
        "normalization_policy": "DECLARED_ONLY",
    }
    accepted = transformed.filter(
        pl.Series([i not in bad_rows for i in range(total)], dtype=pl.Boolean)
    )
    return errors, metrics, accepted


def reconcile(
    source: pl.DataFrame,
    target: pl.DataFrame,
    config: dict,
    source_row_numbers: list[int] | None = None,
    target_row_numbers: list[int] | None = None,
) -> tuple[list[dict], dict]:
    from .portable_engine import compile_comparison

    try:
        config = effective_config("RECON", config)
    except (ConfigurationError, ValueError) as exc:
        raise ProcessingError(str(exc)) from exc
    keys, rules = config["key_columns"], config["comparison_rules"]
    if not rules:
        raise ProcessingError("El control necesita al menos una comparación.")
    aggregation = config.get("aggregation")
    for name, frame, side_name in [("origen", source, "SOURCE"), ("destino", target, "TARGET")]:
        needed = set(
            keys + [r["source_column" if side_name == "SOURCE" else "target_column"] for r in rules]
        )
        if aggregation and aggregation["side"] == side_name:
            needed.discard(aggregation["output_column"])
            if aggregation["operation"] == "sum":
                needed.add(aggregation["column"])
        missing = needed - set(frame.columns)
        if missing:
            raise ProcessingError(f"Columnas ausentes en {name}: {', '.join(sorted(missing))}")
    groups: list[defaultdict[tuple, list[tuple[Any, ...]]]] = [defaultdict(list), defaultdict(list)]
    results, pairs = [], []
    first = rules[0]
    line_maps = [
        _record_lines(source, source_row_numbers),
        _record_lines(target, target_row_numbers),
    ]

    def emit(key, classification, s=None, t=None, message="", comparisons=None):
        detail = comparisons[0] if comparisons else {}
        results.append(
            {
                "key": key,
                "classification": classification,
                "source_value": s[0].get(first["source_column"]) if s else None,
                "target_value": t[0].get(first["target_column"]) if t else None,
                "source_row": s[1] if s else None,
                "target_row": t[1] if t else None,
                "source_rows": s[2] if s and len(s) > 2 else [s[1]] if s else [],
                "target_rows": t[2] if t and len(t) > 2 else [t[1]] if t else [],
                "difference": detail.get("difference"),
                "tolerance": detail.get("tolerance", first["parameters"].get("abs")),
                "rule_code": detail.get("code", first["code"]),
                "comparisons": comparisons or [],
                "message": message,
            }
        )

    for side, frame in enumerate([source, target]):
        for index, row in enumerate(frame.iter_rows(named=True)):
            file_line = line_maps[side][index]
            key = tuple(normalized_value(row[k], config["key_normalization"]) for k in keys)
            if any(v is None or v == "" for v in key):
                emit(
                    f"fila-{side}-{file_line}",
                    "INVALID",
                    (row, file_line) if side == 0 else None,
                    (row, file_line) if side == 1 else None,
                    "Clave vacía: no participa en el cruce",
                )
            else:
                groups[side][key].append((row, file_line))
    if aggregation:
        side = 0 if aggregation["side"] == "SOURCE" else 1
        for key, group in list(groups[side].items()):
            record = dict(group[0][0])
            aggregate: Decimal | None
            if aggregation["operation"] == "count":
                aggregate = Decimal(len(group))
            else:
                numbers = [money(row[0][aggregation["column"]]) for row in group]
                valid_numbers = [v for v in numbers if v is not None]
                with exact_decimal_context(*valid_numbers):
                    aggregate = (
                        sum(valid_numbers, Decimal(0))
                        if len(valid_numbers) == len(numbers)
                        else None
                    )
            record[aggregation["output_column"]] = str(aggregate) if aggregate is not None else None
            groups[side][key] = [(record, group[0][1], [item[1] for item in group])]
    for key in sorted(set(groups[0]) | set(groups[1])):
        left, right = groups[0].get(key, []), groups[1].get(key, [])
        display = " | ".join(key)
        if len(left) > 1 or len(right) > 1:
            for s in left:
                emit(
                    display,
                    "DUPLICATE_SOURCE" if len(left) > 1 else "INVALID",
                    s=s,
                    message="Clave ambigua excluida antes del cruce; no se genera many-to-many",
                )
            for t in right:
                emit(
                    display,
                    "DUPLICATE_TARGET" if len(right) > 1 else "INVALID",
                    t=t,
                    message="Clave ambigua excluida antes del cruce; no se genera many-to-many",
                )
            continue
        source_record, target_record = left[0] if left else None, right[0] if right else None
        if source_record is None:
            emit(
                display, "TARGET_ONLY", t=target_record, message="Registro presente solo en destino"
            )
        elif target_record is None:
            emit(
                display, "SOURCE_ONLY", s=source_record, message="Registro presente solo en origen"
            )
        else:
            pairs.append((display, source_record, target_record))
    evaluated = []
    for rule in rules:
        expression = compile_comparison(rule)
        compared = pl.DataFrame(
            {
                "__tv_source": [p[1][0][rule["source_column"]] for p in pairs],
                "__tv_target": [p[2][0][rule["target_column"]] for p in pairs],
            },
            schema={"__tv_source": pl.String, "__tv_target": pl.String},
        )
        evaluated.append(PolarsCompiler().compare(compared, expression))
    for index, (display, s, t) in enumerate(pairs):
        details = [checks[index] for checks in evaluated]
        invalid = any(d["invalid"] for d in details)
        matched = all(d["passed"] for d in details)
        emit(
            display,
            "INVALID" if invalid else "MATCH" if matched else "VALUE_MISMATCH",
            s,
            t,
            "Valor vacío o tipo inválido"
            if invalid
            else "Todas las comparaciones cumplen"
            if matched
            else "Una o más comparaciones no cumplen",
            details,
        )
    results.sort(
        key=lambda r: (r["key"], r["classification"], r["source_row"] or 0, r["target_row"] or 0)
    )
    counts = Counter(r["classification"] for r in results)
    metrics = {
        "total_rows": len(results),
        "source_rows": source.height,
        "target_rows": target.height,
        "matched": counts["MATCH"],
        "mismatched": counts["VALUE_MISMATCH"],
        "source_only": counts["SOURCE_ONLY"],
        "target_only": counts["TARGET_ONLY"],
        "duplicate_source": counts["DUPLICATE_SOURCE"],
        "duplicate_target": counts["DUPLICATE_TARGET"],
        "invalid": counts["INVALID"],
        "match_rate": round(100 * counts["MATCH"] / len(results), 2) if results else 100,
        "counts": {
            k: counts[k]
            for k in [
                "MATCH",
                "VALUE_MISMATCH",
                "SOURCE_ONLY",
                "TARGET_ONLY",
                "DUPLICATE_SOURCE",
                "DUPLICATE_TARGET",
                "INVALID",
            ]
        },
        "diagnostics": {
            "key_normalization": config["key_normalization"],
            "key_normalization_policy": config["key_normalization_policy"],
            "comparison_rules": rules,
            "aggregation": aggregation,
            "semantics_version": 2,
        },
    }
    return results, metrics


def sentinel(
    profile: dict,
    schema: list,
    config: dict,
    created_at: datetime,
    baseline: dict | None,
    observed_at: datetime | None = None,
    frame: pl.DataFrame | None = None,
    previous_schema: list | None = None,
    history: list | None = None,
) -> tuple[list, dict]:
    checks, metric_records = [], []
    observed = observed_at or datetime.now(UTC)
    observed = observed.replace(tzinfo=UTC) if observed.tzinfo is None else observed.astimezone(UTC)
    created = (
        created_at.replace(tzinfo=UTC) if created_at.tzinfo is None else created_at.astimezone(UTC)
    )
    method = profile.get("metric_method", "LEGACY_NORMALIZED")
    version = profile.get("metric_definition_version", 1)
    by_name = {p["name"]: p for p in profile["columns"]}

    def record(key, value, column=None):
        metric_records.append(
            {
                "metric_key": key,
                "numeric_value": str(value),
                "method": method,
                "metric_definition_version": version,
                "dimensions": {"column": column} if column else {},
            }
        )

    record("row_count", profile["row_count"])
    for column, values in by_name.items():
        for key in (
            "null_count",
            "null_rate",
            "distinct_count",
            "distinct_rate",
            "uniqueness_ratio",
        ):
            if key in values:
                record(f"{key}:{column}", values[key], column)

    def add(code, name, actual, expected, passed, message, **extra):
        checks.append(
            {
                "code": code,
                "name": name,
                "actual": actual,
                "expected": expected,
                "status": "PASS" if passed else "FAIL",
                "classification": "PASS" if passed else "FAIL",
                "message": message,
                "metric_method": method,
                "metric_definition_version": version,
                **extra,
            }
        )

    missing = sorted(set(config.get("required_columns", [])) - {s["name"] for s in schema})
    add(
        "SCHEMA_REQUIRED",
        "Estructura del dataset",
        missing,
        [],
        not missing,
        "Todas las columnas requeridas deben existir",
    )
    null_rates = []
    for c in config.get("null_columns", []):
        col = by_name.get(c)
        actual = col["null_rate"] if col else 1.0
        null_rates.append(actual)
        threshold = config.get("max_null_rate", 0.05)
        add(
            "NULL_RATE_" + c.upper(),
            f"Nulos · {c}",
            actual,
            {"max": threshold},
            actual <= threshold,
            f"Proporción de nulos ≤ {threshold:.0%}",
        )
    age = max(0, (observed - created).total_seconds() / 3600)
    max_age = config.get("max_age_hours", 48)
    add(
        "FRESHNESS",
        "Actualización",
        round(age, 2),
        {"max_hours": max_age},
        age <= max_age,
        f"Carga dentro de las últimas {max_age} horas",
    )
    if baseline:
        prev = baseline["row_count"]
        change = (
            abs(profile["row_count"] - prev) / prev * 100
            if prev
            else (100 if profile["row_count"] else 0)
        )
        threshold = config.get("max_volume_change_pct", 15)
        add(
            "VOLUME_CHANGE",
            "Variación de volumen",
            round(change, 2),
            {"max_pct": threshold, "baseline_rows": prev, "method": "previous_version"},
            change <= threshold,
            "Variación absoluta respecto a la versión anterior",
        )
    else:
        add(
            "VOLUME_MIN",
            "Volumen mínimo",
            profile["row_count"],
            {"min": 1, "method": "fixed_threshold"},
            profile["row_count"] > 0,
            "Sin historial: se verifica que existan registros",
        )
    for definition in config.get("rules", []):
        rule = RuleDefinition.model_validate(definition)
        if not rule.enabled:
            continue
        p, kind, column = rule.parameters, rule.type, rule.column
        if kind in METRIC_TYPES:
            metric = PolarsCompiler().measure(
                profile, schema, compile_metric(rule, observed), previous_schema, history
            )
            add(
                rule.code,
                metric["name"],
                metric["actual"],
                metric["expected"],
                metric["passed"],
                metric["message"],
                **{key: metric[key] for key in ("column", "metric_key") if key in metric},
            )
        else:
            if frame is None:
                raise ProcessingError("El control por registro requiere el Parquet canónico.")
            if column not in frame.columns:
                add(
                    rule.code,
                    f"{rule.code} · {column}",
                    None,
                    p,
                    False,
                    "Columna ausente",
                    column=column,
                )
                continue
            mask = PolarsCompiler().validate(frame, compile_rule(rule, observed))
            failed = sum(not value for value in mask)
            add(
                rule.code,
                f"{rule.code} · {column}",
                failed,
                {"max_failed": 0, **p},
                failed == 0,
                rule.message or f"{failed} registros incumplen de {frame.height}",
                column=column,
                evaluated_count=frame.height,
                failed_count=failed,
            )
    failed = sum(c["status"] == "FAIL" for c in checks)
    return checks, {
        "row_count": profile["row_count"],
        "null_rate": max(null_rates, default=0),
        "health_score": round(100 * (len(checks) - failed) / len(checks), 2) if checks else 100,
        "failed_checks": failed,
        "total_checks": len(checks),
        "checks": checks,
        "metric_records": metric_records,
        "metric_method": method,
        "metric_definition_version": version,
    }
