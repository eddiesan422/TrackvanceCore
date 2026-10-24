"""Actual local HTTP boundary tests without a GPU, document data, or downloads."""
import base64
import hashlib
import http.client
import importlib.util
import json
import shutil
import threading
import time
from pathlib import Path

import pytest

HTTPConnection = http.client.HTTPConnection


@pytest.fixture
def gateway(tmp_path):
    root = Path(__file__).resolve().parents[2]
    for path in (root / "deploy/idp").iterdir():
        if path.is_file():
            shutil.copyfile(path, tmp_path / path.name)
    for name in ("document_semantic_fragments.py", "document_template_builder_rules.py", "document_literal_tables.py"):
        shutil.copyfile(root / "backend/src/trackvance" / name, tmp_path / name)
    spec = importlib.util.spec_from_file_location("idp_gateway_test", tmp_path / "server.py")
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
    names = tuple(gateway.STARTUP_FILES)
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
    assert frozen.PROMPTS == gateway.PROMPTS
    assert frozen.LOCK_BYTES == gateway.LOCK_BYTES
    observations = [{"text": "A | B", "page": 1, "region": [0, 0, 1, 1]}]
    assert frozen.FRAGMENTS.build_catalog(observations) == gateway.FRAGMENTS.build_catalog(observations)
    proof["files_sha256"].clear()
    assert frozen.startup_provenance()["files_sha256"] == expected


def interpretation_body(gateway, observations=None):
    observations = [{"text": "Invoice A-1", "page": 1, "region": [0, 0, 1, 1]}] if observations is None else observations
    return {"request_id": "e" * 32, "source_sha256": "b" * 64, "observations": observations,
        "observations_sha256": hashlib.sha256(json.dumps(observations, sort_keys=True, ensure_ascii=False).encode()).hexdigest(),
        "configuration": {"llm_model": gateway.LOCK["qwen"]["model"], "prompt_version": "trackvance-idp-098-v1"},
        "definition": {}, "semantic_schema": {}}


def selection_definition():
    return {"fields": [{"name": "known"}, {"name": "missing"}],
            "tables": [{"name": "rows", "fields": [{"name": "concept"}]}]}


@pytest.mark.parametrize("targets", [None, [], {}, {"fields": ["unknown"], "tables": []},
    {"fields": ["missing", "missing"], "tables": []}, {"fields": [True], "tables": []},
    {"fields": [], "tables": ["unknown"]}, {"fields": [], "tables": [], "extra": []}])
def test_semantic_targets_only_allow_unique_declared_names(gateway, targets):
    with pytest.raises(ValueError, match="LLM_SELECTION_INVALID"):
        gateway.semantic_targets(selection_definition(), targets)


def test_fragment_selection_preserves_null_non_targets_and_exact_ids(gateway):
    definition = selection_definition()
    targets = gateway.semantic_targets(definition, {"fields": ["missing"], "tables": ["rows"]})
    candidate = {"fields": {"known": {"fragment_id": None}, "missing": {"fragment_id": 0}},
                 "tables": {"rows": [{"concept": {"fragment_id": 2}}]}}
    assert gateway.validate_fragment_selection(candidate, definition, targets, 3) is None


@pytest.mark.parametrize("identity", [-1, 3, True, 1.5, "1", {"observed": "invented"}])
def test_fragment_selection_rejects_unknown_or_non_integer_ids(gateway, identity):
    definition = selection_definition()
    candidate = {"fields": {"known": {"fragment_id": None}, "missing": {"fragment_id": identity}},
                 "tables": {"rows": []}}
    with pytest.raises(ValueError, match="LLM_SELECTION_INVALID"):
        gateway.validate_fragment_selection(candidate, definition, {"fields": ["missing"], "tables": []}, 3)


@pytest.mark.parametrize("kind", ["field", "table", "generated_value"])
def test_fragment_selection_cannot_change_non_targets_or_return_generated_values(gateway, kind):
    definition = selection_definition()
    candidate = {"fields": {"known": {"fragment_id": None}, "missing": {"fragment_id": None}},
                 "tables": {"rows": []}}
    if kind == "field":
        candidate["fields"]["known"]["fragment_id"] = 0
    elif kind == "table":
        candidate["tables"]["rows"] = [{"concept": {"fragment_id": 0}}]
    else:
        candidate["fields"]["missing"] = {"fragment_id": 0, "observed": "invented"}
    with pytest.raises(ValueError, match="LLM_SELECTION_INVALID"):
        gateway.validate_fragment_selection(candidate, definition, {"fields": ["missing"], "tables": []}, 3)


def fragment_interpretation_body(gateway):
    body = interpretation_body(gateway, [{"text": r"Concept | A\nB | 12,00", "page": 1, "region": [0, 0, 1, 1]}])
    body["configuration"]["prompt_version"] = "trackvance-idp-098-v2"
    body["definition"] = selection_definition()
    body["semantic_targets"] = {"fields": ["missing"], "tables": ["rows"]}
    body["semantic_schema"] = {"type": "object", "description": "synthetic fragment schema"}
    return body


def fragment_reply(selection):
    return {"done": True, "done_reason": "stop", "prompt_eval_count": 20, "eval_count": 10,
            "message": {"content": json.dumps(selection)}}


def resolved_detail_body(gateway):
    body = fragment_interpretation_body(gateway)
    body["definition"]["tables"][0]["visual"] = {"header_labels": ["Concept"], "row_start": {"column": 0,
        "segments": [{"alphabet": "ASCII_ALPHA", "minimum": 2, "maximum": 2},
            {"alphabet": "ASCII_DIGIT", "minimum": 1, "maximum": 1}]}}
    body["observations"] = [
        {"text": "Invoice: source", "page": 1, "region": [0, 0, 1, .1], "method": "OCR"},
        {"text": "Concept", "page": 1, "region": [0, .2, 1, .8], "method": "OCR", "block_label": "table"},
        {"text": "AA-1", "page": 1, "region": [0, .2, 1, .8], "method": "OCR", "block_label": "table"}]
    body["observations_sha256"] = hashlib.sha256(json.dumps(body["observations"], sort_keys=True, ensure_ascii=False).encode()).hexdigest()
    body["semantic_targets"]["tables"] = []
    return body


def test_filtered_prompt_preserves_full_catalog_and_source_context(gateway):
    body = resolved_detail_body(gateway)
    content, catalog, view = gateway.fragment_content(body["definition"], body["observations"], body["semantic_targets"])
    shown = json.loads(content)
    assert catalog == gateway.FRAGMENTS.build_catalog(body["observations"])
    assert view["omitted_detail_observation_ids"] == [1, 2]
    assert shown["fragment_catalog_columns"] == ["fragment_id", "evidence_id", "text"]
    assert {item[1] for item in shown["fragment_catalog"]} == {0}
    assert shown["observation_context"] == [{"evidence_id": 0, "page": 1, "region": [0, 0, 1, .1], "block_label": None}]
    assert shown["fragment_prompt_view_sha256"] == view["sha256"]


def test_http_rejects_omitted_global_id_even_when_cell_is_a_target(gateway, endpoint, monkeypatch):
    body = resolved_detail_body(gateway)
    view = gateway.TABLES.prompt_catalog_view(body["observations"], body["definition"], body["semantic_targets"])["prompt_view"]
    selection = {"fields": {"known": {"fragment_id": None}, "missing": {"fragment_id": view["omitted_fragment_ids"][0]}},
        "tables": {"rows": []}}
    monkeypatch.setattr(gateway, "qwen_verified", lambda: True)
    monkeypatch.setattr(gateway, "stream_chat", lambda *args, **kwargs: fragment_reply(selection))
    assert post(endpoint, "/interpret", body) == (422, {"error": "LLM_SELECTION_INVALID"})


def test_prompt_view_hash_mismatch_fails_before_model_call(gateway, endpoint, monkeypatch):
    body = resolved_detail_body(gateway)
    body["fragment_prompt_view_sha256"] = "0" * 64
    calls = []
    monkeypatch.setattr(gateway, "qwen_verified", lambda: True)
    monkeypatch.setattr(gateway, "stream_chat", lambda *args, **kwargs: calls.append(args))
    assert post(endpoint, "/interpret", body) == (422, {"error": "LLM_FRAGMENT_INTEGRITY"})
    assert not calls


def test_literal_helpers_keep_captured_bytes_after_files_change(gateway, tmp_path):
    body = resolved_detail_body(gateway)
    expected = gateway.TABLES.map_candidates(body["observations"], body["definition"])
    hashes = gateway.startup_provenance()
    for name in ("document_semantic_fragments.py", "document_template_builder_rules.py", "document_literal_tables.py"):
        (tmp_path / name).write_bytes(b"untrusted changed file after startup")
    assert gateway.TABLES.map_candidates(body["observations"], body["definition"]) == expected
    assert gateway.startup_provenance() == hashes


def test_v2_interpretation_uses_catalog_and_returns_only_verifiable_source_ids(gateway, endpoint, monkeypatch):
    body = fragment_interpretation_body(gateway)
    catalog = gateway.FRAGMENTS.build_catalog(body["observations"])
    identity = next(item["fragment_id"] for item in catalog["fragments"] if item["text"] == r"A\nB")
    selection = {"fields": {"known": {"fragment_id": None}, "missing": {"fragment_id": None}},
                 "tables": {"rows": [{"concept": {"fragment_id": identity}}]}}
    calls = []

    def chat(request_id, request_body, timeout):
        calls.append(request_body)
        return fragment_reply(selection)

    monkeypatch.setattr(gateway, "qwen_verified", lambda: True)
    monkeypatch.setattr(gateway, "stream_chat", chat)
    status, result = post(endpoint, "/interpret", body)
    assert status == 200
    content = json.loads(calls[0]["messages"][1]["content"])
    assert "evidence" not in content and "observations" not in content
    assert content["semantic_targets"] == body["semantic_targets"]
    assert content["fragment_catalog"] == [[item["fragment_id"], item["evidence_id"], item["text"]] for item in catalog["fragments"]]
    assert calls[0]["messages"][0]["content"] == gateway.PROMPTS["trackvance-idp-098-v2"]
    assert calls[0]["format"] == body["semantic_schema"]
    assert result["semantics"] == selection
    assert result["fragment_catalog_sha256"] == catalog["sha256"]
    assert result["fragment_catalog_version"] == catalog["version"]
    assert result["engine"]["fragment_count"] == len(catalog["fragments"])
    assert result["engine"]["prompt_sha256"] == gateway.STARTUP_HASHES["prompt-v2.txt"]
    assert result["lines"] == body["observations"]


@pytest.mark.parametrize("kind", ["non_target", "out_of_range", "generated_text"])
def test_v2_http_boundary_rejects_non_target_ids_and_generated_strings(gateway, endpoint, monkeypatch, kind):
    body = fragment_interpretation_body(gateway)
    selection = {"fields": {"known": {"fragment_id": None}, "missing": {"fragment_id": None}}, "tables": {"rows": []}}
    if kind == "non_target":
        selection["fields"]["known"]["fragment_id"] = 0
    elif kind == "out_of_range":
        selection["fields"]["missing"]["fragment_id"] = 4096
    else:
        selection["fields"]["missing"] = {"observed": "generated", "evidence_id": 0}
    monkeypatch.setattr(gateway, "qwen_verified", lambda: True)
    monkeypatch.setattr(gateway, "stream_chat", lambda *args, **kwargs: fragment_reply(selection))
    assert post(endpoint, "/interpret", body) == (422, {"error": "LLM_SELECTION_INVALID"})


@pytest.mark.parametrize("text,code", [("a" * 65537, "LLM_FRAGMENT_LIMIT"), (None, "LLM_FRAGMENT_INTEGRITY")])
def test_v2_catalog_fails_closed_before_llm_on_invalid_or_oversized_evidence(gateway, endpoint, monkeypatch, text, code):
    body = fragment_interpretation_body(gateway)
    body["observations"][0]["text"] = text
    body["observations_sha256"] = hashlib.sha256(json.dumps(body["observations"], sort_keys=True, ensure_ascii=False).encode()).hexdigest()
    monkeypatch.setattr(gateway, "qwen_verified", lambda: True)
    monkeypatch.setattr(gateway, "stream_chat", lambda *args, **kwargs: pytest.fail("invalid catalog must not reach Qwen"))
    assert post(endpoint, "/interpret", body) == (422, {"error": code})


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


def test_unknown_cancel_nonces_do_not_allocate_state(gateway, endpoint):
    gateway.STATES["f" * 32] = {"phase": "UNKNOWN_STALE_STATE"}
    for index in range(32):
        assert post(endpoint, "/cancel", {"request_id": format(index, "032x")}) == (200, {"cancelled": False})
    assert post(endpoint, "/cancel", {"request_id": "f" * 32}) == (200, {"cancelled": False})
    assert not gateway.CANCELLED


def test_cancel_remains_accepted_when_child_exits_after_poll(gateway, endpoint, monkeypatch):
    identity = "8" * 32

    class CompletedChild:
        pid = 12345

        def poll(self):
            return None

    def already_exited(pid, requested_signal):
        assert pid == CompletedChild.pid and requested_signal == gateway.signal.SIGKILL
        raise ProcessLookupError("synthetic process already exited")

    gateway.ADMISSIONS[identity] = 1
    gateway.ACTIVE[identity] = CompletedChild()
    monkeypatch.setattr(gateway.os, "killpg", already_exited)
    assert post(endpoint, "/cancel", {"request_id": identity}) == (200, {"cancelled": True})


def test_cancel_racing_completed_response_cannot_leave_permanent_nonce(gateway, endpoint, monkeypatch):
    entered, adding, release_extract, release_add = (threading.Event() for _ in range(4))
    identity = "9" * 32

    class DelayedCancellation(set):
        def add(self, value):
            adding.set()
            release_extract.set()
            assert release_add.wait(timeout=2)
            super().add(value)

    def extract(self, body):
        entered.set()
        assert release_extract.wait(timeout=2)
        return {"engine": {"elapsed_seconds": 0}}

    monkeypatch.setattr(gateway, "CANCELLED", DelayedCancellation())
    monkeypatch.setattr(gateway.Handler, "extract", extract)
    results = []
    inference = threading.Thread(target=lambda: results.append(post(endpoint, "/extract",
        {"request_id": identity, "configuration": {}})))
    cancellation = threading.Thread(target=lambda: results.append(post(endpoint, "/cancel", {"request_id": identity})))
    inference.start()
    assert entered.wait(timeout=2)
    cancellation.start()
    assert adding.wait(timeout=2)
    inference.join(timeout=2)  # Response is delivered before admission cleanup.
    assert not inference.is_alive()
    time.sleep(.05)
    release_add.set()
    cancellation.join(timeout=2)
    assert not cancellation.is_alive()
    deadline = time.monotonic() + 2
    while gateway.ADMISSIONS and time.monotonic() < deadline:
        time.sleep(.01)
    assert len(results) == 2 and all(status == 200 for status, _ in results)
    assert not gateway.ADMISSIONS and not gateway.CANCELLED


def paddle_request(gateway):
    raw = b"synthetic cancellation boundary"
    return {"request_id": "7" * 32, "filename": "document.pdf", "source": base64.b64encode(raw).decode(),
        "source_sha256": hashlib.sha256(raw).hexdigest(), "configuration": {"paddle_model": "PaddleOCR-VL-1.6",
            "llm_model": gateway.LOCK["qwen"]["model"], "prompt_version": "trackvance-idp-098-v1"}}


def test_cancel_before_child_creation_never_starts_a_process(gateway, monkeypatch):
    gateway.RUNTIME["ready"] = True
    monkeypatch.setattr(gateway, "verify_models", lambda: True)
    monkeypatch.setattr(gateway.subprocess, "Popen", lambda *args, **kwargs: pytest.fail("cancelled request must not spawn"))
    body = paddle_request(gateway)
    gateway.CANCELLED.add(body["request_id"])
    with pytest.raises(ValueError, match="^CANCELLED$"):
        gateway.Handler.extract(None, body)


def test_cancel_during_creation_kills_child_before_waiting_for_its_timeout(gateway, endpoint, monkeypatch):
    constructing, release_creation, killed, waited_before_cancel = (threading.Event() for _ in range(4))

    class Child:
        pid = 12345
        returncode = None

        def __init__(self, *args, **kwargs):
            constructing.set()
            assert release_creation.wait(timeout=2)

        def poll(self):
            return self.returncode

        def wait(self, timeout):
            if not killed.is_set():
                waited_before_cancel.set()
                raise gateway.subprocess.TimeoutExpired("synthetic", timeout)
            self.returncode = -9
            return self.returncode

    def kill(pid, _signal):
        assert pid == Child.pid
        killed.set()

    gateway.RUNTIME["ready"] = True
    monkeypatch.setattr(gateway, "verify_models", lambda: True)
    monkeypatch.setattr(gateway.subprocess, "Popen", Child)
    monkeypatch.setattr(gateway.os, "killpg", kill)
    body, result = paddle_request(gateway), []
    active = threading.Thread(target=lambda: result.append(post(endpoint, "/extract", body)))
    active.start()
    assert constructing.wait(timeout=2)
    assert post(endpoint, "/cancel", {"request_id": body["request_id"]}) == (200, {"cancelled": True})
    release_creation.set()
    active.join(timeout=2)
    assert not active.is_alive()
    assert result == [(422, {"error": "CANCELLED"})]
    assert killed.is_set() and not waited_before_cancel.is_set()
    assert not gateway.GATE.locked() and not gateway.ACTIVE
