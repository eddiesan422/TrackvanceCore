"""CPU-only injected Paddle failures; no model, source corpus or GPU calls."""
import base64
import hashlib
import http.client
import importlib.util
import json
import math
import os
import shutil
import tempfile
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[2]
SOURCE = hashlib.sha256(b"synthetic source").hexdigest()
HASHES = {"paddle_runner.py": "a" * 64, "pipeline.yml": "b" * 64}
SPEC = importlib.util.spec_from_file_location("paddle_safety_observability", ROOT / "deploy/idp/idp_observability.py")
obs = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(obs)


def test_structured_request_retention_accepts_only_declared_contracts():
    legacy = {"filename": "document.pdf", "source_sha256": SOURCE, "regions": []}
    structured = {"filename": "document.pdf", "source_sha256": SOURCE,
        "operation": "STRUCTURED", "remaining_work_seconds": 179.5}
    assert obs.valid_child_request(legacy, SOURCE)
    assert obs.valid_child_request(structured, SOURCE)
    assert obs.valid_child_request({**structured, "operation": "RENDER"}, SOURCE)
    for invalid in ({**structured, "operation": "EXEC"}, {**structured, "operation": []}, {**structured, "extra": 1},
            {**structured, "filename": "../document.pdf"}, {**structured, "source_sha256": "b" * 64},
            {**structured, "filename": []}, {**legacy, "regions": None}, {**legacy, "regions": [1] * 5}):
        assert not obs.valid_child_request(invalid, SOURCE)
    for invalid_duration in (True, None, "10", 0, -1, 1801, math.nan, math.inf):
        assert not obs.valid_child_request({**structured, "remaining_work_seconds": invalid_duration}, SOURCE)


def test_admission_does_not_relabel_opaque_http_failures_as_infrastructure():
    guard = obs.PaddleAdmission()
    for ordinal in range(3):
        state = guard.observe("PADDLE_FAILURE", resources_released=True)
        assert state["last_category"] == "UNKNOWN"
        assert state["consecutive_failures"] == ordinal + 1
    assert state["status"] == "SUSPENDED"
    assert state["reason"] == "THREE_UNRECOVERED_PADDLE_FAILURES_CAUSE_UNPROVEN"
    assert guard.observe(None, resources_released=True) == state


def test_cancellation_does_not_refund_failure_and_real_success_recovers():
    guard = obs.PaddleAdmission()
    guard.observe("INFERENCE_TIMEOUT", resources_released=True)
    state = guard.observe("CANCELLED", resources_released=True)
    assert state["consecutive_failures"] == 1
    assert state["last_category"] == "CANCELLATION"
    assert guard.observe(None, resources_released=True)["consecutive_failures"] == 0
    assert guard.observe("INFERENCE_FAILED", resources_released=True)["consecutive_failures"] == 1
    guard.observe("INFERENCE_TIMEOUT", resources_released=True)
    assert guard.observe("PADDLE_FAILURE", resources_released=True)["status"] == "SUSPENDED"


@pytest.mark.parametrize("closure", [False, None, 1, "true"])
def test_uncertain_closure_blocks_even_reported_success(closure):
    guard = obs.PaddleAdmission()
    state = guard.observe(None, resources_released=closure)
    assert state["reason"] == "GPU_CLOSURE_UNCONFIRMED"
    assert guard.observe(None, resources_released=True) == state


@pytest.mark.skipif(os.name != "posix", reason="Linux owned-directory contract")
@pytest.mark.parametrize("operation", ["STRUCTURED", "RENDER"])
def test_private_retention_preserves_returncode_stderr_and_original_exception(operation, tmp_path):
    folder = Path(tempfile.mkdtemp(prefix="idp-recognition-", dir=tmp_path))
    (folder / "request.json").write_text(json.dumps({"filename": "document.pdf", "source_sha256": SOURCE,
        "operation": operation, "remaining_work_seconds": 180}))
    stderr = b"SYNTHETIC_PRIVATE_CHILD_EXCEPTION\n"
    (folder / "error.log").write_bytes(stderr)
    try:
        raise RuntimeError("SYNTHETIC_PRIVATE_CAUSE")
    except RuntimeError as cause:
        try:
            raise TimeoutError("INFERENCE_TIMEOUT") from cause
        except TimeoutError as failure:
            ring = obs.FailureRing(HASHES, base=tmp_path)
            try:
                token = ring.retain(folder, obs.identity(folder.lstat()), "c" * 32, SOURCE,
                    "INFERENCE_TIMEOUT", SimpleNamespace(poll=lambda: -9), error=failure)
                assert token is not None
                metadata = json.loads((ring.root / (token + ".json")).read_bytes())
                assert metadata["version"] == 2
                assert metadata["child_returncode"] == -9
                assert metadata["child_termination_signal"] == 9
                assert metadata["child_exit_status"] is None
                assert metadata["request_contract"] == operation
                trace = (ring.root / (token + ".parenttrace")).read_bytes()
                assert b"SYNTHETIC_PRIVATE_CAUSE" in trace and b"INFERENCE_TIMEOUT" in trace
                assert metadata["parent_trace_sha256"] == hashlib.sha256(trace).hexdigest()
                assert (ring.root / (token + ".stderr")).read_bytes() == stderr
                assert "SYNTHETIC_PRIVATE" not in json.dumps(metadata)
                assert sum(p.stat().st_size for p in ring.root.iterdir()) <= obs.MAX_TOTAL_BYTES
            finally:
                ring.close(clear=True)


def test_private_parent_trace_is_explicitly_bounded():
    trace, original = obs.parent_trace(ValueError("PRIVATE" * 20000))
    assert original > len(trace) == obs.PARENT_TRACE_LIMIT


@pytest.fixture
def safety_gateway(tmp_path, monkeypatch):
    app = tmp_path / "app"
    app.mkdir()
    for path in (ROOT / "deploy/idp").iterdir():
        if path.is_file():
            shutil.copyfile(path, app / path.name)
    for name in ("document_semantic_fragments.py", "document_template_builder_rules.py", "document_literal_tables.py"):
        shutil.copyfile(ROOT / "backend/src/trackvance" / name, app / name)
    spec = importlib.util.spec_from_file_location("paddle_safety_gateway_fixture", app / "server.py")
    gateway = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(gateway)
    monkeypatch.setattr(gateway, "memory_barrier", lambda *_a, **_k: {"status": "EMPTY_RESIDENCY_OBSERVED"})
    monkeypatch.setattr(gateway, "cache_hygiene", lambda *_a: {"status": "DISABLED"})
    server = gateway.ThreadingHTTPServer(("127.0.0.1", 0), gateway.Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    def post(path, body, *, stream=False):
        connection = http.client.HTTPConnection(*server.server_address, timeout=3)
        try:
            connection.request("POST", path, body=json.dumps(body), headers={"Content-Type": "application/json"})
            response = connection.getresponse()
            raw = response.read()
            return response.status, [json.loads(line) for line in raw.splitlines()] if stream else json.loads(raw)
        finally:
            connection.close()
    try:
        yield gateway, post
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
        gateway.FAILURE_DIAGNOSTICS.close(clear=True)
        assert not thread.is_alive()
        assert not gateway.ADMISSIONS and not gateway.BUDGETS and not gateway.GATE.locked()


def test_loopback_circuit_stops_paddle_after_three_dispatches_and_qwen_is_independent(safety_gateway, monkeypatch):
    gateway, post = safety_gateway
    calls = []
    def fail(handler, _body):
        handler.paddle_attempted = True  # Synthetic successful spawn fact, never a real model call.
        calls.append("PADDLE")
        raise ValueError("PADDLE_FAILURE")
    monkeypatch.setattr(gateway.Handler, "extract", fail)
    monkeypatch.setattr(gateway.Handler, "interpret", lambda *_a: {"engine": {}})
    body = {"request_id": "c" * 32, "configuration": {}}
    for _ in range(3):
        assert post("/extract", body) == (422, {"error": "PADDLE_FAILURE"})
    assert post("/extract", body) == (503, {"error": "PADDLE_SERVICE_SUSPENDED"})
    assert calls == ["PADDLE"] * 3
    assert post("/interpret", body)[0] == 200
    assert gateway.PADDLE_ADMISSION.snapshot()["last_category"] == "UNKNOWN"


def test_rejected_input_does_not_consume_service_failure_budget(safety_gateway, monkeypatch):
    gateway, post = safety_gateway
    def invalid(_handler, _body):
        raise ValueError("DOCUMENT_FORMAT")
    monkeypatch.setattr(gateway.Handler, "extract", invalid)
    for _ in range(4):
        assert post("/extract", {"request_id": "d" * 32, "configuration": {}}) == (422, {"error": "DOCUMENT_FORMAT"})
    assert gateway.PADDLE_ADMISSION.snapshot()["consecutive_failures"] == 0


def test_unexpected_failure_is_controlled_and_does_not_leak_exception(safety_gateway, monkeypatch):
    gateway, post = safety_gateway
    def crash(handler, _body):
        handler.paddle_attempted = True
        raise RuntimeError("SYNTHETIC_PRIVATE_SECRET")
    monkeypatch.setattr(gateway.Handler, "extract", crash)
    assert post("/extract", {"request_id": "e" * 32, "configuration": {}}) == (422, {"error": "INFERENCE_FAILED"})
    assert gateway.PADDLE_ADMISSION.snapshot()["consecutive_failures"] == 1


def test_cleanup_failure_after_residency_barrier_never_claims_released_terminal(safety_gateway, monkeypatch):
    gateway, post = safety_gateway
    candidate = {"engine": {"synthetic_model_fixture": True}}
    def complete(session):
        session.handler.paddle_attempted = True
        session.expected = 1
        session.emit("PHASE", {"phase": "PADDLE_RECOGNITION"}, stage="PADDLE_RECOGNITION")
        return candidate
    def unknown_cleanup(_budget):
        raise gateway.MEMORY.UnsafeLifecycle("CACHE_OWNERSHIP_UNVERIFIED")
    monkeypatch.setattr(gateway.RECOGNITION.Session, "execute", complete)
    monkeypatch.setattr(gateway, "cache_hygiene", unknown_cleanup)
    definition = {"fields": [], "tables": []}
    configuration = {"paddle_model": "PaddleOCR-VL-1.6", "llm_model": gateway.LOCK["qwen"]["model"],
        "extraction_strategy": "PADDLE_STRUCTURED", "timeout_seconds": 60}
    body = {"request_id": "a" * 32, "attempt_id": "synthetic-cleanup", "strategy": "PADDLE_STRUCTURED",
        "source_sha256": SOURCE, "configuration": configuration, "configuration_sha256": gateway.RECOGNITION.digest(configuration),
        "definition": definition, "definition_sha256": gateway.RECOGNITION.digest(definition)}
    status, events = post("/recognize", body, stream=True)
    assert status == 200
    terminal = events[-1]
    assert terminal["status"] == "FAILED" and terminal["resources_released"] is False
    assert terminal["payload"]["memory_lifecycle"] is None
    assert terminal["payload"]["error_code"] == "CACHE_OWNERSHIP_UNVERIFIED"
    assert gateway.MEMORY_FAILURE["candidate"] is candidate
    assert gateway.PADDLE_ADMISSION.snapshot()["reason"] == "GPU_CLOSURE_UNCONFIRMED"
    assert post("/interpret", {"request_id": "b" * 32, "configuration": {}}) == (503, {"error": "GPU_MEMORY_UNAVAILABLE"})


@pytest.mark.skipif(os.name != "posix", reason="Linux owned-directory and child process contract")
@pytest.mark.parametrize("retention_available", [True, False])
def test_structured_timeout_retains_timeout_code_and_never_replaces_original(safety_gateway, monkeypatch, tmp_path, retention_available):
    gateway, _post = safety_gateway
    folder = Path(tempfile.mkdtemp(prefix="idp-recognition-", dir=tmp_path))
    ring = obs.FailureRing(HASHES, base=tmp_path)
    state = {"returncode": None, "reaped": False, "ticks": 0}

    class Budget:
        def remaining(self):
            state["ticks"] += 1
            if state["ticks"] >= 2:
                raise TimeoutError("INFERENCE_TIMEOUT")
            return 179.0
        def join_remaining(self):
            return 5.0

    class Child:
        pid = 929292
        def __init__(self, _args, **kwargs):
            kwargs["stderr"].write(b"PRIVATE_SYNTHETIC_TIMEOUT_STACK\n")
        def poll(self):
            return state["returncode"]
        def wait(self, timeout):
            state["reaped"] = True
            return state["returncode"]
    def kill(_pid, _signal):
        state["returncode"] = -9
    session = gateway.RECOGNITION.Session.__new__(gateway.RECOGNITION.Session)
    session.identity, session.source_sha = "b" * 32, SOURCE
    session.body, session.budget = {"filename": "document.pdf"}, Budget()
    session.phase, session.handler = lambda *_a: None, SimpleNamespace(paddle_attempted=False)
    session.api = {"ACTIVE": {}, "CANCELLED": set(), "DIAGNOSTICS": obs, "CHILD_SOURCE_HASHES": HASHES,
        "FAILURE_DIAGNOSTICS": ring, "RUNTIME": {}, "MEMORY": gateway.MEMORY, "safe_error": gateway.safe_error}
    monkeypatch.setattr(gateway.RECOGNITION.subprocess, "Popen", Child)
    monkeypatch.setattr(gateway.RECOGNITION.os, "killpg", kill)
    if not retention_available:
        monkeypatch.setattr(ring, "retain", lambda *_a, **_k: None)
    try:
        with pytest.raises(TimeoutError, match="^INFERENCE_TIMEOUT$"):
            session.child(folder, "STRUCTURED", lambda _page: pytest.fail("No page expected"))
        assert state["reaped"] and session.handler.paddle_attempted and not session.api["ACTIVE"]
        if retention_available:
            metadata = json.loads(next(ring.root.glob("*.json")).read_bytes())
            assert metadata["error"] == "INFERENCE_TIMEOUT"
            assert metadata["child_returncode"] == -9
            assert metadata["parent_trace_captured_bytes"] > 0
        else:
            assert session.api["RUNTIME"]["diagnostic_retention_available"] is False
    finally:
        ring.close(clear=True)


@pytest.mark.skipif(os.name != "posix", reason="Linux owned-directory and child process contract")
def test_structured_child_failure_keeps_partial_page_and_causal_capture(safety_gateway, monkeypatch):
    gateway, _post = safety_gateway
    gateway.RUNTIME["ready"] = True
    monkeypatch.setattr(gateway, "verify_models", lambda: True)
    closed = {"reaped": False}

    class Child:
        pid = 939393
        returncode = 7
        def __init__(self, args, **kwargs):
            folder = Path(args[-1])
            kwargs["stderr"].write(b"SYNTHETIC_PRIVATE_PADDLE_EXCEPTION\n")
            raw = b'{"res":{"page_index":0}}'
            (folder / "page.json").write_bytes(raw)
            page = {"page": 1, "page_geometry": {"width": 10, "height": 20, "unit": "PIXEL"},
                "artifacts": [{"path": "page.json", "kind": "PADDLE_PAGE_JSON", "bytes": len(raw),
                    "sha256": hashlib.sha256(raw).hexdigest()}]}
            (folder / "pages.manifest.json").write_text(json.dumps({"source_sha256": SOURCE, "expected_pages": 2, "pages": [page]}))
        def poll(self):
            return self.returncode
        def wait(self, timeout):
            closed["reaped"] = True
            return self.returncode

    monkeypatch.setattr(gateway.RECOGNITION.subprocess, "Popen", Child)
    # A bounded test-owned ring with the same private contract; HTTP never exposes its location.
    ring = gateway.FAILURE_DIAGNOSTICS
    original = ring.retain
    captures = []
    def retain(*args, **kwargs):
        assert closed["reaped"]
        token = original(*args, **kwargs)
        assert token is not None
        captures.append(json.loads((ring.root / (token + ".json")).read_bytes()))
        return token
    monkeypatch.setattr(ring, "retain", retain)
    definition = {"fields": [], "tables": []}
    configuration = {"paddle_model": "PaddleOCR-VL-1.6", "llm_model": gateway.LOCK["qwen"]["model"],
        "extraction_strategy": "PADDLE_STRUCTURED", "timeout_seconds": 60}
    body = {"request_id": "f" * 32, "attempt_id": "synthetic-1", "strategy": "PADDLE_STRUCTURED",
        "filename": "document.pdf", "source": base64.b64encode(b"synthetic source").decode(),
        "source_sha256": SOURCE, "configuration": configuration, "configuration_sha256": gateway.RECOGNITION.digest(configuration),
        "definition": definition, "definition_sha256": gateway.RECOGNITION.digest(definition)}
    # The helper above expects JSON; this endpoint intentionally streams NDJSON.
    server = gateway.ThreadingHTTPServer(("127.0.0.1", 0), gateway.Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    connection = http.client.HTTPConnection(*server.server_address, timeout=3)
    try:
        connection.request("POST", "/recognize", body=json.dumps(body), headers={"Content-Type": "application/json"})
        response = connection.getresponse()
        raw = response.read()
        assert response.status == 200
        events = [json.loads(line) for line in raw.splitlines()]
        assert len([event for event in events if event["kind"] == "PAGE"]) == 1
        terminal = events[-1]
        assert terminal["status"] == "FAILED" and terminal["pending_pages"] == [2]
        assert terminal["completed_pages"] == [1] and terminal["resources_released"] is True
        assert b"SYNTHETIC_PRIVATE" not in raw
        assert captures[0]["child_returncode"] == 7
        assert captures[0]["error"] == "PADDLE_FAILURE"
        assert captures[0]["parent_trace_captured_bytes"] > 0
        assert gateway.PADDLE_ADMISSION.snapshot()["consecutive_failures"] == 1
    finally:
        connection.close()
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
