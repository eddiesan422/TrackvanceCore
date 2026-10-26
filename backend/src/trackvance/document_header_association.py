"""Bind declared header labels to documentary relations without guessing values.

Block rectangles are measured block evidence. HTML cells retain logical adjacency
and unknown cell geometry; a logical association never invents a measured cell box.
"""
from __future__ import annotations

import re
import unicodedata

VERSION = "DECLARED_HEADER_ASSOCIATION_V1"


def label_key(value):
    return " ".join("".join(char for char in unicodedata.normalize("NFKD", str(value)).casefold()
        if not unicodedata.combining(char) and (char.isalnum() or char.isspace())).split())


def _tokens(text):
    return [(label_key(match.group()), match.start(), match.end())
        for match in re.finditer(r"\w+", text, re.UNICODE)]


def _matches(text, labels):
    tokens, result = _tokens(text), []
    for label in labels:
        wanted = [value for value, _, _ in _tokens(label)]
        if not wanted:
            continue
        for index in range(len(tokens) - len(wanted) + 1):
            if [item[0] for item in tokens[index:index + len(wanted)]] == wanted:
                result.append((tokens[index][1], tokens[index + len(wanted) - 1][2], label))
    # A short label which prefixes another declared label is not a second anchor.
    ordered = sorted(set(result), key=lambda item: (item[0], -(item[1] - item[0])))
    return [item for index, item in enumerate(ordered)
        if not any(other[0] <= item[0] and item[1] <= other[1] for other in ordered[:index])]


def _rules(field):
    declared = [value for value in [field.get("visual"), *field.get("visual_alternatives", [])] if value]
    if not declared:
        declared = [{}]
    return [{**rule, "labels": list(dict.fromkeys(str(label) for label in [rule.get("label"),
        field.get("label"), *field.get("aliases", []), *rule.get("label_aliases", [])] if label))}
        for rule in declared]


def _region(page, region):
    if not isinstance(region, (list, tuple)) or len(region) != 4 or any(not isinstance(v, (float, int)) for v in region):
        return None
    values = list(region)
    geometry = page.get("geometry") or {}
    width, height = geometry.get("width"), geometry.get("height")
    if width and height and geometry.get("unit") == "PIXEL":
        values = [values[0] / width, values[1] / height, values[2] / width, values[3] / height]
    if not (0 <= values[0] < values[2] <= 1 and 0 <= values[1] < values[3] <= 1):
        return None
    return values


def _overlap(a, b):
    return max(0, min(a[2], b[2]) - max(a[0], b[0])) / min(a[2] - a[0], b[2] - b[0])


def _in_scope(item, rule, headings):
    region = item.get("region")
    window = rule.get("region")
    if window and (region is None or min(region[2], window[2]) <= max(region[0], window[0])
            or min(region[3], window[3]) <= max(region[1], window[1])):
        return False
    bounds = rule.get("section_bounds")
    if bounds and (region is None or not bounds[0] <= (region[0] + region[2]) / 2 <= bounds[1]):
        return False
    section = rule.get("section_label")
    if not section:
        return True
    if region is None:
        return False
    relevant = [heading for heading in headings if heading.get("region") is not None
        and heading["region"][1] <= region[1] and _overlap(heading["region"], region) >= .5]
    if not relevant:
        return False
    bottom = max(heading["region"][1] for heading in relevant)
    nearest = [heading for heading in relevant if heading["region"][1] == bottom]
    return bool(nearest) and all(label_key(heading["text"]) == label_key(section) for heading in nearest)


def _capture(value, rule):
    pattern = rule.get("value_pattern")
    if not pattern:
        return value
    matched = re.search(pattern, value)
    return matched[1].strip() if matched and matched.lastindex else None


def _label_ending(text, end, label):
    """Consume only punctuation explicitly printed as part of this label."""
    tokens = _tokens(label)
    suffix = label[tokens[-1][2]:] if tokens else ""
    literal = "".join(re.escape(char) + r"\s*" for char in suffix if not char.isspace())
    matched = re.match(r"\s*" + literal, text[end:]) if literal else None
    consumed = text[end:end + matched.end()] if matched else ""
    return end + (matched.end() if matched else 0), any(char in ":=" for char in consumed)


def _inline(text, label, all_labels):
    if label_key(text) == label_key(label):
        return []  # A caption alone is not an observation of its trailing % or ).
    matches = _matches(text, all_labels)
    result = []
    for start, end, observed_label in matches:
        if label_key(observed_label) != label_key(label):
            continue
        end, declared_separator = _label_ending(text, end, observed_label)
        rest = text[end:]
        delimiter = re.match(r"\s*[:=]\s*", rest)
        # Embedded prose is not a label/value relation. A leading declared label
        # may omit the delimiter, e.g. a measured block "Reference ABC".
        if start and delimiter is None and not declared_separator:
            continue
        value_start = end + (delimiter.end() if delimiter else len(rest) - len(rest.lstrip()))
        later = []
        for other_start, other_end, other_label in matches:
            if other_start >= value_start:
                finished, separated = _label_ending(text, other_end, other_label)
                if separated or re.match(r"\s*[:=]", text[finished:]):
                    later.append(other_start)
        finish = min(later) if later else len(text)
        value = text[value_start:finish].strip()
        if value:
            result.append(value)
    return result


def _label_block(text, labels):
    return any(label_key(line) in {label_key(label) for label in labels}
        or any(start == 0 and re.match(r"\s*[:=]", line[end:]) for start, end, _ in _matches(line, labels))
        for line in text.splitlines())


def _block_observations(page, rule, all_labels, headings):
    label_keys = {label_key(label) for label in all_labels}
    blocks = [{"text": item["text"], "id": item["id"], "region": _region(page, item.get("geometry")),
        "is_label": _label_block(item["text"], all_labels)}
        for item in page["blocks"] if item.get("type") != "table" and isinstance(item.get("text"), str)]
    for anchor in blocks:
        if not _in_scope(anchor, rule, headings):
            continue
        lines = anchor["text"].splitlines()
        for index, line in enumerate(lines):
            for label in rule["labels"]:
                exact = label_key(line) == label_key(label)
                found = _matches(line, [label])
                if not exact and not found:
                    continue
                if rule.get("direction") != "BELOW":
                    for value in _inline(line, label, all_labels):
                        yield value, anchor, anchor, "DECLARED_INLINE_LABEL"
                if exact and index + 1 < len(lines) and label_key(lines[index + 1]) not in label_keys:
                    yield lines[index + 1].strip(), anchor, anchor, "DECLARED_BLOCK_LINE_BELOW"
                region = anchor["region"]
                if region is None or not exact and rule.get("direction") != "BELOW":
                    continue
                if any(_label_block(other, all_labels) for other in lines[index + 1:]):
                    continue
                choices = []
                for value in blocks:
                    target = value["region"]
                    if value is anchor or target is None or not _in_scope(value, rule, headings):
                        continue
                    if value["is_label"] or _capture(value["text"].strip(), rule) is None:
                        continue
                    direction = rule.get("direction")
                    if direction != "RIGHT":
                        gap = target[1] - region[3]
                        limit = rule.get("tolerance", min(.06, max(.02, 3 * (region[3] - region[1]))))
                        barrier = any(other is not anchor and other is not value and other["region"] is not None
                            and region[3] <= other["region"][1] < target[1] and _overlap(region, other["region"]) >= .5
                            and other["is_label"] for other in blocks)
                        if 0 <= gap <= limit and _overlap(region, target) >= .5 and not barrier:
                            choices.append((gap, value, "DECLARED_MEASURED_BELOW"))
                    if direction != "BELOW":
                        gap = target[0] - region[2]
                        barrier = any(other is not anchor and other is not value and other["region"] is not None
                            and region[2] <= other["region"][0] < target[0]
                            and abs(other["region"][1] - region[1]) <= (region[3] - region[1]) / 2
                            and other["is_label"] for other in blocks)
                        if 0 <= gap <= rule.get("tolerance", .06) and abs(target[1] - region[1]) <= min(
                                target[3] - target[1], region[3] - region[1]) / 2 and not barrier:
                            choices.append((gap, value, "DECLARED_MEASURED_RIGHT"))
                if choices:
                    nearest = {reason: min(item[0] for item in choices if item[2] == reason)
                        for reason in {item[2] for item in choices}}
                    for gap, value, reason in choices:
                        if abs(gap - nearest[reason]) < 1e-9:
                            yield value["text"].strip(), anchor, value, reason


def _table_observations(page, rule, all_labels, headings):
    for table in page["tables"]:
        if table.get("errors"):
            continue
        region = _region(page, table.get("query_geometry"))
        scoped = {"region": region}
        if not _in_scope(scoped, {key: value for key, value in rule.items() if key != "section_label"}, []):
            continue
        external_scope = _in_scope(scoped, rule, headings)
        if not external_scope and not rule.get("section_label"):
            continue
        owners = {cell["id"]: cell for cell in table["cells"]}
        rows = [[owners.get(slot["cell_id"]) for slot in row["slots"]] for row in table["rows"]]
        for row_index, row in enumerate(rows):
            for column, cell in enumerate(row):
                if cell is None or cell["row"] != row_index or cell["column"] != column:
                    continue
                if not external_scope:
                    logical = [heading for heading in headings if heading.get("table") == table["id"]
                        and heading["row"] < row_index and heading["column"] <= column < heading["column"] + heading["colspan"]]
                    nearest = max((heading["row"] for heading in logical), default=-1)
                    if (not logical or any(label_key(heading["text"]) != label_key(rule["section_label"])
                            for heading in logical if heading["row"] == nearest)):
                        continue
                anchor = {"id": cell["id"], "text": cell["text"], "region": region, "table": table["id"]}
                for label in rule["labels"]:
                    if rule.get("direction") != "BELOW":
                        for line in cell["text"].splitlines():
                            for value in _inline(line, label, all_labels):
                                yield value, anchor, anchor, "DECLARED_TABLE_INLINE"
                    if label_key(cell["text"]) != label_key(label) or cell["rowspan"] != 1 or cell["colspan"] != 1:
                        continue
                    targets = []
                    if rule.get("direction") != "BELOW" and column + 1 < len(row):
                        targets.append((row[column + 1], "DECLARED_TABLE_RIGHT"))
                    if rule.get("direction") != "RIGHT" and row_index + 1 < len(rows):
                        targets.append((rows[row_index + 1][column], "DECLARED_TABLE_BELOW"))
                    accepted = []
                    for target, reason in targets:
                        if (target is None or target["id"] == cell["id"] or target["rowspan"] != 1
                                or target["colspan"] != 1 or target["tag"] == "th" or _label_block(target["text"], all_labels)):
                            continue
                        accepted.append((target, reason))
                    # When both directions are plausible the retained alternatives
                    # expose ambiguity; a declared direction can disambiguate it.
                    for target, reason in accepted:
                        value = {"id": target["id"], "text": target["text"], "region": region, "table": table["id"]}
                        yield target["text"].strip(), anchor, value, reason


def header_observations(pages, fields):
    """Return legacy observation tuples and auditable declared source relations."""
    rules = {field["name"]: _rules(field) for field in fields}
    # Preserve original strings for token offsets, normalized keys only for membership.
    original_labels = {label for choices in rules.values() for rule in choices for label in rule["labels"]}
    sections = {label_key(rule["section_label"]) for choices in rules.values() for rule in choices if rule.get("section_label")}
    observations, associations = {field["name"]: [] for field in fields}, {field["name"]: [] for field in fields}
    for page in pages:
        if "selection" in page:
            continue
        headings = [{"id": block["id"], "text": block["text"], "region": _region(page, block.get("geometry"))}
            for block in page["blocks"] if isinstance(block.get("text"), str) and label_key(block["text"]) in sections]
        headings.extend({"id": cell["id"], "text": cell["text"], "region": None, "table": table["id"],
            "row": cell["row"], "column": cell["column"], "colspan": cell["colspan"]}
            for table in page["tables"] for cell in table["cells"] if label_key(cell["text"]) in sections)
        for name, choices in rules.items():
            seen = set()
            for rule in choices:
                if rule.get("page") is not None and rule["page"] != page["page_one_based"]:
                    continue
                for source in (_block_observations, _table_observations):
                    for raw, anchor, value, reason in source(page, rule, original_labels, headings):
                        observed = _capture(raw, rule)
                        if observed is None:
                            continue
                        identity = (observed, anchor["id"], value["id"])
                        if identity in seen:
                            continue
                        seen.add(identity)
                        observations[name].append((observed, page["page_one_based"], value["id"], False, []))
                        associations[name].append({"version": VERSION, "rule": reason, "page": page["page_one_based"],
                            "label_refs": [anchor["id"]], "value_refs": [value["id"]],
                            "section_label": rule.get("section_label"), "region": value["region"],
                            "geometry_kind": "TABLE_REGION" if value.get("table") and value["region"]
                                else "MEASURED_BLOCK" if value["region"] else "UNKNOWN"})
    return observations, associations
