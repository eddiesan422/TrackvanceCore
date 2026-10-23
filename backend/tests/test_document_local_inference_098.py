"""Contract/security tests deliberately need no GPU, model downloads, or cloud."""
import copy
import json
import threading
import time

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
    spec["fields"][0]["visual"] = {"page": 1, "region": [.1, .1, .5, .2], "kind": "REGION_CONTEXT"}
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
    spec["fields"][0]["visual"] = {"page": 1, "region": [.1, .1, .8, .2], "kind": "REGION_CONTEXT"}
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
    result = local.run_local(source, spec, {"max_retries": 2, "use_llm": False}, legacy_runner=runner, check=lambda _: None)
    assert calls == ["/extract", "/refine"]
    assert result["engine"]["route"] == "PADDLE"
    assert len(result["engine"]["focused_reextraction"]) == 1
    assert result["payload"]["inference_observation_history"][0]["observations"][0]["text"] == "unclear"


def test_qwen_interpretation_preserves_paddle_child_and_gateway_provenance(tmp_path, monkeypatch):
    source = tmp_path / "sample.pdf"
    source.write_bytes(b"synthetic provenance fixture")
    provenance = {"gateway": {"files_sha256": {"server.py": "a" * 64}},
                  "paddle_runner": {"files_sha256": {"paddle_runner.py": "b" * 64}}}

    def invoke(path, body, **kwargs):
        if path == "/extract":
            return {"source_sha256": body["source_sha256"], "lines": [line()],
                "engine": {"runtime_provenance": copy.deepcopy(provenance), "phases": {"paddle_seconds": .25}}}
        assert path == "/interpret"
        return {"source_sha256": body["source_sha256"], "observations_sha256": body["observations_sha256"],
            "semantics": {"fields": {"invoice": {"observed": "A-1", "evidence_id": 0}}, "tables": {}},
            "engine": {"runtime_provenance": {"gateway": provenance["gateway"]},
                       "ollama_metrics": {"load_duration": 123, "eval_count": 7}, "phases": {"llm_seconds": .5}}}

    monkeypatch.setattr(local, "_invoke", invoke)
    result = local.run_local(source, definition(), {"max_retries": 0, "use_llm": True},
        legacy_runner=lambda *args, **kwargs: {"payload": payload(), "engine": {}}, check=lambda _: None)
    assert result["engine"]["runtime_provenance"] == provenance
    assert result["engine"]["ollama_metrics"] == {"load_duration": 123, "eval_count": 7}
    assert result["engine"]["phases"] == {"paddle_seconds": .25, "llm_seconds": .5}


@pytest.mark.parametrize("status,diagnostic,expected", [
    (422, "LLM_CONTEXT_LIMIT", "DOCUMENT_LLM_CONTEXT_LIMIT"),
    (422, "LLM_INCOMPLETE", "DOCUMENT_LLM_INCOMPLETE"),
    (503, "LLM_MODEL_INTEGRITY", "DOCUMENT_LOCAL_MODEL_INTEGRITY"),
    (503, "CONFIGURED_INFERENCE_CAPACITY", "DOCUMENT_LOCAL_INFERENCE_BUSY"),
    (504, "INFERENCE_TIMEOUT", "DOCUMENT_TIMEOUT"),
    (422, "CANCELLED", "DOCUMENT_CANCELLED"),
    (422, "private document text /credentials", "DOCUMENT_LOCAL_INFERENCE_UNAVAILABLE")])
def test_fixed_gateway_diagnostics_never_echo_untrusted_messages(monkeypatch, status, diagnostic, expected):
    monkeypatch.setenv("TRACKVANCE_IDP_LOCAL_URL", "http://localhost:3288")
    real_client = httpx.Client
    transport = httpx.MockTransport(lambda _: httpx.Response(status, json={"error": diagnostic, "stderr": "secret"}))
    monkeypatch.setattr(local.httpx, "Client", lambda **kwargs: real_client(transport=transport, **kwargs))
    with pytest.raises(OperationError) as caught:
        local._request("/interpret", {})
    assert caught.value.code == expected
    assert "secret" not in caught.value.message and "/credentials" not in caught.value.message


def test_expanded_crops_remove_containment_before_four_region_cap():
    spec, data = definition(), payload()
    spec["fields"] = []
    spec["tables"] = [{"name": "detail", "fields": []}]
    data["tables"] = {"detail": []}
    boxes = [[.1, .1, .4, .3], [.2, .15, .3, .2], [.1, .4, .4, .6], [.1, .7, .4, .9], [.6, .1, .9, .3]]
    observations = [{**line(), "region": box, "block_label": "table"} for box in boxes]
    targets = local.focused_regions(data, spec, observations)
    assert len(targets) == 4
    assert targets[0]["region"] == [.08, .08, .42000000000000004, .32]
    assert targets[-1]["region"][0] == .58


def test_broad_missing_label_rule_never_falls_back_to_full_page():
    spec = definition()
    spec["fields"][0]["visual"] = {"page": 1, "region": [0, 0, 1, 1], "label": "Invoice"}
    assert local.focused_regions(payload(), spec, [line("Other label")]) == []
    targets = local.focused_regions(payload(), spec, [{**line("Invoice: A-1"), "region": [.1, .1, .4, .12]}])
    assert targets and targets[0]["region"][3] < .3
    assert targets[0]["region"] != [0, 0, 1, 1]


def test_ambiguous_anchors_and_other_sections_remain_unresolved():
    field = {"name": "invoice", "visual": {"page": 1, "region": [0, 0, 1, 1], "label": "Invoice",
        "section_bounds": [0, .5]}}
    left = {**line(), "region": [.1, .1, .3, .12]}
    right = {**line(), "region": [.6, .1, .8, .12]}
    assert local.anchored_region(field, [left, right])["region"][2] == .5
    assert local.anchored_region(field, [left, left]) is None
    assert local.anchored_region(field, [right]) is None


def test_overlapping_crop_duplicates_are_removed_but_distinct_rows_survive():
    first = {**line("repeated row"), "block_label": "spotting",
             "words": [{"text": "repeated row", "region": [.1, .1, .8, .2]}]}
    second = {**first, "region": [.1, .3, .8, .4]}
    assert local.refined_observations([], [first, first, second], []) == [first, second]
    block_rows = [line("same row"), line("same row")]
    assert local.refined_observations(block_rows, [], []) == block_rows
    assert local.refined_observations(block_rows, [line("new")], [])[:2] == block_rows


def test_semantic_candidate_completeness_does_not_remove_spatial_approval_blocks():
    data, spec = payload(), definition()
    data["fields"]["invoice"].update(value="A-1", observed="A-1", validity="VALID")
    spec["tables"] = [{"name": "detail", "fields": [{"name": "sku"}]}]
    data["tables"] = {"detail": [{"sku": {"value": "X-1", "observed": "X-1", "validity": "VALID"}}]}
    data["table_unparsed"] = {"detail": ["spatial parser could not map table"]}
    assert local._candidate_complete(data, spec)
    assert not local._sufficient(data, spec)
    assert data["table_unparsed"]["detail"]


def test_semantic_first_avoids_spotting_when_initial_observations_are_sufficient(tmp_path, monkeypatch):
    source = tmp_path / "sample.pdf"
    source.write_bytes(b"synthetic semantic ordering")
    calls = []

    def invoke(path, body, **kwargs):
        calls.append(path)
        if path == "/extract":
            return {"source_sha256": body["source_sha256"], "lines": [line()], "engine": {}}
        assert path == "/interpret"
        return {"source_sha256": body["source_sha256"], "observations_sha256": body["observations_sha256"],
            "semantics": {"fields": {"invoice": {"observed": "A-1", "evidence_id": 0}}, "tables": {}}, "engine": {}}

    monkeypatch.setattr(local, "_invoke", invoke)
    result = local.run_local(source, definition(), {"max_retries": 2},
        legacy_runner=lambda *args, **kwargs: {"payload": payload(), "engine": {}}, check=lambda _: None)
    assert calls == ["/extract", "/interpret"]
    assert result["engine"]["focused_reextraction"] == []
    assert result["payload"]["inference_observation_history"][0]["fields"]["invoice"]["validity"] == "MISSING"


def test_failed_semantic_stage_preserves_initial_candidate_without_success(tmp_path, monkeypatch):
    source = tmp_path / "sample.pdf"
    source.write_bytes(b"synthetic failure evidence")

    def invoke(path, body, **kwargs):
        if path == "/extract":
            return {"source_sha256": body["source_sha256"], "lines": [line()], "engine": {"paddle_revision": "pinned"}}
        raise OperationError(422, "DOCUMENT_LLM_CONTEXT_LIMIT", "fixed safe diagnostic")

    monkeypatch.setattr(local, "_invoke", invoke)
    with pytest.raises(OperationError) as caught:
        local.run_local(source, definition(), {"max_retries": 1},
            legacy_runner=lambda *args, **kwargs: {"payload": payload(), "engine": {}}, check=lambda _: None)
    assert caught.value.code == "DOCUMENT_LLM_CONTEXT_LIMIT"
    evidence = caught.value.details["local_inference"]
    assert evidence["failed_stage"] == "INTERPRET"
    assert evidence["initial_candidate"]["payload"]["fields"]["invoice"]["validity"] == "MISSING"
    assert evidence["completed_stages"][0]["stage"] == "PADDLE_INITIAL"


def test_real_phase_progress_does_not_regress_after_semantic_then_refine(tmp_path, monkeypatch):
    source = tmp_path / "sample.pdf"
    source.write_bytes(b"synthetic progress ordering")
    spec, observed_progress = definition(), []
    spec["fields"][0]["visual"] = {"page": 1, "region": [.1, .1, .8, .2], "kind": "REGION_CONTEXT"}
    calls = []

    def invoke(path, body, **kwargs):
        calls.append(path)
        kwargs["check"](65 if path == "/interpret" else 25)
        if path == "/interpret":
            return {"source_sha256": body["source_sha256"], "observations_sha256": body["observations_sha256"],
                "semantics": {"fields": {"invoice": {"observed": None, "evidence_id": None}}, "tables": {}}, "engine": {}}
        return {"source_sha256": body["source_sha256"], "lines": [line("fixed" if path == "/refine" else "unclear")], "engine": {}}

    def runner(*args, **kwargs):
        data = payload()
        if kwargs.get("candidate_lines", [{}])[0].get("text") == "fixed":
            data["fields"]["invoice"].update(value="A-1", observed="A-1", validity="VALID")
        kwargs["check"](10)
        return {"payload": data, "engine": {}}

    monkeypatch.setattr(local, "_invoke", invoke)
    local.run_local(source, spec, {"max_retries": 1}, legacy_runner=runner, check=observed_progress.append)
    assert calls == ["/extract", "/interpret", "/refine"]
    assert observed_progress == sorted(observed_progress)
    assert 65 in observed_progress and observed_progress[-1] == 100


@pytest.mark.parametrize("status", [[], {"phase": []}])
def test_malformed_status_does_not_abort_the_bounded_active_request(monkeypatch, status):
    released = threading.Event()

    def request(path, *args, **kwargs):
        if path.startswith("/status/"):
            return status
        assert released.wait(timeout=1)
        return {"completed": True}

    monkeypatch.setattr(local, "_request", request)
    result = local._invoke("/extract", {"request_id": "a" * 32}, timeout=30,
        started=time.monotonic(), check=lambda _: released.set())
    assert result == {"completed": True}


def test_semantic_table_alternatives_survive_a_following_spatial_remap(tmp_path, monkeypatch):
    source = tmp_path / "sample.pdf"
    source.write_bytes(b"synthetic disputed rows")
    spec = definition()
    spec["tables"] = [{"name": "detail", "fields": [{"name": "sku", "logical_type": "STRING"}]}]
    observations = [{**line("A-1 SKU-A SKU-B SKU-C"), "block_label": "table"}]
    semantic_calls = []

    def invoke(path, body, **kwargs):
        if path != "/interpret":
            return {"source_sha256": body["source_sha256"], "lines": observations, "engine": {}}
        sku = "SKU-B" if not semantic_calls else "SKU-C"
        semantic_calls.append(sku)
        return {"source_sha256": body["source_sha256"], "observations_sha256": body["observations_sha256"],
            "semantics": {"fields": {"invoice": {"observed": "A-1", "evidence_id": 0}},
                "tables": {"detail": [{"sku": {"observed": sku, "evidence_id": 0}}]}}, "engine": {}}

    def runner(*args, **kwargs):
        data = payload()
        data["native_incomplete_pages"] = [1]
        data["tables"] = {"detail": [{"sku": {"value": "SKU-A", "observed": "SKU-A", "validity": "VALID"}}]}
        data["table_unparsed"] = {"detail": ["unmapped spatial evidence"]}
        return {"payload": data, "engine": {}}

    monkeypatch.setattr(local, "_invoke", invoke)
    result = local.run_local(source, spec, {"max_retries": 1}, legacy_runner=runner, check=lambda _: None)
    history = [entry for entry in result["payload"]["inference_observation_history"] if entry["stage"] == "INTERPRET"]
    assert [entry["semantic_candidates"]["tables"]["detail"][0]["sku"]["value"] for entry in history] == ["SKU-B", "SKU-C"]
    assert all("TABLE:detail" in entry["semantic_conflicts"] for entry in history)
    assert result["payload"]["tables"]["detail"][0]["sku"]["value"] == "SKU-A"


def test_oversized_audit_history_fails_explicitly():
    with pytest.raises(OperationError) as caught:
        local._append_history([], {"observation": "x" * (8 * 1024**2)})
    assert caught.value.code == "DOCUMENT_LOCAL_AUDIT_LIMIT"
