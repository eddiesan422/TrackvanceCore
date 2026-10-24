"""Literal candidates from bounded source table spans. No model or oracle access.

Only exact source pipe-cell spans are selected. Origins remain measured whole
Paddle blocks. This module never certifies scalar truth, spatial completeness,
independent row coverage, human review or automatic approval.
"""

import copy
import hashlib
import json
import math
import re
import unicodedata

from . import document_semantic_fragments as FRAGMENTS
from . import document_template_builder_rules as RULES

MAX_OBSERVATIONS = 4096
MAX_TABLES = 20
MAX_BLOCKS = 512


def label_key(value):
    # Header comparison only; exact observed strings/spans remain unchanged.
    text = " ".join(
        "".join(
            c
            for c in unicodedata.normalize("NFKD", value.casefold())
            if not unicodedata.combining(c)
        )
        .replace(FRAGMENTS.ESCAPED_LINE_BREAK, " ")
        .split()
    )
    return text.removesuffix(".")


def label_match(observed, declared, *, percent_suffix=False):
    actual, wanted = label_key(observed), label_key(declared)
    if actual == wanted:
        return "EXACT_DECLARED_LABEL_AFTER_HEADER_CASE_ACCENT_WHITESPACE"
    if percent_suffix and not wanted.endswith("%") and actual.removesuffix("%").rstrip() == wanted:
        return "DECLARED_LABEL_WITH_OBSERVED_PERCENT_SUFFIX"
    return None


def spans(text):
    output, start = [], 0
    for end in [match.start() for match in re.finditer(r"\|", text)] + [len(text)]:
        left, right = start, end
        while left < right and text[left].isspace():
            left += 1
        while right > left and text[right - 1].isspace():
            right -= 1
        output.append((left, right, text[left:right]))
        start = end + 1
    return output


def valid_region(box):
    return (
        isinstance(box, list)
        and len(box) == 4
        and all(type(v) in (int, float) and math.isfinite(v) and 0 <= v <= 1 for v in box)
        and box[0] < box[2]
        and box[1] < box[3]
    )


def valid_origin(source):
    page = source.get("page")
    return (
        type(page) is int
        and 1 <= page <= 50
        and source.get("method") == "OCR"
        and valid_region(source.get("region"))
    )


def continuation_pages(observations, visual):
    context = visual.get("continuation_context")
    if not isinstance(context, dict):
        return set()
    label, region = context.get("label"), context.get("region")
    if (
        not isinstance(label, str)
        or not label_key(label)
        or not isinstance(region, list)
        or not valid_region(region)
        or region[3] - region[1] > 0.15
    ):
        return set()
    return {
        source["page"]
        for source in observations
        if valid_origin(source)
        and label_key(source["text"]) == label_key(label)
        and region[0] <= source["region"][0] < source["region"][2] <= region[2]
        and region[1] <= source["region"][1] < source["region"][3] <= region[3]
    }


def table_configuration(table):
    fields, visual = table.get("fields"), table.get("visual")
    if not isinstance(fields, list) or not fields or not isinstance(visual, dict):
        return False
    labels = visual.get("header_labels")
    grammar = visual.get("row_start")
    if (
        not isinstance(labels, list)
        or len(labels) != len(fields)
        or any(not isinstance(x, str) or not label_key(x) for x in labels)
        or len(set(map(label_key, labels))) != len(labels)
        or len({field.get("name") for field in fields}) != len(fields)
        or not isinstance(grammar, dict)
        or type(grammar.get("column")) is not int
        or not 0 <= grammar["column"] < len(fields)
    ):
        return False
    start_page = visual.get("page", 1)
    if (
        type(start_page) is not int
        or not 1 <= start_page <= 50
        or type(visual.get("continue_pages", False)) is not bool
    ):
        return False
    multiline = visual.get("multiline_column")
    return (
        multiline is None
        or type(multiline) is int
        and 0 <= multiline < len(fields)
        and fields[multiline].get("logical_type", "STRING") == "STRING"
    )


def permitted_page(page, table):
    start = table["visual"].get("page", 1)
    return page == start or page > start and table["visual"].get("continue_pages") is True


def header_match(parts, table, percentage_fields):
    labels = table["visual"]["header_labels"]
    if len(parts) != len(labels):
        return None
    kinds = [
        label_match(part[2], label, percent_suffix=field["name"] in percentage_fields)
        for part, label, field in zip(parts, labels, table["fields"], strict=True)
    ]
    return kinds if all(kinds) else None


def looks_like_header(parts, table, percentage_fields):
    labels = table["visual"]["header_labels"]
    return len(parts) == len(labels) and sum(
        any(
            label_match(part[2], label, percent_suffix=field["name"] in percentage_fields)
            for label, field in zip(labels, table["fields"], strict=True)
        )
        for part in parts
    ) >= min(2, len(labels))


def row_key(parts, table):
    column = table["visual"]["row_start"]["column"]
    if len(parts) != len(table["fields"]) or not parts[column][2]:
        return None
    text = parts[column][2]
    if FRAGMENTS.ESCAPED_LINE_BREAK in text:
        if table["visual"].get("multiline_column") != column:
            return None
        text = text.replace(FRAGMENTS.ESCAPED_LINE_BREAK, "\n")
    token = text.split()[0]
    return token if RULES.declared_row_start(token, table["visual"]["row_start"]) else None


def row_start_tokens(parts, table):
    column = table["visual"]["row_start"]["column"]
    if column >= len(parts):
        return []
    text = parts[column][2]
    if table["visual"].get("multiline_column") == column:
        text = text.replace(FRAGMENTS.ESCAPED_LINE_BREAK, "\n")
    return [
        token
        for token in text.split()
        if RULES.declared_row_start(token, table["visual"]["row_start"])
    ]


def stop_label_match(text, label):
    observed, declared = label_key(text), label_key(label)
    return bool(declared) and (
        observed == declared or observed.startswith((declared + " |", declared + " "))
    )


def rejected_source(index, source, reason):
    return {
        "evidence_id": index,
        "reason": reason,
        "source": copy.deepcopy(source),
        **{
            key: copy.deepcopy(source[key])
            for key in ("text", "page", "region", "method")
            if key in source
        },
    }


def map_candidates(observations, definition):
    if (
        not isinstance(observations, list)
        or len(observations) > MAX_OBSERVATIONS
        or len(definition.get("tables", [])) > MAX_TABLES
    ):
        raise ValueError("LLM_FRAGMENT_LIMIT")
    catalog = FRAGMENTS.build_catalog(observations)
    lookup = {
        (item["evidence_id"], item["start"], item["end"]): item for item in catalog["fragments"]
    }
    configured = [table for table in definition.get("tables", []) if table_configuration(table)]
    control = definition.get("invoice_controls") or {}
    percentage_fields = {
        table["name"]: {
            control.get(role)
            for role in ("discount_percent", "tax_percent")
            if control.get("table") == table["name"]
        }
        & {
            field["name"]
            for field in table["fields"]
            if field.get("logical_type", "STRING") == "STRING"
        }
        for table in configured
    }
    declared_names = [table["name"] for table in definition.get("tables", [])]
    if len(set(declared_names)) != len(declared_names):
        raise ValueError("DUPLICATE_DECLARED_TABLE_NAME")
    output: dict = {
        "catalog_sha256": catalog["sha256"],
        "observations_sha256": catalog["observations_sha256"],
        "tables": {name: [] for name in declared_names},
        "rejected": {name: [] for name in declared_names},
        "blocks": [],
        "unsupported_tables": [
            name for name in declared_names if name not in {t["name"] for t in configured}
        ],
        "uncertainties": [
            "COARSE_OBSERVED_BLOCK_ORIGINS_ONLY",
            "FLATTENED_PIPE_STRUCTURE_NOT_ORIGINAL_HTML_CELL_PROOF",
        ],
        "independent_row_coverage": False,
        "spatial_completeness_certified": False,
        "scalar_validity_measured": False,
        "automatic_approval_measured": False,
    }
    blocks: dict = {}
    invalid: list[dict] = []
    for index, source in enumerate(observations):
        if source.get("block_label") != "table":
            continue
        if not valid_origin(source):
            invalid.append(
                {
                    "observation_ids": [index],
                    "status": "UNASSIGNED",
                    "reason": "INVALID_SOURCE_ORIGIN",
                    "raw_observations": [copy.deepcopy(source)],
                }
            )
            continue
        key = (source["page"], tuple(source["region"]))
        blocks.setdefault(key, []).append(index)
    output["blocks"].extend(invalid)
    if len(blocks) + len(invalid) > MAX_BLOCKS:
        raise ValueError("LLM_FRAGMENT_LIMIT")
    table_context_pages = {
        table["name"]: continuation_pages(observations, table["visual"]) for table in configured
    }
    table_stops: dict = {}
    for table in configured:
        visual = table["visual"]
        stop_labels = [
            label
            for label in [visual.get("stop_label", ""), *visual.get("stop_label_aliases", [])]
            if isinstance(label, str) and label
        ]
        table_stops[table["name"]] = [
            (index, source)
            for index, source in enumerate(observations)
            if any(stop_label_match(source["text"], label) for label in stop_labels)
        ]
    seen_keys: dict[str, set[str]] = {name: set() for name in declared_names}
    seen_header_pages: dict[str, set[int]] = {name: set() for name in declared_names}
    for (page, box), identities in sorted(
        blocks.items(), key=lambda item: (item[0][0], min(item[1]))
    ):
        parsed = [(index, spans(observations[index]["text"])) for index in identities]
        matches = {
            table["name"]: [
                (index, header_match(parts, table, percentage_fields[table["name"]]))
                for index, parts in parsed
                if permitted_page(page, table)
                and header_match(parts, table, percentage_fields[table["name"]])
            ]
            for table in configured
        }
        matched = [table for table in configured if matches[table["name"]]]
        inferred = False
        if not matched:
            # Headerless continuation needs explicit declared page/context and a
            # unique compatible table. Never inherit only from a nearby box.
            possible = []
            for table in configured:
                visual = table["visual"]
                if (
                    permitted_page(page, table)
                    and visual.get("continue_pages")
                    and page - 1 in seen_header_pages[table["name"]]
                    and page in table_context_pages[table["name"]]
                    and any(row_key(parts, table) for _, parts in parsed)
                ):
                    possible.append(table)
            matched, inferred = possible, bool(possible)
        block = {
            "page": page,
            "region": list(box),
            "observation_ids": identities,
            "raw_observations": [copy.deepcopy(observations[index]) for index in identities],
        }
        if len(matched) != 1:
            block.update(
                status="UNASSIGNED",
                reason="AMBIGUOUS_TABLE_HEADER"
                if matched
                else "NO_UNIQUE_DECLARED_HEADER_OR_CONTINUATION",
            )
            output["blocks"].append(block)
            continue
        table = matched[0]
        name, visual = table["name"], table["visual"]
        headers = {index: kinds for index, kinds in matches[name]}
        if any(
            looks_like_header(parts, table, percentage_fields[name]) and index not in headers
            for index, parts in parsed
        ):
            block.update(status="UNASSIGNED", reason="MISORDERED_OR_CONFLICTING_HEADER", table=name)
            output["blocks"].append(block)
            continue
        if inferred:
            output["uncertainties"].append("CONTINUATION_INFERRED_FROM_DECLARED_CONTEXT:" + name)
        seen_header_pages[name].add(page)
        first_header = min(headers) if headers else -1
        mapped_count, rejected_count = 0, 0
        stops = [(index, line) for index, line in table_stops[name] if line.get("page") == page]
        for index, parts in parsed:
            if index in headers:
                continue
            source, reason = observations[index], None
            key = row_key(parts, table)
            if index < first_header:
                reason = "ROW_PRECEDES_DECLARED_HEADER"
            elif any(
                stop_index <= index
                or valid_origin(stop)
                and source["region"] != stop["region"]
                and source["region"][1] >= stop["region"][3]
                for stop_index, stop in stops
            ):
                reason = "AT_OR_AFTER_DECLARED_PAGE_STOP"
            elif len(parts) != len(table["fields"]):
                reason = "PHYSICAL_PIPE_CELL_COUNT_MISMATCH"
            elif any(not item[2] for item in parts):
                reason = "EMPTY_SOURCE_CELL"
            elif key is None:
                reason = "UNKNOWN_DECLARED_ROW_START"
            elif len(row_start_tokens(parts, table)) != 1:
                reason = "AMBIGUOUS_MULTIPLE_DECLARED_ROW_STARTS"
            elif any(
                (FRAGMENTS.ESCAPED_LINE_BREAK in part[2] or "\n" in part[2])
                and visual.get("multiline_column") != column
                for column, part in enumerate(parts)
            ):
                reason = "UNDECLARED_MULTILINE_CELL"
            if reason:
                output["rejected"][name].append(rejected_source(index, source, reason))
                rejected_count += 1
                continue
            row, issues = {}, []
            for field, (start, end, observed) in zip(table["fields"], parts, strict=True):
                fragment = lookup.get((index, start, end))
                if fragment is None or fragment["text"] != observed:
                    raise ValueError("EXACT_SOURCE_CELL_FRAGMENT_NOT_IN_SHARED_CATALOG")
                cell = {
                    "observed": observed,
                    "value": None,
                    "validity": "UNVALIDATED",
                    "confidence": None,
                    "reviewed": False,
                    "origin": {
                        name: copy.deepcopy(source[name]) for name in ("page", "region", "method")
                    },
                    "source_fragment": {
                        "catalog_sha256": catalog["sha256"],
                        **{
                            name: fragment[name]
                            for name in ("fragment_id", "evidence_id", "start", "end")
                        },
                    },
                    "source_spatial_precision": "OBSERVED_WHOLE_BLOCK",
                }
                if (name, field["name"]) in FRAGMENTS.multiline_fields(
                    definition
                ) and FRAGMENTS.ESCAPED_LINE_BREAK in observed:
                    cell["source_text_normalization"] = FRAGMENTS.escaped_break_normalization(
                        observed
                    )
                    cell["candidate_text"] = FRAGMENTS.canonical_observed(
                        cell, allow_multiline=True
                    )
                else:
                    cell["candidate_text"] = observed
                row[field["name"]] = cell
            if key in seen_keys[name]:
                issues.append("DUPLICATE_DECLARED_ROW_KEY")
                output["rejected"][name].append(rejected_source(index, source, issues[0]))
                rejected_count += 1
            seen_keys[name].add(key)
            # Duplicate rows stay visible in candidates and in rejection facts.
            output["tables"][name].append(
                {"row_key": key, "cells": row, "issues": issues, "source_observation_id": index}
            )
            mapped_count += 1
        block.update(
            status="PARTIAL" if rejected_count else "MAPPED" if mapped_count else "HEADER_ONLY",
            table=name,
            header_observations=[
                {"evidence_id": index, "match_kinds": kinds} for index, kinds in headers.items()
            ],
            continuation_inferred=inferred,
            candidate_rows=mapped_count,
            rejected_rows=rejected_count,
        )
        output["blocks"].append(block)
    # Plausible detail blocks that failed identification/geometry must enter the
    # ordinary review/unparsed channel, not disappear into diagnostic metadata.
    classifications = {
        index: "NON_TABLE_OBSERVATION_RETAINED"
        for index, line in enumerate(observations)
        if line.get("block_label") != "table"
    }
    successful_claims, unknown_ids = [], []
    for block in output["blocks"]:
        block["source_block_id"] = hashlib.sha256(
            json.dumps(
                {key: block.get(key) for key in ("page", "region", "observation_ids")},
                sort_keys=True,
                allow_nan=False,
            ).encode()
        ).hexdigest()
        if block["status"] == "UNASSIGNED":
            plausible = [
                table
                for table in configured
                if any(
                    len(spans(source["text"])) == len(table["fields"])
                    or row_start_tokens(spans(source["text"]), table)
                    or looks_like_header(
                        spans(source["text"]), table, percentage_fields[table["name"]]
                    )
                    for source in block["raw_observations"]
                )
            ]
            block["plausible_declared_tables"] = [table["name"] for table in plausible]
            for table in plausible:
                for index in block["observation_ids"]:
                    # Do not duplicate an earlier whole-block diagnostic.
                    if not any(
                        item.get("evidence_id") == index
                        for item in output["rejected"][table["name"]]
                    ):
                        output["rejected"][table["name"]].append(
                            rejected_source(
                                index,
                                observations[index],
                                "UNASSIGNED_PLAUSIBLE_DETAIL_BLOCK:" + block["reason"],
                            )
                        )
            unknown_ids.extend(block["observation_ids"])
            category = "UNASSIGNED_TABLE_OBSERVATION_RETAINED"
        elif block["status"] == "MAPPED":
            successful_claims.append(
                {
                    "source_block_id": block["source_block_id"],
                    "table": block["table"],
                    "observation_ids": list(block["observation_ids"]),
                    "header_observation_ids": [
                        item["evidence_id"] for item in block["header_observations"]
                    ],
                    "row_observation_ids": [
                        row["source_observation_id"]
                        for row in output["tables"][block["table"]]
                        if row["source_observation_id"] in block["observation_ids"]
                    ],
                    "claim": "EXACT_LITERAL_CELL_SPANS_ONLY_NOT_TYPED_VALIDITY_OR_COMPLETENESS",
                    "prompt_omission_approved": False,
                }
            )
            category = "SUCCESSFUL_LITERAL_MAPPING_UNVALIDATED"
        else:
            unknown_ids.extend(block["observation_ids"])
            category = "PARTIAL_TABLE_BLOCK_ALL_OBSERVATIONS_HELD"
        for index in block["observation_ids"]:
            if index in classifications:
                raise ValueError("SOURCE_OBSERVATION_CLASSIFIED_MORE_THAN_ONCE")
            classifications[index] = category
    if set(classifications) != set(range(len(observations))):
        raise ValueError("SOURCE_OBSERVATION_CLASSIFICATION_GAP")
    output["successful_literal_mapping_claims"] = successful_claims
    output["claimed_detail_observation_ids"] = sorted(
        index for claim in successful_claims for index in claim["observation_ids"]
    )
    output["unknown_or_partial_table_observation_ids"] = sorted(unknown_ids)
    output["observation_classification"] = [
        {"evidence_id": index, "classification": classifications[index]}
        for index in range(len(observations))
    ]
    return output


def prompt_catalog_view(observations, definition, targets):
    """Prompt-only source view; no source or catalog deletion.

    Recompute claims from full observations. The caller's normal typed pipeline
    determines targets. Unknown/partial/header-only blocks never supply claims.
    IDs remain global IDs from the unchanged complete source catalog.
    """
    tables = {table["name"] for table in definition.get("tables", [])}
    fields = {field["name"] for field in definition.get("fields", [])}
    if (
        not isinstance(targets, dict)
        or set(targets) != {"fields", "tables"}
        or not isinstance(targets["fields"], list)
        or not isinstance(targets["tables"], list)
        or any(not isinstance(item, str) for group in targets.values() for item in group)
        or len(set(targets["fields"])) != len(targets["fields"])
        or len(set(targets["tables"])) != len(targets["tables"])
        or not set(targets["fields"]) <= fields
        or not set(targets["tables"]) <= tables
    ):
        raise ValueError("DECLARED_SEMANTIC_TARGETS_REQUIRED")
    mapped = map_candidates(observations, definition)
    catalog = FRAGMENTS.build_catalog(observations)
    eligible = [
        claim
        for claim in mapped["successful_literal_mapping_claims"]
        if claim["table"] not in targets["tables"]
    ]
    omitted_ids = sorted(index for claim in eligible for index in claim["observation_ids"])
    omitted_set = set(omitted_ids)
    selected = [
        fragment for fragment in catalog["fragments"] if fragment["evidence_id"] not in omitted_set
    ]
    view: dict = {
        "full_catalog_sha256": catalog["sha256"],
        "full_observations_sha256": catalog["observations_sha256"],
        "targets": copy.deepcopy(targets),
        "fragments": copy.deepcopy(selected),
        "global_fragment_ids_preserved": True,
        "source_observations_deleted": 0,
        "full_fragment_count": len(catalog["fragments"]),
        "prompt_fragment_count": len(selected),
        "omitted_detail_observation_ids": omitted_ids,
        "omitted_fragment_ids": [
            fragment["fragment_id"]
            for fragment in catalog["fragments"]
            if fragment["evidence_id"] in omitted_set
        ],
        "claimed_source_block_ids": [claim["source_block_id"] for claim in eligible],
        "unknown_or_partial_table_observation_ids_retained": mapped[
            "unknown_or_partial_table_observation_ids"
        ],
        "basis": "ONLY_COMPLETE_LITERAL_MAPPED_DETAIL_BLOCKS_OF_NON_TARGET_TABLES; TYPED_TARGET_COMPUTATION_REMAINS_CALLER_AUTHORITY",
        "independent_row_coverage": False,
        "automatic_approval_measured": False,
    }
    view["sha256"] = hashlib.sha256(
        json.dumps(
            view, sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False
        ).encode()
    ).hexdigest()
    return {"full_catalog": catalog, "prompt_view": view}


def merge_candidates(payload, mapped, definition, validator, *, observations=None):
    """Preserve existing candidates and require the caller to rerun normal validators.

    All pre-existing rows are retained, including mixed valid/invalid rows.
    Geometry-generated unparsed facts are never bulk-cleared by this mapper.
    """
    if observations is None:
        observations = payload.get("lines")
    else:
        # The confined remapper adds real word boxes and strips block labels.
        # Raw block labels may guide candidates only when their actual source
        # text/page/rectangle was retained by that independent input boundary.
        def identity(line):
            return json.dumps(
                {key: line.get(key) for key in ("text", "page", "region", "method")},
                sort_keys=True,
                allow_nan=False,
            )

        validated = {identity(line) for line in payload.get("lines", [])}
        if any(identity(line) not in validated for line in observations):
            raise ValueError("DETERMINISTIC_CANDIDATE_UNVALIDATED_OBSERVATION")
    if mapped != map_candidates(observations, definition):
        raise ValueError("DETERMINISTIC_CANDIDATE_SOURCE_OR_CONFIGURATION_CHANGED")
    result = copy.deepcopy(payload)
    result["deterministic_pipe_candidates"] = copy.deepcopy(mapped)
    result.setdefault("semantic_conflicts", [])
    result.setdefault("table_unparsed", {})
    for table in definition.get("tables", []):
        name = table["name"]
        candidates = []
        for record in mapped["tables"][name]:
            row = {}
            for field in table["fields"]:
                cell = copy.deepcopy(record["cells"][field["name"]])
                cell["value"], cell["validity"] = validator(cell["candidate_text"], field)
                row[field["name"]] = cell
            candidates.append(row)
        previous = result.setdefault("tables", {}).get(name, [])
        if previous:
            names = [field["name"] for field in table["fields"]]
            if candidates and [
                [row.get(field, {}).get("value") for field in names] for row in candidates
            ] != [[row.get(field, {}).get("value") for field in names] for row in previous]:
                result["semantic_conflicts"].append("TABLE:" + name)
        elif candidates:
            result["tables"][name] = candidates
        result["table_unparsed"].setdefault(name, []).extend(
            copy.deepcopy(mapped["rejected"][name])
        )
    return result
