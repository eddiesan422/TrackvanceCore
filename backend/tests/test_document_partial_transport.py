"""Synthetic HTTP transports only; no real server, model, OCR, or external calls."""
import asyncio
import threading
import time

import httpx
import pytest

from trackvance import document_local_inference as local
from trackvance.document_inference_completion import AttemptBudget, DocumentDeadlineExpired
from trackvance.operations_common import OperationError


def transport(monkeypatch, handler):
    monkeypatch.setenv("TRACKVANCE_IDP_LOCAL_URL", "http://localhost:3288")
    real_client, real_request = httpx.AsyncClient, local._request
    monkeypatch.setattr(local.httpx, "AsyncClient", lambda **kwargs: real_client(transport=httpx.MockTransport(handler), **kwargs))
    calls = []
    def request(path, body=None, **kwargs):
        calls.append(path)
        if path.startswith("/status/"):
            return {"phase": "PADDLE"}
        if path == "/cancel":
            return {"cancelled": True}
        return real_request(path, body, **kwargs)
    monkeypatch.setattr(local, "_request", request)
    return calls


@pytest.mark.parametrize("path,optional,expected", [
    ("/refine", True, local.OptionalInferenceTimeout),
    ("/interpret", True, local.OptionalInferenceTimeout),
    ("/extract", False, local.RemoteInferenceTimeout),
])
def test_fixed_remote_timeout_has_typed_optional_origin_only(monkeypatch, path, optional, expected):
    transport(monkeypatch, lambda request: httpx.Response(504, json={"error": "INFERENCE_TIMEOUT"}))
    with pytest.raises(expected) as caught:
        local._invoke(path, {"request_id": "a" *32}, timeout=30, started=time.monotonic(),
            check=lambda _: None, optional=optional)
    assert type(caught.value) is expected


def test_optional_absolute_budget_cancels_stream_and_joins_transport(monkeypatch):
    closed = threading.Event()
    class SlowStream(httpx.AsyncByteStream):
        async def __aiter__(self):
            try:
                await asyncio.sleep(60)
                yield b"{}"
            finally:
                closed.set()
        async def aclose(self):
            closed.set()
    calls = transport(monkeypatch, lambda request: httpx.Response(200, stream=SlowStream()))
    started = time.monotonic()
    with pytest.raises(local.OptionalInferenceTimeout):
        local._invoke("/refine", {"request_id": "a" *32}, timeout=30, started=started,
            check=lambda _: None, phase_deadline=started + .1, optional=True)
    assert time.monotonic() - started < 2
    assert closed.is_set() and calls.count("/cancel") == 1


def test_callback_timeout_is_never_reclassified_and_no_rpc_is_started(monkeypatch):
    calls = transport(monkeypatch, lambda request: pytest.fail("callback failed before RPC"))
    original = OperationError(422, "DOCUMENT_TIMEOUT", "Mandatory worker callback timeout")
    def check(_):
        raise original
    with pytest.raises(OperationError) as caught:
        local._invoke("/refine", {"request_id": "a" *32}, timeout=30, started=time.monotonic(),
            check=check, optional=True)
    assert caught.value is original and not calls


def test_callback_cancellation_during_stream_stops_transport_preserving_exact_error(monkeypatch):
    reading, closed = threading.Event(), threading.Event()
    class SlowStream(httpx.AsyncByteStream):
        async def __aiter__(self):
            try:
                reading.set()
                await asyncio.sleep(60)
                yield b"{}"
            finally:
                closed.set()
        async def aclose(self):
            closed.set()
    calls = transport(monkeypatch, lambda request: httpx.Response(200, stream=SlowStream()))
    original = OperationError(409, "DOCUMENT_CANCELLED", "Synthetic cancelled actor")
    def check(_):
        if reading.is_set():
            raise original
    started = time.monotonic()
    with pytest.raises(OperationError) as caught:
        local._invoke("/refine", {"request_id": "a" *32}, timeout=30, started=started, check=check, optional=True)
    assert caught.value is original and closed.is_set()
    assert time.monotonic() - started < 2 and calls.count("/cancel") == 1


def test_global_deadline_expired_before_optional_request_is_fatal(monkeypatch):
    calls = transport(monkeypatch, lambda request: pytest.fail("expired request started"))
    started = time.monotonic()
    budget = AttemptBudget(30, started=started -31)
    with pytest.raises(DocumentDeadlineExpired):
        local._invoke("/refine", {"request_id": "a" *32}, timeout=600, started=started,
            check=lambda _: None, budget=budget, phase_deadline=started +10, optional=True)
    assert not calls


def test_malformed_semantic_http_response_remains_fatal(monkeypatch):
    transport(monkeypatch, lambda request: httpx.Response(200, content=b"{not-json"))
    with pytest.raises(OperationError) as caught:
        local._invoke("/interpret", {"request_id": "a" *32}, timeout=30, started=time.monotonic(),
            check=lambda _: None, optional=True)
    assert caught.value.code == "DOCUMENT_LOCAL_INFERENCE_UNAVAILABLE"
    assert not isinstance(caught.value, local.OptionalInferenceTimeout)
