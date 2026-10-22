"""Exact, append-only source confirmation of bounded header observations."""
import copy
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from .document_template_builder_rules import source_digest, valid_header_observation
from .operations_common import OperationError

DECISION = "SOURCE_HEADER_ONLY_NO_PRODUCTS_CONFIRMED"
REASONS = {"HEADER_UNVERIFIED", "CONTINUATION_CONTEXT_UNVERIFIED"}


class HeaderConfirmation(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    observation_id: str = Field(pattern=r"^[0-9a-f]{64}$")
    expected_observation_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    expected_payload_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    decision: Literal["SOURCE_HEADER_ONLY_NO_PRODUCTS_CONFIRMED"]
    source_note: str = Field(min_length=1, max_length=2000)

    @field_validator("source_note")
    @classmethod
    def nonempty_note(cls, value):
        if not value.strip():
            raise ValueError("Describe la comprobación del encabezado en el original.")
        return value.strip()


def reject_initial_resolutions(payload):
    if "header_resolutions" in payload:
        raise OperationError(409, "DOCUMENT_HEADER_INTEGRITY", "El resultado automático no puede contener confirmaciones humanas.")


def state_hash(payload):
    """Any later change to a value, origin, review flag or source invalidates consent."""
    return source_digest({key: value for key, value in payload.items() if key != "header_resolutions"})


def invalidate_confirmations(payload, actor_id, reviewed_at, reason):
    if not payload.get("header_resolutions"):
        return payload
    result = copy.deepcopy(payload)
    result["header_resolutions"].append({"event": "INVALIDATED", "actor_id": actor_id,
        "reviewed_at": reviewed_at, "reason": reason.strip()})
    return result


def cells_reviewed(payload):
    cells = [*payload.get("fields", {}).values(),
             *[cell for rows in payload.get("tables", {}).values() for row in rows for cell in row.values()]]
    return bool(cells) and all(cell.get("validity") == "VALID" and cell.get("reviewed") is True for cell in cells)


def observations(payload, definition):
    """Fail closed on every body region, unknown observation or duplicate identity."""
    found = {}
    source = payload.get("lines", [])
    source_hash, definition_hash = source_digest(source), source_digest(definition)
    for table, regions in payload.get("table_unparsed", {}).items():
        for region in regions:
            if (region.get("reason") not in REASONS
                or not valid_header_observation(region, source, definition, table,
                    source_hash=source_hash, definition_hash=definition_hash)):
                raise OperationError(422, "DOCUMENT_HEADER_BODY_UNRESOLVED", "Resuelve las regiones del cuerpo antes de confirmar encabezados.")
            observation = region["header_observation"]
            identity = observation["observation_id"]
            if identity in found:
                raise OperationError(422, "DOCUMENT_HEADER_DUPLICATE", "La observación de origen no es única.")
            found[identity] = region
    return found


def assert_original_source(payload, automatic):
    reject_initial_resolutions(automatic)
    for name in ("lines", "pages", "page_geometry", "table_unparsed"):
        if payload.get(name) != automatic.get(name):
            raise OperationError(409, "DOCUMENT_HEADER_SOURCE_CHANGED", "La fuente y sus observaciones deben conservarse íntegras.")
    # Header confirmation cannot invent/remove/reorder table cells. Human value
    # correction is supported by the existing review API, with original evidence.
    if set(payload.get("tables", {})) != set(automatic.get("tables", {})):
        raise OperationError(409, "DOCUMENT_HEADER_SOURCE_CHANGED", "Cambió la estructura de la extracción.")
    for name, rows in automatic.get("tables", {}).items():
        current = payload["tables"][name]
        if len(rows) != len(current) or any(set(a) != set(b) for a, b in zip(rows, current, strict=True)):
            raise OperationError(409, "DOCUMENT_HEADER_SOURCE_CHANGED", "Las filas originales deben permanecer completas.")
    originals = [automatic.get("fields", {}), *[row for rows in automatic.get("tables", {}).values() for row in rows]]
    current = [payload.get("fields", {}), *[row for rows in payload.get("tables", {}).values() for row in rows]]
    for first, final in zip(originals, current, strict=True):
        if set(first) != set(final):
            raise OperationError(409, "DOCUMENT_HEADER_SOURCE_CHANGED", "Cambió el conjunto de celdas original.")
        for name, cell in first.items():
            reviewed = final[name]
            original_origin = cell.get("observed_origin", cell.get("origin"))
            if (reviewed.get("observed") != cell.get("observed")
                or reviewed.get("observed_origin", reviewed.get("origin")) != original_origin):
                raise OperationError(409, "DOCUMENT_HEADER_SOURCE_CHANGED", "La evidencia observada de cada celda debe conservarse.")


def append_confirmations(payload, automatic, definition, confirmations, source_binding, actor_id, reviewed_at, reason):
    """Validate the complete request before returning a new payload; never mutate inputs."""
    if not confirmations:
        return payload
    assert_original_source(payload, automatic)
    if not cells_reviewed(payload):
        raise OperationError(422, "DOCUMENT_HEADER_CELLS_REQUIRED", "Guarda primero todos los valores válidos y confirmados contra el original.")
    if any(check.get("status") != "VALID" for check in payload.get("arithmetic", [])):
        raise OperationError(422, "DOCUMENT_HEADER_CELLS_REQUIRED", "Resuelve las comprobaciones aritméticas antes del encabezado.")
    available = observations(payload, definition)
    known = observations(automatic, definition)
    supplied = [HeaderConfirmation.model_validate(value).model_dump() for value in confirmations]
    if len(supplied) > 50 or len({value["observation_id"] for value in supplied}) != len(supplied):
        raise OperationError(422, "DOCUMENT_HEADER_DUPLICATE", "Confirma hasta 50 observaciones distintas.")
    if source_binding.get("definition_hash") != source_digest(definition):
        raise OperationError(409, "DOCUMENT_HEADER_SOURCE_CHANGED", "La definición de origen cambió.")
    result = copy.deepcopy(payload)
    records = copy.deepcopy(payload.get("header_resolutions", []))
    payload_hash, reviewed_hash = source_digest(payload), state_hash(payload)
    binding_hash = source_digest(source_binding)
    for value in supplied:
        identity = value["observation_id"]
        region = available.get(identity)
        if (not region or region != known.get(identity)
            or value["expected_payload_hash"] != payload_hash
            or value["expected_observation_hash"] != source_digest(region["header_observation"])):
            raise OperationError(409, "DOCUMENT_HEADER_CONFLICT", "La observación o revisión cambió. Recarga el original antes de confirmar.")
        records.append({**value, "event": "CONFIRMED", "actor_id": actor_id, "reviewed_at": reviewed_at, "reason": reason.strip(),
                        "source_binding": copy.deepcopy(source_binding), "source_binding_hash": binding_hash,
                        "reviewed_state_hash": reviewed_hash})
    # The header validator proves its local band, not body completeness. Only
    # current, otherwise valid requests reach this one confined RAW extraction.
    from .document_builder_executor import verify_automatic_payload
    if not verify_automatic_payload(automatic, definition):
        raise OperationError(409, "DOCUMENT_HEADER_BODY_INTEGRITY", "El cuerpo automático no coincide con su fuente completa.")
    result["header_resolutions"] = records
    return result


def header_review_dto(payload, definition, *, reviewed=False, source_binding=None):
    if not any(region.get("header_observation") for regions in payload.get("table_unparsed", {}).values() for region in regions):
        return None
    eligible = cells_reviewed(payload) and all(check.get("status") == "VALID" for check in payload.get("arithmetic", []))
    try:
        available = observations(payload, definition)
    except OperationError:
        available, eligible = {}, False
    resolved = resolved_observations(payload, definition, reviewed=reviewed, source_binding=source_binding,
                                     available=available)
    return {"payload_hash": source_digest(payload), "eligible": eligible,
            "observations": [{"observation_id": identity, "observation_hash": source_digest(region["header_observation"]),
                "table": region["header_observation"]["table"], "page": region["page"], "region": region["region"], "reason": region["reason"], "text": region["text"],
                "resolved": identity in resolved}
                for identity, region in available.items()]}


def resolved_observations(payload, definition, *, reviewed=False, source_binding=None, available=None):
    if not reviewed or not cells_reviewed(payload):
        return set()
    try:
        found = observations(payload, definition) if available is None else available
        current_hash = state_hash(payload)
        definition_hash = source_digest(definition)
        hashes = {identity: source_digest(region["header_observation"]) for identity, region in found.items()}
        resolved = set()
        for record in reversed(payload.get("header_resolutions", [])):
            if record.get("event") == "INVALIDATED":
                break
            binding = record["source_binding"]
            identity = record["observation_id"]
            if (identity in hashes and record["expected_observation_hash"] == hashes[identity]
                and record["decision"] == DECISION and record["source_note"].strip()
                and record["actor_id"] and record["reviewed_at"] and record["reason"].strip()
                and record["reviewed_state_hash"] == current_hash
                and binding["definition_hash"] == definition_hash
                and record["source_binding_hash"] == source_digest(binding)
                and (source_binding is None or binding == source_binding)):
                resolved.add(identity)
        return resolved
    except (KeyError, TypeError, ValueError, OperationError):
        return set()


def resolved_observation(payload, definition, region, *, reviewed=False, source_binding=None):
    return region.get("header_observation", {}).get("observation_id") in resolved_observations(
        payload, definition, reviewed=reviewed, source_binding=source_binding)
