"""Opt-in extraction orchestration and replay over original recognition events."""
from __future__ import annotations

import base64
import copy
import hashlib
import json
import re
import unicodedata
import uuid
from decimal import Decimal
from typing import Any

from .document_engine import arithmetic_checks, cross_table_checks, extract
from .document_inference_completion import AttemptBudget
from .document_normalization import canonical_hash, normalize_observation
from .document_representation import representation_from_events, stable_id, validate_representation
from .operations_common import OperationError


def _label(text):
    return re.sub(r"[^a-z0-9]+", " ", "".join(char for char in unicodedata.normalize("NFKD", str(text)).casefold()
        if not unicodedata.combining(char))).strip()


def _origin(page, evidence, *, region=None, geometry_kind="QUERY_REGION"):
    return {"page": page, "region": region or [0, 0, 1, 1], "method": "LOSSLESS_SOURCE",
        "geometry_kind": geometry_kind, "evidence_id": evidence}


def _cell(observed, field, page=None, evidence=None, *, ambiguous=False, alternatives=None):
    refs = [evidence] if evidence else []
    value, validity, normalization = normalize_observation(observed, field, evidence_refs=refs)
    return {"observed": observed, "value": None if ambiguous else value,
        "validity": "AMBIGUOUS" if ambiguous else validity, "confidence": None, "reviewed": False,
        "origin": _origin(page, evidence) if page else None, "geometry_kind": "QUERY_REGION" if page else "UNKNOWN",
        "evidence_refs": refs, "normalization": normalization,
        "candidate_alternatives": copy.deepcopy(alternatives or [])}


def _resolve(observations, field):
    nonempty = [item for item in observations if item[0] is not None]
    if not nonempty:
        return _cell(None, field)
    groups: dict[tuple, list[tuple]] = {}
    for observed, page, evidence, ambiguous, alternatives in nonempty:
        value, validity, _ = normalize_observation(observed, field, evidence_refs=[evidence])
        comparable = Decimal(value) if validity == "VALID" and value is not None and field["logical_type"] == "DECIMAL" else value
        key = (comparable, validity)
        groups.setdefault(key, []).append((observed, page, evidence, ambiguous, alternatives))
    first = nonempty[0]
    ambiguous = len(groups) != 1 or any(item[3] for item in nonempty)
    retained = [{"observed": item[0], "page": item[1], "evidence_refs": [item[2]],
        "ambiguous": item[3], "alternatives": copy.deepcopy(item[4])} for item in nonempty]
    result = _cell(first[0], field, first[1], first[2], ambiguous=ambiguous, alternatives=retained)
    result["evidence_refs"] = [item[2] for item in nonempty]
    result["normalization"]["evidence_refs"] = list(result["evidence_refs"])
    return result


def _table_rows(table):
    owners = {cell["id"]: cell for cell in table["cells"]}
    return [[owners.get(slot["cell_id"]) for slot in row["slots"]] for row in table["rows"]]


def map_representation(representation, definition, *, stage=None):
    """Map direct cells/observations without pipe serialization or SKU deduplication."""
    validate_representation(representation)
    pages = [page for page in representation["pages"] if (stage is None or page["stage"] == stage)
        and page.get("recognition_status") != "INCOMPLETE"]
    payload = {"fields": {}, "tables": {table["name"]: [] for table in definition["tables"]},
        "table_unparsed": {table["name"]: [] for table in definition["tables"]}, "lines": [],
        "pages": sorted({page["page_one_based"] for page in pages}), "row_provenance": {},
        "candidate_history": [], "document_representation": copy.deepcopy(representation), "semantic_conflicts": []}
    payload["structural_issues"] = [{"source_table_id": table["id"], "errors": list(table["errors"])}
        for page in pages for table in page["tables"] if table.get("errors")]
    headers: dict[str, list[tuple]] = {field["name"]: [] for field in definition["fields"]}
    previous_tables: dict[str, dict[str, Any]] = {}
    # Native observations are replayed through the same pure mapper used by its
    # original reader; no process or OCR invocation is involved.
    native = [page for page in pages if "native" in page]
    if native:
        lines = [copy.deepcopy(line) for page in native for line in page["native"].get("lines", [])]
        geometry = {str(page["page_one_based"]): page["native"].get("page_geometry") for page in native
            if page["native"].get("page_geometry")}
        mapped = extract(lines, definition, geometry or None)
        for table in definition["tables"]:
            if table.get("structured_header_labels"):
                mapped["table_unparsed"][table["name"]].append({"reason":"STRUCTURED_SOURCE_REQUIRED",
                    "page": native[0]["page_one_based"], "region":[0,0,1,1], "geometry_kind":"QUERY_REGION"})
        for field in definition["fields"]:
            previous = mapped["fields"][field["name"]]
            value = _cell(previous["observed"], field, (previous.get("origin") or {}).get("page"),
                stable_id("native", field["name"], previous.get("origin")))
            value["origin"] = previous.get("origin")
            value["geometry_kind"] = "MEASURED_SOURCE" if value["origin"] else "UNKNOWN"
            mapped["fields"][field["name"]] = value
        for table in definition["tables"]:
            for index, row in enumerate(mapped["tables"][table["name"]]):
                for field in table["fields"]:
                    previous = row[field["name"]]
                    value = _cell(previous["observed"], field, (previous.get("origin") or {}).get("page"),
                        stable_id("native", table["name"], index, field["name"], previous.get("origin")))
                    value["origin"] = previous.get("origin")
                    value["geometry_kind"] = "MEASURED_SOURCE" if value["origin"] else "UNKNOWN"
                    row[field["name"]] = value
        mapped.update(document_representation=copy.deepcopy(representation), row_provenance={},
            candidate_history=[], semantic_conflicts=mapped.get("semantic_conflicts", []))
        return mapped
    for page in pages:
        ordinal = page["page_one_based"]
        if "selection" in page:
            selection = page["selection"]
            for field in definition["fields"]:
                raw = selection.get("fields", {}).get(field["name"], {})
                headers[field["name"]].append((raw.get("observed"), ordinal,
                    stable_id(page["id"], "field", field["name"]), raw.get("ambiguous", False), raw.get("alternatives", [])))
            for table in definition["tables"]:
                name = table["name"]
                for index, raw_row in enumerate(selection.get("tables", {}).get(name, [])):
                    row_id = stable_id(page["id"], name, index)
                    row = {}
                    for field in table["fields"]:
                        raw = raw_row.get(field["name"], {})
                        row[field["name"]] = _cell(raw.get("observed"), field, ordinal,
                            stable_id(row_id, field["name"]), ambiguous=raw.get("ambiguous", False),
                            alternatives=raw.get("alternatives", []))
                    payload["tables"][name].append(row)
                    payload["row_provenance"].setdefault(name, []).append({"occurrence_id": row_id,
                        "source_page": ordinal, "stage": page["stage"], "geometry_kind": "QUERY_REGION"})
            continue
        strings = []
        for block in page["blocks"]:
            if block["type"] != "table" and isinstance(block["text"], str):
                strings.append((block["text"], block["id"]))
                payload["lines"].append({"text": block["text"], "page": ordinal,
                    "region": [0, 0, 1, 1], "geometry_kind": "QUERY_REGION", "method": "PADDLE_BLOCK", "confidence": None})
        for table in page["tables"]:
            rows = _table_rows(table)
            # Label/value rows remain documentary cells; labels are normalized,
            # observations never are. No field is selected from arithmetic.
            for row in rows:
                if len(row) >= 2 and row[0] and row[1]:
                    strings.append((row[0]["text"] + ": " + row[1]["text"], row[1]["id"]))
            for target in definition["tables"]:
                labels = [_label(value) for value in (target.get("structured_header_labels")
                    or (target.get("visual") or {}).get("header_labels", []))]
                if not labels:
                    labels = [_label(field.get("label") or field["name"]) for field in target["fields"]]
                header = None
                for index, row in enumerate(rows):
                    actual = [_label(cell["text"]) if cell else "" for cell in row]
                    if len(actual) == len(labels) and actual == labels:
                        header = index
                        break
                if header is None:
                    visual = target.get("visual") or {}
                    previous = previous_tables.get(target["name"])
                    grammar = visual.get("row_start")
                    from .document_template_builder_rules import declared_row_start
                    continuation = bool(previous is not None and previous["page"] + 1 == ordinal
                        and visual.get("flowing") and visual.get("continue_pages") and grammar is not None and rows
                        and table["column_count"] == len(target["fields"]))
                    if continuation and grammar is not None:
                        for row in rows:
                            start = row[grammar["column"]] if len(row) > grammar["column"] else None
                            if start is None or not declared_row_start(re.split(r"\s|\\n", start["text"].strip(), maxsplit=1)[0], grammar):
                                continuation = False
                                break
                    if continuation and previous is not None:
                        header = -1
                        relation = {"table": target["name"], "source_table_id": table["id"],
                            "previous_table_id": previous["id"], "rule": "DECLARED_CONTINUATION_AND_ROW_GRAMMAR"}
                        payload.setdefault("table_continuations", []).append(relation)
                        payload["document_representation"].setdefault("continuations", []).append(copy.deepcopy(relation))
                    else:
                        if table["column_count"] == len(target["fields"]):
                            payload["table_unparsed"][target["name"]].append({"page": ordinal, "region": [0,0,1,1],
                                "reason": "UNASSOCIATED_SOURCE_TABLE", "source_table_id": table["id"]})
                        continue
                name = target["name"]
                previous_tables[name] = {"id": table["id"], "page": ordinal}
                for index, row in enumerate(rows[header + 1:], header + 1):
                    actual = [_label(cell["text"]) if cell else "" for cell in row]
                    if actual == labels:
                        payload.setdefault("repeated_headers", []).append({"table": name, "source_row_id": table["rows"][index]["id"],
                            "rule": "EXACT_DECLARED_HEADER_LABELS", "retained_in_document_ir": True})
                        continue
                    if not any(cell and cell["text"].strip() for cell in row):
                        continue
                    if len(row) != len(target["fields"]) or any(cell is None or cell["rowspan"] != 1 or cell["colspan"] != 1 for cell in row):
                        payload["table_unparsed"][name].append({"page": ordinal, "region": [0, 0, 1, 1],
                            "reason": "STRUCTURE_REQUIRES_REVIEW", "source_table_id": table["id"], "source_row": index})
                        continue
                    cells = {field["name"]: _cell(cell["text"], field, ordinal, cell["id"])
                        for cell, field in zip(row, target["fields"], strict=True)}
                    payload["tables"][name].append(cells)
                    payload["row_provenance"].setdefault(name, []).append({"occurrence_id": table["rows"][index]["id"],
                        "source_page": ordinal, "source_table_id": table["id"], "stage": page["stage"], "geometry_kind": "TABLE_REGION"})
        for field in definition["fields"]:
            label = field.get("label")
            if not label:
                continue
            for text, evidence in strings:
                before, separator, after = text.partition(":")
                if separator and _label(before) == _label(label):
                    headers[field["name"]].append((after.strip(), ordinal, evidence, False, []))
    for field in definition["fields"]:
        payload["fields"][field["name"]] = _resolve(headers[field["name"]], field)
    payload["arithmetic"] = arithmetic_checks(payload, definition)
    payload["cross_totals"] = cross_table_checks(payload, definition)
    return payload


def _execution(representation, stage):
    terminals = representation.get("terminals", [representation.get("terminal") or {}])
    terminal: dict[str, Any] = next((item for item in reversed(terminals) if item.get("stage") == stage), {})
    expected = representation.get("expected_pages")
    stage_pages = [page for page in representation["pages"] if page["stage"] == stage]
    pages = sorted({page["page_one_based"] for page in stage_pages})
    completed = sorted({page["page_one_based"] for page in stage_pages if page.get("recognition_status") == "COMPLETE"})
    if terminal.get("stage") == stage and isinstance(terminal.get("completed_pages"), list):
        completed = [page for page in completed if page in terminal["completed_pages"]]
    complete = (representation.get("source_page_count_verified") is True
        and terminal.get("status") == "COMPLETE" and expected and pages == list(range(1, expected + 1))
        and completed == list(range(1, expected + 1))
        and all(page.get("recognition_status") != "INCOMPLETE" for page in representation["pages"] if page["stage"] == stage))
    return {"recognition_complete": bool(complete), "expected_pages": expected, "emitted_pages": pages,
        "completed_pages": completed,
        "pending_pages": [page for page in range(1, (expected or 0) + 1) if page not in completed],
        "resources_released": bool(terminals and all(item.get("resources_released") is True for item in terminals)), "validation_complete": False,
        "publication_complete": False, "stage": stage, "terminal_status": terminal.get("status", "INCOMPLETE")}


def adapt_saved_recognition(events, definition, configuration, *, source_sha256, attempt_context=None):
    if not events:
        raise OperationError(409, "DOCUMENT_RECOGNITION_MISSING", "No hay reconocimiento guardado para reproducir.")
    configuration_sha = events[0]["configuration_sha256"]
    try:
        representation = representation_from_events(events, source_sha256=source_sha256,
            configuration_sha256=configuration_sha, expected_pages=(attempt_context or {}).get("expected_pages"))
    except (ValueError, TypeError, KeyError) as error:
        raise OperationError(422, "DOCUMENT_RECOGNITION_INTEGRITY", "El reconocimiento guardado perdió integridad o excede sus límites.") from error
    stages = list(dict.fromkeys(page["stage"] for page in representation["pages"]))
    if not stages:
        raise OperationError(409, "DOCUMENT_RECOGNITION_MISSING", "El intento no emitió páginas utilizables.")
    stage = next((value for value in stages if value.startswith("QWEN")), stages[0])
    payload = map_representation(representation, definition, stage=stage)
    alternatives = []
    for other in stages:
        candidate = map_representation(representation, definition, stage=other)
        alternatives.append({"stage": other, "payload": candidate, "payload_sha256": canonical_hash(candidate)})
    if len(alternatives) > 1:
        _retain_alternatives(payload, alternatives, definition, stage)
    return {"payload": payload, "engine": {"key": "TRACKVANCE_LOSSLESS_098_V1", "lossless_contract_version": 1,
        "route": stage, "configuration": copy.deepcopy(configuration), "configuration_sha256": canonical_hash(configuration),
        "recognition_configuration_sha256": configuration_sha, "source_sha256": source_sha256,
        "recognition_definition_sha256": representation["definition_sha256"], "definition_sha256": canonical_hash(definition),
        "recognition_event_hashes": [event.get("event_sha256") for event in events],
        "execution": _execution(representation, stage), "replay_without_inference_supported": True,
        "validation_policy": copy.deepcopy(definition.get("validation_policy")), "candidate_stages": stages}}


def _retain_alternatives(payload, candidates, definition, selected):
    payload["candidate_history"] = [{"stage": item["stage"], "payload_sha256": item["payload_sha256"],
        "fields": copy.deepcopy(item["payload"]["fields"]), "tables": copy.deepcopy(item["payload"]["tables"])} for item in candidates]
    for item in candidates:
        if item["stage"] == selected:
            continue
        for field in definition["fields"]:
            first, other = payload["fields"][field["name"]], item["payload"]["fields"][field["name"]]
            if other.get("observed") is not None:
                first["candidate_alternatives"].append({"stage": item["stage"], "candidate": copy.deepcopy(other)})
                left, right = first.get("value"), other.get("value")
                comparable = first.get("validity") == other.get("validity") == "VALID" and left is not None and right is not None
                if comparable and field["logical_type"] == "DECIMAL":
                    left, right = Decimal(left), Decimal(right)
                if comparable and left != right:
                    first.update(value=None, validity="AMBIGUOUS")
                    payload["semantic_conflicts"].append("FIELD:" + field["name"])
        for table in definition["tables"]:
            name = table["name"]
            # Complete table hypotheses are retained. No ordinal/SKU patching or
            # combination search is allowed to construct a plausible total.
            from .document_validation_policy import align_rows
            left, right = payload["tables"][name], item["payload"]["tables"][name]
            if not right:
                continue
            matches, _ = align_rows(left, right, table["fields"])
            if len(left) != len(right) or len(matches) != len(left):
                payload["semantic_conflicts"].append("TABLE:" + name)
                continue
            for index, target in matches.items():
                for field in table["fields"]:
                    first, other = left[index][field["name"]], right[target][field["name"]]
                    first["candidate_alternatives"].append({"stage": item["stage"], "candidate": copy.deepcopy(other)})
                    a, b = first.get("value"), other.get("value")
                    comparable = first.get("validity") == other.get("validity") == "VALID" and a is not None and b is not None
                    if comparable and field["logical_type"] == "DECIMAL":
                        a, b = Decimal(a), Decimal(b)
                    if comparable and a != b:
                        first.update(value=None, validity="AMBIGUOUS")
                        payload["semantic_conflicts"].append(f"CELL:{name}.{index}.{field['name']}")


def _requires_second(payload, definition):
    for field in definition["fields"]:
        cell = payload["fields"][field["name"]]
        if field.get("critical") or field["logical_type"] in {"DECIMAL", "INT64"} or cell["validity"] != "VALID":
            return True
    if any(field.get("critical") or field["logical_type"] in {"DECIMAL", "INT64"}
            for table in definition["tables"] for field in table["fields"]):
        return True
    return bool(any(payload["table_unparsed"].values()) or payload.get("semantic_conflicts")
        or any(cell["validity"] != "VALID" for rows in payload["tables"].values() for row in rows for cell in row.values()))


def _native_events(native, source_sha, configuration_sha, context, *, definition_sha256):
    count = native["engine"].get("pages")
    if type(count) is not int or not 1 <= count <= 50:
        return []
    request_id, attempt_id = uuid.uuid4().hex, context.get("attempt_id", uuid.uuid4().hex)
    result: list[dict[str, Any]] = []
    for number in range(1, count + 1):
        raw = {"lines": [line for line in native["payload"]["lines"] if line["page"] == number],
            "page_geometry": native["payload"].get("page_geometry", {}).get(str(number))}
        values = [("NATIVE_PAGE_JSON", raw)]
        if number == 1:
            values.append(("NATIVE_DOCUMENT_JSON", native))
        artifacts = []
        for kind, value in values:
            data = json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False).encode()
            artifacts.append({"kind": kind, "media_type": "application/json", "sha256": hashlib.sha256(data).hexdigest(),
                "bytes": len(data), "content_b64": base64.b64encode(data).decode()})
        event = {"version": 1, "kind": "PAGE", "sequence": len(result), "request_id": request_id,
            "attempt_id": attempt_id, "source_sha256": source_sha, "configuration_sha256": configuration_sha,
            "definition_sha256": definition_sha256,
            "stage": "DIGITAL_NATIVE", "page_one_based": number, "expected_pages": count,
            "payload": {"artifacts": artifacts, "page_geometry": raw["page_geometry"], "engine": native["engine"]}}
        event["event_sha256"] = canonical_hash(event)
        result.append(event)
    terminal = {"version": 1, "kind": "TERMINAL", "sequence": len(result), "request_id": request_id,
        "attempt_id": attempt_id, "source_sha256": source_sha, "configuration_sha256": configuration_sha,
        "definition_sha256": definition_sha256,
        "stage": "DIGITAL_NATIVE", "status": "COMPLETE", "expected_pages": count,
        "emitted_pages": list(range(1, count + 1)), "completed_pages": list(range(1, count + 1)),
        "pending_pages": [], "resources_released": True, "payload": {}}
    terminal["event_sha256"] = canonical_hash(terminal)
    result.append(terminal)
    return result


def run_lossless(source, definition, configuration, *, legacy_runner, check, budget, on_event=None,
                 attempt_context=None, recognize=None):
    budget = budget or AttemptBudget(configuration.get("timeout_seconds", 300))
    context = dict(attempt_context or {})
    context.setdefault("attempt_id", uuid.uuid4().hex)
    reference_discovery = context.get("purpose") == "REFERENCE_DISCOVERY"
    events = []
    with source.open("rb") as stream:
        source_sha = hashlib.file_digest(stream, "sha256").hexdigest()
    configuration_sha = canonical_hash(configuration)

    def completed(data):
        if reference_discovery:
            data["engine"].update(purpose="REFERENCE_DISCOVERY", effective_route=data["engine"]["route"])
        return data

    def accept(event):
        reference = None
        if on_event is not None:
            reference = on_event(event)
            if reference is not None:
                event["durable_refs"] = reference
        events.append(copy.deepcopy(event))
        return reference

    from .document_local_inference import _sufficient
    native = legacy_runner(source, definition, check=check, native_only=True, deadline=budget.extraction_deadline)
    count = native["engine"].get("pages")
    source_count = count if type(count) is int and 1 <= count <= 50 else None
    raw_probe = json.dumps(native, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    probe = {"version":1, "kind":"PHASE", "sequence":1, "request_id":uuid.uuid4().hex,
        "attempt_id":context["attempt_id"], "source_sha256":source_sha, "configuration_sha256":configuration_sha,
        "definition_sha256":canonical_hash(definition), "stage":"NATIVE_SOURCE_PROBE", "page_one_based":None,
        "expected_pages":source_count, "payload":{"artifacts":[{"kind":"NATIVE_SOURCE_PROBE_JSON",
            "media_type":"application/json", "sha256":hashlib.sha256(raw_probe).hexdigest(), "bytes":len(raw_probe),
            "content_b64":base64.b64encode(raw_probe).decode("ascii")}], "source_page_count_verified":source_count is not None}}
    probe["event_sha256"] = canonical_hash(probe)
    accept(probe)
    native_complete = (type(count) is int and 1 <= count <= 50
        and native["payload"].get("pages") == list(range(1, count + 1)))
    if native_complete and _sufficient(native["payload"], definition):
        for event in _native_events(native, source_sha, configuration_sha, context, definition_sha256=canonical_hash(definition)):
            accept(event)
        if events:
            return completed(adapt_saved_recognition(events, definition, configuration, source_sha256=source_sha, attempt_context=context))
    context["expected_pages"] = source_count
    if recognize is None:
        from .document_recognition_transport import recognize_stream
        recognize = recognize_stream
    strategy = configuration.get("extraction_strategy", "NATIVE_FIRST")
    first = "PADDLE_STRUCTURED" if reference_discovery or strategy == "PADDLE_STRUCTURED" else "QWEN_VISUAL"
    recognized_before = len(events)
    response = recognize(source, definition, configuration, strategy=first, check=check,
        budget=budget, on_event=accept, attempt_context=context)
    # A transport may invoke callbacks incrementally or return the same immutable
    # list for replay. Never persist duplicate callbacks a second time.
    if len(events) == recognized_before:
        for event in [*response.get("pages", []), response.get("terminal")]:
            if event:
                accept(event)
    result = adapt_saved_recognition(events, definition, configuration, source_sha256=source_sha, attempt_context=context)
    if strategy == "SELECTIVE" and not reference_discovery and _requires_second(result["payload"], definition):
        terminal = response.get("terminal") or {}
        if terminal.get("resources_released") is True and budget.require(budget.extraction_deadline) > 10:
            recognize(source, definition, configuration, strategy="PADDLE_STRUCTURED", check=check,
                budget=budget, on_event=accept, attempt_context=context)
            result = adapt_saved_recognition(events, definition, configuration, source_sha256=source_sha, attempt_context=context)
        else:
            result["engine"]["secondary_route_blocked"] = "PRIOR_RESOURCES_OR_REMAINING_BUDGET_UNVERIFIED"
    return completed(result)
