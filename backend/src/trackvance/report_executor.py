"""Coordinator-to-confined-process channel, bounded pipes and cancellation."""
from __future__ import annotations

import json
import queue
import subprocess
import sys
import threading
import time
from pathlib import Path

from .operations_common import OperationError
from .report_config import ReportLimits

_slots = threading.BoundedSemaphore(ReportLimits.configured().concurrency)


def execute_messages(sources: list[dict], plan: dict, profile: str, *,
                     staging: Path | None = None, check=lambda: None, probe: dict | None = None):
    limits = ReportLimits.configured(profile)
    if sys.platform != "linux":
        raise OperationError(503, "REPORT_SANDBOX_UNAVAILABLE", "Reportes requiere el ejecutor Linux con Landlock y seccomp.")
    if not _slots.acquire(blocking=False):
        raise OperationError(429, "REPORT_CONCURRENCY_LIMIT", "El ejecutor está ocupado; intenta nuevamente.")
    process = None
    stop = threading.Event()
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
        process = subprocess.Popen([sys.executable, "-I", "-B", str(script)], env=environment,
                                   stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                   stderr=subprocess.DEVNULL, close_fds=True, bufsize=65536)
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

        def read():
            try:
                while not stop.is_set():
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
            except (ValueError, OSError):
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
        if process:
            if process.poll() is None:
                process.kill()
            process.wait(timeout=5)
            if process.stdout:
                process.stdout.close()
        _slots.release()
