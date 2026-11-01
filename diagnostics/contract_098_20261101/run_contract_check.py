"""Two-contract diagnostic wrapper around pinned, unchanged prior pure helpers.

All paths, runtime identities and prompts come from an explicit frozen manifest.
No operational code/data is changed. Preparation is CPU-only. Execution is once,
without retries, and preserves unsuccessful cases and original worker streams.
"""
from __future__ import annotations

import argparse
import base64
import copy
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import time
import uuid

ARMS = ("BASE_CONTRACT", "LITERAL_CONTRACT")
ROUTE = "B_QWEN_DIRECT"
DOCKER_OVERRIDES = {"DOCKER_HOST", "DOCKER_CONTEXT", "DOCKER_TLS_VERIFY", "DOCKER_CERT_PATH"}


def need(value, reason):
    if not value:
        raise RuntimeError(reason)


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()


def strict_json(raw):
    def pairs(items):
        value = {}
        for key, item in items:
            need(key not in value, "DUPLICATE_JSON_KEY")
            value[key] = item
        return value
    def constant(_):
        raise ValueError("NONFINITE_JSON_VALUE")
    return json.loads(raw, object_pairs_hook=pairs, parse_constant=constant)


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def ref(path):
    path = Path(path).resolve()
    return {"path": str(path), "bytes": path.stat().st_size, "sha256": sha(path)}


def read(path):
    return strict_json(Path(path).read_bytes())


def pinned(pin):
    need(sha(pin["path"]) == pin["sha256"], "PINNED_FILE_CHANGED")
    return read(pin["path"])


def save(path, value):
    with Path(path).open("xb") as stream:
        stream.write(canonical(value) + b"\n")
        stream.flush()
        os.fsync(stream.fileno())
    return ref(path)


def load_module(pin, name):
    need(sha(pin["path"]) == pin["sha256"], "PINNED_HELPER_CHANGED")
    spec = importlib.util.spec_from_file_location(name, pin["path"])
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def docker(*args, timeout=30):
    result = subprocess.run(["docker", "--context", "default", *map(str, args)],
        capture_output=True, timeout=timeout, env={k: v for k, v in os.environ.items() if k not in DOCKER_OVERRIDES},
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    need(result.returncode == 0, "DOCKER_READ_COMMAND_FAILED")
    return result.stdout


def verify_active(runtime):
    need(docker("info", "--format", "{{.ID}}").decode().strip() == runtime["engine_id"], "ENGINE_CHANGED")
    rows = strict_json(docker("inspect", runtime["gateway_id"], runtime["ollama_id"]))
    for row, role in zip(rows, ("gateway", "ollama")):
        need(row["Id"] == runtime[role + "_id"] and row["Image"] == runtime[role + "_image"]
             and row["State"]["Running"] and not row["State"]["OOMKilled"]
             and not row["HostConfig"].get("PortBindings"), "RUNTIME_ID_IMAGE_EXCLUSION_CHANGED")
    need(set(docker("ps", "-q", "--no-trunc").decode().split()) == {row["Id"] for row in rows},
         "ONLY_GATEWAY_OLLAMA_MAY_RUN")


def infrastructure_failure(result):
    """Technical/opaque failures count conservatively; model JSON errors do not."""
    if result.get("status") == "COMPLETE":
        return False
    code = str(result.get("failure_code") or result.get("reason") or "")
    if any(item in code for item in ("JSONDecodeError", "SEMANTIC_", "LLM_INCOMPLETE",
                                    "LLM_CONTEXT_LIMIT", "NONFINITE_JSON_VALUE", "DUPLICATE_JSON_KEY",
                                    "DOCUMENT_TYPE_UNSUPPORTED")):
        return False
    return result.get("status") in {"ERROR", "TIMEOUT", "UNSUPPORTED", "PARTIAL"}


def step_guard(consecutive, result):
    consecutive = consecutive + 1 if infrastructure_failure(result) else 0
    closure_unknown = result.get("resources_released") is not True
    return consecutive, closure_unknown or consecutive >= 2


class InferenceClock:
    """Persist once at first inference dispatch admission; never reuse setup time."""
    def __init__(self, path):
        self.path = Path(path)
        self.value = read(self.path) if self.path.exists() else None
        if self.value is not None:
            self.elapsed()

    def start(self):
        need(self.value is None and not self.path.exists(), "INFERENCE_CLOCK_ALREADY_STARTED_NO_RESET")
        self.value = {"started_epoch": time.time(), "started_monotonic": time.monotonic(),
                      "trigger": "FIRST_HOST_INFERENCE_DISPATCH_ADMISSION_BEFORE_WORKER",
                      "setup_time_excluded": True, "immutable_no_budget_refund": True}
        save(self.path, self.value)
        return ref(self.path)

    def elapsed(self):
        if self.value is None:
            return 0.0
        elapsed = time.monotonic() - self.value["started_monotonic"]
        need(elapsed >= 0 and abs((time.time() - self.value["started_epoch"]) - elapsed) < 30,
             "INFERENCE_CLOCK_CHANGED")
        return elapsed


def documents(freeze):
    sample = pinned(freeze["sample"])
    rows = sample["documents"]
    need(len(rows) == 6 and len({row["document_id"] for row in rows}) == 6, "EXACT_SIX_DOCUMENTS")
    docs = {}
    for row in rows:
        descriptor = {"path": row["input_file"], "sha256": row["input_sha256"]}
        doc = pinned(descriptor)
        need(doc["document_id"] == row["document_id"] and 1 <= doc["page_count"] <= 2, "DOCUMENT_ID_OR_PAGES")
        need(len(doc["pages"]) == doc["page_count"], "PAGE_COUNT_CHANGED")
        need(sha(doc["source"]["path"]) == doc["source"]["sha256"] == row["source_sha256"], "SOURCE_CHANGED")
        for index, page in enumerate(doc["pages"], 1):
            need(page["page"] == index and sha(page["path"]) == page["sha256"], "IMAGE_CHANGED")
        docs[doc["document_id"]] = doc
    need(sum(doc["page_count"] for doc in docs.values()) == 11, "EXACT_ELEVEN_UNIQUE_PAGES")
    return docs


def configuration(freeze):
    config, prompts = pinned(freeze["baseline_configuration"]), pinned(freeze["prompts"])
    need(prompts["BASE_CONTRACT"] == config["prompts"][ROUTE], "BASE_PROMPT_CHANGED")
    need(prompts["LITERAL_CONTRACT"] == prompts["BASE_CONTRACT"] + " " + prompts["sole_append_instruction"],
         "ONLY_FROZEN_SUFFIX_PERMITTED")
    need(config["generation_options"]["num_ctx"] == config["context_tokens"] == 32768
         and config["generation_options"]["num_predict"] == config["output_tokens"] == 8192, "FIXED_CONTEXT_OUTPUT")
    return config, prompts


def packet_for(doc, arm, config, prompts, runtime, work=240):
    actual = copy.deepcopy(config)
    actual["prompts"][ROUTE] = prompts[arm]
    return {"request_id": uuid.uuid4().hex, "document_id": doc["document_id"], "route": ROUTE,
            "configuration": actual, "schema": doc["schema"],
            "pages": [{"page": page["page"], "image_sha256": page["sha256"],
                       "image_b64": base64.b64encode(Path(page["path"]).read_bytes()).decode()} for page in doc["pages"]],
            "a_pages": [], "startup_files_sha256": runtime["startup_files_sha256"], "work_seconds": work}


def preflight(freeze):
    """Run unchanged worker against fake transport, capturing actual model bodies."""
    old = load_module(freeze["reused_runner"], "contract_previous_aggregator")
    worker = load_module(freeze["reused_worker"], "contract_previous_worker")
    docs, (config, prompts) = documents(freeze), configuration(freeze)
    order = freeze["execution_order"]
    need([x["document_id"] for x in order] == list(docs), "FROZEN_ORDER_MISMATCH")
    for index, item in enumerate(order):
        need(item["arms"] == list(ARMS if index % 2 == 0 else reversed(ARMS)), "ALTERNATING_ORDER")

    class Budget:
        def remaining(self, **_kwargs):
            return 240

    class Capture:
        LOCK = {"qwen": {"model": freeze["runtime"]["model"]}}
        def __init__(self, replies=None):
            self.BUDGETS, self.bodies, self.replies = {}, [], replies
        def qwen_verified(self):
            return True
        def memory_barrier(self, *_args):
            return {}
        def ollama_metrics(self, _reply):
            return {"prompt_eval_count": 100, "eval_count": 100}
        def stream_chat(self, _identity, body, timeout, raw_capture):
            self.bodies.append(copy.deepcopy(body))
            if self.replies is not None:
                content, reason = self.replies[len(self.bodies) - 1]
            else:
                schema = body["format"]
                content = json.dumps({"fields": {key: None for key in schema["properties"]["fields"]["required"]},
                    "tables": {key: [] for key in schema["properties"]["tables"]["required"]}, "uncertainty": []})
                reason = "stop"
            reply = {"message": {"content": content}, "done": True, "done_reason": reason}
            raw_capture(canonical(reply) + b"\n")
            return reply

    worker.emit = lambda *_args, **_kwargs: None
    body_proofs = []
    for identity, doc in docs.items():
        captures = {}
        for arm in ARMS:
            capture = Capture()
            result = worker.run_qwen(packet_for(doc, arm, config, prompts, freeze["runtime"]), capture, Budget())
            need(result["status"] == "COMPLETE" and len(capture.bodies) == doc["page_count"], "CAPTURE_ALL_PAGES")
            captures[arm] = capture.bodies
        for base, literal in zip(captures[ARMS[0]], captures[ARMS[1]]):
            expected = copy.deepcopy(base)
            expected["messages"][0]["content"] = prompts[ARMS[1]]
            need(expected == literal and base["messages"][0]["content"] == config["prompts"][ROUTE],
                 "MODEL_BODY_DELTA_OUTSIDE_SOLE_SYSTEM_SUFFIX")
            need("paddle_page_json_utf8" not in base["messages"][1]["content"], "NO_PADDLE_CONTEXT")
            body_proofs.append({"document_id": identity, "page": json.loads(base["messages"][1]["content"])["page"],
                "base_request_sha256": hashlib.sha256(canonical(base)).hexdigest(),
                "literal_request_sha256": hashlib.sha256(canonical(literal)).hexdigest(),
                "only_delta": "/messages/0/content"})
        historical = freeze.get("historical_requests", {}).get(identity)
        if historical is not None:
            need(sha(historical["path"]) == historical["sha256"], "HISTORICAL_REQUEST_STREAM_CHANGED")
            frames = [strict_json(line) for line in Path(historical["path"]).read_bytes().splitlines()]
            actual_calls = [frame for frame in frames if frame.get("kind") == "CALL_ADMITTED"]
            need(len(actual_calls) == doc["page_count"], "HISTORICAL_PAGE_CALL_COUNT")
            for body, observed in zip(captures[ARMS[0]], actual_calls):
                metadata = {**body, "messages": [{key: value for key, value in message.items() if key != "images"}
                                                 for message in body["messages"]]}
                need(metadata == observed["request_without_images"]
                     and hashlib.sha256(canonical(body)).hexdigest() == observed["request_sha256"],
                     "BASE_BODY_DIFFERS_FROM_ACTUAL_HISTORICAL_REQUEST")
    need(len(freeze.get("historical_requests", {})) == 5, "FIVE_ORIGINAL_ACTUAL_REQUEST_PROOFS")

    schema = {"fields": [{"name": "amount", "type": "decimal"}],
              "tables": [{"name": "rows", "columns": [{"name": "quantity", "type": "decimal"}]}]}
    full = {"fields": {"amount": "1.234,50"}, "tables": {"rows": [{"quantity": "2,00"}]}, "uncertainty": []}
    null = {"fields": {"amount": None}, "tables": {"rows": []}, "uncertainty": []}
    ambiguity = {"fields": {"amount": None}, "tables": {"rows": []},
                 "uncertainty": [{"path": "/fields/amount", "reason": "Two printed same-role amounts",
                                  "alternatives": ["100,00", "200,00"]}]}
    partial = {"fields": {"amount": None}, "tables": {"rows": [{"quantity": "2,00"}]}, "uncertainty": []}
    fake_packet = {"route": ROUTE, "request_id": "CPU_NO_MODELS", "schema": schema,
        "configuration": config, "pages": [{"page": page, "image_b64": base64.b64encode(b"fake").decode(),
        "image_sha256": hashlib.sha256(b"fake").hexdigest()} for page in (1, 2)]}
    cases = {}
    for name, content, reason, expected in (
        ("complete", full, "stop", "COMPLETE"), ("legitimate_null", null, "stop", "COMPLETE"),
        ("real_ambiguity", ambiguity, "stop", "COMPLETE"), ("numeric_literal_string", full, "stop", "COMPLETE"),
        ("partial_data", partial, "stop", "COMPLETE"), ("truncated_json", '{"fields":', "length", "ERROR")):
        raw = content if isinstance(content, str) else json.dumps(content)
        capture = Capture([(raw, reason), (json.dumps(null), "stop")])
        actual = worker.run_qwen(fake_packet, capture, Budget())
        need(actual["status"] == expected, "CPU_FIXTURE_" + name)
        need(bool(actual["pages"][0]["raw_stream_b64"]), "RAW_NOT_PRESERVED")
        if name == "truncated_json":
            need(len(capture.bodies) == 1 and not infrastructure_failure(actual), "TRUNCATION_NO_RETRY_NOT_INFRA")
        elif name == "real_ambiguity":
            aggregate = old.aggregate(actual["pages"], schema)
            need(aggregate["fields"]["amount"] is None and aggregate["uncertainty"], "AMBIGUITY_NOT_PRESERVED")
            need(actual["pages"][0]["semantic"]["uncertainty"][0]["alternatives"] == ["100,00", "200,00"],
                 "RAW_ALTERNATIVES_CHANGED")
        elif name == "numeric_literal_string":
            need(actual["pages"][0]["semantic"]["fields"]["amount"] == "1.234,50", "NUMERIC_LITERAL_CHANGED")
        cases[name] = actual["status"]
    need(len(cases) == 6 and cases["truncated_json"] == "ERROR", "FAILED_CASE_REMAINS_IN_DENOMINATOR")
    first = {"status": "ERROR", "failure_code": "HTTP_422_OPAQUE_TECHNICAL", "resources_released": True}
    second = {"status": "ERROR", "failure_code": "ConnectionResetError", "resources_released": True}
    consecutive, stopped = step_guard(0, first)
    need(consecutive == 1 and not stopped, "FIRST_INFRA_GUARD")
    consecutive, stopped = step_guard(consecutive, second)
    need(consecutive == 2 and stopped, "SECOND_INFRA_STOP_ALL")
    need(step_guard(0, {"status": "COMPLETE", "resources_released": False})[1], "UNCERTAIN_CLOSE_STOP")
    import tempfile
    with tempfile.TemporaryDirectory(prefix="contract-clock-cpu-") as temporary:
        path = Path(temporary) / "inference-clock.json"
        first = InferenceClock(path)
        need(first.value is None and not path.exists(), "NO_INFERENCE_CLOCK_DURING_PREPARATION")
        original_pin = first.start()
        second = InferenceClock(path)
        need(ref(path) == original_pin and second.value == first.value, "PERSISTED_CLOCK_NOT_REUSED")
        try:
            second.start()
        except RuntimeError:
            pass
        else:
            raise RuntimeError("INFERENCE_CLOCK_RESET_ALLOWED")
    return {"status": "CPU_PREFLIGHT_PASS_NO_MODELS", "fixtures": cases, "body_equality_proofs": body_proofs,
            "two_consecutive_infra_stop": True, "uncertain_close_stop": True, "model_calls": 0,
            "first_inference_clock_persisted_and_reset_denied": True, "hard_page_call_cap": 22,
            "reference_contents_read": 0, "same_unchanged_aggregator_both_arms": freeze["reused_runner"],
            "same_unchanged_worker_both_arms": freeze["reused_worker"]}


def run(freeze, freeze_pin):
    need(freeze.get("authorized") is True and freeze.get("model_admission") == "ROOT_FROZEN", "ROOT_RELEASE_REQUIRED")
    for pin in freeze["files"]:
        need(sha(pin["path"]) == pin["sha256"], "FROZEN_FILE_CHANGED")
    docs, (config, prompts) = documents(freeze), configuration(freeze)
    aggregator = load_module(freeze["reused_runner"], "contract_pinned_aggregator")
    worker_code = Path(freeze["reused_worker"]["path"]).read_bytes()
    need(hashlib.sha256(worker_code).hexdigest() == freeze["reused_worker"]["sha256"], "WORKER_CHANGED")
    need(0 <= time.monotonic() - freeze["preparation_started_monotonic"] < 3600
         and abs((time.time() - freeze["preparation_started_epoch"])
                 - (time.monotonic() - freeze["preparation_started_monotonic"])) < 30,
         "PREPARATION_CLOCK_CHANGED_OR_EXHAUSTED")
    order = freeze["execution_order"]
    need(len(order) == 6 and {row["document_id"] for row in order} == set(docs), "EXACT_ORDER")
    output = Path(freeze["output_dir"])
    output.mkdir()
    clock = InferenceClock(output / "inference-clock.private.json")
    ledger = (output / "ledger.ndjson").open("xb")
    def record(kind, **value):
        ledger.write(canonical({"kind": kind, "at_epoch": time.time(), "at_monotonic": time.monotonic(), **value}) + b"\n")
        ledger.flush()
        os.fsync(ledger.fileno())
    counts, reserved, consecutive, stop_reason, rows = {arm: 0 for arm in ARMS}, 0, 0, None, []
    record("RUN_STARTED", freeze=freeze_pin, inference_clock_not_started=True,
           preparation_started_epoch=freeze["preparation_started_epoch"],
           preparation_started_monotonic=freeze["preparation_started_monotonic"],
           correction_policy="BB29969F_AUXILIARY_REPAIRS_WITHIN_PREPARATION_BUDGET")
    boot = ("import base64,json,sys;v=json.load(sys.stdin);g={'__name__':'diagnostic_import'};"
            "exec(compile(base64.b64decode(v['code']),'contract_diagnostic_worker','exec'),g);g['main'](v['packet'])")
    try:
        for index, item in enumerate(order):
            need(item["arms"] == list(ARMS if index % 2 == 0 else reversed(ARMS)), "ALTERNATING_ORDER")
            doc = docs[item["document_id"]]
            for arm in item["arms"]:
                folder = output / (doc["document_id"] + "." + arm)
                folder.mkdir()
                result = {"document_id": doc["document_id"], "arm": arm, "route": ROUTE,
                    "source_sha256": doc["source"]["sha256"], "schema_id": doc["schema_id"],
                    "type": doc["type"], "modality": doc["modality"], "status": "NOT_RUN", "executed": False,
                    "semantic": None, "pages": [], "metrics": {"call_count": 0}, "diagnostic_only": True,
                    "recognized_text": None, "recognition_text_scope": "SEMANTIC_EXTRACTION_NOT_FULL_TEXT",
                    "operational_approval_measured": False}
                remaining = 3600 - clock.elapsed()
                if stop_reason:
                    result["reason"] = stop_reason
                elif remaining <= 30 or sum(counts.values()) >= 12 or reserved + doc["page_count"] > 22:
                    result["reason"] = "FROZEN_CAMPAIGN_OR_ATTEMPT_PAGE_BUDGET"
                else:
                    controller_started = time.monotonic()
                    if clock.value is None:
                        need(time.monotonic() - freeze["preparation_started_monotonic"] < 3600,
                             "PREPARATION_BUDGET_EXHAUSTED_BEFORE_FIRST_ADMISSION")
                        record("INFERENCE_CLOCK_STARTED", clock=clock.start())
                    counts[arm] += 1
                    reserved += doc["page_count"]
                    packet = packet_for(doc, arm, config, prompts, freeze["runtime"], min(240, remaining - 30))
                    record("ATTEMPT_ADMITTED_NO_REFUND", document_id=doc["document_id"], arm=arm,
                           request_id=packet["request_id"], counts=dict(counts), reserved_page_slots=reserved,
                           work_seconds=packet["work_seconds"])
                    print(json.dumps({"phase": "RUNNING", "document_id": doc["document_id"], "arm": arm}), flush=True)
                    save(folder / "request-metadata.json", {**{k: v for k, v in packet.items() if k not in {"pages"}},
                        "pages": [{k: v for k, v in page.items() if k != "image_b64"} for page in packet["pages"]]})
                    try:
                        verify_active(freeze["runtime"])
                        outer = canonical({"code": base64.b64encode(worker_code).decode(), "packet": packet})
                        with (folder / "worker.stdout.ndjson").open("xb") as stdout, (folder / "worker.stderr.txt").open("xb") as stderr:
                            process = subprocess.Popen(["docker", "--context", "default", "exec", "-i", freeze["runtime"]["gateway_id"],
                                freeze["runtime"].get("worker_python", "/opt/venv/bin/python"), "-I", "-B", "-c", boot],
                                stdin=subprocess.PIPE, stdout=stdout, stderr=stderr,
                                env={k: v for k, v in os.environ.items() if k not in DOCKER_OVERRIDES},
                                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
                            try:
                                process.communicate(outer, timeout=packet["work_seconds"] + 30)
                            except subprocess.TimeoutExpired:
                                process.kill()
                                process.wait(timeout=5)
                                raise TimeoutError("REMOTE_WORKER_CLOSURE_UNVERIFIED")
                            stdout.flush()
                            os.fsync(stdout.fileno())
                            stderr.flush()
                            os.fsync(stderr.fileno())
                        frames = [strict_json(line) for line in (folder / "worker.stdout.ndjson").read_bytes().splitlines()]
                        answers = [frame["result"] for frame in frames if frame.get("kind") == "RESULT"]
                        need(process.returncode == 0 and len(answers) == 1, "DOCKER_EXEC_FAILED_NO_CLOSURE_PROOF")
                        answer = answers[0]
                        result.update(answer, executed=True)
                        result["semantic"] = (aggregator.aggregate(answer["pages"], doc["schema"])
                                              if answer.get("pages") else None)
                        result["metrics"] = {key: answer.get(key) for key in ("work_seconds", "cleanup_seconds", "total_seconds")}
                        result["metrics"].update(controller_elapsed_seconds=round(time.monotonic() - controller_started, 6),
                            call_count=sum(frame.get("kind") == "CALL_ADMITTED" for frame in frames),
                            tokens_by_page=[page.get("tokens") for page in answer.get("pages", [])])
                        result["worker_stream"] = ref(folder / "worker.stdout.ndjson")
                    except Exception as error:
                        result.update(status="TIMEOUT" if isinstance(error, (TimeoutError, subprocess.TimeoutExpired)) else "ERROR",
                            executed=True, reason=type(error).__name__ + ":" + str(error)[:200], resources_released=False)
                        result["metrics"].update(controller_elapsed_seconds=round(time.monotonic() - controller_started, 6),
                                                 call_count=None)
                        # Preserve completed page observations even if the outer
                        # transport/closure failed. These are never a new call.
                        raw_path = folder / "worker.stdout.ndjson"
                        if raw_path.exists():
                            recovered, invalid_lines = [], []
                            for line_number, line in enumerate(raw_path.read_bytes().splitlines(), 1):
                                try:
                                    recovered.append(strict_json(line))
                                except (ValueError, RuntimeError):
                                    invalid_lines.append(line_number)
                            result["pages"] = [frame["page"] for frame in recovered if frame.get("kind") == "PAGE"]
                            result["semantic"] = (aggregator.aggregate(result["pages"], doc["schema"])
                                                  if result["pages"] else None)
                            result["metrics"]["call_count"] = sum(frame.get("kind") == "CALL_ADMITTED" for frame in recovered)
                            result["metrics"]["tokens_by_page"] = [page.get("tokens") for page in result["pages"]]
                            result["worker_stream"] = ref(raw_path)
                            result["outer_transport_invalid_line_numbers"] = invalid_lines
                    consecutive, stopped = step_guard(consecutive, result)
                    if stopped:
                        stop_reason = ("GPU_CLOSURE_UNVERIFIED_STOP_ALL" if result.get("resources_released") is not True
                                       else "TWO_CONSECUTIVE_INFRASTRUCTURE_FAILURES_STOP_ALL")
                    record("ATTEMPT_FINISHED", document_id=doc["document_id"], arm=arm, status=result["status"],
                        resources_released=result.get("resources_released"), actual_call_count=result["metrics"]["call_count"],
                        infrastructure_failure=infrastructure_failure(result), consecutive_infra_failures=consecutive,
                        stop_reason=stop_reason)
                pin = save(folder / "result.json", result)
                rows.append({"document_id": doc["document_id"], "arm": arm, "status": result["status"],
                             "executed": result["executed"], "result": pin})
                print(json.dumps({"phase": "DONE", "document_id": doc["document_id"], "arm": arm,
                    "status": result["status"], "campaign_elapsed_seconds": round(clock.elapsed(), 3)}), flush=True)
        summary = {"rows": rows, "document_attempts": counts, "Qwen_page_slots_reserved": reserved,
                   "global_GPU_stop_reason": stop_reason, "elapsed_campaign_seconds": clock.elapsed(),
                   "inference_clock": ref(clock.path) if clock.value is not None else None,
                   "Paddle_calls": 0, "retries": 0, "operational_writes": 0}
        save(output / "summary.json", summary)
        record("RUN_CLOSED", counts=counts, Qwen_page_slots_reserved=reserved, global_GPU_stop_reason=stop_reason)
    finally:
        ledger.close()


def main():
    parser = argparse.ArgumentParser()
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--preflight", action="store_true")
    mode.add_argument("--run", action="store_true")
    parser.add_argument("--freeze", required=True, type=Path)
    parser.add_argument("--freeze-sha256", required=True)
    parser.add_argument("--out", type=Path)
    args = parser.parse_args()
    need(sha(args.freeze) == args.freeze_sha256, "EXACT_FREEZE_HASH")
    freeze = read(args.freeze)
    if args.preflight:
        result = preflight(freeze)
        print(json.dumps(save(args.out, result) if args.out else result))
    else:
        run(freeze, ref(args.freeze))


if __name__ == "__main__":
    main()
