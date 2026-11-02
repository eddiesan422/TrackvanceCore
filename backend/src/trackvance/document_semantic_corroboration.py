"""Opt-in source corroboration, with independently inventoried source occurrences.

Only the coordinator calls this module, after the existing recognition journal
has registered the original bytes through StorageProvider. A provider proposal,
quote, confidence or row count has no authority here. Historical evidence
contracts do not call this validator.
"""
from __future__ import annotations

import copy
import hashlib
import json
import math
import re
import statistics
from collections import Counter, defaultdict
from decimal import Decimal, InvalidOperation
from itertools import pairwise
from typing import Any, cast

from .document_normalization import canonical_hash, normalization_matches, normalize_observation
from .document_representation import decode_artifact, representation_from_events, stable_id

VALIDATOR_VERSION = "TRACKVANCE_SEMANTIC_EVIDENCE_098_V1"
ASSOCIATION_CONTRACT = "SEMANTIC_SOURCE_ASSOCIATION_V1"
MAX_SOURCE_BYTES = 128 * 1024**2

# This is a versioned vocabulary of semantic roles, not document layouts, values
# or provider identities. Unknown labels remain review exceptions. These aliases
# are never sent to a recognizer as examples or selected after seeing a holdout.
ROLE_ALIASES = {
    "invoice_number": ["invoice number", "invoice no", "invoice #", "invoice", "factura", "número de factura", "numero factura", "factura no"],
    "issuer": ["issuer", "supplier", "vendor", "seller", "issued by", "from", "emisor", "proveedor", "vendedor", "emitido por"],
    "buyer": ["buyer", "customer", "client", "bill to", "sold to", "comprador", "cliente", "adquirente", "facturar a"],
    "issue_date": ["issue date", "invoice date", "date issued", "fecha de emisión", "fecha de emision", "fecha factura"],
    "currency": ["currency", "moneda", "divisa"],
    "subtotal": ["subtotal", "sub total", "net amount", "net subtotal", "base imponible"],
    "tax": ["tax", "taxes", "vat", "iva", "impuesto", "impuestos"],
    "total": ["total", "total due", "amount due", "grand total", "total a pagar", "importe total", "total factura"],
    "description": ["description", "item description", "concept", "details", "descripción", "descripcion", "concepto", "detalle"],
    "item_code": ["item code", "product code", "code", "sku", "código", "codigo", "referencia artículo"],
    "quantity": ["quantity", "qty", "cantidad", "cant"],
    "unit_price": ["unit price", "unit cost", "rate", "price", "precio unitario", "precio", "tarifa"],
    "amount": ["amount", "line amount", "line total", "importe", "importe de línea", "importe con signo", "valor", "monto"],
    "account": ["account", "account number", "cuenta", "número de cuenta"],
    "holder": ["holder", "account holder", "titular"],
    "period": ["period", "statement period", "período", "periodo"],
    "opening_balance": ["opening balance", "initial balance", "saldo inicial"],
    "closing_balance": ["closing balance", "ending balance", "saldo final"],
    "balance": ["balance", "saldo"],
    "date": ["date", "transaction date", "fecha", "fecha movimiento"],
    "certificate_id": ["certificate id", "certificate number", "identificador del certificado", "número de certificado"],
    "subject": ["subject", "certified subject", "persona o entidad certificada", "sujeto"],
    "statement": ["statement", "certification", "certifica", "afirmación certificada"],
}
EVIDENCE_ONLY_ROLES = frozenset({"source_quote"})
ASSOCIATION_CONTRACT_SHA256 = canonical_hash({"version": ASSOCIATION_CONTRACT,
    "roles": ROLE_ALIASES, "evidence_only": sorted(EVIDENCE_ONLY_ROLES),
    "native_rows": "MEASURED_WORD_BANDS_AND_SEPARATED_COLUMNS_V1",
    "paddle_rows": "COMPLETE_LOGICAL_HTML_GRID_V1", "headerless_continuation": "REVIEW"})
# Hash of the existing general prompt, normalized only for source-file CRLF.
# Pinning prevents a second input containing another recognizer's observations
# from being represented as the current independent image-only producer.
GENERAL_PROMPT_SHA256 = "5d880d3d3cfb4c92f3a374e4ff0bd0522031734e855cb514498ade14a240f8bf"


def semantic_source_context(events, base_context):
    """Retain exact hydrated journal bodies supplied by the privileged worker.

    ``base_context`` comes from registered Artifact rows, never an RPC response.
    All body hashes and registry references are checked again when certifying.
    """
    result = copy.deepcopy(base_context)
    result["recognition_events"] = copy.deepcopy(events)
    result["semantic_source_contract"] = ASSOCIATION_CONTRACT
    return result


def _cell_hash(cell):
    return canonical_hash({key: value for key, value in cell.items() if key != "reviewed"})


def semantic_table_hash(rows):
    return canonical_hash([{name: {key: value for key, value in cell.items() if key != "reviewed"}
        for name, cell in row.items()} for row in rows])


def _key(value):
    from .document_header_association import label_key

    return label_key(value)


def _aliases(field):
    return list(dict.fromkeys([field["label"], field["name"].replace("_", " "),
        *ROLE_ALIASES.get(field["name"], []), *field.get("aliases", [])]))


def _box(region):
    return bool(isinstance(region, list) and len(region) == 4
        and all(type(v) in (int, float) and math.isfinite(v) and 0 <= v <= 1 for v in region)
        and region[0] < region[2] and region[1] < region[3])


def _normalized(raw, field):
    value, status, _ = normalize_observation(raw, field)
    return (value, status)


def _comparison(value, field):
    if value is None:
        return None
    if field["logical_type"] == "DECIMAL":
        try:
            decimal = Decimal(value)
            if not decimal.is_finite():
                return ["UNRESOLVED_DECIMAL", canonical_hash(value)]
            parts = decimal.as_tuple()
        except (InvalidOperation, TypeError, ValueError):
            return ["UNRESOLVED_DECIMAL", canonical_hash(value)]
        digits, exponent = list(parts.digits), cast(int, parts.exponent)
        while len(digits) > 1 and digits[-1] == 0:
            digits.pop()
            exponent += 1
        if not any(digits):
            return ["DECIMAL", 0, [0], 0]
        return ["DECIMAL", parts.sign, digits, exponent]
    if field["logical_type"] == "INT64":
        try:
            return int(value)
        except (TypeError, ValueError):
            return ["UNRESOLVED_INT64", canonical_hash(value)]
    return value


def _valid_cell(cell, field):
    from .document_observation_uncertainty import unresolved_uncertainty

    return bool(isinstance(cell, dict) and not unresolved_uncertainty(cell)
        and cell.get("validity") == "VALID" and normalization_matches(cell, field))


def _ref(identity, page, source_id, *, geometry_kind="UNKNOWN", artifact_sha256=None):
    return {"id": source_id, "page": page, "stage": identity["stage"],
        "mechanism": identity["mechanism"], "geometry_kind": geometry_kind,
        "artifact_sha256": artifact_sha256, "event_hashes": identity.get("event_hashes", [])}


def _authenticate(data, definition, source_sha256, context, check):
    """Rebuild from original bytes; parsed IR and declared stage names are not proof."""
    from .document_evidence_v3 import _bound_stage
    from .document_lossless import map_representation

    errors, controls = [], []
    engine, payload = data["engine"], data["payload"]
    configuration = engine.get("configuration", {})
    if ((definition.get("semantic_general") or {}).get("automatic_verification") != "SEMANTIC_AUTO_V1"
            or (definition.get("validation_policy") or {}).get("version") != "SEMANTIC_EVIDENCE_V1"
            or (definition.get("semantic_general") or {}).get("association_contract") != ASSOCIATION_CONTRACT):
        errors.append("SEMANTIC_AUTO_CONTRACT_REQUIRED")
    if (source_sha256 != engine.get("source_sha256") or source_sha256 != context.get("source_sha256")
            or engine.get("configuration_sha256") != canonical_hash(configuration)
            or engine.get("definition_sha256") != canonical_hash(definition)):
        errors.append("SOURCE_CONFIGURATION_BINDING")
    events = context.get("recognition_events")
    if not isinstance(events, list) or not 1 <= len(events) <= 1000:
        return None, [], [*errors, "SOURCE_EVIDENCE_UNAVAILABLE"]
    if len(json.dumps(events, ensure_ascii=False, allow_nan=False).encode()) > MAX_SOURCE_BYTES:
        return None, [], [*errors, "SOURCE_EVIDENCE_LIMIT"]
    registered = context.get("artifacts", [])
    persisted = set(context.get("persisted_event_hashes", []))
    event_hashes = []
    try:
        for event in events:
            check()
            hashed = canonical_hash({key: value for key, value in event.items()
                if key not in {"event_sha256", "durable_refs"}})
            if hashed != event.get("event_sha256") or hashed not in persisted:
                errors.append("SOURCE_EVENT_INTEGRITY")
            event_hashes.append(hashed)
            receipt = event.get("durable_refs", {})
            if (not receipt.get("manifest_artifact_id") or receipt.get("event_sha256") != hashed
                    or receipt.get("source_sha256", source_sha256) != source_sha256):
                errors.append("SOURCE_REGISTRY_BINDING")
            for artifact in event.get("payload", {}).get("artifacts", []):
                decode_artifact(artifact)
                matches = [item for item in receipt.get("artifacts", []) if item.get("kind") == artifact["kind"]
                    and item.get("sha256") == artifact["sha256"] and item.get("bytes", artifact["bytes"]) == artifact["bytes"]
                    and item.get("stage") == event["stage"] and item.get("page", item.get("page_one_based")) == event.get("page_one_based")]
                if len(matches) != 1 or not matches[0].get("artifact_id") or matches[0] not in registered:
                    errors.append("SOURCE_REGISTRY_BINDING")
        if len(set(event_hashes)) != len(event_hashes) or engine.get("recognition_event_hashes") != event_hashes:
            errors.append("SOURCE_EVENT_INTEGRITY")
        bindings = engine.get("recognition_bindings")
        ir = representation_from_events(events, source_sha256=source_sha256,
            configuration_sha256=engine.get("recognition_configuration_sha256"),
            expected_pages=context.get("expected_pages"),
            **({"approved_bindings": bindings} if bindings else {}))
        if canonical_hash(ir) != canonical_hash(payload.get("document_representation")):
            errors.append("SOURCE_IR_INTEGRITY")
        if (ir.get("expected_pages") != context.get("expected_pages") or context.get("resources_released") is not True
                or engine.get("execution", {}).get("recognition_complete") is not True
                or engine.get("execution", {}).get("resources_released") is not True
                or engine.get("execution", {}).get("pending_pages")):
            errors.append("RECOGNITION_INCOMPLETE_OR_RESOURCES_UNVERIFIED")
        for stage in dict.fromkeys(page["stage"] for page in ir["pages"]):
            identity = _bound_stage(data, stage, context, definition)
            if identity is None:
                errors.append("SOURCE_REGISTRY_BINDING")
            else:
                controls.append({"identity": identity, "payload": map_representation(ir, definition, stage=stage)})
    except (KeyError, TypeError, ValueError, OverflowError):
        return None, [], sorted({*errors, "SOURCE_ARTIFACT_INTEGRITY"})
    return ir, controls, sorted(set(errors))


def _qwen_independent(control, events, definition):
    """Validate exact effective image-only request, not a family-name assertion."""
    identity = control["identity"]
    if identity["mechanism"] != "QWEN":
        return False
    profile = definition["semantic_general"]["contract"]
    expected_semantics = {"semantic_definition_version": "SEMANTIC_GENERAL_V1",
        "reading_mode": "VERBATIM_OBSERVED_VALUES_NOT_NORMALIZED_OR_EXPECTED_ANSWERS",
        "profile_id": profile["id"], "profile_revision": profile["revision"],
        "profile_sha256": definition["semantic_general"]["profile_sha256"],
        "document_type": profile["document_type"], "description": profile["description"],
        "fields": profile["fields"], "tables": profile["tables"]}
    selected = [event for event in events if event.get("stage") == identity["stage"] and event.get("kind") == "PAGE"]
    try:
        for page in selected:
            records = [(event, artifact) for event in events if event.get("page_one_based") == page["page_one_based"]
                and event.get("request_id") == page["request_id"] for artifact in event.get("payload", {}).get("artifacts", [])
                if artifact.get("kind") == "QWEN_REQUEST_JSON"]
            if len(records) != 1:
                return False
            request = json.loads(decode_artifact(records[0][1]))
            body = request["request_without_image_bytes"]
            if (request.get("version") != "TRACKVANCE_QWEN_REQUEST_V1" or request.get("strategy") != "QWEN_VISUAL"
                    or any(request.get(key) != page.get(key) for key in ("source_sha256", "definition_sha256", "configuration_sha256"))
                    or [item.get("role") for item in body["messages"]] != ["system", "user"]
                    or any(set(item) != {"role", "content"} for item in body["messages"])
                    or hashlib.sha256(body["messages"][0]["content"].replace("\r\n", "\n").encode()).hexdigest() != GENERAL_PROMPT_SHA256):
                return False
            user = json.loads(body["messages"][1]["content"])
            if (set(user) != {"page", "document_pages", "definition", "json_schema", "targets"}
                    or user["page"] != page["page_one_based"] or user["document_pages"] != page["expected_pages"]
                    or user["definition"] != expected_semantics or user["json_schema"] != body["format"]
                    or user["targets"] != {"fields": [f["name"] for f in definition["fields"]],
                        "tables": [t["name"] for t in definition["tables"]]}):
                return False
            image = request["image"]
            images = [artifact for artifact in page["payload"]["artifacts"] if artifact["kind"] == "PAGE_IMAGE_PNG"]
            if len(images) != 1 or image["sha256"] != images[0]["sha256"] or image["source_page"] != page["page_one_based"] or image["query_region"] != [0, 0, 1, 1]:
                return False
            effective = copy.deepcopy(body)
            effective["messages"][1]["images"] = [images[0]["content_b64"]]
            if canonical_hash(effective) != request["request_sha256"]:
                return False
        return bool(selected)
    except (KeyError, TypeError, ValueError):
        return False


def _bands(words):
    bands: list[dict[str, Any]] = []
    for word in sorted(words, key=lambda w: ((w["region"][1] + w["region"][3]) / 2, w["region"][0])):
        box = word["region"]
        center = (box[1] + box[3]) / 2
        choices = [band for band in bands if abs(center - band["center"]) <= .45 * min(band["height"], box[3] - box[1])]
        if len(choices) > 1:
            raise ValueError("NATIVE_LINE_GEOMETRY_AMBIGUOUS")
        if choices:
            choices[0]["words"].append(word)
        else:
            bands.append({"center": center, "height": box[3] - box[1], "words": [word]})
    for band in bands:
        band["words"].sort(key=lambda w: w["region"][0])
        positions = [word["region"] for word in band["words"]]
        band["region"] = [min(p[0] for p in positions), min(p[1] for p in positions), max(p[2] for p in positions), max(p[3] for p in positions)]
        band["text"] = " ".join(word["text"] for word in band["words"])
        band["id"] = stable_id("source-band", [word["id"] for word in band["words"]])
    return bands


def _native_source(ir, events):
    probes = [event for event in events if event.get("stage") == "NATIVE_SOURCE_PROBE"]
    if len(probes) != 1:
        return None
    record = probes[0]["payload"]["artifacts"][0]
    original = json.loads(decode_artifact(record))
    payload, count = original["payload"], ir["expected_pages"]
    expected = list(range(1, count + 1))
    lines = payload.get("lines", [])
    # The native reader explicitly tests pdfimages per page. Any image or empty
    # page is a gap, even when the same page also has good digital text.
    if (payload.get("native_incomplete_pages") != [] or payload.get("pages") != expected
            or {line.get("page") for line in lines} != set(expected)
            or original["engine"].get("pages") != count):
        return None
    geometry = payload.get("page_geometry", {})
    pages = []
    for page in expected:
        page_geometry = geometry.get(str(page), {})
        if page_geometry.get("unit") != "PDF_POINT" or any(type(page_geometry.get(key)) not in (int, float) or page_geometry[key] <= 0 for key in ("width", "height")):
            return None
        words, positions = [], set()
        for index, line in enumerate(lines):
            if line.get("page") != page:
                continue
            if line.get("method") != "DIGITAL" or not _box(line.get("region")) or not line.get("words"):
                return None
            if " ".join(word["text"] for word in line["words"]) != line["text"]:
                return None
            for ordinal, word in enumerate(line["words"]):
                box = word.get("region")
                if not _box(box) or not word.get("text") or box[0] < line["region"][0] or box[2] > line["region"][2] or box[1] < line["region"][1] or box[3] > line["region"][3]:
                    return None
                position = (word["text"], *box)
                if position in positions:
                    return None
                positions.add(position)
                words.append({"text": word["text"], "region": box,
                    "id": stable_id(record["sha256"], index, ordinal)})
        pages.append({"page": page, "bands": _bands(words)})
    return {"identity": {"stage": "NATIVE_SOURCE_PROBE", "mechanism": "PDF_NATIVE", "model": "PDF_NATIVE",
        "event_hashes": [probes[0]["event_sha256"]], "completed_pages": expected},
        "artifact_sha256": record["sha256"], "pages": pages, "complete": True}


def _column_header(texts, fields):
    mapping: dict[int, str] = {}
    for index, text in enumerate(texts):
        names = [field["name"] for field in fields if field["name"] not in EVIDENCE_ONLY_ROLES
            and _key(text) in {_key(value) for value in _aliases(field)}]
        if len(names) != 1 or names[0] in mapping.values():
            return None
        mapping[index] = names[0]
    return mapping if len(mapping) >= 2 and all(not field.get("required", True)
        or field["name"] in mapping.values() for field in fields if field["name"] not in EVIDENCE_ONLY_ROLES) else None


def _segments(band):
    height = statistics.median(word["region"][3] - word["region"][1] for word in band["words"])
    segments: list[list[dict[str, Any]]] = []
    for word in band["words"]:
        if not segments or word["region"][0] - segments[-1][-1]["region"][2] > max(.012, 1.5 * height):
            segments.append([])
        segments[-1].append(word)
    return [{"text": " ".join(word["text"] for word in segment),
        "region": [segment[0]["region"][0], min(word["region"][1] for word in segment), segment[-1]["region"][2], max(word["region"][3] for word in segment)],
        "ids": [word["id"] for word in segment]} for segment in segments]


def _literal_inline(text, label, labels):
    from .document_header_association import _label_ending, _matches

    matches = _matches(text, labels)
    if not matches or matches[0][0] != 0:
        return []
    first_end, first_separator = _label_ending(text, matches[0][1], matches[0][2])
    if not first_separator and not re.match(r"\s*[:=]", text[first_end:]):
        return []
    values = []
    for _start, end, caption in matches:
        if _key(caption) != _key(label):
            continue
        finished, explicit = _label_ending(text, end, caption)
        delimiter = re.match(r"\s*[:=]\s*", text[finished:])
        if not explicit and delimiter is None:
            continue
        start = finished + (delimiter.end() if delimiter else 0)
        later = []
        for other_start, other_end, other_label in matches:
            if other_start < start:
                continue
            boundary, declared = _label_ending(text, other_end, other_label)
            if declared or re.match(r"\s*[:=]", text[boundary:]):
                later.append(other_start)
        value = text[start:min(later) if later else len(text)].strip()
        if value:
            values.append(value)
    return values


def _header_facts(facts, definition):
    """Literal label/value adjacency, independently read from measured sources."""
    result = defaultdict(list)
    labels = {label for field in definition["fields"] for label in _aliases(field)}
    for index, fact in enumerate(facts):
        text = fact["text"]
        for field in definition["fields"]:
            for label in _aliases(field):
                section = field.get("section", "DOCUMENT")
                if section in {"ISSUER", "BUYER"}:
                    role = "issuer" if section == "ISSUER" else "buyer"
                    intrinsic = _key(label) in {_key(value) for value in ROLE_ALIASES[role]}
                    nearest = None
                    for previous in reversed(facts[:index + 1]):
                        if previous["page"] != fact["page"]:
                            break
                        # Only an actual standalone heading establishes an
                        # enclosing section. An inline caption in a wrong
                        # section cannot overwrite that section's authority.
                        caption = previous["text"].strip()
                        found = [name.upper() for name in ("issuer", "buyer") if _key(caption) in {_key(value) for value in ROLE_ALIASES[name]}]
                        if found:
                            nearest = found[0]
                            break
                    if nearest is not None and nearest != section or not intrinsic and nearest != section:
                        continue
                for value in _literal_inline(text, label, labels):
                    result[field["name"]].append({**fact, "observed": value, "label": label, "association": "LABEL_VALUE_INLINE",
                        "source_section": nearest if section in {"ISSUER", "BUYER"} else None})
                if _key(text) != _key(label) or index + 1 >= len(facts):
                    continue
                after = facts[index + 1]
                a, b = fact.get("region"), after.get("region")
                if fact["page"] != after["page"] or not a or not b or _key(after["text"]) in {_key(value) for value in labels}:
                    continue
                overlap = max(0, min(a[2], b[2]) - max(a[0], b[0]))
                if 0 <= b[1] - a[3] <= 3 * (a[3] - a[1]) and overlap >= .5 * min(a[2] - a[0], b[2] - b[0]):
                    result[field["name"]].append({**after, "observed": after["text"], "label": label,
                        "association": "LABEL_VALUE_MEASURED_BELOW", "label_ref": fact["ref"],
                        "source_section": nearest if section in {"ISSUER", "BUYER"} else None})
    return result


def _field_boundary(text, definition):
    from .document_header_association import _inline

    labels = {label for field in definition["fields"] for label in _aliases(field)}
    return any(_inline(text, label, labels) or _key(text) == _key(label) for label in labels)


def _page_marker(text, page, count):
    return bool(re.fullmatch(rf"(?:page|página|pagina)\s+{page}\s*(?:of|de|/)\s*{count}", text.strip(), re.IGNORECASE))


def _unclassified_facts(facts, observations, definition):
    accounted = {item["ref"]["id"] for values in observations.values() for item in values}
    accounted.update(item["label_ref"]["id"] for values in observations.values() for item in values if item.get("label_ref"))
    # Exact declared captions and generic document/collection headings are
    # structural labels. No open-ended keyword classifier declares other prose
    # harmless: a single-column collection may be printed entirely as text.
    headings = {_key(alias) for field in definition["fields"] for alias in _aliases(field)}
    headings.update(_key(table["name"].replace("_", " ")) for table in definition["tables"])
    headings.update({"invoice", "factura", "statement", "extracto", "certificate", "certificado", "totals", "totales"})
    return [fact["ref"] for fact in facts if fact["ref"]["id"] not in accounted and _key(fact["text"]) not in headings]


def _native_inventory(source, definition):
    inventories = {table["name"]: {"rows": [], "headers": [], "issues": [], "source": source["identity"]} for table in definition["tables"]}
    facts = []
    for page in source["pages"]:
        current = None
        for band in page["bands"]:
            segments = _segments(band)
            candidates = [(table, mapping) for table in definition["tables"]
                if (mapping := _column_header([segment["text"] for segment in segments], table["fields"]))]
            reference = _ref(source["identity"], page["page"], band["id"], geometry_kind="MEASURED_SOURCE", artifact_sha256=source["artifact_sha256"])
            fact = {"text": band["text"], "page": page["page"], "region": band["region"], "ref": reference}
            if len(candidates) == 1:
                table, mapping = candidates[0]
                current = (table, mapping, segments)
                inventories[table["name"]]["headers"].append({"page": page["page"], "ref": reference, "columns": mapping})
                continue
            if len(candidates) > 1:
                for table, _ in candidates:
                    inventories[table["name"]]["issues"].append("ASSOCIATION_AMBIGUOUS")
                current = None
                continue
            if _page_marker(band["text"], page["page"], len(source["pages"])):
                continue
            if _field_boundary(band["text"], definition):
                facts.append(fact)
                current = None
                continue
            if current is None:
                facts.append(fact)
                # Multiple separated numeric cells outside an inventoried table
                # cannot silently disappear just because its header was missed.
                if len(segments) >= 2:
                    for inventory in inventories.values():
                        inventory["issues"].append("UNASSOCIATED_SOURCE_TABLE")
                continue
            table, mapping, header = current
            inventory = inventories[table["name"]]
            centers = [(cell["region"][0] + cell["region"][2]) / 2 for cell in header]
            bounds = [0, *[(left + right) / 2 for left, right in pairwise(centers)], 1]
            values, refs = {}, []
            for segment in segments:
                center = (segment["region"][0] + segment["region"][2]) / 2
                columns = [index for index in mapping if bounds[index] <= center < bounds[index + 1]]
                if len(columns) != 1 or mapping[columns[0]] in values:
                    inventory["issues"].append("ROW_IDENTITY_AMBIGUOUS")
                    continue
                values[mapping[columns[0]]] = segment["text"]
                refs.extend(segment["ids"])
            if len(values) != len(mapping):
                inventory["issues"].append("MISSING_REQUIRED_COLUMN")
            inventory["rows"].append({"id": band["id"], "page": page["page"], "values": values,
                "quote": band["text"], "ref": reference, "source_refs": refs, "columns": list(mapping.values())})
    observations = _header_facts(facts, definition)
    unknown = _unclassified_facts(facts, observations, definition)
    for inventory in inventories.values():
        if unknown:
            inventory["issues"].append("UNCLASSIFIED_SOURCE_SCOPE")
        inventory["unclassified_source_refs"] = unknown
        inventory["complete"] = bool(inventory["headers"] and not inventory["issues"])
    return inventories, observations


def _paddle_inventory(ir, control, definition):
    identity = control["identity"]
    inventories = {table["name"]: {"rows": [], "headers": [], "issues": [], "source": identity} for table in definition["tables"]}
    facts = []
    for page in [page for page in ir["pages"] if page["stage"] == identity["stage"]]:
        geometry = page.get("geometry") or {}
        for block in page["blocks"]:
            if block["type"] == "table" or not isinstance(block.get("text"), str):
                continue
            box = block.get("geometry")
            normalized = None
            if isinstance(box, list) and len(box) == 4 and geometry.get("unit") == "PIXEL" and geometry.get("width") and geometry.get("height"):
                normalized = [box[0] / geometry["width"], box[1] / geometry["height"], box[2] / geometry["width"], box[3] / geometry["height"]]
                if not _box(normalized):
                    normalized = None
            for text in block["text"].splitlines():
                facts.append({"text": text, "page": page["page_one_based"], "region": normalized,
                    "ref": _ref(identity, page["page_one_based"], block["id"], geometry_kind="MEASURED_BLOCK" if normalized else "UNKNOWN")})
        for physical in page["tables"]:
            owners = {cell["id"]: cell for cell in physical["cells"]}
            rows: list[list[Any]] = [[owners.get(slot["cell_id"]) for slot in row["slots"]] for row in physical["rows"]]
            candidates = [(table, index, mapping) for table in definition["tables"] for index, row in enumerate(rows)
                if all(cell is not None and cell["rowspan"] == cell["colspan"] == 1 for cell in row)
                and (mapping := _column_header([cell["text"] for cell in row], table["fields"]))]
            # A complete label/value table is header evidence, not a missing
            # detail table. Unknown rows prevent this classification.
            if not candidates:
                headers = []
                for index, row in enumerate(rows):
                    if len(row) != 2 or any(cell is None or cell["rowspan"] != 1 or cell["colspan"] != 1 for cell in row):
                        break
                    matches = [field for field in definition["fields"] if _key(row[0]["text"]) in {_key(alias) for alias in _aliases(field)}]
                    if len(matches) != 1:
                        break
                    headers.append((matches[0], row))
                if headers and len(headers) == len(rows) and not physical["errors"]:
                    for field, row in headers:
                        facts.append({"text": row[0]["text"] + ": " + row[1]["text"], "page": page["page_one_based"], "region": None,
                            "ref": _ref(identity, page["page_one_based"], row[1]["id"], geometry_kind="UNKNOWN")})
                    continue
                for inventory in inventories.values():
                    inventory["issues"].append("UNASSOCIATED_SOURCE_TABLE")
                continue
            names = {table["name"] for table, _, _ in candidates}
            if len(names) != 1:
                for inventory in inventories.values():
                    inventory["issues"].append("ASSOCIATION_AMBIGUOUS")
                continue
            table, first, mapping = candidates[0]
            inventory = inventories[table["name"]]
            if physical["errors"] or first != 0:
                inventory["issues"].append("UNPARSED_SOURCE_STRUCTURE")
            header_indices = {index for _, index, _ in candidates}
            inventory["headers"].append({"page": page["page_one_based"], "ref": _ref(identity, page["page_one_based"], physical["rows"][first]["id"]), "columns": mapping})
            for index, row in enumerate(rows):
                if index in header_indices:
                    continue
                if any(cell is None or cell["rowspan"] != 1 or cell["colspan"] != 1 for cell in row):
                    inventory["issues"].append("UNPARSED_SOURCE_STRUCTURE")
                    continue
                values = {name: row[column]["text"] for column, name in mapping.items()}
                inventory["rows"].append({"id": physical["rows"][index]["id"], "page": page["page_one_based"],
                    "values": values, "quote": " ".join(cell["text"] for cell in row),
                    "ref": _ref(identity, page["page_one_based"], physical["rows"][index]["id"], geometry_kind="UNKNOWN"),
                    "source_refs": [cell["id"] for cell in row], "columns": list(mapping.values())})
    facts.sort(key=lambda fact: (fact["page"], (fact.get("region") or [0, 0, 0, 0])[1]))
    observations = _header_facts(facts, definition)
    unknown = _unclassified_facts(facts, observations, definition)
    for inventory in inventories.values():
        if unknown:
            inventory["issues"].append("UNCLASSIFIED_SOURCE_SCOPE")
        inventory["unclassified_source_refs"] = unknown
        inventory["complete"] = bool(inventory["headers"] and not inventory["issues"])
    return inventories, observations


def _all_cells(payload, definition):
    for field in definition["fields"]:
        yield "headers.0." + field["name"], payload.get("fields", {}).get(field["name"]), field
    for table in definition["tables"]:
        for index, row in enumerate(payload.get("tables", {}).get(table["name"], [])):
            for field in table["fields"]:
                yield f"{table['name']}.{index}.{field['name']}", row.get(field["name"]), field


def _baseline_engine(engine):
    keys = ("key", "lossless_contract_version", "route", "configuration", "configuration_sha256",
        "recognition_configuration_sha256", "source_sha256", "recognition_definition_sha256",
        "definition_sha256", "recognition_event_hashes", "execution", "recognition_sources", "recognition_bindings")
    return {key: copy.deepcopy(engine[key]) for key in keys if key in engine}


def certify_semantic_candidate(data, definition, source_sha256, *, source_context, check=None):
    """Worker-only pure certification of a new explicitly opted-in contract."""
    from .document_engine import arithmetic_checks, cross_table_checks
    from .document_evidence_v3 import _selected_source
    from .document_semantic_business import business_report

    check = check or (lambda: None)
    check()
    payload, engine = data["payload"], data["engine"]
    context = copy.deepcopy(source_context or {})
    ir, controls, global_issues = _authenticate(data, definition, source_sha256, context, check)
    observations, verified, issues = {}, {}, list(global_issues)
    coverage = {table["name"]: False for table in definition["tables"]}
    details, source_inventories, source_headers = {}, [], []
    native = None
    independent_qwen = [control for control in controls if _qwen_independent(control, context.get("recognition_events", []), definition)] if ir is not None else []
    if ir is not None and not global_issues:
        try:
            native = _native_source(ir, context["recognition_events"])
            if native is not None:
                inventories, facts = _native_inventory(native, definition)
                source_inventories.append(inventories)
                source_headers.append((native["identity"], facts))
            # The structured route reads the same original directly; its input
            # contract contains source/configuration, never Qwen proposals.
            for control in controls:
                if control["identity"]["mechanism"] == "PADDLE":
                    inventories, facts = _paddle_inventory(ir, control, definition)
                    source_inventories.append(inventories)
                    source_headers.append((control["identity"], facts))
        except (KeyError, TypeError, ValueError):
            global_issues.append("SOURCE_STRUCTURE_INTEGRITY")
            issues.append("SOURCE_STRUCTURE_INTEGRITY")
    narrative = definition.get("semantic_general", {}).get("contract", {}).get("document_type") == "NARRATIVE"
    for path, cell, field in _all_cells(payload, definition):
        check()
        reasons, refs, mechanisms, associations = [], [], [], []
        selected = _selected_source(cell, path, data, controls) if isinstance(cell, dict) else None
        if not _valid_cell(cell, field):
            reasons.append("VALUE_UNRESOLVED")
        if selected is None:
            reasons.append("SOURCE_VALUE_BINDING")
        elif selected["identity"]["mechanism"] == "QWEN" and selected not in independent_qwen:
            reasons.append("INDEPENDENT_REQUEST_UNVERIFIED")
        if path.startswith("headers.") and not reasons:
            candidates: list[Any] = []
            for identity, facts in source_headers:
                if identity["mechanism"] == cast(dict[str, Any], selected)["identity"]["mechanism"]:
                    continue
                values = facts.get(field["name"], [])
                # Null cannot be inferred from label absence or model-local
                # NOT_ON_PAGE. An explicit optional physical column is handled
                # with its complete table inventory instead.
                if cell.get("value") is None:
                    continue
                valid = [(fact, *_normalized(fact["observed"], field)) for fact in values]
                if any(status != "VALID" for _, _, status in valid):
                    reasons.append("OBSERVATION_UNCERTAINTY")
                distinct = {canonical_hash(_comparison(value, field)) for _, value, status in valid if status == "VALID"}
                if len(distinct) > 1 or any(status == "VALID" and _comparison(value, field) != _comparison(cell["value"], field) for _, value, status in valid):
                    reasons.append("SOURCE_CONTRADICTION")
                matches = [fact for fact, value, status in valid if status == "VALID" and _comparison(value, field) == _comparison(cell["value"], field)
                    and fact["page"] == (cell.get("origin") or {}).get("page")]
                candidates.extend((identity, fact) for fact in matches)
            if candidates and not reasons:
                for identity, fact in candidates:
                    mechanisms.append(identity["mechanism"])
                    refs.append(fact["ref"])
                    associations.append({"basis": identity["mechanism"] + "_LABEL_VALUE", "label": fact["label"],
                        "section": fact.get("source_section"), "requested_section": field.get("section", "DOCUMENT"),
                        "relation": fact["association"]})
            else:
                reasons.append("OPTIONAL_ABSENCE_UNVERIFIED" if cell.get("value") is None else "ASSOCIATION_NOT_PROVEN")
            if field["name"] in {"statement", "summary"}:
                # A matching first line cannot certify a complete assertion or
                # summary with limitations, negations or following paragraphs.
                reasons.append("TEXT_SCOPE_UNVERIFIED")
        observations[path] = {"status": "REVIEW_REQUIRED", "reasons": sorted(set(reasons)),
            "mechanisms": sorted(set(mechanisms)), "source_refs": refs, "association": associations}
        if path.startswith("headers.") and not reasons and not global_issues:
            verified[path] = _cell_hash(cell)
            observations[path]["status"] = "VERIFIED"
    for table in definition["tables"]:
        name = table["name"]
        rows = payload.get("tables", {}).get(name, [])
        references: list[dict[str, Any]]
        table_issues, alignments, references = [], [], []
        if narrative or next((value.get("kind") for value in definition["semantic_general"]["contract"]["tables"] if value["name"] == name), "TABLE") == "COLLECTION":
            table_issues.append("NARRATIVE_SEMANTICS_UNVERIFIED")
        candidates = [inventory[name] for inventory in source_inventories]
        for inventory in candidates:
            source_rows = inventory["rows"]
            physical_fields = [field for field in table["fields"] if field["name"] not in EVIDENCE_ONLY_ROLES]
            expected, actual = defaultdict(list), defaultdict(list)
            invalid_source = False
            for index, source in enumerate(source_rows):
                values = []
                for field in physical_fields:
                    value, status = _normalized(source["values"].get(field["name"]), field)
                    if status != "VALID":
                        invalid_source = True
                    values.append(_comparison(value, field))
                expected[canonical_hash([source["page"], values])].append(index)
            for index, row in enumerate(rows):
                provenance = payload.get("row_provenance", {}).get(name, [])
                page = provenance[index].get("source_page") if index < len(provenance) else next(((cell.get("origin") or {}).get("page") for cell in row.values() if isinstance(cell, dict) and cell.get("origin")), None)
                actual[canonical_hash([page, [_comparison(row.get(field["name"], {}).get("value"), field) for field in physical_fields]])].append(index)
            source_counter = Counter({key: len(value) for key, value in expected.items()})
            actual_counter = Counter({key: len(value) for key, value in actual.items()})
            local = list(inventory["issues"])
            if invalid_source:
                local.append("OBSERVATION_UNCERTAINTY")
            if source_counter - actual_counter:
                local.append("UNMATCHED_SOURCE_ROW")
            if actual_counter - source_counter:
                local.append("UNMATCHED_OUTPUT_ROW")
            if not inventory["complete"] or not rows or not source_rows:
                local.append("ROW_COVERAGE_UNPROVEN")
            matches = []
            for key in sorted(expected.keys() & actual.keys()):
                if len(expected[key]) != len(actual[key]):
                    continue
                for source_index, output_index in zip(expected[key], actual[key], strict=True):
                    matches.append((output_index, source_rows[source_index]))
            matches.sort(key=lambda match: match[0])
            # All source occurrences and all output occurrences must participate.
            complete = bool(not local and len(matches) == len(rows) == len(source_rows))
            references.extend(header["ref"] for header in inventory["headers"])
            for index, source in matches:
                row = rows[index]
                supported = []
                for field in table["fields"]:
                    path = f"{name}.{index}.{field['name']}"
                    item = observations[path]
                    cell = row.get(field["name"])
                    selected = _selected_source(cell, path, data, controls) if isinstance(cell, dict) else None
                    reasons = list(item["reasons"])
                    if selected is None or selected["identity"]["mechanism"] == inventory["source"]["mechanism"]:
                        reasons.append("INDEPENDENT_SOURCE_REQUIRED")
                    if field["name"] in EVIDENCE_ONLY_ROLES:
                        if cell.get("value") is not None and _normalized(source["quote"], field) != (cell["value"], "VALID"):
                            reasons.append("ASSOCIATION_NOT_PROVEN")
                    elif field["name"] not in source["columns"] and (field.get("required", True) or cell.get("value") is not None):
                        reasons.append("MISSING_REQUIRED_COLUMN")
                    if narrative:
                        reasons.append("NARRATIVE_SEMANTICS_UNVERIFIED")
                    if not reasons and not global_issues:
                        verified[path] = _cell_hash(cell)
                        item.update(status="VERIFIED", mechanisms=[inventory["source"]["mechanism"]],
                            source_refs=[source["ref"]], association=[{"basis": inventory["source"]["mechanism"] + "_SOURCE_COLUMN",
                                "column_role": field["name"], "source_occurrence": source["id"],
                                "scope": "EVIDENCE_ONLY" if field["name"] in EVIDENCE_ONLY_ROLES else "PRINTED_COLUMN" if field["name"] in source["columns"] else "OPTIONAL_COLUMN_ABSENT_COMPLETE_HEADER"}])
                        supported.append(field["name"])
                    elif item["status"] != "VERIFIED":
                        item["reasons"] = sorted(set(reasons or ["ROW_COVERAGE_UNPROVEN"]))
                alignments.append({"source_occurrence": source["id"], "output_occurrence": index,
                    "source_page": source["page"], "mechanism": inventory["source"]["mechanism"],
                    "basis": "FULL_NORMALIZED_ROW_MULTISET_WITH_SOURCE_OCCURRENCES", "verified_fields": supported})
            if complete:
                coverage[name] = not table_issues and not global_issues
            else:
                table_issues.extend(local)
        # A successful source never erases a contradictory or unaccounted
        # second source. Per-cell matches may still spare an authorized reviewer
        # from reconfirming known values; document coverage remains blocked.
        coverage[name] = bool(coverage[name] and not table_issues)
        if not candidates:
            table_issues.append("INDEPENDENT_SOURCE_REQUIRED")
        if not coverage[name]:
            table_issues.append("ROW_COVERAGE_UNPROVEN")
        details[name] = {"covered": coverage[name], "reasons": sorted(set(table_issues)),
            "source_inventory": candidates, "occurrence_alignment": alignments, "proof_refs": references,
            "basis": "SOURCE_INVENTORY_BIJECTION_NOT_ROW_COUNT_OR_ARITHMETIC"}
        issues.extend(code + ":" + name for code in sorted(set(table_issues)))
    for path, item in observations.items():
        if item["status"] != "VERIFIED":
            if not item["reasons"]:
                item["reasons"] = ["INDEPENDENT_SOURCE_REQUIRED"]
            issues.extend(code + ":" + path for code in item["reasons"])
    business_rules = [*arithmetic_checks(payload, definition), *cross_table_checks(payload, definition)]
    business_validation = business_report(payload, definition, verified_paths=set(verified), table_coverage=coverage)
    if business_validation["required"] and not business_validation["applicable"] and not business_validation["issues"]:
        business_validation["issues"].append("QUALITY_BUSINESS_SOURCE_SCOPE_UNVERIFIED")
    issues.extend(business_validation["issues"])
    if business_validation["required"]:
        business_rules = business_validation["checks"]
        arithmetic = bool(business_validation["applicable"] and not business_validation["issues"]
            and all(item["status"] == "VALID" for item in business_rules))
    else:
        arithmetic = all(item["status"] == "VALID" for item in business_rules)
    if any(item["status"] == "INVALID" for item in business_rules):
        issues.append("DECLARED_BUSINESS_RULE_FAILED")
    proof = {"validator_version": VALIDATOR_VERSION, "association_contract": ASSOCIATION_CONTRACT,
        "association_contract_sha256": ASSOCIATION_CONTRACT_SHA256, "source_sha256": source_sha256,
        "definition_sha256": canonical_hash(definition), "configuration_sha256": canonical_hash(engine.get("configuration", {})),
        "candidate_sha256": canonical_hash(payload), "source_context_sha256": canonical_hash(context),
        "representation_sha256": canonical_hash(payload.get("document_representation")),
        "recognition_event_hashes": copy.deepcopy(engine.get("recognition_event_hashes", [])),
        "durable_source_bound": not global_issues, "recognition_complete": not global_issues,
        "worker_registry_verified": not global_issues, "verified_cells": verified,
        "corroboration": observations, "table_coverage": coverage, "coverage_details": details,
        "table_hashes": {table["name"]: semantic_table_hash(payload.get("tables", {}).get(table["name"], [])) for table in definition["tables"]},
        "arithmetic": arithmetic, "business_rules": business_rules, "business_validation": business_validation,
        "global_issues": sorted(set(global_issues)),
        "issues": sorted(set(issues)), "independent_extraction": bool(source_headers),
        "native_scope_complete": native is not None, "risk_policy": copy.deepcopy(definition.get("validation_policy")),
        "recognition_sources": [control["identity"] for control in controls], "source_context": context,
        "automatic_candidate": {"payload": copy.deepcopy(payload), "engine": _baseline_engine(engine)}}
    proof["attestation_sha256"] = canonical_hash(proof)
    check()
    return proof


def trusted_semantic_proof(extraction, definition):
    """Recompute the worker baseline; reviewed edits do not gain AUTO authority."""
    proof = extraction.engine.get("validation") or {}
    try:
        if (proof.get("validator_version") != VALIDATOR_VERSION
                or proof.get("association_contract_sha256") != ASSOCIATION_CONTRACT_SHA256
                or proof.get("definition_sha256") != canonical_hash(definition)
                or proof.get("configuration_sha256") != canonical_hash(extraction.engine.get("configuration", {}))
                or proof.get("source_sha256") != extraction.engine.get("source_sha256")
                or proof.get("representation_sha256") != canonical_hash(extraction.payload.get("document_representation"))
                or proof.get("recognition_event_hashes") != extraction.engine.get("recognition_event_hashes")
                or proof.get("attestation_sha256") != canonical_hash({key: value for key, value in proof.items() if key != "attestation_sha256"})):
            return None
        original = proof["automatic_candidate"]
        if canonical_hash(original["payload"]) != proof["candidate_sha256"] or canonical_hash(original["engine"]) != canonical_hash(_baseline_engine(extraction.engine)):
            return None
        regenerated = certify_semantic_candidate(original, definition, proof["source_sha256"], source_context=proof["source_context"])
        if canonical_hash(regenerated) != canonical_hash(proof):
            return None
        return proof
    except (KeyError, TypeError, ValueError, OverflowError):
        return None


def semantic_verification_decision(data, definition, context):
    """Request a structured control only where native scope cannot cover scans."""
    proof = certify_semantic_candidate(data, definition, data["engine"].get("source_sha256"), source_context=context)
    reasons = []
    for issue in proof["issues"]:
        code, _, location = issue.partition(":")
        kind = "INTEGRITY" if issue in proof["global_issues"] else "INCIDENT"
        reasons.append({"kind": kind, "code": code, **({"path" if location.count(".") == 2 else "table": location} if location else {})})
    families = {control["mechanism"] for control in proof["recognition_sources"]}
    actionable = bool(reasons and not proof["global_issues"] and not proof["native_scope_complete"]
        and families == {"QWEN"} and any(table.get("kind", "TABLE") == "TABLE"
            for table in definition["semantic_general"]["contract"]["tables"]))
    return {"version": VALIDATOR_VERSION, "required": bool(reasons), "actionable": actionable,
        "reasons": reasons, "scope": {"kind": "WHOLE_DOCUMENT", "pages": list(data["payload"].get("pages", [])),
            "focal_reread": False, "reason": "STRUCTURAL_CONTROL_ONLY_FOR_INCOMPLETE_NATIVE_SOURCE_SCOPE"},
        "decision_sha256": canonical_hash(reasons), "native_scope_complete": proof["native_scope_complete"]}
