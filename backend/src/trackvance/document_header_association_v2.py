"""Source relations for opt-in V2, retaining rejected and uncertain observations.

Only a positively different caption/role/section excludes an observation from a
field. Type failure by itself is never an exclusion. Geometry remains measured
block geometry or logical HTML adjacency; no cell rectangle is manufactured.
"""
from __future__ import annotations

import copy
import re
from typing import Any

from . import document_header_association as v1
from .document_normalization import normalize_observation
from .document_semantic_vocabulary import (
    MONETARY_ROLES,
    ROLE_CAPTIONS,
    caption_currency_qualifier,
    caption_role,
    role_captions,
    section_role,
)

VERSION = "DECLARED_HEADER_ASSOCIATION_V2"


def _field_role(field: dict[str, Any]) -> str | None:
    name = field.get("name")
    return name if name in ROLE_CAPTIONS else caption_role(field["label"])


def _rules(field: dict[str, Any], *, expanded: bool) -> list[dict[str, Any]]:
    configured = copy.deepcopy(field)
    if expanded and field.get("source_association") != v1.SOURCE_ASSOCIATION:
        configured["aliases"] = role_captions(field)
    return v1._rules(configured)


def _section(page: dict[str, Any], anchor: dict[str, Any]) -> str | None:
    """Observe enclosing party sections, independently of the requested section."""
    candidates: list[tuple[float, str]] = []
    for block in page["blocks"]:
        role = section_role(str(block.get("text", "")))
        region = v1._region(page, block.get("geometry"))
        if (role is not None and region is not None and anchor.get("region") is not None
                and region[1] < anchor["region"][1] and v1._overlap(region, anchor["region"]) >= .5):
            candidates.append((region[1], role))
    logical: list[tuple[float, str]] = []
    if anchor.get("table"):
        for table in page["tables"]:
            if table["id"] != anchor["table"]:
                continue
            owner = next((cell for cell in table["cells"] if cell["id"] == anchor["id"]), None)
            if owner is None:
                continue
            for cell in table["cells"]:
                role = section_role(cell["text"])
                if (role is not None and (cell["tag"] == "th" or cell["colspan"] > 1) and cell["row"] < owner["row"]
                        and cell["column"] <= owner["column"] < cell["column"] + cell["colspan"]):
                    logical.append((float(cell["row"]), role))
    candidates = logical or candidates
    if not candidates:
        return None
    nearest = max(item[0] for item in candidates)
    roles = {role for distance, role in candidates if distance == nearest}
    return next(iter(roles)) if len(roles) == 1 else "AMBIGUOUS"


def _inline_values(line: str, label: str, labels: list[str]) -> list[tuple[str, int, int]]:
    values = []
    matches = v1._matches(line, labels)
    for start, end, found in matches:
        if v1.label_key(found) != v1.label_key(label):
            continue
        finished, declared = v1._label_ending(line, end, found)
        delimiter = re.match(r"\s*[:=]\s*", line[finished:])
        if delimiter is None and not declared:
            continue
        begin = finished + (delimiter.end() if delimiter else 0)
        later = [other_start for other_start, other_end, other_label in matches
            if other_start >= begin and (v1._label_ending(line, other_end, other_label)[1]
                or re.match(r"\s*[:=]", line[v1._label_ending(line, other_end, other_label)[0]:]))]
        finish = min(later) if later else len(line)
        values.append((line[begin:finish].strip(), start, finished))
    return values


def _source_anchors(page):
    anchors = [{"id": block["id"], "text": block["text"], "region": v1._region(page, block.get("geometry"))}
        for block in page["blocks"] if block.get("type") != "table" and isinstance(block.get("text"), str)]
    for table in page["tables"]:
        if table.get("errors"):
            continue
        region = v1._region(page, table.get("query_geometry"))
        anchors.extend({"id": cell["id"], "text": cell["text"], "region": region,
            "table": table["id"], "row": cell["row"], "column": cell["column"],
            "colspan": cell["colspan"]} for cell in table["cells"])
    return anchors


def _anchor_in_scope(anchor, rule, headings):
    window_rule = {key: value for key, value in rule.items() if key != "section_label"}
    if not v1._in_scope(anchor, window_rule, []):
        return "UNCERTAIN" if anchor.get("region") is None else "REJECTED"
    section = rule.get("section_label")
    if not section or v1._in_scope(anchor, rule, headings):
        return "PROVEN"
    logical = [heading for heading in headings if anchor.get("table")
        and heading.get("table") == anchor["table"] and heading["row"] < anchor["row"]
        and heading["column"] <= anchor["column"] < heading["column"] + heading["colspan"]]
    if logical:
        nearest = max(heading["row"] for heading in logical)
        nearest_headings = [heading for heading in logical if heading["row"] == nearest]
    else:
        nearest_headings = [heading for heading in headings if heading.get("region") is not None
            and anchor.get("region") is not None and heading["region"][1] <= anchor["region"][1]
            and v1._overlap(heading["region"], anchor["region"]) >= .5]
        if nearest_headings:
            nearest_y = max(heading["region"][1] for heading in nearest_headings)
            nearest_headings = [heading for heading in nearest_headings if heading["region"][1] == nearest_y]
    if not nearest_headings:
        return "UNCERTAIN"
    matches = [v1.label_key(heading["text"]) == v1.label_key(section)
        or section_role(section) is not None and section_role(heading["text"]) == section_role(section)
        for heading in nearest_headings]
    return "PROVEN" if all(matches) else "REJECTED" if not any(matches) else "UNCERTAIN"


def _explicit_inline_observations(page, rule, labels, headings):
    if rule.get("direction") == "BELOW":
        return
    anchors = _source_anchors(page)
    for anchor in anchors:
        anchor["_scope_status"] = _anchor_in_scope(anchor, rule, headings)
        for line in anchor["text"].splitlines():
            for label in rule["labels"]:
                for raw, _, _ in _inline_values(line, label, labels):
                    yield raw, anchor, anchor, "DECLARED_TABLE_INLINE" if anchor.get("table") else "DECLARED_INLINE_LABEL"


def _suffix_candidates(text: str, field: dict[str, Any]) -> list[tuple[str, str, str]]:
    if field.get("logical_type") != "DECIMAL":
        return []
    wanted = _field_role(field)
    if wanted not in MONETARY_ROLES:
        return []
    all_roles = [label for labels in ROLE_CAPTIONS.values() for label in labels]
    result = []
    for line in text.splitlines():
        if caption_role(line) is not None:
            continue  # e.g. "IVA 19%" is a complete tax caption, not money.
        for start, end, _ in v1._matches(line, role_captions(field)):
            if line[:start].strip():
                continue
            tail = line[end:].strip()
            if not tail or tail.startswith((":", "=")):
                continue
            owners = [caption_role(line[:finish]) for begin, finish, _ in v1._matches(line, all_roles)
                if not line[:begin].strip() and finish > end]
            if any(owner is not None and owner != wanted for owner in owners):
                result.append((tail, "REJECTED", "CAPTION_OWNED_BY_DIFFERENT_ROLE"))
                continue
            _, validity, _ = normalize_observation(tail, field)
            proven = validity == "VALID" and field.get("decimal_format_explicit") is True
            result.append((tail, "PROVEN" if proven else "UNCERTAIN",
                "DECLARED_MONETARY_ROLE_VALUE_SUFFIX" if proven else "MONETARY_SUFFIX_UNRESOLVED"))
    return result


def _suffix_observations(page, rule, labels, headings):
    if rule.get("direction") == "BELOW":
        return
    for anchor in _source_anchors(page):
        anchor["_scope_status"] = _anchor_in_scope(anchor, rule, headings)
        for raw, _, _ in _suffix_candidates(anchor["text"], rule["field"]):
            yield raw, anchor, anchor, "DECLARED_ROLE_VALUE_SUFFIX"


def _short_alias_caption_fragment(text: str, raw: str, field: dict[str, Any]) -> bool:
    """Reject only a captured span that starts inside this same field's caption.

    The original literal and cell reference remain in the audit trace. Neither
    normalization failure nor a different nearby value establishes this fact.
    A real value under another explicit pair in the same cell stays relevant.
    """
    captions = role_captions(field)
    lines = text.splitlines()
    for line in lines:
        if any(value == raw and status != "REJECTED" for value, status, _ in _suffix_candidates(line, field)):
            return False
        for label in captions:
            for value, start, _finished in _inline_values(line, label, captions):
                boundary = max(line.rfind(char, 0, start) for char in "|;") + 1
                if value == raw and not line[boundary:start].strip():
                    return False
    for line in lines:
        for full_start, full_end, full_label in v1._matches(line, captions):
            boundary = max(line.rfind(char, 0, full_start) for char in "|;") + 1
            if line[boundary:full_start].strip():
                continue
            finished, declared = v1._label_ending(line, full_end, full_label)
            delimiter = re.match(r"\s*[:=]\s*", line[finished:])
            if delimiter is None and not declared and not _suffix_candidates(line, field):
                continue
            for short_label in captions:
                for short_start, short_end, _ in v1._matches(line, [short_label]):
                    if short_start != full_start or short_end >= full_end:
                        continue
                    short_finished, _separator = v1._label_ending(line, short_end, short_label)
                    rest = line[short_finished:]
                    short_delimiter = re.match(r"\s*[:=]\s*", rest)
                    begin = short_finished + (short_delimiter.end() if short_delimiter else len(rest) - len(rest.lstrip()))
                    if begin < full_end and raw and line[begin:].startswith(raw):
                        return True
    return False


def _inline_status(text: str, raw: str, labels: list[str], field: dict[str, Any]) -> tuple[str, str]:
    """A delimiter belongs to the complete caption, not a word within it."""
    if _short_alias_caption_fragment(text, raw, field):
        return "REJECTED", "SHORT_ALIAS_CAPTION_FRAGMENT"
    if caption_role(text) is not None and not re.search(r"[:=]", text):
        return "REJECTED", "ANCHOR_IS_ROLE_CAPTION_WITHOUT_VALUE"
    suffix = [item for item in _suffix_candidates(text, field) if item[0] == raw]
    if suffix:
        return suffix[0][1], suffix[0][2]
    wanted = _field_role(field)
    for line in text.splitlines():
        for label in labels:
            for value, start, finished in _inline_values(line, label, labels):
                boundary = max(line.rfind(char, 0, start) for char in "|;") + 1
                caption = line[boundary:finished].strip()
                owner = caption_role(caption)
                if owner is not None and wanted is not None and owner != wanted:
                    return "REJECTED", "CAPTION_OWNED_BY_DIFFERENT_ROLE"
                if not line[boundary:start].strip() and value == raw:
                    return "PROVEN", "WHOLE_DECLARED_INLINE_CAPTION"
    return "UNCERTAIN", "INLINE_CAPTION_RELATION_UNPROVEN"


def _target_owns_value(page: dict[str, Any], anchor: dict[str, Any], value: dict[str, Any], field: dict[str, Any], mechanism: str) -> bool:
    """A word equal to a role is not by itself proof that it owns another value."""
    for table in page["tables"]:
        if table["id"] != value.get("table"):
            continue
        owner = next((cell for cell in table["cells"] if cell["id"] == value["id"]), None)
        if owner is None:
            continue
        if owner["tag"] == "th":
            return True
        # A nearby TD alone proves nothing. Require a consistent caption/value
        # axis in this real grid: the original monetary caption and the other
        # role's caption occupy the same column, with declared valid scalars in
        # the adjacent value column. Invalid neighboring literals stay uncertain.
        previous = next((cell for cell in table["cells"] if cell["id"] == anchor["id"]), None)
        if (mechanism != "DECLARED_TABLE_BELOW" or field.get("logical_type") != "DECIMAL"
                or previous is None or previous["column"] != owner["column"]
                or previous["colspan"] != 1 or owner["colspan"] != 1):
            return False
        right = next((cell for cell in table["cells"] if cell["row"] == owner["row"]
            and cell["column"] == owner["column"] + owner["colspan"]), None)
        previous_right = next((cell for cell in table["cells"] if cell["row"] == previous["row"]
            and cell["column"] == previous["column"] + 1), None)
        return bool(right and previous_right and right["colspan"] == previous_right["colspan"] == 1
            and normalize_observation(right["text"], field)[1] == "VALID"
            and normalize_observation(previous_right["text"], field)[1] == "VALID")
    return False


def _roles_with_competing_table_currencies(pages: list[dict[str, Any]]) -> set[str]:
    currencies: dict[str, set[str]] = {}
    for page in pages:
        if "selection" in page:
            continue
        for table in page["tables"]:
            if table.get("errors"):
                continue
            for cell in table["cells"]:
                qualifier = caption_currency_qualifier(cell["text"])
                role = caption_role(cell["text"]) if qualifier else None
                if qualifier and role is not None and role in MONETARY_ROLES:
                    currencies.setdefault(role, set()).add(qualifier[1])
    return {role for role, codes in currencies.items() if len(codes) > 1}


def _table_observations(page, rule, labels, headings):
    yield from v1._table_observations(page, rule, labels, headings)
    field = rule["field"]
    role = _field_role(field)
    if field["logical_type"] != "DECIMAL" or role not in MONETARY_ROLES or rule.get("direction") == "BELOW":
        return
    keys = {v1.label_key(label) for label in rule["labels"]}
    codes = {str(symbol).upper() for symbol in field.get("currency_symbols", [])}
    qualified = sorted({cell["text"] for table in page["tables"] if not table.get("errors") for cell in table["cells"]
        if (item := caption_currency_qualifier(cell["text"])) and item[1] in codes
        and v1.label_key(item[0]) in keys and caption_role(cell["text"]) == role})
    if qualified:
        observations = v1._table_observations(page, {**rule, "labels": qualified, "direction": "RIGHT"}, labels, headings)
        yield from (item for item in observations if item[3] == "DECLARED_TABLE_RIGHT")


def header_observations_v2(pages: list[dict[str, Any]], fields: list[dict[str, Any]]) -> tuple[dict[str, list[tuple]], dict[str, list[dict[str, Any]]]]:
    """Return mapper tuples plus source-bound positive/rejected relation traces.

    Original rules are also inspected so a rejected V1 relation remains visible.
    The source IR itself is never modified. Unproven relations contribute an
    ambiguous observation; they are not silently removed from candidate selection.
    """
    observations: dict[str, list[tuple]] = {field["name"]: [] for field in fields}
    associations: dict[str, list[dict[str, Any]]] = {field["name"]: [] for field in fields}
    competing_currencies = _roles_with_competing_table_currencies(pages)
    for page in pages:
        if "selection" in page:
            continue  # Model-reported anchors are not independent source facts.
        rule_sets = [{field["name"]: _rules(field, expanded=expanded) for field in fields}
            for expanded in (True, False)]
        seen: set[tuple] = set()
        for rules in rule_sets:
            labels = sorted({label for choices in rules.values() for rule in choices for label in rule["labels"]})
            sections = {v1.label_key(rule["section_label"]) for choices in rules.values()
                for rule in choices if rule.get("section_label")}
            headings = [{"id": block["id"], "text": block["text"], "region": v1._region(page, block.get("geometry"))}
                for block in page["blocks"] if isinstance(block.get("text"), str)
                and (v1.label_key(block["text"]) in sections or section_role(block["text"]) is not None)]
            headings.extend({"id": cell["id"], "text": cell["text"], "region": None, "table": table["id"],
                "row": cell["row"], "column": cell["column"], "colspan": cell["colspan"]}
                for table in page["tables"] for cell in table["cells"] if (cell["tag"] == "th" or cell["colspan"] > 1)
                and (v1.label_key(cell["text"]) in sections or section_role(cell["text"]) is not None))
            for field in fields:
                name = field["name"]
                for rule in rules[name]:
                    if rule.get("page") is not None and rule["page"] != page["page_one_based"]:
                        continue
                    structural_rule = {key: value for key, value in rule.items() if key != "value_pattern"}
                    structural_rule["field"] = field
                    for source in (_explicit_inline_observations, _suffix_observations, v1._block_observations, _table_observations):
                        for raw, anchor, value, mechanism in source(page, structural_rule, labels, headings):
                            identity = (name, raw, anchor["id"], value["id"], mechanism)
                            if identity in seen:
                                continue
                            seen.add(identity)
                            status, reason = "PROVEN", "DECLARED_SOURCE_RELATION"
                            source_section = _section(page, anchor)
                            requested = field.get("section", "DOCUMENT")
                            if anchor.get("_scope_status") == "REJECTED":
                                status, reason = "REJECTED", "DECLARED_SOURCE_SCOPE_MISMATCH"
                            elif source_section == "AMBIGUOUS":
                                status, reason = "UNCERTAIN", "SOURCE_SECTION_AMBIGUOUS"
                            elif requested in {"ISSUER", "BUYER", "TOTALS"} and source_section is not None and source_section != requested:
                                status, reason = "REJECTED", "OBSERVED_SECTION_OWNS_DIFFERENT_ROLE"
                            elif mechanism == "DECLARED_ROLE_VALUE_SUFFIX":
                                _, status, reason = next(item for item in _suffix_candidates(anchor["text"], field) if item[0] == raw)
                            elif "INLINE" in mechanism:
                                status, reason = _inline_status(anchor["text"], raw, rule["labels"], field)
                            elif (mechanism == "DECLARED_TABLE_RIGHT" and _field_role(field) in competing_currencies
                                    and caption_currency_qualifier(anchor["text"])):
                                status, reason = "UNCERTAIN", "DECLARED_SOURCE_SCOPE_UNPROVEN"
                            elif caption_role(value["text"]) is not None:
                                if _target_owns_value(page, anchor, value, field, mechanism):
                                    status, reason = "REJECTED", "TARGET_IS_ROLE_CAPTION"
                                else:
                                    status, reason = "UNCERTAIN", "TARGET_CAPTION_ROLE_UNPROVEN"
                            if status != "REJECTED" and anchor.get("_scope_status") == "UNCERTAIN":
                                status, reason = "UNCERTAIN", "DECLARED_SOURCE_SCOPE_UNPROVEN"
                            captured = v1._capture(raw, rule)
                            if captured is None and status != "REJECTED":
                                status, reason = "UNCERTAIN", "DECLARED_VALUE_PATTERN_UNRESOLVED"
                            eligible = status != "REJECTED"
                            observed = raw if captured is None else captured
                            table = bool(value.get("table"))
                            trace = {"version": VERSION, "rule": mechanism, "reason": reason,
                                "status": status, "eligible": eligible, "eligible_for_field": eligible,
                                "observed": observed, "raw_observed": raw, "label_text": anchor["text"],
                                "value_text": value["text"], "page": page["page_one_based"],
                                "label_refs": [anchor["id"]], "value_refs": [value["id"]],
                                "section_label": rule.get("section_label"), "source_section": source_section,
                                "requested_section": requested, "region": None if table else value.get("region"),
                                "geometry_kind": "UNKNOWN" if table else "MEASURED_BLOCK" if value.get("region") else "UNKNOWN"}
                            if table:
                                trace.update(table_region=value.get("region"), region_semantics="TABLE_LOCATOR_ONLY")
                            associations[name].append(trace)
                            if eligible:
                                item: tuple = (observed, page["page_one_based"], value["id"], status == "UNCERTAIN", [])
                                if item not in observations[name]:
                                    observations[name].append(item)
    return observations, associations
