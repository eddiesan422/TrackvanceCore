"""Conservative semantic observation annotations; never independent attestation."""
from __future__ import annotations

import copy
import re
from decimal import Decimal
from typing import Any

from .document_normalization import canonical_hash, normalize_observation

VERSION = "SEMANTIC_OBSERVATIONS_V1"
CONTEXT_KEYS = {"literal_context", "basis", "polarity"}
BASES = {"PRINTED", "CALCULATED", "INFERRED", "NOT_ON_PAGE", "UNREADABLE", "UNCERTAIN"}
POLARITIES = {"AFFIRMATIVE", "NEGATED", "CONDITIONAL", "UNKNOWN", "NOT_APPLICABLE"}


def _standalone_placeholder(observed):
    if not isinstance(observed, str):
        return False
    match = re.fullmatch(r"\[([^\[\]\r\n]+)\]", observed.strip())
    return bool(match and " ".join(match[1].casefold().split()) in {
        "not provided", "not supplied", "not available", "no proporcionado", "no suministrado",
        "sin informar", "sin proporcionar", "no disponible"})


def _placeholder_context(observation):
    """Disqualify an explicit empty-form proposal; never delete marker text.

    A marker embedded in a sentence, identifier or name is ordinary source text.
    The self-reported label/marker context can block acceptance, never attest
    documentary absence or authorize publication.
    """
    observed = observation["observed"]
    context = observation.get("model_context") or {}
    if (not _standalone_placeholder(observed) or context.get("basis") != "PRINTED"
            or context.get("polarity") != "NEGATED" or observation.get("ambiguous")
            or observation.get("alternatives")):
        return False
    quote = context.get("literal_context")
    return bool(quote and re.fullmatch(r"[^:\r\n]{1,160}:\s*" + re.escape(observed.strip()), quote.strip()))


def _possible_placeholder_context(observation):
    """A short marker-only quote cannot establish a substantive field value.

    The model's polarity is not authority to accept it or prove document-wide
    absence. A legitimate name or identifier can be resolved by source review.
    """
    context = observation.get("model_context") or {}
    observed = observation["observed"]
    quote = context.get("literal_context")
    return bool(_standalone_placeholder(observed) and context.get("basis") == "PRINTED"
        and isinstance(quote, str) and (quote.strip() == observed.strip()
            or re.fullmatch(r"[^:\r\n]{1,160}:\s*" + re.escape(observed.strip()), quote.strip())))


def _context_contains_literal(observed, quote, field):
    if observed in quote:
        return True
    policy = field.get("normalization") or {}
    if field["logical_type"] != "STRING" or policy.get("text_linebreaks") != "DECLARED_MULTILINE_V1":
        return False
    # Use only the text equivalence declared in this contract, on both sides.
    # This sanity check still grants no independent authority to a model quote.
    literal, status, _ = normalize_observation(observed, field)
    context, context_status, _ = normalize_observation(quote, field)
    return bool(status == context_status == "VALID" and literal and literal in context)


def _comparable(raw, specification):
    value, validity, _trace = normalize_observation(raw, specification)
    if validity == "VALID" and specification["logical_type"] == "DECIMAL" and value is not None:
        value = Decimal(value)
    return value, validity


def validate_model_context(value):
    if (not isinstance(value, dict) or set(value) != CONTEXT_KEYS
            or not isinstance(value["basis"], str) or value["basis"] not in BASES
            or not isinstance(value["polarity"], str) or value["polarity"] not in POLARITIES
            or value["literal_context"] is not None and (not isinstance(value["literal_context"], str)
                or len(value["literal_context"]) > 16000)):
        raise ValueError("SEMANTIC_OBSERVATION_CONTEXT")
    return value


def annotate_semantic_payload(payload, definition):
    """Retain every source occurrence, including nulls, alternatives and rejected roles.

    Self-reported quotes and roles may disqualify a proposal. They cannot prove
    that the literal or association exists in the original, or approve a value.
    """
    metadata = definition.get("semantic_general")
    if not metadata:
        return payload
    observations: list[dict[str, Any]] = []
    by_path: dict[str, list[dict[str, Any]]] = {}
    profile = metadata["contract"]
    collections = {table["name"]: table for table in profile["tables"]}
    specifications = {("headers", field["name"]): field for field in definition["fields"]}
    specifications.update({(table["name"], field["name"]): field for table in definition["tables"] for field in table["fields"]})
    for page in payload.get("document_representation", {}).get("pages", []):
        selection = page.get("selection")
        if not selection:
            continue
        ordinal = page["page_one_based"]
        rows = [("headers", 0, selection.get("fields", {}))]
        for name, values in selection.get("tables", {}).items():
            rows.extend((name, index, row) for index, row in enumerate(values))
        for name, row, values in rows:
            for field, raw in values.items():
                context = copy.deepcopy(raw.get("semantic_context"))
                if context is not None:
                    validate_model_context(context)
                observation = {"source_page": ordinal, "stage": page["stage"], "structure": name,
                    "source_row_ordinal": row if name != "headers" else None, "field": field,
                    "observed": raw.get("observed"), "ambiguous": raw.get("ambiguous", False),
                    "alternatives": copy.deepcopy(raw.get("alternatives", [])),
                    "model_context": context, "context_authority": "MODEL_SELF_REPORT_NOT_INDEPENDENT_EVIDENCE",
                    "page_evidence_id": page["id"], "geometry_kind": "PAGE_LOCATOR_ONLY"}
                observation["observation_id"] = canonical_hash(observation)[:32]
                observations.append(observation)
                if name == "headers":
                    by_path.setdefault(f"headers.0.{field}", []).append(observation)
    # Rows are source occurrences across pages. Never collapse by text or SKU.
    for name, rows in payload.get("tables", {}).items():
        provenance = payload.get("row_provenance", {}).get(name, [])
        page_ordinals: dict[int | None, int] = {}
        for index, row in enumerate(rows):
            source = provenance[index] if index < len(provenance) else {}
            page = source.get("source_page")
            ordinal = page_ordinals.get(page, 0)
            page_ordinals[page] = ordinal + 1
            for field in row:
                by_path[f"{name}.{index}.{field}"] = [item for item in observations if item["structure"] == name
                    and item["field"] == field and item["source_page"] == page and item["source_row_ordinal"] == ordinal]
    guards: list[dict[str, Any]] = []
    scopes = [("headers", [payload["fields"]]), *payload.get("tables", {}).items()]
    for name, rows in scopes:
        for index, row in enumerate(rows):
            for field, cell in row.items():
                path = f"{name}.{index}.{field}"
                occurrences = by_path.get(path, [])
                reasons = []
                for observation in occurrences:
                    context = observation["model_context"] or {}
                    if context.get("basis") in {"CALCULATED", "INFERRED"} and observation["observed"] is not None:
                        reasons.append("PROPOSED_VALUE_IS_NOT_A_PRINTED_OBSERVATION")
                    if context.get("basis") in {"UNREADABLE", "UNCERTAIN"}:
                        reasons.append("UNRESOLVED_SOURCE_OBSERVATION")
                    if context.get("basis") == "NOT_ON_PAGE" and observation["observed"] is not None:
                        reasons.append("VALUE_CONTRADICTS_REPORTED_LOCAL_ABSENCE")
                    if (name in collections and collections[name]["kind"] == "COLLECTION"
                            and observation["observed"] is not None
                            and context.get("polarity") in {"NEGATED", "CONDITIONAL"}):
                        reasons.append("NONAFFIRMATIVE_NARRATIVE_PROPOSAL")
                    quote = context.get("literal_context")
                    if (quote and observation["observed"] and not _context_contains_literal(
                            observation["observed"], quote, specifications[(name, field)])):
                        reasons.append("MODEL_CONTEXT_DOES_NOT_CONTAIN_LITERAL")
                    if observation["observed"] is not None and observation["alternatives"]:
                        specification = specifications[(name, field)]
                        selected = _comparable(observation["observed"], specification)
                        if any(_comparable(alternative, specification) != selected for alternative in observation["alternatives"]):
                            reasons.append("CONTRADICTORY_SOURCE_ALTERNATIVES")
                    if _placeholder_context(observation):
                        reasons.append("EXPLICIT_ABSENCE_MARKER_REQUIRES_SOURCE_REVIEW")
                    elif _possible_placeholder_context(observation):
                        reasons.append("POSSIBLE_ABSENCE_MARKER_REQUIRES_SOURCE_REVIEW")
                cell["semantic_observation_refs"] = [item["observation_id"] for item in occurrences]
                cell["semantic_value_state"] = "OBSERVED_PENDING_VERIFICATION" if cell.get("observed") is not None else "PENDING_OR_ABSENT"
                if reasons:
                    cell["value"], cell["validity"] = None, "AMBIGUOUS"
                    markers = [item for item in occurrences if _placeholder_context(item)]
                    possible = [item for item in occurrences if not _placeholder_context(item)
                        and _possible_placeholder_context(item)]
                    if markers or possible:
                        cell["semantic_absence"] = {"reason": "CONTEXTUAL_PLACEHOLDER_V1" if markers else "POSSIBLE_PLACEHOLDER_V1",
                            "observed_preserved": True, "documentary_absence_verified": False,
                            "observation_refs": [item["observation_id"] for item in [*markers, *possible]]}
                        if (specifications[(name, field)].get("required", True)
                                and all(item["observed"] is None or _placeholder_context(item) for item in occurrences)
                                and set(reasons) == {"EXPLICIT_ABSENCE_MARKER_REQUIRES_SOURCE_REVIEW"}):
                            cell["validity"] = "MISSING"
                    conflict = f"FIELD:{field}" if name == "headers" else f"CELL:{name}.{index}.{field}"
                    payload.setdefault("semantic_conflicts", []).append(conflict)
                    guards.append({"path": path, "reasons": list(dict.fromkeys(reasons)),
                        "observation_refs": list(cell["semantic_observation_refs"])})
    # Duplicate proposals remain visible and require an authorized table review;
    # equality is a warning, never grounds to delete a possibly legitimate row.
    for name, rows in payload.get("tables", {}).items():
        seen: dict[str, int] = {}
        for index, row in enumerate(rows):
            key = canonical_hash({field: cell.get("observed") for field, cell in row.items()})
            if key in seen:
                payload.setdefault("semantic_conflicts", []).append("TABLE:" + name)
                guards.append({"path": name, "reasons": ["REPEATED_ROW_PROPOSAL_REQUIRES_SOURCE_REVIEW"],
                    "row_ordinals": [seen[key], index], "rows_preserved": True})
            else:
                seen[key] = index
    payload["semantic_conflicts"] = list(dict.fromkeys(payload.get("semantic_conflicts", [])))
    payload["semantic_evidence"] = {"version": VERSION, "mode": metadata["mode"],
        "profile_sha256": metadata["profile_sha256"], "profile_revision": metadata["profile_revision"],
        "observations": observations, "guards": guards, "verification": "AUTHORIZED_REVIEW_REQUIRED",
        "model_labels_are_independent_evidence": False, "arithmetic_proves_printed_values": False,
        "document_context_preserved_in_ir": True,
        "projections": [{"structure": "headers", "kind": "HEADER"},
            *[{"structure": table["name"], "kind": table["kind"]} for table in profile["tables"]]]}
    return payload


def semantic_review_issues(payload, definition, *, reviewed=False, source_sha256=None):
    if not definition.get("semantic_general"):
        return []
    result = []
    for table in definition["tables"]:
        name = table["name"]
        rows = payload["tables"].get(name, [])
        receipt: dict[str, Any] = next((item for item in reversed(payload.get("semantic_table_reviews", []))
            if item.get("table") == name), {})
        confirmed = bool(reviewed and receipt.get("scope") == "ALL_SOURCE_ROWS_OR_CONFIRMED_ABSENCE"
            and receipt.get("rows_sha256") == canonical_hash(rows)
            and receipt.get("definition_sha256") == canonical_hash(definition)
            and receipt.get("source_sha256") == source_sha256
            and isinstance(receipt.get("actor_id"), str) and receipt.get("actor_id")
            and all(cell.get("reviewed") for row in rows for cell in row.values()))
        if not confirmed:
            result.append("SEMANTIC_TABLE_COVERAGE_REVIEW_REQUIRED:" + name)
    return result


def record_table_reviews(payload, definition, names, *, source_sha256, actor_id, reason, created_at):
    if not names:
        return
    from .operations_common import OperationError

    declared = {table["name"] for table in definition["tables"]}
    if not definition.get("semantic_general") or len(names) != len(set(names)) or set(names) - declared:
        raise OperationError(422, "DOCUMENT_SEMANTIC_TABLE_REVIEW", "Confirma sólo colecciones semánticas declaradas y sin duplicados.")
    for name in names:
        rows = payload["tables"].get(name, [])
        if any(cell.get("reviewed") is not True or cell.get("validity") != "VALID" for row in rows for cell in row.values()):
            raise OperationError(422, "DOCUMENT_SEMANTIC_TABLE_REVIEW", "Revisa y confirma todos los valores antes de acreditar la cobertura de la colección.")
        payload.setdefault("semantic_table_reviews", []).append({"table": name,
            "scope": "ALL_SOURCE_ROWS_OR_CONFIRMED_ABSENCE", "rows_sha256": canonical_hash(rows),
            "definition_sha256": canonical_hash(definition), "source_sha256": source_sha256,
            "actor_id": actor_id, "reason": reason.strip(), "created_at": created_at})


def semantic_review_dto(extraction, definition):
    if not definition.get("semantic_general"):
        return None
    payload = extraction.payload
    pending = semantic_review_issues(payload, definition, reviewed=extraction.reviewed,
        source_sha256=extraction.engine.get("source_sha256"))
    states = []
    for name, rows in [("headers", [payload["fields"]]), *payload["tables"].items()]:
        for index, row in enumerate(rows):
            for field, cell in row.items():
                state = ("AUTHORIZED_CORRECTION" if cell.get("corrected") else "VERIFIED_BY_AUTHORIZED_REVIEW") if cell.get("reviewed") else (
                    "NORMALIZED_PENDING_VERIFICATION" if cell.get("validity") == "VALID" and cell.get("value") is not None
                    else "OBSERVED_PENDING_RESOLUTION" if cell.get("observed") is not None else "PENDING_OR_ABSENT")
                states.append({"path": f"{name}.{index}.{field}", "state": state,
                    "observed": cell.get("observed"), "normalized": cell.get("value"),
                    "verified": cell.get("value") if cell.get("reviewed") else None,
                    "evidence_refs": list(cell.get("evidence_refs", []))})
    return {"mode": "SEMANTIC_GENERAL_V1", "automatic_approval": False, "values": states,
        "tables": [{"name": table["name"],
            "review_required": "SEMANTIC_TABLE_COVERAGE_REVIEW_REQUIRED:" + table["name"] in pending,
            "latest_confirmation": next((copy.deepcopy(item) for item in reversed(payload.get("semantic_table_reviews", []))
                if item.get("table") == table["name"]), None)} for table in definition["tables"]]}
