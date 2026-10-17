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


def visual_lines(lines):
    """Join actual words sharing a baseline, including separate PDF text runs."""
    result = []
    for page in sorted({line["page"] for line in lines}):
        for group in row_groups(lines, page, 0, 1):
            tokens = [word for word, _ in group]
            source = group[0][1]
            result.append({"text": " ".join(w["text"] for w in tokens), "page": page,
                           "region": box(tokens), "words": tokens, "method": source["method"],
                           "confidence": source.get("confidence")})
    return result


def field_candidates(lines, rule):
    lines = visual_lines(lines)
    candidates = []
    for line in lines:
        if line["page"] != rule["page"]:
            continue
        found = anchor(line, rule["label"])
        if not found:
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
                source = min(following, key=lambda other: other["region"][1])
                selected = words(source)
        if rule["kind"] == "REGION_CONTEXT":
            # Translate the selected value region with its textual anchor.
            dx = label_box[0] - rule["region"][0]
            selected = [w for w in selected if rule["region"][0] + dx - .02 <= w["region"][0]
                        <= rule["region"][2] + dx + .02]
        raw = " ".join(w["text"] for w in selected).strip(" :=\t")
        if raw:
            candidates.append((raw, origin(source, selected)))
    return candidates


def row_groups(lines, page, start, end):
    tokens = [(word, line) for line in lines if line["page"] == page
              for word in words(line) if start < (word["region"][1] + word["region"][3]) / 2 < end]
    tokens.sort(key=lambda item: ((item[0]["region"][1] + item[0]["region"][3]) / 2, item[0]["region"][0]))
    groups: list[tuple[float, list[tuple[dict, dict]]]] = []
    for word, line in tokens:
        middle = (word["region"][1] + word["region"][3]) / 2
        height = word["region"][3] - word["region"][1]
        if not groups or abs(groups[-1][0] - middle) > max(.004, height * .6):
            groups.append((middle, [(word, line)]))
        else:
            groups[-1][1].append((word, line))
    return [sorted(items, key=lambda item: item[0]["region"][0]) for _, items in groups]


def table_candidates(lines, rule):
    lines = visual_lines(lines)
    rows, rejected = [], []
    labels = rule["header_labels"]
    for line in lines:
        anchors = [anchor(line, label) for label in labels]
        if not all(anchors):
            continue
        positions = [item[2][0] for item in anchors]
        if positions != sorted(positions):
            continue
        # All labels must match. A changed order never becomes a silent remap.
        dx = anchors[0][2][0] - rule["region"][0]
        if abs(dx) > rule.get("tolerance", .12):
            continue
        header_bottom = max(item[2][3] for item in anchors)
        sample_height = rule["region"][3] - rule["region"][1]
        end = min(1, line["region"][1] + sample_height + rule.get("tolerance", .12))
        later_headers = [other["region"][1] for other in lines if other["page"] == line["page"]
                         and other["region"][1] > header_bottom and all(anchor(other, label) for label in labels)]
        if later_headers:
            end = min(end, min(later_headers))
        if rule.get("stop_label"):
            stops = [other["region"][1] for other in lines if other["page"] == line["page"]
                     and other["region"][1] > header_bottom and anchor(other, rule["stop_label"])]
            if stops:
                end = min(end, min(stops))
        bounds = [value + dx for value in rule["column_bounds"]]
        for group in row_groups(lines, line["page"], header_bottom, end):
            columns: list[list[tuple[dict, dict]]] = [[] for _ in labels]
            for word, source in group:
                center = (word["region"][0] + word["region"][2]) / 2
                for index, (left, right) in enumerate(pairwise(bounds)):
                    if left <= center < right:
                        columns[index].append((word, source))
                        break
            if not any(columns):
                continue
            if not all(columns):
                rejected.append({"text": " ".join(w["text"] for w, _ in group),
                                 "page": line["page"], "region": box([w for w, _ in group]), "method": line["method"]})
                continue
            rows.append([(" ".join(w["text"] for w, _ in column), origin(column[0][1], [w for w, _ in column]))
                         for column in columns])
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
