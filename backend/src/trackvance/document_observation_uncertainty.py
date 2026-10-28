"""Versioned treatment of missing observations and retained source uncertainty.

These decisions do not create independent evidence or an automatic approval.
An actual observation is still required before equivalent hypotheses can resolve.
"""
from __future__ import annotations

from decimal import Decimal

from .document_normalization import normalize_observation

OBSERVATION_POLICY_VERSION = "TRACKVANCE_OBSERVATION_UNCERTAINTY_098_V3"


def empty_observation(value):
    return value is None or isinstance(value, str) and not value.strip()


def relevant_alternatives(values):
    return [value for value in values if isinstance(value, str) and value.strip()]


def null_uncertainty(observed, ambiguous=False, alternatives=()):
    return empty_observation(observed) and bool(ambiguous or relevant_alternatives(alternatives))


def uncertainty_trace(*, unresolved, reason, source_ambiguous=False, signal_count=1):
    return {"version": OBSERVATION_POLICY_VERSION, "unresolved": bool(unresolved),
        "reason": reason, "source_ambiguous": bool(source_ambiguous), "signal_count": signal_count,
        "grants_independent_corroboration": False}


def unresolved_uncertainty(cell):
    if not isinstance(cell, dict):
        return False
    trace = cell.get("observation_policy")
    if isinstance(trace, dict) and trace.get("version") == OBSERVATION_POLICY_VERSION:
        return trace.get("unresolved") is not False
    return cell.get("validity") == "AMBIGUOUS"


def null_observations(cell):
    """Recover only relevant retained null source records, never excluded context."""
    records = []
    for item in cell.get("candidate_alternatives", []):
        if not isinstance(item, dict) or item.get("eligible_for_field") is False:
            continue
        item = item.get("candidate", item)
        if null_uncertainty(item.get("observed"), item.get("ambiguous", False), item.get("alternatives", [])):
            records.append(item)
    if not records and empty_observation(cell.get("observed")) and unresolved_uncertainty(cell):
        trace = cell.get("observation_policy") or {}
        records.append({"observed": cell.get("observed"),
            "ambiguous": trace.get("source_ambiguous", True),
            "alternatives": relevant_alternatives(cell.get("candidate_alternatives", []))})
    return records


def null_alternatives_equivalent(observed, field, records):
    """Resolve explicit hypotheses only through an already-declared scalar rule.

    An empty/illegible hypothesis or an ambiguity flag without hypotheses stays
    unresolved. Exact repeats without a declared normalization are insufficient.
    """
    if empty_observation(observed) or not records or field.get("normalization") is None:
        return False
    expected, status, trace = normalize_observation(observed, field)
    if status != "VALID" or expected is None or not trace.get("applied_rules"):
        return False
    def comparable(value):
        return Decimal(value) if field["logical_type"] == "DECIMAL" else value
    for record in records:
        alternatives = record.get("alternatives", [])
        if (record.get("context_unresolved") or not empty_observation(record.get("observed")) or not alternatives
                or len(relevant_alternatives(alternatives)) != len(alternatives)):
            return False
        for alternative in alternatives:
            value, validity, _ = normalize_observation(alternative, field)
            if validity != "VALID" or value is None or comparable(value) != comparable(expected):
                return False
    return True
