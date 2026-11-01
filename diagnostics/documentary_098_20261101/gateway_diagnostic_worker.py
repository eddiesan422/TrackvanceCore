"""Diagnostic-only invocation inside the existing isolated gateway.

No database, product events, publication, package install or operational edit.
A calls the real gateway HTTP interface; B/C call its existing joined reader.
The host durably reserves the entire route/page budget before invoking this file.
"""
from __future__ import annotations

import base64
import hashlib
import http.client
import importlib.util
import io
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import threading
import time


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()


def digest(value):
    return hashlib.sha256(canonical(value)).hexdigest()


def strict_json(raw):
    def pairs(items):
        result = {}
        for key, value in items:
            need(key not in result, "DUPLICATE_JSON_KEY")
            result[key] = value
        return result
    def constant(_value):
        raise ValueError("NONFINITE_JSON_VALUE")
    return json.loads(raw, object_pairs_hook=pairs, parse_constant=constant)


def need(value, reason):
    if not value:
        raise ValueError(reason)


def emit(kind, **value):
    sys.stdout.buffer.write(canonical({"kind": kind, **value}) + b"\n")
    sys.stdout.buffer.flush()


def api(path, body=None, timeout=5):
    connection = http.client.HTTPConnection("127.0.0.1", 8088, timeout=timeout)
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
        status = Path("/proc/self/status").read_text()
        match = re.search(r"^VmRSS:\s+(\d+)\s+kB$", status, re.M)
        result["worker_rss_bytes"] = int(match[1]) * 1024 if match else None
        result["gateway_cgroup_memory_current_bytes"] = int(Path("/sys/fs/cgroup/memory.current").read_text())
    except (OSError, ValueError):
        pass
    try:
        row = subprocess.run(["nvidia-smi", "--query-gpu=memory.used,memory.total,utilization.gpu",
                              "--format=csv,noheader,nounits"], capture_output=True, timeout=2, check=False)
        result["GPU_global_sample"] = row.stdout.decode().strip() if row.returncode == 0 else None
    except (OSError, subprocess.TimeoutExpired):
        pass
    return result


def output_schema(schema):
    cell = {"type": ["string", "null"]}
    fields = {x["name"]: cell for x in schema["fields"]}
    tables = {x["name"]: {"type": "array", "items": {"type": "object", "additionalProperties": False,
              "properties": {c["name"]: cell for c in x["columns"]},
              "required": [c["name"] for c in x["columns"]]}} for x in schema["tables"]}
    return {"type": "object", "additionalProperties": False,
            "properties": {"fields": {"type": "object", "additionalProperties": False,
                "properties": fields, "required": list(fields)},
                "tables": {"type": "object", "additionalProperties": False,
                "properties": tables, "required": list(tables)},
                "uncertainty": {"type": "array", "items": {"type": "object", "additionalProperties": False,
                    "properties": {"path": {"type": "string"}, "reason": {"type": "string"},
                        "alternatives": {"type": "array", "items": {"type": "string"}}},
                    "required": ["path", "reason", "alternatives"]}}},
            "required": ["fields", "tables", "uncertainty"]}


def validate_semantic(value, schema):
    need(isinstance(value, dict) and set(value) == {"fields", "tables", "uncertainty"}, "SEMANTIC_JSON_SHAPE")
    need(isinstance(value["fields"], dict) and set(value["fields"]) == {f["name"] for f in schema["fields"]}, "SEMANTIC_FIELDS_SHAPE")
    need(all(x is None or isinstance(x, str) for x in value["fields"].values()), "SEMANTIC_CELL_TYPE")
    need(isinstance(value["tables"], dict) and set(value["tables"]) == {t["name"] for t in schema["tables"]}, "SEMANTIC_TABLES_SHAPE")
    for table in schema["tables"]:
        rows = value["tables"][table["name"]]
        need(isinstance(rows, list) and len(rows) <= 10000, "SEMANTIC_ROWS_SHAPE")
        for row in rows:
            need(isinstance(row, dict) and set(row) == {f["name"] for f in table["columns"]}
                 and all(x is None or isinstance(x, str) for x in row.values()), "SEMANTIC_ROW_SHAPE")
    need(isinstance(value["uncertainty"], list) and len(value["uncertainty"]) <= 10000, "SEMANTIC_UNCERTAINTY_SHAPE")
    for item in value["uncertainty"]:
        need(isinstance(item, dict) and set(item) == {"path", "reason", "alternatives"}
             and isinstance(item["path"], str) and isinstance(item["reason"], str)
             and isinstance(item["alternatives"], list) and all(isinstance(a, str) for a in item["alternatives"]),
             "SEMANTIC_UNCERTAINTY_ITEM")
    return value


def load_gateway(expected):
    spec = importlib.util.spec_from_file_location("documentary_gateway_runtime", "/app/server.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    need(module.STARTUP_HASHES == expected, "ACTUAL_GATEWAY_SOURCE_CHANGED")
    return module


def ready(expected, timeout=10):
    status, health = api("/health", timeout=timeout)
    need(status == 200 and health.get("ready") is True and health.get("busy") is False
         and health.get("memory_lifecycle", {}).get("status") == "AVAILABLE"
         and health.get("gpu_concurrency") == 1
         and health.get("runtime_provenance", {}).get("gateway", {}).get("files_sha256") == expected,
         "GATEWAY_NOT_READY_AND_EXCLUSIVE")
    return health


def close_proof(gateway, identity, budget, expected):
    health = ready(expected, timeout=min(10, budget.remaining(cleanup=True)))
    need(not child_pids(), "GPU_CHILD_NOT_CLOSED")
    released = gateway.memory_barrier(identity, budget, cleanup=True)
    need(not gateway.ACTIVE and not gateway.UPSTREAM and not child_pids(), "GPU_PROCESS_OR_READER_NOT_CLOSED")
    ps = gateway.memory_request(identity, budget, "/ps", cleanup=True)
    need(ps.get("models") == [], "OLLAMA_RESIDENCY_NOT_EMPTY")
    processes = compute_processes(timeout=min(3, budget.remaining(cleanup=True)))
    need(not processes, "GPU_COMPUTE_RESIDENCY_NOT_EMPTY")
    return {"resources_released": True, "joined_reader_registry_empty": True,
            "paddle_child_pids": [], "ollama_ps": ps, "nvidia_compute_processes": processes,
            "memory_lifecycle": released, "main_gateway_health": health}


def run_a(packet, budget):
    identity = packet["request_id"]
    connection = http.client.HTTPConnection("127.0.0.1", 8088, timeout=max(1, budget.join_remaining()))
    terminal, count, http_status = None, 0, None
    try:
        remaining_work = budget.remaining()
        # Existing gateway reserves min(5, request_duration*.1). Invert that
        # formula so its real work deadline never exceeds our remaining work.
        total_for_remaining_work = remaining_work+5 if remaining_work >= 45 else remaining_work/.9
        actual = {**packet["a_request"], "request_budget_seconds": min(packet["a_request"]["request_budget_seconds"], total_for_remaining_work)}
        emit("A_REQUEST_ACTUAL", request_budget_seconds=actual["request_budget_seconds"],
             source_sha256=actual["source_sha256"], definition_sha256=actual["definition_sha256"], configuration_sha256=actual["configuration_sha256"])
        connection.request("POST", "/recognize", body=canonical(actual), headers={"Content-Type": "application/json"})
        response = connection.getresponse()
        http_status = response.status
        emit("HTTP_HEADERS", status=http_status, content_type=response.getheader("Content-Type"))
        if response.status != 200:
            raw = response.read(1024**2 + 1)
            emit("A_RAW", content_b64=base64.b64encode(raw).decode())
            return {"status": "ERROR", "failure_code": "PADDLE_HTTP_" + str(response.status),
                    "http_status": http_status, "terminal": None}
        while True:
            budget.remaining(cleanup=True)  # Gateway itself enforces A's 180-second work deadline.
            line = response.readline(24 * 1024**2 + 1)
            if not line:
                break
            count += len(line)
            need(len(line) <= 24 * 1024**2 and count <= 64 * 1024**2, "A_STREAM_LIMIT")
            emit("A_RAW", content_b64=base64.b64encode(line).decode())
            event = strict_json(line)
            if event.get("kind") == "TERMINAL":
                terminal = event
                break
        need(terminal is not None and terminal.get("resources_released") is True, "PADDLE_TERMINAL_CLOSE_UNVERIFIED")
        state = terminal.get("status")
        return {"status": "COMPLETE" if state == "COMPLETE" else "TIMEOUT" if state == "TIMEOUT" else "ERROR",
                "failure_code": terminal.get("payload", {}).get("error_code"),
                "http_status": http_status, "terminal": terminal}
    finally:
        connection.close()


def run_qwen(packet, gateway, budget):
    config = packet["configuration"]
    route, pages, calls = packet["route"], [], 0
    identity = packet["request_id"]
    need(gateway.qwen_verified(), "LLM_MODEL_INTEGRITY")
    gateway.BUDGETS[identity] = budget
    combined = {p["page"]: p for p in packet.get("a_pages", [])}
    schema = packet["schema"]
    for page in packet["pages"]:
        budget.remaining()
        image = base64.b64decode(page["image_b64"], validate=True)
        need(hashlib.sha256(image).hexdigest() == page["image_sha256"], "IMAGE_INTEGRITY")
        user = {"page": page["page"], "document_pages": len(packet["pages"]), "semantic_schema": schema}
        if route == "C_PADDLE_QWEN":
            paddle = combined[page["page"]]
            raw = base64.b64decode(paddle["content_b64"], validate=True)
            need(hashlib.sha256(raw).hexdigest() == paddle["sha256"], "EXACT_A_PAGE_INTEGRITY")
            # A's exact original UTF-8 bytes are passed as a string, not a summary.
            user["paddle_page_json_utf8"] = raw.decode("utf-8")
            user["paddle_page_sha256"] = paddle["sha256"]
        body = {"model": gateway.LOCK["qwen"]["model"], "think": False, "keep_alive": 0,
                "format": output_schema(schema), "truncate": False, "shift": False,
                "messages": [{"role": "system", "content": config["prompts"][route]},
                    {"role": "user", "content": json.dumps(user, ensure_ascii=False), "images": [page["image_b64"]]}],
                "options": config["generation_options"]}
        metadata = {**body, "messages": [{k: v for k, v in m.items() if k != "images"} for m in body["messages"]]}
        emit("CALL_ADMITTED", route=route, page=page["page"], request_sha256=digest(body),
             image_sha256=page["image_sha256"], request_without_images=metadata)
        calls += 1
        started = time.monotonic()
        stream = io.BytesIO()
        result = {"page": page["page"], "image_sha256": page["image_sha256"], "request_sha256": digest(body)}
        try:
            reply = gateway.stream_chat(identity, body, timeout=budget.remaining(), raw_capture=stream.write)
            result["reply"] = reply
            result["tokens"] = gateway.ollama_metrics(reply)
            result["semantic"] = validate_semantic(strict_json(reply.get("message", {}).get("content", "")), schema)
            need(reply.get("done") is True and reply.get("done_reason") == "stop", "LLM_INCOMPLETE")
            metrics = result["tokens"]
            need(metrics["prompt_eval_count"] is not None and metrics["eval_count"] is not None, "LLM_BUDGET_UNVERIFIED")
            need(metrics["prompt_eval_count"] <= config["context_tokens"] - config["output_tokens"]
                 and metrics["eval_count"] <= config["output_tokens"], "LLM_CONTEXT_LIMIT")
            result["status"] = "COMPLETE"
        except Exception as error:
            result.update(status="TIMEOUT" if isinstance(error, TimeoutError) else "UNSUPPORTED" if "DOCUMENT_TYPE_UNSUPPORTED" in str(error) else "PARTIAL" if "LLM_INCOMPLETE" in str(error) else "ERROR",
                          failure_code=type(error).__name__ + ":" + str(error)[:180])
        result["raw_stream_b64"] = base64.b64encode(stream.getvalue()).decode()
        result["elapsed_seconds"] = round(time.monotonic() - started, 6)
        pages.append(result)
        emit("PAGE", page=result)
        if result["status"] != "COMPLETE":
            return {"status": result["status"], "pages": pages, "call_count": calls, "failure_code": result["failure_code"]}
        gateway.memory_barrier(identity, budget)
    return {"status": "COMPLETE", "pages": pages, "call_count": calls}


def main(packet):
    import fcntl
    started = time.monotonic()
    lock = open("/tmp/trackvance-documentary-evaluation.lock", "a+b")
    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    marker = Path("/tmp/trackvance-documentary-evaluation.active.json")
    need(not marker.exists(), "PREVIOUS_DIAGNOSTIC_UNCLOSED")
    marker.write_bytes(canonical({"pid": os.getpid(), "request_id": packet["request_id"], "route": packet["route"]}))
    gateway = load_gateway(packet["startup_files_sha256"])
    budget = gateway.MEMORY.Budget(packet["work_seconds"] + 30, packet["work_seconds"] + 30, started=started)
    budget.cleanup_reserve = 30.0
    budget.work_deadline = started + packet["work_seconds"]
    budget.deadline = started + packet["work_seconds"] + 30
    stop = threading.Event()
    samples = []
    def sample():
        while not stop.is_set():
            samples.append(resource_sample())
            stop.wait(5)
    thread = threading.Thread(target=sample, daemon=True)
    result, closure, error = {}, None, None
    thread.start()
    try:
        ready(packet["startup_files_sha256"])
        need(not child_pids() and not compute_processes(), "FOREIGN_GPU_OR_CHILD_ON_ENTRY")
        gateway.memory_barrier(packet["request_id"], budget)
        emit("STARTED", worker_pid=os.getpid(), route=packet["route"], work_seconds=packet["work_seconds"])
        result = run_a(packet, budget) if packet["route"] == "A_PADDLE" else run_qwen(packet, gateway, budget)
    except Exception as failure:
        error = failure
        result = {"status": "TIMEOUT" if isinstance(failure, TimeoutError) else "ERROR",
                  "failure_code": type(failure).__name__ + ":" + str(failure)[:180]}
        if packet["route"] == "A_PADDLE":
            try:
                emit("CANCEL_CONTROL", response=api("/cancel", {"request_id": packet["request_id"]}, timeout=3))
            except Exception as cancel_error:
                emit("CANCEL_CONTROL_FAILED", error=type(cancel_error).__name__)
    finally:
        work_ended = time.monotonic()
        deadline = min(budget.deadline, work_ended + 30)
        budget.deadline = deadline
        stop.set()
        thread.join(timeout=max(0, min(3, budget.join_remaining())))
        # Main gateway may still be finishing its own fixed five-second cleanup.
        while time.monotonic() < deadline:
            try:
                closure = close_proof(gateway, packet["request_id"], budget, packet["startup_files_sha256"])
                break
            except Exception as close_error:
                result["closure_failure"] = type(close_error).__name__ + ":" + str(close_error)[:180]
                time.sleep(min(.2, max(0, deadline - time.monotonic())))
        if closure is None and "closure_failure" not in result:
            result["closure_failure"] = "CLEANUP_DEADLINE_EXHAUSTED"
        if thread.is_alive():
            closure = None
            result["closure_failure"] = "RESOURCE_SAMPLER_JOIN_UNVERIFIED"
        if closure is not None:
            marker.unlink()
        result.update(resources_released=closure is not None, close_proof=closure, worker_pid=os.getpid(),
                      resource_samples=samples, work_seconds=round(work_ended-started, 6),
                      cleanup_seconds=round(time.monotonic()-work_ended, 6), total_seconds=round(time.monotonic()-started, 6),
                      diagnostic_only=True, operational_approval_measured=False)
        emit("RESULT", result=result)
        lock.close()


if __name__ == "__main__":
    main(json.load(sys.stdin))
