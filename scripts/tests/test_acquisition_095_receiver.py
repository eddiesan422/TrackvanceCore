"""The browser's real synthetic source has an independent, durable retry oracle."""

import importlib.util
import json
import threading
import urllib.error
import urllib.request
from decimal import Decimal
from pathlib import Path

import pytest

module_spec = importlib.util.spec_from_file_location("acquisition_095_receiver", Path(__file__).with_name("documents_095_receiver.py"))
receiver = importlib.util.module_from_spec(module_spec)
module_spec.loader.exec_module(receiver)


@pytest.fixture
def source(monkeypatch, tmp_path):
    monkeypatch.setenv("TRACKVANCE_CERTIFICATION_PROJECT", "trackvance-codex")
    monkeypatch.setenv("TRACKVANCE_DOCUMENT_FIXTURE_DIR", str(tmp_path))
    server = receiver.serve(0, str(tmp_path / "documents-095-123456abcdef.sqlite"), serve_forever=False)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_port}/acquisition/abcdef123456"
    server.shutdown()
    server.server_close()
    thread.join(timeout=2)


def request(url, method="GET", payload=None):
    try:
        body = json.dumps(payload).encode() if payload is not None else None
        with urllib.request.urlopen(urllib.request.Request(url, data=body, method=method), timeout=5) as response:
            return response.status, json.loads(response.read(), parse_float=Decimal)
    except urllib.error.HTTPError as error:
        return error.code, json.loads(error.read())


def test_discovery_unarmed_then_armed_acquisition_preserves_exact_same_page_and_population(source):
    initial = [request(source + f"/source?page={page}&page_size=2") for page in (1, 2, 3)]
    assert [status for status, _ in initial] == [200, 200, 200]
    rows = [row for _, body in initial for row in body["records"]]
    assert [row["id"] for row in rows] == ["000001", "000002", "000003"]
    assert type(rows[0]["amount"]) is Decimal and type(rows[0]["quantity"]) is int
    assert rows[0]["amount"] == Decimal("1.0") and rows[-1]["later"] == "last-only"
    assert "later" not in rows[0] and rows[0]["tags"] == ["not-expanded"]
    assert request(source + "/arm", "POST") == (201, {"armed": True, "page": 2, "failures": 1})
    acquired = [request(source + f"/source?page={page}&page_size=2") for page in (1, 2, 2, 3)]
    assert [status for status, _ in acquired] == [200, 503, 200, 200]
    assert [row for status, body in acquired if status == 200 for row in body["records"]] == rows
    status, proof = request(source + "/receipt")
    assert status == 200 and proof["expected_rows"] == 3 and proof["failures_remaining"] == 0
    assert proof["project"] == "trackvance-codex"
    assert proof["calls"] == [{"page": page, "page_size": 2, "status": status} for page, status in
        [(1, 200), (2, 200), (3, 200), (1, 200), (2, 503), (2, 200), (3, 200)]]


def test_source_rejects_invalid_pagination_without_counting_a_source_call(source):
    for parameters in ("page=0", "page=no", "page_size=0", "page_size=101"):
        assert request(source + "/source?" + parameters) == (422, {"code": "INVALID_SYNTHETIC_PAGINATION"})
    assert request(source + "/receipt")[1]["calls"] == []


def test_bounded_failure_controls_support_cancellation_name_reuse_and_failed_refresh(source):
    assert request(source + "/faults", "POST", {"failures": 0})[0] == 404
    assert request(source + "/arm", "POST", {"failures": 8})[0] == 201
    assert request(source + "/arm", "POST")[0] == 409
    assert request(source + "/source?page=2&page_size=2")[0] == 503
    assert request(source + "/receipt")[1]["failures_remaining"] == 7
    for invalid in (-1, 9, True, "2", None):
        assert request(source + "/faults", "POST", {"failures": invalid})[0] == 422
    assert request(source + "/receipt")[1]["failures_remaining"] == 7
    assert request(source + "/faults", "POST", {"failures": 0})[0] == 200
    assert request(source + "/source?page=2&page_size=2")[0] == 200
    assert request(source + "/faults", "POST", {"failures": 8})[0] == 200
    assert [request(source + "/source?page=2&page_size=2")[0] for _ in range(4)] == [503] * 4


def test_source_guard_never_opens_a_user_database(monkeypatch, tmp_path):
    monkeypatch.setenv("TRACKVANCE_DOCUMENT_FIXTURE_DIR", str(tmp_path))
    target = tmp_path / "documents-095-123456abcdef.sqlite"
    for project in ("trackvance-certification", "", "trackvance-codex-copy"):
        monkeypatch.setenv("TRACKVANCE_CERTIFICATION_PROJECT", project)
        with pytest.raises(RuntimeError):
            receiver.serve(0, str(target), serve_forever=False)
    monkeypatch.setenv("TRACKVANCE_CERTIFICATION_PROJECT", "trackvance-codex")
    for forbidden in (tmp_path / "metadata.sqlite", tmp_path.parent / target.name):
        with pytest.raises(RuntimeError):
            receiver.serve(0, str(forbidden), serve_forever=False)
    assert not target.exists() and not (tmp_path / "metadata.sqlite").exists()
