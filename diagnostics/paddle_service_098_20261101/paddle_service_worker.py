"""Private, bounded observation of the original Paddle gateway Handler.

Executed only by a newly frozen host controller inside the exact existing gateway.
It imports captured /app source, starts a loopback diagnostic Handler, and leaves
the operational main:8088 and every /app file unchanged. No Qwen generation path.
The inherited source and documents remain private; stdout is private NDJSON.
"""
from __future__ import annotations

import base64
import functools
import hashlib
import http.client
import importlib.util
import json
import os
from pathlib import Path
import re
import select
import stat
import subprocess
import sys
import threading
import time
import traceback
from http.server import ThreadingHTTPServer

CAPTURE_LIMIT = 64 * 1024**2
CAPTURE_NAMES = ("error.log", "phase.json", "request.json", "pages.manifest.json")
OUTPUT_LOCK = threading.Lock()


def need(value, reason):
    if not value:
        raise ValueError(reason)


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()


def emit(kind, **value):
    with OUTPUT_LOCK:
        sys.stdout.buffer.write(canonical({"kind": kind, **value}) + b"\n")
        sys.stdout.buffer.flush()


class DiagnosticHTTPServer(ThreadingHTTPServer):
    """Track only this diagnostic server's handlers and join them explicitly."""
    def __init__(self, *args, **kwargs):
        self.handler_threads = set()
        self.handler_lock = threading.Lock()
        super().__init__(*args, **kwargs)

    def process_request_thread(self, request, client_address):
        current = threading.current_thread()
        with self.handler_lock:
            self.handler_threads.add(current)
        try:
            return super().process_request_thread(request, client_address)
        finally:
            with self.handler_lock:
                self.handler_threads.discard(current)

    def join_handlers(self, timeout):
        deadline = time.monotonic() + max(0, timeout)
        with self.handler_lock:
            threads = list(self.handler_threads)
        for thread in threads:
            if thread is not threading.current_thread():
                thread.join(timeout=max(0, deadline-time.monotonic()))
        with self.handler_lock:
            return not any(t.is_alive() for t in self.handler_threads)


def file_identity(info):
    return info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns


def capture_job(folder, *, base=Path("/tmp"), limit=CAPTURE_LIMIT):
    """Read only completed original child diagnostics, never follow a symlink."""
    folder, base = Path(folder), Path(base)
    need(folder.parent == base and folder.name.startswith("idp-recognition-"), "CAPTURE_JOB_SCOPE")
    before = folder.lstat()
    need(stat.S_ISDIR(before.st_mode) and not folder.is_symlink() and before.st_uid == os.getuid(), "CAPTURE_JOB_OWNER")
    directory = os.open(folder, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    output = []
    try:
        need(file_identity(os.fstat(directory)) == file_identity(before), "CAPTURE_JOB_REPLACED")
        for name in CAPTURE_NAMES:
            try:
                descriptor = os.open(name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=directory)
            except FileNotFoundError:
                output.append({"name": name, "present": False})
                continue
            with os.fdopen(descriptor, "rb") as stream:
                info = os.fstat(stream.fileno())
                need(stat.S_ISREG(info.st_mode) and info.st_uid == os.getuid(), "CAPTURE_FILE_OWNER_OR_TYPE")
                need(info.st_size <= limit, "CAPTURE_FILE_LIMIT")
                raw = stream.read(limit + 1)
                need(len(raw) <= limit and len(raw) == info.st_size, "CAPTURE_FILE_LIMIT_OR_CHANGED")
                need(file_identity(os.fstat(stream.fileno())) == file_identity(info), "CAPTURE_FILE_CHANGED")
                output.append({"name": name, "present": True, "bytes": len(raw),
                               "sha256": hashlib.sha256(raw).hexdigest(),
                               "content_b64": base64.b64encode(raw).decode("ascii"), "truncated": False})
        return output
    finally:
        os.close(directory)


class Observer:
    """Observers never replace original return values, exception objects or codes."""
    def __init__(self, sender=emit, capture=capture_job):
        self.sender, self.capture = sender, capture
        self.document_id = None
        self.request_id = None
        self.observer_errors = []
        self.exception_count = 0
        self.capture_count = 0
        self.spawned_children = []

    def report(self, kind, **value):
        try:
            self.sender(kind, document_id=self.document_id, request_id=self.request_id, **value)
        except Exception as error:
            self.observer_errors.append({"stage": "OBSERVER_EMIT", "type": type(error).__name__})

    def exception(self, error, stage):
        self.exception_count += 1
        try:
            self.report("EXCEPTION", stage=stage, exception_type=type(error).__name__,
                        message=str(error), traceback="".join(traceback.format_exception(type(error), error, error.__traceback__)))
        except Exception as observation_error:
            self.observer_errors.append({"stage": stage, "type": type(observation_error).__name__})

    def safe_error_hook(self, original):
        @functools.wraps(original)
        def observed(error, identity=None):
            self.exception(error, "GATEWAY_SAFE_ERROR_BEFORE_MAPPING")
            return original(error, identity)
        return observed

    def child_hook(self, original):
        @functools.wraps(original)
        def observed(session, folder, operation, consume):
            try:
                return original(session, folder, operation, consume)
            except BaseException as error:
                self.exception(error, "SESSION_CHILD_ORIGINAL_EXCEPTION")
                raise
            finally:
                try:
                    for item in self.capture(folder):
                        self.capture_count += 1
                        self.report("CAPTURE", operation=operation, **item)
                except Exception as error:
                    self.observer_errors.append({"stage": "CAPTURE_CHILD_BEFORE_TEMP_DELETE", "type": type(error).__name__})
                    self.report("OBSERVER_ERROR", stage="CAPTURE_CHILD_BEFORE_TEMP_DELETE", error_type=type(error).__name__)
        return observed

    def popen_hook(self, original):
        @functools.wraps(original)
        def observed(*args, **kwargs):
            argv = args[0] if args else kwargs.get("args")
            paddle_child = isinstance(argv, (list, tuple)) and "/app/paddle_runner.py" in argv
            try:
                process = original(*args, **kwargs)
            except BaseException as error:
                if paddle_child:
                    self.exception(error, "PADDLE_CHILD_SPAWN_FAILED")
                raise
            if paddle_child:
                record = {"pid": process.pid, "argv": list(argv), "spawn_returned_at_monotonic": time.monotonic()}
                self.spawned_children.append(record)
                self.report("CHILD_SPAWN", **record)
            return process
        return observed

    def install(self, gateway):
        self.original_safe_error = gateway.safe_error
        self.original_child = gateway.RECOGNITION.Session.child
        self.original_popen = gateway.subprocess.Popen
        gateway.safe_error = self.safe_error_hook(self.original_safe_error)
        gateway.RECOGNITION.Session.child = self.child_hook(self.original_child)
        gateway.subprocess.Popen = self.popen_hook(self.original_popen)

    def restore(self, gateway):
        gateway.safe_error = self.original_safe_error
        gateway.RECOGNITION.Session.child = self.original_child
        gateway.subprocess.Popen = self.original_popen


def api(port, path, body=None, timeout=5):
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=timeout)
    try:
        connection.request("POST" if body is not None else "GET", path,
                           body=canonical(body) if body is not None else None,
                           headers={"Content-Type": "application/json"})
        response = connection.getresponse()
        raw = response.read(1024**2 + 1)
        need(len(raw) <= 1024**2, "CONTROL_RESPONSE_LIMIT")
        return response.status, json.loads(raw)
    finally:
        connection.close()


def child_pids():
    children = []
    for path in Path("/proc").glob("[0-9]*/cmdline"):
        try:
            raw = path.read_bytes()
        except (OSError, ProcessLookupError):
            continue
        if b"/app/paddle_runner.py\x00" in raw or b"/app/runtime_probe.py\x00" in raw:
            children.append(int(path.parent.name))
    return sorted(children)


def compute_processes(timeout=3):
    result = subprocess.run(["nvidia-smi", "--query-compute-apps=pid,process_name,used_memory",
                             "--format=csv,noheader,nounits"], capture_output=True, timeout=timeout, check=False)
    need(result.returncode == 0, "GPU_COMPUTE_INVENTORY_UNAVAILABLE")
    return result.stdout.decode("utf-8", "replace").strip()


def resource_sample():
    result = {"at_monotonic": time.monotonic(), "worker_rss_bytes": None,
              "gateway_cgroup_memory_current_bytes": None, "GPU_global_sample": None,
              "GPU_global_usage_is_not_attributed_to_model": True}
    try:
        match = re.search(r"^VmRSS:\s+(\d+)\s+kB$", Path("/proc/self/status").read_text(), re.M)
        result["worker_rss_bytes"] = int(match[1]) * 1024 if match else None
        result["gateway_cgroup_memory_current_bytes"] = int(Path("/sys/fs/cgroup/memory.current").read_text())
    except (OSError, ValueError):
        pass
    try:
        sample = subprocess.run(["nvidia-smi", "--query-gpu=memory.used,memory.total,utilization.gpu",
                                 "--format=csv,noheader,nounits"], capture_output=True, timeout=2, check=False)
        result["GPU_global_sample"] = sample.stdout.decode().strip() if sample.returncode == 0 else None
    except (OSError, subprocess.TimeoutExpired):
        pass
    return result


def need_health(value, expected):
    need(value.get("ready") is True and value.get("busy") is False
         and value.get("memory_lifecycle", {}).get("status") == "AVAILABLE"
         and value.get("gpu_concurrency") == 1
         and value.get("runtime_provenance", {}).get("gateway", {}).get("files_sha256") == expected,
         "GATEWAY_NOT_READY_IDLE_OR_EXACT_SOURCE")
    return value


def main_health(expected, timeout=10):
    status, value = api(8088, "/health", timeout=timeout)
    need(status == 200, "MAIN_HEALTH_HTTP")
    return need_health(value, expected)


def load_gateway(expected):
    spec = importlib.util.spec_from_file_location("paddle_diagnostic_gateway", "/app/server.py")
    gateway = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(gateway)
    need(gateway.STARTUP_HASHES == expected, "ACTUAL_GATEWAY_SOURCE_CHANGED")
    return gateway


def make_budget(gateway, work_seconds, cleanup_seconds=30, *, started=None):
    started = time.monotonic() if started is None else started
    budget = gateway.MEMORY.Budget(work_seconds + cleanup_seconds, work_seconds + cleanup_seconds, started=started)
    budget.cleanup_reserve = float(cleanup_seconds)
    budget.work_deadline = started + work_seconds
    budget.deadline = started + work_seconds + cleanup_seconds
    return budget


def close_proof(gateway, identity, budget, expected, server=None):
    if server is not None:
        need(server.join_handlers(min(1, budget.remaining(cleanup=True))), "DIAGNOSTIC_HTTP_HANDLER_NOT_JOINED")
    need(not gateway.ACTIVE and not gateway.UPSTREAM, "DIAGNOSTIC_PROCESS_OR_READER_NOT_CLOSED")
    need(not child_pids(), "GPU_CHILD_OR_PROBE_NOT_CLOSED")
    released = gateway.memory_barrier(identity, budget, cleanup=True)
    ps = gateway.memory_request(identity, budget, "/ps", cleanup=True)
    need(isinstance(ps, dict) and ps.get("models") == [], "OLLAMA_RESIDENCY_NOT_EMPTY")
    processes = compute_processes(timeout=min(3, budget.remaining(cleanup=True)))
    need(not processes, "GPU_COMPUTE_RESIDENCY_NOT_EMPTY")
    shadow = need_health(gateway.health(), expected)
    main = main_health(expected, timeout=min(10, budget.remaining(cleanup=True)))
    need(not gateway.ACTIVE and not gateway.UPSTREAM and not child_pids(), "FINAL_GPU_OWNER_OR_READER_NOT_CLOSED")
    return {"resources_released": True, "diagnostic_ACTIVE_empty": True, "diagnostic_UPSTREAM_empty": True,
            "diagnostic_HTTP_handlers_joined": server is not None,
            "child_or_probe_pids": [], "ollama_ps": ps, "nvidia_compute_processes": processes,
            "diagnostic_memory_lifecycle": released, "diagnostic_health": shadow, "main8088_health": main}


def adjusted_request(a_request, remaining_work):
    # Same work-reserve inversion as the frozen documentary worker run_a.
    total = remaining_work + 5 if remaining_work >= 45 else remaining_work / .9
    return {**a_request, "request_budget_seconds": min(a_request["request_budget_seconds"], total)}


def read_recognition(port, request, budget, *, sender=emit, connection_factory=http.client.HTTPConnection):
    connection = connection_factory("127.0.0.1", port, timeout=max(1, budget.join_remaining()))
    count, pages, terminal = 0, 0, None
    try:
        actual = adjusted_request(request, budget.remaining())
        sender("A_REQUEST_ACTUAL", request_budget_seconds=actual["request_budget_seconds"],
               source_sha256=actual["source_sha256"], definition_sha256=actual["definition_sha256"],
               configuration_sha256=actual["configuration_sha256"])
        connection.request("POST", "/recognize", body=canonical(actual), headers={"Content-Type": "application/json"})
        response = connection.getresponse()
        sender("HTTP_HEADERS", status=response.status, content_type=response.getheader("Content-Type"),
               handler_scope="ISOLATED_DIAGNOSTIC_HANDLER_NOT_MAIN8088")
        if response.status != 200:
            raw = response.read(1024**2 + 1)
            need(len(raw) <= 1024**2, "ERROR_RESPONSE_LIMIT")
            sender("A_RAW", content_b64=base64.b64encode(raw).decode("ascii"))
            try:
                code = json.loads(raw).get("error")
            except (ValueError, TypeError, AttributeError):
                code = None
            return {"status": "TIMEOUT" if code == "INFERENCE_TIMEOUT" else "ERROR",
                    "failure_code": code or "PADDLE_HTTP_" + str(response.status), "http_status": response.status,
                    "terminal": None, "page_events": 0, "actual_HTTP_requests": 1}
        while True:
            budget.remaining(cleanup=True)
            if connection.sock is not None:
                connection.sock.settimeout(max(.01, budget.join_remaining()))
            line = response.readline(24 * 1024**2 + 1)
            if not line:
                break
            count += len(line)
            need(len(line) <= 24 * 1024**2 and count <= 64 * 1024**2, "RECOGNITION_STREAM_LIMIT")
            sender("A_RAW", content_b64=base64.b64encode(line).decode("ascii"))
            event = json.loads(line)
            pages += int(event.get("kind") == "PAGE")
            if event.get("kind") == "TERMINAL":
                terminal = event
                break
        need(terminal is not None and terminal.get("resources_released") is True, "PADDLE_TERMINAL_CLOSE_UNVERIFIED")
        state = terminal.get("status")
        return {"status": "COMPLETE" if state == "COMPLETE" else "TIMEOUT" if state == "TIMEOUT" else "CANCELLED" if state == "CANCELLED" else "ERROR",
                "failure_code": terminal.get("payload", {}).get("error_code"), "http_status": response.status,
                "terminal": terminal, "page_events": pages, "actual_HTTP_requests": 1}
    finally:
        connection.close()


class AttemptGate:
    """No models here: irreversible admission tracking for the two fixed inputs."""
    def __init__(self, config):
        need(config["allowed_documents"] in (["F02-02"], ["F02-02", "F01-02"]), "FIXED_DOCUMENT_SET")
        self.allowed = list(config["allowed_documents"])
        self.hashes = config["document_sha256"]
        need(set(self.hashes) == set(self.allowed) and all(re.fullmatch(r"[a-f0-9]{64}", x) for x in self.hashes.values()), "FIXED_DOCUMENT_HASHES")
        self.consumed = []
        self.closed = True

    def admit(self, command):
        need(self.closed, "PREVIOUS_CLOSURE_UNCONFIRMED")
        identity, request = command["document_id"], command["a_request"]
        need(identity in self.allowed and identity not in self.consumed and len(self.consumed) < 2, "DOCUMENT_ALREADY_CONSUMED_OR_NOT_ALLOWED")
        need(identity == "F02-02" if not self.consumed else identity == "F01-02", "FIXED_DOCUMENT_ORDER")
        need(command.get("work_seconds") == 180, "FIXED_WORK_SECONDS")
        need(request["source_sha256"] == self.hashes[identity], "DOCUMENT_HASH_BINDING")
        cfg = request["configuration"]
        need(cfg.get("extraction_strategy") == "PADDLE_STRUCTURED" and cfg.get("use_llm") is False
             and cfg.get("paddle_model") == "PaddleOCR-VL-1.6" and cfg.get("timeout_seconds") == 185, "FIXED_PADDLE_ONLY_CONFIGURATION")
        need(hashlib.sha256(base64.b64decode(request["source"], validate=True)).hexdigest() == self.hashes[identity], "SOURCE_BYTES_CHANGED")
        need(re.fullmatch(r"[a-f0-9]{32}", request["request_id"]), "REQUEST_ID")
        self.consumed.append(identity)  # Before any network dispatch; never refunded.
        self.closed = False
        return identity, request


def run_attempt(gateway, port, observer, command, expected, campaign_deadline, server=None):
    identity, request = command["document_id"], command["a_request"]
    started = time.monotonic()
    need(campaign_deadline - started >= 210, "CAMPAIGN_REMAINING_INSUFFICIENT")
    budget = make_budget(gateway, 180, started=started)
    observer.document_id, observer.request_id = identity, request["request_id"]
    before_errors = len(observer.observer_errors)
    before_spawns = len(observer.spawned_children)
    stop, samples = threading.Event(), []
    def sample():
        while not stop.is_set():
            samples.append(resource_sample())
            stop.wait(5)
    sampler = threading.Thread(target=sample, name="paddle-diagnostic-resource-sampler", daemon=True)
    sampler.start()
    result, closure = {}, None
    try:
        need(not child_pids() and not compute_processes(), "FOREIGN_GPU_OR_CHILD_ON_ENTRY")
        main_health(expected)
        need_health(gateway.health(), expected)
        gateway.memory_barrier(request["request_id"], budget)
        emit("RUN_ADMITTED", document_id=identity, request_id=request["request_id"], source_sha256=request["source_sha256"],
             work_seconds=180, cleanup_seconds=30, Qwen_calls=0, retries=0)
        result = read_recognition(port, request, budget)
    except Exception as error:
        observer.exception(error, "DIAGNOSTIC_CLIENT_OR_CONTROL")
        result = {"status": "TIMEOUT" if isinstance(error, (TimeoutError, subprocess.TimeoutExpired)) else "ERROR",
                  "failure_code": type(error).__name__ + ":" + str(error), "actual_HTTP_requests": None}
        try:
            emit("CANCEL_CONTROL", response=api(port, "/cancel", {"request_id": request["request_id"]}, timeout=3))
        except Exception as cancel_error:
            observer.exception(cancel_error, "DIAGNOSTIC_CANCEL_CONTROL")
    finally:
        work_ended = time.monotonic()
        budget.deadline = min(budget.deadline, work_ended + 30, campaign_deadline)
        stop.set()
        sampler.join(timeout=max(0, min(3, budget.deadline-time.monotonic())))
        while time.monotonic() < budget.deadline:
            try:
                closure = close_proof(gateway, request["request_id"], budget, expected, server)
                break
            except Exception as error:
                result["closure_failure"] = type(error).__name__ + ":" + str(error)
                time.sleep(min(.2, max(0, budget.deadline - time.monotonic())))
        if sampler.is_alive():
            closure = None
            result["closure_failure"] = "RESOURCE_SAMPLER_JOIN_UNVERIFIED"
        result.update(document_id=identity, request_id=request["request_id"], resources_released=closure is not None,
                      close_proof=closure, resource_samples=samples, work_seconds=round(work_ended-started, 6),
                      cleanup_seconds=round(time.monotonic()-work_ended, 6), total_seconds=round(time.monotonic()-started, 6),
                      observer_errors=observer.observer_errors[before_errors:], actual_Qwen_calls=0,
                      actual_Paddle_child_spawns=len(observer.spawned_children)-before_spawns,
                      Paddle_child_spawn_records=observer.spawned_children[before_spawns:],
                      Paddle_internal_VLM_calls=None, handler_scope="ISOLATED_DIAGNOSTIC_HANDLER_NOT_MAIN8088",
                      private_traceback_and_capture=True, operational_approval_measured=False)
        emit("RESULT", result=result)
    return result


def read_control(deadline):
    remaining = deadline - time.monotonic()
    need(remaining > 0, "DIAGNOSTIC_LIFETIME_EXHAUSTED")
    ready, _, _ = select.select([sys.stdin], [], [], remaining)
    need(bool(ready), "DIAGNOSTIC_LIFETIME_EXHAUSTED")
    raw = sys.stdin.buffer.readline(48 * 1024**2 + 1)
    need(bool(raw) and len(raw) <= 48 * 1024**2, "CONTROL_EOF_OR_LIMIT")
    return json.loads(raw)


def main(config):
    import fcntl
    limit = config["campaign_remaining_seconds"]
    need(type(limit) in (int, float) and 0 < limit <= 900, "CAMPAIGN_LIMIT")
    deadline = time.monotonic() + limit
    gate = AttemptGate(config)
    expected = config["startup_files_sha256"]
    lock_path = Path("/tmp/trackvance-paddle-service-diagnosis.lock")
    lock_descriptor = os.open(lock_path, os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    lock = os.fdopen(lock_descriptor, "a+b")
    lock_info = os.fstat(lock.fileno())
    need(stat.S_ISREG(lock_info.st_mode) and lock_info.st_uid == os.getuid(), "DIAGNOSTIC_LOCK_OWNER_OR_TYPE")
    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    marker = Path("/tmp/trackvance-paddle-service-diagnosis.active.json")
    marker_descriptor = os.open(marker, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(marker_descriptor, "wb") as marker_stream:
        marker_stream.write(canonical({"pid": os.getpid(), "diagnostic": "PADDLE_SERVICE_FOCUSED"}))
    marker_info = marker.lstat()
    owned_temporaries = [{"path": str(lock_path), "device": lock_info.st_dev, "inode": lock_info.st_ino,
                          "uid": lock_info.st_uid, "type": "WORKER_LOCK", "remove_only_after_worker_exit": True},
                         {"path": str(marker), "device": marker_info.st_dev, "inode": marker_info.st_ino,
                          "uid": marker_info.st_uid, "type": "WORKER_ACTIVE_MARKER", "remove_only_after_close_proof": True}]
    gateway = observer = server = serving = None
    closed = False
    try:
        main_health(expected)
        need(not child_pids() and not compute_processes(), "FOREIGN_GPU_OR_PROBE_ON_ENTRY")
        gateway = load_gateway(expected)
        gateway.probe_runtime()  # Real unchanged probe, no fabricated ready state.
        need(gateway.RUNTIME.get("ready") is True and gateway.RUNTIME.get("kernel_probe") == "PASS", "SHADOW_REAL_KERNEL_PROBE_FAILED")
        health = need_health(gateway.health(), expected)  # Original model guards, including normal first hash.
        need(not child_pids() and not compute_processes(), "PROBE_NOT_CLOSED")
        observer = Observer()
        observer.install(gateway)
        server = DiagnosticHTTPServer(("127.0.0.1", 0), gateway.Handler)
        serving = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": .1}, name="paddle-diagnostic-http", daemon=False)
        serving.start()
        port = server.server_address[1]
        emit("READY", worker_pid=os.getpid(), diagnostic_port=port,
             handler_scope="ISOLATED_DIAGNOSTIC_HANDLER_NOT_MAIN8088", startup_files_sha256=gateway.STARTUP_HASHES,
             runtime_probe=gateway.RUNTIME, health=health, child_or_probe_pids=[], nvidia_compute_processes="",
             normal_probe_and_model_guards_unmodified=True, model_generations=0, owned_temporaries=owned_temporaries)
        while True:
            command = read_control(deadline)
            if command.get("kind") == "CLOSE":
                need(gate.closed, "CANNOT_CLOSE_WITHOUT_RESOURCE_PROOF")
                closed = True
                break
            need(command.get("kind") == "RUN", "CONTROL_KIND")
            gate.admit(command)
            result = run_attempt(gateway, port, observer, command, expected, deadline, server)
            gate.closed = result["resources_released"] is True
            if not gate.closed:
                emit("ADMISSION_CLOSED", reason="GPU_CLOSURE_UNCONFIRMED", consumed=gate.consumed)
                break
    except Exception as error:
        if observer is not None:
            observer.exception(error, "WORKER_CONTROL")
        else:
            emit("WORKER_ERROR", exception_type=type(error).__name__, message=str(error), traceback=traceback.format_exc())
        raise
    finally:
        if server is not None:
            if serving is not None and serving.is_alive():
                server.shutdown()
            server.server_close()
            closed = closed and server.join_handlers(3)
        if serving is not None:
            serving.join(timeout=3)
            closed = closed and not serving.is_alive()
        if gateway is not None:
            closed = closed and not gateway.ACTIVE and not gateway.UPSTREAM and not child_pids()
            if observer is not None:
                observer.restore(gateway)
            gateway.FAILURE_DIAGNOSTICS.close(clear=False)
        if closed:
            info = marker.lstat()
            if (info.st_dev, info.st_ino, info.st_uid) == (marker_info.st_dev, marker_info.st_ino, marker_info.st_uid) and stat.S_ISREG(info.st_mode):
                marker.unlink()
            else:
                closed = False
        emit("WORKER_CLOSED", effective_close=closed, admission_closed=True, consumed=gate.consumed,
             serving_thread_joined=serving is None or not serving.is_alive(), marker_retained=not closed,
             actual_Qwen_calls=0, no_app_or_environment_edits=True, owned_temporaries=owned_temporaries,
             diagnostic_failure_ring_path=str(gateway.FAILURE_DIAGNOSTICS.root) if gateway is not None and gateway.FAILURE_DIAGNOSTICS.root is not None else None)
        lock.close()


if __name__ == "__main__":
    raw = sys.stdin.buffer.readline(1024**2 + 1)
    need(bool(raw) and len(raw) <= 1024**2, "INITIAL_CONTROL_LIMIT")
    main(json.loads(raw))
