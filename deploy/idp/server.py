"""Private serial GPU gateway without database/storage/cloud credentials.

No source filenames, URLs or model names are used as executable paths. The
disposable Paddle child exits before Qwen, bounding the shared 16GB GPU budget.
Docker enforces memory/PIDs/network/filesystem limits independently of this API.
"""
from __future__ import annotations

import base64
import hashlib
import http.client
import json
import os
import re
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

MAX_BYTES = 48 * 1024**2
LOCK_BYTES = Path("/app/models.lock.json").read_bytes()
LOCK = json.loads(LOCK_BYTES)
GATE = threading.Lock()
REQUEST_SLOTS = threading.BoundedSemaphore(4)
CONTROL_SLOTS = threading.BoundedSemaphore(8)
ADMISSION_LOCK = threading.Lock()
ADMISSIONS = {}
ACTIVE = {}
UPSTREAM = {}
CANCELLED = set()
STATES = {}
OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))
VERIFIED_SIGNATURES = None
RUNTIME = {"ready": False, "kernel_probe": "PENDING", "model_inference": False}
OBSERVED_INFERENCE = {"paddle": False, "qwen": False}


def probe_runtime():
    global RUNTIME
    try:
        completed = subprocess.run([sys.executable, "-I", "-B", "/app/runtime_probe.py"],
            capture_output=True, text=True, timeout=75, check=False)
        if len(completed.stdout) > 1024 * 1024 or not completed.stdout.splitlines() or completed.returncode:
            raise ValueError("RUNTIME_PROBE_FAILED")
        result = json.loads(completed.stdout.splitlines()[-1])
        if not isinstance(result, dict) or result.get("kernel_probe") not in {"PASS", "FAIL"}:
            raise ValueError("RUNTIME_PROBE_INVALID")
        RUNTIME = result
    except (OSError, ValueError, subprocess.SubprocessError):
        RUNTIME = {"ready": False, "kernel_probe": "FAIL", "model_inference": False, "error_type": "RUNTIME_PROBE_FAILED"}


def verify_models():
    global VERIFIED_SIGNATURES
    try:
        signatures = {}
        for key in ("paddle", "layout"):
            for filename in LOCK[key]["files"]:
                path = Path("/models") / key / filename
                stat = path.stat()
                signatures[(key, filename)] = (stat.st_size, stat.st_mtime_ns, stat.st_ino)
        if VERIFIED_SIGNATURES == signatures:
            return True
        for (key, filename), signature in signatures.items():
            path = Path("/models") / key / filename
            with path.open("rb") as stream:
                if hashlib.file_digest(stream, "sha256").hexdigest() != LOCK[key]["files"][filename]:
                    return False
            after = path.stat()
            if (after.st_size, after.st_mtime_ns, after.st_ino) != signature:
                return False
        VERIFIED_SIGNATURES = signatures
    except OSError:
        return False
    return True


def ollama(path, body=None, timeout=5):
    request = urllib.request.Request("http://idp-ollama:11434/api" + path,
        data=json.dumps(body).encode() if body is not None else None,
        headers={"Content-Type": "application/json"})
    with OPENER.open(request, timeout=timeout) as response:
        data = response.read(16 * 1024**2 + 1)
        if len(data) > 16 * 1024**2:
            raise ValueError("LLM_RESPONSE_LIMIT")
        return json.loads(data)


def qwen_verified():
    tags = ollama("/tags")
    return isinstance(tags, dict) and isinstance(tags.get("models"), list) and any(
        isinstance(item, dict) and item.get("name") == LOCK["qwen"]["model"]
        and item.get("digest") == LOCK["qwen"]["manifest_sha256"] for item in tags["models"])


def stream_chat(identity, body, timeout):
    """Track the connection before waiting for headers, including cold load."""
    connection = http.client.HTTPConnection("idp-ollama", 11434, timeout=timeout)
    UPSTREAM[identity] = connection
    chunks, length, final = [], 0, {}
    try:
        if identity in CANCELLED:
            raise ValueError("CANCELLED")
        connection.request("POST", "/api/chat", body=json.dumps({**body, "stream": True}).encode(),
                           headers={"Content-Type": "application/json"})
        response = connection.getresponse()
        if response.status != 200:
            raise ValueError("LLM_FAILURE")
        while True:
            if identity in CANCELLED:
                raise ValueError("CANCELLED")
            raw = response.readline(1024 * 1024 + 1)
            if not raw:
                break
            if len(raw) > 1024 * 1024:
                raise ValueError("LLM_RESPONSE_LIMIT")
            item = json.loads(raw)
            if item.get("error"):
                raise ValueError("LLM_FAILURE")
            content = item.get("message", {}).get("content", "")
            length += len(content.encode())
            if length > 16 * 1024**2:
                raise ValueError("LLM_RESPONSE_LIMIT")
            chunks.append(content)
            if item.get("done"):
                final = item
                break
        if not final:
            raise ValueError("LLM_INCOMPLETE")
        return {**final, "message": {"content": "".join(chunks)}}
    finally:
        UPSTREAM.pop(identity, None)
        connection.close()


def health():
    required = [Path("/models/paddle/model.safetensors"), Path("/models/layout/inference.pdiparams")]
    installed = all(path.is_file() for path in required) and verify_models()
    qwen_installed = False
    try:
        qwen_installed = qwen_verified()
    except (OSError, ValueError):
        pass
    ready = installed and qwen_installed and RUNTIME.get("ready") is True
    return {"ready": ready, "reason": "Modelos verificados y kernel CUDA comprobado; aceptación del corpus registrada por separado." if ready else "Modelos o runtime CUDA pendientes/no disponibles.",
            "models_installed": {"paddle": installed, "qwen": qwen_installed}, "runtime": RUNTIME,
            "observed_model_inference": dict(OBSERVED_INFERENCE),
            "gpu_concurrency": 1, "busy": GATE.locked(), "paddle_model": "PaddleOCR-VL-1.6", "llm_model": LOCK["qwen"]["model"]}


class Handler(BaseHTTPRequestHandler):
    def setup(self):
        super().setup()
        self.connection.settimeout(15)

    def log_message(self, *args):
        pass  # Never log document contents or URLs.

    def send(self, code, value):
        raw = json.dumps(value, ensure_ascii=False, allow_nan=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self):
        if self.path == "/health":
            self.send(200, health())
        elif re.fullmatch(r"/status/[a-f0-9]{32}", self.path):
            state = STATES.get(self.path.rsplit("/", 1)[1])
            self.send(200 if state else 404, state or {"phase": "UNKNOWN"})
        else:
            self.send(404, {"error": "NOT_FOUND"})

    def do_POST(self):
        slot = False
        control_slot = False
        admitted = False
        identity = None
        try:
            if self.path not in {"/extract", "/interpret", "/refine", "/cancel"}:
                self.close_connection = True
                self.send(404, {"error": "NOT_FOUND"})
                return
            if self.path in {"/extract", "/interpret", "/refine"}:
                slot = REQUEST_SLOTS.acquire(blocking=False)
                if not slot:
                    self.close_connection = True
                    self.send(503, {"error": "GPU_QUEUE_FULL"})
                    return
            else:
                control_slot = CONTROL_SLOTS.acquire(blocking=False)
                if not control_slot:
                    self.close_connection = True
                    self.send(503, {"error": "CONTROL_BUSY"})
                    return
            length = int(self.headers.get("Content-Length", 0))
            if not 0 < length <= (1024 if control_slot else MAX_BYTES):
                self.close_connection = True
                raise ValueError("REQUEST_LIMIT")
            body = json.loads(self.rfile.read(length))
            identity = body.get("request_id", "")
            if not re.fullmatch(r"[a-f0-9]{32}", identity):
                raise ValueError("REQUEST_ID")
            if self.path == "/cancel":
                if identity in STATES:
                    CANCELLED.add(identity)
                process = ACTIVE.get(identity)
                if process and process.poll() is None:
                    os.killpg(process.pid, signal.SIGKILL)
                upstream = UPSTREAM.get(identity)
                if upstream and upstream.sock:
                    try:
                        upstream.sock.shutdown(socket.SHUT_RDWR)
                    except OSError:
                        pass
                    upstream.close()
                self.send(200, {"cancelled": identity in CANCELLED})
                return
            if self.path not in {"/extract", "/interpret", "/refine"}:
                self.send(404, {"error": "NOT_FOUND"})
                return
            maximum = body["configuration"].get("max_parallel_jobs", 1)
            if type(maximum) is not int or not 1 <= maximum <= 4:
                raise ValueError("ADMISSION_CONFIGURATION")
            with ADMISSION_LOCK:
                effective = min(maximum, *ADMISSIONS.values()) if ADMISSIONS else maximum
                if len(ADMISSIONS) >= effective:
                    self.send(503, {"error": "CONFIGURED_INFERENCE_CAPACITY"})
                    return
                if identity in ADMISSIONS:
                    raise ValueError("DUPLICATE_REQUEST")
                ADMISSIONS[identity] = maximum
                admitted = True
            queued = time.monotonic()
            queue_timeout = min(1800, max(30, int(body["configuration"].get("timeout_seconds", 300))))
            STATES[identity] = {"phase": "QUEUED", "progress": 15}
            while not GATE.acquire(timeout=.2):
                if identity in CANCELLED or time.monotonic() - queued >= queue_timeout:
                    CANCELLED.discard(identity)
                    STATES.pop(identity, None)
                    self.send(503, {"error": "GPU_QUEUE_CANCELLED_OR_TIMEOUT"})
                    return
            try:
                if identity in CANCELLED:
                    raise ValueError("CANCELLED")
                result = self.interpret(body) if self.path == "/interpret" else self.extract(body)
                result["engine"]["queue_seconds"] = round(time.monotonic() - queued - result["engine"]["elapsed_seconds"], 4)
                self.send(200, result)
            finally:
                CANCELLED.discard(identity)
                STATES.pop(identity, None)
                GATE.release()
        except (ValueError, KeyError, TypeError, OSError, subprocess.SubprocessError, urllib.error.URLError, http.client.HTTPException):
            self.send(422, {"error": "INFERENCE_FAILED"})
        finally:
            if slot:
                REQUEST_SLOTS.release()
            if control_slot:
                CONTROL_SLOTS.release()
            if admitted:
                with ADMISSION_LOCK:
                    ADMISSIONS.pop(identity, None)

    def extract(self, body):
        if RUNTIME.get("ready") is not True:
            raise ValueError("GPU_RUNTIME_UNAVAILABLE")
        if not verify_models():
            raise ValueError("MODEL_INTEGRITY")
        config = body["configuration"]
        if (config["paddle_model"] != "PaddleOCR-VL-1.6" or config["llm_model"] != LOCK["qwen"]["model"]
                or config["prompt_version"] != "trackvance-idp-098-v1"):
            raise ValueError("MODEL_NOT_PINNED")
        timeout = min(1800, max(30, int(config.get("timeout_seconds", 300))))
        filename = body["filename"]
        if filename not in {"document.pdf", "document.png", "document.jpg", "document.jpeg"}:
            raise ValueError("DOCUMENT_FORMAT")
        raw = base64.b64decode(body["source"], validate=True)
        if not raw or len(raw) > 32 * 1024**2 or hashlib.sha256(raw).hexdigest() != body["source_sha256"]:
            raise ValueError("SOURCE_INTEGRITY")
        started = time.monotonic()
        STATES[body["request_id"]] = {"phase": "PADDLE", "progress": 25}
        with tempfile.TemporaryDirectory(prefix="idp-") as temporary:
            folder = Path(temporary)
            (folder / filename).write_bytes(raw)
            regions = body.get("regions", [])
            if not isinstance(regions, list) or len(regions) > 4:
                raise ValueError("REGION_LIMIT")
            (folder / "request.json").write_text(json.dumps({"filename": filename, "source_sha256": body["source_sha256"], "regions": regions}))
            with (folder / "error.log").open("wb") as error_stream:
                process = subprocess.Popen([sys.executable, "-I", "-B", "/app/paddle_runner.py", temporary],
                    stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=error_stream,
                    start_new_session=True, close_fds=True)
            ACTIVE[body["request_id"]] = process
            try:
                process.wait(timeout=timeout)
                if process.returncode or not (folder / "result.json").is_file():
                    raise ValueError("PADDLE_FAILURE")
                if (folder / "result.json").stat().st_size > 16 * 1024**2:
                    raise ValueError("PADDLE_RESULT_LIMIT")
                result = json.loads((folder / "result.json").read_text())
                OBSERVED_INFERENCE["paddle"] = True
            finally:
                if process.poll() is None:
                    os.killpg(process.pid, signal.SIGKILL)
                    process.wait(timeout=5)
                ACTIVE.pop(body["request_id"], None)
        # The coordinator runs confined template validation before deciding
        # whether a separate /interpret request is necessary.
        result["engine"]["elapsed_seconds"] = round(time.monotonic() - started, 4)
        result["engine"].update(model_lock_sha256=hashlib.sha256(LOCK_BYTES).hexdigest(),
            paddle_revision=LOCK["paddle"]["revision"], layout_revision=LOCK["layout"]["revision"],
            layout_model_sha256=LOCK["layout"]["files"]["inference.pdiparams"])
        return result

    def interpret(self, body):
        config = body["configuration"]
        if config["llm_model"] != LOCK["qwen"]["model"] or config["prompt_version"] != "trackvance-idp-098-v1":
            raise ValueError("MODEL_NOT_PINNED")
        if not qwen_verified():
            raise ValueError("LLM_MODEL_INTEGRITY")
        started = time.monotonic()
        timeout = min(1800, max(30, int(config.get("timeout_seconds", 300))))
        lines = body["observations"]
        if (not isinstance(lines, list) or len(lines) > 100000
                or hashlib.sha256(json.dumps(lines, sort_keys=True, ensure_ascii=False).encode()).hexdigest() != body["observations_sha256"]):
            raise ValueError("OBSERVATION_INTEGRITY")
        result = {"lines": lines, "source_sha256": body["source_sha256"],
            "observations_sha256": body["observations_sha256"], "engine": {"phases": {}}}
        if lines:
            if body["request_id"] in CANCELLED:
                raise ValueError("CANCELLED")
            remaining = max(1, timeout - (time.monotonic() - started))
            STATES[body["request_id"]] = {"phase": "QWEN", "progress": 65}
            prompt = Path("/app/prompt-v1.txt").read_text()
            content = json.dumps({"definition": body["definition"], "evidence": [dict(line, evidence_id=index) for index, line in enumerate(result["lines"])]}, ensure_ascii=False)
            if len(content) > 120000:
                raise ValueError("LLM_CONTEXT_LIMIT")
            reply = stream_chat(body["request_id"], {"model": LOCK["qwen"]["model"], "think": False,
                "keep_alive": 0, "format": body["semantic_schema"],
                "messages": [{"role": "system", "content": prompt}, {"role": "user", "content": content}],
                "options": {"num_ctx": min(32768, max(1024, int(config.get("context_tokens", 8192)))),
                            "num_predict": 8192, "temperature": 0, "seed": 98}}, timeout=remaining)
            if body["request_id"] in CANCELLED:
                raise ValueError("CANCELLED")
            result["semantics"] = json.loads(reply["message"]["content"])
            OBSERVED_INFERENCE["qwen"] = True
            result["engine"]["llm_model"] = LOCK["qwen"]["model"]
            result["engine"]["llm_manifest_sha256"] = LOCK["qwen"]["manifest_sha256"]
            result["engine"]["prompt_sha256"] = hashlib.sha256(prompt.encode()).hexdigest()
            result["engine"]["phases"]["llm_seconds"] = round(reply.get("total_duration", 0) / 1e9, 4)
        result["engine"]["elapsed_seconds"] = round(time.monotonic() - started, 4)
        result["engine"]["model_lock_sha256"] = hashlib.sha256(LOCK_BYTES).hexdigest()
        return result


if __name__ == "__main__":
    threading.Thread(target=probe_runtime, daemon=True).start()
    ThreadingHTTPServer(("0.0.0.0", 8088), Handler).serve_forever()
