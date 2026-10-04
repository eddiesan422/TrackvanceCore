"""Spark execution over immutable Parquet parts, with bounded portable kernels.

Spark owns partitioning, global joins, uniqueness and result materialization.
The existing portable compiler evaluates scalar rules in bounded executor batches;
this preserves the exact Decimal/Unicode/temporal contract without Spark's
DECIMAL(38) coercions. No business population is returned to the driver.
"""

from __future__ import annotations

import csv
import importlib.util
import itertools
import json
import os
import re
import shutil
import subprocess
import sys
import zipfile
from collections.abc import Callable, Iterator
from dataclasses import asdict, dataclass
from datetime import datetime
from functools import lru_cache
from pathlib import Path
from typing import Any

import polars as pl

from .config_semantics import METRIC_TYPES, RuleDefinition, effective_config, rule_columns
from .portable_engine import PolarsCompiler, RuleEvaluation, compile_rule, condition_matches
from .processing import (
    ProcessingError,
    apply_row_transforms,
    configured_rules,
    normalized_value,
    reconcile,
    rule_failure_message,
    sentinel,
)

SPARK_VERSION = "4.0.3"
RECORD_NUMBER_COLUMN = "__tv_record_number"
RESULT_SORT_COLUMN = "__tv_sort_key"


@dataclass(frozen=True)
class SparkSettings:
    master: str = "local[2]"
    driver_memory_mb: int = 768
    executor_memory_mb: int = 768
    executor_cores: int = 1
    total_cores: int = 2
    partitions: int = 4
    batch_rows: int = 2048
    batch_bytes: int = 8 * 1024 * 1024
    max_record_bytes: int = 64 * 1024
    max_group_rows: int = 10000
    max_group_bytes: int = 16 * 1024 * 1024
    max_result_bytes: int = 16 * 1024 * 1024

    def __post_init__(self) -> None:
        if not (re.fullmatch(r"local\[[1-9][0-9]*\]", self.master)
                or re.fullmatch(r"spark://[A-Za-z0-9_.-]+:[0-9]{1,5}", self.master)):
            raise ValueError("TRACKVANCE_SPARK_MASTER debe ser local[K] o spark://host:puerto.")
        limits = {
            "driver_memory_mb": (512, 16384), "executor_memory_mb": (512, 16384),
            "executor_cores": (1, 16), "total_cores": (1, 64),
            "partitions": (1, 256), "batch_rows": (1, 10000),
            "batch_bytes": (65536, 67108864), "max_record_bytes": (1024, 1048576),
            "max_group_rows": (1, 100000), "max_result_bytes": (1048576, 67108864),
            "max_group_bytes": (65536, 67108864),
        }
        for field, (minimum, maximum) in limits.items():
            value = getattr(self, field)
            if isinstance(value, bool) or not minimum <= value <= maximum:
                raise ValueError(f"Parámetro Spark {field} fuera del intervalo admitido.")
        if self.master.startswith("local["):
            cores = int(self.master[6:-1])
            if cores > self.total_cores:
                raise ValueError("local[K] supera TRACKVANCE_SPARK_TOTAL_CORES.")

    @classmethod
    def from_environment(cls) -> SparkSettings:
        defaults = cls()
        values = {field: int(os.getenv("TRACKVANCE_SPARK_" + field.upper(), str(value)))
                  for field, value in asdict(defaults).items() if field != "master"}
        return cls(master=os.getenv("TRACKVANCE_SPARK_MASTER", defaults.master), **values)

    @property
    def deployment_mode(self) -> str:
        return "LOCAL" if self.master.startswith("local[") else "STANDALONE_CLIENT"


@lru_cache(maxsize=8)
def _java_version(java: str) -> tuple[str | None, bool]:
    try:
        result = subprocess.run([java, "-version"], capture_output=True, text=True, timeout=5, check=False)
        version = re.search(r'version "([0-9]+(?:\.[0-9]+)*)', result.stderr + result.stdout)
        return (version[1], int(version[1].split(".")[0]) in {17, 21}) if version else (None, False)
    except (OSError, subprocess.TimeoutExpired):
        return None, False


def runtime_status() -> dict:
    installed = importlib.util.find_spec("pyspark") is not None
    java_home = os.getenv("JAVA_HOME")
    java_candidate = Path(java_home) / "bin" / ("java.exe" if os.name == "nt" else "java") if java_home else None
    java = str(java_candidate) if java_candidate and java_candidate.is_file() else shutil.which("java")
    java_version, compatible = _java_version(java) if java else (None, False)
    version = None
    if installed:
        from importlib.metadata import version as package_version
        version = package_version("pyspark")
    settings = SparkSettings.from_environment()
    return {"available": installed and compatible and version == SPARK_VERSION,
            "version": version, "java_available": bool(java), "java_version": java_version,
            "java_compatible": compatible, "required_version": SPARK_VERSION,
            "deployment_mode": settings.deployment_mode, "master": settings.master,
            "parameters": asdict(settings),
            "memory_budget_bytes": int(os.getenv("TRACKVANCE_SPARK_MEMORY_BUDGET_BYTES", str(2 * 1024 ** 3)))}


@dataclass(frozen=True)
class SparkDataset:
    paths: tuple[str, ...]
    columns: tuple[str, ...]
    row_count: int
    row_numbering: str = "RECORD_NUMBER"
    record_number_column: str | None = None
    original_delimited_path: str | None = None
    delimiter: str | None = None
    observed_record_bound: int | None = None


@dataclass(frozen=True)
class SparkProcessingResult:
    result_paths: tuple[Path, ...]
    accepted_paths: tuple[Path, ...]
    metrics: dict
    decision: str
    runtime: dict


def _row_bytes(row: dict) -> int:
    return sum(32 + (len(value.encode("utf-8")) if isinstance(value, str) else 0) for value in row.values())


def _batches(records: Iterator, size: int, max_bytes: int = 8 * 1024 * 1024) -> Iterator[list]:
    batch: list[Any] = []
    observed = 0
    for item in records:
        value = item[1]
        row = value[1] if isinstance(value, tuple) else value
        width = _row_bytes(row)
        if batch and (len(batch) >= size or observed + width > max_bytes):
            yield batch
            batch, observed = [], 0
        batch.append(item)
        observed += width
    if batch:
        yield batch


def _physical_lines(path: str, delimiter: str | None) -> Iterator[int]:
    with Path(path).open(encoding="utf-8-sig", newline="") as source:
        if not delimiter:
            sample = source.read(8192)
            header = sample.splitlines()[0] if sample.splitlines() else ""
            delimiter = ";" if header.count(";") > header.count(",") else ","
            source.seek(0)
        records = csv.reader(source, delimiter=delimiter)
        next(records, None)
        previous = records.line_num
        for _ in records:
            yield previous + 1
            previous = records.line_num


def _read_parts(parts: Iterator[tuple[str, int]], dataset: SparkDataset, batch_rows: int,
                batch_bytes: int = 8 * 1024 * 1024, max_record_bytes: int = 65536):
    import pyarrow.parquet as pq  # type: ignore[import-untyped]

    for path, offset in parts:
        numbers = (
            itertools.islice(_physical_lines(dataset.original_delimited_path, dataset.delimiter),
                             offset, None)
            if dataset.original_delimited_path else None
        )
        requested = [*dataset.columns]
        if dataset.record_number_column:
            requested.append(dataset.record_number_column)
        index = offset
        # A known global width bounds Arrow allocation before Python conversion.
        # Legacy/unknown widths use a single record, rather than an unsafe average.
        read_rows = max(1, min(batch_rows, batch_bytes // dataset.observed_record_bound)) if dataset.observed_record_bound else 1
        for batch in pq.ParquetFile(path).iter_batches(batch_size=read_rows, columns=requested, use_threads=False):
            for row in batch.to_pylist():  # Explicit bounded Arrow batch, never a dataset.
                number = (int(row.pop(dataset.record_number_column))
                          if dataset.record_number_column else next(numbers)
                          if numbers is not None else index + 1)
                if _row_bytes(row) > max_record_bytes:
                    raise ProcessingError("RESOURCE_SPARK_RECORD_LIMIT: El registro excede el presupuesto de bytes.")
                yield number, row
                index += 1


def _transform_partition(records, columns, transforms, batch_rows, max_record_bytes=65536):
    for number, original in records:
        row = apply_row_transforms(original, transforms)
        if _row_bytes(row) > max_record_bytes:
            raise ProcessingError("RESOURCE_SPARK_RECORD_LIMIT: La transformación excede el presupuesto de bytes.")
        yield number, (original, row)


def _scalar_evaluation(records, columns, rule, observed_at, batch_rows, batch_bytes=8 * 1024 * 1024):
    expression = compile_rule(rule, observed_at)
    for batch in _batches(records, batch_rows, batch_bytes):
        frame = pl.DataFrame([row[1] for _, row in batch], schema={c: pl.String for c in columns})
        evaluation = PolarsCompiler().evaluate(frame, expression)
        for (number, _), passed, evaluated in zip(
            batch, evaluation.passed, evaluation.evaluated, strict=True
        ):
            yield number, (passed, evaluated)


def _global_rule_key(item, rule):
    number, (_, row) = item
    columns = rule.parameters.get("columns", [rule.column] if rule.column else [])
    key = tuple(row[c] for c in columns)
    evaluated = condition_matches(row, rule.when) and not (
        rule.parameters.get("null_policy") == "IGNORE" and any(v is None for v in key)
    )
    return key, (number, evaluated)


def _sum_counts(left, right):
    return tuple(a + b for a, b in zip(left, right, strict=True))


def _stream_global_matches(records):
    """Lookup precedes active rows; a hot key never builds a population list."""
    for _, group in itertools.groupby(records, key=lambda item: item[0][0]):
        matched = False
        for (_, tag), value in group:
            if tag == 0:
                matched = bool(value)
            else:
                yield value[0], (matched, True)


def _error_record(item, rule, ordinal=0):
    number, ((original, _), _) = item
    columns = rule.parameters.get("columns", [rule.column] if rule.column else [])
    if rule.type == "column_compare":
        columns = [*columns, rule.parameters["other_column"]]
    label = ", ".join(columns)
    return {"original_row_number": number, "rule_code": rule.code, "column": label,
            "columns": columns, "rule_id": rule.rule_id,
            "received_value": original[columns[0]] if len(columns) == 1
            else {c: original[c] for c in columns},
            "severity": rule.severity, "message": rule_failure_message(rule, label),
            "parameters": rule.parameters, "condition": rule.when,
            "classification": rule.severity, "__tv_rule_order": ordinal}


def _result_tuple(record, module):
    if module == "intake":
        sort = (f"{record['original_row_number']:020d}\0{record['rule_code']}\0{record['column']}\0"
                f"{record.get('__tv_rule_order', 0):06d}")
    elif module == "recon":
        sort = (f"{record['key']}\0{record['classification']}\0"
                f"{record.get('source_row') or 0:020d}\0{record.get('target_row') or 0:020d}")
    else:
        sort = f"{record.get('__tv_rule_order', 0):06d}"
    payload = {key: value for key, value in record.items() if key != "__tv_rule_order"}
    return record.get("classification", ""), json.dumps(payload, ensure_ascii=False), sort


def _recon_key(item, side, keys, normalization, transforms, max_record_bytes=65536):
    number, original = item
    row = apply_row_transforms(original, transforms)
    if _row_bytes(row) > max_record_bytes:
        raise ProcessingError("RESOURCE_SPARK_RECORD_LIMIT: La transformación excede el presupuesto de bytes.")
    key = tuple(normalized_value(row[c], normalization) for c in keys)
    # Null/empty keys are independent INVALID records, never a shared join key.
    return ((1, *key) if all(v is not None and v != "" for v in key)
            else (0, str(side), f"{number:020d}")), (side, number, original)


def _recon_batch(rows, source_columns, target_columns, config):
    sides = [[value for _, value in rows if value[0] == side] for side in (0, 1)]
    for side in sides:
        side.sort(key=lambda value: value[1])
    frames = [pl.DataFrame([value[2] for value in side], schema={c: pl.String for c in columns})
              for side, columns in zip(sides, (source_columns, target_columns), strict=True)]
    results, metrics = reconcile(
        frames[0], frames[1], config, source_row_numbers=[v[1] for v in sides[0]],
        target_row_numbers=[v[1] for v in sides[1]]
    )
    for record in results:
        yield "result", record
    yield "metrics", {"counts": metrics["counts"], "comparisons": metrics["comparisons"]}


def _recon_kernel_bytes(rows, config, comparison_count, declaration_bytes):
    counts, widths = [0, 0], [0, 0]
    for _, (side, _, record) in rows:
        counts[side] += 1
        widths[side] = max(widths[side], _row_bytes(record))
    aggregation = config.get("aggregation")
    if aggregation:
        side = 0 if aggregation["side"] == "SOURCE" else 1
        counts[side] = min(counts[side], 1)
    lineage = len(rows) * 64
    if counts == [1, 1]:
        # One paired output retains every comparison, including exact values.
        output = 1024 + lineage + declaration_bytes + comparison_count * (512 + sum(widths))
    else:
        # Unmatched/ambiguous rows do not retain comparison details.
        output = lineage + sum(counts[side] * (512 + 2 * widths[side]) for side in (0, 1))
    return output * 2  # Portable dictionaries and serialized output coexist.


def _recon_partition(records, source_columns, target_columns, config, max_group_rows, batch_rows=2048,
                      max_group_bytes=16 * 1024 * 1024, batch_bytes=8 * 1024 * 1024):
    buffered: list[Any] = []
    buffered_bytes = 0
    comparisons = config.get("comparison_rules") or ([{"amount_column": config["amount_column"]}]
                                                    if config.get("amount_column") else [])
    comparison_count = len(comparisons)
    declaration_bytes = len(json.dumps(comparisons, ensure_ascii=False, default=str).encode("utf-8"))
    for _, group in itertools.groupby(records, key=lambda item: item[0]):
        rows: list[Any] = []
        group_bytes = 0
        for item in group:
            group_bytes += _row_bytes(item[1][2])
            if len(rows) >= max_group_rows or group_bytes > max_group_bytes:
                raise ProcessingError("RESOURCE_RECON_GROUP_LIMIT: La clave excede el límite de evidencia por grupo.")
            rows.append(item)
        kernel_bytes = _recon_kernel_bytes(rows, config, comparison_count, declaration_bytes)
        if kernel_bytes > max_group_bytes:
            raise ProcessingError("RESOURCE_RECON_GROUP_LIMIT: La evidencia de comparaciones excede el presupuesto por clave.")
        working_bytes = group_bytes + kernel_bytes
        if buffered and (len(buffered) + len(rows) > batch_rows or buffered_bytes + working_bytes > batch_bytes):
            yield from _recon_batch(buffered, source_columns, target_columns, config)
            buffered.clear()
            buffered_bytes = 0
        buffered.extend(rows)
        buffered_bytes += working_bytes
    if buffered:
        yield from _recon_batch(buffered, source_columns, target_columns, config)


def _merge_recon_metrics(left, right):
    for key, value in right["counts"].items():
        left["counts"][key] = left["counts"].get(key, 0) + value
    for index, item in enumerate(right["comparisons"]):
        if index >= len(left["comparisons"]):
            left["comparisons"].append(dict(item))
        else:
            target = left["comparisons"][index]
            for key in ("evaluated_count", "failed_count", "invalid_count"):
                target[key] += item[key]
    return left


class PySparkProcessingEngine:
    """Distributed dataset adapter; portable scalar kernels remain batch bounded."""

    family = "PYSPARK"

    def __init__(self, session, settings: SparkSettings | None = None):
        self.session = session
        self.settings = settings or SparkSettings.from_environment()

    def evaluate(self, frame, expression, references=None):
        """Compatibility ProcessingEngine port, explicitly bounded to one batch.

        Full persisted runs use the distributed dataset methods below rather
        than rebuilding a population-sized Polars frame or masks on the driver.
        """
        if frame.height > self.settings.batch_rows or any(
            reference.height > self.settings.batch_rows for reference in (references or {}).values()
        ):
            raise ProcessingError("SPARK_COMPATIBILITY_BATCH_LIMIT")
        context = self.session.sparkContext
        records = context.parallelize(list(enumerate(frame.to_dicts())), self.settings.partitions).mapValues(lambda row: (row, row))
        reference_rdds = {key: context.parallelize(list(enumerate(value.to_dicts())), self.settings.partitions)
                          for key, value in (references or {}).items()}
        flags = list(self._evaluate(records, tuple(frame.columns), expression.definition,
                                    expression.observed_at, reference_rdds).sortByKey().values().toLocalIterator())
        return RuleEvaluation([value[0] for value in flags], [value[1] for value in flags])

    def validate(self, frame, expression, references=None):
        return self.evaluate(frame, expression, references).passed

    def compare(self, frame, expression):
        if frame.height > self.settings.batch_rows:
            raise ProcessingError("SPARK_COMPATIBILITY_BATCH_LIMIT")
        columns, size = tuple(frame.columns), self.settings.batch_rows

        def compare_batches(values):
            for batch in _batches(values, size):
                relation = pl.DataFrame([row for _, row in batch], schema={c: pl.String for c in columns})
                for (index, _), result in zip(batch, PolarsCompiler().compare(relation, expression), strict=True):
                    yield index, result

        return list(self.session.sparkContext.parallelize(list(enumerate(frame.to_dicts())), self.settings.partitions)
                    .mapPartitions(compare_batches).sortByKey().values().toLocalIterator())

    def measure(self, profile, schema, expression, previous_schema=None, history=None):
        # Immutable profile facts/history are bounded metadata, never the population.
        return PolarsCompiler().measure(profile, schema, expression, previous_schema, history)

    def _dataset(self, dataset: SparkDataset):
        import pyarrow.parquet as pq  # type: ignore[import-untyped]
        parts, offset = [], 0
        for path in dataset.paths:
            parts.append((path, offset))
            offset += pq.ParquetFile(path).metadata.num_rows
        if offset != dataset.row_count:
            raise ProcessingError("DATASET_ROW_COUNT_MISMATCH: Las partes no coinciden con la versión.")
        settings = self.settings
        return self.session.sparkContext.parallelize(parts, max(1, min(len(parts), settings.partitions))).mapPartitions(
            lambda values: _read_parts(values, dataset, settings.batch_rows, settings.batch_bytes, settings.max_record_bytes)
        ).repartition(settings.partitions)

    def _evaluate(self, records, columns, rule, observed_at, references):
        if rule.type not in {"unique", "compound_unique", "reference"}:
            size, batch_bytes = self.settings.batch_rows, self.settings.batch_bytes
            return records.mapPartitions(lambda values: _scalar_evaluation(values, columns, rule, observed_at, size, batch_bytes))
        keyed = records.map(lambda item: _global_rule_key(item, rule))
        active = keyed.filter(lambda item: item[1][1] and all(v is not None for v in item[0]))
        if rule.type == "reference":
            reference = references.get(rule.parameters["dataset_version_id"])
            if reference is None:
                raise ProcessingError("La referencia inmutable no está disponible.")
            reference_columns = rule.parameters["reference_columns"]
            lookup = reference.map(lambda item: tuple(item[1][c] for c in reference_columns)).filter(
                lambda key: all(value is not None for value in key)
            ).distinct().map(lambda key: (key, True))
        else:
            lookup = active.mapValues(lambda _: 1).reduceByKey(lambda a, b: a + b).mapValues(lambda count: count == 1)
        from pyspark.core.rdd import portable_hash

        # RDD.leftOuterJoin buffers every same-key value in Python. Sort lookup
        # first and merge lazily instead, even for a million identical values.
        tagged = lookup.map(lambda item: ((item[0], 0), item[1])).union(
            active.map(lambda item: ((item[0], 1), item[1])))
        matches = tagged.repartitionAndSortWithinPartitions(
            self.settings.partitions, partitionFunc=lambda key: portable_hash(key[0])
        ).mapPartitions(_stream_global_matches)
        ignored = keyed.filter(lambda item: not item[1][1] or any(v is None for v in item[0])).map(
            lambda item: (item[1][0], (not item[1][1] or rule.parameters.get("null_policy", "ALLOW") != "FAIL", item[1][1]))
        )
        return matches.union(ignored)

    def _write_results(self, records, directory: Path, module: str) -> tuple[Path, ...]:
        from pyspark.sql.types import StringType, StructField, StructType
        schema = StructType([StructField(name, StringType(), False)
                             for name in ("classification", "payload", RESULT_SORT_COLUMN)])
        self.session.createDataFrame(records.map(lambda row: _result_tuple(row, module)), schema).write.mode(
            "errorifexists"
        ).option("maxRecordsPerFile", self.settings.batch_rows).parquet(str(directory))
        paths = tuple(sorted(directory.glob("*.parquet")))
        if not paths:
            raise ProcessingError("SPARK_RESULT_NOT_MATERIALIZED")
        return paths

    def intake(self, source, config, observed_at, references, directory):
        from pyspark import StorageLevel
        effective = effective_config("intake", config)
        rules = configured_rules(effective)
        missing = {c for rule in rules for c in rule_columns(rule)} - set(source.columns)
        if missing:
            raise ProcessingError("Columnas del contrato ausentes: " + ", ".join(sorted(missing)))
        transforms, columns, size = effective["transforms"], source.columns, self.settings.batch_rows
        record_limit = self.settings.max_record_bytes
        records = self._dataset(source).mapPartitions(
            lambda values: _transform_partition(values, columns, transforms, size, record_limit)
        ).persist(StorageLevel.DISK_ONLY)
        references = {key: self._dataset(value) for key, value in references.items()}
        errors = self.session.sparkContext.emptyRDD()
        summaries = []
        for ordinal, rule in enumerate(rules):
            evaluation = self._evaluate(records, columns, rule, observed_at, references).persist(StorageLevel.DISK_ONLY)
            evaluated, failed = evaluation.values().map(lambda flags: (int(flags[1]), int(not flags[0]))).fold((0, 0), _sum_counts)
            label_columns = rule.parameters.get("columns", [rule.column] if rule.column else [])
            if rule.type == "column_compare":
                label_columns = [*label_columns, rule.parameters["other_column"]]
            summaries.append({"code": rule.code, "column": ", ".join(label_columns), "columns": label_columns,
                              "severity": rule.severity, "evaluated_count": evaluated,
                              "skipped_count": source.row_count - evaluated, "failed_count": failed,
                              "status": "FAIL" if failed else "PASS", "rule_id": rule.rule_id})
            if not failed:
                continue
            failures = evaluation.filter(lambda item: not item[1][0])
            # Freeze each rule in the closure: Spark actions may execute after this loop.
            if failed <= size:
                # Only a bounded set of integer identities is a driver hint.
                # No row/value population or unbounded masks leave executors.
                hint = self.session.sparkContext.broadcast(set(failures.keys().toLocalIterator()))
                errors = errors.union(records.filter(lambda item, ids=hint: item[0] in ids.value)
                    .map(lambda item, definition=rule, index=ordinal: _error_record((item[0], (item[1], None)), definition, index)))
            else:
                errors = errors.union(records.join(failures).map(lambda item, definition=rule, index=ordinal: _error_record(item, definition, index)))
        errors = errors.persist(StorageLevel.DISK_ONLY)
        bad = errors.filter(lambda row: row["severity"] == "ERROR").map(lambda row: (row["original_row_number"], True)).reduceByKey(lambda a, b: True)
        warnings = errors.filter(lambda row: row["severity"] == "WARNING").map(lambda row: row["original_row_number"]).distinct().count()
        failed = bad.count()
        total = source.row_count
        decision = ("REJECTED" if total and failed / total > effective.get("max_error_rate", 0)
                    else "APPROVED_WITH_WARNINGS" if failed or warnings else "APPROVED")
        metrics = {"total_rows": total, "valid_rows": total - failed, "error_rows": failed,
                   "warning_rows": warnings, "error_count": sum(s["failed_count"] for s in summaries if s["severity"] == "ERROR"),
                   "acceptance_rate": round(100 * (total - failed) / total, 2) if total else 100,
                   "rules": summaries, "decision": decision, "normalization_policy": "DECLARED_ONLY"}
        result_paths = self._write_results(errors, directory / "results", "intake")
        if failed <= size:
            bad_hint = self.session.sparkContext.broadcast(set(bad.keys().toLocalIterator()))
            valid = records.filter(lambda item: item[0] not in bad_hint.value)
        else:
            valid = records.subtractByKey(bad)
        accepted = valid.map(lambda item: tuple(item[1][1][c] for c in columns) + (item[0],))
        from pyspark.sql.types import LongType, StringType, StructField, StructType
        schema = StructType([*[StructField(c, StringType(), True) for c in columns],
                             StructField(RECORD_NUMBER_COLUMN, LongType(), False)])
        self.session.createDataFrame(accepted, schema).write.mode("errorifexists").option(
            "maxRecordsPerFile", size
        ).parquet(str(directory / "accepted"))
        return result_paths, tuple(sorted((directory / "accepted").glob("*.parquet"))), metrics, decision

    def recon(self, source, target, config, directory):
        from pyspark import StorageLevel
        effective = effective_config("recon", config)
        sides = []
        for side, dataset in enumerate((source, target)):
            keys, normalization = effective["key_columns"], effective["key_normalization"]
            transforms = effective["source_transforms" if side == 0 else "target_transforms"]
            sides.append(self._dataset(dataset).map(
                lambda item, index=side, declarations=transforms, fields=keys, policy=normalization, limit=self.settings.max_record_bytes: _recon_key(item, index, fields, policy, declarations, limit)
            ))
        max_group, batch_rows = self.settings.max_group_rows, self.settings.batch_rows
        group_bytes, batch_bytes = self.settings.max_group_bytes, self.settings.batch_bytes
        source_columns, target_columns = source.columns, target.columns
        processed = sides[0].union(sides[1]).repartitionAndSortWithinPartitions(self.settings.partitions).mapPartitions(
            lambda records: _recon_partition(records, source_columns, target_columns, effective, max_group, batch_rows, group_bytes, batch_bytes)
        ).persist(StorageLevel.DISK_ONLY)
        aggregated = processed.filter(lambda item: item[0] == "metrics").values().fold(
            {"counts": {}, "comparisons": []}, _merge_recon_metrics
        )
        classifications = ("MATCH", "VALUE_MISMATCH", "SOURCE_ONLY", "TARGET_ONLY", "DUPLICATE_SOURCE", "DUPLICATE_TARGET", "INVALID")
        counts = {key: aggregated["counts"].get(key, 0) for key in classifications}
        total = sum(counts.values())
        _, template = reconcile(pl.DataFrame(schema={c: pl.String for c in source.columns}),
                                pl.DataFrame(schema={c: pl.String for c in target.columns}), config)
        metrics = {**template, "total_rows": total, "source_rows": source.row_count,
                   "target_rows": target.row_count, "matched": counts["MATCH"],
                   "mismatched": counts["VALUE_MISMATCH"], "source_only": counts["SOURCE_ONLY"],
                   "target_only": counts["TARGET_ONLY"], "duplicate_source": counts["DUPLICATE_SOURCE"],
                   "duplicate_target": counts["DUPLICATE_TARGET"], "invalid": counts["INVALID"],
                   "match_rate": round(100 * counts["MATCH"] / total, 2) if total else 100,
                   "counts": counts, "comparisons": aggregated["comparisons"] or template["comparisons"]}
        paths = self._write_results(processed.filter(lambda item: item[0] == "result").values(), directory / "results", "recon")
        return paths, (), metrics, "CONFORME" if counts["MATCH"] == total else "WITH_FINDINGS"

    def sentinel(self, source, profile, schema, config, created_at, baseline, observed_at,
                 references, directory, previous_schema=None, history=None):
        from pyspark import StorageLevel
        records = self._dataset(source).mapValues(lambda row: (row, row)).persist(StorageLevel.DISK_ONLY)
        references = {key: self._dataset(value) for key, value in references.items()}
        counts: dict[int, dict[str, int] | None] = {}
        for index, definition in enumerate(config.get("rules", [])):
            rule = RuleDefinition.model_validate(definition)
            if not rule.enabled or rule.type in METRIC_TYPES:
                continue
            if set(rule_columns(rule)) - set(source.columns):
                counts[index] = None
                continue
            evaluation = self._evaluate(records, source.columns, rule, observed_at, references)
            evaluated, failed = evaluation.values().map(lambda flags: (int(flags[1]), int(not flags[0]))).fold((0, 0), _sum_counts)
            counts[index] = {"evaluated_count": evaluated, "failed_count": failed,
                             "skipped_count": source.row_count - evaluated}
        checks, metrics = sentinel(profile, schema, config, created_at, baseline, observed_at=observed_at,
                                   previous_schema=previous_schema, history=history,
                                   column_rule_counts=counts)
        numbered_checks = [{**check, "__tv_rule_order": index} for index, check in enumerate(checks)]
        paths = self._write_results(self.session.sparkContext.parallelize(numbered_checks, self.settings.partitions),
                                    directory / "results", "sentinel")
        return paths, (), metrics, "HEALTHY" if metrics["failed_checks"] == 0 else "ALERT"


class SparkRuntime:
    """One application per Run; cancelJobGroup is safe across unrelated applications."""

    def __init__(self, run_id: str, directory: Path, settings: SparkSettings | None = None):
        self.run_id, self.directory = run_id, directory
        self.settings = settings or SparkSettings.from_environment()
        self.session: Any = None

    def __enter__(self):
        if not runtime_status()["available"]:
            raise ProcessingError("ENGINE_UNAVAILABLE: PySpark 4.0.3 y Java 17/21 son necesarios.")
        from pyspark.sql import SparkSession
        settings = self.settings
        self.directory.mkdir(parents=True, exist_ok=False)
        builder = SparkSession.builder.master(settings.master).appName("Trackvance-" + self.run_id)
        effective = {"spark.submit.deployMode": "client", "spark.driver.memory": f"{settings.driver_memory_mb}m",
                     "spark.executor.memory": f"{settings.executor_memory_mb}m",
                     "spark.executor.cores": str(settings.executor_cores), "spark.cores.max": str(settings.total_cores),
                     "spark.sql.shuffle.partitions": str(settings.partitions), "spark.default.parallelism": str(settings.partitions),
                     "spark.driver.maxResultSize": str(settings.max_result_bytes), "spark.ui.enabled": "false",
                     "spark.ui.showConsoleProgress": "false",
                     "spark.sql.session.timeZone": "UTC", "spark.sql.ansi.enabled": "true",
                     "spark.local.dir": str(self.directory / "spill"), "spark.task.maxFailures": "2",
                     "spark.python.worker.memory": "128m", "spark.python.worker.reuse": "true",
                     "spark.executorEnv.POLARS_MAX_THREADS": "1", "spark.executorEnv.OMP_NUM_THREADS": "1",
                     "spark.pyspark.python": os.getenv("PYSPARK_PYTHON", sys.executable)}
        for name, variable in (("spark.driver.host", "TRACKVANCE_SPARK_DRIVER_HOST"),
                               ("spark.driver.bindAddress", "TRACKVANCE_SPARK_DRIVER_BIND_ADDRESS"),
                               ("spark.driver.port", "TRACKVANCE_SPARK_DRIVER_PORT"),
                               ("spark.blockManager.port", "TRACKVANCE_SPARK_BLOCK_MANAGER_PORT")):
            if value := os.getenv(variable):
                effective[name] = value
        for key, value in effective.items():
            builder = builder.config(key, value)
        self.session = builder.getOrCreate()
        self.session.sparkContext.setLogLevel("ERROR")
        self.session.sparkContext.setJobGroup(self.run_id, "Trackvance processing", interruptOnCancel=True)
        # Only package code is shipped; no .env, database, staging or secret files.
        package = Path(__file__).parent
        archive = self.directory / "trackvance-code.zip"
        with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED) as stream:
            for path in sorted(package.glob("*.py")):
                stream.write(path, "trackvance/" + path.name)
        self.session.sparkContext.addPyFile(str(archive))
        self.metadata = {"engine": "PYSPARK", "engine_version": self.session.version,
                         "java_version": runtime_status()["java_version"],
                         "deployment_mode": settings.deployment_mode, "master": settings.master,
                         "application_id": self.session.sparkContext.applicationId,
                         "effective_parameters": effective, "resource_budget": asdict(settings),
                         "scalar_kernel": "PORTABLE_POLARS_BATCH_V1"}
        return self

    def cancel(self):
        if self.session is not None:
            self.session.sparkContext.cancelJobGroup(self.run_id)

    def __exit__(self, *_):
        if self.session is not None:
            self.session.stop()


def process_dataset_run(module: str, source: SparkDataset, config: dict, directory: Path,
                        *, observed_at: datetime, target: SparkDataset | None = None,
                        references: dict[str, SparkDataset] | None = None,
                        sentinel_context: dict | None = None,
                        on_runtime: Callable[[SparkRuntime], None] | None = None,
                        settings: SparkSettings | None = None, run_id: str | None = None) -> SparkProcessingResult:
    with SparkRuntime(run_id or directory.name, directory, settings) as runtime:
        if on_runtime:
            on_runtime(runtime)
        engine = PySparkProcessingEngine(runtime.session, runtime.settings)
        if module == "intake":
            result = engine.intake(source, config, observed_at, references or {}, directory)
        elif module == "recon":
            if target is None:
                raise ProcessingError("ReconOps requiere la versión de destino.")
            result = engine.recon(source, target, config, directory)
        elif module == "sentinel":
            context = sentinel_context or {}
            result = engine.sentinel(source, config=config, observed_at=observed_at,
                                     references=references or {}, directory=directory, **context)
        else:
            raise ProcessingError("Módulo no soportado por PySpark.")
        paths, accepted, metrics, decision = result
        return SparkProcessingResult(result_paths=paths, accepted_paths=accepted, metrics=metrics,
                                     decision=decision, runtime=runtime.metadata)
