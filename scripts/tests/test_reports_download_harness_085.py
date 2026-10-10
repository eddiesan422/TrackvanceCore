"""Independent client readers reject partial exports and compare every typed row."""
import importlib.util
import json
import sys
import threading
import zipfile
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "backend/src"))
SPEC = importlib.util.spec_from_file_location("reports_download_085", Path(__file__).with_name("reports_download_085.py"))
assert SPEC and SPEC.loader
runner = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(runner)


@pytest.mark.parametrize("zip64_reader_fixture", [False, True])
def test_full_independent_csv_xml_and_reader_oracles_preserve_exact_values(tmp_path, monkeypatch, zip64_reader_fixture):
    monkeypatch.setenv("DATABASE_URL", "sqlite:///:memory:")
    monkeypatch.setenv("TRACKVANCE_STORAGE_DIR", str(tmp_path / "storage"))
    from trackvance.report_exports import csv_stream, xlsx_stream

    expected_rows = list(runner.expected_rows(15, 3))
    expected = runner.population_hash(iter(expected_rows))
    columns = [{"name": name} for name in runner.HEADERS]
    csv_path, xlsx_path = tmp_path / "complete.csv", tmp_path / "complete.xlsx.incomplete"
    csv_path.write_bytes(b"".join(csv_stream(columns, iter([expected_rows]))))
    xlsx_path.write_bytes(b"".join(xlsx_stream(columns, iter([expected_rows]))))
    if zip64_reader_fixture:
        # Reader coverage of ZIP64 is independent from the bounded ZIP32
        # streaming product. A seekable client fixture has exact local sizes.
        with zipfile.ZipFile(xlsx_path) as source:
            members = {name: source.read(name) for name in source.namelist()}
        with zipfile.ZipFile(xlsx_path, "w", compression=zipfile.ZIP_DEFLATED) as target:
            for name, content in members.items():
                with target.open(name, "w", force_zip64=name.endswith("sheet1.xml")) as output:
                    output.write(content)
    assert runner.population_hash(runner.csv_population(csv_path)) == expected
    assert runner.population_hash(runner.xlsx_population(xlsx_path)) == expected
    assert runner.population_hash(runner.openpyxl_population(xlsx_path)) == expected
    result = runner.validate_package(xlsx_path)
    assert result["status"] == "PASS" and result["expanded_bytes"] > result["serialized_zip_bytes"]
    assert result["zip64_present"] is zip64_reader_fixture
    assert expected_rows[0][0] == "000000000001"
    assert [row[8] for row in expected_rows[:5]] == [None, "", r"\N", "'literal", " "]


def test_partial_zip_is_never_certified(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "sqlite:///:memory:")
    monkeypatch.setenv("TRACKVANCE_STORAGE_DIR", str(tmp_path / "storage"))
    from trackvance.report_exports import xlsx_stream

    path = tmp_path / "partial.xlsx"
    content = b"".join(xlsx_stream([{"name": "id"}], iter([[["001"]]])))
    path.write_bytes(content[:-50])
    with pytest.raises(zipfile.BadZipFile):
        runner.validate_package(path)


@pytest.mark.parametrize("rows,limit,browser,actions,valid", [
    ([1000000], 1000000, True, False, True), ([400000, 1000000], 1000000, True, False, False),
    ([1000000], 1000000, False, False, False), ([120], 120, False, False, False),
    ([1000000], 1000000, True, True, False),
])
def test_comparison_rejects_actions_and_any_scope_other_than_local_million(rows, limit, browser, actions, valid, monkeypatch):
    monkeypatch.delenv("GITHUB_ACTIONS", raising=False)
    if actions:
        monkeypatch.setenv("GITHUB_ACTIONS", "true")
    if valid:
        runner.validate_comparison_scope(rows, limit, browser)
    else:
        with pytest.raises(runner.CheckFailed, match="PREVIEW_COMPARISON"):
            runner.validate_comparison_scope(rows, limit, browser)


@pytest.mark.parametrize("status,code,baseline,allowed", [
    (200, None, True, True), (422, "REPORT_TIMEOUT", True, True),
    (422, "REPORT_TIMEOUT", False, False), (422, "REPORT_QUERY_FAILED", True, False),
])
def test_comparison_actual_http_contract_retains_failed_baseline_and_request_counter_deltas(monkeypatch, status, code, baseline, allowed):
    # Real loopback HTTP, small synthetic response and synthetic cgroup counters:
    # this unit guard is not a Linux performance/causality certification.
    rows = list(runner.expected_rows(10, 100000))
    value = {"status": "SUCCESS", "execution_id": "unit-execution", "rows":
             [dict(zip(runner.HEADERS, row, strict=True)) for row in rows]} if status == 200 else {
             "error": {"code": code, "details": {"execution_id": "unit-execution"}}}
    observed = []
    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            observed.append(json.loads(self.rfile.read(int(self.headers["Content-Length"]))))
            self.send_response(status)
            self.end_headers()
            self.wfile.write(json.dumps(value).encode())

        def log_message(self, *_):
            pass
    server = HTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    counters = iter([{"container_id": "a" * 64, "cpu.stat": {"usage_usec": n},
                      "memory.events": {"oom": 0, "oom_kill": 0}, "memory.peak": 1000}
                     for n in (100, 500)])
    monkeypatch.setattr(runner, "api_counters", lambda _: next(counters))
    frozen = {"context_id": "same-context", "query_hash": "f" * 64, "sources": [{"version_id": "same-version"}]}
    try:
        receipt = runner.measured_preview(runner.ProxyClient(server.server_port, 3), {}, frozen, 100000, baseline=baseline)
    finally:
        server.shutdown()
        server.server_close()
        thread.join(2)
    assert observed == [{"context_id": "same-context"}]
    assert receipt["allowed_observation"] is allowed and receipt["query_hash"] == frozen["query_hash"]
    assert receipt["counter_deltas"]["cpu.stat"]["usage_usec"] == 400
    assert receipt["error_code"] == code and receipt["status"] == ("PASS" if status == 200 else "FAIL")
    assert receipt["expected_baseline_timeout"] is (baseline and code == "REPORT_TIMEOUT")
