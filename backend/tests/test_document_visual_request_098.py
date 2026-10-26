"""Effective visual requests are source-bound evidence, never observations."""
import base64
import copy
import hashlib
import importlib.util
import json
from pathlib import Path

import pytest
import test_document_recognition_stream_098 as stream_tests
from test_document_recognition_stream_098 import install_visual, invoke

from trackvance.document_lossless import adapt_saved_recognition
from trackvance.document_normalization import canonical_hash
from trackvance.document_recognition_transport import ARTIFACT_KINDS, validate_event
from trackvance.operations_common import OperationError

service = stream_tests.service


def gateway():
    path = Path(__file__).resolve().parents[2] / "deploy/idp/recognition.py"
    spec = importlib.util.spec_from_file_location("visual_request_gateway", path)
    value = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(value)
    return value


def request_record():
    api = gateway()
    body = {"model": "qwen3.5:9b-q4_K_M", "think": False, "keep_alive": 0, "format": {"type": "object"},
        "truncate": False, "shift": False, "messages": [{"role": "system", "content": "Read attached source."},
            {"role": "user", "content": '{"page":1}', "images": ["SYNTHETIC_IMAGE_BYTES"]}],
        "options": {"num_ctx": 32768, "num_predict": 8192, "temperature": 0, "seed": 98}}
    image = {"sha256": "d" * 64}
    page = {"page": 1, "page_geometry": {"width": 1000, "height": 700,
        "transformations": [{"operation": "RENDER_RGB", "scale": 2}]}}
    return body, api.visual_request_metadata(body, source_sha256="a" * 64,
        configuration_sha256="b" * 64, definition_sha256="c" * 64, image=image, page=page)


def event(record=None):
    if record is None:
        _, record = request_record()
    raw = json.dumps(record, sort_keys=True).encode()
    value = {"version": 1, "sequence": 1, "kind": "PHASE", "request_id": "request", "attempt_id": "attempt",
        "source_sha256": "a" * 64, "configuration_sha256": "b" * 64, "definition_sha256": "c" * 64,
        "stage": "QWEN_GENERATION", "page_one_based": 1, "expected_pages": 1,
        "payload": {"phase": "QWEN_GENERATION", "artifacts": [{"kind": "QWEN_REQUEST_JSON",
            "media_type": "application/json", "bytes": len(raw),
            "sha256": hashlib.sha256(raw).hexdigest(), "content_b64": base64.b64encode(raw).decode()}]}}
    value["event_sha256"] = canonical_hash(value)
    return value


def binding(value):
    return {key: value[key] for key in ("request_id", "attempt_id", "source_sha256", "configuration_sha256", "definition_sha256")}


def test_effective_request_retains_exact_prompt_schema_options_and_image_hash_without_bytes():
    body, record = request_record()
    assert record["request_sha256"] == canonical_hash(body)
    restored = copy.deepcopy(record["request_without_image_bytes"])
    assert "SYNTHETIC_IMAGE_BYTES" not in json.dumps(record)
    restored["messages"][1]["images"] = body["messages"][1]["images"]
    assert restored == body and canonical_hash(restored) == record["request_sha256"]
    assert record["transport_options"] == {"stream": True}
    assert record["strategy"] == "QWEN_VISUAL"
    assert record["image"]["geometry_kind"] == "QUERY_REGION"
    assert record["image"]["transformations"] == [{"operation": "RENDER_RGB", "scale": 2}]


def test_request_phase_validates_as_source_bound_private_artifact_without_becoming_a_page():
    value = event()
    assert "QWEN_REQUEST_JSON" in ARTIFACT_KINDS
    assert validate_event(value, binding(value), 1)["kind"] == "PHASE"
    assert value["stage"] == "QWEN_GENERATION"
    with pytest.raises(OperationError) as error:
        adapt_saved_recognition([value], {"fields": [], "tables": []}, {}, source_sha256="a" * 64)
    assert error.value.code == "DOCUMENT_RECOGNITION_MISSING"


def test_real_http_preserves_request_phase_before_page_and_links_the_effective_body(service, monkeypatch):
    runtime, source = service
    calls = install_visual(runtime, monkeypatch)
    saved = []
    result = invoke(source, "QWEN_VISUAL", saved.append)
    request_index, request = next((index, item) for index, item in enumerate(saved)
        if any(raw["kind"] == "QWEN_REQUEST_JSON" for raw in item["payload"].get("artifacts", [])))
    assert request["kind"] == "PHASE" and request["payload"]["phase"] == "QWEN_GENERATION"
    assert request["stage"] == "QWEN_GENERATION"
    assert request_index < next(index for index, item in enumerate(saved) if item["kind"] == "PAGE")
    artifact = request["payload"]["artifacts"][0]
    metadata = json.loads(base64.b64decode(artifact["content_b64"]))
    assert len(calls) == 1 and metadata["request_sha256"] == canonical_hash(calls[0])
    content = json.loads(calls[0]["messages"][1]["content"])
    assert content["json_schema"] == calls[0]["format"]
    assert content["targets"] == {"fields": ["identifier"], "tables": ["items"]}
    assert content["document_pages"] == 1 and content["definition"]["semantic_definition_version"] == "trackvance-visual-semantic-098-v2"
    assert "Copy every visible requested amount" in calls[0]["messages"][0]["content"]
    assert metadata["configuration_sha256"] == request["configuration_sha256"]
    assert result["terminal"]["status"] == "COMPLETE" and len(result["pages"]) == 1
    assert not any(raw["kind"] == "QWEN_REQUEST_JSON" for raw in result["pages"][0]["payload"]["artifacts"])


def test_real_http_stream_failure_retains_request_and_raw_incomplete_page(service, monkeypatch):
    runtime, source = service
    install_visual(runtime, monkeypatch)
    def failure(_identity, _body, timeout, raw_capture=None):
        if raw_capture:
            raw_capture(b'{"partial":true}\n')
        raise ValueError("LLM_FAILURE")
    monkeypatch.setattr(runtime, "stream_chat", failure)
    saved = []
    result = invoke(source, "QWEN_VISUAL", saved.append)
    request = next(item for item in saved if any(raw["kind"] == "QWEN_REQUEST_JSON"
        for raw in item["payload"].get("artifacts", [])))
    assert request["kind"] == "PHASE" and len(result["pages"]) == 1
    assert result["pages"][0]["payload"]["recognition_status"] == "INCOMPLETE"
    assert result["terminal"]["status"] == "FAILED" and result["terminal"]["resources_released"] is True


@pytest.mark.parametrize("change", ["body_image", "page", "source", "mime", "kind", "stage", "hash", "oversize", "not_object"])
def test_request_phase_rejects_invalid_or_unbound_metadata(change):
    _, record = request_record()
    if change == "body_image":
        record["request_without_image_bytes"]["messages"][1]["images"] = ["NOT_ALLOWED"]
    elif change == "page":
        record["image"]["source_page"] = 2
    elif change == "source":
        record["source_sha256"] = "e" * 64
    elif change == "oversize":
        record["request_without_image_bytes"]["messages"][0]["content"] = "x" * (256 * 1024)
    value = event(record)
    if change == "mime":
        value["payload"]["artifacts"][0]["media_type"] = "text/plain"
    elif change == "kind":
        value["payload"]["artifacts"][0]["kind"] = "PAGE_IMAGE_PNG"
    elif change == "stage":
        value["stage"] = "PADDLE_STRUCTURED"
    elif change == "hash":
        value["payload"]["artifacts"][0]["sha256"] = "0" * 64
    elif change == "not_object":
        value["payload"]["artifacts"][0] = None
    value["event_sha256"] = canonical_hash({key: item for key, item in value.items() if key != "event_sha256"})
    with pytest.raises(OperationError) as error:
        validate_event(value, binding(value), 1)
    assert error.value.code == "DOCUMENT_RECOGNITION_CONTRACT"
