"""Versioned documentary representation retaining original structure and provenance.

Logical HTML cells have no measured per-cell geometry. A table/query rectangle
remains a table/query rectangle, even when a mapper associates one of its cells.
"""
from __future__ import annotations

import base64
import copy
import hashlib
import json
import re
from html.parser import HTMLParser
from typing import Any

from .document_normalization import canonical_hash

VERSION = "TRACKVANCE_DOCUMENT_IR_V1"
MAX_ARTIFACT_BYTES = 16 * 1024**2


def stable_id(*parts):
    return canonical_hash(parts)[:32]


class TableGrid(HTMLParser):
    """Preserve cell text/spans and distinguish placeholders from empty cells."""
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.tables: list[dict[str, Any]] = []
        self.table: dict[str, Any] | None = None
        self.row: list[dict[str, Any]] | None = None
        self.cell: dict[str, Any] | None = None
        self.depth = 0
        self.errors: list[str] = []

    def handle_starttag(self, tag, attrs):
        attributes = dict(attrs)
        if tag == "table":
            self.depth += 1
            if self.depth == 1:
                self.table = {"rows": [], "cells": [], "errors": []}
                self.tables.append(self.table)
            else:
                self.errors.append("NESTED_TABLE_REQUIRES_REVIEW")
            return
        if self.depth != 1 or self.table is None:
            return
        if tag == "tr":
            self._finish_cell()
            self.row = []
            self.table["rows"].append(self.row)
        elif tag in {"td", "th"}:
            self._finish_cell()
            if self.row is None:
                self.row = []
                self.table["rows"].append(self.row)
            try:
                rowspan, colspan = int(attributes.get("rowspan", "1")), int(attributes.get("colspan", "1"))
                if not 1 <= rowspan <= 1000 or not 1 <= colspan <= 1000:
                    raise ValueError()
            except (ValueError, TypeError):
                rowspan = colspan = 1
                self.table["errors"].append("INVALID_SPAN")
            self.cell = {"tag": tag, "rowspan": rowspan, "colspan": colspan, "parts": []}
        elif tag == "br" and self.cell is not None or tag in {"p", "div"} and self.cell is not None and self.cell["parts"]:
            self.cell["parts"].append("\n")

    def handle_endtag(self, tag):
        if tag == "table":
            if self.depth == 1:
                self._finish_cell()
                self.row = None
            self.depth = max(0, self.depth - 1)
        elif self.depth == 1 and tag in {"td", "th", "tr"}:
            self._finish_cell()
            if tag == "tr":
                self.row = None

    def handle_data(self, data):
        if self.depth == 1 and self.cell is not None:
            self.cell["parts"].append(data)

    def _finish_cell(self):
        if self.cell is None:
            return
        cell, self.cell = self.cell, None
        cell["text"] = "".join(cell.pop("parts"))
        cell["state"] = "EMPTY" if not cell["text"].strip() else "PRESENT"
        if self.row is None:
            raise ValueError("TABLE_CELL_OUTSIDE_ROW")
        self.row.append(cell)


def parse_table_html(html, *, identity="table"):
    if not isinstance(html, str) or len(html.encode()) > MAX_ARTIFACT_BYTES:
        raise ValueError("TABLE_HTML_LIMIT")
    parser = TableGrid()
    parser.feed(html)
    parser.close()
    parser._finish_cell()
    result = []
    for table_index, raw in enumerate(parser.tables):
        occupied: dict[tuple[int, int], str] = {}
        cells, rows = [], []
        table_id = stable_id(identity, table_index, hashlib.sha256(html.encode()).hexdigest())
        if len(raw["rows"]) > 10000:
            raise ValueError("TABLE_ROW_LIMIT")
        for row_index, physical in enumerate(raw["rows"]):
            column = 0
            for source_cell in physical:
                while (row_index, column) in occupied:
                    column += 1
                cell = {**source_cell, "id": stable_id(table_id, row_index, column),
                    "row": row_index, "column": column, "geometry_kind": "UNKNOWN", "geometry": None}
                cells.append(cell)
                if cell["rowspan"] * cell["colspan"] + len(occupied) > 100000:
                    raise ValueError("TABLE_CELL_LIMIT")
                for rr in range(row_index, row_index + cell["rowspan"]):
                    for cc in range(column, column + cell["colspan"]):
                        if (rr, cc) in occupied:
                            raw["errors"].append("OVERLAPPING_SPAN")
                        occupied[(rr, cc)] = cell["id"]
                column += cell["colspan"]
                if column > 1000 or len(occupied) > 100000:
                    raise ValueError("TABLE_CELL_LIMIT")
        width = max((col + 1 for _, col in occupied), default=0)
        height = max(len(raw["rows"]), max((row + 1 for row, _ in occupied), default=0))
        if width * height > 100000:
            raise ValueError("TABLE_GRID_LIMIT")
        owners = {cell["id"]: cell for cell in cells}
        for row in range(height):
            slots = []
            for col in range(width):
                owner = occupied.get((row, col))
                if owner is None:
                    slots.append({"state": "MISSING", "cell_id": None})
                else:
                    first = owners[owner]
                    slots.append({"state": first["state"] if (first["row"], first["column"]) == (row, col) else "SPAN",
                        "cell_id": owner})
            rows.append({"id": stable_id(table_id, "row", row), "ordinal": row,
                "slots": slots})
        result.append({"id": table_id, "original_html": html, "html_sha256": hashlib.sha256(html.encode()).hexdigest(),
            "rows": rows, "cells": cells, "column_count": width,
            "header_row_ids": [rows[i]["id"] for i, row in enumerate(raw["rows"]) if any(c["tag"] == "th" for c in row)],
            "errors": list(dict.fromkeys([*parser.errors, *raw["errors"]])),
            "continuation": {"status": "UNRESOLVED", "previous_table_id": None}})
    return result


def decode_artifact(artifact):
    if not isinstance(artifact, dict) or type(artifact.get("bytes")) is not int or not 0 <= artifact["bytes"] <= MAX_ARTIFACT_BYTES:
        raise ValueError("RECOGNITION_ARTIFACT_LIMIT")
    if not isinstance(artifact.get("content_b64"), str) or len(artifact["content_b64"]) > ((MAX_ARTIFACT_BYTES + 2) // 3) * 4:
        raise ValueError("RECOGNITION_ARTIFACT_LIMIT")
    data = base64.b64decode(artifact["content_b64"], validate=True)
    if len(data) != artifact["bytes"] or hashlib.sha256(data).hexdigest() != artifact["sha256"]:
        raise ValueError("RECOGNITION_ARTIFACT_INTEGRITY")
    return data


def event_artifacts(event, kind):
    return [artifact for artifact in event.get("payload", {}).get("artifacts", []) if artifact["kind"] == kind]


def _selection(value):
    if not isinstance(value, dict) or not isinstance(value.get("fields"), dict) or not isinstance(value.get("tables"), dict):
        raise TypeError("VISUAL_SELECTION_CONTRACT")
    rows = [value["fields"]]
    for table in value["tables"].values():
        if not isinstance(table, list) or len(table) > 10000:
            raise ValueError("VISUAL_SELECTION_CONTRACT")
        rows.extend(table)
    for row in rows:
        if not isinstance(row, dict):
            raise TypeError("VISUAL_SELECTION_CONTRACT")
        for cell in row.values():
            if (not isinstance(cell, dict) or cell.get("observed") is not None and not isinstance(cell.get("observed"), str)
                    or type(cell.get("ambiguous")) is not bool or not isinstance(cell.get("alternatives"), list)
                    or any(not isinstance(item, str) for item in cell["alternatives"])):
                raise ValueError("VISUAL_SELECTION_CONTRACT")
    return value


def representation_from_events(events, *, source_sha256, configuration_sha256, expected_pages=None):
    """Rebuild an IR from durable recognition events without calling any model."""
    pages, stage_pages, terminal, terminals = [], {}, None, []
    definition_sha = events[0].get("definition_sha256") if events else None
    if not isinstance(definition_sha, str) or re.fullmatch(r"[a-f0-9]{64}", definition_sha) is None:
        raise ValueError("RECOGNITION_DEFINITION_BINDING")
    probes = [event for event in events if event.get("kind") == "PHASE" and event.get("stage") == "NATIVE_SOURCE_PROBE"]
    if len(probes) > 1:
        raise ValueError("DUPLICATE_SOURCE_PAGE_PROBE")
    source_probe = None
    if probes:
        probe = probes[0]
        artifacts = probe["payload"].get("artifacts", [])
        if len(artifacts) != 1 or artifacts[0].get("kind") != "NATIVE_SOURCE_PROBE_JSON":
            raise ValueError("SOURCE_PAGE_PROBE_CONTRACT")
        original_probe = json.loads(decode_artifact(artifacts[0]))
        if (not isinstance(original_probe, dict) or not isinstance(original_probe.get("engine"), dict)
                or not isinstance(original_probe.get("payload"), dict)):
            raise ValueError("SOURCE_PAGE_PROBE_CONTRACT")
        count = original_probe["engine"].get("pages")
        count = count if type(count) is int and 1 <= count <= 50 else None
        if probe.get("expected_pages") != count or probe["payload"].get("source_page_count_verified") is not (count is not None):
            raise ValueError("SOURCE_PAGE_PROBE_BINDING")
        if expected_pages is not None and expected_pages != count:
            raise ValueError("SOURCE_PAGE_PROBE_BINDING")
        expected_pages = count
        source_probe = {"event_sha256":probe.get("event_sha256"), "stage":"NATIVE_SOURCE_PROBE", "expected_pages":count,
            "artifact_refs":[{key: artifact[key] for key in ("kind","media_type","sha256","bytes")} for artifact in artifacts],
            "native_incomplete_pages":copy.deepcopy(original_probe["payload"].get("native_incomplete_pages", []))}
    for event in events:
        bound = {key: value for key, value in event.items() if key not in {"event_sha256", "durable_refs"}}
        if event.get("event_sha256") != canonical_hash(bound):
            raise ValueError("RECOGNITION_EVENT_INTEGRITY")
        if event.get("source_sha256") != source_sha256 or event.get("configuration_sha256") != configuration_sha256:
            raise ValueError("RECOGNITION_EVENT_BINDING")
        if event.get("definition_sha256") != definition_sha:
            raise ValueError("RECOGNITION_DEFINITION_BINDING")
        if event["kind"] == "TERMINAL":
            if expected_pages is not None and event.get("expected_pages") != expected_pages:
                raise ValueError("RECOGNITION_PAGE_COUNT_BINDING")
            terminal = copy.deepcopy(event)
            terminals.append(terminal)
            continue
        if event["kind"] != "PAGE":
            continue
        number = event["page_one_based"]
        if type(number) is not int or not 1 <= number <= 50:
            raise ValueError("RECOGNITION_PAGE_RANGE")
        if expected_pages is not None and event["expected_pages"] != expected_pages:
            raise ValueError("RECOGNITION_PAGE_COUNT_BINDING")
        page = {"id": stable_id(source_sha256, event["stage"], number, event.get("event_sha256")),
            "page_one_based": number, "stage": event["stage"], "attempt_id": event["attempt_id"],
            "event_sha256": event.get("event_sha256"), "geometry": copy.deepcopy(event["payload"].get("page_geometry")),
            "recognition_status": event.get("recognition_status", event["payload"].get("recognition_status", "COMPLETE")),
            "image": copy.deepcopy(event["payload"].get("image")), "blocks": [], "tables": [],
            "artifact_refs": [{key: copy.deepcopy(a[key]) for key in ("kind", "media_type", "sha256", "bytes")}
                for a in event["payload"].get("artifacts", [])]}
        for artifact in event["payload"].get("artifacts", []):
            raw_bytes = decode_artifact(artifact)
            if artifact["kind"] == "PADDLE_PAGE_JSON":
                raw = json.loads(raw_bytes)
                if not isinstance(raw, dict):
                    raise ValueError("PADDLE_PAGE_CONTRACT")
                raw = raw.get("res", raw)
                if not isinstance(raw, dict) or not isinstance(raw.get("parsing_res_list", []), list):
                    raise ValueError("PADDLE_PAGE_CONTRACT")
                page["official_page_metadata"] = {key: copy.deepcopy(raw[key]) for key in raw if key != "parsing_res_list"}
                for index, block in enumerate(raw.get("parsing_res_list", [])):
                    if not isinstance(block, dict):
                        raise TypeError("PADDLE_PAGE_CONTRACT")
                    block_id = stable_id(page["id"], index, artifact["sha256"])
                    item = {"id": block_id, "ordinal": index, "raw": copy.deepcopy(block),
                        "type": block.get("block_label"), "text": block.get("block_content"),
                        "available_order": block.get("block_order"), "available_global_id": block.get("block_id"),
                        "geometry": copy.deepcopy(block.get("block_bbox")),
                        "geometry_kind": "MEASURED_BLOCK" if block.get("block_bbox") else "UNKNOWN"}
                    page["blocks"].append(item)
                    if block.get("block_label") == "table" and isinstance(block.get("block_content"), str):
                        for table in parse_table_html(block["block_content"], identity=block_id):
                            table.update(source_block_id=block_id, page_one_based=number,
                                query_geometry=copy.deepcopy(item["geometry"]), geometry_kind="TABLE_REGION")
                            page["tables"].append(table)
            elif artifact["kind"] == "QWEN_SELECTION_JSON":
                page["selection"] = _selection(json.loads(raw_bytes))
            elif artifact["kind"] == "NATIVE_PAGE_JSON":
                page["native"] = json.loads(raw_bytes)
        key = (event["stage"], number)
        if key in stage_pages:
            raise ValueError("DUPLICATE_STAGE_PAGE")
        stage_pages[key] = page["id"]
        pages.append(page)
    return {"version": VERSION, "source_sha256": source_sha256, "configuration_sha256": configuration_sha256,
        "definition_sha256": definition_sha,
        "source_page_count_verified": bool(source_probe and source_probe["expected_pages"] is not None),
        "source_probe": source_probe,
        "expected_pages": expected_pages or (terminal or {}).get("expected_pages"), "pages": pages,
        "terminal": terminal, "terminals": terminals, "geometry_contract": "MEASURED_BLOCK_TABLE_QUERY_AND_UNKNOWN_ARE_DISTINCT",
        "row_identity": "SOURCE_OCCURRENCE_NOT_SKU"}


def validate_representation(value):
    if not isinstance(value, dict) or value.get("version") != VERSION or not isinstance(value.get("pages"), list):
        raise ValueError("DOCUMENT_IR_VERSION")
    ids = [page["id"] for page in value["pages"]]
    if len(ids) != len(set(ids)) or len(ids) > 150:
        raise ValueError("DOCUMENT_IR_PAGE_IDENTITY")
    for page in value["pages"]:
        for table in page["tables"]:
            if any(cell.get("geometry_kind") != "UNKNOWN" or cell.get("geometry") is not None for cell in table["cells"]):
                raise ValueError("DOCUMENT_IR_UNMEASURED_CELL_GEOMETRY")
    return value
