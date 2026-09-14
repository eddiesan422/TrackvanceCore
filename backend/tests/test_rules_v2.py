"""Observed-value semantics and native portable rule parity for correction cycle v2."""

from copy import deepcopy
from datetime import UTC, datetime
from decimal import Decimal

import polars as pl
import pytest
from pydantic import ValidationError

from trackvance.config_semantics import (
    ConfigurationError,
    RuleDefinition,
    effective_config,
    validate_config,
)
from trackvance.portable_engine import (
    DuckDBCompiler,
    PolarsCompiler,
    compile_comparison,
    compile_metric,
    compile_rule,
)
from trackvance.processing import (
    ProcessingError,
    csv_record_lines,
    intake,
    profile_frame,
    read_csv,
    reconcile,
    sentinel,
)

NOW = datetime(2026, 9, 13, 12, tzinfo=UTC)


def rule(kind, parameters=None, column="value", **kwargs):
    return {"type": kind, "column": column, "parameters": parameters or {}, **kwargs}


def comparison(kind="numeric_tolerance", parameters=None, source="value", target="value"):
    return {
        "type": kind,
        "source_column": source,
        "target_column": target,
        "parameters": parameters or {},
    }


def recon_config(*comparisons, **kwargs):
    return {
        "schema_version": 2,
        "key_columns": ["id"],
        "comparison_rules": list(comparisons),
        **kwargs,
    }


def test_profile_preserves_whitespace_case_unicode_null_and_empty(tmp_path):
    path = tmp_path / "observed.csv"
    path.write_text(
        'customer_name\nCliente 12\n  Cliente 12  \ncliente 12\nCAFÉ\nCAFE\u0301\n\n""\n',
        encoding="utf-8",
    )
    frame = read_csv(path)
    assert frame["customer_name"].to_list() == [
        "Cliente 12",
        "  Cliente 12  ",
        "cliente 12",
        "CAFÉ",
        "CAFE\u0301",
        None,
        "",
    ]
    _, profile, _ = profile_frame(frame)
    column = profile["columns"][0]
    assert column["distinct_count"] == 6
    assert column["null_count"] == 1
    assert column["distinct_rate"] == 1
    assert profile["null_policy"] == "NULL_EXCLUDED_EMPTY_PRESERVED"
    assert profile["profiling_policy"] == "OBSERVED_EXACT_V2"


@pytest.mark.parametrize(
    "values,expected",
    [
        (["2024-02-29", "2026-09-13", None], "DATE"),
        (["2023-02-29", "2026-09-13"], "STRING"),
        (["2026-09-13", "13/09/2026"], "STRING"),
        (["2026-09-13", ""], "STRING"),
        ([None, None], "STRING"),
        (["2026-09-13T12:00:00Z", None, "2026-09-13T07:00:00-05:00"], "TIMESTAMP"),
        (["2026-09-13T12:00:00", "2026-09-13"], "STRING"),
        (["2026-09-13T12:00:00Z", "2026-09-13"], "STRING"),
        (["0000-01-01"], "STRING"),
    ],
)
def test_unambiguous_date_inference(values, expected):
    schema, _, _ = profile_frame(
        pl.DataFrame({"transaction_date": values}, schema={"transaction_date": pl.String})
    )
    assert schema[0]["logical_type"] == expected


def test_identifier_heuristic_and_explicit_override_preserve_original_values():
    frame = pl.DataFrame({"document_id": ["001234567", "1234567"], "amount": ["001", "2.50"]})
    schema, profile, _ = profile_frame(frame)
    assert schema[0]["semantic_tag"] == "IDENTIFIER"
    assert schema[0]["logical_type"] == "STRING"
    assert schema[1]["logical_type"] == "DECIMAL"
    assert profile["columns"][0]["distinct_count"] == 2
    overridden, _, _ = profile_frame(
        frame, {"document_id": {"logical_type": "DECIMAL", "semantic_tag": None}}
    )
    assert overridden[0]["logical_type"] == "DECIMAL"
    assert frame["document_id"].to_list() == ["001234567", "1234567"]
    with pytest.raises(ProcessingError, match="override"):
        profile_frame(pl.DataFrame({"x": ["not a date"]}), {"x": "DATE"})


def test_intake_normalizes_only_ordered_explicit_transforms():
    frame = pl.DataFrame({"id": [" A ", "a", ""], "untouched": [" A ", "B", ""]})
    _, raw_metrics, raw = intake(frame, {"unique_columns": ["id"]})
    assert raw_metrics["error_rows"] == 0
    assert raw.equals(frame)
    errors, metrics, accepted = intake(
        frame,
        {
            "required_columns": ["id"],
            "unique_columns": ["id"],
            "transforms": [
                {"type": "trim", "column": "id"},
                {"type": "case", "column": "id", "parameters": {"case": "UPPER"}},
                {"type": "empty_to_null", "column": "id"},
            ],
        },
    )
    assert metrics["error_rows"] == 3
    assert accepted.height == 0
    assert {r["received_value"] for r in errors} == {" A ", "a", ""}
    assert frame["untouched"].to_list() == [" A ", "B", ""]


@pytest.mark.parametrize(
    "kind,parameters,values,expected",
    [
        (
            "range",
            {"gte": "1.00", "lt": "2"},
            ["1", "1.99999999999999999999", "2", "-1", "bad", None],
            [True, True, False, False, False, True],
        ),
        (
            "range",
            {"gt": "-2", "lte": "-1.00000000000000000001", "null_policy": "FAIL"},
            ["-2", "-1.00000000000000000002", "-1", None],
            [False, True, False, False],
        ),
        (
            "allowed_values",
            {"values": ["A", "Á", ""], "null_policy": "FAIL"},
            ["A", "Á", "a", " A", "", None],
            [True, True, False, False, True, False],
        ),
        (
            "regex",
            {"pattern": r"^café[0-9]+$", "flags": "i"},
            ["CAFÉ12", "café2", "cafe2", "café１２", None],
            [True, True, False, False, True],
        ),
        (
            "regex",
            {"pattern": r"^\d+$"},
            ["123", "１２３", "١٢٣", "abc"],
            [True, False, False, False],
        ),
        (
            "date_rule",
            {"not_future": True, "min": "2024-02-29", "max": "2026-12-31"},
            ["2024-02-29", "2026-09-13", "2026-09-14", "2023-02-29", "2026-09-13T00:00:00Z", None],
            [True, True, False, False, False, True],
        ),
        (
            "date_rule",
            {"not_future": True, "null_policy": "FAIL"},
            ["0000-01-01", "2026-02-30", "2026-09-13", None],
            [False, False, True, False],
        ),
        (
            "unique",
            {},
            ["A", "A", " A ", "a", None, None, ""],
            [False, False, True, True, True, True, True],
        ),
        (
            "type",
            {"logical_type": "TIMESTAMP"},
            [
                "2026-09-13T12:00:00Z",
                "2026-09-13T07:00:00-05:00",
                "2026-09-13",
                "2026-09-13T12:00:00",
                "2026-09-13T12:00:00+24:00",
            ],
            [True, True, False, False, False],
        ),
        (
            "numeric",
            {"null_policy": "FAIL"},
            [
                "12345678901234567890123456789012345678901234567890.01",
                "NaN",
                "1e3",
                "1,00",
                "",
                None,
            ],
            [True, False, False, False, False, False],
        ),
        (
            "positive",
            {},
            ["0", "-0", "+000.00", "0.00000000000000000001", "-0.01", None],
            [False, False, False, True, False, True],
        ),
    ],
)
def test_portable_rule_native_polars_duckdb_parity(kind, parameters, values, expected):
    frame = pl.DataFrame({"value": values}, schema={"value": pl.String})
    expression = compile_rule(rule(kind, parameters), NOW)
    assert PolarsCompiler().validate(frame, expression) == expected
    assert DuckDBCompiler().validate(frame, expression) == expected


@pytest.mark.parametrize("null_policy,failed", [("ALLOW", 0), ("IGNORE", 0), ("FAIL", 1)])
def test_intake_declared_null_policy_and_warning_decision(null_policy, failed):
    errors, metrics, accepted = intake(
        pl.DataFrame({"value": ["A", None]}),
        {
            "rules": [
                rule(
                    "allowed_values",
                    {"values": ["A"], "null_policy": null_policy},
                    severity="WARNING",
                )
            ]
        },
        observed_at=NOW,
    )
    assert len(errors) == failed
    assert metrics["warning_rows"] == failed
    assert metrics["error_rows"] == 0
    assert accepted.height == 2
    assert metrics["decision"] == ("APPROVED_WITH_WARNINGS" if failed else "APPROVED")


@pytest.mark.parametrize(
    "definition",
    [
        rule("range", {"gte": 2, "lte": 1}),
        rule("range", {"gt": 1, "lte": 1}),
        rule("range", {"gte": 1, "gt": 0}),
        rule("range", {}),
        rule("range", {"gte": "NaN"}),
        rule("allowed_values", {"values": [{"a": 1}]}),
        rule("regex", {"pattern": r"(.)\1"}),
        rule("regex", {"pattern": "(?=bad)"}),
        rule("regex", {"pattern": "["}),
        rule("regex", {"pattern": "x", "flags": "e"}),
        rule("date_rule", {"min": "2026-02-30"}),
        rule("date_rule", {"min": "2026-09-14", "max": "2026-09-13"}),
        rule("date_rule", {"not_future": "true"}),
        rule("date_rule", {}),
    ],
)
def test_invalid_rule_declarations_fail_before_execution(definition):
    with pytest.raises((ValidationError, ConfigurationError)):
        RuleDefinition.model_validate(definition)


def test_recon_key_policy_explicit_and_legacy_adapter_are_immutable():
    source = pl.DataFrame({"id": [" A "], "value": ["1"]})
    target = pl.DataFrame({"id": ["A"], "value": ["1"]})
    legacy = {"key_columns": ["id"], "amount_column": "value", "tolerance": "0"}
    snapshot = deepcopy(legacy)
    rows, metrics = reconcile(source, target, legacy)
    assert rows[0]["classification"] == "MATCH"
    assert legacy == snapshot
    assert metrics["diagnostics"]["key_normalization_policy"] == "LEGACY_V1_TRIM_EMPTY_NULL"
    assert effective_config("RECON", legacy)["comparison_rules"][0]["legacy_alias"] == "EXACT_MATCH"
    _, explicit = reconcile(source, target, recon_config(comparison()))
    assert explicit["source_only"] == explicit["target_only"] == 1
    _, trimmed = reconcile(
        source, target, recon_config(comparison(), key_normalization={"trim": True})
    )
    assert trimmed["matched"] == 1


def test_recon_key_unicode_and_case_normalization_is_declared():
    source = pl.DataFrame({"id": [" café "], "value": ["1"]})
    target = pl.DataFrame({"id": ["CAFE\u0301"], "value": ["1"]})
    _, metrics = reconcile(
        source,
        target,
        recon_config(
            comparison(),
            key_normalization={"trim": True, "case": "UPPER", "unicode_normalization": "NFC"},
        ),
    )
    assert metrics["matched"] == 1
    assert metrics["diagnostics"]["key_normalization"]["unicode_normalization"] == "NFC"


def test_recon_multiple_exact_comparisons_do_not_mean_numeric_tolerance():
    source = pl.DataFrame({"id": ["A", "B"], "value": ["1.0", " x "], "city": ["BOG", "CALI"]})
    target = pl.DataFrame({"id": ["A", "B"], "value": ["1.00", "x"], "city": ["BOG", "cali"]})
    rows, metrics = reconcile(
        source,
        target,
        recon_config(
            comparison("exact_compare", {"normalization": {"trim": True}}),
            comparison("exact_compare", {"normalization": {"case": "UPPER"}}, "city", "city"),
        ),
    )
    assert [r["classification"] for r in rows] == ["VALUE_MISMATCH", "MATCH"]
    assert metrics["matched"] == 1
    assert all(len(r["comparisons"]) == 2 for r in rows)


@pytest.mark.parametrize(
    "definition,left,right,passed,invalid",
    [
        (
            comparison("exact_compare"),
            ["1", " a ", None],
            ["1.0", "a", None],
            [False, False, False],
            [False, False, False],
        ),
        (
            comparison("exact_compare", {"equal_nulls": True}),
            [None, "A"],
            [None, None],
            [True, False],
            [False, False],
        ),
        (
            comparison(parameters={"abs": "0.01"}),
            ["1", "1", "bad", None],
            ["1.01", "1.01000000000000000001", "2", None],
            [True, False, False, False],
            [False, False, True, True],
        ),
        (
            comparison(parameters={"abs": "0", "percent": "5", "denominator": "SOURCE"}),
            ["100", "100", "0", "0"],
            ["105", "105.00000000000000000001", "0", "0.01"],
            [True, False, True, False],
            [False, False, False, False],
        ),
        (
            comparison(parameters={"percent": "5", "denominator": "TARGET"}),
            ["105"],
            ["100"],
            [True],
            [False],
        ),
        (
            comparison(parameters={"percent": "5", "denominator": "MAX_ABS"}),
            ["-105"],
            ["-100"],
            [True],
            [False],
        ),
        (
            comparison("date_tolerance", {"days": "1"}),
            ["2026-09-13", "2026-09-13", "2026-02-30"],
            ["2026-09-14", "2026-09-15", "2026-03-02"],
            [True, False, False],
            [False, False, True],
        ),
        (
            comparison("date_tolerance", {"hours": "0"}),
            ["2026-09-13T12:00:00Z"],
            ["2026-09-13T07:00:00-05:00"],
            [True],
            [False],
        ),
    ],
)
def test_native_comparison_parity(definition, left, right, passed, invalid):
    frame = pl.DataFrame(
        {"__tv_source": left, "__tv_target": right},
        schema={"__tv_source": pl.String, "__tv_target": pl.String},
    )
    expression = compile_comparison(definition)
    polars_result = PolarsCompiler().compare(frame, expression)
    assert DuckDBCompiler().compare(frame, expression) == polars_result
    assert [r["passed"] for r in polars_result] == passed
    assert [r["invalid"] for r in polars_result] == invalid


@pytest.mark.parametrize(
    "operation,values,expected",
    [("sum", ["1.10", "2.20"], "3.30"), ("count", ["ignored", None], "2")],
)
def test_recon_one_to_many_aggregate_preserves_lineage(operation, values, expected):
    source = pl.DataFrame({"id": ["A"], "value": [expected]})
    target = pl.DataFrame({"id": ["A", "A"], "value": values})
    rows, metrics = reconcile(
        source,
        target,
        recon_config(
            comparison(target="aggregate"),
            aggregation={
                "side": "TARGET",
                "operation": operation,
                "column": "value",
                "output_column": "aggregate",
            },
        ),
    )
    assert metrics["matched"] == 1
    assert metrics["duplicate_target"] == 0
    assert rows[0]["target_rows"] == [2, 3]
    assert Decimal(rows[0]["target_value"]) == Decimal(expected)


def test_sentinel_distinct_unique_schema_change_and_row_rules():
    frame = pl.DataFrame(
        {"value": ["A", "A", "B", None], "date": ["2026-09-13", "2026-09-14", "2026-09-13", None]}
    )
    schema, profile, _ = profile_frame(frame)
    checks, metrics = sentinel(
        profile,
        schema,
        {
            "rules": [
                rule("distinct_count", {"min": 2, "max": 2}),
                rule("distinct_rate", {"min": "0.6", "max": "0.7"}),
                rule("uniqueness_ratio", {"min": "0.5"}),
                rule("schema_type", {"expected_type": "DECIMAL"}),
                rule("date_rule", {"not_future": True}, column="date"),
            ]
        },
        NOW,
        None,
        observed_at=NOW,
        frame=frame,
        previous_schema=[{"name": "value", "logical_type": "DECIMAL"}],
    )
    by_code = {c["code"]: c for c in checks}
    assert by_code["DISTINCT_COUNT"]["status"] == "PASS"
    assert by_code["DISTINCT_RATE"]["status"] == "PASS"
    assert by_code["UNIQUENESS_RATIO"]["status"] == "FAIL"
    assert by_code["SCHEMA_TYPE"]["status"] == "FAIL"
    assert by_code["DATE_RULE"]["failed_count"] == 1
    assert metrics["failed_checks"] == 3


def test_sentinel_iqr_uses_only_compatible_successful_history():
    schema, profile, _ = profile_frame(pl.DataFrame({"value": [str(i) for i in range(30)]}))
    compatible = {
        "metric_key": "row_count",
        "method": "EXACT_OBSERVED",
        "metric_definition_version": 2,
        "status": "SUCCESS",
    }
    history = [{**compatible, "numeric_value": str(n)} for n in [9, 10, 11, 12]]
    history += [
        {**compatible, "numeric_value": "1000", **override}
        for override in [
            {"status": "FAILED"},
            {"method": "LEGACY_NORMALIZED"},
            {"metric_definition_version": 1},
            {"metric_key": "distinct_count:value"},
        ]
    ]
    checks, _ = sentinel(
        profile,
        schema,
        {
            "rules": [
                rule(
                    "historical_band",
                    {"metric": "row_count", "min_history": 4, "window": 10},
                    column=None,
                    scope="dataset",
                )
            ]
        },
        NOW,
        None,
        observed_at=NOW,
        history=history,
    )
    check = next(c for c in checks if c["code"] == "HISTORICAL_BAND")
    assert check["status"] == "FAIL"
    assert check["expected"]["history_count"] == 4
    assert Decimal(check["expected"]["median"]) == Decimal("10.5")
    assert Decimal(check["expected"]["iqr"]) == Decimal("1.5")
    assert check["metric_method"] == "EXACT_OBSERVED"
    assert check["metric_definition_version"] == 2


def test_published_config_makes_normalization_explicit_and_rejects_invalid_aggregation():
    config = validate_config("RECON", recon_config(comparison()))
    assert config["key_normalization"] == {
        "trim": False,
        "case": "NONE",
        "unicode_normalization": "NONE",
    }
    assert config["schema_version"] == 2
    with pytest.raises(ConfigurationError, match="Agregación"):
        validate_config(
            "RECON", recon_config(comparison(), aggregation={"side": "TARGET", "operation": "eval"})
        )


def test_intake_rejects_sentinel_metric_rule_before_publication():
    with pytest.raises(ConfigurationError, match="Sentinel"):
        validate_config("INTAKE", {"rules": [rule("distinct_count", {"min": 1})]})


def test_csv_multiline_physical_line_numbers_are_preserved_in_intake_and_recon(tmp_path):
    path = tmp_path / "multiline.csv"
    path.write_text('id,value,note\nA,1,"two\nlines"\nB,-1,normal\n', encoding="utf-8")
    frame = read_csv(path)
    physical_lines = csv_record_lines(path)
    assert physical_lines == [2, 4]
    errors, _, _ = intake(frame, {"positive_columns": ["value"]}, input_row_numbers=physical_lines)
    assert errors[0]["original_row_number"] == 4
    target = pl.DataFrame({"id": ["B"], "value": ["0"]})
    rows, _ = reconcile(
        frame,
        target,
        recon_config(comparison()),
        source_row_numbers=physical_lines,
        target_row_numbers=[8],
    )
    mismatch = next(r for r in rows if r["classification"] == "VALUE_MISMATCH")
    assert mismatch["source_row"] == 4
    assert mismatch["target_row"] == 8
    with pytest.raises(ProcessingError, match="mapa de líneas"):
        intake(frame, {"required_columns": ["id"]}, input_row_numbers=[2])


@pytest.mark.parametrize("side", ["SOURCE", "TARGET"])
def test_recon_aggregate_invalid_decimal_is_finding_and_preserves_all_input_lines(side):
    many = pl.DataFrame({"id": ["A", "A"], "value": ["1", "invalid"]})
    one = pl.DataFrame({"id": ["A"], "value": ["1"]})
    source, target = (many, one) if side == "SOURCE" else (one, many)
    rows, metrics = reconcile(
        source,
        target,
        recon_config(
            comparison(
                source="total" if side == "SOURCE" else "value",
                target="total" if side == "TARGET" else "value",
            ),
            aggregation={
                "side": side,
                "operation": "sum",
                "column": "value",
                "output_column": "total",
            },
        ),
    )
    assert metrics["invalid"] == 1
    assert rows[0][f"{side.lower()}_rows"] == [2, 3]
    assert rows[0]["classification"] == "INVALID"


def test_empty_and_null_keys_never_join_but_whitespace_policy_is_explicit():
    source = pl.DataFrame({"id": [None, "", " "], "value": ["1", "1", "1"]})
    target = pl.DataFrame({"id": [" "], "value": ["1"]})
    _, raw = reconcile(source, target, recon_config(comparison()))
    assert raw["matched"] == 1
    assert raw["invalid"] == 2
    _, trimmed = reconcile(
        source, target, recon_config(comparison(), key_normalization={"trim": True})
    )
    assert trimmed["matched"] == 0
    assert trimmed["invalid"] == 4


@pytest.mark.parametrize("compiler", [PolarsCompiler, DuckDBCompiler])
def test_empty_relation_and_literal_sql_like_values_are_safe(compiler):
    expression = compile_rule(
        rule("allowed_values", {"values": ["'); DROP TABLE rule_input; --"]}), NOW
    )
    engine = compiler()
    assert (
        engine.validate(pl.DataFrame({"value": []}, schema={"value": pl.String}), expression) == []
    )
    assert engine.validate(
        pl.DataFrame({"value": ["'); DROP TABLE rule_input; --", "different"]}), expression
    ) == [True, False]


def test_sentinel_history_fallback_is_explicit_and_ignores_failed_run():
    schema, profile, _ = profile_frame(pl.DataFrame({"value": ["a", "b", "c"]}))
    checks, _ = sentinel(
        profile,
        schema,
        {
            "rules": [
                rule(
                    "historical_band",
                    {"metric": "row_count", "min": 1, "max": 5},
                    column=None,
                    scope="dataset",
                )
            ]
        },
        NOW,
        None,
        observed_at=NOW,
        history=[
            {
                "metric_key": "row_count",
                "method": "EXACT_OBSERVED",
                "metric_definition_version": 2,
                "numeric_value": "1000",
                "status": "FAILED",
            }
        ],
    )
    check = next(c for c in checks if c["code"] == "HISTORICAL_BAND")
    assert check["status"] == "PASS"
    assert check["expected"]["method"] == "FIXED_THRESHOLD_FALLBACK"
    assert check["expected"]["history_count"] == 0
    assert check["expected"]["max"] == "5"


@pytest.mark.parametrize(
    "definition,expected_passed",
    [
        (rule("distinct_count", {"min": "2", "max": "2"}), True),
        (rule("distinct_rate", {"min": "0.6", "max": "0.7"}), True),
        (rule("uniqueness_ratio", {"min": "0.5"}), False),
        (rule("schema_type", {"expected_type": "DECIMAL"}), False),
        (rule("schema_type", {}), False),
        (
            rule(
                "metric_threshold",
                {"metric": "row_count", "operator": "eq", "threshold": "4"},
                column=None,
                scope="dataset",
            ),
            True,
        ),
        (
            rule(
                "metric_threshold",
                {"metric": "row_count", "operator": "lt", "threshold": "4"},
                column=None,
                scope="dataset",
            ),
            False,
        ),
        (rule("distinct_count", {"min": "1"}, column="missing"), False),
        (
            rule(
                "historical_band",
                {"metric": "row_count", "min_history": 4, "window": 10},
                column=None,
                scope="dataset",
            ),
            False,
        ),
    ],
)
def test_compiled_metric_predicates_have_native_polars_duckdb_parity(definition, expected_passed):
    schema, profile, _ = profile_frame(pl.DataFrame({"value": ["A", "A", "B", None]}))
    history = [
        {
            "metric_key": "row_count",
            "numeric_value": str(value),
            "method": "EXACT_OBSERVED",
            "metric_definition_version": 2,
            "status": "SUCCESS",
        }
        for value in [10, 11, 12, 13]
    ]
    expression = compile_metric(definition, NOW)
    previous = [{"name": "value", "logical_type": "DECIMAL"}]
    polars_result = PolarsCompiler().measure(profile, schema, expression, previous, history)
    assert DuckDBCompiler().measure(profile, schema, expression, previous, history) == polars_result
    assert polars_result["passed"] is expected_passed


def test_sentinel_schema_type_previous_mode_establishes_then_compares_baseline():
    configuration = validate_config(
        "SENTINEL", {"rules": [rule("schema_type", {}, "transaction_date")]}
    )
    previous_schema, previous_profile, _ = profile_frame(
        pl.DataFrame({"transaction_date": ["2026-09-13"]})
    )
    baseline_checks, _ = sentinel(
        previous_profile, previous_schema, configuration, NOW, None, observed_at=NOW
    )
    baseline = next(c for c in baseline_checks if c["code"] == "SCHEMA_TYPE")
    assert baseline["status"] == "PASS"
    assert baseline["expected"]["method"] == "INITIAL_BASELINE"
    schema, profile, _ = profile_frame(pl.DataFrame({"transaction_date": ["not a date"]}))
    checks, _ = sentinel(
        profile,
        schema,
        configuration,
        NOW,
        previous_profile,
        observed_at=NOW,
        previous_schema=previous_schema,
    )
    drift = next(c for c in checks if c["code"] == "SCHEMA_TYPE")
    assert drift["status"] == "FAIL"
    assert drift["actual"] == "STRING"
    assert drift["expected"]["logical_type"] == "DATE"
    assert drift["expected"]["method"] == "PREVIOUS_SCHEMA"
