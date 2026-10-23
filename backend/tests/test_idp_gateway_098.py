"""Actual local HTTP boundary tests without a GPU, document data, or downloads."""
import hashlib
import http.client
import importlib.util
import json
import threading
from pathlib import Path
from unittest.mock import patch

import pytest

HTTPConnection = http.client.HTTPConnection


@pytest.fixture
def gateway():
    root = Path(__file__).resolve().parents[2]
    original_read = Path.read_bytes
    lock = (root / "deploy/idp/models.lock.json").read_bytes()
    spec = importlib.util.spec_from_file_location("idp_gateway_test", root / "deploy/idp/server.py")
    module = importlib.util.module_from_spec(spec)
    with patch.object(Path, "read_bytes", lambda path: lock if path.as_posix() == "/app/models.lock.json" else original_read(path)):
        spec.loader.exec_module(module)
    return module


@pytest.fixture
def endpoint(gateway):
    server = gateway.ThreadingHTTPServer(("127.0.0.1", 0), gateway.Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield server.server_address
    server.shutdown()
    server.server_close()
    thread.join(timeout=2)


def post(endpoint, path, body=None, length=None):
    connection = HTTPConnection(*endpoint, timeout=2)
    raw = json.dumps(body).encode() if body is not None else None
    connection.request("POST", path, body=raw, headers={"Content-Length": str(length if length is not None else len(raw or b""))})
    response = connection.getresponse()
    result = response.status, json.loads(response.read())
    connection.close()
    return result


def test_unknown_paths_and_control_bodies_rejected_before_read(endpoint):
    assert post(endpoint, "/untrusted", length=100_000_000)[0] == 404
    assert post(endpoint, "/cancel", length=1025)[0] == 422


def test_full_queue_does_not_buffer_request_body(gateway, endpoint):
    for _ in range(4):
        assert gateway.REQUEST_SLOTS.acquire(blocking=False)
    try:
        assert post(endpoint, "/extract", length=100_000_000) == (503, {"error": "GPU_QUEUE_FULL"})
    finally:
        for _ in range(4):
            gateway.REQUEST_SLOTS.release()


@pytest.mark.parametrize("limit,active,expected", [(1, 1, 503), (4, 4, 503), (4, 3, 200)])
def test_configured_admission_limits_active_and_queued_requests(gateway, endpoint, monkeypatch, limit, active, expected):
    gateway.ADMISSIONS.update({str(index): limit for index in range(active)})
    monkeypatch.setattr(gateway.Handler, "extract", lambda self, body: {"engine": {"elapsed_seconds": 0}})
    code, _ = post(endpoint, "/extract", {"request_id": "c" * 32, "configuration": {"max_parallel_jobs": limit}})
    assert code == expected
    assert len(gateway.ADMISSIONS) == active


@pytest.mark.parametrize("limit", [0, 5, True, "4", None])
def test_untrusted_admission_limit_cannot_expand_hard_capacity(gateway, endpoint, limit):
    code, _ = post(endpoint, "/extract", {"request_id": "d" * 32, "configuration": {"max_parallel_jobs": limit}})
    assert code == 422
    assert not gateway.ADMISSIONS


def test_health_requires_exact_model_tag_and_digest(gateway, monkeypatch):
    expected = gateway.LOCK["qwen"]
    monkeypatch.setattr(gateway, "ollama", lambda _: {"models": [
        {"name": "other:tag", "digest": expected["manifest_sha256"]},
        {"name": expected["model"], "digest": "other-digest"}]})
    assert not gateway.qwen_verified()


def test_modified_host_model_invalidates_cached_hash(gateway, tmp_path, monkeypatch):
    folder = tmp_path / "paddle"
    folder.mkdir()
    model = folder / "weights.bin"
    model.write_bytes(b"abc")
    gateway.LOCK["paddle"]["files"] = {"weights.bin": hashlib.sha256(b"abc").hexdigest()}
    gateway.LOCK["layout"]["files"] = {}
    monkeypatch.setattr(gateway, "Path", lambda value: tmp_path if value == "/models" else Path(value))
    assert gateway.verify_models()
    model.write_bytes(b"changed")
    assert not gateway.verify_models()


def test_cancel_closes_upstream_before_response_headers(gateway, endpoint, monkeypatch):
    waiting, cancelled = threading.Event(), threading.Event()

    class Socket:
        def shutdown(self, _how):
            cancelled.set()

    class Upstream:
        def __init__(self, *args, **kwargs):
            self.sock = Socket()

        def request(self, *args, **kwargs):
            pass

        def getresponse(self):
            waiting.set()
            assert cancelled.wait(timeout=2)
            raise OSError("upstream cancelled")

        def close(self):
            pass

    monkeypatch.setattr(gateway.http.client, "HTTPConnection", Upstream)
    monkeypatch.setattr(gateway, "qwen_verified", lambda: True)
    observations = [{"text": "Invoice A-1", "page": 1, "region": [0, 0, 1, 1]}]
    identity = "a" * 32
    body = {"request_id": identity, "source_sha256": "b" * 64, "observations": observations,
        "observations_sha256": hashlib.sha256(json.dumps(observations, sort_keys=True, ensure_ascii=False).encode()).hexdigest(),
        "configuration": {"llm_model": gateway.LOCK["qwen"]["model"], "prompt_version": "trackvance-idp-098-v1"},
        "definition": {}, "semantic_schema": {}}
    original_read = Path.read_text
    monkeypatch.setattr(Path, "read_text", lambda path, *args, **kwargs: "synthetic prompt" if path.as_posix() == "/app/prompt-v1.txt" else original_read(path, *args, **kwargs))
    result = []
    thread = threading.Thread(target=lambda: result.append(post(endpoint, "/interpret", body)))
    thread.start()
    assert waiting.wait(timeout=2)
    assert post(endpoint, "/cancel", {"request_id": identity}) == (200, {"cancelled": True})
    thread.join(timeout=2)
    assert not thread.is_alive()
    assert result[0][0] == 422
    assert not gateway.GATE.locked()
    assert not gateway.UPSTREAM


def test_ingress_has_fixed_upstream_and_rejects_unknown_routes(monkeypatch):
    root = Path(__file__).resolve().parents[2]
    spec = importlib.util.spec_from_file_location("idp_ingress_test", root / "deploy/idp/ingress.py")
    relay = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(relay)
    calls = []

    class Response:
        status = 200

        def getheader(self, *args):
            return "2"

        def read(self, _size):
            return b"{}"

    class FixedUpstream:
        def __init__(self, host, port, **kwargs):
            calls.append((host, port))

        def putrequest(self, method, path):
            calls.append((method, path))

        def putheader(self, *args):
            pass

        def endheaders(self):
            pass

        def send(self, data):
            pass

        def getresponse(self):
            return Response()

        def close(self):
            pass

    monkeypatch.setattr(relay.http.client, "HTTPConnection", FixedUpstream)
    server = relay.ThreadingHTTPServer(("127.0.0.1", 0), relay.Relay)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        assert post(server.server_address, "/interpret", {"url": "http://169.254.169.254"}) == (200, {})
        assert calls == [("idp-gateway", 8088), ("POST", "/interpret")]
        assert post(server.server_address, "/http://169.254.169.254", length=100_000_000)[0] == 404
        assert post(server.server_address, "/cancel", length=1025)[0] == 503
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
