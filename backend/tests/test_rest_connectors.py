"""Small real HTTP cycle: exact acquisition and durable per-batch delivery."""

import hashlib
import json
import threading
import time
from datetime import timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit
from urllib.request import urlopen

import pytest
from sqlalchemy import select

from trackvance.db import SessionLocal, utcnow
from trackvance.models import DatasetVersion, DeliveryAttempt, Job, Run
from trackvance.rest_models import RestDeliveryBatch
from trackvance.rest_transport import RestError, endpoint, exact_json, request
from trackvance.worker import process_once


@pytest.fixture
def receiver(monkeypatch):
    state = SimpleNamespace(posts=[], headers=[], mode="CONFIRMED", fail_page=None)
    records = [{"id": "0001", "amount": "12.2300", "occurred": "2026-10-01T08:00:00-05:00"},
               {"id": "0002", "amount": "0.0001", "occurred": "2026-10-02T08:00:00-05:00"},
               {"id": "0003", "amount": "999.9900", "occurred": "2026-10-03T08:00:00-05:00"}]
    for ordinal, record in enumerate(records, 1):
        record["sequence_id"] = ordinal

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def send(self, status, value):
            body = json.dumps(value, separators=(",", ":")).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_HEAD(self):
            self.send_response(200)
            self.end_headers()

        def do_GET(self):
            if urlsplit(self.path).path in {"/drip-body", "/drip-headers"}:
                try:
                    if urlsplit(self.path).path == "/drip-body":
                        self.send_response(200)
                        self.send_header("Content-Length", "1000")
                        self.end_headers()
                        stream = b"x" * 1000
                    else:
                        stream = b"HTTP/1.1 200 OK\r\nContent-Length: 0\r\n\r\n"
                    for byte in stream:
                        self.wfile.write(bytes([byte]))
                        self.wfile.flush()
                        time.sleep(0.04)
                except (BrokenPipeError, ConnectionResetError):
                    pass
                return
            query = parse_qs(urlsplit(self.path).query)
            page = int(query.get("page", [1])[0])
            size = int(query.get("page_size", query.get("limit", [2]))[0])
            start = int(query.get("offset", query.get("cursor", [(page - 1) * size]))[0])
            if urlsplit(self.path).path == "/capped-offset":
                size = min(size, 1)
            if page == state.fail_page:
                self.send(503, {"error": "synthetic"})
            elif urlsplit(self.path).path == "/redirect":
                self.send_response(302)
                self.send_header("Location", "/source")
                self.end_headers()
            else:
                state.headers.append(dict(self.headers))
                self.send(200, {"records": records[start:start + size], "next_cursor": str(start + size) if start + size < len(records) else None})

        def do_POST(self):
            body = self.rfile.read(int(self.headers["Content-Length"]))
            value = json.loads(body)
            state.posts.append((value, body))
            state.headers.append(dict(self.headers))
            status, accepted, count = 200, True, len(value["records"])
            if state.mode == "PENDING":
                status = 202
            if state.mode == "REJECT_SECOND" and len(state.posts) == 2:
                status, accepted, count = 422, False, 0
            self.send(status, {"accepted": accepted, "rows": count, "batch_id": value["batch_id"],
                               "payload_sha256": hashlib.sha256(body).hexdigest()})

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    state.url = f"http://127.0.0.1:{server.server_port}"
    monkeypatch.setenv("TRACKVANCE_REST_ALLOWED_INTERNAL_ORIGINS", state.url)
    yield state
    server.shutdown()
    server.server_close()
    thread.join(timeout=2)


def acquire(client, receiver):
    response = client.post("/api/v1/connections", json={"name": "REST source", "source_type": "REST", "password": "source-private-key",
        "options": {"url": receiver.url + "/source", "auth": "API_KEY", "api_key_header": "X-Read-Key",
            "collection": "records", "fields": [{"name": "id", "logical_type": "STRING", "nullable": False},
                {"name": "amount", "logical_type": "DECIMAL", "nullable": False},
                {"name": "occurred", "logical_type": "TIMESTAMP", "nullable": False}],
            "pagination": {"mode": "PAGE"}, "page_size": 2, "max_pages": 10}})
    assert response.status_code == 201, response.text
    assert "source-private-key" not in response.text
    connection = response.json()
    response = client.post(f"/api/v1/connections/{connection['id']}/acquisitions", headers={"Idempotency-Key": "source-registration"},
        json={"name": "Exact REST orders", "schema_name": "REST", "object_name": "collection"})
    assert response.status_code == 202, response.text
    acquisition = response.json()["acquisition"]
    from trackvance.acquisition_worker import process_once as acquire_once
    assert acquire_once()
    response = client.get(f"/api/v1/acquisitions/{acquisition['id']}")
    assert response.status_code == 200
    return connection, response.json()


def configure_delivery(client, receiver, acquisition, *, batch_rows=2):
    result = client.post("/api/v1/delivery/destinations", json={"name": "REST destination", "sink_type": "REST",
        "password": "destination-private-key", "options": {"url": receiver.url + "/write", "auth": "BEARER", "idempotency_supported": True}})
    assert result.status_code == 201, result.text
    assert "destination-private-key" not in result.text
    destination = result.json()
    draft = {"dataset_version_id": acquisition["output_version_id"], "destination_id": destination["id"],
        "destination_version_id": destination["destination_version_id"], "target": {"mode": "REST", "batch_rows": batch_rows,
            "confirmation_path": "accepted", "confirmation_value": True, "rows_path": "rows", "batch_id_path": "batch_id", "payload_hash_path": "payload_sha256"},
        "columns": [{"source_name": "id", "target_name": "id", "target_type": "STRING", "ordinal": 0, "nullable": False},
            {"source_name": "amount", "target_name": "amount", "target_type": "DECIMAL", "precision": 38, "scale": 18, "ordinal": 1, "nullable": False},
            {"source_name": "occurred", "target_name": "occurred", "target_type": "TIMESTAMP", "ordinal": 2, "nullable": False}]}
    assert client.post("/api/v1/delivery/preview", json=draft).status_code == 200
    result = client.post("/api/v1/delivery/preflight", json=draft)
    assert result.status_code == 200, result.text
    assert receiver.posts == []
    result = client.post("/api/v1/delivery/configurations", json={"name": "REST orders delivery", **draft})
    assert result.status_code == 201, result.text
    body = {"configuration_id": result.json()["id"], "dataset_version_id": acquisition["output_version_id"]}
    return body


def delivery(client, receiver, acquisition, *, batch_rows=2):
    body = configure_delivery(client, receiver, acquisition, batch_rows=batch_rows)
    result = client.post("/api/v1/delivery/runs", headers={"Idempotency-Key": "delivery-stable"}, json=body)
    assert result.status_code == 202, result.text
    assert process_once(lane="DELIVERY")
    return result.json()["id"], body


@pytest.mark.parametrize("connector", ["connections", "delivery/destinations"])
@pytest.mark.parametrize("kind", ["POSTGRESQL", "SQLSERVER"])
@pytest.mark.parametrize("missing", ["host", "port", "database", "username"])
def test_sql_endpoint_original_required_fields_never_default_to_rest(authenticated, monkeypatch, connector, kind, missing):
    from trackvance.data_sinks import sink_registry
    from trackvance.dataset_sources import source_registry

    def forbidden_connection(*_args, **_kwargs):
        raise AssertionError("An incomplete SQL endpoint must fail before any connection attempt.")

    monkeypatch.setattr(source_registry, "create", forbidden_connection)
    monkeypatch.setattr(sink_registry, "create", forbidden_connection)
    body = {"name": "Incomplete SQL endpoint", "source_type" if connector == "connections" else "sink_type": kind,
            "host": "database.example.invalid", "port": 5432 if kind == "POSTGRESQL" else 1433,
            "database": "synthetic", "username": "synthetic", "password": "synthetic-only-password"}
    body.pop(missing)
    for suffix in ("", "/test"):
        response = authenticated.post(f"/api/v1/{connector}{suffix}", json=body)
        assert response.status_code == 422, response.text
        assert response.json()["error"]["code"] == "VALIDATION_ERROR"


def test_rest_none_endpoints_create_and_test_without_sql_fields_or_password(authenticated, receiver):
    # NONE takes the one declared collection response. This fixture's endpoint
    # returns two records when pagination parameters are absent.
    with urlopen(receiver.url + "/source", timeout=5) as response:
        expected_rows = [{"id": row["id"]} for row in json.load(response)["records"]]
    options = {"url": receiver.url + "/source", "auth": "NONE", "collection": "records",
               "fields": [{"name": "id", "logical_type": "STRING", "nullable": False}]}
    for connector, type_field, url in (("connections", "source_type", "/source"),
                                        ("delivery/destinations", "sink_type", "/write")):
        body = {"name": "REST without SQL credentials", type_field: "REST", "options": {**options, "url": receiver.url + url}}
        if connector != "connections":
            body["options"] = {"url": receiver.url + url, "auth": "NONE"}
        assert authenticated.post(f"/api/v1/{connector}/test", json=body).status_code == 200
        created = authenticated.post(f"/api/v1/{connector}", json=body)
        assert created.status_code == 201, created.text
        assert "password" not in created.json()
        if connector == "connections":
            acquired = authenticated.post(f"/api/v1/connections/{created.json()['id']}/acquisitions",
                                          json={"name": "REST default collection selection"})
            assert acquired.status_code == 202, acquired.text
            from trackvance.acquisition_worker import process_once as acquire_once
            assert acquire_once()
            result = authenticated.get(f"/api/v1/acquisitions/{acquired.json()['acquisition']['id']}").json()
            assert result["status"] == "SUCCESS" and result["published_rows"] == len(expected_rows)
            snapshot = authenticated.get(f"/api/v1/dataset-versions/{result['output_version_id']}/profile").json()
            assert snapshot["row_count"] == len(expected_rows) and snapshot["sample"] == expected_rows
    assert not receiver.posts


def test_source_sql_password_remains_optional_in_request_contract(authenticated, monkeypatch):
    from trackvance.dataset_sources import source_registry

    def forbidden_connection(*_args, **_kwargs):
        raise AssertionError("A missing SQL password must fail before any connection attempt.")

    monkeypatch.setattr(source_registry, "create", forbidden_connection)
    body = {"name": "SQL optional request password", "source_type": "POSTGRESQL", "host": "database.example.invalid",
            "port": 5432, "database": "synthetic", "username": "synthetic"}
    for suffix in ("", "/test"):
        response = authenticated.post(f"/api/v1/connections{suffix}", json=body)
        assert response.status_code == 422, response.text
        assert response.json()["error"]["code"] == "PASSWORD_REQUIRED"


def test_real_rest_acquisition_delivery_exact_complete(authenticated, receiver):
    _, acquisition = acquire(authenticated, receiver)
    assert acquisition["status"] == "SUCCESS", acquisition
    assert acquisition["published_rows"] == 3
    run_id, body = delivery(authenticated, receiver, acquisition)
    with SessionLocal() as db:
        run = db.get(Run, run_id)
        assert (run.status, run.decision) == ("SUCCESS", "COMMITTED"), run.error
        assert run.metrics["rows_written"] == 3
        batches = db.scalars(select(RestDeliveryBatch).where(RestDeliveryBatch.run_id == run_id).order_by(RestDeliveryBatch.ordinal)).all()
        assert [item.status for item in batches] == ["COMMITTED", "COMMITTED"]
        assert all(item.attempts == 1 for item in batches)
    received = [row for value, _ in receiver.posts for row in value["records"]]
    assert [item["id"] for item in received] == ["0001", "0002", "0003"]
    assert [item["amount"] for item in received] == ["12.2300", "0.0001", "999.9900"]
    assert received[0]["occurred"] == "2026-10-01T08:00:00-05:00"
    assert receiver.headers[0].get("X-Read-Key") == "source-private-key"
    write_headers = [item for item in receiver.headers if "Authorization" in item]
    assert all(item["Authorization"] == "Bearer destination-private-key" for item in write_headers)
    assert all("X-Read-Key" not in item for item in write_headers)
    result = authenticated.post("/api/v1/delivery/runs", headers={"Idempotency-Key": "delivery-stable"}, json=body)
    assert result.json()["id"] == run_id
    assert len(receiver.posts) == 2
    receipt = authenticated.get(f"/api/v1/delivery/runs/{run_id}/receipt")
    assert receipt.status_code == 200, receipt.text
    assert len(receipt.json()["rest_batches"]) == 2


@pytest.mark.parametrize(("mode", "expected", "posts"), [("PENDING", "UNKNOWN", 1), ("REJECT_SECOND", "PARTIAL", 2)])
def test_pending_and_partial_never_replay(authenticated, receiver, mode, expected, posts):
    _, acquisition = acquire(authenticated, receiver)
    receiver.mode = mode
    run_id, _ = delivery(authenticated, receiver, acquisition)
    with SessionLocal() as db:
        run = db.get(Run, run_id)
        assert run.status == expected, run.error
        assert run.metrics["rows_written"] == (2 if expected == "PARTIAL" else 0)
        from trackvance.delivery_service import execute_delivery_run
        execute_delivery_run(db, run)
    assert len(receiver.posts) == posts


def test_failed_page_never_publishes_partial(authenticated, receiver):
    receiver.fail_page = 2
    _, acquisition = acquire(authenticated, receiver)
    assert acquisition["status"] == "FAILED"
    assert acquisition["output_version_id"] is None
    with SessionLocal() as db:
        assert db.scalar(select(DatasetVersion).where(DatasetVersion.dataset_id == acquisition["dataset_id"])) is None


def test_security_blocks_cleartext_internal_and_redirect(receiver, monkeypatch):
    with pytest.raises(RestError, match="redirecciones"):
        request({"url": receiver.url + "/redirect"}, "", "GET")
    monkeypatch.delenv("TRACKVANCE_REST_ALLOWED_INTERNAL_ORIGINS")
    with pytest.raises(RestError) as error:
        endpoint(receiver.url + "/source")
    assert error.value.code == "REST_TLS_REQUIRED"
    for invalid in ("https://user:secret@example.test/path", "https://example.test/path?token=secret"):
        with pytest.raises(RestError):
            endpoint(invalid)
    with pytest.raises(RestError):
        exact_json(b'{"records":[],"records":[]}')


def test_only_exact_codex_scope_allows_reserved_synthetic_fixture_origin(monkeypatch):
    monkeypatch.delenv("TRACKVANCE_REST_ALLOWED_INTERNAL_ORIGINS", raising=False)
    monkeypatch.setenv("TRACKVANCE_CERTIFICATION_PROJECT", "trackvance-codex")
    assert endpoint("http://rest-fixture:18090/source")[2] is True
    for url in ("http://rest-fixture:18091/source", "http://another-service:18090/source", "http://127.0.0.1:18090/source"):
        with pytest.raises(RestError):
            endpoint(url)
    for project in ("trackvance-certification", "bikerwash", "trackvance-codex-copy", ""):
        monkeypatch.setenv("TRACKVANCE_CERTIFICATION_PROJECT", project)
        with pytest.raises(RestError):
            endpoint("http://rest-fixture:18090/source")


@pytest.mark.parametrize("path", ["/drip-body", "/drip-headers"])
def test_rest_deadline_bounds_slow_drip_body_and_headers(receiver, path):
    started = time.monotonic()
    with pytest.raises(RestError):
        request({"url": receiver.url + path, "timeout_seconds": 1}, "", "GET")
    assert time.monotonic() - started < 3


def test_rest_batch_evidence_is_paginated_without_duplicate_or_lost_identities(authenticated, receiver):
    _, acquisition = acquire(authenticated, receiver)
    run_id, _ = delivery(authenticated, receiver, acquisition, batch_rows=1)
    first_response = authenticated.get(f"/api/v1/delivery/runs/{run_id}/rest-batches?limit=2")
    second_response = authenticated.get(f"/api/v1/delivery/runs/{run_id}/rest-batches?limit=2&offset=2")
    assert first_response.status_code == second_response.status_code == 200, (first_response.text, second_response.text)
    first, second = first_response.json(), second_response.json()
    assert first["total"] == second["total"] == 3
    assert [item["ordinal"] for item in first["items"]] == [0, 1]
    assert [item["ordinal"] for item in second["items"]] == [2]
    assert len({item["id"] for item in first["items"] + second["items"]}) == 3


def test_rest_payload_splits_before_accumulating_above_byte_budget():
    from trackvance.delivery_schemas import DeliveryDraft
    from trackvance.rest_delivery import _payloads
    draft = DeliveryDraft.model_validate({"dataset_version_id": "version", "destination_id": "destination", "destination_version_id": "revision",
        "target": {"mode": "REST", "batch_rows": 10000, "max_payload_bytes": 1024, "confirmation_path": "accepted", "confirmation_value": True,
                   "rows_path": "rows", "batch_id_path": "batch_id"},
        "columns": [{"source_name": "id", "target_name": "id", "target_type": "STRING", "ordinal": 0, "nullable": False}]})
    rows = [{"id": "x" * 400} for _ in range(10)]
    payloads = list(_payloads(rows, draft, "bounded"))
    assert len(payloads) == 5
    assert sum(len(records) for _, _, records, _ in payloads) == 10
    assert all(len(body) <= 1024 for _, _, _, body in payloads)


@pytest.mark.parametrize(("mode", "path"), [("PAGE", "/source"), ("OFFSET", "/capped-offset"), ("CURSOR", "/source")])
def test_rest_all_pagination_modes_preserve_population_when_server_caps_size(receiver, mode, path):
    from trackvance.dataset_sources import ConnectionSettings, source_registry
    options = {"url": receiver.url + path, "collection": "records", "page_size": 2,
        "fields": [{"name": "id", "logical_type": "STRING", "nullable": False}],
        "pagination": {"mode": mode, **({"next_cursor_path": "next_cursor"} if mode == "CURSOR" else {})}}
    settings = ConnectionSettings("REST", "REST", 443, "REST", "REST", "REST_NO_AUTH", options)
    source = source_registry.create(settings, "REST", "collection")
    result = source.read()
    assert result.frame["id"].to_list() == ["0001", "0002", "0003"]


def test_rest_manual_cancellation_before_first_post_remains_cancelled(authenticated, receiver, monkeypatch):
    from trackvance import delivery_service
    original = delivery_service.delivery_control
    cancelled = []
    def control(run_id, lease_owner):
        checkpoint = original(run_id, lease_owner)
        def check():
            with SessionLocal() as db:
                started = db.scalar(select(DeliveryAttempt.id).where(DeliveryAttempt.run_id == run_id))
            if started and not cancelled:
                response = authenticated.post(f"/api/v1/runs/{run_id}/cancel")
                assert response.status_code == 200, response.text
                cancelled.append(run_id)
            checkpoint()
        return check
    monkeypatch.setattr(delivery_service, "delivery_control", control)
    _, acquisition = acquire(authenticated, receiver)
    run_id, _ = delivery(authenticated, receiver, acquisition)
    with SessionLocal() as db:
        run = db.get(Run, run_id)
        assert run.status == "CANCELLED", run.error
        assert run.metrics["rows_written"] == 0
    assert cancelled == [run_id]
    assert receiver.posts == []


def test_rest_operational_authority_identity_canonicalizes_aliases_without_rewriting_config():
    from trackvance.delivery_audit import target_identity
    aliases = ["https://Receiver.EXAMPLE/write", "https://receiver.example:443/write", "https://receiver.example.:443/write"]
    fingerprints = [target_identity("organization", "REST", {"options": {"url": url}}, {"mode": "REST"})[0] for url in aliases]
    assert len(set(fingerprints)) == 1
    assert target_identity("other-organization", "REST", {"options": {"url": aliases[0]}}, {"mode": "REST"})[0] != fingerprints[0]
    assert target_identity("organization", "REST", {"options": {"url": "https://receiver.example/other"}}, {"mode": "REST"})[0] != fingerprints[0]
    assert aliases[0] == "https://Receiver.EXAMPLE/write"


def test_rest_recovery_of_confirmed_batches_never_replays_http(authenticated, receiver):
    _, acquisition = acquire(authenticated, receiver)
    run_id, _ = delivery(authenticated, receiver, acquisition)
    with SessionLocal() as db:
        run = db.get(Run, run_id)
        attempt = db.scalar(select(DeliveryAttempt).where(DeliveryAttempt.run_id == run_id))
        job = db.scalar(select(Job).where(Job.run_id == run_id))
        run.status, run.decision = "RUNNING", None
        attempt.status = "STARTED"
        job.status, job.lease_owner, job.lease_until = "RUNNING", "lost-worker", utcnow() - timedelta(seconds=1)
        db.commit()
    assert process_once(lane="DELIVERY")
    with SessionLocal() as db:
        run = db.get(Run, run_id)
        attempt = db.scalar(select(DeliveryAttempt).where(DeliveryAttempt.run_id == run_id))
        assert run.status == "SUCCESS" and attempt.status == "COMMITTED"
        assert run.metrics["rows_written"] == 3
    assert len(receiver.posts) == 2


def test_rest_explicit_identifier_numeric_type_survives_complete_acquisition(authenticated, receiver):
    response = authenticated.post("/api/v1/connections", json={"name": "Typed REST identifier", "source_type": "REST",
        "options": {"url": receiver.url + "/source", "collection": "records", "pagination": {"mode": "PAGE"}, "page_size": 2,
            "fields": [{"name": "sequence_id", "logical_type": "INT64", "nullable": False}]}})
    assert response.status_code == 201, response.text
    response = authenticated.post(f"/api/v1/connections/{response.json()['id']}/acquisitions", json={"name": "Declared numeric identifier"})
    assert response.status_code == 202, response.text
    from trackvance.acquisition_worker import process_once as acquire_once
    assert acquire_once()
    result = authenticated.get(f"/api/v1/acquisitions/{response.json()['acquisition']['id']}").json()
    assert result["status"] == "SUCCESS", result
    assert result["column_overrides"]["sequence_id"]["logical_type"] == "INT64"
    with SessionLocal() as db:
        version = db.get(DatasetVersion, result["output_version_id"])
        assert version.schema_json[0]["logical_type"] == "INT64"


def test_functional_real_rest_automatic_sentinel_intake_and_chained_delivery(authenticated, receiver):
    """Discovered by functional backend pytest; all processing and outbox are real."""
    from trackvance import events
    from trackvance.acquisition_worker import process_once as acquire_once
    from trackvance.automation_models import OutboxEvent

    _connection, bootstrap = acquire(authenticated, receiver)
    contract = authenticated.post("/api/v1/intake/contracts", json={"name": "REST automatic pipeline", "dataset_id": bootstrap["dataset_id"],
        "config": {"required_columns": ["id", "amount", "occurred"], "max_error_rate": 0}})
    assert contract.status_code == 201, contract.text
    contract_id = contract.json()["id"]
    actor_id = authenticated.get("/api/v1/me").json()["user"]["id"]
    starts = (utcnow() - timedelta(seconds=1)).isoformat()
    schedule = authenticated.post(f"/api/v1/intake/contracts/{contract_id}/schedule", json={"enabled": True,
        "responsible_user_id": actor_id, "settings": {"mode": "EVENT", "timezone": "America/Bogota", "starts_at": starts}})
    assert schedule.status_code == 200, schedule.text
    template = configure_delivery(authenticated, receiver, bootstrap)
    chained = authenticated.post("/api/v1/delivery/automations", json={"name": "Automatic REST publication",
        "configuration_id": template["configuration_id"], "settings": {"mode": "CHAINED", "timezone": "America/Bogota",
            "starts_at": starts, "source_policy": "INTAKE_OUTPUT", "intake_configuration_id": contract_id}})
    assert chained.status_code == 201, chained.text
    refresh = authenticated.post(f"/api/v1/datasets/{bootstrap['dataset_id']}/acquisitions/refresh", headers={"Idempotency-Key": "pipeline-source-v2"})
    assert refresh.status_code == 202, refresh.text
    assert acquire_once()
    full = authenticated.get(f"/api/v1/acquisitions/{refresh.json()['id']}").json()
    assert full["status"] == "SUCCESS" and full["published_rows"] == 3
    with SessionLocal() as db:
        publication = db.scalar(select(OutboxEvent).where(OutboxEvent.event_type == "DATASET_VERSION_PUBLISHED",
            OutboxEvent.aggregate_id == full["output_version_id"]))
        assert publication is not None and publication.payload["responsible_user_id"] == actor_id
    # These are the exact production consumer and queue dispatchers. Only this
    # test's disposable DB exists; no other Codex or installed jobs are touched.
    for _ in range(100):
        progressed = events.consume_once("CHAINING")
        progressed = process_once(lane="DEFAULT") or progressed
        progressed = process_once(lane="DELIVERY") or progressed
        if not progressed:
            break
    else:
        pytest.fail("La pipeline real no terminó dentro del límite finito de despacho.")
    with SessionLocal() as db:
        intake = db.scalar(select(Run).where(Run.config_id == contract_id, Run.dataset_version_id == full["output_version_id"]))
        assert intake is not None and intake.status == "SUCCESS" and intake.decision == "APPROVED", intake.error if intake else "missing"
        output_id = intake.output_version_id
        assert output_id and db.get(DatasetVersion, output_id).row_count == 3
        monitors = db.scalars(select(Run).where(Run.module == "sentinel", Run.dataset_version_id == full["output_version_id"])).all()
        assert len(monitors) == 1 and monitors[0].status == "SUCCESS"
        monitor_id = monitors[0].id
        delivered = db.scalar(select(Run).where(Run.module == "DELIVERY", Run.dataset_version_id == output_id))
        assert delivered is not None and delivered.status == "SUCCESS" and delivered.decision == "COMMITTED", delivered.error if delivered else "missing"
        assert delivered.metrics["rows_written"] == 3
        assert delivered.execution_plan["automation"]["source_run_id"] == intake.id
    visual = authenticated.get(f"/api/v1/runs/{monitor_id}/visual-summary")
    assert visual.status_code == 200, visual.text
    assert visual.json()["dataset_version_id"] == full["output_version_id"]
    assert visual.json()["indicators"]["row_count"] == 3 and visual.json()["scans"] == 0
    received = [row for payload, _ in receiver.posts for row in payload["records"]]
    assert len(receiver.posts) == 2
    assert [row["id"] for row in received] == ["0001", "0002", "0003"]
    assert [row["amount"] for row in received] == ["12.2300", "0.0001", "999.9900"]
    assert len({row["id"] for row in received}) == 3
