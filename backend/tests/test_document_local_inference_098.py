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
    "http://host.docker.internal.evil.test", "http://host.docker.internal@evil.test:3288",
    "http://user:password@localhost:3288", "http://localhost:3288/path", "http://localhost:3288?query=1", "file:///etc/passwd"])
def test_only_deployment_local_endpoint(url, monkeypatch):
    monkeypatch.setenv("TRACKVANCE_IDP_LOCAL_URL", url)
    with pytest.raises(OperationError) as caught:
        local._endpoint()
    assert caught.value.code == "DOCUMENT_LOCAL_INFERENCE_UNCONFIGURED"


def test_reserved_docker_host_endpoint_is_deployment_owned(monkeypatch):
    monkeypatch.setenv("TRACKVANCE_IDP_LOCAL_URL", "http://host.docker.internal:3288")
    assert local._endpoint() == "http://host.docker.internal:3288"


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


def test_focused_reextraction_uses_only_problem_regions_and_is_bounded():
    spec = definition()
    spec["fields"][0]["visual"] = {"page": 1, "region": [.1, .1, .5, .2]}
    targets = local.focused_regions(payload(), spec, [line()])
    assert targets == [{"page": 1, "region": [.08, .08, .52, .22]}]
    spec["fields"] *= 20
    assert len(local.focused_regions(payload(), spec, [line()])) <= 4


def test_refinement_preserves_unrelated_observations_and_valid_disagreements():
    old, untouched = line(), {**line("Footer"), "region": [.1, .8, .8, .9]}
    replacement = line("Invoice: B-2")
    output = local.refined_observations([old, untouched], [replacement], [{"page": 1, "region": [.05, .05, .9, .3]}])
    assert output == [untouched, replacement]
    before, after = payload(), payload()
    before["fields"]["invoice"].update(value="A-1", observed="A-1", validity="VALID")
    after["fields"]["invoice"].update(value="B-2", observed="B-2", validity="VALID")
    result = local.retain_disagreements(before, after)
    assert result["fields"]["invoice"]["value"] == "A-1"
    assert result["fields"]["invoice"]["validity"] == "AMBIGUOUS"
    assert result["semantic_conflicts"] == ["REEXTRACTION_FIELD:invoice"]


def test_reextraction_preserves_table_candidates_and_flags_changed_cells():
    before, after = payload(), payload()
    old = {"value": "12.30", "observed": "12.30", "validity": "VALID", "reviewed": False}
    new = {"value": "12.80", "observed": "12.80", "validity": "VALID", "reviewed": False}
    before["tables"] = {"detail": [{"amount": old}]}
    after["tables"] = {"detail": [{"amount": new}]}
    result = local.retain_disagreements(before, after)
    assert result["tables"]["detail"][0]["amount"]["validity"] == "AMBIGUOUS"
    assert result["reextraction_table_candidates"]["detail"]["before"][0]["amount"]["value"] == "12.30"
    assert result["reextraction_table_candidates"]["detail"]["after"][0]["amount"]["value"] == "12.80"
    assert old["validity"] == "VALID"


def test_scanned_route_refines_only_failed_region_and_skips_unneeded_llm(tmp_path, monkeypatch):
    source = tmp_path / "sample.pdf"
    source.write_bytes(b"synthetic transport fixture")
    spec = definition()
    spec["fields"][0]["visual"] = {"page": 1, "region": [.1, .1, .8, .2]}
    calls = []

    def invoke(path, body, **kwargs):
        calls.append(path)
        assert body["configuration"]["use_llm"] is False
        return {"source_sha256": body["source_sha256"], "lines": [line("fixed" if path == "/refine" else "unclear")],
                "engine": {"phases": {"paddle_seconds": .01}}}

    def runner(_source, _definition, **kwargs):
        result = payload()
        result["native_incomplete_pages"] = [1]
        candidates = kwargs.get("candidate_lines")
        if candidates and candidates[0]["text"] == "fixed":
            result["fields"]["invoice"].update(value="A-1", observed="A-1", validity="VALID")
        return {"payload": result, "engine": {}}

    monkeypatch.setattr(local, "_invoke", invoke)
    result = local.run_local(source, spec, {"max_retries": 2, "use_llm": True}, legacy_runner=runner, check=lambda _: None)
    assert calls == ["/extract", "/refine"]
    assert result["engine"]["route"] == "PADDLE"
    assert len(result["engine"]["focused_reextraction"]) == 1
    assert result["payload"]["inference_observation_history"][0]["observations"][0]["text"] == "unclear"
