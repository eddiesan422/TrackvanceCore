"""Assisted proposals from real local OCR geometry; proposals never activate."""
from itertools import pairwise

from .document_schemas import DocumentDefinition
from .document_template_builder_rules import (
    box,
    field_candidates,
    field_name,
    infer_type,
    inside,
    table_candidates,
    visual_lines,
    words,
)
from .document_template_builder_schemas import BuilderProposal
from .operations_common import OperationError


def propose(lines: list[dict], body: BuilderProposal) -> dict:
    if any(not line.get("words") for line in lines):
        raise OperationError(409, "DOCUMENT_BUILDER_GEOMETRY", "Prueba de nuevo este ejemplo para obtener las coordenadas de sus palabras.")
    lines = visual_lines(lines)
    region = body.selection.region
    selected_lines = [line for line in lines if line["page"] == body.selection.page
                      and any(inside(word, region) for word in words(line))]
    if not selected_lines:
        raise OperationError(422, "DOCUMENT_BUILDER_EMPTY", "Selecciona texto reconocido en el documento.")
    if any(not line.get("words") for line in selected_lines):
        raise OperationError(409, "DOCUMENT_BUILDER_GEOMETRY", "Prueba de nuevo este ejemplo para obtener las coordenadas de sus palabras.")
    selected_lines.sort(key=lambda line: (line["region"][1], line["region"][0]))
    if body.kind == "TABLE":
        header = selected_lines[0]
        labels = [word for word in words(header) if inside(word, region)]
        if len(labels) < 2:
            raise OperationError(422, "DOCUMENT_BUILDER_COLUMNS", "Incluye la cabecera y al menos dos columnas en la selección.")
        boundaries = [region[0]] + [(a["region"][2] + b["region"][0]) / 2 for a, b in pairwise(labels)] + [region[2]]
        after = [line for line in lines if line["page"] == header["page"] and line["region"][1] >= region[3]]
        stop = min(after, key=lambda line: line["region"][1])["text"].split(":", 1)[0].strip() if after else ""
        rule = {"page": header["page"], "region": [labels[0]["region"][0], header["region"][1], region[2], region[3]],
                "header_labels": [w["text"].strip(":") for w in labels], "column_bounds": boundaries,
                "stop_label": stop[:160], "tolerance": .12}
        candidates, _ = table_candidates(lines, rule)
        names = []
        for index, label in enumerate(rule["header_labels"]):
            name = field_name(label, f"columna_{index + 1}")
            names.append(name if name not in names else f"{name}_{index + 1}")
        fields = [{"name": name, "label": label, "logical_type": infer_type(candidates[0][index][0]) if candidates else "STRING",
                   "required": True, "decimal_separator": "," if candidates and "," in candidates[0][index][0] else "."}
                  for index, (name, label) in enumerate(zip(names, rule["header_labels"], strict=True))]
        table = {"name": "detalle", "fields": fields, "required": True, "visual": rule}
        validated = DocumentDefinition.model_validate({"fields": [{"name": "documento", "required": False}], "tables": [table]})
        return {"table": validated.model_dump(mode="json")["tables"][0], "sample_rows": len(candidates),
                "notice": "Revisa nombres, tipos y límites; la cabecera textual ancla las columnas. Prueba cada formato antes de activarlo."}
    line = selected_lines[0]
    tokens = words(line)
    chosen = [word for word in tokens if inside(word, region)]
    first = tokens.index(chosen[0])
    label = " ".join(word["text"] for word in tokens[:first]).strip(" :=")
    prior_colons = [index for index, word in enumerate(tokens[:first]) if word["text"].endswith(":")]
    if len(prior_colons) > 1:
        label = tokens[prior_colons[-1]]["text"].strip(" :=")
    value = " ".join(word["text"] for word in chosen)
    direction = "RIGHT"
    if not label and ":" in value:
        label, value = [part.strip() for part in value.split(":", 1)]
    if not label:
        previous = [other for other in lines if other["page"] == line["page"]
                    and 0 <= line["region"][1] - other["region"][3] <= .12 and other["region"][3] <= line["region"][1]]
        if previous:
            label = min(previous, key=lambda other: line["region"][1] - other["region"][3])["text"].strip(" :=")
            direction = "BELOW"
    if not label:
        raise OperationError(422, "DOCUMENT_BUILDER_LABEL", "Incluye la etiqueta y el valor, o selecciona el valor situado junto a su etiqueta.")
    following = tokens[tokens.index(chosen[-1]) + 1:]
    end_label = next((word["text"].strip(" :=") for word in following if word["text"].endswith(":")), "")
    rule = {"page": line["page"], "region": box(chosen), "label": label[:160], "direction": direction,
            "kind": "LABEL_VALUE", "sample_value": value[:500], "context": line["text"][:160], "tolerance": .12}
    rule["end_label"] = end_label
    candidate = field_candidates(lines, rule)
    observed = candidate[0][0] if len(candidate) == 1 else value
    field = {"name": field_name(label), "label": label[:160], "logical_type": infer_type(observed), "visual": rule,
             "required": True, "decimal_separator": "," if "," in observed else "."}
    validated = DocumentDefinition.model_validate({"fields": [field]})
    return {"field": validated.model_dump(mode="json")["fields"][0], "sample_value": observed,
            "notice": "Propuesta basada en etiqueta y contexto reales. Confirma el tipo; los identificadores con ceros iniciales se conservan como texto."}
