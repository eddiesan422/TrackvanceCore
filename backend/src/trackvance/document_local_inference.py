"""Bounded private inference transport. The confined OCR child never gains network.

Only deployment-owned local endpoints are accepted, independent of organization
configuration and document content. AI produces observations and candidates;
publication remains the responsibility of Trackvance's independent validator.
"""
from __future__ import annotations

import base64
import copy
import hashlib
import json
import os
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import urlsplit

import httpx

from .document_engine import arithmetic_checks, cross_table_checks, validate_value
from .operations_common import OperationError

MAX_SOURCE = 32 * 1024**2
MAX_RESPONSE = 16 * 1024**2
PROMPT_VERSION = "trackvance-idp-098-v1"


def _endpoint():
    raw = os.environ.get("TRACKVANCE_IDP_LOCAL_URL", "").rstrip("/")
    parsed = urlsplit(raw)
    if (parsed.scheme != "http" or parsed.hostname not in {"127.0.0.1", "localhost", "idp-gateway"}
            or parsed.username or parsed.password or parsed.query or parsed.fragment or parsed.path):
        raise OperationError(503, "DOCUMENT_LOCAL_INFERENCE_UNCONFIGURED", "Configura el servicio IA local aislado.")
    return raw


def _request(path, body=None, *, timeout=2):
    try:
        with httpx.Client(timeout=timeout, follow_redirects=False, trust_env=False) as client, \
                client.stream("POST" if body is not None else "GET", _endpoint() + path, json=body) as response:
                if response.status_code != 200:
                    raise OperationError(503, "DOCUMENT_LOCAL_INFERENCE_UNAVAILABLE", "El servicio IA local no está disponible o está ocupado.")
                raw = bytearray()
                for chunk in response.iter_bytes():
                    raw.extend(chunk)
                    if len(raw) > MAX_RESPONSE:
                        raise OperationError(422, "DOCUMENT_LOCAL_INFERENCE_LIMIT", "La inferencia excedió el límite de respuesta.")
                return json.loads(raw)
    except (httpx.HTTPError, ValueError, OSError):
        raise OperationError(503, "DOCUMENT_LOCAL_INFERENCE_UNAVAILABLE", "No fue posible completar la inferencia IA local.") from None


def availability():
    try:
        status = _request("/health")
        if not isinstance(status, dict):
            return {"status": "UNAVAILABLE", "available": False, "execution_enabled": False,
                    "reason": "El servicio IA local devolvió un diagnóstico inválido."}
        ready = status.get("ready") is True
        return {"status": "AVAILABLE" if ready else "UNAVAILABLE", "available": ready,
                "execution_enabled": ready, "reason": status.get("reason", "Servicio IA local comprobado."),
                "observations": status}
    except OperationError as error:
        return {"status": "UNAVAILABLE", "available": False, "execution_enabled": False, "reason": error.message}


def _sufficient(payload, definition):
    if payload.get("native_incomplete_pages") or not payload.get("lines"):
        return False
    if not definition["fields"] and not definition["tables"]:
        return True  # Reference READ preserves complete native evidence without semantic work.
    cells = [payload["fields"].get(field["name"]) for field in definition["fields"]
             if field.get("required", True) or payload["fields"].get(field["name"], {}).get("observed") is not None]
    for table in definition["tables"]:
        rows = payload["tables"].get(table["name"], [])
        if not rows or payload["table_unparsed"].get(table["name"]):
            return False
        cells.extend(cell for row in rows for cell in row.values())
    return bool(cells) and all(cell and cell.get("validity") == "VALID" for cell in cells) and all(
        item["status"] == "VALID" for item in [*payload.get("arithmetic", []), *payload.get("cross_totals", [])])


def semantic_schema(definition):
    cell = {"type": "object", "properties": {
        "observed": {"type": ["string", "null"]}, "evidence_id": {"type": ["integer", "null"]}},
        "required": ["observed", "evidence_id"], "additionalProperties": False}

    def fields(items):
        return {"type": "object", "properties": {item["name"]: cell for item in items},
                "required": [item["name"] for item in items], "additionalProperties": False}

    tables = {table["name"]: {"type": "array", "maxItems": 10000, "items": fields(table["fields"])}
              for table in definition["tables"]}
    return {"type": "object", "properties": {"fields": fields(definition["fields"]),
        "tables": {"type": "object", "properties": tables, "required": list(tables), "additionalProperties": False}},
        "required": ["fields", "tables"], "additionalProperties": False}


def _semantic_cell(candidate, field, lines):
    if not isinstance(candidate, dict) or set(candidate) != {"observed", "evidence_id"}:
        raise ValueError("cell schema")
    raw, index = candidate["observed"], candidate["evidence_id"]
    if raw is None and index is None:
        value, validity = validate_value(None, field)
        return {"value": value, "observed": None, "validity": validity, "origin": None, "confidence": None, "reviewed": False}
    if (not isinstance(raw, str) or not raw or len(raw) > 65536 or type(index) is not int
            or not 0 <= index < len(lines) or raw not in lines[index]["text"]):
        raise ValueError("unobserved candidate")
    source = lines[index]
    value, validity = validate_value(raw, field)
    return {"value": value, "observed": raw, "validity": validity, "confidence": None, "reviewed": False,
            "origin": {key: source[key] for key in ("page", "region", "method")}}


def apply_semantics(payload, candidate, definition, lines):
    """No generated values, confidence, human status, or silent overwriting."""
    try:
        if not isinstance(candidate, dict) or set(candidate) != {"fields", "tables"}:
            raise ValueError()
        if set(candidate["fields"]) != {field["name"] for field in definition["fields"]}:
            raise ValueError()
        if set(candidate["tables"]) != {table["name"] for table in definition["tables"]}:
            raise ValueError()
        fields = {field["name"]: _semantic_cell(candidate["fields"][field["name"]], field, lines) for field in definition["fields"]}
        tables = {}
        for table in definition["tables"]:
            rows = candidate["tables"][table["name"]]
            if not isinstance(rows, list) or len(rows) > 10000:
                raise ValueError()
            tables[table["name"]] = []
            for row in rows:
                if not isinstance(row, dict) or set(row) != {field["name"] for field in table["fields"]}:
                    raise ValueError()
                tables[table["name"]].append({field["name"]: _semantic_cell(row[field["name"]], field, lines) for field in table["fields"]})
    except (KeyError, TypeError, ValueError):
        raise OperationError(422, "DOCUMENT_LLM_EVIDENCE_INVALID", "La respuesta semántica contiene campos sin evidencia verificable.") from None
    result = copy.deepcopy(payload)
    conflicts = []
    for name, cell in fields.items():
        previous = result["fields"][name]
        if previous["validity"] == "VALID" and previous["observed"] is not None:
            if cell["observed"] is not None and previous["value"] != cell["value"]:
                conflicts.append("FIELD:" + name)
                result["fields"][name] = {**previous, "validity": "AMBIGUOUS", "alternatives": [previous["observed"], cell["observed"]]}
        elif cell["observed"] is not None:
            result["fields"][name] = cell
    for name, rows in tables.items():
        previous = result["tables"][name]
        if not previous:
            result["tables"][name] = rows
        elif [[cell["value"] for cell in row.values()] for row in previous] != [[cell["value"] for cell in row.values()] for row in rows]:
            conflicts.append("TABLE:" + name)
    result["semantic_conflicts"] = conflicts
    result["semantic_candidates"] = {"fields": fields, "tables": tables}
    result["arithmetic"] = arithmetic_checks(result, definition)
    if definition.get("cross_totals"):
        result["cross_totals"] = cross_table_checks(result, definition)
    period = definition.get("business_period_field")
    if period:
        result["business_period"] = {"field": period, **result["fields"][period]}
    return result


def _invoke(path, body, *, timeout, started, check, minimum_progress=15):
    identity = body["request_id"]
    remaining = max(1, timeout - (time.monotonic() - started))
    executor = ThreadPoolExecutor(max_workers=1)
    future = executor.submit(_request, path, body, timeout=remaining)
    completed = False
    last_phase_poll = 0.0
    phase_progress = minimum_progress
    try:
        while not future.done():
            if time.monotonic() - last_phase_poll >= 2:
                try:
                    phase = _request("/status/" + identity, timeout=1)
                    phase_progress = max(minimum_progress, {"QUEUED": 15, "PADDLE": 25, "QWEN": 65}.get(phase.get("phase"), phase_progress))
                except OperationError:
                    pass  # Registration/finish can race the read-only status poll.
                last_phase_poll = time.monotonic()
            check(phase_progress)  # Real service phases; elapsed time never becomes invented progress.
            if time.monotonic() - started > timeout:
                raise OperationError(422, "DOCUMENT_TIMEOUT", "La inferencia IA superó el tiempo configurado.")
            time.sleep(.2)
        response = future.result()
        completed = True
    finally:
        if not completed:
            try:
                _request("/cancel", {"request_id": identity}, timeout=2)
            except OperationError:
                pass
        executor.shutdown(wait=False, cancel_futures=True)
    return response


def run_local(source, definition, configuration, *, legacy_runner, check):
    if configuration.get("prompt_version", PROMPT_VERSION) != PROMPT_VERSION:
        raise OperationError(422, "DOCUMENT_PROMPT_UNKNOWN", "La versión del prompt no está instalada.")
    started = time.monotonic()
    native = legacy_runner(source, definition, check=lambda progress: check(int(progress * .15)), native_only=True)
    if _sufficient(native["payload"], definition):
        native["engine"].update(key="PADDLE_QWEN_LOCAL_098", route="DIGITAL_NATIVE", inference_skipped=True)
        return native
    if source.stat().st_size > MAX_SOURCE:
        raise OperationError(422, "DOCUMENT_LOCAL_SOURCE_LIMIT", "El documento excede el límite de transferencia IA local.")
    raw = source.read_bytes()
    body = {"request_id": uuid.uuid4().hex, "filename": "document" + source.suffix.lower(),
        "source": base64.b64encode(raw).decode(), "source_sha256": hashlib.sha256(raw).hexdigest(),
        "configuration": {**configuration, "use_llm": False}, "definition": definition,
        "semantic_schema": semantic_schema(definition)}
    timeout = min(1800, max(30, configuration.get("timeout_seconds", 300)))
    response = _invoke("/extract", body, timeout=timeout, started=started, check=check)
    if (not isinstance(response, dict) or response.get("source_sha256") != body["source_sha256"]
            or not isinstance(response.get("lines"), list) or not isinstance(response.get("engine"), dict)):
        raise OperationError(422, "DOCUMENT_LOCAL_INFERENCE_CONTRACT", "La inferencia no devolvió evidencia del documento solicitado.")
    result = legacy_runner(source, definition, check=lambda progress: check(35 + int(progress * .15)),
                           native_only=True, candidate_lines=response["lines"])
    semantic_used = False
    # Native gaps decide OCR need. Once real OCR observations exist, field/type/
    # table checks decide semantic need; missing spatial proof still blocks AUTO.
    needs_semantics = not _sufficient({**result["payload"], "native_incomplete_pages": []}, definition)
    if configuration.get("use_llm", True) and response["lines"] and needs_semantics:
        observation_hash = hashlib.sha256(json.dumps(response["lines"], sort_keys=True, ensure_ascii=False).encode()).hexdigest()
        interpretation = _invoke("/interpret", {"request_id": uuid.uuid4().hex, "source_sha256": body["source_sha256"],
            "configuration": configuration, "definition": definition, "semantic_schema": semantic_schema(definition),
            "observations": response["lines"], "observations_sha256": observation_hash},
            timeout=timeout, started=started, check=check, minimum_progress=50)
        if (not isinstance(interpretation, dict) or interpretation.get("source_sha256") != body["source_sha256"]
                or interpretation.get("observations_sha256") != observation_hash or "semantics" not in interpretation):
            raise OperationError(422, "DOCUMENT_LOCAL_INFERENCE_CONTRACT", "La interpretación no corresponde a la evidencia OCR solicitada.")
        result["payload"] = apply_semantics(result["payload"], interpretation["semantics"], definition, response["lines"])
        response["engine"].update({key: value for key, value in interpretation["engine"].items() if key != "phases"})
        response["engine"].setdefault("phases", {}).update(interpretation["engine"].get("phases", {}))
        semantic_used = True
    result["engine"].update(response["engine"], key="PADDLE_QWEN_LOCAL_098", route="PADDLE_QWEN" if semantic_used else "PADDLE",
        prompt_version=PROMPT_VERSION, transport="PRIVATE_LOCAL_HTTP", inference_skipped=False)
    result["engine"]["elapsed_seconds"] = round(time.monotonic() - started, 4)
    check(100)
    return result
