"""Pure geometry/context rules, also loaded by the confined OCR subprocess.

The caller retains scalar validation and provenance. Coordinates are measured
from the actual OCR/PDF words, never synthesized by dividing line width.
"""
import re
import unicodedata
from bisect import bisect_right
from itertools import pairwise


def normalized(value):
    return " ".join("".join(c for c in unicodedata.normalize("NFKD", value.casefold())
                            if not unicodedata.combining(c)).replace(":", " ").split())


def words(line):
    return line.get("words") or [{"text": line["text"], "region": line["region"],
                                 "confidence": line.get("confidence")}]


def inside(word, region):
    x1, y1, x2, y2 = word["region"]
    return region[0] <= (x1 + x2) / 2 <= region[2] and region[1] <= (y1 + y2) / 2 <= region[3]


def box(items):
    return [min(w["region"][0] for w in items), min(w["region"][1] for w in items),
            max(w["region"][2] for w in items), max(w["region"][3] for w in items)]


def anchor_key(value):
    """Only an optional single terminal abbreviation dot is label punctuation."""
    value = normalized(value)
    return value[:-1] if value.endswith(".") and not value.endswith("..") else value


def anchor_matches(line, label):
    tokens = words(line)
    target = anchor_key(label)
    result: list[tuple[int, int, list[float]]] = []
    if not target:
        return result
    for start in range(len(tokens)):
        for end in range(start + 1, min(len(tokens), start + 24) + 1):
            if anchor_key(" ".join(w["text"] for w in tokens[start:end])) == target:
                result.append((start, end, box(tokens[start:end])))
    return result


def anchor(line, label):
    matches = anchor_matches(line, label)
    return matches[0] if matches else None


def table_header_anchors(line, labels):
    if len({anchor_key(label) for label in labels}) != len(labels):
        return None  # Punctuation cannot distinguish two logical columns.
    matches = [anchor_matches(line, label) for label in labels]
    resolved = []
    for index, candidates in enumerate(matches):
        # "Amount" inside the declared longer "Net amount" cannot also stand
        # for a separate Amount column. Its own distinct occurrence is needed.
        candidates = [candidate for candidate in candidates if not any(
            other[0] <= candidate[0] and candidate[1] <= other[1]
            and other[1] - other[0] > candidate[1] - candidate[0]
            for other_index, group in enumerate(matches) if other_index != index for other in group)]
        if len(candidates) != 1:
            return None
        resolved.append(candidates[0])
    if any(first[1] > second[0] for first, second in pairwise(resolved)):
        return None  # Header spans must be distinct and in declared order.
    return resolved


def origin(line, selected):
    result = {k: line[k] for k in ("page", "method")}
    result["region"] = box(selected)
    confidence = [w.get("confidence") for w in selected if w.get("confidence") is not None]
    result["confidence"] = min(confidence) if confidence else line.get("confidence")
    return result


def visual_lines(lines, baseline_tolerance=.004):
    """Join actual words sharing a baseline, including separate PDF text runs."""
    result = []
    for page in sorted({line["page"] for line in lines}):
        for group in row_groups(lines, page, 0, 1, baseline_tolerance):
            tokens = [word for word, _ in group]
            source = group[0][1]
            result.append({"text": " ".join(w["text"] for w in tokens), "page": page,
                           "region": box(tokens), "words": tokens, "method": source["method"],
                           "confidence": source.get("confidence")})
    return result


def field_candidates(lines, rule):
    lines = visual_lines(lines, rule.get("baseline_tolerance", .004))
    if rule.get("section_bounds"):
        left, right = rule["section_bounds"]
        sliced = []
        for line in lines:
            selected = [word for word in words(line) if left <= (word["region"][0] + word["region"][2]) / 2 <= right]
            if selected:
                sliced.append({**line, "words": selected, "region": box(selected), "text": " ".join(word["text"] for word in selected)})
        lines = sliced
    candidates = []
    for line in lines:
        if line["page"] != rule["page"]:
            continue
        found = next((matched for label in [rule["label"], *rule.get("label_aliases", [])] if (matched := anchor(line, label))), None)
        if not found:
            continue
        if rule.get("section_label"):
            headings = [other for other in lines if other["page"] == line["page"]
                        and other["region"][1] <= line["region"][1] and anchor(other, rule["section_label"])]
            if not headings:
                continue
            heading = max(headings, key=lambda other: other["region"][1])
            party_labels = ["Emisor", "Proveedor", "Vendedor", "Seller", "Adquiriente", "Cliente", "Comprador", "Buyer"]
            competing = [other for other in lines if other["page"] == line["page"]
                         and heading["region"][1] < other["region"][1] <= line["region"][1]
                         and any((matched := anchor(other, label)) and matched[0] == 0 for label in party_labels)
                         and not anchor(other, rule["section_label"])]
            if competing:
                continue
            # Section geometry follows the named heading; another party's NIT
            # cannot be chosen just because it occurs first in reading order.
            if line["region"][1] - heading["region"][1] > .24:
                continue
        _, end, label_box = found
        selected = []
        source = line
        if rule["direction"] == "RIGHT":
            selected = words(line)[end:]
            stop = anchor(line, rule["end_label"]) if rule.get("end_label") else None
            if stop and stop[0] >= end:
                selected = words(line)[end:stop[0]]
        else:
            following = [other for other in lines if other["page"] == line["page"]
                         and other["region"][1] >= label_box[3]
                         and other["region"][1] - label_box[3] <= rule.get("tolerance", .12)
                         and other["region"][2] >= label_box[0]
                         and other["region"][0] <= label_box[2] + .2]
            if following:
                if rule.get("value_pattern"):
                    following = [other for other in following if re.search(rule["value_pattern"], other["text"])]
                if not following:
                    continue
                source = min(following, key=lambda other: other["region"][1])
                selected = words(source)
        if rule["kind"] == "REGION_CONTEXT":
            # Translate the selected value region with its textual anchor.
            dx = label_box[0] - rule["region"][0]
            selected = [w for w in selected if rule["region"][0] + dx - .02 <= w["region"][0]
                        <= rule["region"][2] + dx + .02]
        raw = " ".join(w["text"] for w in selected).strip(" :=\t")
        if rule.get("value_pattern"):
            matched = re.search(rule["value_pattern"], raw)
            if not matched:
                continue
            raw = matched[1].strip()
        if raw:
            candidates.append((raw, origin(source, selected)))
    return candidates


def row_groups(lines, page, start, end, baseline_tolerance=.004):
    tokens = [(word, line) for line in lines if line["page"] == page
              for word in words(line) if start < (word["region"][1] + word["region"][3]) / 2 < end]
    tokens.sort(key=lambda item: ((item[0]["region"][1] + item[0]["region"][3]) / 2, item[0]["region"][0]))
    groups: list[tuple[float, list[tuple[dict, dict]]]] = []
    for word, line in tokens:
        middle = (word["region"][1] + word["region"][3]) / 2
        height = word["region"][3] - word["region"][1]
        if not groups or abs(groups[-1][0] - middle) > max(baseline_tolerance, height * .6):
            groups.append((middle, [(word, line)]))
        else:
            groups[-1][1].append((word, line))
    return [sorted(items, key=lambda item: item[0]["region"][0]) for _, items in groups]


def column_reading_order(column):
    """Keep separate text baselines in reading order inside one table cell."""
    if not column:
        return column
    lines = [{**source, "words": [word]} for word, source in column]
    return [item for group in row_groups(lines, column[0][1]["page"], 0, 1) for item in group]


def flowing_row_groups(lines, page, start, end, bounds, multiline, tolerance):
    """Use other columns as row baselines; retain incomplete and stray groups."""
    def column_number(word):
        center = (word["region"][0] + word["region"][2]) / 2
        return next((i for i, (left, right) in enumerate(pairwise(bounds)) if left <= center < right), None)

    def middle(items):
        return sum((word["region"][1] + word["region"][3]) / 2 for word, _ in items) / len(items)

    classified = []
    for group in row_groups(lines, page, start, end):
        parts: list[list[tuple[dict, dict]]] = [[], [], []]
        for item in group:
            column = column_number(item[0])
            parts[2 if column is None else 1 if column == multiline else 0].append(item)
        classified.append(parts)
    backbones = sorted([(index, parts[0]) for index, parts in enumerate(classified) if parts[0]], key=lambda item: middle(item[1]))
    if not backbones:
        return [group for parts in classified for group in parts if group]
    descriptions = sorted([(middle(parts[1]), index if parts[0] else None) for index, parts in enumerate(classified) if parts[1]],
                          key=lambda item: item[0])
    description_centers = [item[0] for item in descriptions]
    backbone_centers = [middle(items) for _, items in backbones]
    starts: list[float] = []
    for position, (group_index, backbone) in enumerate(backbones):
        center = middle(backbone)
        index = bisect_right(description_centers, center) - 1
        selected = None
        if index >= 0 and description_centers[index] >= center - tolerance and (not starts or description_centers[index] > starts[-1]):
            selected = index
        else:
            index = bisect_right(description_centers, center)
            if index < len(descriptions) and description_centers[index] <= center + .004 and (not starts or description_centers[index] > starts[-1]):
                selected = index
        proposed = description_centers[selected] if selected is not None else center
        if selected is not None:
            owner = descriptions[selected][1]
            neighbors = backbone_centers[max(0, position - 1):position] + backbone_centers[position + 1:position + 2]
            if owner is not None and owner != group_index or any(abs(proposed - neighbor) <= abs(proposed - center) for neighbor in neighbors):
                return None  # A neighboring row has an equal or stronger claim.
        starts.append(proposed)
        if len(starts) > 1 and starts[-1] <= starts[-2]:
            return None  # The geometry cannot attribute these overlapping rows.
    # Each token enters exactly one bucket. The first explicitly holds any
    # fragment before the first attributed row; nothing is discarded.
    result: list[list[tuple[dict, dict]]] = [[] for _ in range(len(backbones) + 1)]
    backbone_buckets = {index: position + 1 for position, (index, _) in enumerate(backbones)}
    for index, parts in enumerate(classified):
        if parts[0]:
            result[backbone_buckets[index]].extend(parts[0])
        for fragment in parts[1:]:
            if fragment:
                result[bisect_right(starts, middle(fragment))].extend(fragment)
    return [group for group in result if group]


def prefix_furniture_words(lines, page, start, end, rule, fields, header_values, validator, dx=0):
    """Positive whole-line provenance, never 'invalid numeric means metadata'."""
    scalar_columns = [index for index, field in enumerate(fields or []) if field.get("logical_type", "STRING") != "STRING"]
    if not scalar_columns or not header_values or validator is None:
        return set()
    values = {" ".join(value.split()) for value in header_values if isinstance(value, str) and any(c.isalpha() for c in value)}
    excluded: set[int] = set()
    bounds = [value + dx for value in rule["column_bounds"]]
    for group in row_groups(lines, page, start, end):
        columns: list[list[dict]] = [[] for _ in rule["header_labels"]]
        for word, _ in group:
            center = (word["region"][0] + word["region"][2]) / 2
            target = next((index for index, (left, right) in enumerate(pairwise(bounds)) if left <= center < right), None)
            if target is not None:
                columns[target].append(word)
        if sum(bool(column) for column in columns) < min(3, len(columns)):
            continue
        if any(columns[index] and validator(" ".join(word["text"] for word in columns[index]), fields[index])[1] == "VALID"
               for index in scalar_columns):
            continue  # Even one valid product scalar keeps the region visible.
        reachable = {0}
        for first in range(len(group)):
            if first not in reachable:
                continue
            for last in range(first + 1, min(len(group), first + 24) + 1):
                text = " ".join(" ".join(word["text"].split()) for word, _ in group[first:last])
                if text in values:
                    reachable.add(last)
        if len(group) in reachable:
            # Every actual token must belong to complete, already valid header
            # values in a nonoverlapping partition. Unioning overlapping spans
            # cannot prove the identity of the whole line.
            excluded.update(id(word) for word, _ in group)
    return excluded


def table_candidates(lines, rule, fields=None, header_values=(), validator=None):
    lines = visual_lines(lines, .012 if rule.get("flowing") else .004)
    rows: list[list[tuple[str, dict]]] = []
    rejected: list[dict] = []
    labels = rule["header_labels"]
    headers = []
    for line in lines:
        anchors = table_header_anchors(line, labels)
        if not anchors:
            continue
        positions = [item[2][0] for item in anchors]
        if positions != sorted(positions):
            continue
        dx = anchors[0][2][0] - rule["region"][0]
        if abs(dx) <= rule.get("tolerance", .12):
            headers.append((line, anchors, dx))
    previous_bounds, continuing = None, False
    for page in sorted({line["page"] for line in lines}):
        page_headers = [h for h in headers if h[0]["page"] == page]
        segments = [(max(a[2][3] for a in anchors), line["region"][1], dx) for line, anchors, dx in page_headers]
        if not segments and rule.get("continue_pages") and continuing and previous_bounds is not None:
            segments = [(0, 0, previous_bounds)]
        if (page_headers or not segments) and rule.get("flowing") and page >= rule["page"] and (page == rule["page"] or rule.get("continue_pages")):
            # OCR may recognize every product but omit a white-on-dark header.
            # Never invent its rows or silently discard that visible region
            # merely because a later page supplies the first usable anchors.
            start = rule["region"][1] if page == rule["page"] else 0
            stop_labels = [label for label in [rule.get("stop_label", ""), *rule.get("stop_label_aliases", [])] if label]
            stops = [line["region"][1] for line in lines if line["page"] == page and line["region"][1] > start
                     and any(anchor(line, label) for label in stop_labels)]
            end = min([line["region"][1] for line, _, _ in page_headers] + stops, default=1)
            groups = row_groups(lines, page, start, end, rule.get("row_tolerance", .018))
            dx = page_headers[0][2] if page_headers else 0
            if page_headers and page > rule["page"]:
                furniture = prefix_furniture_words(lines, page, start, end, rule, fields, header_values, validator, dx)
                groups = [[item for item in group if id(item[0]) not in furniture] for group in groups]
                groups = [group for group in groups if group]
            bounds = [value + dx for value in rule["column_bounds"]]
            occupied = [{i for word, _ in group for i, (left, right) in enumerate(pairwise(bounds))
                         if left <= (word["region"][0] + word["region"][2]) / 2 < right} for group in groups]
            # Require three aligned columns when available: a summary label
            # plus one amount alone then cannot imply a product table. Respect
            # the configured multiline tolerance without reconstructing rows.
            if any(len(columns) >= min(3, len(labels)) for columns in occupied):
                body = [item for group in groups for item in group]
                rejected.append({"text": " ".join(word["text"] for word, _ in body), "page": page,
                                 "region": box([word for word, _ in body]), "method": body[0][1]["method"],
                                 "reason": "TABLE_HEADER_NOT_FOUND"})
        for header_bottom, header_top, dx in segments:
            end = 1 if rule.get("flowing") else min(1, header_top + rule["region"][3] - rule["region"][1] + rule.get("tolerance", .12))
            following_headers = [line["region"][1] for line, _, _ in page_headers if line["region"][1] > header_bottom]
            if following_headers:
                end = min(end, min(following_headers))
            stop_labels = [label for label in [rule.get("stop_label", ""), *rule.get("stop_label_aliases", [])] if label]
            stops = [line["region"][1] for line in lines if line["page"] == page and line["region"][1] > header_bottom
                     and any(anchor(line, label) for label in stop_labels)]
            if stops:
                end = min(end, min(stops))
            continuing = not bool(stops)
            previous_bounds = dx
            bounds = [value + dx for value in rule["column_bounds"]]
            groups = (flowing_row_groups(lines, page, header_bottom, end, bounds, rule["multiline_column"], rule.get("row_tolerance", .018))
                      if rule.get("flowing") and rule.get("multiline_column") is not None
                      else row_groups(lines, page, header_bottom, end, rule.get("row_tolerance", .018) if rule.get("flowing") else .004))
            if groups is None:
                for group in row_groups(lines, page, header_bottom, end):
                    rejected.append({"text": " ".join(word["text"] for word, _ in group), "page": page,
                                     "region": box([word for word, _ in group]), "method": group[0][1]["method"],
                                     "reason": "AMBIGUOUS_ROW_BOUNDARY"})
                continue
            for group in groups:
                columns: list[list[tuple[dict, dict]]] = [[] for _ in labels]
                outside = []
                for word, source in group:
                    center = (word["region"][0] + word["region"][2]) / 2
                    target = next((i for i, (left, right) in enumerate(pairwise(bounds)) if left <= center < right), None)
                    if target is None:
                        outside.append(word)
                    else:
                        columns[target].append((word, source))
                if not any(columns):
                    if rule.get("flowing"):
                        rejected.append({"text": " ".join(w["text"] for w, _ in group), "page": page,
                                         "region": box([w for w, _ in group]), "method": group[0][1]["method"]})
                    continue
                columns = [column_reading_order(column) for column in columns]
                continuation = rule.get("multiline_column")
                if continuation is not None and continuation < len(columns) and columns[continuation] and sum(bool(c) for c in columns) == 1 and rows and not outside:
                    raw, prior = rows[-1][continuation]
                    extra = columns[continuation]
                    merged = dict(prior)
                    if prior["page"] == page:
                        merged["region"] = box([{"region": prior["region"]}, *[word for word, _ in extra]])
                    else:
                        merged["continuation_origins"] = [*prior.get("continuation_origins", []), origin(extra[0][1], [word for word, _ in extra])]
                    rows[-1][continuation] = (raw + " " + " ".join(word["text"] for word, _ in extra), merged)
                    continue
                if not all(columns) or outside and rule.get("flowing"):
                    rejected.append({"text": " ".join(w["text"] for w, _ in group), "page": page,
                                     "region": box([w for w, _ in group]), "method": group[0][1]["method"]})
                    continue
                rows.append([(" ".join(w["text"] for w, _ in column), origin(column[0][1], [w for w, _ in column])) for column in columns])
                if len(rows) > 10000:
                    raise ValueError("DOCUMENT_ROW_LIMIT")
    return rows, rejected


def infer_type(value):
    if re.fullmatch(r"[+-]?[0-9]+[.,][0-9]+", value):
        return "DECIMAL"
    if re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", value):
        return "DATE"
    if re.fullmatch(r"-?(?:0|[1-9][0-9]*)", value) and len(value) < 19:
        return "INT64"
    if value in {"true", "false"}:
        return "BOOLEAN"
    return "STRING"


def field_name(label, fallback="campo"):
    name = re.sub(r"[^a-z0-9]+", "_", normalized(label)).strip("_")[:55]
    return name if name and name[0].isalpha() else fallback
