"""Real loopback stream/authority tests with explicit synthetic model outputs."""
import base64
import hashlib
import importlib.util
import json
import shutil
import threading
import time
from pathlib import Path

import pytest

from trackvance import document_recognition_transport as transport
from trackvance.document_inference_completion import AttemptBudget, canonical_hash
from trackvance.document_lossless import run_lossless
from trackvance.document_providers import EngineConfiguration
from trackvance.document_schemas import DocumentDefinition
from trackvance.operations_common import OperationError

PNG = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAusB9Wl6OdwAAAAASUVORK5CYII=")
DEFINITION = {"fields": [{"name": "identifier", "label": "Identifier", "logical_type": "STRING"}],
    "tables": [{"name": "items", "fields": [{"name": "code", "label": "Code", "logical_type": "STRING"}]}]}


@pytest.fixture
def service(tmp_path, monkeypatch):
    root = Path(__file__).resolve().parents[2]
    app = tmp_path / "app"
    app.mkdir()
    for path in (root / "deploy/idp").iterdir():
        if path.is_file():
            shutil.copyfile(path, app / path.name)
    for name in ("document_semantic_fragments.py", "document_template_builder_rules.py", "document_literal_tables.py"):
        shutil.copyfile(root / "backend/src/trackvance" / name, app / name)
    spec = importlib.util.spec_from_file_location("recognition_gateway_fixture", app / "server.py")
    gateway = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(gateway)
    gateway.RUNTIME["ready"] = True
    monkeypatch.setattr(gateway, "verify_models", lambda: True)
    monkeypatch.setattr(gateway, "health", lambda: {"ready": True,
        "recognition": {"contract": "TRACKVANCE_RECOGNITION_V1", "strategies": ["QWEN_VISUAL", "PADDLE_STRUCTURED"]}})
    monkeypatch.setattr(gateway, "memory_barrier", lambda *_args, **_kwargs: {"status": "EMPTY_RESIDENCY_OBSERVED"})
    monkeypatch.setattr(gateway, "cache_hygiene", lambda *_: {"status": "DISABLED"})
    def metadata(_identity, _budget, path, body=None, *, cleanup=False):
        if path == "/tags":
            return {"models": [{"name": gateway.LOCK["qwen"]["model"], "digest": gateway.LOCK["qwen"]["manifest_sha256"]}]}
        if path == "/version":
            return {"version": "synthetic-version-no-model-runtime"}
        pytest.fail("Unexpected synthetic metadata query")
    monkeypatch.setattr(gateway, "memory_request", metadata)
    server = gateway.ThreadingHTTPServer(("127.0.0.1", 0), gateway.Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    monkeypatch.setenv("TRACKVANCE_IDP_LOCAL_URL", "http://127.0.0.1:" + str(server.server_address[1]))
    monkeypatch.setattr(transport, "TRANSPORT_UNAVAILABLE", False)
    source = tmp_path / "source.pdf"
    source.write_bytes(b"synthetic model source; not a PDF/model accuracy fixture")
    try:
        yield gateway, source
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
        gateway.FAILURE_DIAGNOSTICS.close(clear=True)
        assert not thread.is_alive()
        assert not gateway.ACTIVE
        assert not gateway.UPSTREAM


def configuration(strategy):
    return EngineConfiguration(provider="PADDLE_QWEN_LOCAL", extraction_strategy=strategy,
        timeout_seconds=60, context_tokens=32768).model_dump()


def invoke(source, strategy, on_event, check=lambda _: None):
    return transport.recognize_stream(source, DEFINITION, configuration(strategy), strategy=strategy,
        check=check, budget=AttemptBudget(60), on_event=on_event, attempt_context={"attempt_id": "attempt-1"})


def synthetic_paddle_child(session, folder, operation, consume, *, page_count=2):
    assert operation == "STRUCTURED"
    session.phase("PADDLE_RECOGNITION")
    session.expected = page_count
    for number in range(1, page_count + 1):
        if session.identity in session.api["CANCELLED"]:
            raise ValueError("CANCELLED")
        raw = json.dumps({"res": {"page_index": number - 1, "width": 10, "height": 20,
            "parsing_res_list": [{"block_label": "table", "block_content": "<table><tr><td>A|B<br>C</td></tr></table>"}]}}).encode()
        path = folder / f"synthetic-{number}.json"
        path.write_bytes(raw)
        consume({"page": number, "page_geometry": {"width": 10, "height": 20, "unit": "PIXEL", "transformations": []},
            "artifacts": [{"path": path.name, "kind": "PADDLE_PAGE_JSON", "sha256": hashlib.sha256(raw).hexdigest(), "bytes": len(raw)}]})
        if number == 1:
            time.sleep(.15)  # Permit a real coordinator cancel/lease-loss race.
    return {"engine": {"synthetic_model_fixture": True}}


def test_legacy_configuration_serializes_without_new_default_keys():
    assert "extraction_strategy" not in EngineConfiguration().model_dump()
    assert configuration("NATIVE_FIRST")["extraction_strategy"] == "NATIVE_FIRST"


def test_real_stream_persists_pages_on_coordinator_thread_before_terminal(service, monkeypatch):
    gateway, source = service
    monkeypatch.setattr(gateway.RECOGNITION.Session, "child", synthetic_paddle_child)
    coordinator = threading.get_ident()
    observed = []
    def sink(event):
        assert threading.get_ident() == coordinator
        observed.append(event)
        assert "durable_refs" not in event
        return {"manifest_artifact_id": "synthetic-immutable-artifact", "event_sha256": event["event_sha256"], "artifacts": []}
    result = invoke(source, "PADDLE_STRUCTURED", sink)
    assert [event["kind"] for event in observed] == ["PHASE", "PAGE", "PAGE", "TERMINAL"]
    assert len(result["pages"]) == 2
    assert result["terminal"]["status"] == "COMPLETE"
    assert result["terminal"]["resources_released"] is True
    assert all("durable_refs" in event for event in result["pages"])
    first = result["pages"][0]["payload"]["artifacts"][0]
    assert b"A|B<br>C" in base64.b64decode(first["content_b64"])
    assert not gateway.ADMISSIONS and not gateway.BUDGETS
    assert transport.transport_available()


def test_page_survives_later_failure_and_terminal_is_not_complete(service, monkeypatch):
    gateway, source = service
    def partial(session, folder, operation, consume):
        def after_first(page):
            consume(page)
            raise ValueError("PADDLE_FAILURE")
        return synthetic_paddle_child(session, folder, operation, after_first)
    monkeypatch.setattr(gateway.RECOGNITION.Session, "child", partial)
    saved = []
    result = invoke(source, "PADDLE_STRUCTURED", saved.append)
    assert len(result["pages"]) == 1
    assert result["terminal"]["status"] == "FAILED"
    assert result["terminal"]["pending_pages"] == [2]
    assert result["terminal"]["resources_released"] is True
    assert len([event for event in saved if event["kind"] == "PAGE"]) == 1


def test_lease_loss_cancels_real_http_stream_and_rejects_late_pages(service, monkeypatch):
    gateway, source = service
    monkeypatch.setattr(gateway.RECOGNITION.Session, "child", synthetic_paddle_child)
    page_callbacks = []
    def lost(event):
        if event["kind"] == "PAGE":
            page_callbacks.append(event["page_one_based"])
            raise OperationError(409, "DOCUMENT_LEASE_LOST", "synthetic lease loss")
    with pytest.raises(OperationError) as caught:
        invoke(source, "PADDLE_STRUCTURED", lost)
    assert caught.value.code == "DOCUMENT_LEASE_LOST"
    assert page_callbacks == [1]
    assert transport.transport_available()
    assert not gateway.ADMISSIONS


def test_cancelled_work_still_persists_authorized_terminal_during_cleanup(service, monkeypatch):
    gateway, source = service
    monkeypatch.setattr(gateway.RECOGNITION.Session, "child", synthetic_paddle_child)
    cancelled, saved = threading.Event(), []
    def checkpoint(_progress):
        if cancelled.is_set():
            raise OperationError(409, "DOCUMENT_CANCELLED", "synthetic authorized cancellation")
    def durable_sink(event):
        # The real worker's terminal sink checks lease/attempt/actor, allowing
        # an already requested cancellation only for the immutable close proof.
        saved.append(event)
        if event["kind"] == "PAGE":
            cancelled.set()
    with pytest.raises(OperationError) as caught:
        invoke(source, "PADDLE_STRUCTURED", durable_sink, check=checkpoint)
    assert caught.value.code == "DOCUMENT_CANCELLED"
    assert [event["page_one_based"] for event in saved if event["kind"] == "PAGE"] == [1]
    assert saved[-1]["kind"] == "TERMINAL"
    # Completion can win the remote cancel race. It remains closure evidence;
    # the original local cancellation must still reject that late success.
    assert saved[-1]["status"] in {"CANCELLED", "COMPLETE"}
    assert saved[-1]["resources_released"] is True
    assert transport.transport_available()
    assert not gateway.ADMISSIONS


def install_visual(gateway, monkeypatch, *, invalid=False, incomplete=None):
    def render(session, folder, operation, consume):
        assert operation == "RENDER"
        session.phase("RENDER_SOURCE")
        session.expected = 1
        path = folder / "image.png"
        path.write_bytes(PNG)
        consume({"page": 1, "page_geometry": {"width": 1, "height": 1, "unit": "PIXEL", "transformations": [{"operation": "RENDER_RGB", "scale": 1}]},
            "artifacts": [{"path": path.name, "kind": "PAGE_IMAGE_PNG", "bytes": len(PNG), "sha256": hashlib.sha256(PNG).hexdigest()}]})
        return {"engine": {"render_only": True}}
    calls = []
    def chat(_identity, body, timeout, raw_capture=None):
        calls.append(body)
        assert timeout > 0
        assert base64.b64decode(body["messages"][1]["images"][0]) == PNG
        assert "fragment_id" not in json.dumps(body["format"])
        cell = {"observed": "0007", "ambiguous": False, "alternatives": []}
        selection = {"fields": {"identifier": cell}, "tables": {"items": [{"code": cell}]}}
        if invalid:
            selection["fields"] = {}
        content = json.dumps(selection)
        reply = {"done": True, "done_reason": "stop", "message": {"content": content},
            "prompt_eval_count": 100, "eval_count": 20, "total_duration": 1000, "load_duration": 100,
            "prompt_eval_duration": 100, "eval_duration": 800}
        if incomplete == "stop":
            reply["done_reason"] = "length"
        elif incomplete == "missing_metrics":
            reply.pop("eval_count")
        elif incomplete == "input_budget":
            reply["prompt_eval_count"] = 24577
        elif incomplete == "output_budget":
            reply["eval_count"] = 8193
        if raw_capture:
            raw_capture((json.dumps(reply) + "\n").encode())
        return reply
    monkeypatch.setattr(gateway.RECOGNITION.Session, "child", render)
    monkeypatch.setattr(gateway, "stream_chat", chat)
    return calls


def test_visual_route_sends_real_image_and_preserves_original_reply_and_stream(service, monkeypatch):
    gateway, source = service
    calls = install_visual(gateway, monkeypatch)
    result = invoke(source, "QWEN_VISUAL", lambda _: None)
    assert len(calls) == 1
    assert result["terminal"]["status"] == "COMPLETE"
    artifacts = result["pages"][0]["payload"]["artifacts"]
    assert {item["kind"] for item in artifacts} == {"PAGE_IMAGE_PNG", "QWEN_REPLY_JSON", "QWEN_STREAM_NDJSON", "QWEN_SELECTION_JSON"}
    selection = next(item for item in artifacts if item["kind"] == "QWEN_SELECTION_JSON")
    assert json.loads(base64.b64decode(selection["content_b64"]))["fields"]["identifier"]["observed"] == "0007"


def incomplete_native_probe(pages):
    return {"engine": {"key": "EXPLICIT_NATIVE_READER_DOUBLE", "pages": pages},
        "payload": {"lines": [], "pages": list(range(1, pages + 1)), "native_incomplete_pages": list(range(1, pages + 1))}}


def lossless_definition():
    return DocumentDefinition.model_validate({**DEFINITION, "tables": [{**DEFINITION["tables"][0],
        "row_label": "ROW ", "row_pattern": r"^ROW (?P<code>.+)$"}]}).model_dump(mode="json")


@pytest.mark.parametrize("strategy,pages", [("QWEN_VISUAL", 1), ("PADDLE_STRUCTURED", 2)])
def test_real_session_terminal_reaches_lossless_completion_for_its_provider_stage(service, monkeypatch, strategy, pages):
    gateway, source = service
    if strategy == "QWEN_VISUAL":
        install_visual(gateway, monkeypatch)
    else:
        monkeypatch.setattr(gateway.RECOGNITION.Session, "child", synthetic_paddle_child)
    definition = lossless_definition()
    emitted = []
    result = run_lossless(source, definition, configuration(strategy),
        legacy_runner=lambda *_args, **_kwargs: incomplete_native_probe(pages),
        check=lambda _: None, budget=AttemptBudget(60), on_event=emitted.append,
        attempt_context={"attempt_id": "real-terminal-1"}, recognize=transport.recognize_stream)
    # The HTTP gateway uses the actual Session.finish and emits the terminal;
    # this test never constructs a provider terminal event in its fixture.
    assert result["engine"]["execution"]["recognition_complete"] is True
    assert result["engine"]["execution"]["resources_released"] is True
    terminal = next(event for event in reversed(emitted) if event["kind"] == "TERMINAL")
    assert terminal["stage"] == strategy and terminal["status"] == "COMPLETE"
    assert terminal["completed_pages"] == list(range(1, pages + 1))
    assert result["payload"]["document_representation"]["terminals"][-1]["event_sha256"] == terminal["event_sha256"]


def test_real_failed_qwen_terminal_cannot_borrow_later_paddle_completion(service, monkeypatch):
    gateway, source = service
    install_visual(gateway, monkeypatch, invalid=True)
    render = gateway.RECOGNITION.Session.child
    def selective_child(session, folder, operation, consume):
        if operation == "RENDER":
            return render(session, folder, operation, consume)
        return synthetic_paddle_child(session, folder, operation, consume, page_count=1)
    monkeypatch.setattr(gateway.RECOGNITION.Session, "child", selective_child)
    definition = lossless_definition()
    result = run_lossless(source, definition, configuration("SELECTIVE"),
        legacy_runner=lambda *_args, **_kwargs: incomplete_native_probe(1),
        check=lambda _: None, budget=AttemptBudget(60),
        attempt_context={"attempt_id": "real-selective-terminal-1"}, recognize=transport.recognize_stream)
    terminals = result["payload"]["document_representation"]["terminals"]
    assert [(event["stage"], event["status"]) for event in terminals] == [
        ("QWEN_VISUAL", "FAILED"), ("PADDLE_STRUCTURED", "COMPLETE")]
    assert len({event["request_id"] for event in terminals}) == 2
    assert result["engine"]["execution"]["stage"] == "QWEN_VISUAL"
    assert result["engine"]["execution"]["recognition_complete"] is False
    assert result["engine"]["execution"]["resources_released"] is True


def test_invalid_selection_keeps_originals_and_fails_terminal(service, monkeypatch):
    gateway, source = service
    install_visual(gateway, monkeypatch, invalid=True)
    result = invoke(source, "QWEN_VISUAL", lambda _: None)
    assert result["terminal"]["status"] == "FAILED"
    assert result["terminal"]["pending_pages"] == [1]
    assert {item["kind"] for item in result["pages"][0]["payload"]["artifacts"]} == {"PAGE_IMAGE_PNG", "QWEN_REPLY_JSON", "QWEN_STREAM_NDJSON"}


@pytest.mark.parametrize("incomplete", ["stop", "missing_metrics", "input_budget", "output_budget"])
def test_schema_valid_reply_cannot_complete_without_generation_and_budget_proof(service, monkeypatch, incomplete):
    gateway, source = service
    install_visual(gateway, monkeypatch, incomplete=incomplete)
    result = invoke(source, "QWEN_VISUAL", lambda _: None)
    page = result["pages"][0]
    assert page["payload"]["selection_schema_valid"] is True
    assert page["payload"]["recognition_status"] == "INCOMPLETE"
    assert {item["kind"] for item in page["payload"]["artifacts"]} == {
        "PAGE_IMAGE_PNG", "QWEN_REPLY_JSON", "QWEN_STREAM_NDJSON", "QWEN_SELECTION_JSON"}
    assert result["terminal"]["status"] == "FAILED"
    assert result["terminal"]["completed_pages"] == [] and result["terminal"]["pending_pages"] == [1]


def test_unproved_resource_close_blocks_later_admission(service, monkeypatch):
    gateway, source = service
    monkeypatch.setattr(gateway.RECOGNITION.Session, "child", synthetic_paddle_child)
    def barrier(_identity, _budget, *, cleanup=False):
        if cleanup:
            raise gateway.MEMORY.UnsafeLifecycle("GPU_RESIDENCY_UNVERIFIED")
        return {"status": "EMPTY_RESIDENCY_OBSERVED"}
    monkeypatch.setattr(gateway, "memory_barrier", barrier)
    with pytest.raises(OperationError) as caught:
        invoke(source, "PADDLE_STRUCTURED", lambda _: None)
    assert caught.value.code == "DOCUMENT_LOCAL_CLEANUP_UNVERIFIED"
    assert not transport.transport_available()
    with pytest.raises(OperationError, match="cierre"):
        invoke(source, "PADDLE_STRUCTURED", lambda _: None)


@pytest.mark.parametrize("mutation", ["hash", "source", "forged_durable_refs", "sequence"])
def test_frame_integrity_and_remote_authority_forgery_rejected(mutation):
    binding = {"request_id": "a" * 32, "attempt_id": "attempt-1", "source_sha256": "b" * 64,
        "configuration_sha256": "c" * 64, "definition_sha256": "d" * 64}
    event = {**binding, "version": 1, "sequence": 1, "kind": "PHASE", "stage": "RENDER_SOURCE",
        "page_one_based": None, "expected_pages": None, "payload": {"phase": "RENDER_SOURCE"}}
    event["event_sha256"] = canonical_hash(event)
    if mutation == "hash":
        event["event_sha256"] = "e" * 64
    elif mutation == "source":
        event["source_sha256"] = "f" * 64
    elif mutation == "forged_durable_refs":
        event["durable_refs"] = {"manifest_artifact_id": "forged"}
    else:
        event["sequence"] = 2
    with pytest.raises(OperationError):
        transport.validate_event(event, binding, 1)
