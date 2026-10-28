"""Coordinator validation of independently persisted recognition mechanisms.

Recognition stage names, provider completion flags and row counts alone have no
approval authority. Controls are remapped from the retained IR, then bound to
the raw format, configured mechanism and persisted events of the same source.
"""
from __future__ import annotations

from decimal import Decimal
from typing import Any

from .document_normalization import normalization_matches, normalize_observation

EVIDENCE_VERSION = "TRACKVANCE_DOCUMENT_EVIDENCE_098_V3"


def _normalized(cell, field):
    from .document_observation_uncertainty import unresolved_uncertainty

    if (not isinstance(cell, dict) or unresolved_uncertainty(cell)
            or cell.get("validity") != "VALID" or not normalization_matches(cell, field)):
        return None
    value, status, _ = normalize_observation(cell.get("observed"), field)
    if status != "VALID" or value is None:
        return None
    if field["logical_type"] == "DECIMAL":
        return Decimal(value)
    if field["logical_type"] == "INT64":
        return int(value)
    return value


def _family(pages):
    families = set()
    for page in pages:
        kinds = {item["kind"] for item in page.get("artifact_refs", [])}
        present = {family for family, marker in (("QWEN", "QWEN_SELECTION_JSON"),
            ("PADDLE", "PADDLE_PAGE_JSON"), ("NATIVE", "NATIVE_PAGE_JSON")) if marker in kinds}
        if len(present) != 1:
            return None
        families.update(present)
    return next(iter(families)) if len(families) == 1 else None


def _bound_stage(data, stage, source_context, definition):
    """Authenticate coordinator metadata against IR and durable store context."""
    from .document_lossless import _execution

    engine = data["engine"]
    representation = data["payload"]["document_representation"]
    pages = [page for page in representation["pages"] if page["stage"] == stage]
    binding = engine.get("recognition_sources", {}).get(stage)
    if not pages or not isinstance(binding, dict):
        return None
    family = _family(pages)
    config = engine.get("configuration", {})
    reusable = representation.get("recognition_bindings", {})
    reused = reusable.get(stage)
    expected_hash = engine.get("recognition_configuration_sha256")
    if reused is not None:
        from .document_recognition_compatibility import validate_reuse_snapshot

        try:
            validate_reuse_snapshot(reused, source_sha256=engine.get("source_sha256"),
                configuration=config, definition=definition)
        except (KeyError, TypeError, ValueError):
            return None
        if family != reused.get("mechanism_family"):
            return None
        if source_context is not None and (source_context.get("recognition_bindings") != reusable
                or source_context.get("recognition_binding_event_hash") not in source_context.get("persisted_event_hashes", [])):
            return None
        config = reused["configuration"]
        expected_hash = reused["configuration_sha256"]
    expected_model = {"QWEN": config.get("llm_model"), "PADDLE": config.get("paddle_model"),
        "NATIVE": "PDF_NATIVE"}.get(family)
    page_configurations = {page.get("configuration_sha256", representation.get("configuration_sha256")) for page in pages}
    if (not expected_model or family != binding.get("mechanism_family")
            or binding.get("model") != expected_model
            or binding.get("source_sha256") != engine.get("source_sha256")
            or binding.get("source_sha256") != representation.get("source_sha256")
            or binding.get("configuration_sha256") != expected_hash
            or page_configurations != {expected_hash}):
        return None
    execution = _execution(representation, stage)
    if (not execution["recognition_complete"] or not execution["resources_released"]
            or binding.get("completed_pages") != execution["completed_pages"]
            or binding.get("emitted_pages") != execution["emitted_pages"]
            or binding.get("resources_released") is not True
            or binding.get("terminal_status") != "COMPLETE"):
        return None
    terminals = [item for item in representation.get("terminals", []) if item.get("stage") == stage]
    expected_events = {page["event_sha256"] for page in pages} | {item["event_sha256"] for item in terminals}
    if (not terminals or set(binding.get("event_hashes", [])) != expected_events
            or set(binding.get("request_ids", [])) != {item["request_id"] for item in terminals}):
        return None
    if source_context is not None:
        durable_artifacts = {(item.get("stage"), item.get("page", item.get("page_one_based")),
            item.get("kind"), item.get("sha256")) for item in source_context.get("artifacts", [])}
        needed = {(stage, page["page_one_based"], item["kind"], item["sha256"])
            for page in pages for item in page.get("artifact_refs", [])}
        if (source_context.get("source_sha256") != binding["source_sha256"]
                or source_context.get("expected_pages") != representation.get("expected_pages")
                or source_context.get("resources_released") is not True
                or not expected_events <= set(source_context.get("persisted_event_hashes", []))
                or not needed or not needed <= durable_artifacts):
            return None
    return {"stage": stage, "mechanism": family, "model": expected_model,
        "event_hashes": sorted(expected_events), "completed_pages": execution["completed_pages"],
        "source_sha256": binding["source_sha256"], "configuration_sha256": expected_hash,
        "durable_store_verified": source_context is not None}


def _source_signature(cell):
    if not isinstance(cell, dict):
        return None
    return (cell.get("observed"), cell.get("value"), cell.get("origin"), cell.get("evidence_refs"))


def _at(payload, path):
    scope, occurrence, name = path.split(".")
    if scope == "headers":
        return payload.get("fields", {}).get(name)
    rows = payload.get("tables", {}).get(scope, [])
    return rows[int(occurrence)].get(name) if int(occurrence) < len(rows) else None


def _selected_source(cell, path, data, controls):
    selection = cell.get("candidate_selection") or {}
    stage = selection.get("selected_stage") or data["engine"].get("route")
    source = next((item for item in controls if item["identity"]["stage"] == stage), None)
    source_path = path
    identity = selection.get("identity_basis")
    if stage != data["engine"].get("route") and isinstance(identity, list) and not path.startswith("headers."):
        occurrence = next((item.get("alternative_occurrence") for item in identity if item.get("stage") == stage), None)
        if type(occurrence) is not int or occurrence < 0:
            return None
        scope, _old_index, name = path.split(".")
        source_path = f"{scope}.{occurrence}.{name}"
    if (source is None or selection.get("selected_mechanism") not in {None, source["identity"]["mechanism"]}
            or _source_signature(cell) != _source_signature(_at(source["payload"], source_path))):
        return None
    return source


def _located(cell, source):
    return bool(isinstance(cell, dict) and cell.get("evidence_refs")
        and (cell.get("origin") or {}).get("page") in source["identity"]["completed_pages"])


def _inventory(source, table, definition, representation):
    """Account for every retained source row, continuation and relevant table."""
    from .document_lossless import _label, _table_rows

    name = table["name"]
    mapped = source["payload"]
    identity = source["identity"]
    reasons, entries = [], []
    if mapped.get("structural_issues") or mapped.get("table_unparsed", {}).get(name):
        reasons.append("UNPARSED_OR_INVALID_SOURCE_STRUCTURE")
    stage_pages = [p for p in representation["pages"] if p["stage"] == identity["stage"]]
    rows = mapped.get("tables", {}).get(name, [])
    provenance = mapped.get("row_provenance", {}).get(name, [])
    if identity["mechanism"] == "NATIVE":
        visual = table.get("visual") or {}
        if not (visual.get("flowing") and visual.get("row_start")):
            reasons.append("NATIVE_ROW_GRAMMAR_COVERAGE_NOT_DECLARED")
        return {"covered": not reasons, "reasons": reasons, "pages": identity["completed_pages"],
            "source_tables": [], "row_count": len(rows), "basis": "CONFINED_NATIVE_ROW_GRAMMAR"}
    occurrence_ids = [item.get("occurrence_id") for item in provenance]
    if (len(provenance) != len(rows) or not all(occurrence_ids)
            or len(set(occurrence_ids)) != len(occurrence_ids)):
        reasons.append("SOURCE_OCCURRENCE_INVENTORY_INVALID")
    if identity["mechanism"] == "QWEN":
        count = sum(len(page.get("selection", {}).get("tables", {}).get(name, [])) for page in stage_pages)
        if count != len(rows):
            reasons.append("VISUAL_SOURCE_OCCURRENCES_NOT_ALL_MAPPED")
        return {"covered": not reasons, "reasons": reasons, "pages": identity["completed_pages"],
            "source_tables": [], "row_count": len(rows), "basis": "VISUAL_OCCURRENCES_REQUIRE_STRUCTURAL_CONTROL"}
    labels = [_label(value) for value in (table.get("structured_header_labels")
        or (table.get("visual") or {}).get("header_labels", [])
        or [field.get("label") or field["name"] for field in table["fields"]])]
    header_labels = {_label(field.get("label") or field["name"]) for field in definition["fields"]}
    continuations = {item["source_table_id"]: item for item in mapped.get("table_continuations", [])
        if item.get("table") == name}
    used_tables = {item.get("source_table_id") for item in provenance}
    known_tables = {item["id"]: item for page in stage_pages for item in page.get("tables", [])}
    for page in stage_pages:
        for raw in page.get("tables", []):
            physical = _table_rows(raw)
            normalized_rows = [[_label(cell["text"]) if cell else "" for cell in row] for row in physical]
            header = next((index for index, row in enumerate(normalized_rows) if row == labels), None)
            used = raw["id"] in used_tables
            if not used:
                words = {text for row in normalized_rows for text in row if text}
                body_present = any(index > header and row != labels and any(row)
                    for index, row in enumerate(normalized_rows)) if header is not None else True
                if header is not None and not body_present and not raw.get("errors"):
                    disposition = "DECLARED_EMPTY_DETAIL_TABLE"
                elif header is not None or set(labels) & words:
                    reasons.append("RELEVANT_SOURCE_TABLE_UNASSOCIATED:" + raw["id"])
                    disposition = "UNASSOCIATED_RELEVANT_TABLE"
                elif words & header_labels:
                    disposition = "DECLARED_HEADER_FIELD_TABLE"
                else:
                    reasons.append("SOURCE_TABLE_SCOPE_UNRESOLVED:" + raw["id"])
                    disposition = "UNRESOLVED_TABLE_SCOPE"
                entries.append({"source_table_id": raw["id"], "page": page["page_one_based"], "disposition": disposition})
                continue
            if raw.get("errors"):
                reasons.append("SOURCE_TABLE_GRID_ERRORS:" + raw["id"])
            if header is None:
                link = continuations.get(raw["id"], {})
                previous = known_tables.get(link.get("previous_table_id"), {})
                if (link.get("rule") != "DECLARED_CONTINUATION_AND_ROW_GRAMMAR"
                        or previous.get("page_one_based", 0) + 1 != page["page_one_based"]):
                    reasons.append("SOURCE_CONTINUATION_UNPROVEN:" + raw["id"])
                header = -1
            expected_rows = {raw["rows"][index]["id"] for index, row in enumerate(physical)
                if index > header and normalized_rows[index] != labels
                and any(cell and cell["text"].strip() for cell in row)}
            retained_rows = {item["occurrence_id"] for item in provenance if item.get("source_table_id") == raw["id"]}
            if expected_rows != retained_rows:
                reasons.append("SOURCE_BODY_OCCURRENCES_UNACCOUNTED:" + raw["id"])
            entries.append({"source_table_id": raw["id"], "page": page["page_one_based"],
                "disposition": "DECLARED_DETAIL_TABLE", "source_body_rows": len(expected_rows),
                "mapped_occurrences": len(retained_rows), "continuation": raw["id"] in continuations})
    if used_tables - set(known_tables):
        reasons.append("MAPPED_SOURCE_TABLE_NOT_IN_RETAINED_IR")
    return {"covered": not reasons, "reasons": sorted(set(reasons)), "pages": identity["completed_pages"],
        "source_tables": entries, "row_count": len(rows), "basis": "RETAINED_STRUCTURAL_GRID_AND_ALL_BODY_OCCURRENCES"}


def corroboration_v3(data, definition, *, source_context=None, independent=None):
    """Reusable planning summary; final attestation must supply durable context.

    ``independent`` legacy objects have no V3 authority without persisted source
    events. They remain supported only by the separate historical V2 reader.
    """
    from .document_lossless import map_representation
    from .document_validation_policy import align_rows, cells

    del independent
    payload = data["payload"]
    representation = payload["document_representation"]
    controls = []
    integrity_issues = []
    for stage in dict.fromkeys(page["stage"] for page in representation["pages"]):
        identity = _bound_stage(data, stage, source_context, definition)
        if identity is not None:
            mapped = map_representation(representation, definition, stage=stage)
            for retained in payload.get("candidate_history", []):
                if retained.get("stage") == stage and any(retained.get(key, {}) != mapped.get(key, {})
                        for key in ("fields", "tables", "row_provenance", "table_unparsed")):
                    integrity_issues.append("QUALITY_RETAINED_CANDIDATE_INTEGRITY:" + stage)
            if not mapped.get("structural_issues"):
                controls.append({"identity": identity, "payload": mapped})
    paths, bound_paths, structure, inventories, absent = set(), set(), {}, {}, {}
    alignment: dict[str, list[dict[str, Any]]] = {}
    for path, cell, field in cells(payload, definition):
        value = _normalized(cell, field)
        selected = _selected_source(cell, path, data, controls) if isinstance(cell, dict) else None
        if selected is not None and cell.get("validity") == "VALID" and normalization_matches(cell, field):
            bound_paths.add(path)
        if value is None or selected is None:
            continue
        for other in controls:
            control = _at(other["payload"], path) if path.startswith("headers.") else None
            if (control is not None and other["identity"]["mechanism"] != selected["identity"]["mechanism"]
                    and _located(control, other) and _normalized(control, field) == value):
                paths.add(path)
    primary = next((item for item in controls if item["identity"]["stage"] == data["engine"].get("route")), None)
    for table in definition["tables"]:
        name, fields = table["name"], table["fields"]
        rows = payload.get("tables", {}).get(name, [])
        structure[name] = False
        inventories[name] = {item["identity"]["stage"]: _inventory(item, table, definition, representation) for item in controls}
        empty_controls = [item for item in controls if not item["payload"].get("tables", {}).get(name)
            and inventories[name][item["identity"]["stage"]]["covered"]]
        absent[name] = bool(not rows and (any(item["identity"]["mechanism"] == "NATIVE" for item in empty_controls)
            or {item["identity"]["mechanism"] for item in empty_controls} >= {"PADDLE", "QWEN"}))
        for other in controls:
            other_rows = other["payload"].get("tables", {}).get(name, [])
            matches, bases = align_rows(rows, other_rows, fields)
            evidence = []
            for index, target in sorted(matches.items()):
                supported = []
                for field in fields:
                    path = f"{name}.{index}.{field['name']}"
                    cell = rows[index].get(field["name"])
                    selected = _selected_source(cell, path, data, controls) if isinstance(cell, dict) else None
                    control = other_rows[target].get(field["name"])
                    if (selected is not None and selected["identity"]["mechanism"] != other["identity"]["mechanism"]
                            and _located(control, other) and _normalized(cell, field) is not None
                            and _normalized(cell, field) == _normalized(control, field)):
                        paths.add(path)
                        supported.append(field["name"])
                evidence.append({"source_occurrence": index, "control_occurrence": target, "basis": bases[index],
                    "control_stage": other["identity"]["stage"], "independent_fields": supported})
            if evidence:
                alignment.setdefault(name, []).extend(evidence)
            if (primary is not None and rows and len(rows) == len(other_rows) == len(matches)
                    and all(len(item["independent_fields"]) >= 2 for item in evidence)
                    and inventories[name][primary["identity"]["stage"]]["covered"]
                    and inventories[name][other["identity"]["stage"]]["covered"]
                    and {primary["identity"]["mechanism"], other["identity"]["mechanism"]} >= {"QWEN", "PADDLE"}):
                structure[name] = True
    return {"paths": paths, "structure": structure, "row_alignment": alignment,
        "controls": [item["identity"] for item in controls], "table_inventory": inventories,
        "optional_absence": absent, "source_bound_paths": bound_paths,
        "integrity_issues": sorted(set(integrity_issues))}
