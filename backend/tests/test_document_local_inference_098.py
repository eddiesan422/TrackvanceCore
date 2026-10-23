"""Contract/security tests deliberately need no GPU, model downloads, or cloud."""
import copy
import json

import httpx
import pytest

from trackvance import document_local_inference as local
from trackvance.document_engine import validated_candidate_lines
from trackvance.operations_common import OperationError


def definition():
    return {"fields": [{"name": "invoice", "logical_type": "STRING", "required": True}], "tables": []}


def line(text="Invoice: A-1"):
    return {"text": text, "page": 1, "region": [.1, .1, .8, .2], "method": "OCR", "confidence": None}


def payload():
    return {"fields": {"invoice": {"value": None, "observed": None, "validity": "MISSING", "origin": None,
        "confidence": None, "reviewed": False}}, "tables": {}, "table_unparsed": {}, "lines": [line()], "pages": [1]}


@pytest.mark.parametrize("url", ["https://example.com", "http://169.254.169.254", "http://localhost.evil.test",
    "http://user:password@localhost:3288", "http://localhost:3288/path", "http://localhost:3288?query=1", "file:///etc/passwd"])
def test_only_deployment_local_endpoint(url, monkeypatch):
    monkeypatch.setenv("TRACKVANCE_IDP_LOCAL_URL", url)
    with pytest.raises(OperationError) as caught:
        local._endpoint()
    assert caught.value.code == "DOCUMENT_LOCAL_INFERENCE_UNCONFIGURED"


def test_unconfigured_status_is_honest(monkeypatch):
    monkeypatch.delenv("TRACKVANCE_IDP_LOCAL_URL", raising=False)
    assert local.availability()["execution_enabled"] is False


@pytest.mark.parametrize("status", [None, [], "unexpected", 123])
def test_malformed_health_is_unavailable(status, monkeypatch):
    monkeypatch.setattr(local, "_request", lambda *args, **kwargs: status)
    assert local.availability()["available"] is False


def test_provider_word_polygons_retained_and_outside_words_rejected():
    observation = line()
    observation["words"] = [{"text": "A-1", "region": [.4, .1, .6, .2], "confidence": 99}]
    clean = validated_candidate_lines([observation], 1)
    assert clean[0]["words"] == [{"text": "A-1", "region": [.4, .1, .6, .2], "confidence": None}]
    observation["words"][0]["region"] = [.4, .1, .9, .2]
    with pytest.raises(ValueError, match="DOCUMENT_CANDIDATE_INVALID"):
        validated_candidate_lines([observation], 1)


def test_llm_observations_are_independently_normalized():
    candidate = {"fields": {"invoice": {"observed": "A-1", "evidence_id": 0}}, "tables": {}}
    original = payload()
    result = local.apply_semantics(original, candidate, definition(), [line()])
    assert result["fields"]["invoice"]["value"] == "A-1"
    assert result["fields"]["invoice"]["confidence"] is None
    assert result["fields"]["invoice"]["reviewed"] is False
    assert original["fields"]["invoice"]["value"] is None


@pytest.mark.parametrize("cell", [{"observed": "invented", "evidence_id": 0}, {"observed": "A-1", "evidence_id": 3},
    {"observed": "A-1", "evidence_id": True}, {"observed": "A-1", "evidence_id": 0, "reviewed": True},
    {"observed": "A-1", "evidence_id": None}, {"observed": None, "evidence_id": 0}])
def test_hallucination_or_forged_review_is_rejected(cell):
    with pytest.raises(OperationError) as caught:
        local.apply_semantics(payload(), {"fields": {"invoice": cell}, "tables": {}}, definition(), [line()])
    assert caught.value.code == "DOCUMENT_LLM_EVIDENCE_INVALID"


def test_llm_cannot_silently_replace_valid_value():
    original = payload()
    original["fields"]["invoice"].update(value="A-1", observed="A-1", validity="VALID")
    result = local.apply_semantics(original, {"fields": {"invoice": {"observed": "B-2", "evidence_id": 0}}, "tables": {}}, definition(), [line("A-1 B-2")])
    assert result["fields"]["invoice"]["value"] == "A-1"
    assert result["fields"]["invoice"]["validity"] == "AMBIGUOUS"


@pytest.mark.parametrize("change", [{"page": 2}, {"page": True}, {"region": [0, 0, float("nan"), 1]},
    {"region": [0, 0, 0, 1]}, {"region": [-1, 0, 1, 1]}, {"text": 123}])
def test_candidate_geometry_validated_before_rules(change):
    with pytest.raises(ValueError, match="DOCUMENT_CANDIDATE_INVALID"):
        validated_candidate_lines([{**line(), **change}], 1)


def test_candidate_confidence_and_review_not_trusted():
    clean = validated_candidate_lines([{**line(), "confidence": 100, "reviewed": True}], 1)
    assert clean[0]["confidence"] is None
    assert "reviewed" not in clean[0]


def test_native_sufficiency_does_not_ignore_embedded_scans():
    data = payload()
    data["fields"]["invoice"].update(value="A-1", validity="VALID")
    assert local._sufficient(data, definition())
    data["native_incomplete_pages"] = [1]
    assert not local._sufficient(data, definition())


def test_native_reference_reads_skip_models_but_scanned_reference_does_not():
    data = {"fields": {}, "tables": {}, "lines": [line()], "native_incomplete_pages": []}
    assert local._sufficient(data, {"fields": [], "tables": []})
    data["native_incomplete_pages"] = [1]
    assert not local._sufficient(data, {"fields": [], "tables": []})


def test_native_only_route_avoids_inference(tmp_path, monkeypatch):
    data = payload()
    data["fields"]["invoice"].update(value="A-1", validity="VALID")
    calls = []

    def runner(source, definition, **kwargs):
        calls.append(kwargs)
        return {"payload": copy.deepcopy(data), "engine": {"key": "LEGACY"}}

    monkeypatch.setattr(local, "_request", lambda *args, **kwargs: pytest.fail("native document must not use GPU"))
    result = local.run_local(tmp_path / "no-read.pdf", definition(), {}, legacy_runner=runner, check=lambda _: None)
    assert calls[0]["native_only"] is True
    assert result["engine"]["inference_skipped"] is True


def test_response_limit_and_redirects(monkeypatch):
    monkeypatch.setenv("TRACKVANCE_IDP_LOCAL_URL", "http://localhost:3288")
    monkeypatch.setattr(local, "MAX_RESPONSE", 8)
    real_client = httpx.Client
    transport = httpx.MockTransport(lambda request: httpx.Response(200, content=json.dumps({"long": "x" * 50}).encode()))
    monkeypatch.setattr(local.httpx, "Client", lambda **kwargs: real_client(transport=transport, **kwargs))
    with pytest.raises(OperationError) as caught:
        local._request("/health")
    assert caught.value.code == "DOCUMENT_LOCAL_INFERENCE_LIMIT"
