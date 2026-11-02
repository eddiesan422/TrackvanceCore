"""Bounded source-backed printed counters, separate from file/reader page counts.

This pure verifier receives already authenticated source facts. It performs no
OCR, source IO, layout invention or open-ended narrative interpretation. A model
claim can record uncertainty but cannot prove a counter or its documentary scope.
"""
from __future__ import annotations

import math
import re
from collections import defaultdict
from typing import Any

from .document_header_association import label_key

VERSION = "DOCUMENT_PRINTED_PAGINATION_V1"
MAX_PRINTED_PAGES = 10000
_COUNTER = re.compile(r"(?<!\w)(?:page|p[áa]gina|hoja)\s+(\d{1,6})\s*(?:of|de|/)\s*(\d{1,6})(?!\d)", re.IGNORECASE)
_SECTION = re.compile(r"\b(annex|appendix|section|anexo|ap[eé]ndice|secci[oó]n)\s+([\w.-]{1,40})\s*[-|:,]*\s*$", re.IGNORECASE)
_DOCUMENT = re.compile(r"(?:document|documento|invoice|factura|statement|extracto|report|informe)\s*[-|:,]*\s*$", re.IGNORECASE)
_CITATION = re.compile(r"\b(?:see|refer\s+to|v[eé]ase|ver|consulte|consultar)\b", re.IGNORECASE)
_THIS_DOCUMENT = r"(?:this\s+(?:invoice|document|statement|report)|(?:esta|este)\s+(?:factura|documento|extracto|informe))"
_CONTINUATION = re.compile(r"(?:contin[uú](?:a|an|e|es|ed|es)?|continued|continues)\b.{0,100}?(?:page|p[áa]gina|hoja)\s+(\d{1,6}).{0,40}?" + _THIS_DOCUMENT
    + r"|" + _THIS_DOCUMENT + r".{0,60}?(?:continues|continued|contin[uú]a).{0,30}?(?:page|p[áa]gina|hoja)\s+(\d{1,6})", re.IGNORECASE)


def _independent(fact: dict[str, Any]) -> bool:
    ref = fact.get("ref") or {}
    mechanism = str(ref.get("mechanism", fact.get("mechanism", ""))).upper()
    return bool(mechanism in {"PDF_NATIVE", "NATIVE", "PADDLE"}
        and fact.get("source_independent", True) is True and not fact.get("dependencies")
        and not ref.get("dependencies"))


def _measured_footer(fact: dict[str, Any]) -> bool:
    ref = fact.get("ref") or {}
    kind = str(fact.get("geometry_kind", ref.get("geometry_kind", "UNKNOWN")))
    box = fact.get("region")
    measured = bool("MEASURED" in kind and isinstance(box, (list, tuple)) and len(box) == 4
        and all(type(item) in (int, float) and math.isfinite(item) and 0 <= item <= 1 for item in box)
        and box[0] < box[2] and .85 <= box[1] < box[3])
    # The role is usable only when supplied by the independent structure reader.
    return measured or bool(fact.get("block_role") == "footer" and fact.get("block_role_source") == "SOURCE")


def _scope(text: str, match: re.Match[str], fact: dict[str, Any], continuations: dict[int, list[dict[str, Any]]]) -> tuple[str, str | None, str, list[dict[str, Any]]]:
    prefix, suffix = text[:match.start()].strip(), text[match.end():].strip()
    section = _SECTION.search(prefix)
    if section:
        return "SECTION", label_key(section.group()), "EXPLICIT_SECTION_CAPTION", []
    if _DOCUMENT.search(prefix) or re.fullmatch(r"(?:of|de)\s+" + _THIS_DOCUMENT + r"[.\s]*", suffix, re.IGNORECASE):
        return "DOCUMENT", None, "EXPLICIT_DOCUMENT_CAPTION", []
    witnesses = continuations.get(fact["page"], [])
    if _measured_footer(fact) and witnesses:
        return "DOCUMENT", None, "MEASURED_FOOTER_AND_EXPLICIT_DOCUMENT_CONTINUATION", witnesses
    return "UNKNOWN", None, "PRINTED_SCOPE_UNPROVEN", []


def assess_documentary_pagination(facts: list[dict[str, Any]], *, physical_pages: list[int], processed_pages: list[int]) -> dict[str, Any]:
    physical, processed = sorted(set(physical_pages)), sorted(set(processed_pages))
    missing_execution = sorted(set(physical) - set(processed))
    issues = ["PAGE_EXECUTION_INCOMPLETE"] if missing_execution else []
    continuations: dict[int, list[dict[str, Any]]] = defaultdict(list)
    for fact in facts:
        if _independent(fact) and _CONTINUATION.search(str(fact.get("text", ""))):
            continuations[fact["page"]].append(fact.get("ref", {}))
    observations: list[dict[str, Any]] = []
    ignored: list[dict[str, Any]] = []
    for fact in facts:
        text = str(fact.get("text", ""))
        for match in _COUNTER.finditer(text):
            if _CITATION.search(text[:match.start()]):
                ignored.append({"literal": match.group(), "physical_page": fact["page"],
                    "reason": "PAGINATION_CITATION_NOT_COUNTER", "source_refs": [fact.get("ref", {})]})
                continue
            scope, label, basis, witnesses = _scope(text, match, fact, continuations)
            independent = _independent(fact)
            number, total = int(match[1]), int(match[2])
            observation = {"literal": match.group(), "physical_page": fact["page"],
                "number": number, "total": total, "scope": scope, "scope_label": label,
                "source_refs": [fact.get("ref", {}), *witnesses], "independent": independent,
                "geometry_kind": fact.get("geometry_kind", (fact.get("ref") or {}).get("geometry_kind", "UNKNOWN")),
                "mechanism": basis, "status": "SUPPORTED"}
            if not independent:
                observation.update(status="UNVERIFIED", reason="PRINTED_PAGINATION_UNCORROBORATED")
                issues.append("PRINTED_PAGINATION_UNCORROBORATED")
            elif not (1 <= number <= total <= MAX_PRINTED_PAGES):
                reason = "PRINTED_PAGINATION_LIMIT" if total > MAX_PRINTED_PAGES else "PRINTED_PAGINATION_CONFLICT"
                observation.update(status="UNVERIFIED" if total > MAX_PRINTED_PAGES else "CONFLICT", reason=reason)
                issues.append(reason)
            elif scope == "UNKNOWN":
                observation.update(status="UNVERIFIED", reason="PRINTED_SCOPE_UNPROVEN")
                issues.append("PRINTED_SCOPE_UNPROVEN")
            observations.append(observation)
    groups: dict[tuple[str, str | None], list[dict[str, Any]]] = defaultdict(list)
    for observation in observations:
        if observation["status"] == "SUPPORTED":
            groups[(observation["scope"], observation["scope_label"])].append(observation)
    sequences: list[dict[str, Any]] = []
    for (scope, label), values in sorted(groups.items(), key=lambda item: str(item[0])):
        totals = {item["total"] for item in values}
        numbers = sorted({item["number"] for item in values})
        pages_by_number: dict[int, set[int]] = defaultdict(set)
        numbers_by_page: dict[int, set[int]] = defaultdict(set)
        for item in values:
            pages_by_number[item["number"]].add(item["physical_page"])
            numbers_by_page[item["physical_page"]].add(item["number"])
        conflict = len(totals) != 1 or any(len(pages) > 1 for pages in pages_by_number.values()) or any(len(nums) > 1 for nums in numbers_by_page.values())
        sequence_total = next(iter(totals)) if len(totals) == 1 else None
        missing = sorted(set(range(1, sequence_total + 1)) - set(numbers)) if sequence_total is not None else []
        # Missing markers alone are not missing original pages. A proven global
        # total exceeding all received pages, or a complete page assignment with
        # a logical gap, does prove an incomplete source document.
        all_assigned = set(numbers_by_page) == set(physical)
        incomplete = bool(sequence_total is not None and (sequence_total > len(physical) or scope == "DOCUMENT" and all_assigned and missing))
        if conflict:
            status = "CONFLICT"
            issues.append("PRINTED_PAGINATION_CONFLICT")
        elif incomplete:
            status = "INCOMPLETE"
            issues.append("DOCUMENT_SOURCE_INCOMPLETE" if scope == "DOCUMENT" else "SECTION_SOURCE_INCOMPLETE")
        elif not missing:
            status = "CONSISTENT"
        elif scope == "SECTION":
            # A valid local counter does not redefine the global page count.
            status = "PARTIALLY_OBSERVED"
        else:
            status = "UNVERIFIED"
            issues.append("PRINTED_SEQUENCE_UNVERIFIED")
        sequences.append({"scope": scope, "scope_label": label, "expected_total": sequence_total,
            "observed_numbers": numbers, "missing_logical_numbers": missing,
            "physical_pages": sorted(numbers_by_page), "status": status,
            "source_refs": [ref for item in values for ref in item["source_refs"]]})
    if any(issue in issues for issue in ("PRINTED_PAGINATION_CONFLICT",)):
        status = "CONFLICT"
    elif any(sequence["status"] == "INCOMPLETE" for sequence in sequences):
        status = "INCOMPLETE"
    elif issues:
        status = "UNVERIFIED"
    elif observations:
        status = "CONSISTENT"
    else:
        status = "NOT_OBSERVED"
    document_sequences = [item for item in sequences if item["scope"] == "DOCUMENT"]
    documentary_complete = bool(document_sequences and all(item["status"] == "CONSISTENT" for item in document_sequences)) if not issues else None
    return {"version": VERSION, "status": status, "physical_pages": physical,
        "processed_pages": processed, "missing_execution_pages": missing_execution,
        "observations": observations, "sequences": sequences, "ignored_mentions": ignored,
        "reasons": sorted(set(issues)), "documentary_complete": documentary_complete or None,
        "blocking": bool(issues)}
