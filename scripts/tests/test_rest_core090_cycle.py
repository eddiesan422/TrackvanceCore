"""The real synthetic receiver rejects corrupt late values and atomic duplicates."""

import hashlib
import importlib.util
import json
import threading
import urllib.error
import urllib.request
from pathlib import Path

import pytest

module_spec = importlib.util.spec_from_file_location("rest_core090_cycle", Path(__file__).with_name("rest_core090_cycle.py"))
cycle = importlib.util.module_from_spec(module_spec)
module_spec.loader.exec_module(cycle)


@pytest.fixture
def server(monkeypatch, tmp_path):
    monkeypatch.setenv("TRACKVANCE_CERTIFICATION_PROJECT", "trackvance-codex")
    monkeypatch.setenv("TRACKVANCE_REST_FIXTURE_DIR", str(tmp_path))
    http = cycle.receiver(25, 0, str(tmp_path / "rest-core090-unit.sqlite"), serve_forever=False)
    thread = threading.Thread(target=http.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{http.server_port}"
    http.shutdown()
    http.server_close()
    thread.join(timeout=2)


def send(server, identity, records):
    body = json.dumps({"batch_id": identity, "records": records}, separators=(",", ":")).encode()
    digest = hashlib.sha256(body).hexdigest()
    request = urllib.request.Request(server + "/write", data=body, headers={"Content-Type": "application/json",
        "Idempotency-Key": identity, "X-Trackvance-Payload-SHA256": digest})
    try:
        with urllib.request.urlopen(request, timeout=5) as response:
            return response.status, json.load(response)
    except urllib.error.HTTPError as error:
        return error.code, json.load(error)


def receipt(server):
    with urllib.request.urlopen(server + "/receipt", timeout=5) as response:
        return json.load(response)


def test_receiver_guard_rejects_permanent_installation_and_unowned_path(monkeypatch, tmp_path):
    for project in ("trackvance-certification", "bikerwash", "", "trackvance-codex-copy"):
        monkeypatch.setenv("TRACKVANCE_CERTIFICATION_PROJECT", project)
        with pytest.raises(RuntimeError):
            cycle.receiver(1, 0, str(tmp_path / "rest-core090-guard.sqlite"), serve_forever=False)
    monkeypatch.setenv("TRACKVANCE_CERTIFICATION_PROJECT", "trackvance-codex")
    monkeypatch.setenv("TRACKVANCE_REST_FIXTURE_DIR", str(tmp_path))
    with pytest.raises(RuntimeError):
        cycle.receiver(1, 0, str(tmp_path / "metadata.sqlite"), serve_forever=False)
    with pytest.raises(RuntimeError):
        cycle.receiver(1, 0, str(tmp_path.parent / "rest-core090-other.sqlite"), serve_forever=False)
    assert not (tmp_path / "metadata.sqlite").exists()


def test_receiver_real_idempotency_reuses_identity_and_payload(server):
    records = [cycle.synthetic_row(i) for i in range(1, 26)]
    first, replay = send(server, "same-batch", records), send(server, "same-batch", records)
    assert first == replay and first[0] == 200
    state = receipt(server)
    assert state["received_rows"] == 25 and state["batches"] == 1
    assert state["population_sha256"] == state["expected_population_sha256"]
    status, response = send(server, "same-batch", records[:-1])
    assert status == 422 and response["accepted"] is False
    assert receipt(server)["received_rows"] == 25


def test_duplicate_ids_roll_back_whole_later_batch(server):
    assert send(server, "first", [cycle.synthetic_row(1)])[0] == 200
    status, response = send(server, "duplicate", [cycle.synthetic_row(2), cycle.synthetic_row(1)])
    assert status == 422 and response["rows"] == 0
    state = receipt(server)
    assert state["received_rows"] == 1 and state["batches"] == 1
    assert send(server, "second", [cycle.synthetic_row(2)])[0] == 200


def test_full_population_oracle_detects_last_row_mutation(server):
    rows = [cycle.synthetic_row(i) for i in range(1, 26)]
    rows[-1]["amount"] = "0.0000"
    assert send(server, "late-corruption", rows)[0] == 422
    assert receipt(server)["received_rows"] == 0
    rows[-1] = cycle.synthetic_row(25)
    assert send(server, "complete", rows)[0] == 200
    state = receipt(server)
    assert state["received_rows"] == 25 and state["population_sha256"] == state["expected_population_sha256"]


def test_source_pagination_preserves_complete_population_and_empty_terminal_page(server):
    observed = []
    for page in range(1, 5):
        with urllib.request.urlopen(server + f"/source?page={page}&page_size=10", timeout=5) as response:
            rows = json.load(response)["records"]
        assert len(rows) == [10, 10, 5, 0][page - 1]
        observed.extend(rows)
    assert observed == [cycle.synthetic_row(i) for i in range(1, 26)]


def committed_batches(server):
    result = []
    for identity, first, last in (("z-first", 1, 11), ("a-last", 11, 26)):
        records = [cycle.synthetic_row(i) for i in range(first, last)]
        body = json.dumps({"batch_id": identity, "records": records}, separators=(",", ":")).encode()
        status, confirmation = send(server, identity, records)
        assert status == 200
        result.append({"id": identity, "payload_sha256": confirmation["payload_sha256"],
                       "rows": confirmation["rows"], "payload_bytes": len(body)})
    return result


def test_batch_metadata_matches_independent_durable_receiver_in_id_order(server):
    batches = committed_batches(server)
    state = receipt(server)
    assert state["received_rows"] == state["confirmed_rows"] == 25
    assert state["batches"] == 2 and state["payload_bytes"] == sum(batch["payload_bytes"] for batch in batches)
    assert state["batch_metadata_sha256"] == cycle.batch_metadata_digest(batches)
    assert state["batch_metadata_sha256"] == cycle.batch_metadata_digest(list(reversed(batches)))
    assert state["population_sha256"] == state["expected_population_sha256"]


@pytest.mark.parametrize(("field", "corrupt"), [("id", "other-batch"), ("payload_sha256", "0" * 64),
                                               ("rows", 1), ("payload_bytes", 1)])
def test_batch_metadata_oracle_detects_altered_api_evidence(server, field, corrupt):
    batches = committed_batches(server)
    batches[-1][field] = corrupt
    state = receipt(server)
    assert state["confirmed_rows"] == 25 and state["batches"] == 2
    assert state["batch_metadata_sha256"] != cycle.batch_metadata_digest(batches)
