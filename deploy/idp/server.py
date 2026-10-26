"""Private serial GPU gateway without database/storage/cloud credentials.

No source filenames, URLs or model names are used as executable paths. The
disposable Paddle child exits before Qwen, bounding the shared 16GB GPU budget.
Docker enforces memory/PIDs/network/filesystem limits independently of this API.
"""
from __future__ import annotations

import base64
import hashlib
import http.client
import importlib.abc
import importlib.util
import json
import os
import re
import signal
import subprocess
import sys
import tempfile
import threading
import time
import types
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

MAX_BYTES = 48 * 1024**2
APP_ROOT = Path(__file__).resolve().parent
# Capture this process's inputs once. A later bind-mount edit must not rewrite
# the provenance of an already running gateway or change its prompt/lock.
STARTUP_FILES = {name: (APP_ROOT / name).read_bytes() for name in (
    "server.py", "paddle_runner.py", "pipeline.yml", "prompt-v1.txt", "prompt-v2.txt",
    "document_semantic_fragments.py", "document_template_builder_rules.py", "document_literal_tables.py", "idp_observability.py", "idp_memory_lifecycle.py", "idp_cache_advice.py", "recognition.py", "prompt-visual-v1.txt", "models.lock.json")}
STARTUP_HASHES = {name: hashlib.sha256(raw).hexdigest() for name, raw in STARTUP_FILES.items()}
LOCK_BYTES = STARTUP_FILES["models.lock.json"]
LOCK = json.loads(LOCK_BYTES)
PROMPT = STARTUP_FILES["prompt-v1.txt"].decode("utf-8")
PROMPTS = {"trackvance-idp-098-v1": PROMPT,
           "trackvance-idp-098-v2": STARTUP_FILES["prompt-v2.txt"].decode("utf-8")}
VISUAL_PROMPT = STARTUP_FILES["prompt-visual-v1.txt"].decode("utf-8")


class FrozenSourceLoader(importlib.abc.SourceLoader):
    """Import only captured trusted module bytes, without rereading a mount."""

    def __init__(self, filename):
        self.filename = filename

    def get_filename(self, fullname):
        return str(APP_ROOT / self.filename)

    def get_data(self, path):
        if path != self.get_filename(""):
            raise OSError("FROZEN_MODULE_PATH")
        return STARTUP_FILES[self.filename]


FROZEN_PACKAGE = types.ModuleType("trackvance_idp_frozen")
FROZEN_PACKAGE.__path__ = []
sys.modules[FROZEN_PACKAGE.__name__] = FROZEN_PACKAGE


def frozen_module(filename):
    fullname = FROZEN_PACKAGE.__name__ + "." + filename.removesuffix(".py")
    spec = importlib.util.spec_from_loader(fullname, FrozenSourceLoader(filename))
    module = importlib.util.module_from_spec(spec)
    sys.modules[fullname] = module
    spec.loader.exec_module(module)
    setattr(FROZEN_PACKAGE, filename.removesuffix(".py"), module)
    return module


FRAGMENTS = frozen_module("document_semantic_fragments.py")
RULES = frozen_module("document_template_builder_rules.py")
TABLES = frozen_module("document_literal_tables.py")
DIAGNOSTICS = frozen_module("idp_observability.py")
MEMORY = frozen_module("idp_memory_lifecycle.py")
RECOGNITION = frozen_module("recognition.py")
CHILD_SOURCE_HASHES = {name: STARTUP_HASHES[name] for name in ("paddle_runner.py", "pipeline.yml", "idp_observability.py")}
FAILURE_DIAGNOSTICS = DIAGNOSTICS.FailureRing(CHILD_SOURCE_HASHES)
GATE = threading.Lock()
REQUEST_SLOTS = threading.BoundedSemaphore(4)
CONTROL_SLOTS = threading.BoundedSemaphore(8)
ADMISSION_LOCK = threading.Lock()
ADMISSIONS = {}
ACTIVE = {}
UPSTREAM = {}
BUDGETS = {}
MEMORY_STATE = {"status": "AVAILABLE"}
MEMORY_FAILURE = None
CACHE_HYGIENE = os.environ.get("TRACKVANCE_IDP_CACHE_HYGIENE", "0") == "1"
CANCELLED = set()
STATES = {}
OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))
VERIFIED_SIGNATURES = None
RUNTIME = {"ready": False, "kernel_probe": "PENDING", "model_inference": False}
OBSERVED_INFERENCE = {"paddle": False, "qwen": False}
ERROR_STATUS = {
    "GPU_RUNTIME_UNAVAILABLE": 503, "MODEL_INTEGRITY": 503, "LLM_MODEL_INTEGRITY": 503,
    "MODEL_NOT_PINNED": 422, "DOCUMENT_FORMAT": 422, "SOURCE_INTEGRITY": 422,
    "REGION_LIMIT": 422, "OBSERVATION_INTEGRITY": 422, "LLM_CONTEXT_LIMIT": 422,
    "LLM_INCOMPLETE": 422, "LLM_BUDGET_UNVERIFIED": 422, "CANCELLED": 422,
    "PADDLE_FAILURE": 422, "PADDLE_RESULT_LIMIT": 422, "LLM_FAILURE": 422,
    "LLM_RESPONSE_LIMIT": 422, "INFERENCE_TIMEOUT": 504, "REQUEST_LIMIT": 413,
    "LLM_FRAGMENT_LIMIT": 422, "LLM_FRAGMENT_INTEGRITY": 422, "LLM_SELECTION_INVALID": 422,
    "REQUEST_BUDGET_INVALID": 422, "GPU_MEMORY_UNAVAILABLE": 503,
    "GPU_READER_QUIESCENCE_UNVERIFIED": 503, "GPU_OWNER_QUIESCENCE_UNVERIFIED": 503,
    "GPU_RESIDENCY_UNVERIFIED": 503, "GPU_FOREIGN_RESIDENCY": 503,
    "GPU_PIN_UNVERIFIED": 503, "GPU_UNLOAD_UNVERIFIED": 503, "CACHE_OWNERSHIP_UNVERIFIED": 503,
    "REQUEST_ID": 422, "ADMISSION_CONFIGURATION": 422, "DUPLICATE_REQUEST": 422,
}


def safe_error(error, identity=None):
    """Only exact fixed codes cross this boundary; never upstream text or paths."""
    if identity in CANCELLED:
        return 422, "CANCELLED"
    if isinstance(error, (subprocess.TimeoutExpired, TimeoutError)):
        return 504, "INFERENCE_TIMEOUT"
    code = str(error) if isinstance(error, (ValueError, TypeError)) else "INFERENCE_FAILED"
    return ERROR_STATUS.get(code, 422), code if code in ERROR_STATUS else "INFERENCE_FAILED"


def compact_definition(definition):
    """Versioned semantic metadata; parser samples and coordinates never reach Qwen."""
    def field(item, extra_labels=()):
        result = {key: item[key] for key in ("name", "label", "logical_type", "required", "section",
            "date_format", "decimal_separator", "thousands_separator", "currency_symbols",
            "numeric_fragments", "decimal_format_explicit") if key in item}
        visual = item.get("visual") or {}
        labels = [*extra_labels, visual.get("label", ""), *visual.get("label_aliases", [])]
        for alternative in item.get("visual_alternatives", []):
            labels.extend([alternative.get("label", ""), *alternative.get("label_aliases", [])])
        aliases = list(dict.fromkeys(value for value in labels if value and value != item.get("label")))
        if aliases:
            result["labels"] = aliases
        if visual.get("value_pattern"):
            result["literal_capture_pattern"] = visual["value_pattern"]
            result["literal_capture_group"] = 1
        return result

    tables = []
    for table in definition.get("tables", []):
        visual = table.get("visual") or {}
        labels, aliases = visual.get("header_labels", []), visual.get("header_label_aliases") or []
        columns = [field(item, ([labels[index]] if index < len(labels) else [])
                         + (aliases[index] if index < len(aliases) else [])) for index, item in enumerate(table["fields"])]
        tables.append({"name": table["name"], "fields": columns})
    return {"semantic_definition_version": "trackvance-semantic-098-v2",
            "fields": [field(item) for item in definition.get("fields", [])], "tables": tables}


def semantic_content(definition, observations):
    evidence = []
    for index, line in enumerate(observations):
        if not isinstance(line, dict) or not isinstance(line.get("text"), str):
            raise TypeError("OBSERVATION_INTEGRITY")
        evidence.append({**{key: line[key] for key in ("text", "page", "region")}, "evidence_id": index})
    return json.dumps({"definition": compact_definition(definition), "evidence": evidence}, ensure_ascii=False)


def semantic_targets(definition, targets):
    """The parent can request bounded missing work, never add schema names."""
    if not isinstance(targets, dict) or set(targets) != {"fields", "tables"}:
        raise ValueError("LLM_SELECTION_INVALID")
    for kind in ("fields", "tables"):
        allowed = {item["name"] for item in definition.get(kind, [])}
        names = targets[kind]
        if (not isinstance(names, list) or any(not isinstance(name, str) for name in names)
                or len(names) != len(set(names)) or not set(names) <= allowed):
            raise ValueError("LLM_SELECTION_INVALID")
    return {key: list(targets[key]) for key in ("fields", "tables")}


def validate_fragment_selection(candidate, definition, targets, fragment_count, *, allowed_ids=None):
    """V2 permits IDs only; unknown, invented, and non-target selections fail."""
    def cell(value, enabled):
        if not isinstance(value, dict) or set(value) != {"fragment_id"}:
            raise ValueError("LLM_SELECTION_INVALID")
        identity = value["fragment_id"]
        if identity is not None and (not enabled or type(identity) is not int or not 0 <= identity < fragment_count
                or allowed_ids is not None and identity not in allowed_ids):
            raise ValueError("LLM_SELECTION_INVALID")

    if not isinstance(candidate, dict) or set(candidate) != {"fields", "tables"}:
        raise ValueError("LLM_SELECTION_INVALID")
    for kind in ("fields", "tables"):
        selected = candidate[kind]
        if not isinstance(selected, dict) or set(selected) != {item["name"] for item in definition.get(kind, [])}:
            raise ValueError("LLM_SELECTION_INVALID")
    for field in definition.get("fields", []):
        cell(candidate["fields"][field["name"]], field["name"] in targets["fields"])
    for table in definition.get("tables", []):
        rows = candidate["tables"][table["name"]]
        if (not isinstance(rows, list) or len(rows) > 10000
                or table["name"] not in targets["tables"] and rows):
            raise ValueError("LLM_SELECTION_INVALID")
        for row in rows:
            if not isinstance(row, dict) or set(row) != {field["name"] for field in table["fields"]}:
                raise ValueError("LLM_SELECTION_INVALID")
            for value in row.values():
                cell(value, True)


def fragment_content(definition, observations, targets):
    selection = TABLES.prompt_catalog_view(observations, definition, targets)
    catalog, view = selection["full_catalog"], selection["prompt_view"]
    visible_observations = {fragment["evidence_id"] for fragment in view["fragments"]}
    content = json.dumps({"definition": compact_definition(definition), "semantic_targets": targets,
        "fragment_catalog_version": catalog["version"], "fragment_catalog_columns": ["fragment_id", "evidence_id", "text"],
        "fragment_catalog": [
            [fragment["fragment_id"], fragment["evidence_id"], fragment["text"]]
            for fragment in view["fragments"]],
        "observation_context": [{"evidence_id": index, **{key: source.get(key) for key in ("page", "region", "block_label")}}
            for index, source in enumerate(observations) if index in visible_observations],
        "fragment_prompt_view_sha256": view["sha256"],
        "omitted_resolved_detail_observation_ids": view["omitted_detail_observation_ids"]}, ensure_ascii=False)
    return content, catalog, view


def startup_provenance():
    return {"capture": "PROCESS_STARTUP", "files_sha256": dict(STARTUP_HASHES)}


def ollama_metrics(reply):
    """Actual Ollama final-response observations; unavailable is never zero."""
    return {name: value if type(value := reply.get(name)) is int and 0 <= value <= 2**63 - 1 else None
            for name in ("total_duration", "load_duration", "prompt_eval_duration", "eval_duration",
                         "prompt_eval_count", "eval_count")}


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


def memory_request(identity, budget, path, body=None, *, cleanup=False):
    def operation(connection):
        raw = json.dumps(body).encode() if body is not None else None
        connection.request("POST" if body is not None else "GET", "/api" + path, body=raw,
                           headers={"Content-Type": "application/json"})
        response = connection.getresponse()
        try:
            data = response.read(65537)
            if response.status != 200 or len(data) > 65536:
                raise MEMORY.UnsafeLifecycle("GPU_RESIDENCY_UNVERIFIED")
            return json.loads(data)
        finally:
            response.close()
    return upstream_read(identity, budget, operation, cleanup=cleanup)


def upstream_read(identity, budget, operation, *, cleanup=False):
    def publish(handle):
        if UPSTREAM:
            raise MEMORY.UnsafeLifecycle("GPU_READER_QUIESCENCE_UNVERIFIED")
        UPSTREAM[identity] = handle
    def withdraw(handle):
        if UPSTREAM.get(identity) is not handle:
            raise MEMORY.UnsafeLifecycle("GPU_READER_QUIESCENCE_UNVERIFIED")
        UPSTREAM.pop(identity)
    return MEMORY.read_joined(operation, budget, lambda: identity in CANCELLED, publish, withdraw, cleanup=cleanup,
        connection_factory=http.client.HTTPConnection)


def memory_barrier(identity, budget, *, cleanup=False):
    return MEMORY.quiet_ollama(budget,
        lambda path, body, final: memory_request(identity, budget, path, body, cleanup=final),
        LOCK["qwen"], lambda: not ACTIVE and not UPSTREAM, cleanup=cleanup)


def cache_hygiene(budget):
    if not CACHE_HYGIENE:
        return {"status": "DISABLED"}
    if ACTIVE or UPSTREAM:
        raise MEMORY.UnsafeLifecycle("GPU_OWNER_QUIESCENCE_UNVERIFIED")
    name = "idp_cache_advice.py"
    if hashlib.sha256((APP_ROOT / name).read_bytes()).hexdigest() != STARTUP_HASHES[name]:
        raise MEMORY.UnsafeLifecycle("CACHE_OWNERSHIP_UNVERIFIED")
    # Advisory work gets only the remaining caller allowance, never a new quota.
    remaining = budget.join_remaining()
    if remaining <= .05:
        return {"status": "SKIPPED_NO_REMAINING_BUDGET"}
    started = time.monotonic()
    def observed(value):
        return {**value, "elapsed_seconds": round(time.monotonic() - started, 4)}
    process = subprocess.Popen(["/usr/local/bin/python", "-I", "-B", str(APP_ROOT / name), str(min(4.0, remaining))],
        stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
        start_new_session=True, close_fds=True)
    try:
        output, _ = process.communicate(timeout=remaining)
    except subprocess.TimeoutExpired:
        process.kill()
        try:
            process.wait(timeout=budget.join_remaining())
        except subprocess.TimeoutExpired:
            raise MEMORY.UnsafeLifecycle("CACHE_OWNERSHIP_UNVERIFIED") from None
        return observed({"status": "ADVICE_TIMEOUT", "reclaim_or_cold_state_guaranteed": False})
    if process.returncode != 0 or len(output) > 4096:
        return observed({"status": "ADVICE_UNAVAILABLE", "reclaim_or_cold_state_guaranteed": False})
    try:
        result = json.loads(output)
    except (ValueError, UnicodeError):
        return observed({"status": "ADVICE_UNAVAILABLE", "reclaim_or_cold_state_guaranteed": False})
    if not isinstance(result, dict) or result.get("status") == "OWNERSHIP_UNVERIFIED":
        raise MEMORY.UnsafeLifecycle("CACHE_OWNERSHIP_UNVERIFIED")
    return observed(result)


def stream_chat(identity, body, timeout, raw_capture=None):
    """One joined reader; queue, socket and cleanup share the admitted deadline."""
    budget = BUDGETS.get(identity) or MEMORY.Budget(max(30, min(1800, timeout)), timeout)
    def operation(connection):
        connection.request("POST", "/api/chat", body=json.dumps({**body, "stream": True}).encode(),
                           headers={"Content-Type": "application/json"})
        response = connection.getresponse()
        try:
            if response.status != 200:
                raise ValueError(_llm_error(response.read(4097), response.status))
            chunks, length, final, stream_length = [], 0, {}, 0
            while True:
                if identity in CANCELLED:
                    raise ValueError("CANCELLED")
                budget.remaining()
                raw = response.readline(1024 * 1024 + 1)
                if not raw:
                    break
                if len(raw) > 1024 * 1024:
                    raise ValueError("LLM_RESPONSE_LIMIT")
                stream_length += len(raw)
                if stream_length > 16 * 1024**2:
                    raise ValueError("LLM_RESPONSE_LIMIT")
                if raw_capture is not None:
                    raw_capture(raw)
                item = json.loads(raw)
                if item.get("error"):
                    raise ValueError(_llm_error(raw, item.get("status")))
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
            response.close()
    return upstream_read(identity, budget, operation)


def _llm_error(raw, status=None):
    try:
        value = json.loads(raw) if len(raw) <= 4096 else {}
        message = value.get("error", "") if isinstance(value, dict) else ""
    except (ValueError, UnicodeError):
        message = ""
    context_errors = {"the input length exceeds the context length", "input length exceeds maximum context length",
                      "input exceeds maximum context length", "input after truncation exceeds maximum context length"}
    return "LLM_CONTEXT_LIMIT" if status == 413 or message in context_errors else "LLM_FAILURE"


def health():
    required = [Path("/models/paddle/model.safetensors"), Path("/models/layout/inference.pdiparams")]
    installed = all(path.is_file() for path in required) and verify_models()
    qwen_installed = False
    try:
        qwen_installed = qwen_verified()
    except (OSError, ValueError):
        pass
    ready = installed and qwen_installed and RUNTIME.get("ready") is True and MEMORY_STATE["status"] == "AVAILABLE"
    return {"ready": ready, "reason": "Modelos verificados y kernel CUDA comprobado; aceptación del corpus registrada por separado." if ready else "Modelos o runtime CUDA pendientes/no disponibles.",
            "models_installed": {"paddle": installed, "qwen": qwen_installed}, "runtime": RUNTIME,
            "observed_model_inference": dict(OBSERVED_INFERENCE),
            "recognition": {"contract": "TRACKVANCE_RECOGNITION_V1",
                "strategies": ["QWEN_VISUAL", "PADDLE_STRUCTURED"], "incremental_originals": True},
            "runtime_provenance": {"gateway": startup_provenance()},
            "memory_lifecycle": dict(MEMORY_STATE), "gpu_concurrency": 1, "busy": GATE.locked(), "paddle_model": "PaddleOCR-VL-1.6", "llm_model": LOCK["qwen"]["model"]}


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
            identity = self.path.rsplit("/", 1)[1]
            state = STATES.get(identity) or RECOGNITION.terminal_for(identity)
            self.send(200 if state else 404, state or {"phase": "UNKNOWN"})
        else:
            self.send(404, {"error": "NOT_FOUND"})

    def do_POST(self):
        arrived = time.monotonic()
        slot = False
        control_slot = False
        admitted = False
        identity = None
        try:
            if self.path not in {"/extract", "/interpret", "/refine", "/recognize", "/cancel"}:
                self.close_connection = True
                self.send(404, {"error": "NOT_FOUND"})
                return
            if self.path in {"/extract", "/interpret", "/refine", "/recognize"}:
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
            if not isinstance(body, dict):
                raise TypeError("REQUEST_ID")
            identity = body.get("request_id", "")
            if not re.fullmatch(r"[a-f0-9]{32}", identity):
                raise ValueError("REQUEST_ID")
            if self.path == "/cancel":
                with ADMISSION_LOCK:
                    accepted = identity in ADMISSIONS
                    if accepted:
                        CANCELLED.add(identity)
                process = ACTIVE.get(identity)
                if process and process.poll() is None:
                    try:
                        os.killpg(process.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                upstream = UPSTREAM.get(identity)
                if upstream:
                    upstream.close()
                self.send(200, {"cancelled": accepted})
                return
            if self.path not in {"/extract", "/interpret", "/refine", "/recognize"}:
                self.send(404, {"error": "NOT_FOUND"})
                return
            if not isinstance(body.get("configuration"), dict):
                raise TypeError("ADMISSION_CONFIGURATION")
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
            if "request_budget_seconds" in body and body["request_budget_seconds"] is None:
                raise ValueError("REQUEST_BUDGET_INVALID")
            BUDGETS[identity] = budget = MEMORY.Budget(queue_timeout, body.get("request_budget_seconds"), started=arrived)
            STATES[identity] = ({"phase": "QUEUED", "page_one_based": None, "expected_pages": None, "emitted_pages": []}
                if self.path == "/recognize" else {"phase": "QUEUED", "progress": 15})
            while not GATE.acquire(timeout=min(.2, budget.remaining())):
                if identity in CANCELLED or time.monotonic() - queued >= queue_timeout:
                    code = "CANCELLED" if identity in CANCELLED else "GPU_QUEUE_TIMEOUT"
                    CANCELLED.discard(identity)
                    STATES.pop(identity, None)
                    self.send(422, {"error": code})
                    return
            acquired = time.monotonic()
            result, failure, released, recognition = None, None, None, None
            quarantined_on_entry = MEMORY_STATE["status"] != "AVAILABLE"
            try:
                if quarantined_on_entry:
                    raise MEMORY.UnsafeLifecycle("GPU_MEMORY_UNAVAILABLE")
                if identity in CANCELLED:
                    raise ValueError("CANCELLED")
                if self.path == "/recognize":
                    recognition = RECOGNITION.Session(globals(), self, body)
                    result = recognition.execute()
                else:
                    result = self.interpret(body) if self.path == "/interpret" else self.extract(body)
            except (ValueError, KeyError, TypeError, OSError, subprocess.SubprocessError,
                    urllib.error.URLError, http.client.HTTPException) as error:
                failure = error
            finally:
                if not quarantined_on_entry:
                    try:
                        released = memory_barrier(identity, budget, cleanup=True)
                        released["cache_advice"] = cache_hygiene(budget)
                    except (ValueError, KeyError, TypeError, OSError, subprocess.SubprocessError,
                            urllib.error.URLError, http.client.HTTPException) as cleanup_error:
                        global MEMORY_FAILURE
                        code = str(cleanup_error) if isinstance(cleanup_error, MEMORY.UnsafeLifecycle) else "GPU_RESIDENCY_UNVERIFIED"
                        # Latch the first original candidate/failure. Rejected requests cannot erase evidence.
                        if MEMORY_FAILURE is None:
                            MEMORY_FAILURE = {"candidate": result, "original_error": "CANCELLED" if identity in CANCELLED
                                else safe_error(failure, identity)[1] if failure else None,
                                "cleanup_error": code, "source_sha256": body.get("source_sha256")}
                            MEMORY_STATE.update(status="UNAVAILABLE", reason=code)
                        failure = MEMORY.UnsafeLifecycle(code)
                with ADMISSION_LOCK:
                    was_cancelled = identity in CANCELLED
                    ADMISSIONS.pop(identity, None)
                    CANCELLED.discard(identity)
                    BUDGETS.pop(identity, None)
                    STATES.pop(identity, None)
                    admitted = False  # No later cleanup may erase a subsequent admission with the same ID.
                if was_cancelled and not isinstance(failure, MEMORY.UnsafeLifecycle):
                    failure = ValueError("CANCELLED")
                GATE.release()
            if failure is not None:
                if recognition is not None:
                    code = safe_error(failure, identity)[1]
                    status = "CANCELLED" if code == "CANCELLED" else "TIMEOUT" if code == "INFERENCE_TIMEOUT" else "FAILED"
                    recognition.finish(status, released, code)
                    if recognition.started:
                        return
                raise failure
            result["engine"]["memory_lifecycle"] = released
            result["engine"]["queue_seconds"] = round(acquired - queued, 4)
            if recognition is not None:
                recognition.finish("COMPLETE", released)
                return
            self.send(200, result)
        except (ValueError, KeyError, TypeError, OSError, subprocess.SubprocessError,
                urllib.error.URLError, http.client.HTTPException) as error:
            status, code = safe_error(error, identity)
            if not getattr(self, "recognition_stream_started", False):
                self.send(status, {"error": code})
        finally:
            if slot:
                REQUEST_SLOTS.release()
            if control_slot:
                CONTROL_SLOTS.release()
            if admitted:
                with ADMISSION_LOCK:
                    ADMISSIONS.pop(identity, None)
                    CANCELLED.discard(identity)
                    BUDGETS.pop(identity, None)
                    STATES.pop(identity, None)

    def extract(self, body):
        if RUNTIME.get("ready") is not True:
            raise ValueError("GPU_RUNTIME_UNAVAILABLE")
        if not verify_models():
            raise ValueError("MODEL_INTEGRITY")
        config = body["configuration"]
        if (config["paddle_model"] != "PaddleOCR-VL-1.6" or config["llm_model"] != LOCK["qwen"]["model"]
                or config["prompt_version"] not in PROMPTS):
            raise ValueError("MODEL_NOT_PINNED")
        timeout = min(1800, max(30, int(config.get("timeout_seconds", 300))))
        filename = body["filename"]
        if filename not in {"document.pdf", "document.png", "document.jpg", "document.jpeg"}:
            raise ValueError("DOCUMENT_FORMAT")
        raw = base64.b64decode(body["source"], validate=True)
        if not raw or len(raw) > 32 * 1024**2 or hashlib.sha256(raw).hexdigest() != body["source_sha256"]:
            raise ValueError("SOURCE_INTEGRITY")
        started = time.monotonic()
        budget = BUDGETS[body["request_id"]]
        memory_barrier(body["request_id"], budget)
        STATES[body["request_id"]] = {"phase": "PADDLE", "progress": 25}
        with tempfile.TemporaryDirectory(prefix="idp-") as temporary:
            folder = Path(temporary)
            owned_job_identity = DIAGNOSTICS.identity(folder.lstat())
            (folder / filename).write_bytes(raw)
            regions = body.get("regions", [])
            if not isinstance(regions, list) or len(regions) > 4:
                raise ValueError("REGION_LIMIT")
            (folder / "request.json").write_text(json.dumps({"filename": filename, "source_sha256": body["source_sha256"], "regions": regions}))
            if body["request_id"] in CANCELLED:
                raise ValueError("CANCELLED")
            budget.remaining()
            with (folder / "error.log").open("wb") as error_stream:
                process = subprocess.Popen([sys.executable, "-I", "-B", "/app/paddle_runner.py", temporary],
                    stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=error_stream,
                    start_new_session=True, close_fds=True)
            ACTIVE[body["request_id"]] = process
            failure_code = None
            try:
                deadline = budget.work_deadline
                while process.poll() is None:
                    if body["request_id"] in CANCELLED:
                        raise ValueError("CANCELLED")
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise subprocess.TimeoutExpired("paddle_runner", timeout)
                    try:
                        process.wait(timeout=min(.2, remaining))
                    except subprocess.TimeoutExpired:
                        pass
                if body["request_id"] in CANCELLED:
                    raise ValueError("CANCELLED")
                if process.returncode or not (folder / "result.json").is_file():
                    raise ValueError("PADDLE_FAILURE")
                if (folder / "result.json").stat().st_size > 16 * 1024**2:
                    raise ValueError("PADDLE_RESULT_LIMIT")
                result = json.loads((folder / "result.json").read_text())
                OBSERVED_INFERENCE["paddle"] = True
                child_phase = DIAGNOSTICS.phase_from_job(folder, owned_job_identity, CHILD_SOURCE_HASHES, body["source_sha256"])
                if child_phase is not None:
                    result["engine"]["child_diagnostics"] = child_phase
            except (ValueError, KeyError, TypeError, OSError, subprocess.SubprocessError) as error:
                failure_code = safe_error(error, body["request_id"])[1]
                raise
            finally:
                if process.poll() is None:
                    try:
                        os.killpg(process.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                    try:
                        process.wait(timeout=budget.join_remaining())
                    except subprocess.TimeoutExpired:
                        raise MEMORY.UnsafeLifecycle("GPU_OWNER_QUIESCENCE_UNVERIFIED") from None
                ACTIVE.pop(body["request_id"], None)
                if failure_code in {"INFERENCE_TIMEOUT", "PADDLE_FAILURE", "CANCELLED"}:
                    try:
                        FAILURE_DIAGNOSTICS.retain(folder, owned_job_identity, body["request_id"],
                            body["source_sha256"], failure_code, process)
                    except (OSError, ValueError, KeyError, TypeError, RuntimeError):
                        RUNTIME["diagnostic_retention_available"] = False
        # The coordinator runs confined template validation before deciding
        # whether a separate /interpret request is necessary.
        result["engine"]["elapsed_seconds"] = round(time.monotonic() - started, 4)
        result["engine"].update(model_lock_sha256=hashlib.sha256(LOCK_BYTES).hexdigest(),
            paddle_revision=LOCK["paddle"]["revision"], layout_revision=LOCK["layout"]["revision"],
            layout_model_sha256=LOCK["layout"]["files"]["inference.pdiparams"])
        result["engine"].setdefault("runtime_provenance", {})["gateway"] = startup_provenance()
        return result

    def interpret(self, body):
        config = body["configuration"]
        if config["llm_model"] != LOCK["qwen"]["model"] or config["prompt_version"] not in PROMPTS:
            raise ValueError("MODEL_NOT_PINNED")
        budget = BUDGETS[body["request_id"]]
        memory_barrier(body["request_id"], budget)
        tags = memory_request(body["request_id"], budget, "/tags")
        if not isinstance(tags, dict) or not any(isinstance(item, dict) and item.get("name") == LOCK["qwen"]["model"]
                and item.get("digest") == LOCK["qwen"]["manifest_sha256"] for item in tags.get("models", [])):
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
            prompt = PROMPTS[config["prompt_version"]]
            catalog = None
            targets = None
            view = None
            if config["prompt_version"] == "trackvance-idp-098-v2":
                targets = semantic_targets(body["definition"], body.get("semantic_targets"))
                content, catalog, view = fragment_content(body["definition"], result["lines"], targets)
                if body.get("fragment_prompt_view_sha256", view["sha256"]) != view["sha256"]:
                    raise ValueError("LLM_FRAGMENT_INTEGRITY")
            else:
                content = semantic_content(body["definition"], result["lines"])
            content_bytes = len(content.encode())
            if content_bytes > 120000:
                raise ValueError("LLM_CONTEXT_LIMIT")
            context = config.get("context_tokens", 8192)
            if type(context) is not int or not 1024 <= context <= 32768:
                raise ValueError("LLM_CONTEXT_LIMIT")
            output_budget = min(8192, context // 2)
            input_budget = context - output_budget
            reply = stream_chat(body["request_id"], {"model": LOCK["qwen"]["model"], "think": False,
                "keep_alive": 0, "format": body["semantic_schema"], "truncate": False, "shift": False,
                "messages": [{"role": "system", "content": prompt}, {"role": "user", "content": content}],
                "options": {"num_ctx": context, "num_predict": output_budget, "temperature": 0, "seed": 98}}, timeout=remaining)
            if body["request_id"] in CANCELLED:
                raise ValueError("CANCELLED")
            if reply.get("done") is not True or reply.get("done_reason") != "stop":
                raise ValueError("LLM_INCOMPLETE")
            metrics = ollama_metrics(reply)
            if metrics["prompt_eval_count"] is None or metrics["eval_count"] is None:
                raise ValueError("LLM_BUDGET_UNVERIFIED")
            if metrics["prompt_eval_count"] > input_budget or metrics["eval_count"] > output_budget:
                raise ValueError("LLM_CONTEXT_LIMIT")
            selection = json.loads(reply["message"]["content"])
            if catalog is not None:
                validate_fragment_selection(selection, body["definition"], targets, len(catalog["fragments"]),
                    allowed_ids={fragment["fragment_id"] for fragment in view["fragments"]})
                result["fragment_catalog_sha256"] = catalog["sha256"]
                result["fragment_catalog_version"] = catalog["version"]
                result["fragment_prompt_view_sha256"] = view["sha256"]
            result["semantics"] = selection
            OBSERVED_INFERENCE["qwen"] = True
            result["engine"]["llm_model"] = LOCK["qwen"]["model"]
            result["engine"]["llm_manifest_sha256"] = LOCK["qwen"]["manifest_sha256"]
            result["engine"]["prompt_sha256"] = hashlib.sha256(prompt.encode()).hexdigest()
            result["engine"]["prompt_version"] = config["prompt_version"]
            if catalog is not None:
                result["engine"]["fragment_catalog_sha256"] = catalog["sha256"]
                result["engine"]["fragment_catalog_version"] = catalog["version"]
                result["engine"]["fragment_count"] = len(catalog["fragments"])
                result["engine"]["fragment_prompt_view"] = {key: view[key] for key in (
                    "sha256", "full_fragment_count", "prompt_fragment_count", "omitted_detail_observation_ids")}
            result["engine"]["ollama_metrics"] = metrics
            result["engine"]["semantic_definition_version"] = "trackvance-semantic-098-v2"
            result["engine"]["context_budget"] = {"context_tokens": context, "input_tokens": input_budget,
                "output_tokens": output_budget, "input_utf8_bytes": len(prompt.encode()) + content_bytes,
                "input_tokens_observed": metrics["prompt_eval_count"], "output_tokens_observed": metrics["eval_count"],
                "truncate": False, "shift": False, "done_reason": reply["done_reason"], "verified": True}
            result["engine"]["phases"]["llm_seconds"] = (
                round(metrics["total_duration"] / 1e9, 4) if metrics["total_duration"] is not None else None)
        result["engine"]["elapsed_seconds"] = round(time.monotonic() - started, 4)
        result["engine"]["model_lock_sha256"] = hashlib.sha256(LOCK_BYTES).hexdigest()
        result["engine"]["runtime_provenance"] = {"gateway": startup_provenance()}
        return result


if __name__ == "__main__":
    threading.Thread(target=probe_runtime, daemon=True).start()
    ThreadingHTTPServer(("0.0.0.0", 8088), Handler).serve_forever()
