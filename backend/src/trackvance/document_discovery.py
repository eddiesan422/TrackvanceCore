"""Bounded, local proposals from every configuration reference, without an oracle."""
import copy
import re
from itertools import pairwise

from .document_engine import extract
from .document_lossless import map_representation
from .document_schemas import DocumentDefinition, DocumentDraftDefinition
from .document_template_builder_rules import (
    anchor,
    box,
    field_candidates,
    field_name,
    normalized,
    table_candidates,
    visual_lines,
)
from .operations_common import OperationError

# Ordered longest aliases first, to avoid proposing the same label twice.
FIELDS = {
    "numero_factura": ("DOCUMENT", "STRING", ["Factura electrónica de venta No.", "Factura electrónica de venta", "Número de factura", "Factura No.", "Factura", "Invoice"]),
    "fecha_emision": ("DOCUMENT", "DATE", ["Fecha de emisión", "Fecha de expedición", "Fecha factura", "Fecha", "Date"]),
    "fecha_vencimiento": ("DOCUMENT", "DATE", ["Fecha de vencimiento", "Vencimiento", "Due date"]),
    "orden_compra": ("DOCUMENT", "STRING", ["Orden de compra", "Orden compra", "Purchase order"]),
    "moneda": ("DOCUMENT", "STRING", ["Moneda", "Currency"]),
    "emisor": ("ISSUER", "STRING", ["Razón social emisor", "Razón social", "Emisor", "Proveedor", "Seller"]),
    "adquiriente": ("BUYER", "STRING", ["Razón social adquiriente", "Adquiriente", "Cliente", "Comprador", "Buyer"]),
    "nit_emisor": ("ISSUER", "STRING", ["NIT emisor", "NIT", "Identificación", "Tax ID"]),
    "nit_adquiriente": ("BUYER", "STRING", ["NIT adquiriente", "NIT", "Identificación", "Tax ID"]),
    "subtotal": ("TOTALS", "DECIMAL", ["Subtotal", "Sub total"]),
    "base_imponible": ("TOTALS", "DECIMAL", ["Base imponible", "Base gravable"]),
    "descuento": ("TOTALS", "DECIMAL", ["Descuento", "Descuentos"]),
    "impuesto": ("TOTALS", "DECIMAL", ["Total IVA", "IVA", "Impuesto"]),
    "total": ("TOTALS", "DECIMAL", ["Total a pagar", "Valor total", "Total factura", "Total"]),
}
PARTIES = {"ISSUER": ["Emisor", "Proveedor", "Vendedor", "Seller"],
           "BUYER": ["Adquiriente", "Cliente", "Comprador", "Buyer"]}
COLUMNS = ["Descripción / Código", "Descripción y Código", "Código producto", "Código", "Code", "Descripción del producto", "Descripción", "Description",
           "Cantidad", "Cant.", "Cant", "Qty", "Unidad de medida", "Unidad", "Precio unitario", "Valor unitario", "Unit price",
           "Descuento", "IVA", "Impuesto", "Valor total", "Total", "Importe", "Amount"]


def party_heading(lines, line):
    headings = [(candidate, section, label) for candidate in lines
                if candidate["page"] == line["page"] and candidate["region"][1] <= line["region"][1]
                for section, labels in PARTIES.items() for label in labels if anchor(candidate, label)]
    if not headings:
        return None, ""
    candidate, section, label = max(headings, key=lambda item: item[0]["region"][1])
    return (section, label) if line["region"][1] - candidate["region"][1] <= .24 else (None, "")


def section_lines(lines, section):
    if section not in PARTIES:
        return lines, None, ""
    headings = [(line, label, match) for line in lines for label in PARTIES[section] if (match := anchor(line, label))]
    if not headings:
        return lines, None, ""
    heading, label, match = headings[0]
    peers = [candidate for labels in PARTIES.values() for other in labels if (candidate := anchor(heading, other))]
    left = max([0, *[(peer[2][2] + match[2][0]) / 2 for peer in peers if peer[2][2] < match[2][0]]])
    right = min([1, *[(peer[2][0] + match[2][2]) / 2 for peer in peers if peer[2][0] > match[2][2]]])
    result = []
    for line in lines:
        if line["page"] != heading["page"] or not heading["region"][1] <= line["region"][1] <= heading["region"][1] + .24:
            continue
        tokens = [word for word in line["words"] if left <= (word["region"][0] + word["region"][2]) / 2 <= right]
        if tokens:
            result.append({**line, "words": tokens, "region": box(tokens), "text": " ".join(word["text"] for word in tokens)})
    return result, [left, right], label


def extract_reference(reference, definition):
    """Use direct documentary cells when present; keep historical lines unchanged."""
    payload = reference.payload
    if "document_representation" not in payload:
        return extract(payload["lines"], definition, payload.get("page_geometry"))
    representation = payload["document_representation"]
    stages = list(dict.fromkeys(page["stage"] for page in representation["pages"]))
    stage = next((value for value in stages if value.startswith("QWEN")), stages[0] if stages else None)
    result = map_representation(representation, definition, stage=stage)
    result["lines"] = copy.deepcopy(payload["lines"])
    if "page_geometry" in payload:
        result["page_geometry"] = copy.deepcopy(payload["page_geometry"])
    return result


def _discover_ir(references):
    """Propose from explicit text labels/HTML headers, without invented cell boxes."""
    observations: dict[str, list[dict]] = {}
    fields: dict[str, dict] = {}
    proposals, unproposed = [], []
    aliases: dict[str, set[str]] = {}
    for name, (_, _, labels) in FIELDS.items():
        for label in labels:
            aliases.setdefault(normalized(label), set()).add(name)
    known = {label: next(iter(names)) for label, names in aliases.items() if len(names) == 1}
    column_labels = {normalized(label) for label in COLUMNS}

    def observe(reference_id, label, value, evidence):
        label, value = label.strip().rstrip(":"), value.strip()
        if not label or len(label) > 160 or not value:
            return
        name = known.get(normalized(label), field_name(label))
        if name not in fields:
            if len(fields) >= 100:
                raise OperationError(422, "DOCUMENT_DISCOVERY_VARIANTS_LIMIT", "Las etiquetas explícitas superan 100 campos; reduce referencias o selecciona las etiquetas necesarias.")
            # Type and normalization are editable declarations, never inferred
            # from a code, a currency symbol, or a mathematically plausible value.
            fields[name] = {"name": name, "label": label, "logical_type": "STRING"}
        observations.setdefault(name, []).append({"reference_id": reference_id, "value": value,
            "source_evidence_id": evidence, "geometry_kind": "UNKNOWN"})

    for reference in references:
        representation = reference.payload.get("document_representation") or {}
        for line_index, line in enumerate(reference.payload["lines"]):
            for text in line["text"].splitlines():
                label, separator, value = text.partition(":")
                if separator:
                    observe(reference.id, label, value, "line:" + str(line_index))
        for page in representation.get("pages", []):
            if page.get("recognition_status") == "INCOMPLETE":
                continue
            for table in page.get("tables", []):
                owners = {cell["id"]: cell for cell in table["cells"]}
                rows = [[owners.get(slot["cell_id"]) for slot in row["slots"]] for row in table["rows"]]
                header = None
                header_cells = []
                for index, row in enumerate(rows):
                    present = [cell for cell in row if cell is not None]
                    if (not 2 <= len(row) <= 40 or len(present) != len(row)
                            or any(cell["rowspan"] != 1 or cell["colspan"] != 1 for cell in present)):
                        continue
                    labels = [cell["text"].strip() for cell in present]
                    explicit = all(cell.get("tag") == "th" for cell in present)
                    if not explicit:
                        explicit = sum(normalized(label) in column_labels for label in labels) >= 2
                    if explicit and all(labels) and len({normalized(label) for label in labels}) == len(labels):
                        header = index
                        header_cells = present
                        break
                if header is None:
                    for row in rows:
                        if len(row) == 2:
                            label_cell, value_cell = row
                            if label_cell is not None and value_cell is not None and (label_cell["text"].rstrip().endswith(":") or normalized(label_cell["text"]) in known):
                                observe(reference.id, label_cell["text"], value_cell["text"], value_cell["id"])
                    unproposed.append({"reference_id": reference.id, "source_table_id": table["id"],
                        "reason": "EXPLICIT_HEADER_NOT_LOCATED", "geometry_kind": "UNKNOWN"})
                    continue
                labels = [cell["text"].strip() for cell in header_cells]
                names = [field_name(label, "column_" + str(index + 1)) for index, label in enumerate(labels)]
                if len(set(names)) != len(names):
                    unproposed.append({"reference_id": reference.id, "source_table_id": table["id"],
                        "reason": "COLUMN_NAMES_AMBIGUOUS", "geometry_kind": "UNKNOWN"})
                    continue
                proposed = {"name": "detalle", "fields": [{"name": name, "label": label, "logical_type": "STRING"}
                    for name, label in zip(names, labels, strict=True)], "structured_header_labels": labels}
                proposals.append({"reference_id": reference.id, "definition": proposed, "source_table_id": table["id"],
                    "source_header_row_id": table["rows"][header]["id"],
                    "source_body_row_ids": [row["id"] for row in table["rows"][header + 1:]],
                    "geometry_kind": "UNKNOWN", "query_geometry": copy.deepcopy(table.get("query_geometry"))})
    for name, field in fields.items():
        field["required"] = len({sample["reference_id"] for sample in observations[name]}) == len(references)
    tables = []
    if proposals:
        chosen = max(proposals, key=lambda proposal: sum(proposal["definition"]["structured_header_labels"]
            == other["definition"]["structured_header_labels"] for other in proposals))
        tables = [chosen["definition"]]
    definition = DocumentDraftDefinition.model_validate({"fields": list(fields.values()), "tables": tables}).model_dump(mode="json")
    return {"definition": definition, "observations": observations, "table_alternatives": proposals,
        "unproposed_tables": unproposed, "results": {reference.id: extract_reference(reference, definition)
            for reference in references} if fields else {},
        "notice": ("Propuesta editable desde estructura original; confirma tipos, etiquetas y filas sin geometría de celda medida."
            if fields else "Se conservaron las tablas originales; configura al menos un campo de cabecera antes de probar la plantilla.")}


def discover(references):
    if any(page.get("stage") != "DIGITAL_NATIVE" for reference in references
            for page in reference.payload.get("document_representation", {}).get("pages", [])):
        return _discover_ir(references)
    observations: dict[str, list[dict]] = {name: [] for name in FIELDS}
    table_proposals: list[dict] = []
    for reference in references:
        lines = visual_lines(reference.payload["lines"], .012)
        for name, (section, kind, aliases) in FIELDS.items():
            context, section_bounds, section_heading = section_lines(lines, section)
            for line in context:
                matched = next(((label, found) for label in aliases if (found := anchor(line, label))), None)
                if not matched:
                    continue
                label, found = matched
                party, heading = party_heading(context, line)
                if name.startswith("nit_") and party and party != section:
                    continue
                remaining = line["words"][found[1]:]
                # Stop at another explicit label on the same baseline.
                remaining = remaining[:next((i for i, word in enumerate(remaining) if word["text"].endswith(":") and i > 0), len(remaining))]
                value = " ".join(word["text"] for word in remaining).strip(" :=")
                rule = {"page": line["page"], "region": box(remaining) if remaining else found[2],
                        "label": label, "label_aliases": aliases, "kind": "LABEL_VALUE", "direction": "RIGHT",
                        "sample_value": value[:500], "section_label": heading if name.startswith("nit_") and party else "",
                        "require_unique_origin": name.startswith("nit_"), "section_bounds": section_bounds, "tolerance": .12, "baseline_tolerance": .012}
                if name in {"emisor", "adquiriente"} and section_heading:
                    rule.update(label=section_heading, direction="BELOW", label_aliases=[section_heading])
                if name in {"numero_factura", "orden_compra"}:
                    rule["value_pattern"] = r"([A-Z0-9][A-Z0-9._/-]*[0-9][A-Z0-9._/-]*)"
                elif kind == "DATE":
                    rule["value_pattern"] = r"(\d{4}-\d{2}-\d{2}|\d{2}/\d{2}/\d{4})"
                elif name == "moneda":
                    rule["value_pattern"] = r"\b([A-Z]{3})\b"
                elif kind == "DECIMAL":
                    rule["value_pattern"] = r"([+-]?\s*(?:[$]|COP|USD)?\s*[0-9][0-9., ]*[.,][0-9]{2})"
                if not section_bounds and kind in {"DATE", "DECIMAL"} or name in {"numero_factura", "orden_compra", "moneda"}:
                    next_labels = [matched[2][0] for labels in FIELDS.values() for alias in labels[2]
                                   if (matched := anchor(line, alias)) and matched[2][0] > found[2][2]]
                    rule["section_bounds"] = [max(0, found[2][0] - .01), min(next_labels) - .01 if next_labels else 1]
                candidates = field_candidates(reference.payload["lines"], rule)
                if not candidates and section != "TOTALS":
                    rule["direction"] = "BELOW"
                    candidates = field_candidates(reference.payload["lines"], rule)
                if candidates:
                    observations[name].append({"reference_id": reference.id, "value": candidates[0][0], "rule": rule})
        for line in lines:
            found = []
            occupied: set[int] = set()
            for label in COLUMNS:
                match = anchor(line, label)
                if match and not occupied.intersection(range(match[0], match[1])):
                    occupied.update(range(match[0], match[1]))
                    found.append((label, match[2]))
            found.sort(key=lambda item: item[1][0])
            if len(found) < 3 or not any(any(token in normalized(label) for token in ("descripcion", "description", "codigo", "code")) for label, _ in found):
                continue
            labels = [label for label, _ in found]
            bounds = [max(0, found[0][1][0] - .01)] + [(a[1][2] + b[1][0]) / 2 for a, b in pairwise(found)] + [min(1, found[-1][1][2] + .04)]
            columns = []
            for label in labels:
                key = field_name(label)
                kind = "STRING" if any(s in normalized(label) for s in ["codigo", "code", "descripcion", "description", "unidad"]) else "DECIMAL"
                columns.append({"name": key, "label": label, "logical_type": kind, "required": True})
            stops = [other for other in lines if other["page"] == line["page"] and other["region"][1] > line["region"][3]
                     and any(anchor(other, label) for label in ["Subtotal", "Sub total", "Total a pagar", "Valor total", "Total"])]
            stop = next((label for label in ["Subtotal", "Sub total", "Total a pagar", "Valor total", "Total"] if stops and anchor(min(stops, key=lambda other: other["region"][1]), label)), "")
            table_proposals.append({"name": "detalle", "fields": columns, "required": True,
                "visual": {"page": line["page"], "region": [found[0][1][0], line["region"][1], bounds[-1], min(.999, line["region"][3] + .02)],
                    "header_labels": labels, "column_bounds": bounds, "stop_label": stop, "flowing": True, "continue_pages": True,
                    "row_tolerance": .027,
                    "multiline_column": next((i for i, label in enumerate(labels) if "descri" in normalized(label)), None)}})
            sample_rows, _ = table_candidates(reference.payload["lines"], table_proposals[-1]["visual"])
            for index, column in enumerate(columns):
                values = [row[index][0] for row in sample_rows]
                if column["logical_type"] == "DECIMAL" and any(re.search(r",\d{2}$", value) for value in values):
                    column.update(decimal_separator=",", thousands_separator=".", currency_symbols=["$", "COP", "USD"], decimal_format_explicit=True, numeric_fragments=True)
    fields = []
    for name, samples in observations.items():
        if not samples:
            continue
        section, kind, aliases = FIELDS[name]
        field = {"name": name, "section": section, "label": aliases[0], "logical_type": kind,
                 "required": len({s["reference_id"] for s in samples}) == len(references), "visual": samples[0]["rule"]}
        # Each observed placement/section remains a rule candidate. Every rule is
        # applied to every test reference; conflicting candidates remain ambiguous.
        alternatives: list[dict] = []
        def identity(rule):
            return (rule["page"], rule["direction"], rule["section_label"])
        for sample in samples[1:]:
            if identity(sample["rule"]) != identity(field["visual"]) and all(identity(sample["rule"]) != identity(other) for other in alternatives):
                alternatives.append(sample["rule"])
        if len(alternatives) > 20:
            raise OperationError(422, "DOCUMENT_DISCOVERY_VARIANTS_LIMIT", "Este campo supera 20 variantes de página/sección. Divide la familia o ajusta las reglas; no se omitieron variantes silenciosamente.")
        field["visual_alternatives"] = alternatives
        if kind == "DECIMAL":
            # A regional rule is proposed only from an explicit fractional part;
            # 1.234 alone is not enough evidence to infer a thousands separator.
            regional = [s["value"] for s in samples if re.search(r",\d{2}$", s["value"])]
            dotted = [s["value"] for s in samples if re.search(r"\.\d{2}$", s["value"])]
            field["decimal_format_explicit"] = bool(regional or dotted) and not (regional and dotted)
            if regional and not dotted:
                field.update(decimal_separator=",", thousands_separator=".", currency_symbols=["$", "COP", "USD"], numeric_fragments=True)
        if kind == "DATE" and all(re.fullmatch(r"\d{2}/\d{2}/\d{4}", s["value"]) for s in samples):
            field["date_format"] = "%d/%m/%Y"
        fields.append(field)
    if not fields:
        return {"definition": {"fields": [], "tables": []}, "observations": observations,
                "notice": "No se localizaron etiquetas compatibles. Ajusta la selección visual; no se inventaron campos."}
    # Prefer the structure corroborated by the most examples, retaining all
    # alternatives as evidence rather than silently combining different layouts.
    tables = []
    if table_proposals:
        chosen = max(table_proposals, key=lambda t: sum([f["name"] for f in t["fields"]] == [f["name"] for f in other["fields"]] for other in table_proposals))
        tables = [chosen]
    definition = DocumentDefinition.model_validate({"fields": fields, "tables": tables}).model_dump(mode="json")
    return {"definition": definition, "observations": observations,
            "expected_not_located": [name for name, samples in observations.items() if not samples],
            "results": {reference.id: extract_reference(reference, definition) for reference in references},
            "table_alternatives": table_proposals,
            "notice": "Propuesta editable obtenida de todas las referencias de configuración; revisa ambigüedades, formatos y filas no interpretadas."}
