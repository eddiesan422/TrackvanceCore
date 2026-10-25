"""One caller budget and proved reader/model quiescence; no model/runtime imports."""
from __future__ import annotations

import http.client
import io
import math
import queue
import select
import socket
import threading
import time


class UnsafeLifecycle(ValueError):
    """Admission must remain quarantined until an operator proves recovery."""


class Budget:
    def __init__(self, configured_seconds, requested_seconds=None, *, clock=time.monotonic, started=None):
        if type(configured_seconds) not in (int, float) or not math.isfinite(configured_seconds) or not 30 <= configured_seconds <= 1800:
            raise ValueError("ADMISSION_CONFIGURATION")
        duration = configured_seconds if requested_seconds is None else requested_seconds
        if type(duration) not in (int, float) or not math.isfinite(duration) or not 0 < duration <= configured_seconds:
            raise ValueError("REQUEST_BUDGET_INVALID")
        self.clock, self.started, self.duration = clock, clock() if started is None else started, duration
        if type(self.started) not in (int, float) or not math.isfinite(self.started) or self.started > clock():
            raise ValueError("REQUEST_BUDGET_INVALID")
        self.deadline = self.started + duration
        # This is part of the caller's phase budget, never validation/commit time.
        self.cleanup_reserve = min(5.0, duration * .1)
        self.work_deadline = self.deadline - self.cleanup_reserve

    def remaining(self, *, cleanup=False):
        value = (self.deadline if cleanup else self.work_deadline) - self.clock()
        if value <= 0:
            raise TimeoutError("INFERENCE_TIMEOUT")
        return value

    def join_remaining(self):
        return max(0.0, self.deadline - self.clock())


class ConnectionHandle:
    """Own one non-reused connection and its socket through response detachment."""
    def __init__(self):
        self.lock = threading.Lock()
        self.connection, self.original_close, self.socket = None, None, None
        self.closed, self.close_failed = threading.Event(), threading.Event()

    def set(self, connection):
        with self.lock:
            self.connection, self.original_close = connection, connection.close
            # HTTPConnection.getresponse may call close while HTTPResponse.fp
            # still owns a read. Keep that actual socket open for cancellation;
            # this connection is never reused and finish() performs real close.
            connection.close = self.defer_close

    def defer_close(self):
        with self.lock:
            if self.connection.sock is not None:
                self.socket = self.connection.sock
                self.connection.sock = None

    def connected(self, connection):
        with self.lock:
            self.socket = connection.sock

    def close(self):
        self.closed.set()
        with self.lock:
            connected = self.socket if self.socket is not None else self.connection.sock if self.connection else None
            if connected is not None:
                try:
                    connected.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass

    def finish(self):
        # Called by the reader only after its response read/finally has exited.
        with self.lock:
            finished = False
            try:
                if self.original_close is not None:
                    self.original_close()
                if self.socket is not None:
                    self.socket.close()
                finished = True
            finally:
                if not finished:
                    self.close_failed.set()



class ResponseSocket:
    """Expose only HTTPResponse's binary file API with bounded raw reads.

    A timed-out SocketIO cannot be retried, and Windows shutdown need not wake
    an in-progress recv. Poll before a short raw recv instead; keep idle peers
    valid until the original absolute deadline, without resetting any budget.
    """
    def __init__(self, connected, budget, handle, cleanup):
        self.connected, self.budget, self.handle, self.cleanup = connected, budget, handle, cleanup

    def makefile(self, mode):
        if mode != "rb":
            raise ValueError("GPU_RESPONSE_STREAM_INVALID")
        source = self
        class Reader(io.RawIOBase):
            def readable(self):
                return True

            def readinto(self, target):
                if not target:
                    return 0
                while True:
                    if source.handle.closed.is_set():
                        raise ValueError("CANCELLED")
                    remaining = source.budget.remaining(cleanup=source.cleanup)
                    ready, _, _ = select.select([source.connected], [], [], min(.05, remaining))
                    if ready:
                        # Readiness can race with socket shutdown. This timeout
                        # bounds that final recv; it is caught before BufferedIO.
                        source.connected.settimeout(min(.05, remaining))
                        try:
                            return source.connected.recv_into(target)
                        except TimeoutError:
                            continue
        return io.BufferedReader(Reader())



class _ReaderOutcome:
    """Publish one owned thread result after final close, retaining normal errors.

    The context suppresses Exception only after handing its exact object to the
    supervisor. It neither catches BaseException nor leaks thread tracebacks.
    """
    def __init__(self, answers):
        self.answers, self.value = answers, None

    def __enter__(self):
        return self

    def __exit__(self, _kind, error, _traceback):
        if error is None:
            self.answers.put((True, self.value))
        elif isinstance(error, Exception):
            self.answers.put((False, error))
            return True
        return False


def read_joined(operation, budget, cancelled, publish, withdraw, *, cleanup=False,
                connection_factory=http.client.HTTPConnection):
    """Bound absolute time even when a peer dribbles data or delays headers."""
    handle, answers = ConnectionHandle(), queue.Queue(maxsize=1)
    publish(handle)

    def read():
        connection = None
        with _ReaderOutcome(answers) as terminal:
            try:
                connection = connection_factory("idp-ollama", 11434, timeout=budget.remaining(cleanup=cleanup))
                handle.set(connection)
                connect = connection.connect
                response_class = getattr(connection, "response_class", None)
                if response_class is not None:
                    def joined_response(connected, *args, **kwargs):
                        return response_class(ResponseSocket(connected, budget, handle, cleanup), *args, **kwargs)
                    connection.response_class = joined_response
                def guarded_connect():
                    connect()
                    # HTTPResponse may later detach HTTPConnection.sock on Connection:close.
                    handle.connected(connection)
                    if handle.closed.is_set() or (not cleanup and cancelled()):
                        handle.close()
                        raise ValueError("CANCELLED")
                connection.connect = guarded_connect
                if handle.closed.is_set():
                    raise ValueError("CANCELLED")
                terminal.value = operation(connection)
            finally:
                if connection is not None:
                    handle.finish()

    reader = threading.Thread(target=read, name="idp-owned-upstream", daemon=True)
    reader.start()
    answer, failure = None, None
    try:
        while answer is None:
            if not cleanup and cancelled():
                raise ValueError("CANCELLED")
            remaining = budget.remaining(cleanup=cleanup)
            try:
                answer = answers.get(timeout=min(.05, remaining))
            except queue.Empty:
                pass
    except (ValueError, TimeoutError) as error:
        failure = error
    finally:
        handle.close()
        reader.join(timeout=budget.join_remaining())
        if reader.is_alive():
            # Keep the published handle: an empty registry must never fake a join.
            raise UnsafeLifecycle("GPU_READER_QUIESCENCE_UNVERIFIED")
        if handle.close_failed.is_set():
            raise UnsafeLifecycle("GPU_READER_QUIESCENCE_UNVERIFIED")
        withdraw(handle)
    if failure is not None:
        raise failure
    if answer[0] is False:
        if not isinstance(answer[1], (OSError, ValueError, TypeError, KeyError, http.client.HTTPException)):
            # Keep the exact exception as a private cause; only the fixed code
            # crosses the gateway HTTP error boundary, never its message/path.
            raise ValueError("LLM_FAILURE") from answer[1]
        raise answer[1]
    return answer[1]


def quiet_ollama(budget, request, pinned, quiescent, *, cleanup=False, sleep=time.sleep):
    """Only a reaped/closed owner may unload its exact pinned resident model."""
    if not quiescent():
        raise UnsafeLifecycle("GPU_OWNER_QUIESCENCE_UNVERIFIED")
    started, polls, unloaded = budget.clock(), 0, False
    while True:
        budget.remaining(cleanup=cleanup)
        value = request("/ps", None, cleanup)
        models = value.get("models") if isinstance(value, dict) else None
        if not isinstance(models, list) or len(models) > 16:
            raise UnsafeLifecycle("GPU_RESIDENCY_UNVERIFIED")
        polls += 1
        if not models:
            return {"status": "EMPTY_RESIDENCY_OBSERVED", "polls": polls, "owned_unload_requested": unloaded,
                "elapsed_seconds": round(budget.clock() - started, 4)}
        if len(models) != 1 or any(not isinstance(item, dict) or item.get("name") != pinned["model"]
                or item.get("model", item.get("name")) != pinned["model"]
                or item.get("digest") != pinned["manifest_sha256"] for item in models):
            raise UnsafeLifecycle("GPU_FOREIGN_RESIDENCY")
        if not unloaded:
            tags = request("/tags", None, cleanup)
            declared = tags.get("models") if isinstance(tags, dict) else None
            matches = [item for item in declared if isinstance(item, dict) and item.get("name") == pinned["model"]] if isinstance(declared, list) else []
            if len(matches) != 1 or matches[0].get("digest") != pinned["manifest_sha256"]:
                raise UnsafeLifecycle("GPU_PIN_UNVERIFIED")
            reply = request("/generate", {"model": pinned["model"], "prompt": "", "keep_alive": 0, "stream": False}, cleanup)
            if (not isinstance(reply, dict) or reply.get("model") != pinned["model"]
                    or reply.get("done") is not True or reply.get("done_reason") != "unload"):
                raise UnsafeLifecycle("GPU_UNLOAD_UNVERIFIED")
            unloaded = True
        sleep(min(.05, budget.remaining(cleanup=cleanup)))
