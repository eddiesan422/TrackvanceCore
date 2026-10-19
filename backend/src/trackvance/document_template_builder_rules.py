"""Pure geometry/context rules, also loaded by the confined OCR subprocess.

The caller retains scalar validation and provenance. Coordinates are measured
from the actual OCR/PDF words, never synthesized by dividing line width.
"""
import re
import unicodedata
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


def anchor(line, label):
    tokens = words(line)
    target = normalized(label)
    for start in range(len(tokens)):
        for end in range(start + 1, min(len(tokens), start + 24) + 1):
            if normalized(" ".join(w["text"] for w in tokens[start:end])) == target:
                return start, end, box(tokens[start:end])
    return None


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


def table_candidates(lines, rule):
    lines = visual_lines(lines, .012 if rule.get("flowing") else .004)
    rows: list[list[tuple[str, dict]]] = []
    rejected: list[dict] = []
    labels = rule["header_labels"]
    headers = []
    for line in lines:
        anchors = [anchor(line, label) for label in labels]
        if not all(anchors):
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
        for header_bottom, header_top, dx in segments:
            end = 1 if rule.get("flowing") else min(1, header_top + rule["region"][3] - rule["region"][1] + rule.get("tolerance", .12))
            following_headers = [line["region"][1] for line, _, _ in page_headers if line["region"][1] > header_bottom]
            if following_headers:
                end = min(end, min(following_headers))
            stops = [line["region"][1] for line in lines if line["page"] == page and line["region"][1] > header_bottom
                     and rule.get("stop_label") and anchor(line, rule["stop_label"])]
            if stops:
                end = min(end, min(stops))
            continuing = not bool(stops)
            previous_bounds = dx
            bounds = [value + dx for value in rule["column_bounds"]]
            for group in row_groups(lines, page, header_bottom, end, rule.get("row_tolerance", .018) if rule.get("flowing") else .004):
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
