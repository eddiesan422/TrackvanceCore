"""Bounded recognition stream; only the owning coordinator calls its sink.

The network reader cannot use a database session. Its bounded one-event queue is
acknowledged only after source/binding validation and the coordinator's durable
write. A terminal verdict requires both remote and local quiescence.
"""
from __future__ import annotations

import asyncio
import base64
import copy
import hashlib
import json
import os
import queue
import re
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeout
from typing import Any

import httpx

from .document_inference_completion import (
    DocumentCheckpointFailure,
    DocumentDeadlineExpired,
    canonical_hash,
)
from .operations_common import OperationError

MAX_SOURCE = 32 * 1024**2
MAX_EVENT = 24 * 1024**2
MAX_STREAM = 64 * 1024**2
ARTIFACT_KINDS = {"PADDLE_PAGE_JSON", "PADDLE_TABLE_HTML", "PAGE_IMAGE_PNG", "QWEN_REPLY_JSON",
    "QWEN_SELECTION_JSON", "QWEN_STREAM_NDJSON", "QWEN_REQUEST_JSON"}
TRANSPORT_UNAVAILABLE = False
STREAM_ERRORS = (OperationError, DocumentCheckpointFailure, DocumentDeadlineExpired, ValueError, TypeError,
    KeyError, OSError, RuntimeError, AssertionError, asyncio.CancelledError, httpx.HTTPError)


def transport_available():
    return not TRANSPORT_UNAVAILABLE


def _invalid():
    return OperationError(422, "DOCUMENT_RECOGNITION_CONTRACT", "La respuesta de reconocimiento perdió integridad o procedencia.")


def _json(raw):
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError("DUPLICATE_JSON_KEY")
            result[key] = value
        return result
    def constant(_value):
        raise ValueError("NONFINITE_JSON")
    return json.loads(raw, object_pairs_hook=pairs, parse_constant=constant)


def _validate_visual_request(raw, event):
    if len(raw) > 256 * 1024:
        raise _invalid()
    try:
        value = _json(raw)
        if (not isinstance(value, dict) or set(value) != {"version", "strategy", "request_sha256", "source_sha256",
                "definition_sha256", "configuration_sha256", "request_without_image_bytes", "transport_options", "image"}
                or value["version"] != "TRACKVANCE_QWEN_REQUEST_V1"
                or value["strategy"] != "QWEN_VISUAL"
                or value["transport_options"] != {"stream": True}
                or any(value[key] != event[key] for key in ("source_sha256", "definition_sha256", "configuration_sha256"))
                or not isinstance(value["request_sha256"], str) or not re.fullmatch(r"[a-f0-9]{64}", value["request_sha256"])):
            raise _invalid()
        body, image = value["request_without_image_bytes"], value["image"]
        if (not isinstance(body, dict) or set(body) != {"model", "think", "keep_alive", "format", "truncate", "shift", "messages", "options"}
                or not isinstance(body["messages"], list) or len(body["messages"]) != 2
                or [message.get("role") for message in body["messages"]] != ["system", "user"]
                or any(set(message) != {"role", "content"} or not isinstance(message["content"], str) for message in body["messages"])
                or not isinstance(image, dict) or set(image) != {"sha256", "source_page", "width", "height", "query_region", "geometry_kind", "transformations"}
                or not isinstance(image["sha256"], str) or not re.fullmatch(r"[a-f0-9]{64}", image["sha256"])
                or image["source_page"] != event["page_one_based"]
                or any(type(image[key]) is not int or not 1 <= image[key] <= 32768 for key in ("width", "height"))
                or image["query_region"] != [0, 0, 1, 1] or image["geometry_kind"] != "QUERY_REGION"
                or not isinstance(image["transformations"], list) or len(image["transformations"]) > 32):
            raise _invalid()
    except (ValueError, TypeError, KeyError, AttributeError):
        raise _invalid() from None


def validate_event(event, binding, sequence, *, status_poll=False):
    if not isinstance(event, dict) or "durable_refs" in event:
        raise _invalid()
    kind = event.get("kind")
    base_keys = {"version", "sequence", "kind", "request_id", "attempt_id", "source_sha256",
        "configuration_sha256", "definition_sha256", "stage", "page_one_based", "expected_pages", "payload", "event_sha256"}
    terminal_keys = {"status", "resources_released", "emitted_pages", "completed_pages", "pending_pages"}
    if set(event) != base_keys | (terminal_keys if kind == "TERMINAL" else set()):
        raise _invalid()
    if (event.get("version") != 1 or type(event.get("sequence")) is not int or event["sequence"] < 1
            or not status_poll and event["sequence"] != sequence
            or kind not in {"PHASE", "PAGE", "TERMINAL"}
            or any(event.get(key) != value for key, value in binding.items())
            or event.get("event_sha256") != canonical_hash({key: value for key, value in event.items() if key != "event_sha256"})
            or not isinstance(event.get("stage"), str) or not re.fullmatch(r"[A-Z0-9_]{1,80}", event["stage"])
            or not isinstance(event.get("payload"), dict)):
        raise _invalid()
    expected, page = event.get("expected_pages"), event.get("page_one_based")
    if expected is not None and (type(expected) is not int or not 1 <= expected <= 50):
        raise _invalid()
    if page is not None and (type(page) is not int or not 1 <= page <= 50 or expected is not None and page > expected):
        raise _invalid()
    if kind == "PAGE" or kind == "PHASE" and "artifacts" in event["payload"]:
        artifacts = event["payload"].get("artifacts")
        if expected is None or page is None or not isinstance(artifacts, list) or not 1 <= len(artifacts) <= 1000:
            raise _invalid()
        if kind == "PHASE" and (event["stage"] != "QWEN_GENERATION" or event["payload"].get("phase") != "QWEN_GENERATION"
                or len(artifacts) != 1 or not isinstance(artifacts[0], dict) or artifacts[0].get("kind") != "QWEN_REQUEST_JSON"):
            raise _invalid()
        for item in artifacts:
            if not isinstance(item, dict) or set(item) != {"kind", "media_type", "sha256", "bytes", "content_b64"}:
                raise _invalid()
            if (item["kind"] not in ARTIFACT_KINDS or not isinstance(item["media_type"], str)
                    or len(item["media_type"]) > 128 or "\r" in item["media_type"] or "\n" in item["media_type"]
                    or type(item["bytes"]) is not int or not 0 <= item["bytes"] <= 16 * 1024**2
                    or not isinstance(item["sha256"], str) or not re.fullmatch(r"[a-f0-9]{64}", item["sha256"])
                    or not isinstance(item["content_b64"], str)):
                raise _invalid()
            try:
                raw = base64.b64decode(item["content_b64"], validate=True)
            except ValueError:
                raise _invalid() from None
            if len(raw) != item["bytes"] or hashlib.sha256(raw).hexdigest() != item["sha256"]:
                raise _invalid()
            if item["kind"] == "QWEN_REQUEST_JSON":
                if item["media_type"] != "application/json":
                    raise _invalid()
                _validate_visual_request(raw, event)
    if kind == "TERMINAL":
        if (event.get("status") not in {"COMPLETE", "PARTIAL", "CANCELLED", "FAILED", "TIMEOUT"}
                or type(event.get("resources_released")) is not bool or page is not None):
            raise _invalid()
        for name in ("emitted_pages", "completed_pages", "pending_pages"):
            values = event.get(name)
            if (not isinstance(values, list) or len(values) > 50 or len(set(values)) != len(values)
                    or any(type(value) is not int or not 1 <= value <= (expected or 50) for value in values)):
                raise _invalid()
        if event["status"] == "COMPLETE" and (expected is None or event["completed_pages"] != list(range(1, expected + 1))
                or event["emitted_pages"] != event["completed_pages"] or event["pending_pages"]):
            raise _invalid()
    return event


async def _read(endpoint, body, seconds, control, events, rejected):
    from .document_local_inference import _decoded_response
    control.register(asyncio.get_running_loop(), asyncio.current_task())
    async with asyncio.timeout(seconds):
        async with httpx.AsyncClient(timeout=seconds, follow_redirects=False, trust_env=False) as client, \
                client.stream("POST", endpoint + "/recognize", json=body) as response:
            if response.status_code != 200:
                raw = bytearray()
                async for chunk in response.aiter_bytes():
                    raw.extend(chunk)
                    if len(raw) > 4096:
                        raise _invalid()
                value = _json(raw)
                if isinstance(value, dict) and value.get("error") in {
                    "GPU_QUEUE_FULL", "CONFIGURED_INFERENCE_CAPACITY", "MODEL_NOT_PINNED", "MODEL_INTEGRITY",
                    "GPU_RUNTIME_UNAVAILABLE", "SOURCE_INTEGRITY", "REQUEST_LIMIT", "REQUEST_ID", "ADMISSION_CONFIGURATION"}:
                    rejected.set()  # A fixed pre-work rejection is not an admitted model job.
                return _decoded_response(raw, response.status_code)
            if response.headers.get("content-type") != "application/x-ndjson":
                raise _invalid()
            buffer, total, terminal = bytearray(), 0, False
            async for chunk in response.aiter_bytes():
                total += len(chunk)
                if total > MAX_STREAM:
                    raise _invalid()
                buffer.extend(chunk)
                while b"\n" in buffer:
                    raw, _, rest = buffer.partition(b"\n")
                    buffer = bytearray(rest)
                    if len(raw) > MAX_EVENT or not raw or terminal:
                        raise _invalid()
                    event, ack = _json(raw), threading.Event()
                    while True:
                        if control.cancelled:
                            raise asyncio.CancelledError()
                        try:
                            events.put_nowait((event, ack))
                            break
                        except queue.Full:
                            await asyncio.sleep(.01)
                    while not ack.is_set():
                        if control.cancelled:
                            raise asyncio.CancelledError()
                        await asyncio.sleep(.01)
                    terminal = isinstance(event, dict) and event.get("kind") == "TERMINAL"
                if len(buffer) > MAX_EVENT:
                    raise _invalid()
            if buffer or not terminal:
                raise _invalid()
    return True


def recognize_stream(source, definition, configuration, *, strategy, check, budget, on_event, attempt_context):
    """One model mechanism. Returned pages are original events plus trusted refs."""
    global TRANSPORT_UNAVAILABLE
    from .document_local_inference import _endpoint, _request, _RequestControl
    if TRANSPORT_UNAVAILABLE:
        raise OperationError(503, "DOCUMENT_LOCAL_CLEANUP_UNVERIFIED", "La inferencia local requiere comprobar el cierre del intento anterior.")
    if strategy not in {"QWEN_VISUAL", "PADDLE_STRUCTURED"}:
        raise _invalid()
    check(0)
    attempt = attempt_context.get("attempt_id") if isinstance(attempt_context, dict) else None
    if not isinstance(attempt, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,100}", attempt):
        raise _invalid()
    if source.stat().st_size > MAX_SOURCE:
        raise OperationError(422, "DOCUMENT_LOCAL_SOURCE_LIMIT", "El original excede el límite de reconocimiento local.")
    with source.open("rb") as stream:
        before = stream.fileno()
        original_stat = os.fstat(before)
        raw = stream.read(MAX_SOURCE + 1)
        after = os.fstat(before)
    if len(raw) > MAX_SOURCE or (original_stat.st_size, original_stat.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
        raise _invalid()
    identity = uuid.uuid4().hex
    binding = {"request_id": identity, "attempt_id": attempt, "source_sha256": hashlib.sha256(raw).hexdigest(),
        "configuration_sha256": canonical_hash(configuration), "definition_sha256": canonical_hash(definition)}
    for name in ("source_sha256", "configuration_sha256", "definition_sha256"):
        if attempt_context.get(name) is not None and attempt_context[name] != binding[name]:
            raise _invalid()
    remaining = budget.require(budget.extraction_deadline)
    body = {**binding, "filename": "document" + source.suffix.lower(), "source": base64.b64encode(raw).decode("ascii"),
        "strategy": strategy, "configuration": configuration, "definition": definition,
        "request_budget_seconds": remaining}
    health = _request("/health", timeout=min(2, remaining))
    if (not isinstance(health, dict) or health.get("ready") is not True
            or health.get("recognition", {}).get("contract") != "TRACKVANCE_RECOGNITION_V1"
            or strategy not in health.get("recognition", {}).get("strategies", [])):
        raise OperationError(503, "DOCUMENT_RECOGNITION_RUNTIME_UNAVAILABLE", "El runtime local no ofrece la estrategia de reconocimiento configurada.")
    # Metadata verification belongs to the same attempt; never reset its time.
    remaining = budget.require(budget.extraction_deadline)
    body["request_budget_seconds"] = remaining
    control, rejected = _RequestControl(), threading.Event()
    events: queue.Queue[tuple[dict[str, Any], threading.Event]] = queue.Queue(maxsize=1)
    executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="document-recognition-reader")
    future = executor.submit(asyncio.run, _read(_endpoint(), body, remaining, control, events, rejected))
    pages: list[dict[str, Any]] = []
    terminal: dict[str, Any] | None = None
    sequence, page_keys = 1, set()
    error = None
    remote_closed = False

    def accept(event, *, status_poll=False):
        nonlocal sequence, terminal, remote_closed
        validate_event(event, binding, sequence, status_poll=status_poll)
        if event["kind"] != "TERMINAL":
            budget.require(budget.extraction_deadline)
            check(0)  # Work checkpoints remain on this coordinator thread.
        elif on_event is None:
            check(0)
        # A durable terminal sink performs its own lease/attempt/actor check.
        # Reusing the work checkpoint here would reject cancellation or an
        # expired work budget before that authorized sink can preserve closure.
        if event["kind"] == "PAGE":
            key = (event["stage"], event["page_one_based"])
            if event["stage"] != strategy or key in page_keys:
                raise _invalid()
            page_keys.add(key)
        elif event["kind"] == "TERMINAL" and not status_poll:
            observed_pages = [item["page_one_based"] for item in pages]
            if (event["emitted_pages"] != observed_pages
                    or not set(event["completed_pages"]) <= set(event["emitted_pages"])
                    or event["expected_pages"] is not None and event["pending_pages"] != [
                        number for number in range(1, event["expected_pages"] + 1) if number not in event["completed_pages"]]):
                raise _invalid()
        event = copy.deepcopy(event)
        refs = on_event(copy.deepcopy(event)) if on_event is not None else None
        if refs is not None:
            if not isinstance(refs, dict):
                raise _invalid()
            event["durable_refs"] = refs
        if event["kind"] == "PAGE":
            pages.append(event)
        elif event["kind"] == "TERMINAL":
            terminal = event
            remote_closed = event["resources_released"] is True
        sequence = event["sequence"] + 1

    try:
        while True:
            try:
                event, ack = events.get(timeout=.05)
            except queue.Empty:
                if future.done():
                    future.result()
                    break
                budget.require(budget.extraction_deadline)
                check(0)
                continue
            try:
                accept(event)
            finally:
                ack.set()
        if terminal is None or not remote_closed:
            raise _invalid()
    except STREAM_ERRORS as caught:
        error = caught
    finally:
        if error is not None or terminal is None or not remote_closed:
            control.cancel()
            if not rejected.is_set():
                cleanup_duration = max(0, min(5, budget.deadline - budget.clock()))
                cleanup_until = time.monotonic() + cleanup_duration
                try:
                    left = cleanup_until - time.monotonic()
                    if left > 0:
                        _request("/cancel", {"request_id": identity}, timeout=min(1.5, left))
                except OperationError:
                    pass
                while time.monotonic() < cleanup_until and not remote_closed:
                    try:
                        value = _request("/status/" + identity, timeout=min(.5, cleanup_until - time.monotonic()))
                    except OperationError:
                        value = None  # Registration/cleanup can race a read-only poll.
                    try:
                        if isinstance(value, dict) and isinstance(value.get("terminal"), dict):
                            observed = value["terminal"]
                            validate_event(observed, binding, sequence, status_poll=True)
                            remote_closed = observed["resources_released"] is True
                            if remote_closed and terminal is None:
                                # A deadline/cancel terminal can be persisted during cleanup.
                                try:
                                    accept(observed, status_poll=True)
                                except STREAM_ERRORS as cleanup_error:
                                    error = error or cleanup_error
                            break
                    except (OperationError, ValueError, TypeError, KeyError):
                        break
                    time.sleep(min(.05, max(0, cleanup_until - time.monotonic())))
            else:
                remote_closed = True
        try:
            future.result(timeout=max(0, min(2, budget.deadline - budget.clock())))
        except FutureTimeout:
            TRANSPORT_UNAVAILABLE = True
        except STREAM_ERRORS as reader_error:
            error = error or reader_error
        executor.shutdown(wait=False, cancel_futures=True)
        if not remote_closed or not future.done():
            TRANSPORT_UNAVAILABLE = True
    if TRANSPORT_UNAVAILABLE:
        failure = OperationError(503, "DOCUMENT_LOCAL_CLEANUP_UNVERIFIED", "No se pudo comprobar el cierre del reconocimiento local.")
        failure.details = {"recognition": {"request_id": identity, "attempt_id": attempt,
            "source_sha256": binding["source_sha256"], "pages": [{key: copy.deepcopy(page.get(key))
                for key in ("page_one_based", "stage", "event_sha256", "durable_refs")} for page in pages], "terminal": terminal}}
        raise failure from error
    if error is not None:
        if isinstance(error, (DocumentDeadlineExpired, TimeoutError)) and terminal is not None and terminal["status"] != "COMPLETE":
            return {"pages": pages, "terminal": terminal, "engine": terminal["payload"].get("engine", {})}
        raise error
    if terminal is None:
        raise _invalid()
    return {"pages": pages, "terminal": terminal, "engine": terminal["payload"].get("engine", {})}
