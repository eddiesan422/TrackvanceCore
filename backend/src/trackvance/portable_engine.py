"""RuleDefinition -> portable expression -> native, bounded local engine compiler.

Rule values are literals/SQL parameters; configured text is never executable Python or SQL.
Regex runs in the native linear-time Rust/RE2 engines, with a common supported subset.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Context, Decimal, localcontext
from typing import Protocol

import duckdb
import polars as pl

from .config_semantics import METRIC_TYPES, RuleDefinition, _comparison, portable_regex_pattern

DECIMAL_PATTERN = r"^[+-]?[0-9]+(?:\.[0-9]+)?$"
DATE_PATTERN = r"^[0-9]{4}-[0-9]{2}-[0-9]{2}$"
TIMESTAMP_PATTERN = (
    r"^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}"
    r"(?:\.[0-9]{1,6})?(?:Z|[+-](?:[01][0-9]|2[0-3]):[0-5][0-9])$"
)


def exact_decimal_context(*values: Decimal):
    """Preserve every input digit during sums, differences and percentage products."""
    precision = max(
        28,
        2
        * max(
            (len(v.as_tuple().digits) + abs(int(v.as_tuple().exponent)) for v in values), default=1
        )
        + len(str(len(values)))
        + 10,
    )
    return localcontext(Context(prec=precision))


def _range_relation(
    frame: pl.DataFrame, expression: PortableRuleExpression
) -> tuple[pl.DataFrame, dict[str, str]]:
    """Lower exact Decimal order to native string comparisons without rounding.

    Padding only affects the internal predicate relation, never source values.
    Negative digits are complemented to preserve signed numeric ordering.
    """
    from .processing import money

    column = expression.definition.column
    assert column is not None
    numbers = [money(value) for value in frame[column]]
    bounds = {
        key: Decimal(str(value))
        for key, value in expression.definition.parameters.items()
        if key in {"gt", "gte", "lt", "lte"}
    }
    texts = [
        format(value.copy_abs(), "f") for value in [*numbers, *bounds.values()] if value is not None
    ]
    integral = max((len(text.partition(".")[0]) for text in texts), default=1)
    fractional = max((len(text.partition(".")[2]) for text in texts), default=0)

    def key(value: Decimal) -> str:
        before, _, after = format(value.copy_abs(), "f").partition(".")
        digits = before.zfill(integral) + after.ljust(fractional, "0")
        return (
            "0" + digits.translate(str.maketrans("0123456789", "9876543210"))
            if value < 0
            else "1" + digits
        )

    return frame.with_columns(
        pl.Series(
            "__tv_decimal_key",
            [key(v) if v is not None else None for v in numbers],
            dtype=pl.String,
        )
    ), {name: key(value) for name, value in bounds.items()}


@dataclass(frozen=True)
class PortableRuleExpression:
    definition: RuleDefinition
    observed_at: datetime


@dataclass(frozen=True)
class PortableComparisonExpression:
    definition: dict


@dataclass(frozen=True)
class PortableMetricExpression:
    definition: RuleDefinition
    observed_at: datetime


def compile_metric(rule: RuleDefinition | dict, observed_at: datetime) -> PortableMetricExpression:
    definition = rule if isinstance(rule, RuleDefinition) else RuleDefinition.model_validate(rule)
    if definition.type not in METRIC_TYPES:
        raise ValueError("La regla por registro requiere compile_rule().")
    observed = (
        observed_at.replace(tzinfo=UTC)
        if observed_at.tzinfo is None
        else observed_at.astimezone(UTC)
    )
    return PortableMetricExpression(definition, observed)


def _percentile(values: list[Decimal], fraction: Decimal) -> Decimal:
    ordered = sorted(values)
    point = (len(ordered) - 1) * fraction
    index = int(point)
    return ordered[index] + (ordered[min(index + 1, len(ordered) - 1)] - ordered[index]) * (
        point - index
    )


def _evaluate_metric(
    compiler: ProcessingEngine,
    profile: dict,
    schema: list,
    expression: PortableMetricExpression,
    previous_schema: list | None,
    history: list | None,
) -> dict:
    """Prepare versioned metric facts; evaluate their predicates in the selected engine.

    Profiles are immutable observations, not re-profiled by the reader. Median
    and IQR use the same exact Decimal algorithm on both backends so a backend
    switch cannot introduce an implicit quantile-method change.
    """
    rule = expression.definition
    p, kind = rule.parameters, rule.type
    column = rule.column or p.get("column")
    if kind == "schema_type":
        actual = next((s["logical_type"] for s in schema if s["name"] == column), None)
        previous = next(
            (s["logical_type"] for s in previous_schema or [] if s["name"] == column), None
        )
        expected_type = p.get("expected_type") or previous
        baseline = expected_type is None and not p.get("expected_type")
        compared = compiler.compare(
            pl.DataFrame(
                {"__tv_source": [actual], "__tv_target": [actual if baseline else expected_type]},
                schema={"__tv_source": pl.String, "__tv_target": pl.String},
            ),
            compile_comparison(
                {"type": "exact_compare", "source_column": "type", "target_column": "type"}
            ),
        )
        return {
            "actual": actual,
            "expected": {
                "logical_type": actual if baseline else expected_type,
                "previous_type": previous,
                "method": "DECLARED_TYPE"
                if p.get("expected_type")
                else "INITIAL_BASELINE"
                if baseline
                else "PREVIOUS_SCHEMA",
            },
            "passed": compared[0]["passed"],
            "name": f"Tipo · {column}",
            "message": rule.message or "El tipo observado debe coincidir con el declarado",
            "column": column,
        }
    method = profile.get("metric_method", "LEGACY_NORMALIZED")
    version = profile.get("metric_definition_version", 1)
    by_name = {item["name"]: item for item in profile["columns"]}
    metric_key = p.get("metric", kind)
    actual = profile.get(metric_key) if not column else by_name.get(column, {}).get(metric_key)
    key = f"{metric_key}:{column}" if column else metric_key
    expected: dict = {bound: str(p[bound]) for bound in ("min", "max") if bound in p}
    if kind == "historical_band" and actual is not None:
        compatible = [
            Decimal(str(item["numeric_value"]))
            for item in history or []
            if item.get("metric_key") == key
            and item.get("method") == method
            and item.get("metric_definition_version") == version
            and item.get("status", "SUCCESS") == "SUCCESS"
            and item.get("numeric_value") is not None
        ]
        values = compatible[-p.get("window", 10) :]
        with exact_decimal_context(*values, Decimal(str(p.get("iqr_multiplier", "1.5")))):
            if len(values) >= p.get("min_history", 4):
                q1 = _percentile(values, Decimal("0.25"))
                q3 = _percentile(values, Decimal("0.75"))
                spread = (q3 - q1) * Decimal(str(p.get("iqr_multiplier", "1.5")))
                expected.update(
                    min=str(q1 - spread),
                    max=str(q3 + spread),
                    median=str(_percentile(values, Decimal("0.5"))),
                    q1=str(q1),
                    q3=str(q3),
                    iqr=str(q3 - q1),
                    method="MEDIAN_IQR_LINEAR_V1",
                    history_count=len(values),
                )
            else:
                expected.update(
                    method="FIXED_THRESHOLD_FALLBACK",
                    history_count=len(values),
                    minimum_history=p.get("min_history", 4),
                )
                if not any(bound in expected for bound in ("min", "max")) and values:
                    previous_value = values[-1]
                    tolerance = (
                        abs(previous_value) * Decimal(str(p.get("fallback_change_pct", 15))) / 100
                    )
                    expected.update(
                        min=str(previous_value - tolerance),
                        max=str(previous_value + tolerance),
                        method="PREVIOUS_COMPATIBLE_METRIC",
                    )
    relation = pl.DataFrame({"value": [None if actual is None else str(actual)]})
    passed = compiler.validate(
        relation,
        compile_rule({"type": "not_null", "column": "value"}, expression.observed_at),
    )[0]
    predicates = [("gte", expected["min"])] if "min" in expected else []
    if "max" in expected:
        predicates.append(("lte", expected["max"]))
    if kind == "metric_threshold" and "threshold" in p:
        operator, target = p.get("operator", "lte"), str(p["threshold"])
        predicates.extend(
            [("gte", target), ("lte", target)] if operator == "eq" else [(operator, target)]
        )
        expected.update(operator=operator, threshold=target)
    for operator, boundary in predicates:
        passed &= compiler.validate(
            relation,
            compile_rule(
                {
                    "type": "range",
                    "column": "value",
                    "parameters": {operator: boundary, "null_policy": "FAIL"},
                },
                expression.observed_at,
            ),
        )[0]
    return {
        "actual": actual,
        "expected": expected,
        "passed": passed,
        "name": f"{kind} · {column or 'dataset'}",
        "message": rule.message or "Evaluación de métrica con método e historial compatibles",
        "column": column,
        "metric_key": key,
    }


def compile_comparison(rule: dict) -> PortableComparisonExpression:
    return PortableComparisonExpression(_comparison(rule))


def _prepare_comparison(
    frame: pl.DataFrame, expression: PortableComparisonExpression
) -> tuple[pl.DataFrame, list[dict]]:
    """Lower comparisons to exact scalar facts plus a portable comparison predicate.

    Decimal preparation is shared to avoid engine-dependent rounding of percent
    division and UTC durations. Predicate evaluation remains engine native. No
    business value is converted through binary floating point.
    """
    from .processing import iso_date, iso_timestamp, money, normalized_value

    rule = expression.definition
    p, kind = rule["parameters"], rule["type"]
    records, facts = [], []
    for source, target in frame.iter_rows():
        invalid, difference, tolerance, left, right = False, None, None, source, target
        both_null = source is None and target is None
        null_match = both_null and p.get("equal_nulls", False)
        if kind == "exact_compare":
            left, right = (
                normalized_value(source, p["normalization"]),
                normalized_value(target, p["normalization"]),
            )
        elif source is None or target is None:
            invalid = not null_match
        elif kind == "numeric_tolerance":
            source_number, target_number = money(source), money(target)
            invalid = source_number is None or target_number is None
            if source_number is not None and target_number is not None:
                tolerance = Decimal(str(p["abs"]))
                percent = Decimal(p["percent"]) if p.get("percent") is not None else None
                with exact_decimal_context(
                    source_number, target_number, tolerance, percent or Decimal(0)
                ):
                    difference = source_number - target_number
                    if percent is not None:
                        denominator = {
                            "SOURCE": source_number.copy_abs(),
                            "TARGET": target_number.copy_abs(),
                            "MAX_ABS": max(source_number.copy_abs(), target_number.copy_abs()),
                        }[p["denominator"]]
                        if denominator:
                            tolerance = max(tolerance, denominator * percent / 100)
                        elif difference:
                            tolerance = Decimal(0)
        else:

            def timestamp(value):
                d = iso_date(value)
                return datetime(d.year, d.month, d.day, tzinfo=UTC) if d else iso_timestamp(value)

            source_time, target_time = timestamp(source), timestamp(target)
            invalid = source_time is None or target_time is None
            if source_time is not None and target_time is not None:
                delta = source_time - target_time
                difference = (
                    Decimal(delta.days * 86400 + delta.seconds)
                    + Decimal(delta.microseconds) / 1000000
                )
                tolerance = Decimal(p.get("hours", p.get("days", "0"))) * (
                    3600 if "hours" in p else 86400
                )
        records.append(
            {
                "left": left,
                "right": right,
                "delta": format(difference.copy_abs(), "f") if difference is not None else None,
                "limit": format(tolerance, "f") if tolerance is not None else None,
                "invalid": invalid,
                "null_match": null_match,
            }
        )
        facts.append(
            {
                "code": rule["code"],
                "type": kind,
                "source_column": rule["source_column"],
                "target_column": rule["target_column"],
                "source_value": source,
                "target_value": target,
                "difference": str(difference) if difference is not None else None,
                "tolerance": str(tolerance) if tolerance is not None else None,
                "parameters": p,
                "invalid": invalid,
            }
        )
    # Fixed-width nonnegative decimal text has exactly the same ordering as
    # Decimal. Both native compilers can compare it without float conversion,
    # overflow, or rounding a 19th fractional digit in DECIMAL(38,18).
    numbers = [r[k] for r in records for k in ("delta", "limit") if r[k] is not None]
    integral = max((len(v.partition(".")[0]) for v in numbers), default=1)
    fractional = max((len(v.partition(".")[2]) for v in numbers), default=0)
    for record in records:
        for key in ("delta", "limit"):
            if record[key] is not None:
                before, _, after = record[key].partition(".")
                record[key] = before.zfill(integral) + after.ljust(fractional, "0")
    return pl.DataFrame(
        records,
        schema={
            "left": pl.String,
            "right": pl.String,
            "delta": pl.String,
            "limit": pl.String,
            "invalid": pl.Boolean,
            "null_match": pl.Boolean,
        },
    ), facts


def compile_rule(rule: RuleDefinition | dict, observed_at: datetime) -> PortableRuleExpression:
    definition = rule if isinstance(rule, RuleDefinition) else RuleDefinition.model_validate(rule)
    if definition.type not in {
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
    }:
        raise ValueError("La regla agregada requiere el compilador de métricas Sentinel.")
    observed = (
        observed_at.replace(tzinfo=UTC)
        if observed_at.tzinfo is None
        else observed_at.astimezone(UTC)
    )
    return PortableRuleExpression(definition, observed)


class ProcessingEngine(Protocol):
    family: str

    def validate(self, frame: pl.DataFrame, expression: PortableRuleExpression) -> list[bool]: ...

    def compare(
        self, frame: pl.DataFrame, expression: PortableComparisonExpression
    ) -> list[dict]: ...

    def measure(
        self,
        profile: dict,
        schema: list,
        expression: PortableMetricExpression,
        previous_schema: list | None = None,
        history: list | None = None,
    ) -> dict: ...


class MetricCompilerMixin(ProcessingEngine):
    def measure(
        self,
        profile: dict,
        schema: list,
        expression: PortableMetricExpression,
        previous_schema: list | None = None,
        history: list | None = None,
    ) -> dict:
        return _evaluate_metric(self, profile, schema, expression, previous_schema, history)


class PolarsCompiler(MetricCompilerMixin):
    family = "POLARS"

    def compare(self, frame: pl.DataFrame, expression: PortableComparisonExpression) -> list[dict]:
        prepared, facts = _prepare_comparison(frame, expression)
        kind = expression.definition["type"]
        predicate = (
            (pl.col("left") == pl.col("right"))
            if kind == "exact_compare"
            else (pl.col("delta") <= pl.col("limit"))
        )
        passed = prepared.select(
            ((predicate.fill_null(False) | pl.col("null_match")) & ~pl.col("invalid")).alias(
                "passed"
            )
        )["passed"].to_list()
        return [dict(fact, passed=passed[i]) for i, fact in enumerate(facts)]

    def expression(
        self, expression: PortableRuleExpression, range_bounds: dict[str, str] | None = None
    ) -> pl.Expr:
        rule, observed = expression.definition, expression.observed_at
        p, kind = rule.parameters, rule.type
        assert rule.column is not None
        col = pl.col(rule.column).cast(pl.String)
        numeric = col.str.contains(DECIMAL_PATTERN)
        parsed_date = col.str.strptime(pl.Date, "%Y-%m-%d", strict=False, exact=True)
        valid_date = (
            col.str.contains(DATE_PATTERN)
            & ~col.str.starts_with("0000-")
            & parsed_date.is_not_null()
        )
        if kind == "required":
            return col.is_not_null() & (col != "")
        if kind == "not_null":
            return col.is_not_null()
        if kind == "unique":
            valid = pl.col(rule.column).count().over(rule.column) == 1
        elif kind == "numeric":
            valid = numeric
        elif kind == "positive":
            valid = numeric & ~col.str.starts_with("-") & ~col.str.contains(r"^\+?0+(?:\.0+)?$")
        elif kind == "range":
            if range_bounds is None:
                raise ValueError("range requires the exact decimal relation; use validate().")
            number = pl.col("__tv_decimal_key")
            valid = number.is_not_null()
            for operator in ("gt", "gte", "lt", "lte"):
                if operator in range_bounds:
                    boundary = pl.lit(range_bounds[operator])
                    valid &= {
                        "gt": number > boundary,
                        "gte": number >= boundary,
                        "lt": number < boundary,
                        "lte": number <= boundary,
                    }[operator]
        elif kind == "allowed_values":
            valid = col.is_in([str(v) for v in p["values"] if v is not None])
        elif kind == "regex":
            valid = col.str.contains(portable_regex_pattern(p))
        elif kind == "date_rule":
            valid = valid_date
            if p.get("not_future"):
                valid &= parsed_date <= observed.date()
            for bound in ("min", "max"):
                if bound in p:
                    target = date.fromisoformat(p[bound])
                    valid &= parsed_date >= target if bound == "min" else parsed_date <= target
        elif kind == "type":
            logical = p["logical_type"]
            if logical == "DATE":
                valid = valid_date
            elif logical == "TIMESTAMP":
                parsed = col.str.to_datetime("%+", strict=False, time_zone="UTC")
                valid = col.str.contains(TIMESTAMP_PATTERN) & parsed.is_not_null()
            elif logical == "INT64":
                valid = (
                    col.str.contains(r"^[+-]?[0-9]+$")
                    & col.cast(pl.Int64, strict=False).is_not_null()
                )
            elif logical == "DECIMAL":
                valid = numeric
            elif logical == "BOOLEAN":
                valid = col.is_in(["true", "false", "True", "False"])
            else:
                valid = pl.lit(True)
        else:
            raise ValueError(f"Regla no soportada por {self.family}: {kind}")
        allow_null = p.get("null_policy", "ALLOW") != "FAIL"
        return pl.when(col.is_null()).then(pl.lit(allow_null)).otherwise(valid.fill_null(False))

    def validate(self, frame: pl.DataFrame, expression: PortableRuleExpression) -> list[bool]:
        bounds = None
        if expression.definition.type == "range":
            frame, bounds = _range_relation(frame, expression)
        return frame.select(self.expression(expression, bounds).alias("valid"))["valid"].to_list()


class DuckDBCompiler(MetricCompilerMixin):
    family = "DUCKDB"

    def compare(self, frame: pl.DataFrame, expression: PortableComparisonExpression) -> list[dict]:
        prepared, facts = _prepare_comparison(frame, expression)
        predicate = (
            '"left" = "right"'
            if expression.definition["type"] == "exact_compare"
            else 'delta <= "limit"'
        )
        with duckdb.connect(":memory:") as connection:
            connection.execute(
                'CREATE TABLE comparisons ("left" VARCHAR, "right" VARCHAR, delta VARCHAR, '
                '"limit" VARCHAR, invalid BOOLEAN, null_match BOOLEAN, row_number BIGINT)'
            )
            if prepared.height:
                connection.executemany(
                    "INSERT INTO comparisons VALUES (?,?,?,?,?,?,?)",
                    [row + (i,) for i, row in enumerate(prepared.iter_rows())],
                )
            passed = [
                r[0]
                for r in connection.execute(
                    f"SELECT (COALESCE(({predicate}),FALSE) OR null_match) AND NOT invalid "
                    "FROM comparisons ORDER BY row_number"
                ).fetchall()
            ]
        return [dict(fact, passed=passed[i]) for i, fact in enumerate(facts)]

    def expression(
        self, expression: PortableRuleExpression, range_bounds: dict[str, str] | None = None
    ) -> tuple[str, list]:
        rule, observed = expression.definition, expression.observed_at
        p, kind = rule.parameters, rule.type
        col = '"' + str(rule.column).replace('"', '""') + '"'
        params: list = []

        def parameter(value) -> str:
            params.append(value)
            return "?"

        numeric = f"regexp_full_match({col}, '{DECIMAL_PATTERN}')"
        parsed_date = f"TRY_CAST({col} AS DATE)"
        valid_date = (
            f"(regexp_full_match({col}, '{DATE_PATTERN}') AND NOT starts_with({col}, '0000-') "
            f"AND {parsed_date} IS NOT NULL)"
        )
        if kind == "required":
            return f"({col} IS NOT NULL AND {col} <> '')", []
        if kind == "not_null":
            return f"{col} IS NOT NULL", []
        if kind == "unique":
            valid = f"COUNT({col}) OVER (PARTITION BY {col}) = 1"
        elif kind == "numeric":
            valid = numeric
        elif kind == "positive":
            valid = (
                f"{numeric} AND NOT starts_with({col}, '-') "
                f"AND NOT regexp_full_match({col}, '\\+?0+(?:\\.0+)?')"
            )
        elif kind == "range":
            if range_bounds is None:
                raise ValueError("range requires the exact decimal relation; use validate().")
            pieces = ['"__tv_decimal_key" IS NOT NULL']
            for operator, sql in (("gt", ">"), ("gte", ">="), ("lt", "<"), ("lte", "<=")):
                if operator in range_bounds:
                    pieces.append(f'"__tv_decimal_key" {sql} {parameter(range_bounds[operator])}')
            valid = " AND ".join(pieces)
        elif kind == "allowed_values":
            values = [str(v) for v in p["values"] if v is not None]
            valid = f"{col} IN ({', '.join(parameter(v) for v in values)})" if values else "FALSE"
        elif kind == "regex":
            valid = f"regexp_matches({col}, {parameter(portable_regex_pattern(p))})"
        elif kind == "date_rule":
            pieces = [valid_date]
            if p.get("not_future"):
                pieces.append(
                    f"{parsed_date} <= CAST({parameter(observed.date().isoformat())} AS DATE)"
                )
            for bound, sql in (("min", ">="), ("max", "<=")):
                if bound in p:
                    pieces.append(f"{parsed_date} {sql} CAST({parameter(p[bound])} AS DATE)")
            valid = " AND ".join(pieces)
        elif kind == "type":
            logical = p["logical_type"]
            valid = {
                "STRING": "TRUE",
                "DECIMAL": numeric,
                "DATE": valid_date,
                "INT64": (
                    f"regexp_full_match({col}, '[+-]?[0-9]+') "
                    f"AND TRY_CAST({col} AS BIGINT) IS NOT NULL"
                ),
                "TIMESTAMP": (
                    f"regexp_full_match({col}, '{TIMESTAMP_PATTERN}') "
                    f"AND TRY_CAST({col} AS TIMESTAMPTZ) IS NOT NULL"
                ),
                "BOOLEAN": f"{col} IN ('true','false','True','False')",
            }[logical]
        else:
            raise ValueError(f"Regla no soportada por {self.family}: {kind}")
        allow_null = "FALSE" if p.get("null_policy", "ALLOW") == "FAIL" else "TRUE"
        return (
            f"CASE WHEN {col} IS NULL THEN {allow_null} ELSE COALESCE(({valid}), FALSE) END",
            params,
        )

    def validate(self, frame: pl.DataFrame, expression: PortableRuleExpression) -> list[bool]:
        bounds = None
        if expression.definition.type == "range":
            frame, bounds = _range_relation(frame, expression)
        sql, parameters = self.expression(expression, bounds)
        # Register a bounded relation without requiring Arrow/Pandas or trusting column identifiers.
        with duckdb.connect(":memory:") as connection:
            definition = ", ".join('"' + c.replace('"', '""') + '" VARCHAR' for c in frame.columns)
            connection.execute(f"CREATE TABLE rule_input ({definition}, __tv_row_number BIGINT)")
            if frame.height:
                placeholders = ",".join("?" for _ in range(frame.width + 1))
                connection.executemany(
                    f"INSERT INTO rule_input VALUES ({placeholders})",
                    [
                        tuple(None if v is None else str(v) for v in row) + (i,)
                        for i, row in enumerate(frame.iter_rows())
                    ],
                )
            return [
                row[0]
                for row in connection.execute(
                    f"SELECT {sql} AS valid FROM rule_input ORDER BY __tv_row_number", parameters
                ).fetchall()
            ]
