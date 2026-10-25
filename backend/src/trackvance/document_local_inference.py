"""Bounded private inference transport. The confined OCR child never gains network.

Only deployment-owned local endpoints are accepted, independent of organization
configuration and document content. AI produces observations and candidates;
publication remains the responsibility of Trackvance's independent validator.
"""
from __future__ import annotations

import asyncio
import base64
import copy
import hashlib
import json
import math
import os
import threading
import time
import unicodedata
import uuid
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeout
from urllib.parse import urlsplit

import httpx

from .document_engine import arithmetic_checks, cross_table_checks, validate_value
from .document_inference_completion import (
    AttemptBudget,
    DocumentDeadlineExpired,
    make_marker,
    source_pages,
    trusted_partial_result,
    usable_candidate,
)
from .document_literal_tables import map_candidates, merge_candidates, prompt_catalog_view
from .document_semantic_fragments import (
    ESCAPED_LINE_BREAK,
    build_catalog,
    canonical_observed,
    escaped_break_normalization,
    multiline_fields,
)
from .operations_common import OperationError

MAX_SOURCE = 32 * 1024**2
MAX_RESPONSE = 16 * 1024**2
PROMPT_VERSION = "trackvance-idp-098-v2"
LEGACY_PROMPT_VERSION = "trackvance-idp-098-v1"
SUPPORTED_PROMPTS = {LEGACY_PROMPT_VERSION, PROMPT_VERSION}


class RemoteInferenceTimeout(OperationError):
    def __init__(self):
        super().__init__(422, "DOCUMENT_TIMEOUT", "La inferencia IA superó el tiempo configurado.")


class OptionalInferenceTimeout(RemoteInferenceTimeout):
    def __init__(self, request_id):
        super().__init__()
        self.request_id = request_id


class _RequestControl:
    def __init__(self):
        self.lock, self.loop, self.task, self.cancelled = threading.Lock(), None, None, False

    def register(self, loop, task):
        with self.lock:
            self.loop, self.task = loop, task
            if self.cancelled:
                task.cancel()

    def cancel(self):
        with self.lock:
            self.cancelled = True
            if self.loop is not None and self.task is not None and not self.loop.is_closed():
                try:
                    self.loop.call_soon_threadsafe(self.task.cancel)
                except RuntimeError:
                    pass  # A completed request can close its loop concurrently.
SERVICE_ERRORS = {
    "LLM_CONTEXT_LIMIT": (422, "DOCUMENT_LLM_CONTEXT_LIMIT", "La evidencia excede el contexto IA configurado; amplía el contexto o reduce el documento."),
    "LLM_INCOMPLETE": (422, "DOCUMENT_LLM_INCOMPLETE", "El modelo no completó la respuesta estructurada dentro de su presupuesto."),
    "LLM_BUDGET_UNVERIFIED": (422, "DOCUMENT_LLM_BUDGET_UNVERIFIED", "No fue posible verificar el presupuesto de contexto de la respuesta IA."),
    "LLM_FAILURE": (422, "DOCUMENT_LLM_FAILURE", "El modelo semántico local no pudo completar la interpretación."),
    "MODEL_INTEGRITY": (503, "DOCUMENT_LOCAL_MODEL_INTEGRITY", "Los archivos del modelo OCR no coinciden con la versión fijada."),
    "LLM_MODEL_INTEGRITY": (503, "DOCUMENT_LOCAL_MODEL_INTEGRITY", "El modelo semántico no coincide con la versión fijada."),
    "MODEL_NOT_PINNED": (422, "DOCUMENT_LOCAL_MODEL_UNSUPPORTED", "La configuración solicita una versión de modelo o prompt no instalada."),
    "GPU_RUNTIME_UNAVAILABLE": (503, "DOCUMENT_LOCAL_RUNTIME_UNAVAILABLE", "El runtime GPU local no ha superado la comprobación CUDA."),
    "PADDLE_FAILURE": (422, "DOCUMENT_PADDLE_FAILURE", "El modelo OCR local no pudo completar la lectura del documento."),
    "PADDLE_RESULT_LIMIT": (422, "DOCUMENT_LOCAL_INFERENCE_LIMIT", "La lectura OCR excedió el límite de respuesta."),
    "LLM_RESPONSE_LIMIT": (422, "DOCUMENT_LOCAL_INFERENCE_LIMIT", "La interpretación IA excedió el límite de respuesta."),
    "INFERENCE_TIMEOUT": (422, "DOCUMENT_TIMEOUT", "La inferencia IA superó el tiempo configurado."),
    "CANCELLED": (422, "DOCUMENT_CANCELLED", "La inferencia IA fue cancelada."),
    "LLM_FRAGMENT_LIMIT": (422, "DOCUMENT_LLM_FRAGMENT_LIMIT", "La evidencia excede el catálogo acotado de fragmentos IA."),
    "LLM_FRAGMENT_INTEGRITY": (422, "DOCUMENT_LLM_EVIDENCE_INVALID", "No se pudo verificar el catálogo de evidencia OCR."),
    "LLM_SELECTION_INVALID": (422, "DOCUMENT_LLM_EVIDENCE_INVALID", "La interpretación seleccionó evidencia fuera del contrato."),
    "GPU_QUEUE_FULL": (503, "DOCUMENT_LOCAL_INFERENCE_BUSY", "La capacidad de inferencia local está ocupada."),
    "CONFIGURED_INFERENCE_CAPACITY": (503, "DOCUMENT_LOCAL_INFERENCE_BUSY", "Se alcanzó el máximo de trabajos IA configurado."),
    "GPU_QUEUE_TIMEOUT": (422, "DOCUMENT_TIMEOUT", "El trabajo agotó su tiempo esperando la GPU local."),
    "CONTROL_BUSY": (503, "DOCUMENT_LOCAL_INFERENCE_BUSY", "El servicio IA local está ocupado."),
    "SOURCE_INTEGRITY": (422, "DOCUMENT_LOCAL_INFERENCE_CONTRACT", "El servicio rechazó la integridad del documento transferido."),
    "OBSERVATION_INTEGRITY": (422, "DOCUMENT_LOCAL_INFERENCE_CONTRACT", "El servicio rechazó la integridad de la evidencia OCR."),
    "REQUEST_LIMIT": (422, "DOCUMENT_LOCAL_SOURCE_LIMIT", "La solicitud excede el límite de transferencia IA local."),
}


def _endpoint():
    raw = os.environ.get("TRACKVANCE_IDP_LOCAL_URL", "").rstrip("/")
    parsed = urlsplit(raw)
    if (parsed.scheme != "http" or parsed.hostname not in {
            "127.0.0.1", "localhost", "idp-gateway", "idp-ingress", "host.docker.internal"}
            or parsed.username or parsed.password or parsed.query or parsed.fragment or parsed.path):
        raise OperationError(503, "DOCUMENT_LOCAL_INFERENCE_UNCONFIGURED", "Configura el servicio IA local aislado.")
    return raw


def _decoded_response(raw, status):
    value = json.loads(raw)
    if status != 200:
        diagnostic = value.get("error") if isinstance(value, dict) else None
        known = SERVICE_ERRORS.get(diagnostic) if isinstance(diagnostic, str) else None
        if known:
            if diagnostic in {"INFERENCE_TIMEOUT", "GPU_QUEUE_TIMEOUT"}:
                raise RemoteInferenceTimeout()
            raise OperationError(*known)
        raise OperationError(503, "DOCUMENT_LOCAL_INFERENCE_UNAVAILABLE", "El servicio IA local no está disponible o está ocupado.")
    return value


async def _async_request(path, body, timeout, control):
    control.register(asyncio.get_running_loop(), asyncio.current_task())
    try:
        async with asyncio.timeout(timeout):
            async with httpx.AsyncClient(timeout=timeout, follow_redirects=False, trust_env=False) as client, \
                    client.stream("POST", _endpoint() + path, json=body) as response:
                raw = bytearray()
                async for chunk in response.aiter_bytes():
                    raw.extend(chunk)
                    if len(raw) > MAX_RESPONSE:
                        raise OperationError(422, "DOCUMENT_LOCAL_INFERENCE_LIMIT", "La inferencia excedió el límite de respuesta.")
                return _decoded_response(raw, response.status_code)
    except (TimeoutError, httpx.TimeoutException):
        raise RemoteInferenceTimeout() from None
    except (httpx.HTTPError, ValueError, OSError):
        raise OperationError(503, "DOCUMENT_LOCAL_INFERENCE_UNAVAILABLE", "No fue posible completar la inferencia IA local.") from None


def _request(path, body=None, *, timeout=2, control=None):
    if control is not None:
        return asyncio.run(_async_request(path, body, timeout, control))
    try:
        with httpx.Client(timeout=timeout, follow_redirects=False, trust_env=False) as client, \
                client.stream("POST" if body is not None else "GET", _endpoint() + path, json=body) as response:
                raw = bytearray()
                for chunk in response.iter_bytes():
                    raw.extend(chunk)
                    if len(raw) > MAX_RESPONSE:
                        raise OperationError(422, "DOCUMENT_LOCAL_INFERENCE_LIMIT", "La inferencia excedió el límite de respuesta.")
                return _decoded_response(raw, response.status_code)
    except httpx.TimeoutException:
        raise RemoteInferenceTimeout() from None
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


def semantic_targets(payload, definition):
    conflicts = {value for value in payload.get("semantic_conflicts", []) if isinstance(value, str)}
    return {"fields": [field["name"] for field in definition["fields"]
            if payload["fields"][field["name"]]["validity"] != "VALID"
            or payload["fields"][field["name"]].get("observed") is None
            or any(prefix + field["name"] in conflicts for prefix in ("FIELD:", "REEXTRACTION_FIELD:"))],
        "tables": [table["name"] for table in definition["tables"]
            if not payload["tables"][table["name"]] or payload["table_unparsed"][table["name"]]
            or any(cell["validity"] != "VALID" for row in payload["tables"][table["name"]] for cell in row.values())
            or any(prefix + table["name"] in conflicts for prefix in ("TABLE:", "REEXTRACTION_TABLE:"))]}


def fragment_schema(definition, catalog, targets, *, allowed_fragment_ids=None):
    def cell(enabled):
        identity: dict = {"type": ["integer", "null"], "minimum": 0,
                    "maximum": len(catalog["fragments"]) - 1} if enabled and catalog["fragments"] else {"type": "null"}
        if enabled and allowed_fragment_ids is not None:
            identity = {"type": ["integer", "null"], "enum": [None, *allowed_fragment_ids]}
        return {"type": "object", "properties": {"fragment_id": identity},
            "required": ["fragment_id"], "additionalProperties": False}

    fields = {field["name"]: cell(field["name"] in targets["fields"]) for field in definition["fields"]}
    tables = {}
    for table in definition["tables"]:
        enabled = table["name"] in targets["tables"]
        tables[table["name"]] = {"type": "array", "maxItems": 10000 if enabled else 0,
            "items": {"type": "object", "properties": {field["name"]: cell(enabled) for field in table["fields"]},
                "required": [field["name"] for field in table["fields"]], "additionalProperties": False}}
    return {"type": "object", "properties": {
        "fields": {"type": "object", "properties": fields, "required": list(fields), "additionalProperties": False},
        "tables": {"type": "object", "properties": tables, "required": list(tables), "additionalProperties": False}},
        "required": ["fields", "tables"], "additionalProperties": False}


def apply_fragment_semantics(payload, candidate, definition, lines, catalog, targets):
    allowed = multiline_fields(definition)

    def resolve(selection, field, scope, enabled):
        if not isinstance(selection, dict) or set(selection) != {"fragment_id"}:
            raise ValueError("fragment cell schema")
        identity = selection["fragment_id"]
        if identity is None:
            return _semantic_cell({"observed": None, "evidence_id": None}, field, lines)
        if (not enabled or type(identity) is not int or not 0 <= identity < len(catalog["fragments"])
                or identity not in visible_ids):
            raise ValueError("fragment selection")
        fragment = catalog["fragments"][identity]
        cell = _semantic_cell({"observed": fragment["text"], "evidence_id": fragment["evidence_id"]}, field, lines)
        cell["source_fragment"] = {"catalog_sha256": catalog["sha256"],
            **{key: fragment[key] for key in ("fragment_id", "evidence_id", "start", "end")}}
        if (scope, field["name"]) in allowed and ESCAPED_LINE_BREAK in fragment["text"]:
            cell["source_text_normalization"] = escaped_break_normalization(cell["observed"])
            cell["value"], cell["validity"] = validate_value(canonical_observed(cell, allow_multiline=True), field)
        return cell

    try:
        if catalog != build_catalog(lines) or targets != semantic_targets(payload, definition):
            raise ValueError("fragment catalog or target integrity")
        view = prompt_catalog_view(lines, definition, targets)["prompt_view"]
        visible_ids = {item["fragment_id"] for item in view["fragments"]}
        if (not isinstance(candidate, dict) or set(candidate) != {"fields", "tables"}
                or not isinstance(candidate["fields"], dict) or not isinstance(candidate["tables"], dict)
                or set(candidate["fields"]) != {field["name"] for field in definition["fields"]}
                or set(candidate["tables"]) != {table["name"] for table in definition["tables"]}):
            raise ValueError("fragment schema")
        fields = {field["name"]: resolve(candidate["fields"][field["name"]], field, "headers",
            field["name"] in targets["fields"]) for field in definition["fields"]}
        tables: dict[str, list[dict[str, dict]]] = {}
        for table in definition["tables"]:
            name = table["name"]
            rows = candidate["tables"][name]
            if not isinstance(rows, list) or len(rows) > 10000 or (name not in targets["tables"] and rows):
                raise ValueError("fragment table")
            if name not in targets["tables"]:
                continue
            tables[name] = []
            for row in rows:
                if not isinstance(row, dict) or set(row) != {field["name"] for field in table["fields"]}:
                    raise ValueError("fragment row")
                tables[name].append({field["name"]: resolve(row[field["name"]], field, name, True) for field in table["fields"]})
    except (KeyError, TypeError, ValueError, IndexError):
        raise OperationError(422, "DOCUMENT_LLM_EVIDENCE_INVALID", "La interpretación seleccionó fragmentos fuera de la evidencia verificable.") from None
    return _merge_semantic_cells(payload, fields, tables, definition)


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
        tables: dict[str, list[dict[str, dict]]] = {}
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
    return _merge_semantic_cells(payload, fields, tables, definition)


def _merge_semantic_cells(payload, fields, tables, definition):
    result = copy.deepcopy(payload)
    conflicts = list(payload.get("semantic_conflicts", []))
    for name, cell in fields.items():
        previous = result["fields"][name]
        if (previous["validity"] == "VALID" or previous.get("reextraction_conflict")) and previous["observed"] is not None:
            if cell["observed"] is not None and previous["value"] != cell["value"]:
                conflicts.append("FIELD:" + name)
                result["fields"][name] = {**previous, "validity": "AMBIGUOUS", "alternatives": [previous["observed"], cell["observed"]]}
        elif cell["observed"] is not None:
            result["fields"][name] = cell
    for name, rows in tables.items():
        previous = result["tables"][name]
        if not previous:
            result["tables"][name] = rows
        elif [{key: cell["value"] for key, cell in row.items()} for row in previous] != [
                {key: cell["value"] for key, cell in row.items()} for row in rows]:
            conflicts.append("TABLE:" + name)
    result["semantic_conflicts"] = conflicts
    result["semantic_candidates"] = {"fields": fields, "tables": tables}
    return _refresh_checks(result, definition)


def _refresh_checks(result, definition):
    result["arithmetic"] = arithmetic_checks(result, definition)
    if definition.get("cross_totals"):
        result["cross_totals"] = cross_table_checks(result, definition)
    period = definition.get("business_period_field")
    if period:
        result["business_period"] = {"field": period, **result["fields"][period]}
    return result


def _literal_candidate(payload, lines, definition):
    try:
        mapped = map_candidates(lines, definition)
        result = merge_candidates(payload, mapped, definition, validate_value, observations=lines)
    except (TypeError, ValueError, KeyError):
        raise OperationError(422, "DOCUMENT_LITERAL_TABLE_INVALID", "No se pudo vincular la tabla literal con la evidencia OCR validada.") from None
    return _refresh_checks(result, definition)


def _region(target, margin=0):
    page, box = target.get("page"), target.get("region")
    if (type(page) is not int or not 1 <= page <= 50 or not isinstance(box, list) or len(box) != 4
            or any(type(value) not in (int, float) or not math.isfinite(value) or not 0 <= value <= 1 for value in box)
            or box[0] >= box[2] or box[1] >= box[3]):
        return None
    return {"page": page, "region": [max(0, box[0] - margin), max(0, box[1] - margin),
                                      min(1, box[2] + margin), min(1, box[3] + margin)]}


def _contains(outer, inner):
    a, b = outer["region"], inner["region"]
    return outer["page"] == inner["page"] and a[0] <= b[0] and a[1] <= b[1] and a[2] >= b[2] and a[3] >= b[3]


def _label(value):
    return " ".join("".join(char for char in unicodedata.normalize("NFKD", value.casefold())
                            if not unicodedata.combining(char)).replace(":", " ").split())


def anchored_region(field, observations):
    """Use one observed anchor and declared search bounds; never a page fallback."""
    visual = field.get("visual") or {}
    search = _region(visual)
    if not search:
        return None
    if visual.get("kind", "LABEL_VALUE") == "REGION_CONTEXT":
        return search if search["region"] != [0, 0, 1, 1] else None
    labels = {_label(label) for label in [visual.get("label", ""), field.get("label", ""),
                                        *visual.get("label_aliases", [])] if label}
    bounds = list(search["region"])
    if visual.get("section_bounds"):
        bounds[0] = max(bounds[0], visual["section_bounds"][0])
        bounds[2] = min(bounds[2], visual["section_bounds"][1])
    matches = []
    for line in observations:
        measured = _region(line)
        if measured and _contains({"page": search["page"], "region": bounds}, measured):
            text = _label(line["text"])
            if any(text == label or text.startswith(label + " ") for label in labels):
                matches.append(measured)
    if len(matches) != 1:
        return None  # Repeated labels require independent disambiguation.
    box = matches[0]["region"]
    tolerance = visual.get("tolerance", .12)
    if visual.get("direction", "RIGHT") == "BELOW":
        target = [max(bounds[0], box[0] - tolerance), box[1], min(bounds[2], box[2] + tolerance),
                  min(bounds[3], box[3] + tolerance)]
    else:
        target = [box[0], max(bounds[1], box[1] - tolerance), bounds[2], min(bounds[3], box[3] + tolerance)]
    return _region({"page": search["page"], "region": target})


def _candidate_complete(payload, definition):
    """Typed completeness decides GPU work; approval still requires spatial proof."""
    candidate = {**payload, "native_incomplete_pages": [], "table_unparsed": {}}
    return not payload.get("semantic_conflicts") and _sufficient(candidate, definition)


def focused_regions(payload, definition, observations):
    """At most four non-contained expanded crops for unresolved candidates."""
    targets = []
    for field in definition["fields"]:
        cell = payload["fields"].get(field["name"], {})
        if cell.get("validity") != "VALID" or field.get("required", True) and cell.get("observed") is None:
            origin = anchored_region(field, observations) or cell.get("origin")
            if origin:
                targets.append(origin)
    table_problem = any(not payload["tables"].get(table["name"])
        or any(conflict.endswith(":" + table["name"]) for conflict in payload.get("semantic_conflicts", []))
        or any(cell.get("validity") != "VALID" for row in payload["tables"].get(table["name"], []) for cell in row.values())
        for table in definition["tables"])
    if table_problem:
        targets.extend(line for line in observations if line.get("block_label") == "table")
    for check in [*payload.get("arithmetic", []), *payload.get("cross_totals", [])]:
        if check.get("status") != "VALID":
            names = [check.get("header"), check.get("field")]
            if not check.get("table"):
                for rule in definition.get("arithmetic", []):
                    if rule["name"] == check.get("name"):
                        names.extend([rule["result"], *rule["terms"], *rule.get("subtract", [])])
            targets.extend(payload["fields"][name]["origin"] for name in names
                           if name in payload["fields"] and payload["fields"][name].get("origin"))
            if check.get("table"):
                rows = payload["tables"].get(check["table"], [])
                index = check.get("row")
                if type(index) is int and 0 <= index < len(rows):
                    cells = rows[index]
                    selected = [cells[check["field"]]] if check.get("field") in cells else cells.values()
                    targets.extend(cell["origin"] for cell in selected if cell.get("origin"))
                else:
                    targets.extend(line for line in observations if line.get("block_label") == "table")
    output: list[dict] = []
    for raw in targets:
        target = _region(raw, margin=.02)
        if target is None or target["region"] == [0, 0, 1, 1] or any(_contains(old, target) for old in output):
            continue
        output = [old for old in output if not _contains(target, old)]
        if len(output) < 4:
            output.append(target)
    return output


def refined_observations(original, refined, regions):
    """Replace only covered regions while retaining the preceding evidence elsewhere."""
    def covered(line):
        a = line["region"]
        area = (a[2] - a[0]) * (a[3] - a[1])
        for target in regions:
            b = target["region"]
            intersection = max(0, min(a[2], b[2]) - max(a[0], b[0])) * max(0, min(a[3], b[3]) - max(a[1], b[1]))
            if line["page"] == target["page"] and area > 0 and intersection / area >= .6:
                return True
        return False

    if not refined:
        return list(original)
    # Overlapping crops can return the very same measured observation twice.
    # Position remains part of identity so repeated rows elsewhere survive.
    output, seen = [line for line in original if not covered(line)], set()
    for line in refined:
        identity = (line["page"], tuple(line["region"]), line["text"],
            tuple((word["text"], tuple(word["region"])) for word in line.get("words", [])))
        measured = line.get("block_label") == "spotting" and bool(line.get("words"))
        if not measured or identity not in seen:
            output.append(line)
            if measured:
                seen.add(identity)
    return output


def _missing_table_fills(rows, proposed, table, conflicts):
    """Identify only empty cells in a uniquely matched, otherwise unchanged table.

    Both inputs have already passed the source remap/contract boundary. This
    comparison grants no source coverage, human review, or approval authority.
    """
    name = table.get("name")
    fields = table.get("fields", [])
    names = {field["name"] for field in fields}
    keys = [field["name"] for field in fields if field.get("business_key")]
    start = (table.get("visual") or {}).get("row_start")
    if not keys and start and type(start.get("column")) is int and 0 <= start["column"] < len(fields):
        keys = [fields[start["column"]]["name"]]
    if (not keys or not rows or len(rows) != len(proposed)
            or any(prefix + str(name) in conflicts for prefix in ("TABLE:", "REEXTRACTION_TABLE:"))
            or any(set(row) != names for row in [*rows, *proposed])):
        return []
    identities = []
    for group in (rows, proposed):
        identity = []
        for row in group:
            cells = [row[key] for key in keys]
            if any(cell.get("validity") != "VALID" or cell.get("reextraction_conflict") or cell.get("alternatives")
                    or not isinstance(cell.get("value"), str) or not cell["value"].strip()
                    or not isinstance(cell.get("observed"), str) or not cell["observed"].strip() for cell in cells):
                return []
            identity.append(tuple(cell["value"] for cell in cells))
        if len(set(identity)) != len(identity):
            return []
        identities.append(identity)
    if identities[0] != identities[1]:
        return []
    fills: list[tuple[int, str]] = []
    for index, (previous_row, current_row) in enumerate(zip(rows, proposed, strict=True)):
        for key, previous in previous_row.items():
            current = current_row[key]
            if (previous.get("reextraction_conflict") or current.get("reextraction_conflict")
                    or previous.get("alternatives") or current.get("alternatives")
                    or previous.get("validity") == "AMBIGUOUS" or current.get("validity") == "AMBIGUOUS"):
                return []
            if (previous.get("observed"), previous.get("value"), previous.get("validity")) == (
                    current.get("observed"), current.get("value"), current.get("validity")):
                continue
            if (previous.get("validity") != "MISSING" or previous.get("reviewed") is not False
                    or previous.get("alternatives") or previous.get("corrected")
                    or any(previous.get(item) is not None for item in (
                        "observed", "value", "origin", "source_fragment", "candidate_text"))
                    or current.get("validity") != "VALID" or current.get("reviewed") is not False
                    or current.get("alternatives") or current.get("corrected")
                    or not isinstance(current.get("observed"), str) or not current["observed"].strip()
                    or not isinstance(current.get("value"), str) or not current["value"].strip()
                    or not isinstance(current.get("origin"), dict) or _region(current["origin"]) is None
                    or not isinstance(current["origin"].get("method"), str)):
                return []
            fills.append((index, key))
    return fills



def retain_disagreements(before, after, definition=None):
    """A retry may propose alternatives but cannot erase a preceding valid value."""
    conflicts = list(before.get("semantic_conflicts", []))
    for name, previous in before["fields"].items():
        current = after["fields"][name]
        if (previous.get("validity") == "VALID" or previous.get("reextraction_conflict")) and previous.get("observed") is not None and (
                current.get("value") != previous.get("value") or previous.get("reextraction_conflict")):
            after["fields"][name] = {**previous, "validity": "AMBIGUOUS", "reextraction_conflict": True,
                "alternatives": list(dict.fromkeys(value for value in [previous["observed"], current.get("observed")] if value is not None))}
            conflicts.append("REEXTRACTION_FIELD:" + name)
    for name, rows in before["tables"].items():
        proposed = after["tables"][name]
        previous_values = [{key: (cell.get("value"), cell.get("observed")) for key, cell in row.items()} for row in rows]
        proposed_values = [{key: (cell.get("value"), cell.get("observed")) for key, cell in row.items()} for row in proposed]
        if rows and (previous_values != proposed_values
                or any(cell.get("reextraction_conflict") for row in rows for cell in row.values())):
            table: dict = next((item for item in (definition or {}).get("tables", []) if item["name"] == name), {})
            fills = _missing_table_fills(rows, proposed, table, [*conflicts, *after.get("semantic_conflicts", [])])
            if fills:
                record = {"before": copy.deepcopy(rows), "after": copy.deepcopy(proposed),
                    "reason": "EMPTY_MISSING_FILLED", "filled_cells": [{"row": index, "field": key} for index, key in fills]}
                after.setdefault("reextraction_table_candidates", {})[name] = record
                history = copy.deepcopy(before.get("reextraction_table_history", {}).get(name, []))
                after.setdefault("reextraction_table_history", {})[name] = [*history, copy.deepcopy(record)]
                after["tables"][name] = copy.deepcopy(rows)
                for index, key in fills:
                    after["tables"][name][index][key] = copy.deepcopy(proposed[index][key])
                continue
            conflicts.append("REEXTRACTION_TABLE:" + name)
            keys = [field["name"] for field in table.get("fields", []) if field.get("business_key")]
            start = (table.get("visual") or {}).get("row_start")
            if not keys and start:
                keys = [table["fields"][start["column"]]["name"]]
            if not keys:
                keys = sorted(rows[0])
            structural = len(rows) != len(proposed) or [
                tuple(row.get(key, {}).get("value") for key in keys) for row in rows] != [
                tuple(row.get(key, {}).get("value") for key in keys) for row in proposed]
            after.setdefault("reextraction_table_candidates", {})[name] = {
                "before": copy.deepcopy(rows), "after": copy.deepcopy(proposed),
                "reason": "ROW_COUNT_OR_IDENTITY_ORDER" if structural else "CELL_VALUES"}
            history = copy.deepcopy(before.get("reextraction_table_history", {}).get(name, []))
            history.append(copy.deepcopy(after["reextraction_table_candidates"][name]))
            after.setdefault("reextraction_table_history", {})[name] = history
            # Keep the complete preceding table as the active candidate. An
            # optional read cannot choose, insert, reorder, or erase its rows.
            after["tables"][name] = copy.deepcopy(rows)
            for index, row in enumerate(after["tables"][name]):
                current = proposed[index] if not structural else {}
                for key, cell in row.items():
                    alternative = current.get(key, {})
                    if (structural or cell.get("reextraction_conflict") or cell.get("value") != alternative.get("value")
                            or cell.get("observed") != alternative.get("observed")):
                        cell.update(validity="AMBIGUOUS" if cell.get("observed") is not None else cell["validity"],
                            reextraction_conflict=True, alternatives=list(dict.fromkeys(value for value in [
                                *cell.get("alternatives", []), cell.get("observed"), alternative.get("observed")]
                                if value is not None)))
    after["semantic_conflicts"] = list(dict.fromkeys(conflicts))
    return after


def _invoke(path, body, *, timeout, started, check, minimum_progress=15,
            budget=None, phase_deadline=None, optional=False):
    identity = body["request_id"]
    remaining = timeout - (time.monotonic() - started)
    if budget is not None:
        budget.require()
    if phase_deadline is not None:
        remaining = min(remaining, phase_deadline - time.monotonic())
    if remaining <= 0:
        if optional:
            raise OptionalInferenceTimeout(identity)
        raise DocumentDeadlineExpired()
    check(minimum_progress)  # Fast responses still require current worker authority.
    # Authority callbacks consume time too: publish only the allowance still left.
    remaining = min(remaining, timeout - (time.monotonic() - started))
    if budget is not None:
        remaining = min(remaining, budget.require())
    if phase_deadline is not None:
        remaining = min(remaining, phase_deadline - time.monotonic())
    if remaining <= 0:
        if optional:
            raise OptionalInferenceTimeout(identity)
        raise DocumentDeadlineExpired()
    control = _RequestControl()
    executor = ThreadPoolExecutor(max_workers=1)
    # Ephemeral per-RPC allowance; never alter the frozen configuration/snapshot.
    transport_body = {**body, "request_budget_seconds": remaining}
    future = executor.submit(_request, path, transport_body, timeout=remaining, control=control)
    completed = False
    error = None
    last_phase_poll = 0.0
    phase_progress = minimum_progress
    try:
        while not future.done():
            if budget is not None:
                budget.require()
            if time.monotonic() - last_phase_poll >= 2:
                try:
                    phase = _request("/status/" + identity, timeout=1)
                    if isinstance(phase, dict):
                        phase_name = phase.get("phase")
                        if isinstance(phase_name, str):
                            phase_progress = max(minimum_progress, {"QUEUED": 15, "PADDLE": 25, "QWEN": 65}.get(phase_name, phase_progress))
                except OperationError:
                    pass  # Registration/finish can race the read-only status poll.
                last_phase_poll = time.monotonic()
            check(phase_progress)  # Real service phases; elapsed time never becomes invented progress.
            if phase_deadline is not None and time.monotonic() >= phase_deadline:
                if optional:
                    raise OptionalInferenceTimeout(identity)
                raise DocumentDeadlineExpired()
            if time.monotonic() - started > timeout:
                raise DocumentDeadlineExpired()
            time.sleep(.2)
        if budget is not None:
            budget.require()
        check(phase_progress)
        try:
            response = future.result()
        except RemoteInferenceTimeout:
            if optional:
                raise OptionalInferenceTimeout(identity) from None
            raise
        completed = True
    except (OperationError, DocumentDeadlineExpired, asyncio.CancelledError) as caught:
        error = caught
    finally:
        if not completed:
            try:
                _request("/cancel", {"request_id": identity}, timeout=2)
            except OperationError:
                pass
            control.cancel()
            try:
                future.result(timeout=2)
            except FutureTimeout:
                if isinstance(error, OptionalInferenceTimeout):
                    error = OperationError(503, "DOCUMENT_LOCAL_TRANSPORT_CLEANUP_FAILED", "No se pudo cerrar el transporte IA local de forma verificable.")
            except (OperationError, asyncio.CancelledError):
                completed = future.done()  # Consume the canceled task; preserve the original failure.
        executor.shutdown(wait=False, cancel_futures=True)
    if error is not None:
        raise error
    return response


def _merge_engine(engine, later):
    engine.update({key: value for key, value in later.items() if key not in {
        "phases", "runtime_provenance", "validation", "inference_completion"}})
    if "runtime_provenance" in later:
        engine["runtime_provenance"] = {**engine.get("runtime_provenance", {}), **later["runtime_provenance"]}
    engine.setdefault("phases", {}).update(later.get("phases", {}))


def _failure_details(error, *, stage, source_sha256, initial, latest, history, stages):
    """Private evidence only. A partial candidate never becomes a successful result."""
    diagnostic = {"failed_stage": stage, "source_sha256": source_sha256,
                  "initial_candidate": initial, "last_candidate": latest,
                  "history": history, "completed_stages": stages}
    raw = json.dumps(diagnostic, ensure_ascii=False, sort_keys=True).encode()
    if len(raw) > 8 * 1024**2:
        diagnostic = {"failed_stage": stage, "source_sha256": source_sha256,
            "candidate_evidence_sha256": hashlib.sha256(raw).hexdigest(),
            "candidate_evidence_bytes": len(raw), "retained": False, "reason": "PRIVATE_DIAGNOSTIC_LIMIT",
            "completed_stages": stages}
    error.details = {"local_inference": diagnostic}


def _phase_total(stages, names, phase):
    values = [stage["engine"].get("phases", {}).get(phase) for stage in stages if stage["stage"] in names]
    if not values or any(type(value) not in (int, float) or not math.isfinite(value) or value < 0 for value in values):
        return None
    return round(sum(values), 4)


def _append_history(history, entry):
    history.append(entry)
    if len(json.dumps(history, ensure_ascii=False).encode()) > 8 * 1024**2:
        raise OperationError(422, "DOCUMENT_LOCAL_AUDIT_LIMIT", "El historial de evidencia IA excedió el límite de auditoría local.")


def run_local(source, definition, configuration, *, legacy_runner, check, budget=None):
    prompt_version = configuration.get("prompt_version", LEGACY_PROMPT_VERSION)
    if prompt_version not in SUPPORTED_PROMPTS:
        raise OperationError(422, "DOCUMENT_PROMPT_UNKNOWN", "La versión del prompt no está instalada.")
    configuration = {**configuration, "prompt_version": prompt_version}
    original_check, highest = check, 0

    def report(progress):
        nonlocal highest
        highest = max(highest, progress)
        original_check(highest)  # A later real phase cannot move the UI backwards.

    check = report
    started = time.monotonic()
    timeout = min(1800, max(30, configuration.get("timeout_seconds", 300)))
    budget = budget or AttemptBudget(timeout, started=started)
    native = legacy_runner(source, definition, check=lambda progress: check(int(progress * .15)),
        native_only=True, deadline=budget.extraction_deadline)
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
    response = _invoke("/extract", body, timeout=timeout, started=started, check=check,
        budget=budget, phase_deadline=budget.extraction_deadline)
    if (not isinstance(response, dict) or response.get("source_sha256") != body["source_sha256"]
            or not isinstance(response.get("lines"), list) or not isinstance(response.get("engine"), dict)):
        raise OperationError(422, "DOCUMENT_LOCAL_INFERENCE_CONTRACT", "La inferencia no devolvió evidencia del documento solicitado.")
    result = legacy_runner(source, definition, check=lambda progress: check(35 + int(progress * .15)),
        native_only=True, candidate_lines=response["lines"], deadline=budget.extraction_deadline)
    initial = {"payload": copy.deepcopy(result["payload"]), "engine": copy.deepcopy(response["engine"])}
    history: list[dict] = []
    attempts: list[dict] = []
    stages: list[dict] = [{"stage": "PADDLE_INITIAL", "engine": copy.deepcopy(response["engine"])}]
    semantic_attempts: list[dict] = []
    semantic_used, stage = False, "INTERPRET"
    checkpoint = None
    partial_error = None

    def capture():
        from .document_quality import validate_candidate_contract
        # The source page count comes from the confined native reader, never OCR metadata.
        try:
            pages = source_pages(native["engine"])
        except ValueError:
            return None  # An unverified count cannot support recovery; normal inference still proceeds.
        validate_candidate_contract(result, definition)
        if not usable_candidate(result["payload"]):
            return None
        return {"result": copy.deepcopy(result), "response": copy.deepcopy(response),
            "history": copy.deepcopy(history), "stages": copy.deepcopy(stages),
            "attempts": copy.deepcopy(attempts), "semantic_attempts": copy.deepcopy(semantic_attempts),
            "semantic_used": semantic_used, "pages": pages}

    def interpret_if_needed():
        nonlocal semantic_used, stage
        if not configuration.get("use_llm", True) or not response["lines"] or _candidate_complete(result["payload"], definition):
            return
        stage = "INTERPRET"
        observation_hash = hashlib.sha256(json.dumps(response["lines"], sort_keys=True, ensure_ascii=False).encode()).hexdigest()
        catalog, targets, view = None, None, None
        schema = semantic_schema(definition)
        if prompt_version == PROMPT_VERSION:
            try:
                catalog = build_catalog(response["lines"])
            except ValueError:
                raise OperationError(422, "DOCUMENT_LLM_FRAGMENT_LIMIT", "No se pudo construir el catálogo acotado de evidencia OCR.") from None
            targets = semantic_targets(result["payload"], definition)
            view = prompt_catalog_view(response["lines"], definition, targets)["prompt_view"]
            schema = fragment_schema(definition, catalog, targets,
                allowed_fragment_ids=[item["fragment_id"] for item in view["fragments"]]
                if view["omitted_detail_observation_ids"] else None)
        interpretation = _invoke("/interpret", {"request_id": uuid.uuid4().hex, "source_sha256": body["source_sha256"],
            "configuration": configuration, "definition": definition, "semantic_schema": schema,
            **({"semantic_targets": targets} if targets is not None else {}),
            **({"fragment_prompt_view_sha256": view["sha256"]} if view is not None else {}),
            "observations": response["lines"], "observations_sha256": observation_hash},
            timeout=timeout, started=started, check=check, minimum_progress=50,
            budget=budget, phase_deadline=budget.extraction_deadline, optional=True)
        if (not isinstance(interpretation, dict) or interpretation.get("source_sha256") != body["source_sha256"]
                or interpretation.get("observations_sha256") != observation_hash or "semantics" not in interpretation
                or not isinstance(interpretation.get("engine"), dict)):
            raise OperationError(422, "DOCUMENT_LOCAL_INFERENCE_CONTRACT", "La interpretación no corresponde a la evidencia OCR solicitada.")
        if catalog is not None:
            assert view is not None
            if (interpretation.get("fragment_catalog_sha256") != catalog["sha256"]
                    or interpretation.get("fragment_catalog_version") != catalog["version"]
                    or view["omitted_detail_observation_ids"]
                    and interpretation.get("fragment_prompt_view_sha256") != view["sha256"]):
                raise OperationError(422, "DOCUMENT_LLM_EVIDENCE_INVALID", "El catálogo semántico no coincide con la evidencia OCR original.")
            result["payload"] = apply_fragment_semantics(result["payload"], interpretation["semantics"], definition,
                response["lines"], catalog, targets)
        else:
            result["payload"] = apply_semantics(result["payload"], interpretation["semantics"], definition, response["lines"])
        _merge_engine(response["engine"], interpretation["engine"])
        semantic_attempts.append(copy.deepcopy(interpretation["engine"]))
        stages.append({"stage": "INTERPRET", "engine": copy.deepcopy(interpretation["engine"])})
        _append_history(history, {"stage": "INTERPRET", "attempt": len(semantic_attempts),
            "observations_sha256": observation_hash,
            **{key: copy.deepcopy(result["payload"].get(key)) for key in (
                "fields", "tables", "semantic_candidates", "semantic_conflicts", "reextraction_table_candidates")}})
        semantic_used = True

    try:
        stage = "PADDLE_INITIAL"
        _append_history(history, {"attempt": 0, "stage": "PADDLE_INITIAL", "observations": copy.deepcopy(response["lines"]),
            "fields": copy.deepcopy(result["payload"]["fields"]), "tables": copy.deepcopy(result["payload"]["tables"])})
        if prompt_version == PROMPT_VERSION:
            stage = "PADDLE_LITERAL_TABLES"
            result["payload"] = _literal_candidate(result["payload"], response["lines"], definition)
        checkpoint = capture()
        # Literal observations may already contain all needed values. Interpret
        # them before paying for spotting crops that cannot repair layout rules.
        interpret_if_needed()
        checkpoint = capture()
        for attempt in range(min(2, max(0, int(configuration.get("max_retries", 1))))):
            if _candidate_complete(result["payload"], definition):
                break
            regions = focused_regions(result["payload"], definition, response["lines"])
            if not regions:
                break
            stage = "REFINE"
            _append_history(history, {"attempt": attempt + 1, "stage": "BEFORE_REFINE", "observations": copy.deepcopy(response["lines"]),
                "fields": copy.deepcopy(result["payload"]["fields"]), "tables": copy.deepcopy(result["payload"]["tables"])})
            refined = _invoke("/refine", {**body, "request_id": uuid.uuid4().hex, "regions": regions},
                timeout=timeout, started=started, check=check, minimum_progress=50,
                budget=budget, phase_deadline=budget.extraction_deadline, optional=True)
            if (not isinstance(refined, dict) or refined.get("source_sha256") != body["source_sha256"]
                    or not isinstance(refined.get("lines"), list)):
                raise OperationError(422, "DOCUMENT_LOCAL_INFERENCE_CONTRACT", "La reextracción no corresponde al documento original.")
            response["lines"] = refined_observations(response["lines"], refined["lines"], regions)
            stage = "REMAP"
            remapped = legacy_runner(source, definition, check=lambda _: check(50), native_only=True,
                candidate_lines=response["lines"], deadline=budget.extraction_deadline)
            if prompt_version == PROMPT_VERSION:
                remapped["payload"] = _literal_candidate(remapped["payload"], response["lines"], definition)
            remapped["payload"] = retain_disagreements(result["payload"], remapped["payload"], definition)
            result = remapped
            attempts.append({"attempt": attempt + 1, "regions": regions, "engine": refined.get("engine", {}),
                             "observations": len(refined["lines"])})
            stages.append({"stage": "REFINE", "engine": copy.deepcopy(refined.get("engine", {}))})
            if not refined["lines"]:
                break
            checkpoint = capture()
            interpret_if_needed()
            checkpoint = capture()
    except OptionalInferenceTimeout as error:
        budget.require()  # Global deadline exhaustion and cancellation/authority always win.
        check(highest)
        if checkpoint is None:
            _failure_details(error, stage=stage, source_sha256=body["source_sha256"], initial=initial,
                latest={"payload": result["payload"], "engine": response["engine"]}, history=history, stages=stages)
            raise
        with source.open("rb") as stream:
            before = os.fstat(stream.fileno())
            actual_sha256 = hashlib.file_digest(stream, "sha256").hexdigest()
            after = os.fstat(stream.fileno())
        if (actual_sha256 != body["source_sha256"]
                or (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns)
                != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)):
            raise OperationError(422, "DOCUMENT_LOCAL_INFERENCE_CONTRACT", "El original cambió durante la inferencia.")
        result, response = checkpoint["result"], checkpoint["response"]
        history, stages = checkpoint["history"], checkpoint["stages"]
        attempts, semantic_attempts = checkpoint["attempts"], checkpoint["semantic_attempts"]
        semantic_used = checkpoint["semantic_used"]
        partial_error = {"stage": stage, "request_id": error.request_id, "pages": checkpoint["pages"]}
        _append_history(history, {"stage": stage, "status": "INCOMPLETE", "error_code": error.code,
            "request_id": error.request_id, "candidate_restored_from_validated_checkpoint": True})
    except OperationError as error:
        _failure_details(error, stage=stage, source_sha256=body["source_sha256"], initial=initial,
            latest={"payload": result["payload"], "engine": response["engine"]}, history=history, stages=stages)
        raise
    result["payload"]["inference_observation_history"] = history
    result["engine"].update(response["engine"], key="PADDLE_QWEN_LOCAL_098", route="PADDLE_QWEN" if semantic_used else "PADDLE",
        prompt_version=prompt_version, transport="PRIVATE_LOCAL_HTTP", inference_skipped=False)
    result["engine"]["elapsed_seconds"] = round(time.monotonic() - started, 4)
    result["engine"]["focused_reextraction"] = attempts
    result["engine"]["semantic_attempts"] = semantic_attempts
    result["engine"]["completed_stages"] = stages
    result["engine"].setdefault("phases", {})["paddle_seconds"] = _phase_total(stages, {"PADDLE_INITIAL", "REFINE"}, "paddle_seconds")
    if semantic_used:
        result["engine"]["phases"]["llm_seconds"] = _phase_total(stages, {"INTERPRET"}, "llm_seconds")
    check(100)
    budget.require()
    if partial_error is not None:
        marker = make_marker(payload=result["payload"], stage=partial_error["stage"],
            source_sha256=body["source_sha256"], definition=definition, configuration=configuration,
            pages=partial_error["pages"], completed_stages=stages, request_id=partial_error["request_id"])
        return trusted_partial_result(result, marker)
    return result
