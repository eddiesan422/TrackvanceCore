"""Bounded source spans shared by the coordinator and isolated model gateway.

Only offsets into exact OCR observations enter the catalog. The model selects
identifiers; it cannot return replacement strings, amounts, boxes or confidence.
"""
from __future__ import annotations

import hashlib
import json
import re

CATALOG_VERSION = "TRACKVANCE_SOURCE_FRAGMENTS_098_V1"
MAX_FRAGMENTS = 4096
MAX_FRAGMENT_TEXT_BYTES = 120000
TEXT_NORMALIZATION = "DECLARED_MULTILINE_ESCAPED_BREAK_JOIN_V1"
ESCAPED_LINE_BREAK = chr(92) + "n"


def _digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
        separators=(",", ":"), allow_nan=False).encode()).hexdigest()


def build_catalog(observations):
    if not isinstance(observations, list) or len(observations) > 100000:
        raise ValueError("LLM_FRAGMENT_INTEGRITY")
    spans, total = set(), 0

    def add(index, text, start, end):
        nonlocal total
        while start < end and text[start].isspace():
            start += 1
        while end > start and text[end - 1].isspace():
            end -= 1
        key = (index, start, end)
        if start == end or key in spans:
            return
        spans.add(key)
        total += len(text[start:end].encode("utf-8"))
        if len(spans) > MAX_FRAGMENTS or total > MAX_FRAGMENT_TEXT_BYTES:
            raise ValueError("LLM_FRAGMENT_LIMIT")

    try:
        source_hash = hashlib.sha256(json.dumps(observations, sort_keys=True,
            ensure_ascii=False, allow_nan=False).encode()).hexdigest()
        for index, observation in enumerate(observations):
            if not isinstance(observation, dict) or not isinstance(observation.get("text"), str):
                raise TypeError("observation shape")
            text = observation["text"]
            if len(text) > 65536:
                raise ValueError("LLM_FRAGMENT_LIMIT")
            add(index, text, 0, len(text))
            for match in re.finditer(r"[^|\t]+", text):
                add(index, text, match.start(), match.end())
            colon = text.find(":")
            if colon >= 0:
                add(index, text, colon + 1, len(text))
            for match in re.finditer(r"\S+", text):
                add(index, text, match.start(), match.end())
        fragments = [{"fragment_id": identity, "evidence_id": index, "start": start, "end": end,
            "text": observations[index]["text"][start:end]}
            for identity, (index, start, end) in enumerate(sorted(spans))]
        result = {"version": CATALOG_VERSION, "observations_sha256": source_hash, "fragments": fragments}
        return {**result, "sha256": _digest(result)}
    except ValueError as error:
        if str(error) in {"LLM_FRAGMENT_LIMIT", "LLM_FRAGMENT_INTEGRITY"}:
            raise
        raise ValueError("LLM_FRAGMENT_INTEGRITY") from None
    except (TypeError, UnicodeError, OverflowError):
        raise ValueError("LLM_FRAGMENT_INTEGRITY") from None


def multiline_fields(definition):
    allowed = set()
    for table in definition.get("tables", []):
        index = (table.get("visual") or {}).get("multiline_column")
        fields = table.get("fields", [])
        if type(index) is int and 0 <= index < len(fields) and fields[index].get("logical_type", "STRING") == "STRING":
            allowed.add((table["name"], fields[index]["name"]))
    return allowed


def escaped_break_normalization(observed):
    return {"kind": TEXT_NORMALIZATION, "observed_sha256": hashlib.sha256(observed.encode()).hexdigest()}


def canonical_observed(cell, *, allow_multiline=False):
    """Explicit text-only composition; exact raw observations remain untouched."""
    observed = cell.get("observed")
    normalization = cell.get("source_text_normalization")
    if normalization is None:
        return observed
    if (not allow_multiline or not isinstance(observed, str) or ESCAPED_LINE_BREAK not in observed
            or normalization != escaped_break_normalization(observed)):
        raise ValueError("UNDECLARED_SOURCE_TEXT_NORMALIZATION")
    return " ".join(observed.replace(ESCAPED_LINE_BREAK, "\n").split())
