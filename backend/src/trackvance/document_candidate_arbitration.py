"""Worker-owned source arbitration; no recognition, I/O, approval or human claim.

The caller must supply a binding it made from its own real confined control.
Provider-supplied positive flags and array position never establish authority.
Absent complete independent evidence, preserve the original candidate.
"""
from __future__ import annotations

import copy
import hashlib
import json
import math
import re
from decimal import Decimal, DecimalException, Inexact, localcontext
from itertools import pairwise
from typing import Any

from . import document_engine as _engine

TOLERANCE = .003
VERSION = "TRACKVANCE_IDP_INDEPENDENT_CELL_ARBITRATION_098_V1"


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                                    separators=(",", ":"), allow_nan=False).encode()).hexdigest()


def control_binding(independent, source_sha256, definition, configuration_sha256):
    """Only a trusted worker may construct this immediately after its control."""
    return {"source_sha256": source_sha256, "definition_sha256": digest(definition),
            "configuration_sha256": configuration_sha256, "independent_sha256": digest(independent)}


def _box(value):
    return (isinstance(value, list) and len(value) == 4
            and all(type(number) in (int, float) and math.isfinite(number) and 0 <= number <= 1 for number in value)
            and value[0] < value[2] and value[1] < value[3])


def _origin(cell):
    origin = cell.get("origin")
    return (origin if isinstance(origin, dict) and type(origin.get("page")) is int
            and 1 <= origin["page"] <= 50 and _box(origin.get("region"))
            and origin.get("method") in {"OCR", "NATIVE"} and not origin.get("continuation_origins") else None)


def _contains(outer, inner):
    return all((inner[0] >= outer[0] - TOLERANCE, inner[1] >= outer[1] - TOLERANCE,
                inner[2] <= outer[2] + TOLERANCE, inner[3] <= outer[3] + TOLERANCE))


def _compatible(first, second):
    a, b = first["region"], second["region"]
    return (first["page"] == second["page"]
            and max(a[0], b[0]) < min(a[2], b[2]) + TOLERANCE
            and max(a[1], b[1]) < min(a[3], b[3]) + TOLERANCE)


def _words(lines, page):
    result = []
    for line in lines:
        if line.get("page") != page or not _box(line.get("region")):
            continue
        # No synthetic splitting of a layout block into guessed words.
        line_hash = digest(line)
        for number, word in enumerate(line.get("words") or []):
            if (isinstance(word, dict) and isinstance(word.get("text"), str)
                    and _box(word.get("region")) and _contains(line["region"], word["region"])):
                result.append({"text": word["text"], "page": page, "region": word["region"],
                               "line_sha256": line_hash, "word_index": number})
    return result


def _literal_evidence(cell, word_index):
    origin = _origin(cell)
    if origin is None or not isinstance(cell.get("observed"), str) or not cell["observed"]:
        return None
    selected = [word for word in word_index.get(origin["page"], []) if _contains(origin["region"], word["region"])]
    selected.sort(key=lambda word: (word["region"][1], word["region"][0]))
    # Exact scalar source phrase, not substring, fuzzy, Unicode or type rescue.
    if not selected or " ".join(word["text"] for word in selected) != cell["observed"]:
        return None
    return selected


def _row_map(rows, table, declared_row_start):
    fields = table["fields"]
    grammar = (table.get("visual") or {}).get("row_start")
    if (not isinstance(grammar, dict) or type(grammar.get("column")) is not int
            or not 0 <= grammar["column"] < len(fields) or not isinstance(rows, list) or not 1 <= len(rows) <= 10000):
        return None
    key_name = fields[grammar["column"]]["name"]
    names = {field["name"] for field in fields}
    result = {}
    for index, row in enumerate(rows):
        if not isinstance(row, dict) or set(row) != names or any(not isinstance(cell, dict) for cell in row.values()):
            return None
        text = row[key_name].get("observed")
        if not isinstance(text, str):
            return None
        keys = [token for token in re.split(r"[\\\s|]+", text) if declared_row_start(token, grammar)]
        if len(keys) != 1 or keys[0] in result:
            return None
        result[keys[0]] = {"index": index, "row": row}
    return result


def _key_evidence(key, row, key_name, word_index):
    origin = _origin(row[key_name])
    if origin is None:
        return None
    matches = [word for word in word_index.get(origin["page"], [])
               if word["text"] == key and _contains(origin["region"], word["region"])]
    return matches[0] if len(matches) == 1 else None


def _scalar_valid(cell, field, validator):
    if cell.get("reviewed") is not False or cell.get("validity") != "VALID" or cell.get("corrected"):
        return False
    raw = cell.get("observed")
    if not isinstance(raw, str) or not raw or cell.get("source_text_normalization"):
        return False
    value, status = validator(raw, field)
    return status == "VALID" and value == cell.get("value")


def _in_row_column(cell, key, next_key, column, bounds, measured_stop):
    origin = _origin(cell)
    if origin is None or origin["page"] != key["page"]:
        return False
    box = origin["region"]
    end = next_key["region"][1] if next_key and next_key["page"] == key["page"] else measured_stop["region"][1] if measured_stop else None
    if end is None:
        return False
    return (box[0] >= bounds[column] - TOLERANCE and box[2] <= bounds[column + 1] + TOLERANCE
            and box[1] >= key["region"][1] - TOLERANCE and box[3] <= end)


def _measured_stops(keys, lines, visual, stop_matcher):
    result: dict[int, dict[str, Any]] = {}
    if stop_matcher is None:
        return result
    for key in keys:
        page = key["page"]
        last_bottom = max(item["region"][3] for item in keys if item["page"] == page)
        candidates = [line for line in lines if line.get("page") == page and _box(line.get("region"))
                      and line["region"][1] > last_bottom and any(stop_matcher(line, label, visual)
                      for label in [visual.get("stop_label", ""), *visual.get("stop_label_aliases", [])] if label)]
        if candidates:
            measured = min(candidates, key=lambda line: line["region"][1])
            result[page] = {"page": page, "region": copy.deepcopy(measured["region"]), "line_sha256": digest(measured)}
    return result


def _history_matches(history_rows, active_rows):
    if not isinstance(history_rows, list) or len(history_rows) != len(active_rows):
        return False
    mutable = {"validity", "alternatives", "reextraction_conflict"}
    for prior, active in zip(history_rows, active_rows, strict=True):
        if set(prior) != set(active):
            return False
        for name, cell in prior.items():
            current = active[name]
            if ({key: value for key, value in cell.items() if key not in mutable}
                    != {key: value for key, value in current.items() if key not in mutable}):
                return False
            if (current.get("validity") != cell.get("validity")
                    and not (current.get("validity") == "AMBIGUOUS" and current.get("reextraction_conflict") is True)):
                return False
    return True


def _scalar_view(payload, definition, validator):
    """Extra algebraic veto only. Never replace persisted validity/review state."""
    result = copy.deepcopy(payload)
    scopes = [(result["fields"], definition["fields"])]
    scopes.extend((row, table["fields"]) for table in definition.get("tables", [])
                  for row in result["tables"][table["name"]])
    for row, fields in scopes:
        for field in fields:
            cell = row[field["name"]]
            cell["value"], cell["validity"] = validator(cell.get("observed"), field)
    return result


def _check_map(checks):
    output = {}
    for item in checks:
        identity = tuple((key, item.get(key)) for key in ("scope", "operation", "name", "table", "row", "field", "column", "header"))
        if identity in output:
            return None
        output[identity] = item
    return output


def reconcile_candidates(data, independent, definition, *, source_sha256, configuration_sha256,
                         worker_control_binding, validator, declared_row_start, declared_table_stop=None,
                         declared_checks=None, row_algebra_checks=None, check=lambda: None):
    """Return (copy, report). Unknown/incompatible evidence preserves each cell.

    This has no automatic approval or row coverage authority. It retains every
    conflict/history and never modifies a human-reviewed cell or partial marker.
    """
    check()  # Lease/cancellation/deadline callbacks always propagate.
    original = copy.deepcopy(data)
    report: dict[str, Any] = {"version": VERSION, "status": "UNCHANGED", "selected": [], "rejected": [],
              "automatic_approval_authority": False, "coverage_authority": False,
              "human_review_asserted": False}
    expected = control_binding(independent, source_sha256, definition, configuration_sha256)
    if (not re.fullmatch("[a-f0-9]{64}", source_sha256)
            or not re.fullmatch("[a-f0-9]{64}", configuration_sha256)
            or data["engine"].get("configuration_sha256") != configuration_sha256
            or worker_control_binding != expected
            or independent["engine"].get("key") != "POPPLER_TESSERACT_CPU_V1"
            or data["engine"].get("inference_completion")
            or "independent_cell_arbitration" in data["engine"]
            or "independent_cell_arbitration_history" in data["payload"]):
        report["rejected"].append({"scope": "ALL", "reason": "WORKER_BINDING_OR_ELIGIBILITY_UNPROVEN"})
        return original, report
    if sum(len(rows) * len(table["fields"]) for table in definition.get("tables", [])
           for rows in [data["payload"]["tables"].get(table["name"], [])]) > 512:
        report["rejected"].append({"scope": "ALL", "reason": "ARBITRATION_CELL_CAPACITY"})
        return original, report
    word_indexes = [{page: _words(payload["lines"], page) for page in payload["pages"]}
                    for payload in (data["payload"], independent["payload"])]
    if any(sum(map(len, index.values())) > 100000 for index in word_indexes):
        report["rejected"].append({"scope": "ALL", "reason": "ARBITRATION_WORD_CAPACITY"})
        return original, report
    result = copy.deepcopy(data)
    financial = bool(definition.get("arithmetic") or definition.get("cross_totals") or definition.get("invoice_controls"))
    if financial and (declared_checks is None or definition.get("invoice_controls") and row_algebra_checks is None):
        report["rejected"].append({"scope": "ALL", "reason": "DECLARED_NONREGRESSION_CONTROLS_UNAVAILABLE"})
        return original, report
    for table in definition.get("tables", []):
        check()
        name = table["name"]
        history = data["payload"].get("reextraction_table_history", {}).get(name, [])
        if not history or not isinstance(history[-1], dict):
            continue
        previous = data["payload"]["tables"].get(name, [])
        proposed = history[-1].get("after")
        controlled = independent["payload"]["tables"].get(name, [])
        groups = [_row_map(rows, table, declared_row_start) for rows in (previous, proposed, controlled)]
        if any(group is None for group in groups) or not all(list(group) == list(groups[0]) for group in groups[1:]):
            report["rejected"].append({"scope": name, "reason": "SOURCE_ROW_KEY_BIJECTION_UNPROVEN"})
            continue
        # The actual active snapshot, not a substituted history entry, is prior authority.
        if not _history_matches(history[-1].get("before"), previous):
            report["rejected"].append({"scope": name, "reason": "PRIOR_HISTORY_BINDING_CHANGED"})
            continue
        if any(cell.get("reviewed") is not False or cell.get("corrected") for row in previous for cell in row.values()):
            report["rejected"].append({"scope": name, "reason": "HUMAN_REVISION_PRESENT"})
            continue
        visual = table.get("visual") or {}
        bounds = visual.get("column_bounds")
        if (not isinstance(bounds, list) or len(bounds) != len(table["fields"]) + 1
                or any(type(value) not in (int, float) or not math.isfinite(value) for value in bounds)
                or bounds != sorted(set(bounds)) or not 0 <= bounds[0] < bounds[-1] <= 1):
            report["rejected"].append({"scope": name, "reason": "FROZEN_COLUMN_FRAME_UNPROVEN"})
            continue
        key_name = table["fields"][visual["row_start"]["column"]]["name"]
        key_field = table["fields"][visual["row_start"]["column"]]
        if any(not _scalar_valid(entry["row"][key_name], key_field, validator)
               for group in groups[1:] for entry in group.values()):
            report["rejected"].append({"scope": name, "reason": "NEW_OR_CONTROL_ROW_KEY_SCALAR_UNPROVEN"})
            continue
        key_sets = []
        for mapping, index in zip(groups[1:], word_indexes, strict=True):
            key_sets.append([_key_evidence(key, entry["row"], key_name, index) for key, entry in mapping.items()])
        if any(key is None for keys in key_sets for key in keys):
            report["rejected"].append({"scope": name, "reason": "UNIQUE_MEASURED_SOURCE_KEYS_UNPROVEN"})
            continue
        if any(not _compatible(first, second) for first, second in zip(*key_sets, strict=True)):
            report["rejected"].append({"scope": name, "reason": "SOURCE_KEY_GEOMETRY_DISAGREEMENT"})
            continue
        measured_stops = [_measured_stops(keys, payload["lines"], visual, declared_table_stop)
                          for keys, payload in zip(key_sets, (data["payload"], independent["payload"]), strict=True)]
        for keys in key_sets:
            key_column = visual["row_start"]["column"]
            inset = visual["row_start"].get("maximum_start_inset", .035)
            if ([(key["page"], key["region"][1]) for key in keys] != sorted((key["page"], key["region"][1]) for key in keys)
                    or any(a["page"] == b["page"] and a["region"][3] >= b["region"][1] for a, b in pairwise(keys))
                    or any(not -TOLERANCE <= key["region"][0] - bounds[key_column] <= inset + TOLERANCE
                           or key["region"][2] > bounds[key_column + 1] + TOLERANCE for key in keys)):
                report["rejected"].append({"scope": name, "reason": "SOURCE_KEY_PHYSICAL_ORDER_UNPROVEN"})
                break
        else:
            for row_number, key in enumerate(groups[0]):
                check()
                before_row, after_row, control_row = (group[key]["row"] for group in groups)
                prior_key_origin = _origin(before_row[key_name])
                new_key, independent_key = (keys[row_number] for keys in key_sets)
                if (prior_key_origin is None or prior_key_origin["page"] != new_key["page"]
                        or not _contains(prior_key_origin["region"], new_key["region"])
                        or not _contains(prior_key_origin["region"], independent_key["region"])):
                    report["rejected"].append({"scope": name, "row_key_sha256": digest(key), "reason": "PRIOR_SOURCE_ROW_GEOMETRY_UNPROVEN"})
                    continue
                for column, field in enumerate(table["fields"]):
                    check()
                    if field["name"] == key_name:
                        continue  # Never choose a preferred description/key string.
                    old, new, other = (row[field["name"]] for row in (before_row, after_row, control_row))
                    path = f"{name}.{groups[0][key]['index']}.{field['name']}"
                    if old == new:
                        continue
                    evidence = (_literal_evidence(new, word_indexes[0]), _literal_evidence(other, word_indexes[1]))
                    reason = None
                    if not _scalar_valid(new, field, validator) or not _scalar_valid(other, field, validator):
                        reason = "NEW_OR_CONTROL_SCALAR_UNPROVEN"
                    elif new["observed"] != other["observed"] or new["value"] != other["value"]:
                        reason = "INDEPENDENT_EXACT_LITERAL_OR_VALUE_DISAGREEMENT"
                    elif not all(evidence):
                        reason = "INDEPENDENT_LITERAL_SOURCE_EVIDENCE_UNPROVEN"
                    elif not _compatible(_origin(new), _origin(other)):
                        reason = "INDEPENDENT_CELL_GEOMETRY_DISAGREEMENT"
                    else:
                        for number, candidate in enumerate((new, other)):
                            keys = key_sets[number]
                            following = keys[row_number + 1] if row_number + 1 < len(keys) else None
                            if not _in_row_column(candidate, keys[row_number], following, column, bounds,
                                                  measured_stops[number].get(keys[row_number]["page"])):
                                reason = "SOURCE_ROW_OR_COLUMN_GEOMETRY_UNPROVEN"
                                break
                    if reason is None:
                        old_value, old_status = validator(old.get("observed"), field)
                        if (old.get("value") == other["value"]
                                or old_status == "VALID" and old_value == other["value"]):
                            reason = "PRIOR_VALUE_INDEPENDENTLY_CORROBORATED"
                    if reason is not None:
                        report["rejected"].append({"path": path, "reason": reason})
                        continue
                    chosen = copy.deepcopy(new)
                    chosen["reviewed"] = False
                    chosen["independent_selection_version"] = VERSION
                    # Preserve the conflict marker; normal coverage/semantic gates still run.
                    if old.get("reextraction_conflict"):
                        chosen["reextraction_conflict"] = True
                    if old.get("alternatives"):
                        chosen["prior_alternatives"] = copy.deepcopy(old["alternatives"])
                    result["payload"]["tables"][name][groups[0][key]["index"]][field["name"]] = chosen
                    report["selected"].append({"path": path, "row_key_sha256": digest(key), "before": copy.deepcopy(old),
                        "after": copy.deepcopy(new), "control": copy.deepcopy(other), "source_key": copy.deepcopy(new_key),
                        "control_key": copy.deepcopy(independent_key), "source_words": evidence[0], "control_words": evidence[1],
                        "source_page_stop": measured_stops[0].get(new_key["page"]),
                        "control_page_stop": measured_stops[1].get(independent_key["page"])})
    if report["selected"]:
        if declared_checks is not None:
            check()
            actual_before = declared_checks(data["payload"], definition)
            actual_after = declared_checks(result["payload"], definition)
            scalar_before = _scalar_view(data["payload"], definition, validator)
            scalar_after = _scalar_view(result["payload"], definition, validator)
            before_guards = declared_checks(scalar_before, definition)
            after_guards = declared_checks(scalar_after, definition)
            if row_algebra_checks is not None:
                before_guards += row_algebra_checks(scalar_before, definition)
                after_guards += row_algebra_checks(scalar_after, definition)
            before_map, after_map = _check_map(before_guards), _check_map(after_guards)
            regressions = ([] if before_map is not None and after_map is not None else [{"reason": "CHECK_IDENTITY_AMBIGUOUS"}])
            if before_map is not None and after_map is not None:
                regressions.extend({"before": item, "after": after_map.get(identity)} for identity, item in before_map.items()
                                   if item.get("status") == "VALID" and after_map.get(identity, {}).get("status") != "VALID")
            report["declared_checks_before"] = actual_before
            report["declared_checks_after"] = actual_after
            report["nonregression_scalar_view_checks_before"] = before_guards
            report["nonregression_scalar_view_checks_after"] = after_guards
            if regressions:
                report.update(status="UNCHANGED_DECLARED_CONTROL_REGRESSION", proposed_selections=report["selected"],
                              selected=[], regressions=regressions)
                return original, report
            # Persist genuine full checks, including DISCREPANCY/NOT_EVALUATED.
            # Their complete frozen definition was used; scalar guard views
            # never supply approval facts or replace actual persisted states.
            if definition.get("arithmetic") or definition.get("invoice_controls") or "arithmetic" in result["payload"]:
                result["payload"]["arithmetic"] = [item for item in actual_after if item.get("scope") != "CROSS_TOTAL"]
            if definition.get("cross_totals"):
                result["payload"]["cross_totals"] = [{key: value for key, value in item.items() if key != "scope"}
                    for item in actual_after if item.get("scope") == "CROSS_TOTAL"]
        audit_record = {"version": VERSION, "authority": "WORKER_REAL_INDEPENDENT_CONTROL",
                        "source_sha256": source_sha256, "definition_sha256": digest(definition),
                        "configuration_sha256": configuration_sha256, "before_payload_sha256": digest(data["payload"]),
                        "independent_payload_sha256": digest(independent["payload"]),
                        "selected": copy.deepcopy(report["selected"]),
                        "declared_checks_before": copy.deepcopy(report.get("declared_checks_before", [])),
                        "declared_checks_after": copy.deepcopy(report.get("declared_checks_after", [])),
                        "human_review_asserted": False, "automatic_approval_authority": False}
        result["payload"].setdefault("independent_cell_arbitration_history", []).append(audit_record)
        report["history_record_sha256"] = digest(audit_record)
        prior_coverage_claim = result["engine"].get("independent_row_coverage")
        result["engine"]["independent_row_coverage"] = False
        report.update(status="SOURCE_CORROBORATED_CELLS_SELECTED", source_sha256=source_sha256,
                      definition_sha256=digest(definition), configuration_sha256=configuration_sha256,
                      before_payload_sha256=digest(data["payload"]), after_payload_sha256=digest(result["payload"]),
                      independent_payload_sha256=digest(independent["payload"]), independent_engine=copy.deepcopy(independent["engine"]),
                      requires_recertification=True, prior_provider_row_coverage_claim=prior_coverage_claim)
        result["engine"]["independent_cell_arbitration"] = copy.deepcopy(report)
        if len(json.dumps(result, ensure_ascii=False, allow_nan=False).encode()) > 16 * 1024**2:
            report.update(status="UNCHANGED_AUDIT_CAPACITY", selected=[])
            return original, report
    check()
    return result, report


def _declared_checks(payload, definition):
    return [*_engine.arithmetic_checks(payload, definition),
            *({**item, "scope": "CROSS_TOTAL"} for item in _engine.cross_table_checks(payload, definition))]


def _invoice_row_algebra(payload, definition):
    if definition.get("invoice_controls") is None:
        return []
    module = _engine._invoice_rules
    control = module.validated_invoice_controls(definition)
    table = next(table for table in definition["tables"] if table["name"] == control.table)
    types = {field["name"]: field["logical_type"] for field in table["fields"]}
    rates = {Decimal(item.rate) for item in control.headers.tax_totals}
    result = []
    for index, row in enumerate(payload["tables"][control.table]):
        item = {"scope": "DECLARED_INVOICE_ROW_ALGEBRA_ONLY", "operation": "INVOICE_ROW_ALGEBRA",
                "name": control.name + ".line_net", "table": control.table, "row": index,
                "field": control.line_net, "rounding": control.rounding_mode,
                "rounding_scope": control.rounding_scope, "scale": control.scale}
        try:
            with localcontext() as context:
                context.prec = module.PRECISION_LIMIT
                context.traps[Inexact] = True
                values = {name: module._cell(row, name, types[name], control, f"{control.table}.{index}.{name}",
                          name in {control.discount_percent, control.tax_percent}) for name in control.row_names()}
                if values[control.tax_percent] != 0 and values[control.tax_percent] not in rates:
                    raise ValueError("UNSUPPORTED_TAX_RATE")
                gross = module._product([values[control.quantity], values[control.unit_price]], control.scale)
                if control.rounding_scope == "ROW":
                    gross = module._round(gross, control)
                discount = module._product([gross, values[control.discount_percent].scaleb(-2)], control.scale)
                if control.rounding_scope == "ROW":
                    discount = module._round(discount, control)
                expected = module._round(module._sum([gross, -discount], control.scale), control)
                actual, tolerance = values[control.line_net], Decimal(control.tolerance)
                module._guard([expected, actual, tolerance], control.scale)
                item.update(status="VALID" if abs(actual - expected) <= tolerance else "DISCREPANCY",
                            expected=str(expected), observed=str(actual))
        except (module.ArithmeticUnavailable, DecimalException, KeyError, ValueError, TypeError):
            item["status"] = "NOT_EVALUATED"
        result.append(item)
    return result


def reconcile_worker_candidate(data, independent, definition, source_sha256, check):
    """Only certify_candidate calls this, holding its actual validated control."""
    configuration = data["engine"].get("configuration")
    configuration_sha256 = data["engine"].get("configuration_sha256")
    if (data["engine"].get("provider") != "PADDLE_QWEN_LOCAL"
            or not data["payload"].get("reextraction_table_history")
            or data["engine"].get("inference_completion")
            or not isinstance(configuration, dict) or digest(configuration) != configuration_sha256):
        return data, None
    return reconcile_candidates(data, independent, definition, source_sha256=source_sha256,
        configuration_sha256=configuration_sha256,
        worker_control_binding=control_binding(independent, source_sha256, definition, configuration_sha256),
        validator=_engine.validate_value, declared_row_start=_engine._visual_rules.declared_row_start,
        declared_table_stop=_engine._visual_rules.declared_table_stop,
        declared_checks=_declared_checks, row_algebra_checks=_invoice_row_algebra, check=check)
