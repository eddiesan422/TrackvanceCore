"""The real confined DuckDB process, compared with a Python join oracle."""
import json
import re
import shutil
import subprocess
import sys
from collections import Counter
from pathlib import Path

import polars as pl
import pytest

from trackvance.operations_common import OperationError
from trackvance.report_executor import execute_messages
from trackvance.report_query import compile_draft

pytestmark = pytest.mark.skipif(sys.platform != "linux", reason="Linux Landlock/seccomp executor is certified in Docker")


def fixture_sources(tmp_path):
    a = [["001", "12.01"], ["001", "3.10"], ["é", "7.00"], [None, "9.00"], ["", "8.00"]]
    b = [["001", "A"], ["002", "B"], [None, "NULL"], ["", "EMPTY"], ["東京", "U"]]
    sources, schemas = [], {}
    for alias, names, data, logical in [("a", ["id", "amount"], a, ["STRING", "DECIMAL"]),
                                       ("b", ["key", "value"], b, ["STRING", "STRING"])]:
        path = tmp_path / (alias + ".parquet")
        pl.DataFrame(data, schema={n: pl.String for n in names}, orient="row").write_parquet(path)
        schemas[alias] = [{"name": n, "logical_type": k} for n, k in zip(names, logical, strict=True)]
        sources.append({"alias": alias, "schema": schemas[alias], "paths": [str(path)]})
    return sources, schemas, a, b


def plan_for(schemas, kind="INNER", allow=False):
    draft = {"mode": "GUIDED", "sources": [{"alias": alias} for alias in schemas],
             "joins": [{"left_alias": "a", "right_alias": "b", "type": kind,
                        "keys": [{"left_column": "id", "right_column": "key"}],
                        "expected_cardinality": "N:1", "allow_many_to_many": allow}],
             "columns": [{"source_alias": "a", "column": "id", "alias": "left_id"},
                         {"source_alias": "a", "column": "amount", "alias": "amount"},
                         {"source_alias": "b", "column": "key", "alias": "right_id"},
                         {"source_alias": "b", "column": "value", "alias": "value"}]}
    return compile_draft(draft, schemas)


def _rows(messages):
    return [tuple(value["value"] if isinstance(value, dict) else value for value in row)
            for message in messages if message["kind"] == "batch" for row in message["rows"]]


@pytest.mark.parametrize("kind", ["INNER", "LEFT", "RIGHT", "FULL"])
def test_real_join_complete_population_matches_independent_oracle(tmp_path, kind):
    sources, schemas, a, b = fixture_sources(tmp_path)
    messages = list(execute_messages(sources, plan_for(schemas, kind), "DOWNLOAD"))
    actual = Counter(_rows(messages))
    expected = []
    for left in a:
        matching = [r for r in b if left[0] is not None and r[0] is not None and left[0] == r[0]]
        expected.extend((left[0], left[1], right[0], right[1]) for right in matching)
        if not matching and kind in {"LEFT", "FULL"}:
            expected.append((left[0], left[1], None, None))
    if kind in {"RIGHT", "FULL"}:
        expected.extend((None, None, right[0], right[1]) for right in b
                        if not any(left[0] is not None and right[0] is not None and left[0] == right[0] for left in a))
    assert actual == Counter(expected)
    check = next(m for m in messages if m["kind"] == "cardinality")["items"][0]
    assert check["joined_rows"] == len(expected)
    assert check["observed"] == "N:1"
    metrics = next(message for message in messages if message["kind"] == "complete")
    assert metrics["cpu_user_seconds"] >= 0 and metrics["cpu_system_seconds"] >= 0
    assert metrics["max_rss_bytes"] > 0
    for name in ("cgroup_memory_current_bytes", "cgroup_memory_lifetime_peak_bytes"):
        if name in metrics:
            assert metrics[name] > 0


def test_many_to_many_is_detected_over_full_source_and_authorized_explicitly(tmp_path):
    sources, schemas, _, _ = fixture_sources(tmp_path)
    path = tmp_path / "b.parquet"
    pl.DataFrame({"key": ["001", "001"], "value": ["A", "B"]}).write_parquet(path)
    with pytest.raises(OperationError) as rejected:
        list(execute_messages(sources, plan_for(schemas), "DOWNLOAD"))
    assert rejected.value.code == "REPORT_MANY_TO_MANY"
    messages = list(execute_messages(sources, plan_for(schemas, allow=True), "DOWNLOAD"))
    assert len(_rows(messages)) == 4


def test_pre_filters_change_real_cardinality_without_moving_where(tmp_path):
    sources, schemas, _, _ = fixture_sources(tmp_path)
    plan = plan_for(schemas, "FULL")
    plan["source_filters"] = {"a": '"a"."amount">$cutoff'}
    plan["parameters"] = {"cutoff": {"type": "DECIMAL", "value": "5"}}
    sources[0]["filter"] = plan["source_filters"]["a"]
    messages = list(execute_messages(sources, plan, "DOWNLOAD"))
    assert len([r for r in _rows(messages) if r[0] == "001"]) == 1
    assert next(m for m in messages if m["kind"] == "cardinality")["items"][0]["observed"] == "1:1"


def test_preview_is_real_join_but_at_most_ten_rows(tmp_path):
    sources, schemas, _, _ = fixture_sources(tmp_path)
    messages = list(execute_messages(sources, plan_for(schemas, "FULL"), "PREVIEW"))
    assert len(_rows(messages)) <= 10
    assert next(m for m in messages if m["kind"] == "complete")["sample"] is True


def test_executor_confines_files_network_environment_and_during_write(tmp_path, monkeypatch):
    sources, schemas, _, _ = fixture_sources(tmp_path)
    secret = tmp_path / "unselected-secret"
    secret.write_text("synthetic-do-not-disclose")
    target = tmp_path / "forbidden-result"
    monkeypatch.setenv("DATABASE_URL", "synthetic-sensitive-database")
    monkeypatch.setenv("UNRELATED_SECRET", "synthetic-sensitive-token")
    messages = list(execute_messages(sources, plan_for(schemas), "DOWNLOAD",
                    probe={"forbidden": str(secret), "write_target": str(target)}))
    assert {"confined_threads_available", "clone3_unavailable", "process_creation_denied"} <= messages[0]["checks"].keys()
    assert all(messages[0]["checks"].values())
    assert not target.exists()


def test_expansion_and_result_limits_fail_without_truncation(tmp_path, monkeypatch):
    sources, schemas, _, _ = fixture_sources(tmp_path)
    monkeypatch.setenv("REPORT_MAX_JOIN_ROWS", "1")
    with pytest.raises(OperationError) as rejected:
        list(execute_messages(sources, plan_for(schemas, "FULL"), "DOWNLOAD"))
    assert rejected.value.code == "REPORT_JOIN_EXPANSION"
    monkeypatch.setenv("REPORT_MAX_JOIN_ROWS", "5000000")
    monkeypatch.setenv("REPORT_DOWNLOAD_MAX_ROWS", "1")
    with pytest.raises(OperationError) as rejected:
        list(execute_messages(sources, plan_for(schemas, "FULL"), "DOWNLOAD"))
    assert rejected.value.code == "REPORT_RESULT_LIMIT"


def test_exact_decimal_overflow_is_rejected_before_publication(tmp_path):
    sources, schemas, _, _ = fixture_sources(tmp_path)
    path = tmp_path / "a.parquet"
    pl.DataFrame({"id": ["001"], "amount": ["123456789012345678901234567890123456789.0"]}).write_parquet(path)
    with pytest.raises(OperationError) as rejected:
        list(execute_messages(sources, plan_for(schemas), "DOWNLOAD"))
    assert rejected.value.code == "REPORT_DECIMAL_PRECISION"


@pytest.mark.skipif(shutil.which("strace") is None, reason="Syscall evidence runs in the isolated diagnostic image with strace")
@pytest.mark.parametrize("profile,format_name,fail", [
    ("PREVIEW", "JSON", False), ("DOWNLOAD", "CSV", False),
    ("XLSX", "XLSX", False), ("DOWNLOAD", "CSV", True),
])
def test_ephemeral_engine_and_exporters_make_no_filesystem_write_attempt(tmp_path, profile, format_name, fail):
    """Observe open/mutation syscalls while executing, including rejected results.

    The observer alone owns a trace file. Neither the report coordinator,
    exporter nor its real confined DuckDB child receives that path.
    """
    sources, schemas, _, _ = fixture_sources(tmp_path)
    trace = tmp_path / "observer.trace"
    script = '''
import json, sys
payload = json.load(sys.stdin)
sys.path.insert(0, payload["source_root"])
from trackvance.operations_common import OperationError
from trackvance.report_executor import execute_messages
from trackvance.report_exports import csv_stream, xlsx_stream
sys.stderr.write("REPORT_OBSERVE_START\\n")
sys.stderr.flush()
messages = execute_messages(payload["sources"], payload["plan"], payload["profile"])
try:
    columns = []
    for message in messages:
        if message["kind"] == "schema":
            columns = message["columns"]
            break
    def batches():
        for message in messages:
            if message["kind"] == "batch":
                yield message["rows"]
    if payload["format"] == "JSON":
        count = sum(len(rows) for rows in batches())
    else:
        writer = csv_stream if payload["format"] == "CSV" else xlsx_stream
        count = sum(len(chunk) for chunk in writer(columns, batches()))
    assert not payload["fail"] and count > 0
    print("complete")
except OperationError as exc:
    assert payload["fail"] and exc.code == "REPORT_RESULT_LIMIT"
    print("expected-resource-failure")
finally:
    messages.close()
'''
    environment = {"PATH": "/usr/local/bin:/usr/bin:/bin", "LANG": "C.UTF-8", "HOME": "/nonexistent",
                   "PYTHONDONTWRITEBYTECODE": "1", "OPENBLAS_NUM_THREADS": "1", "OMP_NUM_THREADS": "1",
                   "TRACKVANCE_STORAGE_DIR": str(tmp_path), "DATABASE_URL": "sqlite:///:memory:",
                   "LD_LIBRARY_PATH": str(Path(sys.base_prefix) / "lib")}
    if fail:
        environment["REPORT_DOWNLOAD_MAX_ROWS"] = "1"
    observed = subprocess.run(["strace", "-f", "-e", "trace=%file,mmap,write", "-o", str(trace),
                               sys.executable, "-I", "-B", "-c", script],
        input=json.dumps({"sources": sources, "plan": plan_for(schemas, "FULL"),
                          "source_root": str(Path(__file__).resolve().parents[1] / "src"),
                          "profile": profile, "format": format_name, "fail": fail}),
        text=True, capture_output=True, env=environment, timeout=60, check=False)
    assert observed.returncode == 0, observed.stderr
    all_lines = trace.read_text().splitlines()
    start = next(i for i, line in enumerate(all_lines) if 'write(2, "REPORT_OBSERVE_START\\n"' in line)
    evidence = "\n".join(all_lines[start + 1:])
    assert "part-" not in evidence
    # Popen opens the existing null character device for inherited stderr;
    # it cannot hold result bytes and is the only writable destination allowed.
    writable = [line for line in evidence.splitlines()
                if re.search(r"\bO_(?:WRONLY|RDWR|CREAT|TRUNC|APPEND)\b", line) and '"/dev/null", O_RDWR|O_CLOEXEC)' not in line]
    assert not writable, "\n".join(writable)
    mutations = [line for line in evidence.splitlines() if re.search(r"\b(?:creat|mkdir|mkdirat|unlink|unlinkat|rename|renameat|renameat2|link|linkat|symlink|symlinkat|truncate|ftruncate|chmod|fchmodat|chown|fchownat|utime|utimes|utimensat)\(", line)]
    assert not mutations, "\n".join(mutations)
    assert not any("PROT_WRITE" in line and "MAP_SHARED" in line and "MAP_ANONYMOUS" not in line
                   for line in evidence.splitlines())
