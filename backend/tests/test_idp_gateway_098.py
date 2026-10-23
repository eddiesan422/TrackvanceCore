"""Actual local HTTP boundary tests without a GPU, document data, or downloads."""
import base64
import hashlib
import http.client
import importlib.util
import json
import threading
from pathlib import Path

import pytest

HTTPConnection = http.client.HTTPConnection


@pytest.fixture
def gateway():
    root = Path(__file__).resolve().parents[2]
    spec = importlib.util.spec_from_file_location("idp_gateway_test", root / "deploy/idp/server.py")
    module = importlib.util.module_from_spec(spec)
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
    assert post(endpoint, "/cancel", length=1025) == (413, {"error": "REQUEST_LIMIT"})


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


def test_gateway_health_keeps_process_startup_hashes_after_files_change(gateway, tmp_path, monkeypatch):
    names = ("server.py", "paddle_runner.py", "pipeline.yml", "prompt-v1.txt", "models.lock.json")
    for name in names:
        (tmp_path / name).write_bytes((gateway.APP_ROOT / name).read_bytes())
    spec = importlib.util.spec_from_file_location("idp_gateway_frozen_test", tmp_path / "server.py")
    frozen = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(frozen)
    expected = {name: hashlib.sha256((tmp_path / name).read_bytes()).hexdigest() for name in names}
    for name in names:
        (tmp_path / name).write_bytes(b"changed after process startup")
    monkeypatch.setattr(frozen, "qwen_verified", lambda: False)
    proof = frozen.health()["runtime_provenance"]["gateway"]
    assert proof == {"capture": "PROCESS_STARTUP", "files_sha256": expected}
    assert frozen.PROMPT == gateway.PROMPT
    assert frozen.LOCK_BYTES == gateway.LOCK_BYTES
    proof["files_sha256"].clear()
    assert frozen.startup_provenance()["files_sha256"] == expected


def interpretation_body(gateway, observations=None):
    observations = [{"text": "Invoice A-1", "page": 1, "region": [0, 0, 1, 1]}] if observations is None else observations
    return {"request_id": "e" * 32, "source_sha256": "b" * 64, "observations": observations,
        "observations_sha256": hashlib.sha256(json.dumps(observations, sort_keys=True, ensure_ascii=False).encode()).hexdigest(),
        "configuration": {"llm_model": gateway.LOCK["qwen"]["model"], "prompt_version": "trackvance-idp-098-v1"},
        "definition": {}, "semantic_schema": {}}


def test_interpretation_exposes_actual_ollama_metrics_and_loaded_prompt(gateway, endpoint, monkeypatch):
    metrics = {"total_duration": 9_123_456_789, "load_duration": 5_000_000_000,
               "prompt_eval_duration": 2_000_000_000, "eval_duration": 2_123_456_789,
               "prompt_eval_count": 123, "eval_count": 456}
    calls = []

    def chat(identity, body, timeout):
        calls.append(body)
        return {**metrics, "done": True, "done_reason": "stop", "message": {"content": '{"headers": []}'}}

    monkeypatch.setattr(gateway, "qwen_verified", lambda: True)
    monkeypatch.setattr(gateway, "stream_chat", chat)
    monkeypatch.setattr(Path, "read_text", lambda *args, **kwargs: pytest.fail("prompt must remain the loaded bytes"))
    status, result = post(endpoint, "/interpret", interpretation_body(gateway))
    assert status == 200
    assert calls[0]["messages"][0]["content"] == gateway.PROMPT
    assert calls[0]["truncate"] is False and calls[0]["shift"] is False
    assert calls[0]["options"]["num_ctx"] == 8192
    assert calls[0]["options"]["num_predict"] == 4096
    engine = result["engine"]
    assert engine["ollama_metrics"] == metrics
    assert engine["phases"]["llm_seconds"] == 9.1235
    assert engine["prompt_sha256"] == gateway.STARTUP_HASHES["prompt-v1.txt"]
    assert engine["model_lock_sha256"] == gateway.STARTUP_HASHES["models.lock.json"]
    assert engine["runtime_provenance"] == {"gateway": gateway.startup_provenance()}
    assert engine["context_budget"]["verified"] is True
    assert engine["context_budget"]["input_tokens_observed"] == 123
    assert engine["context_budget"]["output_tokens_observed"] == 456
    assert result["semantics"] == {"headers": []}


@pytest.mark.parametrize("value", [None, -1, True, 1.25, "12", 2**63])
def test_invalid_ollama_observations_remain_unavailable(gateway, value):
    names = ("total_duration", "load_duration", "prompt_eval_duration", "eval_duration", "prompt_eval_count", "eval_count")
    assert gateway.ollama_metrics(dict.fromkeys(names, value)) == dict.fromkeys(names)


@pytest.mark.parametrize("metrics,expected_seconds", [({}, None), ({"total_duration": 0}, 0.0)])
def test_missing_duration_is_distinct_from_observed_zero(gateway, endpoint, monkeypatch, metrics, expected_seconds):
    monkeypatch.setattr(gateway, "qwen_verified", lambda: True)
    monkeypatch.setattr(gateway, "stream_chat", lambda *args, **kwargs: {**metrics, "done": True,
        "done_reason": "stop", "prompt_eval_count": 12, "eval_count": 7, "message": {"content": "{}"}})
    status, result = post(endpoint, "/interpret", interpretation_body(gateway))
    assert status == 200
    assert result["engine"]["phases"]["llm_seconds"] == expected_seconds
    assert result["engine"]["ollama_metrics"]["load_duration"] is None
    assert result["engine"]["ollama_metrics"]["prompt_eval_count"] == 12


def test_no_qwen_invocation_does_not_create_metrics(gateway, endpoint, monkeypatch):
    monkeypatch.setattr(gateway, "qwen_verified", lambda: True)
    monkeypatch.setattr(gateway, "stream_chat", lambda *args, **kwargs: pytest.fail("no evidence to interpret"))
    status, result = post(endpoint, "/interpret", interpretation_body(gateway, []))
    assert status == 200
    assert "ollama_metrics" not in result["engine"]
    assert result["engine"]["phases"] == {}
    assert result["engine"]["runtime_provenance"] == {"gateway": gateway.startup_provenance()}


def test_paddle_child_uses_its_captured_pipeline_bytes(tmp_path, monkeypatch):
    root = Path(__file__).resolve().parents[2] / "deploy/idp"
    spec = importlib.util.spec_from_file_location("idp_runner_test", root / "paddle_runner.py")
    runner = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(runner)
    expected = {name: hashlib.sha256((root / name).read_bytes()).hexdigest()
                for name in ("paddle_runner.py", "pipeline.yml")}
    monkeypatch.setattr(Path, "read_bytes", lambda *args, **kwargs: pytest.fail("child must use captured configuration"))
    snapshot = Path(runner.captured_pipeline(tmp_path))
    with snapshot.open("rb") as stream:
        assert stream.read() == runner.PIPELINE_BYTES
    assert runner.startup_provenance() == {"capture": "PROCESS_STARTUP", "files_sha256": expected}


def test_extract_preserves_child_phases_and_provenance(gateway, monkeypatch):
    child_proof = {"capture": "PROCESS_STARTUP", "files_sha256": {"paddle_runner.py": "a" * 64}}

    class CompletedChild:
        returncode = 0

        def __init__(self, command, **kwargs):
            result = {"lines": [], "engine": {"phases": {"paddle_seconds": 1.25},
                      "runtime_provenance": {"paddle_runner": child_proof}}}
            (Path(command[-1]) / "result.json").write_text(json.dumps(result))

        def wait(self, **kwargs):
            return 0

        def poll(self):
            return 0

    monkeypatch.setattr(gateway.subprocess, "Popen", CompletedChild)
    monkeypatch.setattr(gateway, "verify_models", lambda: True)
    gateway.RUNTIME["ready"] = True
    raw = b"synthetic GPU-boundary test input"
    body = {"request_id": "f" * 32, "filename": "document.pdf",
            "source": base64.b64encode(raw).decode(), "source_sha256": hashlib.sha256(raw).hexdigest(),
            "configuration": {"paddle_model": "PaddleOCR-VL-1.6", "llm_model": gateway.LOCK["qwen"]["model"],
                              "prompt_version": "trackvance-idp-098-v1"}}
    engine = gateway.Handler.extract(None, body)["engine"]
    assert engine["phases"] == {"paddle_seconds": 1.25}
    assert engine["runtime_provenance"] == {"paddle_runner": child_proof, "gateway": gateway.startup_provenance()}


def test_stream_keeps_final_metrics_separate_from_intermediate_chunks(gateway, monkeypatch):
    chunks = iter([
        {"message": {"content": "{"}, "eval_count": 999},
        {"message": {"content": "}"}, "done": True, "total_duration": 4, "eval_count": 2}])

    class Upstream:
        status = 200

        def __init__(self, *args, **kwargs):
            pass

        def request(self, *args, **kwargs):
            pass

        def getresponse(self):
            return self

        def readline(self, size):
            return json.dumps(next(chunks)).encode() + b"\n"

        def close(self):
            pass

    monkeypatch.setattr(gateway.http.client, "HTTPConnection", Upstream)
    result = gateway.stream_chat("1" * 32, {}, timeout=1)
    assert result["message"]["content"] == "{}"
    assert result["eval_count"] == 2
    assert gateway.ollama_metrics(result)["total_duration"] == 4
    assert not gateway.UPSTREAM


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
    assert result[0] == (422, {"error": "CANCELLED"})
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


def test_compact_semantic_metadata_keeps_literal_rules_without_parser_samples(gateway):
    definition = {"id": "private-uuid", "fields": [{"name": "discount", "label": "Discount", "logical_type": "DECIMAL",
        "decimal_separator": ",", "thousands_separator": ".", "numeric_fragments": True, "decimal_format_explicit": True,
        "visual": {"page": 1, "region": [0, 0, 1, 1], "label": "Less", "label_aliases": ["Discount amount"],
                   "sample_value": "do not send", "context": "private context", "value_pattern": r"-\s*([0-9,.]+)"}}],
        "tables": [{"name": "detail", "row_pattern": "do not send", "fields": [{"name": "sku", "logical_type": "STRING"}],
                    "visual": {"header_labels": ["Code"], "header_label_aliases": [["Item"]], "region": [0, 0, 1, 1]}}]}
    compact = gateway.compact_definition(definition)
    text = json.dumps(compact)
    assert compact["semantic_definition_version"] == "trackvance-semantic-098-v2"
    field = compact["fields"][0]
    assert field["numeric_fragments"] is True and field["decimal_format_explicit"] is True
    assert field["literal_capture_pattern"] == r"-\s*([0-9,.]+)"
    assert field["labels"] == ["Less", "Discount amount"]
    assert compact["tables"][0]["fields"][0]["labels"] == ["Code", "Item"]
    assert all(value not in text for value in ("private-uuid", "do not send", "private context", "region", "row_pattern"))
    observations = [{"text": "Less -12,30", "page": 1, "region": [.1, .2, .3, .4], "words": ["large ignored geometry"]}]
    content = json.loads(gateway.semantic_content(definition, observations))
    assert content["evidence"] == [{"text": "Less -12,30", "page": 1, "region": [.1, .2, .3, .4], "evidence_id": 0}]


@pytest.mark.parametrize("reply,expected", [
    ({"done": True, "done_reason": "length", "prompt_eval_count": 12, "eval_count": 4096}, "LLM_INCOMPLETE"),
    ({"done_reason": "stop", "prompt_eval_count": 12, "eval_count": 7}, "LLM_INCOMPLETE"),
    ({"done": True, "done_reason": "stop", "eval_count": 7}, "LLM_BUDGET_UNVERIFIED"),
    ({"done": True, "done_reason": "stop", "prompt_eval_count": 4097, "eval_count": 7}, "LLM_CONTEXT_LIMIT"),
    ({"done": True, "done_reason": "stop", "prompt_eval_count": 12, "eval_count": 4097}, "LLM_CONTEXT_LIMIT")])
def test_incomplete_or_unverifiable_context_never_returns_success(gateway, endpoint, monkeypatch, reply, expected):
    monkeypatch.setattr(gateway, "qwen_verified", lambda: True)
    monkeypatch.setattr(gateway, "stream_chat", lambda *args, **kwargs: {**reply, "message": {"content": "{}"}})
    assert post(endpoint, "/interpret", interpretation_body(gateway)) == (422, {"error": expected})
    assert gateway.OBSERVED_INFERENCE["qwen"] is False


@pytest.mark.parametrize("context", [0, 1023, 32769, True, "8192", None])
def test_invalid_context_configuration_is_not_silently_clamped(gateway, endpoint, monkeypatch, context):
    monkeypatch.setattr(gateway, "qwen_verified", lambda: True)
    monkeypatch.setattr(gateway, "stream_chat", lambda *args, **kwargs: pytest.fail("invalid budget must not invoke a model"))
    body = interpretation_body(gateway)
    body["configuration"]["context_tokens"] = context
    assert post(endpoint, "/interpret", body) == (422, {"error": "LLM_CONTEXT_LIMIT"})


@pytest.mark.parametrize("error,status,code", [
    (ValueError("MODEL_INTEGRITY"), 503, "MODEL_INTEGRITY"),
    (ValueError("PADDLE_FAILURE"), 422, "PADDLE_FAILURE"),
    (ValueError("LLM_CONTEXT_LIMIT"), 422, "LLM_CONTEXT_LIMIT"),
    (ValueError("document values /credentials/error.log"), 422, "INFERENCE_FAILED"),
    (OSError("document values /credentials/error.log"), 422, "INFERENCE_FAILED"),
    (TimeoutError("private timeout text"), 504, "INFERENCE_TIMEOUT")])
def test_error_allowlist_never_echoes_exception_content(gateway, endpoint, monkeypatch, error, status, code):
    def fail(self, body):
        raise error

    monkeypatch.setattr(gateway.Handler, "extract", fail)
    assert post(endpoint, "/extract", {"request_id": "b" * 32, "configuration": {}}) == (status, {"error": code})
    assert not gateway.GATE.locked()


def test_upstream_context_diagnostics_are_normalized_without_echo(gateway):
    assert gateway._llm_error(b'{"error":"the input length exceeds the context length"}') == "LLM_CONTEXT_LIMIT"
    assert gateway._llm_error(b'{"error":"private model output /credentials"}') == "LLM_FAILURE"
    assert gateway._llm_error(b"private /credentials", 413) == "LLM_CONTEXT_LIMIT"
    assert gateway._llm_error(b"x" * 4097) == "LLM_FAILURE"
