#!/usr/bin/env python3
"""Measure real Spark engines inside a disposable certification container.

This runner starts no Docker services and never contacts the installed project.
Run the same command with local[K] and an isolated Standalone master. Its output
records full materialization counts, bounded input generation, JVM identity,
elapsed time, process-tree RSS, cgroup memory and global correctness assertions.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import threading
import time
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path

import polars as pl
import pyarrow.parquet as pq
from trackvance.dataset_scans import profile_paths
from trackvance.processing import reconcile, sentinel
from trackvance.spark_engine import (
    RECORD_NUMBER_COLUMN,
    PySparkProcessingEngine,
    SparkDataset,
    SparkRuntime,
    SparkSettings,
)

NOW = datetime(2026, 10, 3, 12, tzinfo=UTC)
GENERATE_BATCH_ROWS = 8192


class LogicalMultiset:
    """Constant-state fingerprint of complete typed rows, independent of order.

    Count, sum and XOR of SHA256 row hashes retain duplicate multiplicity.
    The final SHA256 binds all three accumulators. This is cryptographic
    evidence, not a mathematical proof that hash collisions cannot exist.
    """

    def __init__(self):
        self.rows, self.total, self.xor = 0, 0, 0

    def update(self, record):
        logical = dict(record)
        if {"classification", "payload", "__tv_sort_key"} <= logical.keys() and isinstance(logical["payload"], str):
            logical["payload"] = json.loads(logical["payload"])
        encoded = json.dumps(logical, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        value = int.from_bytes(hashlib.sha256(encoded).digest(), "big")
        self.rows += 1
        self.total = (self.total + value) % (1 << 256)
        self.xor ^= value

    def evidence(self):
        encoded = self.rows.to_bytes(8, "big") + self.total.to_bytes(32, "big") + self.xor.to_bytes(32, "big")
        return {"method": "CANONICAL_JSON_ROW_SHA256_COUNT_SUM_XOR_V1", "rows": self.rows,
                "sum_sha256": f"{self.total:064x}", "xor_sha256": f"{self.xor:064x}",
                "sha256": hashlib.sha256(encoded).hexdigest()}


def fixture_record(index, side="source"):
    return {"id": f"{index - 1 if index and index % 50000 == 0 else index:012d}",
            "region": f"R{index % 20:02d}",
            "amount": "invalid" if index % 10000 == 0 else
                      "1.00000000000000000000000000000000000001" if side == "target" and index % 12345 == 0 else
                      "1.00000000000000000000000000000000000000",
            "label": None if index % 20000 == 0 else f"e\u0301-{index:012d}-😀",
            RECORD_NUMBER_COLUMN: index + 2}


def duplicate_index(index, rows):
    return bool(index and index % 50000 == 0 or index % 50000 == 49999 and index + 1 < rows)


def logical_result(record, module, ordinal=0):
    if module == "intake":
        order = f"{record['original_row_number']:020d}\0{record['rule_code']}\0{record['column']}\0{ordinal:06d}"
    elif module == "recon":
        order = (f"{record['key']}\0{record['classification']}\0"
                 f"{record.get('source_row') or 0:020d}\0{record.get('target_row') or 0:020d}")
    else:
        order = f"{ordinal:06d}"
    return {"classification": record.get("classification", ""), "payload": record, "__tv_sort_key": order}


def expected_intake_errors(rows):
    result = LogicalMultiset()
    cases = [(index, "NUMERIC", "amount", "invalid", "ERROR", 2) for index in range(0, rows, 10000)]
    cases += [(index, "NOT_NULL", "label", None, "ERROR", 3) for index in range(0, rows, 20000)]
    cases += [(index + offset, "UNIQUE", "id", f"{index - 1:012d}", "WARNING", 1)
              for index in range(50000, rows, 50000) for offset in (-1, 0)]
    for index, code, column, value, severity, ordinal in cases:
        record = {"original_row_number": index + 2, "rule_code": code, "column": column,
                  "columns": [column], "rule_id": None, "received_value": value, "severity": severity,
                  "message": f"{column}: incumple la regla {code}", "parameters": {},
                  "condition": None, "classification": severity}
        result.update(logical_result(record, "intake", ordinal))
    return result.evidence()


def expected_recon_results(rows):
    config = {"key_columns": ["id"], "comparison_rules": [{"type": "numeric_tolerance",
              "source_column": "amount", "target_column": "amount",
              "parameters": {"abs": "0", "percent": "0", "null_policy": "INVALID"}}]}
    templates = {}
    for index, classification in ((1, "MATCH"), (12345, "VALUE_MISMATCH"), (0, "INVALID")):
        source = {k: v for k, v in fixture_record(index).items() if k != RECORD_NUMBER_COLUMN}
        target = {k: v for k, v in fixture_record(index, "target").items() if k != RECORD_NUMBER_COLUMN}
        records, _ = reconcile(pl.DataFrame([source]), pl.DataFrame([target]), config)
        assert len(records) == 1 and records[0]["classification"] == classification
        templates[classification] = records[0]
    source = pl.DataFrame({"id": ["duplicate", "duplicate"], "amount": ["1", "invalid"]})
    records, _ = reconcile(source, source, config)
    for record in records:
        templates.setdefault(record["classification"], record)
    digest = LogicalMultiset()
    for index in range(rows):
        source, target = fixture_record(index), fixture_record(index, "target")
        number = index + 2
        if duplicate_index(index, rows):
            for side in ("SOURCE", "TARGET"):
                record = {**templates["DUPLICATE_" + side], "key": source["id"]}
                record.update(source_row=number if side == "SOURCE" else None,
                              target_row=number if side == "TARGET" else None,
                              source_rows=[number] if side == "SOURCE" else [],
                              target_rows=[number] if side == "TARGET" else [],
                              source_value=source["amount"] if side == "SOURCE" else None,
                              target_value=target["amount"] if side == "TARGET" else None)
                digest.update(logical_result(record, "recon"))
        else:
            classification = "INVALID" if index % 10000 == 0 else "VALUE_MISMATCH" if index % 12345 == 0 else "MATCH"
            record = {**templates[classification], "key": source["id"], "source_row": number,
                      "target_row": number, "source_rows": [number], "target_rows": [number]}
            digest.update(logical_result(record, "recon"))
    return digest.evidence()


def process_tree_rss() -> int:
    """Linux process metadata only; do not inspect commands or credentials."""
    processes = {}
    for path in Path("/proc").glob("[0-9]*/stat"):
        try:
            # comm may contain spaces; fields after its closing parenthesis start at state.
            values = path.read_text().rsplit(")", 1)[1].split()
            processes[int(path.parent.name)] = (int(values[1]), int(values[21]))
        except (OSError, ValueError, IndexError):
            continue
    children = {os.getpid()}
    while addition := {pid for pid, (parent, _) in processes.items() if parent in children} - children:
        children.update(addition)
    return sum(processes.get(pid, (0, 0))[1] for pid in children) * os.sysconf("SC_PAGE_SIZE")


class MemorySampler:
    def __init__(self):
        self.stop = threading.Event()
        self.peak_rss = 0
        self.peak_cgroup = 0
        self.samples = 0
        self.thread = threading.Thread(target=self._sample, daemon=True)

    def _sample(self):
        while not self.stop.is_set():
            self.peak_rss = max(self.peak_rss, process_tree_rss())
            try:
                self.peak_cgroup = max(self.peak_cgroup, int(Path("/sys/fs/cgroup/memory.current").read_text()))
            except (OSError, ValueError):
                pass
            self.samples += 1
            self.stop.wait(0.25)

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *_):
        self.stop.set()
        self.thread.join(timeout=2)

    def evidence(self):
        return {"process_tree_peak_rss_bytes": self.peak_rss,
                "driver_container_sampled_peak_bytes": self.peak_cgroup,
                "memory_samples": self.samples,
                "measurement_method": "PROC_RSS_TREE_AND_CGROUP_V2_250MS",
                "standalone_executors_measured_separately": True}


def generate(root: Path, rows: int):
    paths = {side: [] for side in ("source", "target")}
    expected_accepted = LogicalMultiset()
    for start in range(0, rows, GENERATE_BATCH_ROWS):
        indices = range(start, min(rows, start + GENERATE_BATCH_ROWS))
        numbers = list(indices)
        for side, parts in paths.items():
            frame = pl.DataFrame({
                "id": [f"{i - 1 if i and i % 50000 == 0 else i:012d}" for i in numbers],
                "region": [f"R{i % 20:02d}" for i in numbers],
                "amount": ["invalid" if i % 10000 == 0 else
                           "1.00000000000000000000000000000000000001" if side == "target" and i % 12345 == 0
                           else "1.00000000000000000000000000000000000000" for i in numbers],
                "label": [None if i % 20000 == 0 else f"e\u0301-{i:012d}-😀" for i in numbers],
                RECORD_NUMBER_COLUMN: [i + 2 for i in numbers],
            })
            if side == "source":
                for index, record in zip(numbers, frame.to_dicts(), strict=True):
                    if index % 10000 != 0:
                        expected_accepted.update(record)
            path = root / f"{side}-{start:012d}.parquet"
            frame.write_parquet(path)
            parts.append(str(path))
    columns = ("id", "region", "amount", "label")
    return {side: SparkDataset(tuple(parts), columns, rows, "PHYSICAL_LINE", RECORD_NUMBER_COLUMN,
                              observed_record_bound=512)
            for side, parts in paths.items()}, expected_accepted.evidence()


def summarize(paths):
    counts, digest, rows, size = Counter(), hashlib.sha256(), 0, 0
    logical = LogicalMultiset()
    for path in paths:
        size += path.stat().st_size
        for batch in pq.ParquetFile(path).iter_batches(batch_size=2048):
            for record in batch.to_pylist():
                rows += 1
                if "classification" in record:
                    counts[record["classification"]] += 1
                digest.update(json.dumps(record, sort_keys=True, ensure_ascii=False).encode("utf-8"))
                digest.update(b"\n")
                logical.update(record)
    return {"rows": rows, "classification_counts": dict(counts), "size_bytes": size,
            "parts": len(paths), "materialization_sha256": digest.hexdigest(),
            "logical_fingerprint": logical.evidence()}


def cycle(root: Path, rows: int):
    root.mkdir(parents=True, exist_ok=False)
    inputs, accepted_fingerprint = generate(root, rows)
    expected_results = {"intake": expected_intake_errors(rows), "recon": expected_recon_results(rows)}
    bad = len(range(0, rows, 10000))
    nulls = len(range(0, rows, 20000))
    duplicates = len(range(50000, rows, 50000))
    duplicate_rows = duplicates * 2
    reference = root / "reference.parquet"
    pl.DataFrame({"region": [f"R{i:02d}" for i in range(20)],
                  RECORD_NUMBER_COLUMN: list(range(1, 21))}).write_parquet(reference)
    catalog = SparkDataset((str(reference),), ("region",), 20, record_number_column=RECORD_NUMBER_COLUMN,
                           observed_record_bound=64)
    settings = SparkSettings.from_environment()
    outcomes = {}
    profile_started = time.monotonic()
    with MemorySampler() as profile_memory:
        schema, profile, _ = profile_paths([Path(path) for path in inputs["source"].paths])
    profile_evidence = {"elapsed_seconds": round(time.monotonic() - profile_started, 3), **profile_memory.evidence()}
    for module in ("intake", "recon", "sentinel"):
        started = time.monotonic()
        with MemorySampler() as memory, SparkRuntime("million-" + module, root / module, settings) as runtime:
            engine = PySparkProcessingEngine(runtime.session, settings)
            if module == "intake":
                config = {"rules": [{"type": "required", "column": "id"},
                          {"type": "unique", "column": "id", "severity": "WARNING"},
                          {"type": "numeric", "column": "amount"},
                          {"type": "not_null", "column": "label"},
                          {"type": "reference", "parameters": {"columns": ["region"],
                           "reference_columns": ["region"], "dataset_version_id": "catalog"}}]}
                result, accepted, metrics, decision = engine.intake(inputs["source"], config, NOW,
                                                                   {"catalog": catalog}, root / module)
                assert metrics["total_rows"] == rows and metrics["error_rows"] == bad
                assert metrics["warning_rows"] == duplicate_rows
                assert metrics["error_count"] == bad + nulls
                accepted_evidence = summarize(accepted)
                assert accepted_evidence["rows"] == rows - bad
                assert accepted_evidence["logical_fingerprint"] == accepted_fingerprint
            elif module == "recon":
                result, _, metrics, decision = engine.recon(inputs["source"], inputs["target"],
                    {"key_columns": ["id"], "comparison_rules": [{"type": "numeric_tolerance",
                    "source_column": "amount", "target_column": "amount",
                    "parameters": {"abs": "0", "percent": "0", "null_policy": "INVALID"}}]}, root / module)
                invalid = bad - duplicates
                mismatch = sum(i % 10000 != 0 and not (i % 50000 in (0, 49999))
                               for i in range(12345, rows, 12345))
                assert metrics["source_rows"] == rows and metrics["target_rows"] == rows
                assert metrics["duplicate_source"] == duplicate_rows
                assert metrics["duplicate_target"] == duplicate_rows
                assert metrics["invalid"] == invalid and metrics["mismatched"] == mismatch
                assert metrics["matched"] == rows - duplicate_rows - invalid - mismatch
                accepted_evidence = None
            else:
                config = {"rules": [{"type": "unique", "column": "id"},
                                    {"type": "numeric", "column": "amount"},
                                    {"type": "not_null", "column": "label"}]}
                result, _, metrics, decision = engine.sentinel(inputs["source"], profile, schema,
                    config, NOW, None, NOW, {}, root / module)
                actual = {check["code"]: check.get("failed_count") for check in metrics["checks"]}
                assert actual["UNIQUE"] == duplicate_rows and actual["NUMERIC"] == bad
                assert actual["NOT_NULL"] == nulls
                accepted_evidence = None
                checks, _ = sentinel(profile, schema, config, NOW, None, observed_at=NOW,
                    column_rule_counts={index: {"evaluated_count": rows, "skipped_count": 0, "failed_count": count}
                                        for index, count in enumerate((duplicate_rows, bad, nulls))})
                expected = LogicalMultiset()
                for index, check in enumerate(checks):
                    expected.update(logical_result(check, "sentinel", index))
                expected_results[module] = expected.evidence()
            evidence = summarize(result)
            assert evidence["logical_fingerprint"] == expected_results[module], "Every result value/position must match the fixture oracle"
            if module == "recon":
                assert evidence["classification_counts"] == {key: value for key, value in metrics["counts"].items() if value}
            if module == "intake":
                assert evidence["rows"] == bad + nulls + duplicate_rows
            executor_memory_entries = runtime.session.sparkContext._jsc.sc().getExecutorMemoryStatus().size()
            if settings.deployment_mode == "STANDALONE_CLIENT":
                assert executor_memory_entries >= 3, "Standalone must demonstrate two real executors plus driver"
            outcomes[module] = {"status": "PASS", "metrics": metrics, "decision": decision,
                "results": evidence, "accepted": accepted_evidence,
                "runtime": {**runtime.metadata, "executor_memory_status_entries": executor_memory_entries},
                "elapsed_seconds": round(time.monotonic() - started, 3), **memory.evidence()}
        (root / "evidence.json").write_text(json.dumps({"rows": rows, "outcomes": outcomes},
                                                     indent=2, ensure_ascii=False), encoding="utf-8")
        print(json.dumps({"module": module, "status": "PASS", "rows": rows,
                          "elapsed_seconds": outcomes[module]["elapsed_seconds"]}), flush=True)
    return {"status": "PASS", "certifying_million_rows": rows >= 1000000,
            "input_population_rows": rows, "global_profile": profile_evidence, "outcomes": outcomes,
            "expected_logical_fingerprints": {**expected_results, "intake_accepted": accepted_fingerprint},
            "complete_value_verification": "PASS"}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--rows", type=int, default=1000000)
    options = parser.parse_args()
    if options.rows < 1:
        parser.error("--rows must be positive")
    result = cycle(options.root, options.rows)
    (options.root / "evidence.json").write_text(json.dumps(result, indent=2, ensure_ascii=False), encoding="utf-8")
    print(json.dumps({"status": result["status"], "evidence": str(options.root / "evidence.json")}), flush=True)


if __name__ == "__main__":
    main()
