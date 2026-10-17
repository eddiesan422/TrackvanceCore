"""Small acceptance oracles for QA-090-01/02/03 and full snapshot updates."""
import json
import threading
import time
from datetime import timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

import pytest
from sqlalchemy import func, select

from trackvance import acquisition, rest_retry
from trackvance.acquisition_models import AcquisitionNameReservation, AcquisitionRun
from trackvance.acquisition_schedule import dispatch_due
from trackvance.acquisition_schedule_models import (
    AcquisitionOccurrence,
    AcquisitionSourceConfiguration,
)
from trackvance.automation_models import OutboxEvent
from trackvance.db import utcnow
from trackvance.models import (
    AuditEvent,
    Dataset,
    DatasetSourceBinding,
    DatasetVersion,
    ExternalConnection,
    Job,
)
from trackvance.rest_discovery import infer_records
from trackvance.rest_source import RestDatasetSource
from trackvance.rest_transport import RestError, exact_json, validate_options


@pytest.fixture
def remote(monkeypatch):
    state = SimpleNamespace(rows=[{"id": "000001", "value": "12.2300"}, {"id": "000002", "value": "0.0001"}],
        failures={}, delays={}, calls=[], body=None)
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass
        def do_GET(self):
            query = parse_qs(urlsplit(self.path).query)
            page, size = int(query.get("page", [1])[0]), int(query.get("page_size", [100])[0])
            state.calls.append((page, size))
            delay = state.delays.pop(page, 0)
            if delay:
                time.sleep(delay)
            failing = state.failures.get(page, 0) > 0
            if failing:
                state.failures[page] -= 1
            body = state.body if state.body is not None else json.dumps({"records": state.rows[(page - 1) * size:page * size]}).encode()
            self.send_response(503 if failing else 200)
            self.send_header("Content-Length", str(len(body))); self.end_headers()
            try:
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError):
                pass
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    worker = threading.Thread(target=server.serve_forever, daemon=True); worker.start()
    state.url = f"http://127.0.0.1:{server.server_port}/source"
    monkeypatch.setenv("TRACKVANCE_REST_ALLOWED_INTERNAL_ORIGINS", f"http://127.0.0.1:{server.server_port}")
    yield state
    server.shutdown(); server.server_close(); worker.join(timeout=2)


def connection(client, remote, **options):
    result = client.post("/api/v1/connections", json={"name": "Source 095", "source_type": "REST", "options": {
        "url": remote.url, "auth": "NONE", "collection": "records", "fields": [{"name": "id", "logical_type": "STRING"},
            {"name": "value", "logical_type": "DECIMAL"}], "pagination": {"mode": "PAGE"}, "page_size": 1, "max_pages": 10, **options}})
    assert result.status_code == 201, result.text
    remote.calls.clear()
    return result.json()


def initial(client, connection, *, name="Exact reserved name"):
    result = client.post(f"/api/v1/connections/{connection['id']}/acquisitions", json={"name": name})
    assert result.status_code == 202, result.text
    assert result.json()["dataset"] is None
    assert result.json()["acquisition"]["dataset_id"] is None
    return result.json()["acquisition"]


def completed(client, run):
    assert acquisition.process_once("qa095-worker")
    return client.get(f"/api/v1/acquisitions/{run['id']}").json()


def test_initial_identity_reservation_cancel_failure_and_exact_name_reuse(authenticated, database, remote):
    source = connection(authenticated, remote)
    run = initial(authenticated, source)
    with database() as db:
        assert db.scalar(select(func.count()).select_from(Dataset)) == 0
        assert db.scalar(select(func.count()).select_from(DatasetSourceBinding)) == 0
        assert db.scalar(select(func.count()).select_from(OutboxEvent).where(OutboxEvent.event_type == "DATASET_VERSION_PUBLISHED")) == 0
    assert authenticated.post(f"/api/v1/connections/{source['id']}/acquisitions", json={"name": "Exact reserved name"}).status_code == 409
    assert authenticated.post("/api/v1/datasets", json={"name": "Exact reserved name"}).status_code == 409
    assert authenticated.post(f"/api/v1/acquisitions/{run['id']}/cancel").json()["status"] == "CANCELLED"
    assert not acquisition.process_once("late-worker")
    remote.failures[1] = 1
    failed = completed(authenticated, initial(authenticated, source))
    assert failed["status"] == "FAILED" and failed["dataset_id"] is None
    assert failed["error_code"] == "REST_HTTP_TRANSIENT_503"
    with database() as db:
        assert db.scalar(select(func.count()).select_from(AcquisitionRun)) == 2
        assert db.scalar(select(func.count()).select_from(AcquisitionNameReservation)) == 0
        assert db.scalar(select(func.count()).select_from(Dataset)) == 0
    success = completed(authenticated, initial(authenticated, source))
    assert success["status"] == "SUCCESS" and success["dataset_id"] and success["output_version_id"]
    with database() as db:
        assert db.scalar(select(func.count()).select_from(Dataset)) == 1
        assert db.scalar(select(func.count()).select_from(DatasetSourceBinding)) == 1
        assert db.scalar(select(func.count()).select_from(DatasetVersion)) == 1
        assert db.scalar(select(func.count()).select_from(OutboxEvent).where(OutboxEvent.event_type == "DATASET_VERSION_PUBLISHED")) == 1


@pytest.mark.parametrize("failure", ["HTTP", "EMPTY"])
def test_legacy_first_failure_has_native_evidence_no_dataset_and_exact_name_reuse(authenticated, database, remote, failure):
    source = connection(authenticated, remote)
    if failure == "HTTP":
        remote.failures[1] = 1
    else:
        remote.rows.clear()
    path = f"/api/v1/connections/{source['id']}/datasets"
    body = {"name": "Legacy exact name", "schema_name": "REST", "object_name": "collection"}
    result = authenticated.post(path, json=body)
    assert result.status_code == 422, result.text
    with database() as db:
        assert db.scalar(select(func.count()).select_from(Dataset)) == 0
        assert db.scalar(select(func.count()).select_from(DatasetSourceBinding)) == 0
        assert db.scalar(select(func.count()).select_from(DatasetVersion)) == 0
        assert db.scalar(select(func.count()).select_from(AcquisitionNameReservation)) == 0
        attempt = db.scalar(select(AcquisitionRun))
        assert attempt.status == "FAILED" and attempt.dataset_id is None and attempt.output_version_id is None
        assert attempt.error_code == ("REST_HTTP_TRANSIENT_503" if failure == "HTTP" else "ACQUISITION_EMPTY_BLOCKED")
        assert attempt.initiated_by_id == "test-user" and attempt.error_reference == attempt.id
        assert attempt.source_snapshot["connection_version_id"] == source["connection_version_id"]
        assert attempt.source_snapshot["pending_dataset"]["name"] == "Legacy exact name"
    if not remote.rows:
        remote.rows.append({"id": "000001", "value": "12.2300"})
    successful = authenticated.post(path, json=body)
    assert successful.status_code == 201, successful.text
    version = successful.json()["version"]
    with database() as db:
        assert db.scalar(select(func.count()).select_from(Dataset)) == 1
        assert db.scalar(select(func.count()).select_from(DatasetVersion)) == 1
        successes = db.scalars(select(AcquisitionRun).where(AcquisitionRun.status == "SUCCESS")).all()
        assert len(successes) == 1 and successes[0].output_version_id == version["id"]
        assert db.scalar(select(func.count()).select_from(OutboxEvent).where(OutboxEvent.event_type == "DATASET_VERSION_PUBLISHED")) == 1


def test_configured_dataset_cannot_bypass_on_change_with_legacy_refresh(authenticated, database, remote):
    source = connection(authenticated, remote)
    first = completed(authenticated, initial(authenticated, source))
    _settings(authenticated, source, first["dataset_id"])
    response = authenticated.post(f"/api/v1/datasets/{first['dataset_id']}/refresh-source")
    assert response.status_code == 409 and response.json()["error"]["code"] == "ASYNC_SOURCE_REFRESH_REQUIRED"
    assert "/acquisitions/refresh" in response.json()["error"]["message"]
    with database() as db:
        assert db.scalar(select(func.count()).select_from(DatasetVersion)) == 1


def test_retry_same_page_complete_population_and_history(authenticated, database, remote):
    source = connection(authenticated, remote, retries=2, retry_wait_seconds=1)
    remote.failures[2] = 2
    result = completed(authenticated, initial(authenticated, source))
    assert result["status"] == "SUCCESS", result
    assert [page for page, _ in remote.calls] == [1, 2, 2, 2, 3]
    assert result["retry_state"]["status"] == "SUCCEEDED"
    waits = [item for item in result["retry_state"]["history"] if item["status"] == "WAITING"]
    assert len(waits) == 2 and all(item["cause"] == "REST_HTTP_TRANSIENT_503" and item["next_retry_at"] for item in waits)
    profile = authenticated.get(f"/api/v1/dataset-versions/{result['output_version_id']}/profile").json()
    assert profile["sample"] == remote.rows
    with database() as db:
        assert db.scalar(select(func.count()).select_from(DatasetVersion)) == 1
        events = db.scalars(select(AuditEvent).where(AuditEvent.event_type == "ACQUISITION_REST_RETRY").order_by(AuditEvent.created_at)).all()
        assert [item.metadata_json["status"] for item in events] == ["WAITING", "WAITING", "SUCCEEDED"]
        assert events[0].metadata_json["error_code"] == "REST_HTTP_TRANSIENT_503"
        assert events[0].metadata_json["captured_at"] and events[0].metadata_json["planned_at"]


@pytest.mark.parametrize("total,expected", [(5, "REST_REQUEST_TIMEOUT"), (1, "REST_TOTAL_TIMEOUT")])
def test_first_timeout_keeps_diagnostic_and_releases_name_without_operational_dataset(authenticated, database, remote, total, expected):
    source = connection(authenticated, remote, timeout_seconds=1, total_timeout_seconds=total)
    remote.delays[1] = 1.2
    result = completed(authenticated, initial(authenticated, source))
    assert result["status"] == "FAILED" and result["error_code"] == expected, result
    with database() as db:
        assert db.scalar(select(func.count()).select_from(Dataset)) == 0
        assert db.scalar(select(func.count()).select_from(AcquisitionNameReservation)) == 0
        assert db.get(AcquisitionRun, result["id"]).error_reference == result["id"]
    reused = initial(authenticated, source)
    assert authenticated.post(f"/api/v1/acquisitions/{reused['id']}/cancel").status_code == 200


def test_cancel_during_persisted_retry_wait_never_calls_next_page(authenticated, database, remote):
    source = connection(authenticated, remote, retries=3, retry_wait_seconds=2)
    remote.failures[1] = 3
    run = initial(authenticated, source)
    worker = threading.Thread(target=lambda: acquisition.process_once("cancel-retry-worker"))
    worker.start()
    for _ in range(150):
        state = authenticated.get(f"/api/v1/acquisitions/{run['id']}").json()
        if state["retry_state"].get("status") == "WAITING":
            break
        time.sleep(0.01)
    else:
        pytest.fail("No se observó la espera de retry persistida.")
    assert authenticated.post(f"/api/v1/acquisitions/{run['id']}/cancel").status_code == 200
    worker.join(timeout=5)
    assert not worker.is_alive()
    result = authenticated.get(f"/api/v1/acquisitions/{run['id']}").json()
    assert result["status"] == "CANCELLED" and remote.calls == [(1, 1)]
    with database() as db:
        assert db.scalar(select(func.count()).select_from(DatasetVersion)) == 0
        assert db.scalar(select(func.count()).select_from(AcquisitionNameReservation)) == 0


def test_restart_expired_total_deadline_does_not_restart_budget(authenticated, database, remote):
    source = connection(authenticated, remote, timeout_seconds=1, total_timeout_seconds=2, retries=2)
    run = initial(authenticated, source)
    with database() as db:
        acquisition_run = db.get(AcquisitionRun, run["id"])
        job = db.scalar(select(Job).where(Job.acquisition_id == run["id"]))
        acquisition_run.started_at, acquisition_run.status, acquisition_run.attempt_id = utcnow() - timedelta(seconds=3), "RUNNING", "dead-attempt"
        acquisition_run.retry_state = {"attempt": 1, "status": "WAITING", "cause": "REST_REQUEST_TIMEOUT"}
        job.status, job.lease_owner, job.lease_until = "RUNNING", "dead-worker", utcnow() - timedelta(seconds=1)
        db.commit()
    result = completed(authenticated, run)
    assert result["status"] == "FAILED" and result["error_code"] == "REST_TOTAL_TIMEOUT", result
    assert remote.calls == []


def _settings(client, source, dataset_id, **changes):
    current = client.get(f"/api/v1/datasets/{dataset_id}/source-settings").json()
    result = client.put(f"/api/v1/datasets/{dataset_id}/source-settings", json={"expected_version": current["version"],
        "connection_id": source["id"], "schema_name": "REST", "object_name": "collection", "responsible_user_id": "test-user",
        "publication_policy": "ON_CHANGE", "empty_policy": "BLOCK", **changes})
    assert result.status_code == 200, result.text
    return result.json()


def test_update_no_changes_multiplicity_every_cut_empty_and_failed_refresh(authenticated, database, remote):
    source = connection(authenticated, remote)
    first = completed(authenticated, initial(authenticated, source))
    dataset_id = first["dataset_id"]
    _settings(authenticated, source, dataset_id)
    def refresh():
        response = authenticated.post(f"/api/v1/datasets/{dataset_id}/acquisitions/refresh")
        assert response.status_code == 202, response.text
        return completed(authenticated, response.json())
    remote.rows.reverse()
    remote.rows[1]["value"] = "12.23"
    same = refresh()
    assert same["status"] == "SUCCESS" and same["stage"] == "UNCHANGED" and same["output_version_id"] is None, same
    remote.rows.append(dict(remote.rows[0]))
    second = refresh()
    assert second["status"] == "SUCCESS" and second["output_version_id"]
    _settings(authenticated, source, dataset_id, publication_policy="EVERY_CUT")
    third = refresh()
    assert third["status"] == "SUCCESS" and third["output_version_id"] not in {first["output_version_id"], second["output_version_id"]}
    remote.rows.clear()
    assert refresh()["error_code"] == "ACQUISITION_EMPTY_BLOCKED"
    _settings(authenticated, source, dataset_id, empty_policy="PRESERVE")
    assert refresh()["stage"] == "EMPTY_PRESERVED"
    remote.failures[1] = 1
    assert refresh()["status"] == "FAILED"
    with database() as db:
        assert db.scalar(select(func.count()).select_from(DatasetVersion)) == 3
    clocks = authenticated.get(f"/api/v1/datasets/{dataset_id}/source-settings").json()
    assert clocks["last_attempt_at"] and clocks["last_query_success_at"] and clocks["last_publication_at"]


def test_scheduler_occurrence_exclusion_and_recovery_preserve_one_logical_acquisition(authenticated, database, remote):
    source = connection(authenticated, remote)
    first = completed(authenticated, initial(authenticated, source))
    _settings(authenticated, source, first["dataset_id"], enabled=True, interval_seconds=60, publication_policy="EVERY_CUT")
    with database() as db:
        now = utcnow() + timedelta(seconds=61)
        assert dispatch_due(db, now) == 1
        assert dispatch_due(db, now) == 0
        db.commit()
        config = db.scalar(select(AcquisitionSourceConfiguration))
        occurrence = db.scalar(select(AcquisitionOccurrence))
        acquisition_id = occurrence.acquisition_id
        assert acquisition_id and config.next_run_at
    assert authenticated.post(f"/api/v1/datasets/{first['dataset_id']}/acquisitions/refresh").status_code == 409
    with database() as db:
        run = db.get(AcquisitionRun, acquisition_id)
        job = db.scalar(select(Job).where(Job.acquisition_id == acquisition_id))
        run.status, run.started_at, run.attempt_id = "RUNNING", utcnow(), "expired-attempt"
        job.status, job.lease_owner, job.lease_until = "RUNNING", "dead-worker", utcnow() - timedelta(seconds=1)
        db.commit()
    recovered = completed(authenticated, {"id": acquisition_id})
    assert recovered["status"] == "SUCCESS" and recovered["attempts"] == 1
    with database() as db:
        assert db.scalar(select(func.count()).select_from(DatasetVersion)) == 2


def test_discovery_union_precision_shapes_null_absence_alias_and_empty():
    rows = exact_json(b'[{"id":"000001","mixed":1,"decimal":12.123456789012345678,"date":"2026-10-16","timestamp":"2026-10-16T10:00:00-05:00","flag":true,"null":null,"company":{"name":"Acme"},"array":[1],"a_b":1,"a":{"b":2},"literal.dot":3},{"id":"000002","mixed":"word","later":4,"null":null,"company":{"name":null}}]')
    proposal = infer_records(rows)
    fields = {field["path"]: field for field in proposal["fields"]}
    assert fields["id"]["logical_type"] == "STRING" and fields["id"]["examples"][0] == "000001"
    assert fields["mixed"]["logical_type"] == "STRING" and "MIXED_TYPES_AS_STRING" in fields["mixed"]["warnings"]
    assert fields["decimal"]["logical_type"] == "DECIMAL" and fields["decimal"]["examples"] == ["12.123456789012345678"]
    assert fields["date"]["logical_type"] == "DATE" and fields["timestamp"]["logical_type"] == "TIMESTAMP"
    assert fields["flag"]["logical_type"] == "BOOLEAN"
    assert fields["later"]["missing_count"] == 1 and fields["null"]["null_count"] == 2
    assert fields["null"]["warnings"] == ["PROVISIONAL_ONLY_NULLS"]
    assert fields["array"]["selectable"] is False and fields["array"]["array_count"] == 1
    assert fields["a.b"]["name"] != fields["a_b"]["name"]
    assert "AMBIGUOUS_LITERAL_KEY" in proposal["warnings"]
    assert infer_records([])["fields"] == []


def test_draft_discovery_no_side_effects_multiple_candidates_and_empty(authenticated, database, remote):
    remote.body = b'{"customers":[{"id":"000001"},{"id":"2","later":true}],"orders":[{"amount":12.123456789012345678}]}'
    body = {"draft_id": "draft-generation-1", "name": "Draft", "source_type": "REST", "options": {"url": remote.url, "auth": "NONE"}}
    result = authenticated.post("/api/v1/connections/discover-rest", json=body)
    assert result.status_code == 200, result.text
    assert result.json()["draft_id"] == body["draft_id"] and result.json()["selection_required"] is True
    customers = next(item for item in result.json()["collections"] if item["path"] == "customers")
    assert {item["path"] for item in customers["fields"]} == {"id", "later"}
    with database() as db:
        for model in (Dataset, DatasetSourceBinding, AcquisitionRun, AcquisitionNameReservation, ExternalConnection):
            assert db.scalar(select(func.count()).select_from(model)) == 0
    remote.body = b'{"records":[]}'
    assert authenticated.post("/api/v1/connections/discover-rest", json=body).json()["collections"][0]["fields"] == []


def test_discovery_preserves_json_integer_decimal_scientific_categories_and_precision(authenticated, remote):
    remote.body = b'[{"integer":1,"decimal":1.0,"scientific":1e0,"mixed":1,"large":12345678901234567890123456789012345678,"too_large":123456789012345678901234567890123456789},{"integer":2,"decimal":2.0,"scientific":2e0,"mixed":2.0}]'
    response = authenticated.post("/api/v1/connections/discover-rest", json={"draft_id": "numeric-lexemes", "name": "Numeric draft",
        "source_type": "REST", "options": {"url": remote.url, "auth": "NONE"}})
    assert response.status_code == 200, response.text
    fields = {item["path"]: item for item in response.json()["collections"][0]["fields"]}
    assert fields["integer"]["logical_type"] == "INT64"
    for path in ("decimal", "scientific", "mixed", "large"):
        assert fields[path]["logical_type"] == "DECIMAL", (path, fields[path])
    assert fields["decimal"]["examples"][0] == "1.0"
    assert fields["large"]["examples"][0] == "12345678901234567890123456789012345678"
    assert fields["too_large"]["logical_type"] == "STRING" and fields["too_large"]["warnings"] == ["PRECISION_PRESERVED_AS_STRING"]


def test_new_mapping_separates_missing_null_structure_and_historical_behavior():
    field = {"name": "value", "path": "nested.value", "logical_type": "STRING", "nullable": True, "null_policy": "ERROR", "missing_policy": "NULL"}
    source = RestDatasetSource(SimpleNamespace(options={"fields": [field], "mapping_version": 2}))
    assert source._mapped_cell({}, field) is None
    with pytest.raises(Exception) as error:
        source._mapped_cell({"nested": {"value": None}}, field)
    assert error.value.code == "REST_NULL_NOT_ALLOWED"
    with pytest.raises(Exception) as error:
        source._mapped_cell({"nested": "incompatible"}, field)
    assert error.value.code == "REST_FIELD_STRUCTURE"
    old = RestDatasetSource(SimpleNamespace(options={"fields": [{"name": "value"}]}))
    with pytest.raises(Exception) as error:
        old._mapped_cell({}, {"name": "value"})
    assert error.value.code == "REST_FIELD_MISSING"


def test_retry_timeout_success_exhaustion_total_budget_and_cancellation(monkeypatch):
    timer = SimpleNamespace(now=0.0)
    monkeypatch.setattr(rest_retry, "time", SimpleNamespace(monotonic=lambda: timer.now, sleep=lambda seconds: setattr(timer, "now", timer.now + seconds)))
    calls, reports = [], []
    def send(*args, **kwargs):
        calls.append((args, kwargs))
        if len(calls) < 3:
            raise RestError("REST_TIMEOUT", "individual")
        return 200, b'[]'
    opts = {"retries": 3, "retry_wait_seconds": 10}
    assert rest_retry.read_request(opts, "", deadline=40, transport=send, report=reports.append, query="?page=4") == (200, b'[]')
    assert len(calls) == 3 and all(call[1]["query"] == "?page=4" for call in calls)
    assert reports[1]["cause"] == "REST_REQUEST_TIMEOUT" and timer.now == pytest.approx(20)
    with pytest.raises(RestError) as error:
        rest_retry.read_request(opts, "", deadline=25, transport=lambda *_a, **_k: (503, b''))
    assert error.value.code == "REST_TOTAL_TIMEOUT"
    timer.now = 0
    with pytest.raises(RestError) as error:
        rest_retry.read_request({"retries": 1, "retry_wait_seconds": 1}, "", deadline=20, transport=lambda *_a, **_k: (503, b''))
    assert error.value.code == "REST_RETRIES_EXHAUSTED" and error.value.details["cause"] == "REST_HTTP_TRANSIENT_503"
    cancelled = []
    def check():
        if timer.now >= 1.2:
            raise RuntimeError("cancelled")
    with pytest.raises(RuntimeError, match="cancelled"):
        rest_retry.read_request(opts, "", deadline=20, check=check, transport=lambda *_a, **_k: cancelled.append(1) or (503, b''))
    assert len(cancelled) == 1


def test_retry_wait_checkpoint_crossing_wait_end_preserves_global_budget(monkeypatch):
    timer = SimpleNamespace(now=0.0, waiting=False)
    monkeypatch.setattr(rest_retry, "time", SimpleNamespace(monotonic=lambda: timer.now,
        sleep=lambda seconds: setattr(timer, "now", timer.now + seconds)))
    calls = []

    def checkpoint():
        if timer.waiting:
            # A real DB cancellation/lease checkpoint can cross the local wait
            # boundary while the complete acquisition still has ample time.
            timer.now += 1.1
            timer.waiting = False

    def report(event):
        if event["status"] == "WAITING":
            timer.waiting = True

    def send(*_args, **_kwargs):
        calls.append(timer.now)
        return (503, b'') if len(calls) == 1 else (200, b'[]')

    assert rest_retry.read_request({"retries": 1, "retry_wait_seconds": 1}, "", deadline=30,
        check=checkpoint, report=report, transport=send) == (200, b'[]')
    assert calls == [0.0, 1.1]


def test_retry_defaults_and_limits_remain_product_bounds(monkeypatch):
    monkeypatch.setenv("TRACKVANCE_REST_ALLOWED_INTERNAL_ORIGINS", "http://fixture:80")
    validate_options({"url": "http://fixture", "retries": 5, "retry_wait_seconds": 10}, "", source=True)
    for key, value in (("retries", 6), ("retry_wait_seconds", 0), ("timeout_seconds", 61)):
        with pytest.raises(RestError, match="límites"):
            validate_options({"url": "http://fixture", key: value}, "", source=True)
