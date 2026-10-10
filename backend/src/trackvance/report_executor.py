"""Coordinator-to-confined-process channel, bounded pipes and cancellation."""
from __future__ import annotations

import hashlib
import json
import logging
import queue
import subprocess
import sys
import threading
import time
from pathlib import Path

from .operations_common import OperationError
from .report_config import ReportLimits

_slots = threading.BoundedSemaphore(ReportLimits.configured().concurrency)
logger = logging.getLogger(__name__)


class _ExecutorProcessOwner:
    """Keep the Linux parent thread alive until its child has been reaped."""
    def __init__(self, command, **options):
        self.process = None
        self.error: BaseException | None = None
        self.ready = threading.Event()
        self.reaped = threading.Event()

        def create():
            try:
                self.process = subprocess.Popen(command, **options)
            except Exception as error:  # noqa: BLE001 - propagate creator failures to the caller
                self.error = error
            finally:
                self.ready.set()
            if self.process is not None:
                # PR_SET_PDEATHSIG follows the thread that creates the child.
                # Streaming generators may move between temporary AnyIO workers.
                self.reaped.wait()

        self.thread = threading.Thread(target=create, name="report-process-owner", daemon=True)
        self.thread.start()

    def get(self):
        self.ready.wait()
        if self.error is not None:
            raise self.error
        return self.process

    def release(self):
        self.reaped.set()
        self.thread.join(timeout=5)


def _channel_diagnostic(error: ValueError | OSError, line: bytes | None,
                        profile: str, returncode: int | None, *, stop_requested: bool) -> dict:
    """Private, bounded metadata only; never log rejected bytes or exceptions."""
    return {"event": "reader_failure", "profile": profile,
            "exception_class": type(error).__name__[:80],
            "json_position": error.pos if isinstance(error, json.JSONDecodeError) else None,
            "errno": error.errno if isinstance(error, OSError) else None,
            "line_length": len(line) if line is not None else None,
            "line_newline": line.endswith(b"\n") if line is not None else None,
            "line_cr_count": line.count(b"\r") if line is not None else None,
            "line_sha256": hashlib.sha256(line).hexdigest() if line is not None else None,
            "child_returncode": returncode, "coordinator_killed": False,
            "stop_requested": stop_requested}


def _log_channel_diagnostic(metadata: dict) -> None:
    # No exc_info/stack_info: decoder errors and OSError messages can contain
    # cells, SQL, paths or credentials. Capture only this closed JSON record.
    logger.error("REPORT_CHANNEL_DIAGNOSTIC %s", json.dumps(metadata, separators=(",", ":")))


def execute_messages(sources: list[dict], plan: dict, profile: str, *,
                     staging: Path | None = None, check=lambda: None, probe: dict | None = None):
    limits = ReportLimits.configured(profile)
    if sys.platform != "linux":
        raise OperationError(503, "REPORT_SANDBOX_UNAVAILABLE", "Reportes requiere el ejecutor Linux con Landlock y seccomp.")
    if not _slots.acquire(blocking=False):
        raise OperationError(429, "REPORT_CONCURRENCY_LIMIT", "El ejecutor está ocupado; intenta nuevamente.")
    process = owner = None
    stop = threading.Event()
    failure_metadata: dict | None = None
    try:
        check()
        script = Path(__file__).with_name("report_sandbox.py")
        # No environment inheritance, shell, database connection or business secrets.
        environment = {"PATH": "/usr/local/bin:/usr/bin:/bin", "LANG": "C.UTF-8",
                       "PYTHONDONTWRITEBYTECODE": "1", "HOME": "/nonexistent",
                       "OPENBLAS_NUM_THREADS": "1", "OMP_NUM_THREADS": "1",
                       "TZ": "UTC"}
        # setup-python and other relocated CPython distributions link their
        # interpreter against a library in the base installation. Derive this
        # single trusted runtime directory; never inherit the caller's loader
        # path, which could contain untrusted libraries or secret locations.
        runtime_library = Path(sys.base_prefix) / "lib"
        if runtime_library.is_dir():
            environment["LD_LIBRARY_PATH"] = str(runtime_library.resolve())
        owner = _ExecutorProcessOwner([sys.executable, "-I", "-B", str(script)], env=environment,
                                      stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                      stderr=subprocess.DEVNULL, close_fds=True, bufsize=65536)
        process = owner.get()
        payload = {"sources": sources, "plan": plan, "limits": limits.dto(), "profile": profile,
                   "staging": str(staging) if staging else None, "probe": probe}
        content = json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode() + b"\n"
        if len(content) > 2 * 1024**2:
            raise OperationError(422, "REPORT_CONTEXT_LIMIT", "El contexto resuelto supera 2 MiB.")
        assert process.stdin and process.stdout
        output_stream = process.stdout
        process.stdin.write(content)
        process.stdin.close()
        messages: queue.Queue = queue.Queue(maxsize=1)
        child_process = process

        def read():
            nonlocal failure_metadata
            line: bytes | None = None
            try:
                while not stop.is_set():
                    # Clear the previous successful row before a read that may
                    # raise; an OSError must not hash an unrelated prior batch.
                    line = None
                    line = output_stream.readline(limits.batch_bytes + 1024)
                    if not line:
                        value = None
                    elif len(line) > limits.batch_bytes:
                        value = {"kind": "error", "code": "REPORT_CHANNEL_LIMIT", "message": "El canal excedió el límite de lote."}
                    else:
                        value = json.loads(line)
                    while not stop.is_set():
                        try:
                            messages.put(value, timeout=0.2)
                            break
                        except queue.Full:
                            continue
                    if value is None:
                        return
            except (ValueError, OSError) as error:
                try:
                    returncode = child_process.poll()
                except OSError:
                    returncode = None
                failure_metadata = _channel_diagnostic(error, line, profile, returncode,
                                                       stop_requested=stop.is_set())
                _log_channel_diagnostic(failure_metadata)
                try:
                    messages.put({"kind": "error", "code": "REPORT_CHANNEL_FAILED", "message": "El canal del ejecutor falló."}, timeout=1)
                except queue.Full:
                    pass

        reader = threading.Thread(target=read, daemon=True)
        reader.start()
        deadline, complete = time.monotonic() + limits.timeout_seconds, False
        while True:
            check()
            if time.monotonic() > deadline:
                raise OperationError(422, "REPORT_TIMEOUT", "La consulta excedió su tiempo permitido.")
            try:
                message = messages.get(timeout=0.2)
            except queue.Empty:
                continue
            if message is None:
                if not complete:
                    raise OperationError(422, "REPORT_EXECUTOR_INTERRUPTED", "El ejecutor terminó sin completar el resultado.")
                break
            if message.get("kind") == "error":
                raise OperationError(422, message.get("code", "REPORT_EXECUTOR_FAILED"), message.get("message", "El ejecutor falló."))
            complete = complete or message["kind"] in {"complete", "probe"}
            yield message
        if process.wait(timeout=2) != 0:
            raise OperationError(422, "REPORT_EXECUTOR_FAILED", "El ejecutor terminó con fallo.")
    finally:
        stop.set()
        if owner and process is None:
            # Creation can finish after the caller is interrupted while waiting.
            owner.ready.wait()
            process = owner.process
        if process:
            coordinator_killed = process.poll() is None
            if coordinator_killed:
                process.kill()
            returncode = process.wait(timeout=5)
            if failure_metadata is not None:
                _log_channel_diagnostic({**failure_metadata, "event": "child_reaped",
                                         "child_returncode": returncode,
                                         "coordinator_killed": coordinator_killed})
            if process.stdout:
                process.stdout.close()
        if owner:
            owner.release()
        _slots.release()
