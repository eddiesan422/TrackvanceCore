"""The coordinator accepts only its admission's terminal and complete bytes."""
import copy
import time

import pytest

from trackvance import document_local_inference as local
from trackvance.operations_common import OperationError


def request():
    return {"request_id": "a" * 32, "source_sha256": "b" * 64,
        "configuration": {"timeout_seconds": 30}, "definition": {"fields": [], "tables": []}}


def terminal(body, *, status="COMPLETE", released=True):
    return {**local._rpc_binding("/extract", body), "status": status, "resources_released": released}


def invoke(body, *, optional=False):
    return local._invoke("/extract", body, timeout=30, started=time.monotonic(), check=lambda _: None, optional=optional)


def test_rpc_uses_distinct_nonce_and_preserves_frozen_inputs(monkeypatch):
    body = request()
    before, calls = copy.deepcopy(body), []

    def remote(path, value=None, **kwargs):
        if path.startswith("/status/"):
            return {"phase": "QUEUED"}
        calls.append((path, value))
        return {"source_sha256": value["source_sha256"], "engine": {"rpc_execution": terminal(value)}}

    monkeypatch.setattr(local, "_request", remote)
    result = invoke(body)
    assert body == before and len(calls) == 1
    actual = calls[0][1]
    assert actual["admission_id"] != actual["request_id"] and actual["rpc_contract"] == local.RPC_VERSION
    assert result["engine"]["rpc_execution"]["admission_id"] == actual["admission_id"]


@pytest.mark.parametrize("key,value", [("admission_id", "c" * 32), ("request_sha256", "c" * 64),
    ("configuration_sha256", "c" * 64), ("resources_released", False)])
def test_rpc_rejects_a_different_admission_or_unclosed_result(monkeypatch, key, value):
    captured = []

    def remote(path, body=None, **kwargs):
        if path == "/extract":
            proof = terminal(body)
            proof[key] = value
            captured.append(proof)
            return {"source_sha256": body["source_sha256"], "engine": {"rpc_execution": proof}}
        if path.startswith("/status/"):
            return {"phase": "TERMINAL", "execution": captured[-1]} if captured else {"phase": "QUEUED"}
        return {"cancelled": False}

    monkeypatch.setattr(local, "_request", remote)
    with pytest.raises(OperationError) as failure:
        invoke(request())
    assert failure.value.code in {"DOCUMENT_LOCAL_INFERENCE_CONTRACT", "DOCUMENT_LOCAL_RUNTIME_UNVERIFIED"}


def test_transport_failure_recovers_only_exact_complete_saved_result(monkeypatch):
    calls, saved = [], {}

    def remote(path, body=None, **kwargs):
        if path.startswith("/status/"):
            return {"phase": "QUEUED"}
        calls.append(path)
        if path == "/extract":
            result = {"source_sha256": body["source_sha256"], "lines": [{"text": "synthetic raw"}], "engine": {}}
            saved.update(result=result, execution=terminal(body), result_sha256=local._rpc_hash(result))
            raise OperationError(503, "DOCUMENT_LOCAL_INFERENCE_UNAVAILABLE", "synthetic lost response")
        if path == "/recover":
            assert body["admission_id"] == saved["execution"]["admission_id"]
            return copy.deepcopy(saved)
        pytest.fail("completed durable recovery must not cancel or rerun")

    monkeypatch.setattr(local, "_request", remote)
    result = invoke(request())
    assert calls == ["/extract", "/recover"]
    assert result["engine"]["rpc_recovery"]["result_sha256"] == saved["result_sha256"]


@pytest.mark.parametrize("mutate", ["result_bytes", "source", "admission"])
def test_forged_recovery_never_returns_a_candidate(monkeypatch, mutate):
    saved = {}

    def remote(path, body=None, **kwargs):
        if path == "/extract":
            result = {"source_sha256": body["source_sha256"], "engine": {}}
            saved.update(result=result, execution=terminal(body), result_sha256=local._rpc_hash(result))
            raise OperationError(503, "DOCUMENT_LOCAL_INFERENCE_UNAVAILABLE", "synthetic lost response")
        if path == "/recover":
            value = copy.deepcopy(saved)
            if mutate == "result_bytes":
                value["result"]["invented"] = True
            elif mutate == "source":
                value["result"]["source_sha256"] = "c" * 64
                value["result_sha256"] = local._rpc_hash(value["result"])
            else:
                value["execution"]["admission_id"] = "c" * 32
            return value
        if path.startswith("/status/"):
            return {"phase": "TERMINAL", "execution": saved.get("execution")}
        return {"cancelled": False}

    monkeypatch.setattr(local, "_request", remote)
    with pytest.raises(OperationError):
        invoke(request())


def test_optional_service_timeout_preserves_origin_and_does_not_recover(monkeypatch):
    calls, saved = [], {}

    def remote(path, body=None, **kwargs):
        if path == "/extract":
            execution = terminal(body, status="TIMEOUT")
            saved.update(execution=execution)
            raise local.RemoteInferenceTimeout(origin="SERVICE", execution=execution)
        if path.startswith("/status/"):
            return {"phase": "TERMINAL", **saved}
        calls.append((path, body))
        assert path != "/recover"
        return {"cancelled": False}

    monkeypatch.setattr(local, "_request", remote)
    with pytest.raises(local.OptionalInferenceTimeout) as failed:
        invoke(request(), optional=True)
    assert failed.value.timeout_origin == "SERVICE"
    assert failed.value.details["timeout_origin"] == "SERVICE"
    assert failed.value.details["rpc_execution"]["status"] == "TIMEOUT"
    assert calls[0][0] == "/cancel" and "admission_id" in calls[0][1]


def test_forged_error_receipt_is_not_preserved_as_verified_closure(monkeypatch):
    saved = {}

    def remote(path, body=None, **kwargs):
        if path == "/extract":
            execution = terminal(body, status="TIMEOUT")
            execution["admission_id"] = "c" * 32
            saved.update(execution=execution)
            raise local.RemoteInferenceTimeout(origin="SERVICE", execution=execution)
        if path.startswith("/status/"):
            return {"phase": "TERMINAL", **saved}
        return {"cancelled": False}

    monkeypatch.setattr(local, "_request", remote)
    with pytest.raises(OperationError) as failed:
        invoke(request())
    assert failed.value.code == "DOCUMENT_LOCAL_INFERENCE_CONTRACT"
    assert failed.value.details["rpc_execution_unverified"] is True
    assert "rpc_execution" not in failed.value.details


def test_candidate_failure_diagnostic_preserves_bound_runtime_receipt_and_origin():
    receipt = {"request_id": "a" * 32, "status": "TIMEOUT", "resources_released": True}
    error = local.RemoteInferenceTimeout(origin="SERVICE", execution=receipt)
    local._failure_details(error, stage="INTERPRET", source_sha256="b" * 64,
        initial=None, latest=None, history=[], stages=[])
    assert error.details["rpc_execution"] == receipt
    assert error.details["timeout_origin"] == "SERVICE"
    assert error.details["local_inference"]["failed_stage"] == "INTERPRET"


def test_failed_transport_join_preserves_original_service_timeout_and_rejects_result(monkeypatch):
    observed = []

    class Future:
        def done(self):
            return False

        def result(self, timeout):
            raise local.FutureTimeout()

    class Executor:
        def __init__(self, **kwargs):
            pass

        def submit(self, *args, **kwargs):
            return Future()

        def shutdown(self, **kwargs):
            pass

    def check(progress):
        observed.append(progress)
        if len(observed) == 2:
            raise local.RemoteInferenceTimeout(origin="SERVICE")

    monkeypatch.setattr(local, "ThreadPoolExecutor", Executor)
    monkeypatch.setattr(local, "_request", lambda *args, **kwargs: {"phase": "PADDLE"})
    with pytest.raises(OperationError) as failed:
        local._invoke("/extract", {"request_id": "a" * 32}, timeout=30,
            started=time.monotonic(), check=check)
    assert failed.value.code == "DOCUMENT_LOCAL_TRANSPORT_CLEANUP_FAILED"
    assert failed.value.details["original_error_code"] == "DOCUMENT_TIMEOUT"
    assert failed.value.details["timeout_origin"] == "SERVICE"
    assert failed.value.details["cleanup_error_code"] == failed.value.code
