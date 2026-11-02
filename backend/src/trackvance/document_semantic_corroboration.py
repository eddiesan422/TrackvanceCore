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
VALIDATOR_VERSION_V2 = "TRACKVANCE_SEMANTIC_EVIDENCE_098_V2"
ASSOCIATION_CONTRACT_V2 = "SEMANTIC_SOURCE_ASSOCIATION_V2"
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
    version2 = (definition.get("semantic_general") or {}).get("automatic_verification") == "SEMANTIC_AUTO_V2"
    if ((definition.get("semantic_general") or {}).get("automatic_verification") != ("SEMANTIC_AUTO_V2" if version2 else "SEMANTIC_AUTO_V1")
            or (definition.get("validation_policy") or {}).get("version") != ("SEMANTIC_EVIDENCE_V2" if version2 else "SEMANTIC_EVIDENCE_V1")
            or (definition.get("semantic_general") or {}).get("association_contract") != (ASSOCIATION_CONTRACT_V2 if version2 else ASSOCIATION_CONTRACT)):
        errors.append("SEMANTIC_AUTO_CONTRACT_REQUIRED")
    if version2:
        from .document_semantic_general import verification_contract_v2

        metadata = definition["semantic_general"]
        if (metadata.get("verification_contract") != verification_contract_v2()
                or metadata.get("verification_contract_sha256") != canonical_hash(metadata.get("verification_contract"))):
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
    version2 = (definition.get("semantic_general") or {}).get("automatic_verification") == "SEMANTIC_AUTO_V2"
    context = copy.deepcopy(source_context or {})
    ir, controls, global_issues = _authenticate(data, definition, source_sha256, context, check)
    observations, verified, issues = {}, {}, list(global_issues)
    coverage = {table["name"]: False for table in definition["tables"]}
    details, source_inventories, source_headers = {}, [], []
    native = None
    source_facts: list[dict[str, Any]] = []
    independent_qwen = [control for control in controls if _qwen_independent(control, context.get("recognition_events", []), definition)] if ir is not None else []
    if ir is not None and not global_issues:
        try:
            native = (_native_source_v2 if version2 else _native_source)(ir, context["recognition_events"])
            if native is not None:
                inventories, facts = (_native_inventory_v2 if version2 else _native_inventory)(native, definition)
                source_inventories.append(inventories)
                source_headers.append((native["identity"], facts))
                if version2:
                    source_facts.extend(_native_facts_v2(native))
            # The structured route reads the same original directly; its input
            # contract contains source/configuration, never Qwen proposals.
            for control in controls:
                if control["identity"]["mechanism"] == "PADDLE":
                    inventories, facts = (_paddle_inventory_v2 if version2 else _paddle_inventory)(ir, control, definition)
                    source_inventories.append(inventories)
                    source_headers.append((control["identity"], facts))
                    if version2:
                        source_facts.extend(_paddle_facts_v2(ir, control))
        except (KeyError, TypeError, ValueError):
            global_issues.append("SOURCE_STRUCTURE_INTEGRITY")
            issues.append("SOURCE_STRUCTURE_INTEGRITY")
    narrative = definition.get("semantic_general", {}).get("contract", {}).get("document_type") == "NARRATIVE"
    for path, cell, field in _all_cells(payload, definition):
        check()
        reasons, mechanisms, associations = [], [], []
        refs: list[dict[str, Any]] = []
        rejected_associations: list[dict[str, Any]] = []
        selected = _selected_source(cell, path, data, controls) if isinstance(cell, dict) else None
        if not _valid_cell(cell, field):
            reasons.append("VALUE_UNRESOLVED")
        if selected is None:
            reasons.append("SOURCE_VALUE_BINDING")
        elif selected["identity"]["mechanism"] == "QWEN" and selected not in independent_qwen:
            reasons.append("INDEPENDENT_REQUEST_UNVERIFIED")
        if version2 and path.startswith("headers."):
            actual_source_values = [(identity, fact, *_normalized(fact["observed"], field))
                for identity, facts in source_headers for fact in facts.get(field["name"], [])
                if fact.get("eligible_for_field") is not False and not fact.get("source_uncertain")]
            distinct_source = {canonical_hash(_comparison(value, field)) for _identity, _fact, value, status in actual_source_values
                if status == "VALID" and value is not None}
            if len(distinct_source) > 1:
                reasons.append("DOCUMENTARY_SOURCE_CONTRADICTION")
                refs.extend(fact["ref"] for _identity, fact, _value, status in actual_source_values if status == "VALID")
        if path.startswith("headers.") and not reasons:
            candidates: list[Any] = []
            for identity, facts in source_headers:
                if identity["mechanism"] == cast(dict[str, Any], selected)["identity"]["mechanism"]:
                    continue
                values = facts.get(field["name"], [])
                if version2:
                    rejected_associations.extend(fact["trace"] for fact in values if fact.get("eligible_for_field") is False)
                    values = [fact for fact in values if fact.get("eligible_for_field") is not False]
                    if any(fact.get("source_uncertain") for fact in values):
                        reasons.append("OBSERVATION_UNCERTAINTY")
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
                independent_facts = [fact for fact in source_facts if fact["ref"].get("mechanism")
                    != cast(dict[str, Any], selected)["identity"]["mechanism"]] if version2 else []
                reasons.append("INDEPENDENT_SOURCE_REQUIRED" if version2 and not independent_facts else
                    "OPTIONAL_ABSENCE_UNVERIFIED" if cell.get("value") is None else "ASSOCIATION_NOT_PROVEN")
                literal_refs = _literal_locations_v2(cell, independent_facts) if version2 else []
                if version2:
                    refs.extend(literal_refs)
                if version2 and cell.get("value") is not None and independent_facts and not literal_refs and not any(
                        facts.get(field["name"]) for identity, facts in source_headers
                        if identity["mechanism"] != cast(dict[str, Any], selected)["identity"]["mechanism"]):
                    reasons.append("SOURCE_DATA_NOT_OBSERVED")
            if field["name"] in {"statement", "summary"}:
                # A matching first line cannot certify a complete assertion or
                # summary with limitations, negations or following paragraphs.
                reasons.append("TEXT_SCOPE_UNVERIFIED")
        observations[path] = {"status": "REVIEW_REQUIRED", "reasons": sorted(set(reasons)),
            "mechanisms": sorted(set(mechanisms)), "source_refs": refs, "association": associations}
        if version2:
            observations[path]["rejected_associations"] = rejected_associations
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
        if version2:
            signatures = {_inventory_signature_v2(inventory, table) for inventory in candidates if inventory["complete"]}
            signatures.discard(None)
            if len(signatures) > 1:
                table_issues.append("SOURCE_INVENTORY_CONTRADICTION")
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
        if version2 and "SOURCE_INVENTORY_CONTRADICTION" in table_issues:
            for path, item in observations.items():
                if path.startswith(name + "."):
                    verified.pop(path, None)
                    item.update(status="REVIEW_REQUIRED", reasons=sorted({*item["reasons"], "SOURCE_INVENTORY_CONTRADICTION"}))
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
                item["reasons"] = ["ROW_ASSOCIATION_NOT_PROVEN" if version2 and source_inventories else "INDEPENDENT_SOURCE_REQUIRED"]
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
    proof = {"validator_version": VALIDATOR_VERSION_V2 if version2 else VALIDATOR_VERSION,
        "association_contract": ASSOCIATION_CONTRACT_V2 if version2 else ASSOCIATION_CONTRACT,
        "association_contract_sha256": definition["semantic_general"]["verification_contract_sha256"] if version2
            else ASSOCIATION_CONTRACT_SHA256, "source_sha256": source_sha256,
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
        "native_scope_complete": bool(native and native["complete"]) if version2 else native is not None,
        "risk_policy": copy.deepcopy(definition.get("validation_policy")),
        "recognition_sources": [control["identity"] for control in controls], "source_context": context,
        "automatic_candidate": {"payload": copy.deepcopy(payload), "engine": _baseline_engine(engine)}}
    if version2:
        _complete_source_checks_v2(proof, data, definition, native, source_inventories, source_facts, ir)
    proof["attestation_sha256"] = canonical_hash(proof)
    check()
    return proof


def trusted_semantic_proof(extraction, definition):
    """Recompute the worker baseline; reviewed edits do not gain AUTO authority."""
    proof = extraction.engine.get("validation") or {}
    version2 = (definition.get("semantic_general") or {}).get("automatic_verification") == "SEMANTIC_AUTO_V2"
    try:
        if (proof.get("validator_version") != (VALIDATOR_VERSION_V2 if version2 else VALIDATOR_VERSION)
                or proof.get("association_contract_sha256") != (definition["semantic_general"]["verification_contract_sha256"] if version2
                    else ASSOCIATION_CONTRACT_SHA256)
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
    if (definition.get("semantic_general") or {}).get("automatic_verification") == "SEMANTIC_AUTO_V2":
        return _verification_decision_v2(data, definition, proof)
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


def _native_source_v2(ir, events):
    """Admit measured text per page; text availability is not complete scope."""
    probes = [event for event in events if event.get("stage") == "NATIVE_SOURCE_PROBE"]
    if len(probes) != 1:
        return None
    record = probes[0]["payload"]["artifacts"][0]
    original = json.loads(decode_artifact(record))
    payload, count = original["payload"], ir["expected_pages"]
    if original["engine"].get("pages") != count:
        return None
    expected = list(range(1, count + 1))
    gaps = set(payload.get("native_incomplete_pages") or [])
    geometry = payload.get("page_geometry", {})
    pages, invalid = [], []
    for page in expected:
        measured = geometry.get(str(page), {})
        lines = [line for line in payload.get("lines", []) if line.get("page") == page]
        if (measured.get("unit") != "PDF_POINT" or not lines
                or any(type(measured.get(key)) not in (int, float) or measured[key] <= 0 for key in ("width", "height"))):
            invalid.append(page)
            continue
        words: list[dict[str, Any]] = []
        positions: set[tuple] = set()
        valid = True
        for index, line in enumerate(payload.get("lines", [])):
            if line.get("page") != page:
                continue
            if (line.get("method") != "DIGITAL" or not _box(line.get("region")) or not line.get("words")
                    or " ".join(word["text"] for word in line["words"]) != line["text"]):
                valid = False
                break
            for ordinal, word in enumerate(line["words"]):
                box = word.get("region")
                if (not _box(box) or not word.get("text") or (word["text"], *box) in positions
                        or box[0] < line["region"][0] or box[2] > line["region"][2]
                        or box[1] < line["region"][1] or box[3] > line["region"][3]):
                    valid = False
                    break
                positions.add((word["text"], *box))
                words.append({"text": word["text"], "region": box, "id": stable_id(record["sha256"], index, ordinal)})
        if not valid:
            invalid.append(page)
            continue
        try:
            bands = _bands(words)
        except ValueError:
            invalid.append(page)
            continue
        pages.append({"page": page, "bands": bands})
    if not pages:
        return None
    available = [page["page"] for page in pages]
    return {"identity": {"stage": "NATIVE_SOURCE_PROBE", "mechanism": "PDF_NATIVE", "model": "PDF_NATIVE",
            "event_hashes": [probes[0]["event_sha256"]], "completed_pages": available},
        "artifact_sha256": record["sha256"], "pages": pages, "expected_pages": expected,
        "unavailable_pages": sorted(set(expected) - set(available)),
        "incomplete_scope_pages": sorted(gaps | set(invalid)),
        "complete": not gaps and not invalid and payload.get("pages") == expected}


def _native_facts_v2(source):
    return [{"text": band["text"], "page": page["page"], "region": band["region"],
        "geometry_kind": "MEASURED_SOURCE", "source_independent": True,
        "ref": _ref(source["identity"], page["page"], band["id"], geometry_kind="MEASURED_SOURCE",
            artifact_sha256=source["artifact_sha256"])}
        for page in source["pages"] for band in page["bands"]]


def _paddle_facts_v2(ir, control):
    identity = control["identity"]
    result = []
    for page in ir["pages"]:
        if page["stage"] != identity["stage"]:
            continue
        geometry = page.get("geometry") or {}
        for block in page["blocks"]:
            if block["type"] == "table" or not isinstance(block.get("text"), str):
                continue
            box = block.get("geometry")
            region = None
            if (isinstance(box, list) and len(box) == 4 and geometry.get("unit") == "PIXEL"
                    and geometry.get("width") and geometry.get("height")):
                candidate = [box[0] / geometry["width"], box[1] / geometry["height"],
                    box[2] / geometry["width"], box[3] / geometry["height"]]
                if _box(candidate):
                    region = candidate
            result.append({"text": block["text"], "page": page["page_one_based"], "region": region,
                "geometry_kind": "MEASURED_BLOCK" if region else "UNKNOWN",
                "source_independent": True, "block_role": block["type"],
                "ref": _ref(identity, page["page_one_based"], block["id"],
                    geometry_kind="MEASURED_BLOCK" if region else "UNKNOWN")})
        for table in page["tables"]:
            for cell in table["cells"]:
                result.append({"text": cell["text"], "page": page["page_one_based"],
                    "region": None, "geometry_kind": "UNKNOWN", "source_independent": True,
                    "ref": _ref(identity, page["page_one_based"], cell["id"])})
    return result


def _source_headers_v2(pages, definition, identity, references):
    from .document_header_association_v2 import header_observations_v2

    _observed, traces = header_observations_v2(pages, definition["fields"])
    result: dict[str, list[dict[str, Any]]] = {field["name"]: [] for field in definition["fields"]}
    for field in definition["fields"]:
        for trace in traces[field["name"]]:
            ref_id = trace["value_refs"][0]
            reference = references.get(ref_id) or _ref(identity, trace["page"], ref_id,
                geometry_kind=trace["geometry_kind"])
            result[field["name"]].append({"observed": trace["observed"], "page": trace["page"],
                "label": trace["label_text"], "association": trace["rule"],
                "source_section": trace.get("source_section"), "ref": reference,
                "eligible_for_field": trace["eligible_for_field"], "source_uncertain": trace["status"] == "UNCERTAIN",
                "trace": {**trace, "mechanism": identity["mechanism"], "source_refs": [reference]}})
    return result


def _literal_locations_v2(cell, facts):
    """Literal presence is diagnostic only; it never proves a semantic role."""
    observed = cell.get("observed")
    if not isinstance(observed, str) or not observed.strip():
        return []
    pattern = re.compile(r"(?<!\w)" + re.escape(observed.strip()) + r"(?!\w)")
    return [{**fact["ref"], "proof_scope": "UNASSOCIATED_LITERAL_ONLY"} for fact in facts
        if pattern.search(fact["text"])]


def _column_header_v2(texts, fields):
    from .document_semantic_vocabulary import caption_role, role_captions

    mapping: dict[int, str] = {}
    for index, text in enumerate(texts):
        # Only declared currency/unit annotations may decorate a role caption.
        # The qualifier is retained on the source row; arbitrary prose is not removed.
        candidates = []
        for field in fields:
            if field["name"] in EVIDENCE_ONLY_ROLES:
                continue
            variants = [str(text).strip()]
            monetary = field["name"] in {"unit_price", "amount", "subtotal", "tax", "total", "opening_balance", "closing_balance", "balance"}
            units = [*field.get("currency_symbols", []), *(["USD", "COP", "EUR", "GBP", "CHF"] if monetary else [])]
            for unit in units:
                if not unit:
                    continue
                pattern = r"\s*\(?\s*" + re.escape(unit) + r"\s*\)?\s*$"
                variants.append(re.sub(pattern, "", str(text).strip(), flags=re.IGNORECASE).strip())
            if any(_key(value) in {_key(alias) for alias in role_captions(field)}
                    or caption_role(value) == field["name"] for value in variants):
                candidates.append(field["name"])
        if len(candidates) != 1 or candidates[0] in mapping.values():
            return None
        mapping[index] = candidates[0]
    required = [field for field in fields if field.get("required", True) and field["name"] not in EVIDENCE_ONLY_ROLES]
    return mapping if len(mapping) >= 2 and all(field["name"] in mapping.values() for field in required) else None


def _inventory_signature_v2(inventory, table):
    rows = []
    for row in inventory["rows"]:
        values = []
        for field in table["fields"]:
            if field["name"] in EVIDENCE_ONLY_ROLES:
                continue
            value, status = _normalized(row["values"].get(field["name"]), field)
            if status != "VALID":
                return None
            values.append(_comparison(value, field))
        rows.append(canonical_hash([row["page"], values]))
    return canonical_hash(sorted(Counter(rows).items()))


def _source_page_v2(source, page):
    return {"page_one_based": page["page"], "geometry": {"unit": "NORMALIZED", "width": 1, "height": 1},
        "blocks": [{"id": band["id"], "text": band["text"], "geometry": band["region"], "type": "text"}
            for band in page["bands"]], "tables": []}


def _unclassified_scope_v2(facts, accounted, definition, expected_pages):
    """Keep every unaccounted source occurrence, regardless of numeric content.

    Only independently located page counters and explicit source heading roles
    with a declared heading literal may be excluded. Free text, a role word or a
    reader's choice to put something outside an HTML table proves no absence.
    """
    headings = {_key(table["name"].replace("_", " ")) for table in definition["tables"]}
    headings.update({"invoice", "factura", "statement", "extracto", "certificate", "certificado", "totals", "totales"})
    result = []
    for fact in facts:
        if fact["ref"]["id"] in accounted:
            continue
        region = fact.get("region")
        footer = bool(_box(region) and region[1] >= .85
            and _page_marker(fact["text"], fact["page"], expected_pages))
        heading = bool(fact.get("block_role") in {"doc_title", "paragraph_title", "header"}
            and _key(fact["text"]) in headings)
        if not footer and not heading and fact["text"].strip():
            result.append(fact["ref"])
    return result


def _native_inventory_v2(source, definition):
    inventories: dict[str, Any] = {table["name"]: {"rows": [], "headers": [], "issues": [], "source": source["identity"],
        "scope_refs": [], "complete": False} for table in definition["tables"]}
    source_facts = _native_facts_v2(source)
    references = {fact["ref"]["id"]: fact["ref"] for fact in source_facts}
    pages = [_source_page_v2(source, page) for page in source["pages"]]
    observations = _source_headers_v2(pages, definition, source["identity"], references)
    fields_by_name = {field["name"]: field for field in definition["fields"]}
    positive = {reference for name, facts in observations.items() for fact in facts
        if fact.get("eligible_for_field") and not fact.get("source_uncertain")
        and _normalized(fact["observed"], fields_by_name[name])[1] == "VALID"
        for reference in [*fact["trace"]["label_refs"], *fact["trace"]["value_refs"]]}
    closing_roles = {"subtotal", "tax", "total", "closing_balance"}
    closing = {fact["ref"]["id"] for name, facts in observations.items() if name in closing_roles
        for fact in facts if fact.get("eligible_for_field") and not fact.get("source_uncertain")
        and _normalized(fact["observed"], fields_by_name[name])[1] == "VALID"}
    consumed: set[str] = set()
    carry: Any = None
    previous_page = None
    for page in source["pages"]:
        current = carry if previous_page is not None and page["page"] == previous_page + 1 and len(inventories) == 1 else None
        continued = current is not None
        for band in page["bands"]:
            segments = _segments(band)
            headers = [(table, mapping) for table in definition["tables"]
                if (mapping := _column_header_v2([segment["text"] for segment in segments], table["fields"]))]
            ref = references[band["id"]]
            if len(headers) == 1:
                table, mapping = headers[0]
                current = (table, mapping, segments)
                inventories[table["name"]]["headers"].append({"page": page["page"], "ref": ref,
                    "columns": mapping, "basis": "MEASURED_ROLE_COLUMNS"})
                inventories[table["name"]]["scope_refs"].append(ref)
                consumed.add(band["id"])
                continued = False
                continue
            if len(headers) > 1:
                for table, _mapping in headers:
                    inventories[table["name"]]["issues"].append("ASSOCIATION_AMBIGUOUS")
                current = None
                continue
            if band["id"] in closing:
                if current is not None:
                    inventories[current[0]["name"]]["scope_refs"].append(ref)
                current = None
                continue
            if band["id"] in positive or _page_marker(band["text"], page["page"], len(source["expected_pages"])):
                continue
            if current is None:
                # Unaccounted separated numeric cells can be an omitted table.
                if len(segments) >= 2 and any(re.search(r"\d", segment["text"]) for segment in segments):
                    for inventory in inventories.values():
                        inventory["issues"].append("UNASSOCIATED_SOURCE_TABLE")
                continue
            table, mapping, header = current
            inventory = inventories[table["name"]]
            centers = [(segment["region"][0] + segment["region"][2]) / 2 for segment in header]
            bounds = [0, *[(left + right) / 2 for left, right in pairwise(centers)], 1]
            columns: dict[str, list[dict[str, Any]]] = defaultdict(list)
            for word in band["words"]:
                center = (word["region"][0] + word["region"][2]) / 2
                located = [index for index in mapping if bounds[index] <= center < bounds[index + 1]]
                if len(located) != 1:
                    inventory["issues"].append("ROW_IDENTITY_AMBIGUOUS")
                    continue
                columns[mapping[located[0]]].append(word)
            values = {name: " ".join(word["text"] for word in words) for name, words in columns.items()}
            fields = [field for field in table["fields"] if field["name"] not in EVIDENCE_ONLY_ROLES]
            required = [field for field in fields if field.get("required", True)]
            numeric_roles = [field for field in fields if field["logical_type"] in {"DECIMAL", "INT64", "DATE"}]
            # A continuation needs measured role-aligned row anchors, never just
            # the next page or a repeated code. Unknown prose terminates it.
            anchored = any(field["name"] in values and _normalized(values[field["name"]], field)[1] == "VALID"
                for field in numeric_roles)
            if not anchored or sum(field["name"] in values for field in required) < min(2, len(required)):
                inventory["issues"].append("UNPARSED_SOURCE_STRUCTURE")
                current = None
                continue
            if continued:
                inventory["headers"].append({"page": page["page"], "ref": ref, "columns": mapping,
                    "basis": "MEASURED_CONTINUATION_OF_PREVIOUS_ROLE_COLUMNS"})
                continued = False
            if any(field["name"] not in values for field in required):
                inventory["issues"].append("MISSING_REQUIRED_COLUMN")
            inventory["rows"].append({"id": band["id"], "page": page["page"], "values": values,
                "quote": band["text"], "ref": ref, "source_refs": [word["id"] for word in band["words"]],
                "cell_refs": {name: [_ref(source["identity"], page["page"], word["id"],
                    geometry_kind="MEASURED_SOURCE", artifact_sha256=source["artifact_sha256"]) for word in words]
                    for name, words in columns.items()}, "columns": list(mapping.values())})
            consumed.add(band["id"])
        carry, previous_page = current, page["page"]
    # A field caption inside a detail row is not a document-header assertion.
    observations = {name: [fact for fact in facts if fact["ref"]["id"] not in consumed]
        for name, facts in observations.items()}
    unclassified = _unclassified_scope_v2(source_facts, consumed | positive, definition, len(source["expected_pages"]))
    for inventory in inventories.values():
        if not source["complete"]:
            inventory["issues"].append("SOURCE_SCOPE_INCOMPLETE")
        if unclassified:
            inventory["issues"].append("UNCLASSIFIED_SOURCE_SCOPE")
        inventory["issues"] = sorted(set(inventory["issues"]))
        inventory["complete"] = bool(inventory["headers"] and inventory["rows"] and not inventory["issues"])
        inventory["unclassified_source_refs"] = copy.deepcopy(unclassified)
    return inventories, observations


def _paddle_inventory_v2(ir, control, definition):
    identity = control["identity"]
    pages = [page for page in ir["pages"] if page["stage"] == identity["stage"]]
    facts = _paddle_facts_v2(ir, control)
    references = {fact["ref"]["id"]: fact["ref"] for fact in facts}
    observations = _source_headers_v2(pages, definition, identity, references)
    fields_by_name = {field["name"]: field for field in definition["fields"]}
    positive = {reference for name, values in observations.items() for fact in values
        if fact.get("eligible_for_field") and not fact.get("source_uncertain")
        and _normalized(fact["observed"], fields_by_name[name])[1] == "VALID"
        for reference in [*fact["trace"]["label_refs"], *fact["trace"]["value_refs"]]}
    inventories: dict[str, Any] = {table["name"]: {"rows": [], "headers": [], "issues": [], "source": identity,
        "scope_refs": [], "complete": False} for table in definition["tables"]}
    consumed: set[str] = set()
    carry: Any = None
    previous_page = None
    for page in pages:
        page_number = page["page_one_based"]
        for physical in page["tables"]:
            owners = {cell["id"]: cell for cell in physical["cells"]}
            rows = [[owners.get(slot["cell_id"]) for slot in row["slots"]] for row in physical["rows"]]
            candidates = [(table, index, mapping) for table in definition["tables"] for index, row in enumerate(rows)
                if all(cell is not None and cell["rowspan"] == cell["colspan"] == 1 for cell in row)
                and (mapping := _column_header_v2([cast(dict[str, Any], cell)["text"] for cell in row], table["fields"]))]
            names = {table["name"] for table, _index, _mapping in candidates}
            if len(names) > 1:
                for inventory in inventories.values():
                    inventory["issues"].append("ASSOCIATION_AMBIGUOUS")
                carry = None
                continue
            # Complete key/value header tables are document fields, not omitted
            # detail tables. Every nonempty cell must have a proven role.
            if not candidates and all(cell["id"] in positive or not cell["text"].strip() for cell in physical["cells"]):
                continue
            if not candidates and carry is not None and previous_page is not None and page_number == previous_page + 1 and len(inventories) == 1:
                table, mapping = carry
                if all(len(row) == len(mapping) for row in rows):
                    candidates = [(table, -1, mapping)]
                    names = {table["name"]}
            if not candidates:
                for inventory in inventories.values():
                    inventory["issues"].append("UNASSOCIATED_SOURCE_TABLE")
                continue
            table, first, mapping = candidates[0]
            inventory = inventories[table["name"]]
            if physical["errors"]:
                inventory["issues"].append("UNPARSED_SOURCE_STRUCTURE")
            headers = {index: header_mapping for _table, index, header_mapping in candidates}
            for index, row in enumerate(rows):
                unique = {cell["id"]: cell for cell in row if cell is not None}
                if index in headers:
                    mapping = headers[index]
                    reference = _ref(identity, page_number, physical["rows"][index]["id"])
                    inventory["headers"].append({"page": page_number, "ref": reference,
                        "columns": mapping, "basis": "LOGICAL_ROLE_COLUMNS"})
                    consumed.update(unique)
                    continue
                if unique and all(cell["id"] in positive or not cell["text"].strip() for cell in unique.values()):
                    inventory["scope_refs"].extend(references[cell_id] for cell_id in unique if cell_id in references)
                    continue
                if first >= 0 and index < first:
                    inventory["issues"].append("UNPARSED_SOURCE_STRUCTURE")
                    continue
                if any(cell is None or cell["rowspan"] != 1 or cell["colspan"] != 1 for cell in row) or len(row) != len(mapping):
                    inventory["issues"].append("UNPARSED_SOURCE_STRUCTURE")
                    continue
                values = {name: row[column]["text"] for column, name in mapping.items()}
                fields = [field for field in table["fields"] if field["name"] not in EVIDENCE_ONLY_ROLES]
                if not any(field["logical_type"] in {"DECIMAL", "INT64", "DATE"} and field["name"] in values
                        and _normalized(values[field["name"]], field)[1] == "VALID" for field in fields):
                    inventory["issues"].append("ROW_ASSOCIATION_NOT_PROVEN")
                reference = _ref(identity, page_number, physical["rows"][index]["id"])
                if first == -1 and not inventory["headers"] or first == -1 and not any(header["page"] == page_number for header in inventory["headers"]):
                    inventory["headers"].append({"page": page_number, "ref": reference,
                        "columns": mapping, "basis": "LOGICAL_CONTINUATION_OF_PREVIOUS_ROLE_COLUMNS"})
                inventory["rows"].append({"id": physical["rows"][index]["id"], "page": page_number,
                    "values": values, "quote": " ".join(cast(dict[str, Any], cell)["text"] for cell in row), "ref": reference,
                    "source_refs": list(unique), "columns": list(mapping.values()),
                    "cell_refs": {name: [references[row[column]["id"]]] for column, name in mapping.items()}})
                consumed.update(unique)
            carry = (table, mapping)
        previous_page = page_number
    observations = {name: [fact for fact in values if fact["ref"]["id"] not in consumed]
        for name, values in observations.items()}
    unclassified = _unclassified_scope_v2(facts, consumed | positive, definition, ir["expected_pages"])
    for inventory in inventories.values():
        if unclassified:
            inventory["issues"].append("UNCLASSIFIED_SOURCE_SCOPE")
        inventory["issues"] = sorted(set(inventory["issues"]))
        inventory["complete"] = bool(inventory["headers"] and inventory["rows"] and not inventory["issues"])
        inventory["unclassified_source_refs"] = copy.deepcopy(unclassified)
    return inventories, observations


def _complete_source_checks_v2(proof, data, definition, native, inventories, facts, ir):
    from .document_printed_pagination import assess_documentary_pagination

    physical = list(range(1, (ir or {}).get("expected_pages", 0) + 1))
    processed = sorted(set(data["engine"].get("execution", {}).get("completed_pages", [])))
    integrity_issues = list(proof["global_issues"])
    pagination = assess_documentary_pagination(facts, physical_pages=physical, processed_pages=processed)
    documentary_issues = list(pagination["reasons"]) if pagination.get("blocking") else []
    contradictions = [issue for issue in proof["issues"] if issue.startswith(("DOCUMENTARY_SOURCE_CONTRADICTION:", "SOURCE_INVENTORY_CONTRADICTION:"))]
    # A confirmation flag cannot repair a missing documentary page or a proved
    # disagreement in the source. Integrity remains a separate factual check.
    proof["global_issues"] = sorted({*integrity_issues, *documentary_issues, *contradictions})
    proof["issues"] = sorted({*proof["issues"], *documentary_issues, *contradictions})
    available = {fact["page"] for fact in facts}
    incomplete = set(native.get("incomplete_scope_pages", [])) if native is not None else set()
    paddle_pages = {fact["page"] for fact in facts if fact["ref"].get("mechanism") == "PADDLE"}
    incomplete -= paddle_pages
    missing_text = (set(physical) - available) | incomplete
    if missing_text:
        # This is an unresolved source-scope control, not corrupt evidence. It
        # must remain actionable even for definitions with no physical tables.
        proof["issues"] = sorted({*proof["issues"], "SOURCE_SCOPE_INCOMPLETE"})
    references = list({canonical_hash(fact["ref"]): fact["ref"] for fact in facts}.values())
    total = len(proof["corroboration"])
    verified = len(proof["verified_cells"])
    associations = list(proof["corroboration"].values())
    verified_associations = sum(bool(value["association"]) for value in associations)
    covered = [name for name, value in proof["table_coverage"].items() if value]
    pending = [name for name, value in proof["table_coverage"].items() if not value]
    execution_complete = bool(proof["recognition_complete"] and physical and processed == physical)
    required_present = all(isinstance(data["payload"].get("fields", {}).get(field["name"]), dict)
        and data["payload"]["fields"][field["name"]].get("validity") == "VALID"
        and data["payload"]["fields"][field["name"]].get("value") is not None
        for field in definition["fields"] if field.get("required", True))
    unproved_scope = any(reason in {"TEXT_SCOPE_UNVERIFIED", "NARRATIVE_SEMANTICS_UNVERIFIED"}
        for value in proof["corroboration"].values() for reason in value["reasons"])
    complete = bool(execution_complete and not missing_text and not documentary_issues and not pending and required_present and not unproved_scope)
    document_status = ("INCOMPLETE" if pagination["status"] == "INCOMPLETE" else "CONFLICT"
        if pagination["status"] == "CONFLICT" or contradictions else "VERIFIED" if complete else "PENDING")
    source_reasons = list(dict.fromkeys([*integrity_issues, *documentary_issues,
        *(["SOURCE_SCOPE_INCOMPLETE"] if missing_text else []),
        *(code for detail in proof["coverage_details"].values() for code in detail["reasons"])]))
    proof["verification_contract_sha256"] = definition["semantic_general"]["verification_contract_sha256"]
    proof["printed_pagination"] = pagination
    proof["source_checks"] = {
        "version": "SEMANTIC_SOURCE_CHECKS_V2",
        "integrity": {"status": "UNVERIFIED" if integrity_issues else "VERIFIED", "reasons": integrity_issues, "source_refs": references},
        "text": {"status": "UNAVAILABLE" if not available else "PARTIAL" if missing_text else "AVAILABLE",
            "available_pages": sorted(available), "missing_pages": sorted(missing_text), "source_refs": references},
        "execution_pages": {"status": "COMPLETE" if execution_complete else "INCOMPLETE",
            "physical_pages": physical, "processed_pages": processed, "missing_pages": sorted(set(physical) - set(processed))},
        "associations": {"status": "VERIFIED" if verified_associations == len(associations) else "PENDING",
            "verified": verified_associations, "pending": len(associations) - verified_associations},
        "table_coverage": {"status": "PENDING" if pending else "VERIFIED", "verified": covered, "pending": pending},
        "cell_corroboration": {"status": "VERIFIED" if verified == total else "PENDING", "verified": verified, "pending": total - verified},
        "documentary_completeness": {"status": document_status, "scope": "DECLARED_EXTRACTION_CONTRACT",
            "reasons": source_reasons},
        "automatic_eligibility": {"status": "BLOCKED" if proof["issues"] else "ELIGIBLE", "reasons": list(proof["issues"])},
    }
    proof["source_inventory_version"] = "MEASURED_SOURCE_TABLE_OCCURRENCES_V2"


def _verification_decision_v2(data, definition, proof):
    structural_codes = {"ASSOCIATION_NOT_PROVEN", "SOURCE_DATA_NOT_OBSERVED", "ROW_ASSOCIATION_NOT_PROVEN",
        "ROW_COVERAGE_UNPROVEN", "UNPARSED_SOURCE_STRUCTURE", "UNASSOCIATED_SOURCE_TABLE",
        "UNCLASSIFIED_SOURCE_SCOPE", "MISSING_REQUIRED_COLUMN", "SOURCE_SCOPE_INCOMPLETE"}
    reasons = []
    pending_controls = []
    for issue in proof["issues"]:
        code, _separator, location = issue.partition(":")
        reason = {"kind": "INTEGRITY" if issue in proof["global_issues"] else "INCIDENT", "code": code}
        if location:
            reason["path" if location.count(".") == 2 else "table"] = location
        reasons.append(reason)
        if code in structural_codes:
            pending_controls.append(reason)
    families = {source["mechanism"] for source in proof["recognition_sources"]}
    narrative = definition["semantic_general"]["contract"]["document_type"] == "NARRATIVE"
    actionable = bool(pending_controls and not proof["global_issues"] and families == {"QWEN"} and not narrative)
    scope = {"kind": "WHOLE_DOCUMENT", "pages": list(data["payload"].get("pages", [])),
        "focal_reread": False, "reason": "UNPROVEN_SOURCE_ASSOCIATIONS_OR_COLLECTION_STRUCTURE"}
    return {"version": VALIDATOR_VERSION_V2, "required": bool(reasons), "actionable": actionable,
        "reasons": reasons, "pending_controls": pending_controls, "scope": scope,
        "decision_sha256": canonical_hash({"reasons": reasons, "scope": scope,
            "verification_contract_sha256": proof["verification_contract_sha256"]}),
        "native_scope_complete": proof["native_scope_complete"], "source_checks": copy.deepcopy(proof["source_checks"]),
        "independent_mechanisms_already_attempted": sorted(families)}
