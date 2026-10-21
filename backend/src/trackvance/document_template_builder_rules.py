"""Pure geometry/context rules, also loaded by the confined OCR subprocess.

The caller retains scalar validation and provenance. Coordinates are measured
from the actual OCR/PDF words, never synthesized by dividing line width.
"""
import math
import re
import unicodedata
from bisect import bisect_left, bisect_right
from itertools import pairwise
from statistics import median


def valid_page_geometry(value, pages):
    """Optional dimensions are measured source metadata, never assumed layout."""
    if not isinstance(value, dict) or not 1 <= len(value) <= 50:
        return False
    for page, item in value.items():
        if (not isinstance(page, str) or not 1 <= len(page) <= 2 or not page.isascii() or not page.isdecimal()
            or not 1 <= int(page) <= 50 or str(int(page)) != page
            or not isinstance(item, dict) or set(item) != {"width", "height", "unit"}
            or not isinstance(item["unit"], str) or item["unit"] not in {"PDF_POINT", "PIXEL"}):
            return False
        width, height = item["width"], item["height"]
        if any(isinstance(size, bool) or not isinstance(size, (int, float))
               or isinstance(size, float) and not math.isfinite(size) or size <= 0
               for size in (width, height)):
            return False
        if item["unit"] == "PDF_POINT" and max(width, height) > 14400:
            return False
        if item["unit"] == "PIXEL" and (not isinstance(width, int) or not isinstance(height, int)
                                        or width * height > 20_000_000):
            return False
        if not (0 < width / height < math.inf and 0 < height / width < math.inf):
            return False  # Both physical coordinate scales must be representable.
    return {str(page) for page in pages} <= set(value)


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


def table_anchor_matches(line, label):
    """A full caption can continue vertically within the same horizontal band."""
    matches = anchor_matches(line, label)
    vertical: set[tuple[int, int]] = set()
    target = anchor_key(label)
    parts = normalized(label).split()
    if len(parts) < 2:
        return matches, vertical
    bag = sorted(anchor_key(part) for part in parts)
    tokens = words(line)
    existing = {(item[0], item[1]) for item in matches}
    for start in range(len(tokens)):
        for end in range(start + 1, min(len(tokens), start + min(24, len(parts))) + 1):
            if (start, end) in existing:
                continue
            selected = tokens[start:end]
            observed = normalized(" ".join(word["text"] for word in selected)).split()
            if sorted(anchor_key(part) for part in observed) != bag:
                continue
            rows = row_groups([{"page": line["page"], "words": selected}], line["page"], 0, 1)
            if len(rows) < 2:
                continue  # A different horizontal word order is not this label.
            left = max(min(word["region"][0] for word, _ in row) for row in rows)
            right = min(max(word["region"][2] for word, _ in row) for row in rows)
            if left >= right:
                continue  # Separate columns do not form a vertical caption.
            ordered = [word for row in rows for word, _ in row]
            if anchor_key(" ".join(word["text"] for word in ordered)) != target:
                continue
            matches.append((start, end, box(selected)))
            vertical.add((start, end))
    return matches, vertical


def table_header_anchors(line, labels):
    if len({anchor_key(label) for label in labels}) != len(labels):
        return None  # Punctuation cannot distinguish two logical columns.
    observed = [table_anchor_matches(line, label) for label in labels]
    matches = [item[0] for item in observed]
    filtered = []
    for index, candidates in enumerate(matches):
        # "Amount" inside the declared longer "Net amount" cannot also stand
        # for a separate Amount column. Its own distinct occurrence is needed.
        candidates = [candidate for candidate in candidates if not any(
            other[0] <= candidate[0] and candidate[1] <= other[1]
            and other[1] - other[0] > candidate[1] - candidate[0]
            for other_index, group in enumerate(matches) if other_index != index for other in group)]
        filtered.append(candidates)
    if any(item[1] for item in observed):
        # A horizontal scan can attach the next column's suffix to this one.
        # Accept only a unique complete, ordered partition of the full labels.
        paths: dict[tuple[int, int], tuple[int, list[tuple[int, int, list[float]]]]] = {(-1, -1): (1, [])}
        for candidates in filtered:
            following: dict[tuple[int, int], tuple[int, list[tuple[int, int, list[float]]]]] = {}
            for candidate in candidates:
                compatible = [(count, path) for (_, end), (count, path) in paths.items()
                              if end <= candidate[0]]
                count = min(2, sum(item[0] for item in compatible))
                if count:
                    following[(candidate[0], candidate[1])] = (
                        count, compatible[0][1] + [candidate] if count == 1 else [])
            paths = following
        if sum(item[0] for item in paths.values()) != 1:
            return None
        resolved = next(iter(paths.values()))[1]
        if not any((item[0], item[1]) in observed[index][1] for index, item in enumerate(resolved)) \
                and any(len(candidates) != 1 for candidates in filtered):
            return None  # An unused vertical alternative cannot waive legacy ambiguity.
    else:
        if any(len(candidates) != 1 for candidates in filtered):
            return None
        resolved = [candidates[0] for candidates in filtered]
    if any(first[1] > second[0] for first, second in pairwise(resolved)):
        return None  # Header spans must be distinct and in declared order.
    if any((item[0], item[1]) in observed[index][1] for index, item in enumerate(resolved)):
        covered = {index for start, end, _ in resolved for index in range(start, end)}
        if covered != set(range(len(words(line)))):
            return None  # Never erase an extra STRING while extending a header.
    return resolved


def header_neighborhoods(lines, rule, source_lines, legacy_headers):
    """Alternative full headers only; body baseline grouping stays unchanged."""
    labels = rule["header_labels"]
    budget = sum(len(normalized(label).split()) for label in labels)
    if not budget:
        return []
    legacy_words = {id(word) for line, _, _ in legacy_headers for word in words(line)}
    candidates = {}
    for page in sorted({line["page"] for line in lines}):
        population = [word for line in source_lines if line["page"] == page for word in words(line)]
        tops = sorted(word["region"][1] for word in population)
        bottoms = sorted(word["region"][3] for word in population)
        observed = sorted((line for line in lines if line["page"] == page), key=lambda line: line["region"][1])
        for start in range(len(observed)):
            selected, count = [], 0
            for stop in range(start, min(len(observed), start + budget)):
                selected.extend(words(observed[stop]))
                count += sum(len(normalized(word["text"]).split()) for word in words(observed[stop]))
                if count > budget:
                    break
                region = box(selected)
                height = max(word["region"][3] - word["region"][1] for word in selected)
                limit = min(rule["region"][3] - rule["region"][1], 4 * height, .06)
                if region[3] - region[1] > limit:
                    break
                if count != budget or stop == start:
                    continue
                # Inclusive interval counts cover the complete page in O(log N),
                # including a glyph whose visual-line center is outside this window.
                intersecting = bisect_right(tops, region[3]) - bisect_left(bottoms, region[1])
                if intersecting != len(selected) or any(id(word) in legacy_words for word in selected):
                    break
                tokens = sorted(selected, key=lambda word: (word["region"][0], word["region"][1]))
                merged = {**observed[start], "words": tokens, "text": " ".join(word["text"] for word in tokens),
                          "region": region}
                anchors = table_header_anchors(merged, labels)
                if anchors and {index for first, last, _ in anchors for index in range(first, last)} == set(range(len(tokens))):
                    candidates[frozenset(id(word) for word in tokens)] = (merged, anchors)
                break  # A full literal budget cannot grow into another valid header.
    memberships: dict[int, list[frozenset[int]]] = {}
    for identity in candidates:
        for word_id in identity:
            memberships.setdefault(word_id, []).append(identity)
    ambiguous = {identity for group in memberships.values() if len(group) > 1 for identity in group}
    return [candidate for identity, candidate in candidates.items() if identity not in ambiguous]


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


def projected_point(x, y, frame):
    if frame is None:
        return x, y
    x0, y0 = frame["pivot"]
    if frame["aspect"] is None:
        return x, y - frame["slope"] * (x - x0)
    ratio, cosine, sine = frame["aspect"], frame["cosine"], frame["sine"]
    return (x0 + (x - x0) * cosine + (y - y0) * sine / ratio,
            y0 - (x - x0) * sine * ratio + (y - y0) * cosine)


def projected_center(word, frame):
    return projected_point((word["region"][0] + word["region"][2]) / 2,
                           (word["region"][1] + word["region"][3]) / 2, frame)


def projected_region(word, frame):
    x1, y1, x2, y2 = word["region"]
    corners = [projected_point(x, y, frame) for x, y in ((x1, y1), (x1, y2), (x2, y1), (x2, y2))]
    return [min(x for x, _ in corners), min(y for _, y in corners),
            max(x for x, _ in corners), max(y for _, y in corners)]


def row_groups(lines, page, start, end, baseline_tolerance=.004, baseline_slope=0, coordinate_frame=None):
    def center(word):
        if coordinate_frame is not None:
            return projected_center(word, coordinate_frame)[1]
        return ((word["region"][1] + word["region"][3]) / 2
                - baseline_slope * (word["region"][0] + word["region"][2]) / 2)

    tokens = [(word, line) for line in lines if line["page"] == page
              for word in words(line) if start < (word["region"][1] + word["region"][3]) / 2 < end]
    tokens.sort(key=lambda item: (center(item[0]), item[0]["region"][0]))
    groups: list[tuple[float, list[tuple[dict, dict]]]] = []
    for word, line in tokens:
        middle = center(word)
        height = word["region"][3] - word["region"][1]
        if not groups or abs(groups[-1][0] - middle) > max(baseline_tolerance, height * .6):
            groups.append((middle, [(word, line)]))
        else:
            groups[-1][1].append((word, line))
    return [sorted(items, key=lambda item: item[0]["region"][0]) for _, items in groups]


def column_reading_order(column, baseline_slope=0, coordinate_frame=None):
    """Keep separate text baselines in reading order inside one table cell."""
    if not column:
        return column
    lines = [{**source, "words": [word]} for word, source in column]
    return [item for group in row_groups(lines, column[0][1]["page"], 0, 1,
                                        baseline_slope=baseline_slope,
                                        coordinate_frame=coordinate_frame) for item in group]


def flowing_row_groups(lines, page, start, end, bounds, multiline, tolerance, baseline_slope=0, coordinate_frame=None):
    """Use other columns as row baselines; retain incomplete and stray groups."""
    def column_number(word):
        center = projected_center(word, coordinate_frame)[0]
        return next((i for i, (left, right) in enumerate(pairwise(bounds)) if left <= center < right), None)

    def middle(items):
        if coordinate_frame is not None:
            return sum(projected_center(word, coordinate_frame)[1] for word, _ in items) / len(items)
        return sum((word["region"][1] + word["region"][3]) / 2
                   - baseline_slope * (word["region"][0] + word["region"][2]) / 2
                   for word, _ in items) / len(items)

    classified = []
    for group in row_groups(lines, page, start, end, baseline_slope=baseline_slope,
                            coordinate_frame=coordinate_frame):
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


def header_baseline_slope(anchors):
    """Measure a parallel baseline from complete observed anchors, never values."""
    if len(anchors) < 3:
        return 0
    points = [((item[2][0] + item[2][2]) / 2, (item[2][1] + item[2][3]) / 2)
              for item in anchors]
    span = points[-1][0] - points[0][0]
    if span < .25:
        return 0
    slopes = [(right[1] - left[1]) / (right[0] - left[0])
              for index, left in enumerate(points) for right in points[index + 1:]
              if right[0] - left[0] >= span / 3]
    if not slopes:
        return 0
    slope = median(slopes)
    intercept = median(y - slope * x for x, y in points)
    glyph_height = median(item[2][3] - item[2][1] for item in anchors)
    if max(abs(y - slope * x - intercept) for x, y in points) > glyph_height / 2:
        return 0  # Different caption baselines do not prove a page/table angle.
    return slope


def observed_flowing_groups(lines, page, start, end, bounds, multiline, tolerance, anchors, geometry=None):
    """Recover an incomplete partition using header geometry; retain all origins."""
    original = flowing_row_groups(lines, page, start, end, bounds, multiline, tolerance)

    def columns_for(group, column_bounds, frame=None):
        columns: list[list[dict]] = [[] for _ in column_bounds[:-1]]
        outside: list[dict] = []
        for word, _ in group:
            center = projected_center(word, frame)[0]
            index = next((i for i, (left, right) in enumerate(pairwise(column_bounds)) if left <= center < right), None)
            (outside if index is None else columns[index]).append(word)
        return columns, outside

    def complete(group):
        columns, outside = columns_for(group, bounds)
        return all(columns) and not outside

    if original is not None and all(complete(group) for group in original):
        return original, None, bounds
    slope = header_baseline_slope(anchors)
    if not slope:
        return original, None, bounds
    pivot = ((anchors[0][2][0] + anchors[0][2][2]) / 2,
             (anchors[0][2][1] + anchors[0][2][3]) / 2)
    ratio = geometry["width"] / geometry["height"] if geometry else None
    angle = math.atan(slope / ratio) if ratio is not None else 0
    frame = {"slope": slope, "aspect": ratio, "pivot": pivot,
             "cosine": math.cos(angle), "sine": math.sin(angle)}
    shift = projected_region({"region": anchors[0][2]}, frame)[0] - anchors[0][2][0]
    projected_bounds = [value + shift for value in bounds]
    projected = flowing_row_groups(lines, page, start, end, projected_bounds, multiline, tolerance,
                                   coordinate_frame=frame)
    if projected is None:
        return original, None, bounds
    projected_columns = [columns_for(group, projected_bounds, frame) for group in projected]
    # A partly explained body cannot justify a new complete row: that row may
    # contain a neighboring product's scalar. Preserve every original region.
    if any(outside or not all(columns) for columns, outside in projected_columns):
        return original, None, bounds
    previous_bottoms: dict[int, float] = {}
    for columns, _ in projected_columns:
        for index, column in enumerate(columns):
            if index == multiline:
                continue
            regions = sorted((projected_region(word, frame) for word in column), key=lambda region: region[0])
            if any(left[2] > right[0] for left, right in pairwise(regions)):
                return original, None, bounds  # Overlapping values are not one scalar cell.
            cores = [(region[1] + (region[3] - region[1]) / 3,
                      region[3] - (region[3] - region[1]) / 3) for region in regions]
            if max(top for top, _ in cores) > min(bottom for _, bottom in cores):
                return original, None, bounds  # One cell needs a common observed glyph baseline.
            top, bottom = min(region[1] for region in regions), max(region[3] for region in regions)
            if index in previous_bottoms and top <= previous_bottoms[index]:
                return original, None, bounds  # Neighboring row glyphs are not separable.
            previous_bottoms[index] = bottom
    assert {id(word) for group in projected for word, _ in group} == {
        id(word) for line in lines if line["page"] == page for word in words(line)
        if start < (word["region"][1] + word["region"][3]) / 2 < end}
    return projected, frame, projected_bounds


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


def header_caption_word_ids(lines, rule, fields, validator) -> set[int]:
    """Attribute complete captions in a local band of the observed header.

    Product cues before or overlapping a caption retain the entire region for
    review. No value is synthesized and the start of detail is never moved.
    """
    if (
        not fields
        or validator is None
        or (not any(field.get("logical_type", "STRING") != "STRING" for field in fields))
    ):
        return set()
    visual = visual_lines(lines, 0.012 if rule.get("flowing") else 0.004)
    excluded: set[int] = set()
    for header in visual:
        anchors = table_header_anchors(header, rule["header_labels"])
        if not anchors:
            continue
        dx = anchors[0][2][0] - rule["region"][0]
        if abs(dx) > rule.get("tolerance", 0.12):
            continue
        top = min(anchor[2][1] for anchor in anchors)
        # An example rectangle may contain products. Also require membership
        # in a local band measured from the actual anchor glyphs.
        anchor_height = max(anchor[2][3] - anchor[2][1] for anchor in anchors)
        bottom = min(
            top + rule["region"][3] - rule["region"][1],
            max(anchor[2][3] for anchor in anchors) + 2 * anchor_height,
        )
        bounds = [value + dx for value in rule["column_bounds"]]
        all_anchor_ids = {
            id(word) for observed in anchors for word in words(header)[observed[0] : observed[1]]
        }
        header_excluded: set[int] = set()
        for index, (field, observed) in enumerate(zip(fields, anchors)):
            # A STRING fragment can itself be an identifier or description,
            # including punctuation and leading zeros. Keep it for review.
            if field.get("logical_type", "STRING") == "STRING":
                continue
            label = field.get("label", "")
            if not label or anchor_key(label) == anchor_key(rule["header_labels"][index]):
                continue
            anchor_ids = {id(word) for word in words(header)[observed[0] : observed[1]]}
            column = []
            for entry in lines:
                if entry["page"] != header["page"]:
                    continue
                for word in words(entry):
                    x = (word["region"][0] + word["region"][2]) / 2
                    if (
                        bounds[index] <= x < bounds[index + 1]
                        and top - 1e-09 <= word["region"][1]
                        and (word["region"][3] <= bottom + 1e-09)
                    ):
                        column.append((word, entry))
            if not column:
                continue
            column = column_reading_order(column)
            if field.get("logical_type", "STRING") != "STRING":
                # Valid scalars stay in the body, including before the last
                # caption; they cannot become tokens of the caption match.
                column = [
                    (word, entry)
                    for word, entry in column
                    if id(word) in anchor_ids or validator(word["text"], field)[1] != "VALID"
                ]
            starts = [
                position for position, (word, _) in enumerate(column) if id(word) in anchor_ids
            ]
            if not starts:
                continue
            first = min(starts)
            matches = []
            for last in range(first + 1, min(len(column), first + 24) + 1):
                selected = column[first:last]
                if anchor_ids <= {id(word) for word, _ in selected} and anchor_key(
                    " ".join((word["text"] for word, _ in selected))
                ) == anchor_key(label):
                    matches.append(selected)
            if len(matches) != 1:
                continue
            extra = [word for word, _ in matches[0] if id(word) not in anchor_ids]
            if not extra:
                continue
            if field.get("logical_type", "STRING") != "STRING" and (
                validator(" ".join(word["text"] for word in extra), field)[1] == "VALID"
                or any(validator(word["text"], field)[1] == "VALID" for word in extra)
            ):
                continue
            header_excluded.update(id(word) for word in extra)
        body_regions: list[tuple[float, float]] = []
        extra_bottoms: list[float] = []
        for entry in lines:
            if entry["page"] != header["page"]:
                continue
            for word in words(entry):
                if id(word) in all_anchor_ids:
                    continue
                x = (word["region"][0] + word["region"][2]) / 2
                if id(word) in header_excluded:
                    extra_bottoms.append(word["region"][3])
                column_index = next(
                    (i for i in range(len(fields)) if bounds[i] <= x < bounds[i + 1]), None
                )
                if column_index is None:
                    continue
                kind = fields[column_index].get("logical_type", "STRING")
                if column_index == rule.get("multiline_column") or (
                    kind == "STRING" and id(word) not in header_excluded
                ) or (
                    kind != "STRING"
                    and validator(word["text"], fields[column_index])[1] == "VALID"
                ):
                    body_regions.append((word["region"][1], word["region"][3]))
        # A distinct later product does not invalidate a preceding caption.
        # Earlier/overlapping cues make attribution ambiguous: preserve all
        # original tokens from this header rather than part of the region.
        if not any(
            (
                body_bottom >= top and body_top <= caption_bottom + 0.004
                for caption_bottom in extra_bottoms
                for body_top, body_bottom in body_regions
            )
        ):
            excluded.update(header_excluded)
    return excluded


def without_caption_words(lines, excluded):
    filtered = []
    for entry in lines:
        selected = [word for word in words(entry) if id(word) not in excluded]
        if selected:
            filtered.append(
                {
                    **entry,
                    "words": selected,
                    "text": " ".join(word["text"] for word in selected),
                    "region": box(selected),
                }
            )
    return filtered


def table_candidates(lines, rule, fields=None, header_values=(), validator=None, page_geometry=None):
    source_lines = lines
    excluded = header_caption_word_ids(lines, rule, fields, validator)
    if excluded:
        lines = without_caption_words(lines, excluded)
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
    for line, anchors in header_neighborhoods(lines, rule, source_lines, headers):
        positions = [item[2][0] for item in anchors]
        dx = anchors[0][2][0] - rule["region"][0]
        if positions == sorted(positions) and abs(dx) <= rule.get("tolerance", .12):
            headers.append((line, anchors, dx))
    headers.sort(key=lambda header: (header[0]["page"], header[0]["region"][1], header[0]["region"][0]))
    previous_bounds, continuing = None, False
    for page in sorted({line["page"] for line in lines}):
        page_headers = [h for h in headers if h[0]["page"] == page]
        segments = [(max(a[2][3] for a in anchors), line["region"][1], dx, anchors) for line, anchors, dx in page_headers]
        if not segments and rule.get("continue_pages") and continuing and previous_bounds is not None:
            segments = [(0, 0, previous_bounds, [])]
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
        for header_bottom, header_top, dx, observed_anchors in segments:
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
            body_frame = None
            if rule.get("flowing") and rule.get("multiline_column") is not None:
                groups, body_frame, bounds = observed_flowing_groups(lines, page, header_bottom, end, bounds,
                    rule["multiline_column"], rule.get("row_tolerance", .018), observed_anchors,
                    (page_geometry or {}).get(str(page)))
            else:
                groups = row_groups(lines, page, header_bottom, end,
                                    rule.get("row_tolerance", .018) if rule.get("flowing") else .004)
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
                    center = projected_center(word, body_frame)[0]
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
                columns = [column_reading_order(column, coordinate_frame=body_frame) for column in columns]
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
