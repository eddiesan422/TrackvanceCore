"""Unit guards plus opt-in real Spark parity, run by the dedicated Spark suite.

TRACKVANCE_SPARK_TESTS=1 makes missing Java/runtime a failure instead of a skip.
The standalone harness sets TRACKVANCE_SPARK_MASTER to its isolated master.
"""

import json
import os
import threading
import time
from dataclasses import replace
from datetime import UTC, datetime

import polars as pl
import pytest

from trackvance.config_semantics import effective_config
from trackvance.planner import ExecutionPlanner, ResourceBudget, WorkloadInput
from trackvance.portable_engine import PolarsCompiler, compile_rule
from trackvance.processing import ProcessingError, intake, profile_frame, reconcile, sentinel
from trackvance.spark_engine import (
    RECORD_NUMBER_COLUMN,
    PySparkProcessingEngine,
    SparkDataset,
    SparkRuntime,
    SparkSettings,
    _batches,
    _physical_lines,
    _read_parts,
    _recon_key,
    _recon_partition,
    _stream_global_matches,
    _transform_partition,
    runtime_status,
)

NOW = datetime(2026, 10, 3, 12, tzinfo=UTC)


@pytest.mark.parametrize("field,value", [("driver_memory_mb", 0), ("batch_rows", 10001),
                                         ("executor_cores", 0), ("partitions", 257)])
def test_spark_settings_reject_invalid_limits(field, value):
    with pytest.raises(ValueError):
        replace(SparkSettings(), **{field: value})


@pytest.mark.parametrize("master", ["local[*]", "local[0]", "http://host", "spark://user:secret@host:7077"])
def test_spark_master_is_resource_bounded_and_not_a_secret_locator(master):
    with pytest.raises(ValueError):
        SparkSettings(master=master)


def test_explicit_engine_keeps_resources_and_no_fallback(tmp_path):
    planner = ExecutionPlanner(ResourceBudget(memory_soft_bytes=1000), tmp_path, spark_available=True)
    inputs = [WorkloadInput(1000000, 4, 100 * 1024 ** 2)]
    polars = planner.plan("intake", inputs, {}, free_bytes=1024 ** 3, requested_engine="POLARS")
    assert not polars["allowed"] and polars["rejection_code"] == "RESOURCE_MEMORY_INSUFFICIENT"
    spark = planner.plan("intake", inputs, {}, free_bytes=1024 ** 3, requested_engine="PYSPARK")
    assert spark["allowed"] and spark["engine"] == "PYSPARK" and spark["requested_engine"] == "PYSPARK"
    assert spark["spill_allowed"] and spark["budget_is_estimate"]
    unavailable = ExecutionPlanner(storage=tmp_path, spark_available=False).plan(
        "intake", [], {}, free_bytes=1024 ** 3, requested_engine="PYSPARK"
    )
    assert unavailable["rejection_code"] == "ENGINE_UNAVAILABLE"


def test_spark_memory_budget_is_checked_even_for_explicit_selection(tmp_path, monkeypatch):
    monkeypatch.setenv("TRACKVANCE_SPARK_MEMORY_BUDGET_BYTES", "1")
    plan = ExecutionPlanner(storage=tmp_path, spark_available=True).plan(
        "intake", [], {}, free_bytes=1024 ** 3, requested_engine="PYSPARK"
    )
    assert plan["rejection_code"] == "RESOURCE_SPARK_MEMORY_INSUFFICIENT"


def test_legacy_physical_lines_are_streamed_for_multiline_csv(tmp_path):
    path = tmp_path / "multiline.csv"
    path.write_text('id,value\n001,"uno\ndos"\n002,tres\n', encoding="utf-8")
    assert list(_physical_lines(str(path), ",")) == [2, 4]


def test_recon_skew_limit_fails_before_truncating_evidence():
    config = {"key_columns": ["id"], "amount_column": "amount", "tolerance": 0}
    rows = iter([((1, "same"), (0, index, {"id": "same", "amount": "1"})) for index in range(4)])
    with pytest.raises(ProcessingError, match="RESOURCE_RECON_GROUP_LIMIT"):
        list(_recon_partition(rows, ("id", "amount"), ("id", "amount"), config, 3))


def test_byte_batches_use_observed_unicode_width_and_never_truncate():
    records = [(index, ({"x": "😀" * 3}, {"x": "😀" * 3})) for index in range(7)]
    batches = list(_batches(iter(records), 100, 88))
    assert [len(batch) for batch in batches] == [2, 2, 2, 1]
    assert [record for batch in batches for record in batch] == records


def test_arrow_reader_rejects_actual_record_width(tmp_path):
    path = tmp_path / "wide.parquet"
    pl.DataFrame({"x": ["😀" * 17]}).write_parquet(path)
    value = SparkDataset((str(path),), ("x",), 1)
    with pytest.raises(ProcessingError, match="RESOURCE_SPARK_RECORD_LIMIT"):
        list(_read_parts(iter([(str(path), 0)]), value, 2048, max_record_bytes=99))


def test_ordered_transform_and_recon_key_guard_expanded_width():
    records = [(2, {"id": "1"})]
    transforms = [{"type": "id_padding", "column": "id", "parameters": {"width": 20}}]
    with pytest.raises(ProcessingError, match="RESOURCE_SPARK_RECORD_LIMIT"):
        list(_transform_partition(iter(records), ("id",), transforms, 2048, max_record_bytes=40))
    with pytest.raises(ProcessingError, match="RESOURCE_SPARK_RECORD_LIMIT"):
        _recon_key(records[0], 0, ["id"], {}, transforms, max_record_bytes=40)


def test_recon_group_byte_limit_fails_before_truncating_evidence():
    rows = iter([((1, "same"), (0, index, {"id": "same", "amount": "1"})) for index in range(4)])
    with pytest.raises(ProcessingError, match="RESOURCE_RECON_GROUP_LIMIT"):
        list(_recon_partition(rows, ("id", "amount"), ("id", "amount"),
                              {"key_columns": ["id"], "amount_column": "amount"}, 100,
                              max_group_bytes=150))


def test_global_lookup_streams_hot_keys_without_population_buffer():
    # The first active result must be emitted before requesting a later row.
    def values():
        yield (("hot",), 0), True
        yield (("hot",), 1), (5, True)
        raise AssertionError("The hot-key population was consumed before emitting a result")

    assert next(_stream_global_matches(values())) == (5, (True, True))
    records = [((("a",), 0), False), ((("a",), 1), (1, True)),
               ((("b",), 1), (2, True)), ((("c",), 0), True), ((("c",), 1), (3, True))]
    assert list(_stream_global_matches(iter(records))) == [(1, (False, True)), (2, (False, True)), (3, (True, True))]


@pytest.mark.parametrize("value", ["0.00000000000000000001", "10000000000000000000000000000000000000000000000000000000000000.00000000000000001"])
def test_recon_fixed_decimal_declaration_survives_repeated_compilation(value):
    config = {"schema_version": 2, "key_columns": ["id"], "comparison_rules": [
        {"type": "numeric_tolerance", "source_column": "amount", "target_column": "amount",
         "parameters": {"abs": value, "percent": value}},
        {"type": "date_tolerance", "source_column": "date", "target_column": "date",
         "parameters": {"hours": value}},
    ]}
    first = effective_config("recon", config)
    assert effective_config("recon", first) == first
    assert first["comparison_rules"][0]["parameters"]["abs"] == value
    assert first["comparison_rules"][1]["parameters"]["hours"] == value


@pytest.fixture(scope="module")
def real_spark(tmp_path_factory):
    if os.getenv("TRACKVANCE_SPARK_TESTS") != "1":
        pytest.skip("NOT_RUN_OPT_IN: dedicated suite requires TRACKVANCE_SPARK_TESTS=1")
    assert runtime_status()["available"], "Dedicated Spark suite requires pinned PySpark and Java 17/21"
    settings = replace(SparkSettings.from_environment(), batch_rows=2, partitions=2)
    with SparkRuntime("parity", tmp_path_factory.mktemp("spark") / "application", settings) as runtime:
        yield PySparkProcessingEngine(runtime.session, settings), runtime


def dataset(tmp_path, frame, name):
    numbered = frame.with_columns(pl.Series(RECORD_NUMBER_COLUMN, range(2, frame.height + 2), dtype=pl.Int64))
    paths = []
    for index, part in enumerate(numbered.iter_slices(2)):
        path = tmp_path / f"{name}-{index}.parquet"
        part.write_parquet(path)
        paths.append(str(path))
    if not paths:
        path = tmp_path / f"{name}-empty.parquet"
        numbered.write_parquet(path)
        paths.append(str(path))
    bound = sum(32 + (frame[column].cast(pl.String).str.len_bytes().max() or 0) for column in frame.columns)
    return SparkDataset(tuple(paths), tuple(frame.columns), frame.height, "PHYSICAL_LINE", RECORD_NUMBER_COLUMN,
                        observed_record_bound=bound or 32)


def result_rows(paths):
    return sorted((json.loads(payload) for path in paths for payload in pl.read_parquet(path)["payload"]),
                  key=lambda row: json.dumps(row, sort_keys=True, ensure_ascii=False))


def logical_rows(rows):
    return sorted(rows, key=lambda row: json.dumps(row, sort_keys=True, ensure_ascii=False))


def test_real_spark_catalog_intake_global_references_and_exact_values(real_spark, tmp_path):
    engine, runtime = real_spark
    frame = pl.DataFrame({
        "id": ["001", "002", "001", "004", "005", "006"],
        "code": ["CO", "CO", "CO", "US", "CO", "CO"],
        "amount": ["9999999999999999999999999999999999999999999999.0000000000000000001", "-0", "2", None, "bad", "1.2"],
        "other": ["9999999999999999999999999999999999999999999999.0000000000000000002", "0", "2", None, "1", "1.20"],
        "label": [" e\u0301 ", "😀", "x", "", None, "A_1"],
        "date": ["2026-10-01", "2026-02-30", "2027-01-01", None, "0000-01-01", "2026-10-03"],
        "timestamp": ["2026-10-01T23:20:00.123456-05:00", "2026-10-02T04:20:00.123456Z", "invalid", None, "2026-10-01T00:00:00.1234567Z", "2026-10-03T00:00:00Z"],
    }, schema_overrides={"amount": pl.String, "other": pl.String, "label": pl.String})
    reference = pl.DataFrame({"id": ["001", "002", "006"], "code": ["CO", "CO", "CO"]})
    definitions = [
        {"type": "required", "column": "label"}, {"type": "not_null", "column": "label"},
        {"type": "unique", "column": "id", "when": {"column": "code", "operator": "eq", "value": "CO"}},
        {"type": "compound_unique", "parameters": {"columns": ["id", "code"]}},
        {"type": "numeric", "column": "amount", "parameters": {"null_policy": "IGNORE"}},
        {"type": "positive", "column": "amount"},
        {"type": "range", "column": "amount", "parameters": {"gte": "0", "lte": "9999999999999999999999999999999999999999999999.00000000000000000015"}},
        {"type": "allowed_values", "column": "code", "parameters": {"values": ["CO"]}},
        {"type": "regex", "column": "label", "parameters": {"pattern": "^[A-Z_]\\d$"}},
        {"type": "length", "column": "label", "parameters": {"min": 1, "max": 3}},
        {"type": "column_compare", "column": "amount", "parameters": {"other_column": "other", "operator": "lte", "logical_type": "DECIMAL"}},
        {"type": "date_rule", "column": "date", "parameters": {"not_future": True}},
        {"type": "type", "column": "timestamp", "parameters": {"logical_type": "TIMESTAMP"}},
        {"type": "reference", "parameters": {"columns": ["id", "code"], "reference_columns": ["id", "code"], "dataset_version_id": "catalog", "null_policy": "FAIL"}},
    ]
    config = {"rules": definitions, "transforms": [{"type": "trim", "column": "label"},
              {"type": "unicode_normalization", "column": "label", "parameters": {"form": "NFC"}}]}
    expected, metrics, accepted = intake(frame, config, NOW, list(range(2, 8)), {"catalog": reference})
    paths, accepted_paths, actual, decision = engine.intake(dataset(tmp_path, frame, "input"), config,
                                                          NOW, {"catalog": dataset(tmp_path, reference, "reference")}, tmp_path / "output")
    assert result_rows(paths) == logical_rows(expected)
    assert actual == metrics and decision == metrics["decision"]
    produced = pl.concat([pl.read_parquet(path).drop(RECORD_NUMBER_COLUMN) for path in accepted_paths])
    assert produced.sort("id").to_dicts() == accepted.sort("id").to_dicts()
    assert runtime.metadata["engine_version"] == "4.0.3"


@pytest.mark.parametrize("aggregation_side", [None, "SOURCE", "TARGET"])
def test_real_spark_recon_partitions_decimals_nulls_and_aggregation(real_spark, tmp_path, aggregation_side):
    engine, _ = real_spark
    frame = pl.DataFrame({"id": ["a", "a", "b", "c", "d", "only"], "amount": ["0.00000000000000000001", "0.00000000000000000002", "3", None, "invalid", "1"], "label": ["é", "é", " 😀 ", "", None, "x"]})
    target = pl.DataFrame({"id": ["a", "b", "c", "d", "target"], "amount": ["0.00000000000000000003", "3", None, "1", "1"], "label": ["é", "😀", "", None, "y"]})
    if aggregation_side == "TARGET":
        frame, target = target, frame
    comparison = {"type": "numeric_tolerance", "source_column": "sum_amount" if aggregation_side == "SOURCE" else "amount",
                  "target_column": "sum_amount" if aggregation_side == "TARGET" else "amount",
                  "parameters": {"abs": "0", "percent": "0.0001", "null_policy": "INVALID"}}
    config = {"key_columns": ["id"], "comparison_rules": [comparison],
              "source_transforms": [{"type": "trim", "column": "label"}],
              "target_transforms": [{"type": "trim", "column": "label"}]}
    if aggregation_side:
        config["aggregations"] = [{"side": aggregation_side, "operation": "sum", "column": "amount", "output_column": "sum_amount"}]
    expected, metrics = reconcile(frame, target, config, list(range(2, frame.height + 2)), list(range(2, target.height + 2)))
    paths, _, actual, _ = engine.recon(dataset(tmp_path, frame, "source"), dataset(tmp_path, target, "target"), config, tmp_path / "recon")
    assert result_rows(paths) == logical_rows(expected)
    assert actual == metrics


def test_real_spark_sentinel_rules_and_profile_metrics(real_spark, tmp_path):
    engine, _ = real_spark
    frame = pl.DataFrame({"id": ["001", "002", "001", "004"], "amount": ["1", None, "3", "4"]})
    schema, profile, _ = profile_frame(frame)
    config = {"rules": [{"type": "unique", "column": "id"},
                        {"type": "not_null", "column": "amount"},
                        {"type": "distinct_count", "column": "id", "parameters": {"min": "2", "max": "4"}},
                        {"type": "historical_band", "column": "id", "parameters": {"metric": "distinct_count", "min": "1", "max": "4"}}]}
    expected, metrics = sentinel(profile, schema, config, NOW, None, NOW, frame)
    paths, _, actual, decision = engine.sentinel(dataset(tmp_path, frame, "sentinel"), profile, schema,
                                                config, NOW, None, NOW, {}, tmp_path / "sentinel-output")
    assert result_rows(paths) == logical_rows(expected)
    assert actual == metrics and decision == "ALERT"


@pytest.mark.parametrize("policy", ["ALLOW", "FAIL", "IGNORE"])
def test_real_spark_global_nulls_conditions_and_collision_free_compound_keys(real_spark, policy):
    engine, _ = real_spark
    engine = PySparkProcessingEngine(engine.session, replace(engine.settings, batch_rows=16))
    frame = pl.DataFrame({"a": ["A", "A", "A|B", "A", None, None],
                          "b": ["B", "B", "C", "B|C", "B", "B"],
                          "enabled": ["yes", "no", "yes", "yes", "yes", "yes"]})
    references = {"catalog": pl.DataFrame({"x": ["A", "A|B", None], "y": ["B", "C", "B"]})}
    for declaration in (
        {"type": "unique", "column": "a"},
        {"type": "compound_unique", "parameters": {"columns": ["a", "b"]}},
        {"type": "reference", "parameters": {"columns": ["a", "b"],
          "reference_columns": ["x", "y"], "dataset_version_id": "catalog"}},
    ):
        declaration.setdefault("parameters", {})["null_policy"] = policy
        declaration["when"] = {"column": "enabled", "operator": "eq", "value": "yes"}
        expression = compile_rule(declaration, NOW)
        expected = PolarsCompiler().evaluate(frame, expression, references)
        actual = engine.evaluate(frame, expression, references)
        assert actual == expected


def test_real_spark_all_transforms_preserve_original_evidence_and_numbering(real_spark, tmp_path):
    engine, _ = real_spark
    frame = pl.DataFrame({"id": [" 1-2 ", "03", None], "label": [" e\u0301 ", "", None],
                          "amount": ["1.234,50", "bad", None],
                          "date": ["03/10/2026", "2026-10-02", None]})
    transforms = [{"type": "trim", "column": "id"},
                  {"type": "remove_characters", "column": "id", "parameters": {"characters": "-"}},
                  {"type": "id_padding", "column": "id", "parameters": {"width": 5}},
                  {"type": "trim", "column": "label"},
                  {"type": "case", "column": "label", "parameters": {"case": "UPPER"}},
                  {"type": "unicode_normalization", "column": "label", "parameters": {"form": "NFC"}},
                  {"type": "empty_to_null", "column": "label"},
                  {"type": "decimal_parse", "column": "amount", "parameters": {"decimal_separator": ",", "thousands_separator": "."}},
                  {"type": "date_parse", "column": "date", "parameters": {"formats": ["%d/%m/%Y", "%Y-%m-%d"]}}]
    config = {"transforms": transforms, "rules": [{"type": "numeric", "column": "amount", "severity": "WARNING"},
                                                       {"type": "required", "column": "id", "severity": "WARNING"}]}
    expected, metrics, accepted = intake(frame, config, NOW, [2, 5, 6])
    source = dataset(tmp_path, frame, "transforms")
    # Represent a multiline CSV's physical starts across separate Parquet parts.
    for path, numbers in zip(source.paths, ([2, 5], [6]), strict=True):
        part = pl.read_parquet(path).with_columns(pl.Series(RECORD_NUMBER_COLUMN, numbers, dtype=pl.Int64))
        part.write_parquet(path)
    results, accepted_paths, actual, _ = engine.intake(source, config, NOW, {}, tmp_path / "transformed")
    assert result_rows(results) == logical_rows(expected) and actual == metrics
    produced = pl.concat([pl.read_parquet(path) for path in accepted_paths]).sort(RECORD_NUMBER_COLUMN)
    assert produced[RECORD_NUMBER_COLUMN].to_list() == [2, 5, 6]
    assert produced.drop(RECORD_NUMBER_COLUMN).to_dicts() == accepted.to_dicts()


@pytest.mark.parametrize("empty", [False, True])
def test_real_spark_recon_all_comparisons_normalized_compound_keys_and_empty(real_spark, tmp_path, empty):
    engine, _ = real_spark
    source = pl.DataFrame({"id": [" 001 ", "A|B", "A", None], "country": [" co ", "C", "B|C", "CO"],
                           "label": [" e\u0301 ", "é", None, "x"], "amount": ["0", "-2", None, "1"],
                           "timestamp": ["2026-10-03T00:00:00-05:00", "2026-10-03T00:00:00Z", None, "bad"]})
    target = pl.DataFrame({"id": ["001", "A|B", "A", "only"], "country": ["CO", "C", "B|C", "CO"],
                           "label": ["É", "E\u0301", None, "x"], "amount": ["0", "-2.00000000000000000001", None, "1"],
                           "timestamp": ["2026-10-03T05:00:00Z", "2026-10-03T01:00:00Z", None, "bad"]})
    if empty:
        source, target = source.head(0), target.head(0)
    config = {"key_columns": ["id", "country"],
              "key_normalization": {"trim": True, "case": "UPPER", "unicode_normalization": "NFC"},
              "comparison_rules": [
                  {"type": "exact_compare", "source_column": "label", "target_column": "label", "parameters": {"normalization": {"trim": True, "case": "UPPER", "unicode_normalization": "NFC"}, "null_policy": "MATCH_NULLS"}},
                  {"type": "numeric_tolerance", "source_column": "amount", "target_column": "amount", "parameters": {"abs": "0.00000000000000000001", "percent": "0.5", "denominator": "MAX_ABS", "null_policy": "MATCH_NULLS"}},
                  {"type": "date_tolerance", "source_column": "timestamp", "target_column": "timestamp", "parameters": {"hours": "1", "null_policy": "MATCH_NULLS"}},
                  {"type": "exact_compare", "source_column": "label", "target_column": "label", "parameters": {"normalization": {"trim": True, "case": "UPPER", "unicode_normalization": "NFC"}, "null_policy": "MATCH_NULLS"}},
              ]}
    expected, metrics = reconcile(source, target, config, list(range(2, source.height + 2)), list(range(2, target.height + 2)))
    paths, _, actual, _ = engine.recon(dataset(tmp_path, source, "comparison-source"), dataset(tmp_path, target, "comparison-target"), config, tmp_path / "comparisons")
    assert result_rows(paths) == logical_rows(expected) and actual == metrics


def test_real_spark_job_group_cancellation_stops_computation(real_spark):
    engine, runtime = real_spark
    context = engine.session.sparkContext

    def slow_partition(records):
        for record in records:
            time.sleep(0.03)
            yield record

    timer = threading.Timer(1, runtime.cancel)
    started = time.monotonic()
    timer.start()
    try:
        with pytest.raises(Exception, match="cancelled|canceled"):
            context.parallelize(range(1000), 2).mapPartitions(slow_partition).count()
    finally:
        timer.cancel()
        timer.join(timeout=2)
    assert time.monotonic() - started < 8
    # A cancelled job cannot prevent a separate subsequent action.
    assert context.parallelize([1, 2], 2).sum() == 3


def test_real_spark_global_hot_key_uniqueness_and_reference(real_spark, tmp_path):
    engine, _ = real_spark
    engine = PySparkProcessingEngine(engine.session, replace(engine.settings, batch_rows=1024))
    rows = 20000
    path = tmp_path / "hot-key.parquet"
    pl.DataFrame({"id": ["hot"] * rows, RECORD_NUMBER_COLUMN: range(2, rows + 2)}).write_parquet(path)
    source = SparkDataset((str(path),), ("id",), rows, record_number_column=RECORD_NUMBER_COLUMN,
                          observed_record_bound=35)
    reference = dataset(tmp_path, pl.DataFrame({"code": ["hot"]}), "lookup")
    config = {"rules": [{"type": "unique", "column": "id", "severity": "WARNING"},
                        {"type": "reference", "column": "id", "parameters": {
                            "dataset_version_id": "hot-catalog", "reference_columns": ["code"]}}]}
    paths, accepted, metrics, decision = engine.intake(source, config, NOW,
                                                      {"hot-catalog": reference}, tmp_path / "hot-output")
    assert metrics["rules"][0]["failed_count"] == rows
    assert metrics["rules"][1]["failed_count"] == 0
    assert metrics["valid_rows"] == rows and decision == "APPROVED_WITH_WARNINGS"
    import pyarrow.parquet as pq

    assert sum(pq.ParquetFile(path).metadata.num_rows for path in paths) == rows
    assert sum(pq.ParquetFile(path).metadata.num_rows for path in accepted) == rows


@pytest.mark.parametrize("side", ["SOURCE", "TARGET"])
def test_real_spark_recon_count_aggregation_preserves_lineage(real_spark, tmp_path, side):
    engine, _ = real_spark
    source = pl.DataFrame({"id": ["a", "a", "b"], "amount": ["bad", None, "3"]})
    target = pl.DataFrame({"id": ["a", "b"], "amount": ["2", "1"]})
    if side == "TARGET":
        source, target = target, source
    config = {"key_columns": ["id"], "aggregations": [
        {"side": side, "operation": "count", "output_column": "records"}],
        "comparison_rules": [{"type": "numeric_tolerance", "source_column": "records" if side == "SOURCE" else "amount",
                              "target_column": "records" if side == "TARGET" else "amount", "parameters": {"abs": "0"}}]}
    expected, metrics = reconcile(source, target, config, list(range(2, source.height + 2)),
                                  list(range(2, target.height + 2)))
    paths, _, actual, _ = engine.recon(dataset(tmp_path, source, "count-source"),
                                      dataset(tmp_path, target, "count-target"), config, tmp_path / "count-results")
    assert result_rows(paths) == logical_rows(expected) and actual == metrics
