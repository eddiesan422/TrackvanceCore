"""Versioned, page-streamed recognition. No database, storage or authority access.

Only the coordinator persists these opaque originals in controlled storage and
maps them. An HTTP200 opens the stream; only its verified terminal is a verdict.
"""
from __future__ import annotations

import base64
import hashlib
import http.client
import json
import os
import re
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path

MAX_EVENT = 24 * 1024**2
MAX_STREAM = 64 * 1024**2
TERMINALS = {}
VISUAL_PROMPT_VERSION = "trackvance-idp-098-visual-v2"
SOURCE_ASSOCIATION = "DECLARED_LABEL_CONTEXT_V1"
ASSOCIATION_PROMPT_VERSION = "trackvance-idp-098-visual-v3-source-association"
ASSOCIATION_SEMANTIC_VERSION = "trackvance-visual-semantic-098-v3-source-association"
SOURCE_ASSOCIATION_INSTRUCTIONS = (
    "For a field declaring source_association, transcribe its printed anchor caption and section "
    "caption into source_context.anchor_label and source_context.section_label. These are literal "
    "source observations, not your explanation or the requested field name. Use only an anchor "
    "and page/section allowed by that field's declared source_context. A similar word in another "
    "caption or section does not establish the requested relationship. If the allowed anchor/value "
    "relation is absent on this page, return observed=null and source_context labels=null; do not "
    "borrow a value from another label or page. Equal values may legitimately appear under different "
    "labels: keep each only when its own declared anchor/context is visible. A reported caption "
    "is not independent corroboration or measured geometry."
)
VISUAL_SOURCE_INSTRUCTIONS = (
    "Field names, sections, declared aliases and source context describe the requested meaning; "
    "a repeated caption alone does not identify the issuer, buyer or another section. "
    "An anchor_label locates a value according to its direction and section; it is not "
    "necessarily the semantic name of that value. Declared search regions are template hints, "
    "not measured value coordinates or evidence of correctness. "
    "Copy every visible requested amount into observed as a string, preserving its printed sign, "
    "currency and separators. A numeric logical type does not authorize an empty transcription "
    "or a calculated value. Return null for a field absent or unreadable on this page; "
    "retain genuinely blank source cells and unresolved alternatives. "
    "The JSON schema is a shape contract, not expected data or evidence of correctness."
)


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()


def digest(value):
    return hashlib.sha256(canonical(value)).hexdigest()


def terminal_for(identity):
    now = time.monotonic()
    for key, (created, _) in list(TERMINALS.items()):
        if now - created > 600:
            TERMINALS.pop(key, None)
    item = TERMINALS.get(identity)
    return item[1] if item else None


def visual_contract(definition):
    def cell(field):
        result = {"type": "object", "additionalProperties": False,
            "properties": {"observed": {"type": ["string", "null"], "maxLength": 16000},
                "ambiguous": {"type": "boolean"}, "alternatives": {"type": "array", "maxItems": 16,
                    "items": {"type": "string", "maxLength": 16000}}},
            "required": ["observed", "ambiguous", "alternatives"]}
        if field.get("source_association") == SOURCE_ASSOCIATION:
            result["properties"]["source_context"] = {"type": "object", "additionalProperties": False,
                "properties": {name: {"type": ["string", "null"], "maxLength": 160}
                    for name in ("anchor_label", "section_label")}, "required": ["anchor_label", "section_label"]}
            result["required"].append("source_context")
        return result
    def fields(items):
        names = [item["name"] for item in items]
        if len(names) != len(set(names)) or any(not isinstance(name, str) or not 1 <= len(name) <= 100 for name in names):
            raise ValueError("LLM_SELECTION_INVALID")
        return {"type": "object", "additionalProperties": False,
            "properties": {item["name"]: cell(item) for item in items}, "required": names}
    tables = definition.get("tables", [])
    names = [table["name"] for table in tables]
    if len(names) != len(set(names)):
        raise ValueError("LLM_SELECTION_INVALID")
    return {"type": "object", "additionalProperties": False,
        "properties": {"fields": fields(definition.get("fields", [])), "tables": {
            "type": "object", "additionalProperties": False,
            "properties": {table["name"]: {"type": "array", "maxItems": 10000,
                "items": fields(table["fields"])} for table in tables}, "required": names}},
        "required": ["fields", "tables"]}


def validate_selection(selection, definition):
    """Shape only. Contradictory/ambiguous observations remain for validation."""
    def field_names(fields):
        return {field["name"] for field in fields}
    def cells(values, fields):
        if not isinstance(values, dict) or set(values) != field_names(fields):
            raise ValueError("LLM_SELECTION_INVALID")
        for field in fields:
            value = values[field["name"]]
            required = {"observed", "ambiguous", "alternatives"}
            if field.get("source_association") == SOURCE_ASSOCIATION:
                required.add("source_context")
            if (not isinstance(value, dict) or set(value) != required
                    or value["observed"] is not None and not isinstance(value["observed"], str)
                    or type(value["ambiguous"]) is not bool or not isinstance(value["alternatives"], list)
                    or len(value["alternatives"]) > 16
                    or any(not isinstance(text, str) or len(text) > 16000 for text in value["alternatives"])
                    or isinstance(value["observed"], str) and len(value["observed"]) > 16000):
                raise ValueError("LLM_SELECTION_INVALID")
            if "source_context" in required:
                witness = value["source_context"]
                if (not isinstance(witness, dict) or set(witness) != {"anchor_label", "section_label"}
                        or any(text is not None and (not isinstance(text, str) or len(text) > 160)
                            for text in witness.values())):
                    raise ValueError("LLM_SELECTION_INVALID")
    if not isinstance(selection, dict) or set(selection) != {"fields", "tables"}:
        raise ValueError("LLM_SELECTION_INVALID")
    cells(selection["fields"], definition.get("fields", []))
    if not isinstance(selection["tables"], dict) or set(selection["tables"]) != {table["name"] for table in definition.get("tables", [])}:
        raise ValueError("LLM_SELECTION_INVALID")
    for table in definition.get("tables", []):
        rows = selection["tables"][table["name"]]
        if not isinstance(rows, list) or len(rows) > 10000:
            raise ValueError("LLM_SELECTION_INVALID")
        for row in rows:
            cells(row, table["fields"])


def semantic_definition(definition):
    """Declared semantic context, not parser samples, expected values or authority."""
    def field(value, extra_labels=()):
        result = {key: value[key] for key in ("name", "label", "logical_type", "required", "section",
            "date_format", "decimal_separator", "thousands_separator", "currency_symbols", "source_association") if key in value}
        rules = [rule for rule in [value.get("visual"), *value.get("visual_alternatives", [])] if rule]
        labels = [*value.get("aliases", []), *extra_labels]
        contexts = []
        for rule in rules:
            labels.extend([rule.get("label"), *rule.get("label_aliases", [])])
            context = {key: rule[key] for key in ("page", "section_label", "direction", "end_label") if key in rule}
            if rule.get("label"):
                context["anchor_label"] = rule["label"]
            if rule.get("region"):
                context["declared_search_region"] = list(rule["region"])
            if context and context not in contexts:
                contexts.append(context)
        aliases = list(dict.fromkeys(label for label in labels if isinstance(label, str) and label and label != value.get("label")))
        if aliases:
            result["aliases"] = aliases
        if contexts:
            result["source_context"] = contexts
        return result
    tables = []
    for table in definition.get("tables", []):
        visual = table.get("visual") or {}
        labels = table.get("structured_header_labels") or visual.get("header_labels", [])
        aliases = visual.get("header_label_aliases") or []
        columns = [field(value, ([labels[index]] if index < len(labels) else [])
            + (aliases[index] if index < len(aliases) else [])) for index, value in enumerate(table["fields"])]
        tables.append({"name": table["name"], "fields": columns, "declared_column_labels": list(labels)})
    scoped = any(item.get("source_association") == SOURCE_ASSOCIATION for item in definition.get("fields", []))
    return {"semantic_definition_version": ASSOCIATION_SEMANTIC_VERSION if scoped else "trackvance-visual-semantic-098-v2",
        "reading_mode": "VERBATIM_OBSERVED_VALUES_NOT_NORMALIZED_OR_EXPECTED_ANSWERS",
        "fields": [field(item) for item in definition.get("fields", [])], "tables": tables}


def visual_request_metadata(body, *, source_sha256, definition_sha256, configuration_sha256, image, page):
    """Exact effective text/schema/options, linked to separately retained image bytes."""
    without_images = {**body, "messages": [{key: value for key, value in message.items() if key != "images"}
        for message in body["messages"]]}
    return {"version": "TRACKVANCE_QWEN_REQUEST_V1", "strategy": "QWEN_VISUAL", "request_sha256": digest(body),
        "source_sha256": source_sha256, "definition_sha256": definition_sha256,
        "configuration_sha256": configuration_sha256, "request_without_image_bytes": without_images,
        "transport_options": {"stream": True},
        "image": {"sha256": image["sha256"], "source_page": page["page"],
            "width": page["page_geometry"]["width"], "height": page["page_geometry"]["height"],
            "query_region": [0, 0, 1, 1], "geometry_kind": "QUERY_REGION",
            "transformations": page["page_geometry"].get("transformations", [])}}


class Session:
    def __init__(self, api, handler, body):
        self.api, self.handler, self.body = api, handler, body
        self.identity, self.attempt = body["request_id"], body.get("attempt_id")
        self.strategy = body.get("strategy")
        self.configuration = body["configuration"]
        self.configuration_sha = digest(self.configuration)
        self.source_sha, self.sequence, self.total = body.get("source_sha256"), 0, 0
        self.expected, self.pages, self.completed_pages, self.started = None, [], [], False
        self.engine = {"recognition_contract": "TRACKVANCE_RECOGNITION_V1", "strategy": self.strategy,
            "runtime_provenance": {"gateway": api["startup_provenance"]()},
            "model_lock_sha256": hashlib.sha256(api["LOCK_BYTES"]).hexdigest(),
            "paddle_revision": api["LOCK"]["paddle"]["revision"],
            "paddle_model_sha256": api["LOCK"]["paddle"]["files"]["model.safetensors"],
            "layout_revision": api["LOCK"]["layout"]["revision"],
            "llm_manifest_sha256": api["LOCK"]["qwen"]["manifest_sha256"]}
        if (not isinstance(self.attempt, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,100}", self.attempt)
                or self.strategy not in {"QWEN_VISUAL", "PADDLE_STRUCTURED"}
                or body.get("configuration_sha256") != self.configuration_sha
                or body.get("definition_sha256") != digest(body.get("definition"))
                or not re.fullmatch(r"[a-f0-9]{64}", self.source_sha or "")):
            raise ValueError("SOURCE_INTEGRITY")
        config = self.configuration
        if (config.get("paddle_model") != "PaddleOCR-VL-1.6" or config.get("llm_model") != api["LOCK"]["qwen"]["model"]
                or config.get("extraction_strategy", "LEGACY") == "LEGACY"):
            raise ValueError("MODEL_NOT_PINNED")
        self.budget = api["BUDGETS"][self.identity]

    def event(self, kind, payload, *, stage, page=None, **facts):
        event = {"version": 1, "sequence": self.sequence + 1, "kind": kind, "request_id": self.identity,
            "attempt_id": self.attempt, "source_sha256": self.source_sha,
            "configuration_sha256": self.configuration_sha, "definition_sha256": self.body["definition_sha256"],
            "stage": stage, "page_one_based": page, "expected_pages": self.expected, "payload": payload, **facts}
        event["event_sha256"] = digest(event)
        return event

    def write(self, event):
        raw = canonical(event) + b"\n"
        if len(raw) > MAX_EVENT or self.total + len(raw) > MAX_STREAM:
            raise ValueError("PADDLE_RESULT_LIMIT")
        self.handler.connection.settimeout(min(1.0, max(.01, self.budget.join_remaining())))
        if not self.started:
            self.handler.send_response(200)
            self.handler.send_header("Content-Type", "application/x-ndjson")
            self.handler.send_header("Connection", "close")
            self.handler.end_headers()
            self.handler.close_connection = True
            self.handler.recognition_stream_started = self.started = True
        self.handler.wfile.write(raw)
        self.handler.wfile.flush()
        self.sequence, self.total = event["sequence"], self.total + len(raw)
        return event

    def emit(self, kind, payload, *, stage, page=None):
        return self.write(self.event(kind, payload, stage=stage, page=page))

    def phase(self, value, page=None, child_elapsed=None):
        self.api["STATES"][self.identity] = {"phase": value, "page_one_based": page,
            "expected_pages": self.expected, "emitted_pages": list(self.pages)}
        payload = {"phase": value, "remaining_work_seconds": self.budget.remaining()}
        if child_elapsed is not None:
            payload["child_observed_elapsed_seconds"] = child_elapsed
        self.emit("PHASE", payload, stage=value, page=page)

    def artifact(self, folder, record):
        relative = record["path"]
        if (not isinstance(relative, str) or "\\" in relative or Path(relative).is_absolute()
                or ".." in Path(relative).parts):
            raise ValueError("PADDLE_FAILURE")
        path = folder / relative
        if (path.is_symlink() or not path.resolve().is_relative_to(folder.resolve()) or not path.is_file()
                or path.stat().st_size > 16 * 1024**2):
            raise ValueError("PADDLE_FAILURE")
        raw = path.read_bytes()
        if len(raw) != record["bytes"] or len(raw) > 16 * 1024**2 or hashlib.sha256(raw).hexdigest() != record["sha256"]:
            raise ValueError("PADDLE_FAILURE")
        kind = record["kind"]
        media = "text/html" if kind == "PADDLE_TABLE_HTML" else "image/png" if kind == "PAGE_IMAGE_PNG" else "application/x-ndjson" if kind == "QWEN_STREAM_NDJSON" else "application/json"
        return {"kind": kind, "media_type": media, "sha256": record["sha256"], "bytes": len(raw),
            "content_b64": base64.b64encode(raw).decode("ascii")}

    def child(self, folder, operation, consume):
        self.phase("PADDLE_RECOGNITION" if operation == "STRUCTURED" else "RENDER_SOURCE")
        request = {"filename": self.body["filename"], "source_sha256": self.source_sha,
            "operation": operation, "remaining_work_seconds": self.budget.remaining()}
        (folder / "request.json").write_bytes(canonical(request))
        process = None
        consumed = 0
        failed = False
        observed_phase = None
        folder_identity = self.api["DIAGNOSTICS"].identity(folder.lstat())
        try:
            with (folder / "error.log").open("wb") as errors:
                process = subprocess.Popen([sys.executable, "-I", "-B", "/app/paddle_runner.py", str(folder)],
                    stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=errors,
                    start_new_session=True, close_fds=True)
            self.api["ACTIVE"][self.identity] = process
            while True:
                if self.identity in self.api["CANCELLED"]:
                    raise ValueError("CANCELLED")
                self.budget.remaining()
                observed = self.api["DIAGNOSTICS"].phase_from_job(folder, folder_identity,
                    self.api["CHILD_SOURCE_HASHES"], self.source_sha)
                if observed is not None:
                    latest = observed["events"][-1]
                    key = (latest["stage"], latest.get("page_ordinal"))
                    if key != observed_phase:
                        self.phase(latest["stage"], latest.get("page_ordinal"), latest["elapsed_seconds"])
                        observed_phase = key
                manifest_path = folder / "pages.manifest.json"
                if manifest_path.is_file() and manifest_path.stat().st_size <= 256 * 1024:
                    manifest = json.loads(manifest_path.read_bytes())
                    if manifest.get("source_sha256") != self.source_sha or not 1 <= manifest.get("expected_pages", 0) <= 50:
                        raise ValueError("PADDLE_FAILURE")
                    self.expected = manifest["expected_pages"]
                    for page in manifest["pages"][consumed:]:
                        consume(page)
                        consumed += 1
                if process.poll() is not None:
                    # One final checkpoint read occurs before checking exit status.
                    if manifest_path.is_file():
                        manifest = json.loads(manifest_path.read_bytes())
                        self.expected = manifest["expected_pages"]
                        for page in manifest["pages"][consumed:]:
                            consume(page)
                            consumed += 1
                    if process.returncode or not (folder / "result.json").is_file():
                        raise ValueError("PADDLE_FAILURE")
                    return json.loads((folder / "result.json").read_bytes())
                time.sleep(min(.1, self.budget.remaining()))
        except (ValueError, TypeError, KeyError, OSError, subprocess.SubprocessError):
            failed = True
            raise
        finally:
            if process is not None:
                if process.poll() is None:
                    try:
                        os.killpg(process.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                try:
                    process.wait(timeout=self.budget.join_remaining())
                except subprocess.TimeoutExpired:
                    raise self.api["MEMORY"].UnsafeLifecycle("GPU_OWNER_QUIESCENCE_UNVERIFIED") from None
                self.api["ACTIVE"].pop(self.identity, None)
                if failed:
                    try:
                        self.api["FAILURE_DIAGNOSTICS"].retain(folder, self.api["DIAGNOSTICS"].identity(folder.lstat()),
                            self.identity, self.source_sha, "PADDLE_FAILURE", process)
                    except (OSError, ValueError, KeyError, TypeError, RuntimeError):
                        self.api["RUNTIME"]["diagnostic_retention_available"] = False

    def execute(self):
        if self.api["RUNTIME"].get("ready") is not True:
            raise ValueError("GPU_RUNTIME_UNAVAILABLE")
        if not self.api["verify_models"]():
            raise ValueError("MODEL_INTEGRITY")
        filename = self.body.get("filename")
        if filename not in {"document.pdf", "document.png", "document.jpg", "document.jpeg"}:
            raise ValueError("DOCUMENT_FORMAT")
        raw = base64.b64decode(self.body.get("source", ""), validate=True)
        if not raw or len(raw) > 32 * 1024**2 or hashlib.sha256(raw).hexdigest() != self.source_sha:
            raise ValueError("SOURCE_INTEGRITY")
        self.api["memory_barrier"](self.identity, self.budget)
        with tempfile.TemporaryDirectory(prefix="idp-recognition-") as temporary:
            folder = Path(temporary)
            (folder / filename).write_bytes(raw)
            if self.strategy == "PADDLE_STRUCTURED":
                def page_ready(page):
                    number = page["page"]
                    self.emit("PAGE", {"artifacts": [self.artifact(folder, item) for item in page["artifacts"]],
                        "page_geometry": page["page_geometry"], "engine": self.engine}, stage="PADDLE_STRUCTURED", page=number)
                    self.pages.append(number)
                    self.completed_pages.append(number)
                result = self.child(folder, "STRUCTURED", page_ready)
                self.engine.update(result.get("engine", {}), paddle_revision=self.api["LOCK"]["paddle"]["revision"],
                    model_sha256=self.api["LOCK"]["paddle"]["files"]["model.safetensors"],
                    layout_revision=self.api["LOCK"]["layout"]["revision"])
                self.api["OBSERVED_INFERENCE"]["paddle"] = True
            else:
                rendered = []
                self.child(folder, "RENDER", rendered.append)
                self.api["memory_barrier"](self.identity, self.budget)
                self.visual(folder, rendered)
        if self.expected is None or sorted(self.completed_pages) != list(range(1, self.expected + 1)):
            raise ValueError("PADDLE_FAILURE")
        return {"engine": self.engine}

    def visual(self, folder, rendered):
        definition = self.body["definition"]
        schema = visual_contract(definition)
        tags = self.api["memory_request"](self.identity, self.budget, "/tags")
        pinned = self.api["LOCK"]["qwen"]
        if not isinstance(tags, dict) or not any(isinstance(item, dict) and item.get("name") == pinned["model"]
                and item.get("digest") == pinned["manifest_sha256"] for item in tags.get("models", [])):
            raise ValueError("LLM_MODEL_INTEGRITY")
        version = self.api["memory_request"](self.identity, self.budget, "/version")
        self.engine["ollama_version"] = version.get("version") if isinstance(version, dict) else None
        self.engine["llm_model_sha256"] = pinned["model_sha256"]
        self.engine["llm_projector_sha256"] = pinned["projector_sha256"]
        context = self.configuration.get("context_tokens", 32768)
        if type(context) is not int or not 1024 <= context <= 32768:
            raise ValueError("LLM_CONTEXT_LIMIT")
        output_budget = min(8192, context // 2)
        prompt = self.api["VISUAL_PROMPT"] + "\n" + VISUAL_SOURCE_INSTRUCTIONS
        source_association = any(field.get("source_association") == SOURCE_ASSOCIATION for field in definition.get("fields", []))
        if source_association:
            prompt += "\n" + SOURCE_ASSOCIATION_INSTRUCTIONS
        prompt_version = ASSOCIATION_PROMPT_VERSION if source_association else VISUAL_PROMPT_VERSION
        semantics = semantic_definition(definition)
        for page in rendered:
            number = page["page"]
            self.phase("QWEN_GENERATION", number)
            image = self.artifact(folder, page["artifacts"][0])
            body = {"model": self.api["LOCK"]["qwen"]["model"], "think": False, "keep_alive": 0,
                "format": schema, "truncate": False, "shift": False,
                "messages": [{"role": "system", "content": prompt}, {"role": "user",
                    "content": json.dumps({"page": number, "document_pages": self.expected,
                        "definition": semantics, "json_schema": schema,
                        "targets": {"fields": [field["name"] for field in definition.get("fields", [])],
                            "tables": [table["name"] for table in definition.get("tables", [])]}}, ensure_ascii=False),
                    "images": [image["content_b64"]]}],
                "options": {"num_ctx": context, "num_predict": output_budget, "temperature": 0, "seed": 98,
                    "presence_penalty": 0, "repeat_penalty": 1, "top_k": 20, "top_p": .95}}
            metadata = visual_request_metadata(body, source_sha256=self.source_sha,
                definition_sha256=self.body["definition_sha256"], configuration_sha256=self.configuration_sha,
                image=image, page=page)
            metadata_path = folder / f"qwen-{number:03}.request.json"
            metadata_bytes = canonical(metadata)
            if len(metadata_bytes) > 256 * 1024:
                raise ValueError("LLM_CONTEXT_LIMIT")
            metadata_path.write_bytes(metadata_bytes)
            metadata_record = {"path": metadata_path.name, "kind": "QWEN_REQUEST_JSON",
                "bytes": len(metadata_bytes), "sha256": hashlib.sha256(metadata_bytes).hexdigest()}
            # This precedes model generation on the stream. The existing transport
            # durably acknowledges it on the coordinator thread; the gateway does
            # not claim an additional reverse-channel persistence barrier.
            self.emit("PHASE", {"phase": "QWEN_GENERATION", "remaining_work_seconds": self.budget.remaining(),
                "recognition_strategy": "QWEN_VISUAL",
                "artifacts": [self.artifact(folder, metadata_record)]}, stage="QWEN_GENERATION", page=number)
            stream_path = folder / f"qwen-{number:03}.ndjson"
            failure = None
            with stream_path.open("xb") as stream:
                try:
                    reply = self.api["stream_chat"](self.identity, body, timeout=self.budget.remaining(), raw_capture=stream.write)
                except (ValueError, TypeError, KeyError, OSError, http.client.HTTPException) as error:
                    failure = error
                stream.flush()
                os.fsync(stream.fileno())
            if failure is not None:
                record = {"path": stream_path.name, "kind": "QWEN_STREAM_NDJSON", "bytes": stream_path.stat().st_size,
                    "sha256": hashlib.sha256(stream_path.read_bytes()).hexdigest()}
                self.emit("PAGE", {"artifacts": [image, self.artifact(folder, record)],
                    "page_geometry": page["page_geometry"], "recognition_status": "INCOMPLETE",
                    "selection_schema_valid": False, "engine": self.engine}, stage="QWEN_VISUAL", page=number)
                self.pages.append(number)
                raise failure
            reply_path = folder / f"qwen-{number:03}.reply.json"
            reply_path.write_bytes(canonical(reply))
            metrics = self.api["ollama_metrics"](reply)
            valid, selection = True, None
            try:
                selection = json.loads(reply.get("message", {}).get("content", ""))
                validate_selection(selection, definition)
            except (ValueError, TypeError, KeyError):
                valid = False
            records = [page["artifacts"][0]]
            for path, kind in [(stream_path, "QWEN_STREAM_NDJSON"), (reply_path, "QWEN_REPLY_JSON")]:
                records.append({"path": path.name, "kind": kind, "bytes": path.stat().st_size,
                    "sha256": hashlib.sha256(path.read_bytes()).hexdigest()})
            if valid:
                selection_path = folder / f"qwen-{number:03}.selection.json"
                selection_path.write_bytes(canonical(selection))
                records.append({"path": selection_path.name, "kind": "QWEN_SELECTION_JSON", "bytes": selection_path.stat().st_size,
                    "sha256": hashlib.sha256(selection_path.read_bytes()).hexdigest()})
            engine = {**{key: value for key, value in self.engine.items() if key != "page_engines"}, "llm_model": self.api["LOCK"]["qwen"]["model"],
                "llm_manifest_sha256": self.api["LOCK"]["qwen"]["manifest_sha256"],
                "prompt_version": prompt_version, "prompt_sha256": hashlib.sha256(prompt.encode()).hexdigest(),
                "semantic_definition_version": semantics["semantic_definition_version"],
                "semantic_definition_sha256": digest(semantics),
                "selection_schema_sha256": digest(schema), "request_artifact_sha256": metadata_record["sha256"],
                "ollama_metrics": metrics, "request_sha256": digest(body),
                "context_budget": {"context_tokens": context, "input_tokens": context - output_budget,
                    "output_tokens": output_budget, "truncate": False, "shift": False},
                "generation_options": body["options"], "keep_alive": 0, "think": False}
            completion_error = None
            if reply.get("done") is not True or reply.get("done_reason") != "stop":
                completion_error = "LLM_INCOMPLETE"
            elif metrics["prompt_eval_count"] is None or metrics["eval_count"] is None:
                completion_error = "LLM_BUDGET_UNVERIFIED"
            elif metrics["prompt_eval_count"] > context - output_budget or metrics["eval_count"] > output_budget:
                completion_error = "LLM_CONTEXT_LIMIT"
            elif not valid:
                completion_error = "LLM_SELECTION_INVALID"
            self.emit("PAGE", {"artifacts": [self.artifact(folder, record) for record in records],
                "page_geometry": page["page_geometry"], "image": {"sha256": image["sha256"],
                    "width": page["page_geometry"]["width"], "height": page["page_geometry"]["height"],
                    "source_page": number, "query_region": [0, 0, 1, 1], "geometry_kind": "QUERY_REGION"},
                "selection_schema_valid": valid, "recognition_status": "INCOMPLETE" if completion_error else "COMPLETE",
                "engine": engine}, stage="QWEN_VISUAL", page=number)
            self.pages.append(number)
            if completion_error:
                raise ValueError(completion_error)
            self.completed_pages.append(number)
            self.api["OBSERVED_INFERENCE"]["qwen"] = True
            self.engine.setdefault("page_engines", []).append(engine)
            self.api["memory_barrier"](self.identity, self.budget)

    def finish(self, status, released, error_code=None):
        payload = {"memory_lifecycle": released, "engine": self.engine, "error_code": error_code}
        event = self.event("TERMINAL", payload, stage=self.strategy, status=status,
            emitted_pages=list(self.pages), completed_pages=list(self.completed_pages),
            pending_pages=[page for page in range(1, (self.expected or 0) + 1) if page not in self.completed_pages],
            resources_released=released is not None)
        terminal = {"phase": "TERMINAL", "terminal": event, "resources_released": released is not None}
        TERMINALS[self.identity] = (time.monotonic(), terminal)
        while len(TERMINALS) > 16:
            TERMINALS.pop(next(iter(TERMINALS)))
        if self.started:
            self.write(event)
