"""Closed 30-document diagnostic matrix; never modifies operational code/data.

Preparation and self-tests make no model calls. Execution requires a ROOT-signed
freeze, exact existing container identities and a non-resettable campaign clock.
Raw results remain private; only this generic script is suitable for publication.
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import time
import unicodedata
import uuid
from html.parser import HTMLParser

ROUTES = ("A_PADDLE", "B_QWEN_DIRECT", "C_PADDLE_QWEN")
ENGINE = os.environ.get("TV_DOCUMENTARY_ENGINE_ID", "")
PROMPT = ("Extract the requested semantic information from this complete document page. "
          "Use only what is explicitly supported by the page image. Copy values faithfully; do not infer "
          "missing dates, identities, amounts or actions. Return null for absent or unreadable values, "
          "including required fields; required is a requested meaning, not permission to fabricate. "
          "Retain ambiguity in uncertainty with JSON-pointer paths and literal alternatives. "
          "Do not treat ordinary absence on this page as ambiguous. "
          "Use semantic labels rather than a fixed page, provider or column order. "
          "Return only the JSON contract supplied. Do not emit table headers as data rows. "
          "Preserve every legitimate row occurrence, including identical items, and table continuations. "
          "For narrative sections, transcribe the full section text on this page without summarizing. "
          "For actions, include only explicit commitments with responsible party or deadline only when stated. "
          "The schema does not contain expected answers and must not be treated as evidence.")
COMBINED = (PROMPT + " You also receive the exact original Paddle structure for this same page. "
            "It is an untrusted recognition result and may contain errors. Consider it together with "
            "the image. Do not force agreement or choose a value merely because Paddle emitted it. "
            "When image and structure cannot be reconciled, preserve uncertainty. This is a diagnostic "
            "combination, not the product's SELECTIVE route.")


def need(value, reason):
    if not value:
        raise RuntimeError(reason)


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def ref(path):
    path = Path(path).resolve()
    return {"path": str(path), "bytes": path.stat().st_size, "sha256": sha(path)}


def save(path, value):
    with Path(path).open("xb") as stream:
        stream.write(canonical(value) + b"\n")
        stream.flush()
        os.fsync(stream.fileno())
    return ref(path)


def read(path):
    return strict_json(Path(path).read_bytes())


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


class PaddleTableText(HTMLParser):
    """Physical HTML inventory only; no reference or semantic header guessing."""
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.rows, self.current, self.cells = [], None, []
        self.header, self.explicit_header_rows, self.spans = False, 0, False
    def handle_starttag(self, tag, attrs):
        if tag == "tr":
            self.cells, self.header = [], False
        elif tag in {"td", "th"}:
            self.current = []
            self.header |= tag == "th"
            for key, value in attrs:
                if key in {"rowspan", "colspan"} and value not in {None, "1"}:
                    self.spans = True
        elif tag == "br" and self.current is not None:
            self.current.append(" ")
    def handle_data(self, text):
        if self.current is not None:
            self.current.append(text)
    def handle_endtag(self, tag):
        if tag in {"td", "th"} and self.current is not None:
            self.cells.append("".join(self.current).strip());self.current = None
        elif tag == "tr" and self.cells:
            self.rows.append(self.cells)
            self.explicit_header_rows += int(self.header)
            self.cells = []


def paddle_text_inventory(pages, expected_pages):
    text_pages, tables, errors, block_order = [], [], [], []
    usable = True
    for page in sorted(pages, key=lambda value: value["page"]):
        try:
            raw = read(page["path"])
            raw = raw.get("res", raw)
            blocks = raw.get("parsing_res_list")
            need(isinstance(blocks, list), "PADDLE_BLOCKS_UNAVAILABLE")
            texts = []
            for index, block in enumerate(blocks):
                if not isinstance(block, dict):
                    continue
                content = block.get("block_content")
                if not isinstance(content, str):
                    continue
                block_order.append({"page": page["page"], "provider_list_index": index,
                                    "provider_block_order": block.get("block_order"), "label": block.get("block_label")})
                if block.get("block_label") == "table" and "<table" in content.lower():
                    parser = PaddleTableText();parser.feed(content)
                    tables.append({"page": page["page"], "block_index": index, "rows_physical": len(parser.rows),
                        "columns_max_physical": max((len(row) for row in parser.rows), default=0),
                        "explicit_th_header_rows": parser.explicit_header_rows,
                        "semantic_data_rows": len(parser.rows)-parser.explicit_header_rows if parser.explicit_header_rows else None,
                        "rowspan_colspan_present": parser.spans, "grid_logical_dimensions_measured": not parser.spans,
                        "grid": parser.rows, "header_not_guessed_from_first_row": True})
                    texts.extend(" ".join(row) for row in parser.rows)
                else:
                    texts.append(content)
            page_text = "\n".join(texts)
            usable &= bool(page_text.strip())
            text_pages.append({"page": page["page"], "text": page_text})
        except (ValueError, TypeError, KeyError, RuntimeError):
            usable = False
            errors.append({"page": page["page"], "reason": "RAW_STRUCTURE_NOT_PARSEABLE_BY_FROZEN_ADAPTER"})
    complete = not errors and sorted(p["page"] for p in pages) == list(range(1, expected_pages+1))
    scope = "FULL_DOCUMENT" if complete else "DECLARED_PARTIAL" if text_pages else "UNAVAILABLE"
    return {"recognized_text": "\n".join(p["text"] for p in text_pages) if text_pages else None,
            "recognized_text_scope": scope, "recognition_text_scope": scope,
            "recognition_text_pages": text_pages, "physical_table_inventory": tables,
            "observed_tables": len(tables), "observed_rows_including_headers": sum(t["rows_physical"] for t in tables),
            "observed_logical_rows": None if any(t["semantic_data_rows"] is None for t in tables) else sum(t["semantic_data_rows"] for t in tables),
            "observed_cols_max": max((t["columns_max_physical"] for t in tables), default=0),
            "observed_reading_order": block_order, "raw_structure_errors": errors,
            "raw_all_pages_parse_valid": complete, "raw_all_pages_have_usable_text_or_table": usable and complete,
            "text_derived_only_from_original_Paddle_blocks_not_semantic_or_reference": True}


def command(*args, timeout=30):
    env = {k: v for k, v in os.environ.items() if k not in {"DOCKER_HOST", "DOCKER_CONTEXT", "DOCKER_TLS_VERIFY", "DOCKER_CERT_PATH"}}
    completed = subprocess.run(["docker", "--context", "default", *map(str, args)],
                               capture_output=True, timeout=timeout, env=env,
                               creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    need(completed.returncode == 0, "DOCKER_READ_COMMAND_FAILED")
    return completed.stdout


def generic_definition(schema):
    types = {"string": "STRING", "decimal": "DECIMAL", "date": "DATE"}
    def field(item):
        return {"name": item["name"], "label": item["description"],
                "logical_type": types[item["type"]], "required": item["required"]}
    return {"fields": [field(f) for f in schema["fields"]],
            "tables": [{"name": t["name"], "fields": [field(c) for c in t["columns"]]} for t in schema["tables"]]}


def prepare(args):
    import pypdfium2 as pdfium
    sample, schemas = read(args.sample), read(args.schemas)
    docs = sample["documents"]
    need(len(docs) == 30 and sum(x["pages"] for x in docs) <= 90, "EXACT_SAMPLE30_MAX90")
    destination = args.out.resolve()
    destination.mkdir()
    known = read(args.definitions)["documents"] if args.definitions else {}
    pins, prepared = [], []
    for doc in docs:
        identity = doc["document_id"]
        need(re.fullmatch(r"[A-Za-z0-9_-]{1,80}", identity), "DOCUMENT_ID")
        source = Path(doc["source_file"])
        need(sha(source) == doc["source_sha256"], "SOURCE_HASH_CHANGED")
        schema = schemas["schemas"][doc["type"]]
        definition = read(known[identity]["path"]) if identity in known else generic_definition(schema)
        if identity in known:
            need(sha(known[identity]["path"]) == known[identity]["sha256"], "EXISTING_DEFINITION_CHANGED")
        folder = destination / identity
        folder.mkdir()
        pdf = pdfium.PdfDocument(str(source))
        need(len(pdf) == doc["pages"] and 1 <= len(pdf) <= 3, "EXACT_COMPLETE_PAGE_COUNT")
        images = []
        for index in range(len(pdf)):
            page = pdf[index]
            width, height = page.get_size()
            scale = min(150/72, 2000/max(width, height))
            bitmap = page.render(scale=scale)
            image = bitmap.to_pil().convert("RGB")
            bitmap.close()
            page.close()
            name = folder / f"page-{index+1:02}.png"
            image.save(name, format="PNG")
            record = {**ref(name), "page": index+1, "width": image.width, "height": image.height,
                      "dpi_requested": 150, "render_scale": scale, "RGB": True}
            image.close()
            images.append(record)
            pins.append(ref(name))
        pdf.close()
        # References and private generation contents are deliberately omitted.
        record = {"document_id": identity, "type": doc["type"], "schema_id": schema["schema_id"],
                  "modality": doc["modality"], "source": ref(source), "page_count": doc["pages"],
                  "schema": schema, "a_definition": definition,
                  "a_existing_definition_used": identity in known, "pages": images}
        path = folder / "input.json"
        pins.append(save(path, record))
        prepared.append({"document_id": identity, "input": ref(path), "pages": doc["pages"]})
    config = {"version": "DOCUMENTARY_DIAGNOSTIC_FROZEN_V1", "context_tokens": 32768, "output_tokens": 8192,
              "generation_options": {"num_ctx": 32768, "num_predict": 8192, "temperature": 0,
                  "seed": 98, "presence_penalty": 0, "repeat_penalty": 1, "top_k": 20, "top_p": .95},
              "prompts": {"B_QWEN_DIRECT": PROMPT, "C_PADDLE_QWEN": COMBINED},
              "a_configuration": {"paddle_model": "PaddleOCR-VL-1.6", "llm_model": "qwen3.5:9b-q4_K_M",
                  "extraction_strategy": "PADDLE_STRUCTURED", "max_parallel_jobs": 1, "timeout_seconds": 185, "use_llm": False},
              "a_gateway_work_seconds": 180, "a_gateway_cleanup_reserve_seconds": 5,
              "B_C_work_seconds": 240, "diagnostic_cleanup_seconds_max": 30,
              "render": {"reader": "pypdfium2", "dpi": 150, "max_edge_target": 2000, "mode": "RGB_PNG",
                  "same_actual_pdf_image_for_B_and_C": True,
                  "Paddle_render_is_existing_pipeline_not_modified": True},
              "aggregation": "Equal NFC/collapsed-whitespace nonnull header values merge; uncertainty or conflicts become null with source alternatives. Tables concatenate by page with no deduplication. RAW pages remain unchanged.",
              "retries": 0, "max_Qwen_calls_per_page_per_route": 1}
    config_ref = save(destination / "configuration.json", config)
    receipt = save(destination / "preparation.json", {"status": "PREPARED_NO_MODELS", "documents": prepared,
                  "configuration": config_ref, "files": pins, "sample": ref(args.sample), "schemas": ref(args.schemas),
                  "reference_files_read": 0, "rendered_pages": sum(x["pages"] for x in prepared)})
    print(json.dumps(receipt))


def normalized_identity(value):
    return " ".join(unicodedata.normalize("NFC", value).split())


def aggregate(pages, schema):
    fields, uncertainty, rows = {}, [], {t["name"]: [] for t in schema["tables"]}
    for field in schema["fields"]:
        name = field["name"]
        found, uncertain = [], False
        for page in pages:
            semantic = page.get("semantic")
            if not isinstance(semantic, dict):
                continue
            value = semantic.get("fields", {}).get(name)
            uncertain |= any(x.get("path") == "/fields/" + name for x in semantic.get("uncertainty", []))
            if value is not None:
                found.append(value)
        different = {normalized_identity(x) for x in found}
        fields[name] = found[0] if found and len(different) == 1 and not uncertain else None
        if uncertain or len(different) > 1:
            uncertainty.append({"path": "/fields/"+name, "reason": "PERSISTED_PAGE_UNCERTAINTY" if uncertain else "CONFLICTING_PAGE_OBSERVATIONS",
                                "alternatives": list(dict.fromkeys(found))})
    for page in pages:
        semantic = page.get("semantic")
        if not isinstance(semantic, dict):
            continue
        for table in schema["tables"]:
            name = table["name"]
            offset = len(rows[name])
            copied = json.loads(json.dumps(semantic.get("tables", {}).get(name, [])))
            for item in semantic.get("uncertainty", []):
                prefix = "/tables/"+name+"/"
                if item.get("path", "").startswith(prefix):
                    parts = item["path"][len(prefix):].split("/")
                    if len(parts) == 2 and parts[0].isdigit() and int(parts[0]) < len(copied) and parts[1] in copied[int(parts[0])]:
                        copied[int(parts[0])][parts[1]] = None
                        uncertainty.append({**item, "path": prefix+str(offset+int(parts[0]))+"/"+parts[1], "source_page": page["page"]})
            rows[name].extend(copied)
    return {"fields": fields, "tables": rows, "uncertainty": uncertainty}


def infrastructure_failure(code):
    # Content/precision/schema/truncation failures never count as infrastructure.
    text = code or ""
    return any(x in text for x in ("GPU_RUNTIME_UNAVAILABLE", "MODEL_INTEGRITY", "GPU_MEMORY_UNAVAILABLE",
           "GPU_QUEUE", "ConnectionRefusedError", "ConnectionResetError", "DOCKER_EXEC_FAILED"))


def verify_runtime(runtime):
    need(bool(ENGINE), "EXPLICIT_TV_DOCUMENTARY_ENGINE_ID_REQUIRED")
    need(command("info", "--format", "{{.ID}}").decode().strip() == runtime["engine_id"] == ENGINE, "ENGINE_CHANGED")
    rows = json.loads(command("inspect", runtime["gateway_id"], runtime["ollama_id"]))
    for row, role in zip(rows, ("gateway", "ollama")):
        need(row["Id"] == runtime[role+"_id"] and row["Image"] == runtime[role+"_image"] and row["State"]["Running"], "RUNTIME_ID_IMAGE_STATE_CHANGED")
        need(not row["HostConfig"].get("PortBindings"), "GPU_MODEL_CONTAINER_HAS_PUBLISHED_PORTS")
    active = command("ps", "--format", "{{.Names}}").decode().splitlines()
    need(set(active) == {rows[0]["Name"].lstrip("/"), rows[1]["Name"].lstrip("/")}, "ONLY_GATEWAY_OLLAMA_MAY_RUN")


def run(args):
    need(sha(args.freeze) == args.freeze_sha256, "EXACT_ROOT_FREEZE_HASH")
    freeze = read(args.freeze)
    need(freeze.get("authorized") is True and freeze.get("model_admission") == "ROOT_FROZEN", "ROOT_ADMISSION_REQUIRED")
    for pin in freeze["files"]:
        need(sha(pin["path"]) == pin["sha256"], "FROZEN_FILE_CHANGED")
    config = read(freeze["configuration"]["path"])
    need(sha(freeze["configuration"]["path"]) == freeze["configuration"]["sha256"], "CONFIGURATION_CHANGED")
    inputs = Path(freeze["input_dir"])
    order = freeze["execution_order"]
    need(len(order) == len(set(order)) == 30, "EXACT_FROZEN_ORDER30")
    output = Path(freeze["output_dir"])
    output.mkdir()
    ledger = (output/"ledger.ndjson").open("xb")
    def record(kind, **value):
        raw = canonical({"kind": kind, "at_epoch": time.time(), "at_monotonic": time.monotonic(), **value})+b"\n"
        ledger.write(raw);ledger.flush();os.fsync(ledger.fileno())
    started = freeze["campaign_started_monotonic"]
    need(0 <= time.monotonic()-started <= 10800 and abs((time.time()-freeze["campaign_started_epoch"])-(time.monotonic()-started)) < 30,
         "CAMPAIGN_CLOCK_CHANGED_OR_EXHAUSTED")
    runtime = freeze["runtime"]
    worker_path = Path(__file__).with_name("gateway_diagnostic_worker.py")
    worker_code = worker_path.read_bytes()
    need(any(p["sha256"] == hashlib.sha256(worker_code).hexdigest() and Path(p["path"]).resolve() == worker_path.resolve() for p in freeze["files"]), "WORKER_NOT_FROZEN")
    counts, failures = {x: 0 for x in ROUTES}, {x: 0 for x in ROUTES}
    qwen_reserved, stop_gpu = 0, False
    summary = []
    record("RUN_STARTED", freeze=ref(args.freeze), clock_started_monotonic=started,
           clock_started_epoch=freeze["campaign_started_epoch"], correction_cycles=0)
    try:
        for identity in order:
            doc = read(inputs/identity/"input.json")
            need(doc["document_id"] == identity and sha(doc["source"]["path"]) == doc["source"]["sha256"], "SOURCE_CHANGED_BEFORE_ATTEMPT")
            a_pages, a_complete, a_cost = [], False, None
            for route in ROUTES:
                folder = output/(identity+"."+route)
                folder.mkdir()
                result = {"document_id": identity, "source_sha256": doc["source"]["sha256"], "schema_id": doc["schema_id"],
                          "type": doc["type"], "modality": doc["modality"], "route": route, "executed": False, "semantic": None, "pages": [],
                          "diagnostic_only": True, "operational_approval_measured": False,
                          "recognition_text_scope": "PADDLE_LAYOUT_BLOCKS" if route == "A_PADDLE" else "SEMANTIC_EXTRACTION_NOT_FULL_TEXT",
                          "recognized_text": None, "metrics": {"call_count": 0}}
                remaining = 10800 - (time.monotonic()-started)
                if stop_gpu or failures[route] >= 3:
                    result.update(status="ERROR", reason="NOT_RUN_GPU_CLOSURE_UNVERIFIED" if stop_gpu else "NOT_RUN_ROUTE_SUSPENDED_AFTER_THREE_INFRA_FAILURES", failure_layer="runtime_resources")
                elif remaining <= 30:
                    result.update(status="NOT_RUN_BUDGET", reason="CAMPAIGN_180_MINUTE_ELAPSED_LIMIT")
                elif route == "C_PADDLE_QWEN" and not a_complete:
                    result.update(status="NOT_RUN_DEPENDENCY", reason="A_NOT_COMPLETE_USABLE_ALL_PAGES_RESOURCES_CLOSED")
                elif counts[route] >= 30 or route != "A_PADDLE" and qwen_reserved+doc["page_count"] > 180:
                    result.update(status="NOT_RUN_BUDGET", reason="FROZEN_DOCUMENT_OR_QWEN_PAGE_LIMIT")
                else:
                    verify_runtime(runtime)
                    counts[route] += 1
                    if route != "A_PADDLE":
                        qwen_reserved += doc["page_count"]
                    work = min(180 if route == "A_PADDLE" else 240, remaining-30)
                    request_id = uuid.uuid4().hex
                    config_a = config["a_configuration"]
                    a_total = min(185, work+5)
                    a_request = {"request_id": request_id, "attempt_id": "documentary-"+identity,
                                 "filename": "document.pdf", "source": base64.b64encode(Path(doc["source"]["path"]).read_bytes()).decode(),
                                 "source_sha256": doc["source"]["sha256"], "strategy": "PADDLE_STRUCTURED",
                                 "configuration": config_a, "configuration_sha256": hashlib.sha256(canonical(config_a)).hexdigest(),
                                 "definition": doc["a_definition"], "definition_sha256": hashlib.sha256(canonical(doc["a_definition"])).hexdigest(),
                                 "request_budget_seconds": a_total}
                    pages = []
                    for page in doc["pages"]:
                        need(sha(page["path"]) == page["sha256"], "RENDER_CHANGED")
                        pages.append({"page": page["page"], "image_sha256": page["sha256"],
                                      "image_b64": base64.b64encode(Path(page["path"]).read_bytes()).decode()})
                    packet = {"request_id": request_id, "document_id": identity, "route": route, "a_request": a_request,
                              "configuration": config, "schema": doc["schema"], "pages": pages, "a_pages": a_pages,
                              "startup_files_sha256": runtime["startup_files_sha256"], "work_seconds": work}
                    save(folder/"request-metadata.json", {"request_id": request_id, "document_id": identity, "route": route,
                         "source_sha256": doc["source"]["sha256"], "schema_sha256": hashlib.sha256(canonical(doc["schema"])).hexdigest(),
                         "a_definition": doc["a_definition"], "a_configuration": config_a,
                         "a_configuration_sha256": a_request["configuration_sha256"], "a_definition_sha256": a_request["definition_sha256"],
                         "pages": [{k: v for k, v in p.items() if k != "image_b64"} for p in pages],
                         "A_page_hashes": [{"page": p["page"], "sha256": p["sha256"]} for p in a_pages],
                         "work_limit_seconds": work, "reduced_only_by_campaign_absolute_deadline": work < (180 if route == "A_PADDLE" else 240)})
                    record("ROUTE_ADMITTED_NO_REFUND", document_id=identity, route=route, request_id=request_id,
                           route_counts=dict(counts), qwen_page_slots_reserved=qwen_reserved,
                           route_page_slots=doc["page_count"] if route != "A_PADDLE" else 0, work_seconds=work)
                    print(json.dumps({"phase": "RUNNING", "document_id": identity, "route": route, "campaign_elapsed": round(time.monotonic()-started, 1)}), flush=True)
                    boot = "import base64,json,sys;v=json.load(sys.stdin);s=base64.b64decode(v['code']);exec(compile(s,'documentary_diagnostic_worker','exec'),{'__name__':'diagnostic_import'});main(v['packet'])"
                    # bootstrap's globals must be shared with exec for imported helper functions.
                    boot = "import base64,json,sys;v=json.load(sys.stdin);g={'__name__':'diagnostic_import'};exec(compile(base64.b64decode(v['code']),'documentary_diagnostic_worker','exec'),g);g['main'](v['packet'])"
                    outer = canonical({"code": base64.b64encode(worker_code).decode(), "packet": packet})
                    env = {k: v for k, v in os.environ.items() if k not in {"DOCKER_HOST", "DOCKER_CONTEXT", "DOCKER_TLS_VERIFY", "DOCKER_CERT_PATH"}}
                    controller_started = time.monotonic()
                    try:
                        with (folder/"worker.stdout.ndjson").open("xb") as stdout, (folder/"worker.stderr.txt").open("xb") as stderr:
                            process = subprocess.Popen(["docker", "--context", "default", "exec", "-i", runtime["gateway_id"],
                                "/opt/venv/bin/python", "-I", "-B", "-c", boot], stdin=subprocess.PIPE, stdout=stdout, stderr=stderr,
                                env=env, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
                            try:
                                process.communicate(outer, timeout=work+30)
                            except subprocess.TimeoutExpired:
                                process.kill();process.wait(timeout=5)
                                raise TimeoutError("REMOTE_WORKER_CLOSURE_UNVERIFIED")
                            stdout.flush();os.fsync(stdout.fileno());stderr.flush();os.fsync(stderr.fileno())
                        frames = [strict_json(line) for line in (folder/"worker.stdout.ndjson").read_bytes().splitlines()]
                        final = [x["result"] for x in frames if x.get("kind") == "RESULT"]
                        need(process.returncode == 0 and len(final) == 1, "DOCKER_EXEC_FAILED_NO_CLOSURE_PROOF")
                        answer = final[0]
                        result.update(answer, executed=True)
                        result["metrics"] = {"work_seconds": answer["work_seconds"], "cleanup_seconds": answer["cleanup_seconds"],
                                             "total_seconds": answer["total_seconds"], "controller_elapsed_seconds": round(time.monotonic()-controller_started, 6),
                                             "call_count": 1 if route == "A_PADDLE" else len([x for x in frames if x.get("kind") == "CALL_ADMITTED"])}
                        if route == "A_PADDLE":
                            wire = b"".join(base64.b64decode(x["content_b64"], validate=True) for x in frames if x.get("kind") == "A_RAW")
                            (folder/"recognition.ndjson").write_bytes(wire)
                            events = []
                            for line in wire.splitlines():
                                try:
                                    value = strict_json(line)
                                    if isinstance(value, dict) and value.get("kind"):
                                        events.append(value)
                                except ValueError:
                                    pass
                            for ordinal, event in enumerate(events, 1):
                                need(event.get("sequence") == ordinal
                                     and event.get("event_sha256") == hashlib.sha256(canonical({k:v for k,v in event.items() if k != "event_sha256"})).hexdigest()
                                     and event.get("request_id") == request_id
                                     and event.get("source_sha256") == doc["source"]["sha256"]
                                     and event.get("configuration_sha256") == a_request["configuration_sha256"]
                                     and event.get("definition_sha256") == a_request["definition_sha256"], "GENUINE_A_EVENT_BINDING_FAILED")
                            pages_raw = []
                            for event in events:
                                if event.get("kind") != "PAGE":
                                    continue
                                for index, artifact in enumerate(event.get("payload", {}).get("artifacts", [])):
                                    raw = base64.b64decode(artifact["content_b64"], validate=True)
                                    need(hashlib.sha256(raw).hexdigest() == artifact["sha256"], "A_ARTIFACT_INTEGRITY")
                                    path = folder/(f"p{event['page_one_based']:02}-{index:02}-"+artifact["kind"]+".artifact")
                                    path.write_bytes(raw)
                                    if artifact["kind"] == "PADDLE_PAGE_JSON":
                                        pages_raw.append({"page": event["page_one_based"], "sha256": artifact["sha256"], "path": str(path),
                                                          "content_b64": artifact["content_b64"]})
                            result["pages"] = [{k: v for k, v in p.items() if k != "content_b64"} for p in pages_raw]
                            result["recognition_events"] = ref(folder/"recognition.ndjson")
                            result["actual_request_metadata"] = ref(folder/"request-metadata.json")
                            result.update(paddle_text_inventory(pages_raw, doc["page_count"]))
                            a_pages = pages_raw
                            a_complete = (result["status"] == "COMPLETE" and answer.get("resources_released") is True
                                          and sorted(p["page"] for p in pages_raw) == list(range(1,doc["page_count"]+1)))
                            a_complete &= result["raw_all_pages_parse_valid"] and result["raw_all_pages_have_usable_text_or_table"]
                            a_cost = result["metrics"]["controller_elapsed_seconds"]
                        else:
                            result["semantic"] = aggregate(answer.get("pages", []), doc["schema"])
                            result["metrics"]["tokens_by_page"] = [p.get("tokens") for p in answer.get("pages", [])]
                            if route == "C_PADDLE_QWEN":
                                result["metrics"]["A_reused_controller_seconds"] = a_cost
                                result["metrics"]["combined_total_seconds_including_A"] = a_cost + result["metrics"]["controller_elapsed_seconds"]
                                result["metrics"]["combined_cost_method"] = "OBSERVED_A_PLUS_OBSERVED_C_STAGES_RECONSTRUCTED_SUM_NOT_CONTIGUOUS_WALL_TIME"
                                result["A_reused_pages_exact"] = [{"page": p["page"], "sha256": p["sha256"]} for p in a_pages]
                        stop_gpu = answer.get("resources_released") is not True
                    except Exception as error:
                        result.update(status="TIMEOUT" if isinstance(error, TimeoutError) else "ERROR", executed=True,
                                      reason=type(error).__name__+":"+str(error)[:200], resources_released=False)
                        result["metrics"]["controller_elapsed_seconds"] = round(time.monotonic()-controller_started, 6)
                        result["metrics"]["call_count"] = None
                        stop_gpu = True
                    code = result.get("failure_code") or result.get("reason")
                    failures[route] = failures[route]+1 if infrastructure_failure(code) else 0
                    record("ROUTE_FINISHED", document_id=identity, route=route, status=result["status"],
                           resources_released=result.get("resources_released"), consecutive_infra_failures=failures[route],
                           actual_call_count=result["metrics"].get("call_count"), stop_gpu=stop_gpu)
                pin = save(folder/"result.json", result)
                summary.append({"document_id": identity, "route": route, "status": result["status"],
                                "executed": result["executed"], "result": pin})
                print(json.dumps({"phase": "DONE", "document_id": identity, "route": route, "status": result["status"],
                                  "executed": result["executed"], "campaign_elapsed": round(time.monotonic()-started, 1)}), flush=True)
        save(output/"summary.json", {"rows": summary, "model_document_attempts": counts, "Qwen_page_slots_reserved": qwen_reserved,
             "global_GPU_stop": stop_gpu, "elapsed_campaign_seconds": time.monotonic()-started, "operational_writes": 0})
        record("RUN_CLOSED", counts=counts, Qwen_page_slots_reserved=qwen_reserved, global_GPU_stop=stop_gpu)
    finally:
        ledger.close()


def self_test():
    spec = importlib.util.spec_from_file_location("diagnostic_worker_pure", Path(__file__).with_name("gateway_diagnostic_worker.py"))
    worker = importlib.util.module_from_spec(spec);spec.loader.exec_module(worker)
    schema = {"fields": [{"name": "number"}], "tables": [{"name": "lines", "columns": [{"name": "description"}]}]}
    full = {"fields": {"number": "A"}, "tables": {"lines": [{"description": "x"}, {"description": "x"}]}, "uncertainty": []}
    need(worker.validate_semantic(full, schema) == full, "COMPLETE_FIXTURE")
    incomplete = {"fields": {"number": None}, "tables": {"lines": []}, "uncertainty": []}
    need(worker.validate_semantic(incomplete, schema)["fields"]["number"] is None, "INCOMPLETE_FIXTURE")
    invalid = False
    try:
        json.loads("{invalid")
    except ValueError:
        invalid = True
    need(invalid, "INVALID_JSON_FIXTURE")
    unsupported = False
    try:
        worker.validate_semantic({"unsupported": True}, schema)
    except ValueError:
        unsupported = True
    need(unsupported, "UNSUPPORTED_FIXTURE")
    need(not infrastructure_failure("JSONDecodeError") and not infrastructure_failure("LLM_INCOMPLETE"), "PRECISION_NOT_INFRA")
    timeout = TimeoutError("INFERENCE_TIMEOUT")
    need(isinstance(timeout, TimeoutError), "TIMEOUT_FIXTURE")
    pages = [{"page": 1, "semantic": full}, {"page": 2, "semantic": incomplete}]
    need(aggregate(pages, schema)["fields"]["number"] == "A" and len(aggregate(pages, schema)["tables"]["lines"]) == 2, "ABSENCE_AND_DUPLICATES")
    conflict = {**full, "fields": {"number": "B"}}
    need(aggregate([pages[0], {"page": 2, "semantic": conflict}], schema)["fields"]["number"] is None, "CONFLICT_NOT_BEST_SELECTED")
    for raw in ('{"x":NaN}', '{"x":Infinity}', '{"x":1,"x":2}'):
        rejected = False
        try:
            strict_json(raw)
        except (ValueError, RuntimeError):
            rejected = True
        need(rejected, "STRICT_JSON_FIXTURE")
    import tempfile
    with tempfile.TemporaryDirectory(prefix="documentary-preflight-") as temporary:
        path = Path(temporary)/"raw.json"
        path.write_bytes(canonical({"parsing_res_list": [{"block_label":"text","block_content":"Heading"},
            {"block_label":"table","block_content":"<table><tr><th>Item</th><th>Qty</th></tr><tr><td>x</td><td>1</td></tr><tr><td>x</td><td>1</td></tr></table>"}]}))
        inventory = paddle_text_inventory([{"page":1,"path":str(path)}],1)
        need(inventory["recognized_text_scope"] == "FULL_DOCUMENT" and inventory["observed_rows_including_headers"] == 3
             and inventory["observed_logical_rows"] == 2 and inventory["observed_cols_max"] == 2
             and inventory["recognized_text"].count("x 1") == 2, "PADDLE_RAW_TEXT_INVENTORY_FIXTURE")
        need(paddle_text_inventory([{"page":1,"path":str(path)}],2)["recognized_text_scope"] == "DECLARED_PARTIAL", "PARTIAL_CER_SCOPE_FIXTURE")
    class FakeBudget:
        def remaining(self, **_kwargs):
            return 240
    class FakeGateway:
        LOCK = {"qwen": {"model": "synthetic-no-model"}}
        def __init__(self, case):
            self.case, self.BUDGETS = case, {}
        def qwen_verified(self):
            return True
        def memory_barrier(self, *_args):
            return {}
        def ollama_metrics(self, _reply):
            return {"prompt_eval_count": 20, "eval_count": 10}
        def stream_chat(self, _identity, _body, timeout, raw_capture):
            if self.case == "timeout":
                raise TimeoutError("INFERENCE_TIMEOUT")
            if self.case == "unsupported":
                raise ValueError("DOCUMENT_TYPE_UNSUPPORTED")
            content = "{invalid" if self.case == "invalid_json" else json.dumps(incomplete if self.case == "incomplete" else full)
            reply = {"message": {"content": content}, "done": True, "done_reason": "stop"}
            raw_capture(canonical(reply)+b"\n")
            return reply
    worker.emit = lambda *_args, **_kwargs: None  # Synthetic transport only; no real evidence/events.
    packet = {"route": "B_QWEN_DIRECT", "request_id": "synthetic", "schema": schema,
        "configuration": {"prompts": {"B_QWEN_DIRECT": PROMPT}, "generation_options": {},
                          "context_tokens": 32768, "output_tokens": 8192},
        "pages": [{"page": 1, "image_b64": base64.b64encode(b"synthetic-image-not-inferred").decode(),
                   "image_sha256": hashlib.sha256(b"synthetic-image-not-inferred").hexdigest()}]}
    cases = {}
    for case, expected in [("complete","COMPLETE"),("incomplete","COMPLETE"),("invalid_json","ERROR"),("unsupported","UNSUPPORTED"),("timeout","TIMEOUT")]:
        actual = worker.run_qwen(packet, FakeGateway(case), FakeBudget())
        need(actual["status"] == expected, "SYNTHETIC_TRANSPORT_"+case)
        if case == "invalid_json":
            need(base64.b64decode(actual["pages"][0]["raw_stream_b64"]), "INVALID_RAW_NOT_PRESERVED")
        cases[case] = actual["status"]
    print(json.dumps({"status": "HARNESS_SYNTHETIC_SELF_TEST_PASS", "transport_cases": cases,
        "other_cases": ["absence", "duplicates", "conflict", "schema", "precision_not_infrastructure", "strict_json", "Paddle_RAW_text_inventory", "partial_CER_scope"], "model_calls": 0}))


def main():
    parser = argparse.ArgumentParser()
    modes = parser.add_mutually_exclusive_group(required=True)
    modes.add_argument("--prepare", action="store_true")
    modes.add_argument("--self-test", action="store_true")
    modes.add_argument("--run", action="store_true")
    parser.add_argument("--sample", type=Path)
    parser.add_argument("--schemas", type=Path)
    parser.add_argument("--definitions", type=Path)
    parser.add_argument("--out", type=Path)
    parser.add_argument("--freeze", type=Path)
    parser.add_argument("--freeze-sha256")
    args = parser.parse_args()
    if args.prepare:
        prepare(args)
    elif args.self_test:
        self_test()
    else:
        run(args)


if __name__ == "__main__":
    main()
