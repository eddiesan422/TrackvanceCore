"""DOC-097: independent references, exact snapshots and independent value oracles."""
import copy
from collections import Counter
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from pydantic import ValidationError
from sqlalchemy import func, select

from trackvance.document_discovery import discover
from trackvance.document_engine import extract, validate_value
from trackvance.document_models import (
    DocumentBuilderTest,
    DocumentDraft,
    DocumentOriginal,
    DocumentReference,
    DocumentTemplate,
)
from trackvance.document_schemas import DocumentDefinition
from trackvance.document_service import digest
from trackvance.document_template_builder_rules import box


def test_table_stop_aliases_separate_page_footers_and_final_page_summary():
    header = line([(.05, "Descripción"), (.5, "Cantidad"), (.7, "Precio"), (.86, "Total")], .2)
    repeated = copy.deepcopy(header)
    repeated["page"] = 2
    source = [header, line([(.05, "A001 producto"), (.5, "2"), (.7, "10.00"), (.86, "20.00")], .25),
              line([(.05, "Documento de muestra")], .9), repeated,
              line([(.05, "A002 servicio"), (.5, "1"), (.7, "3.00"), (.86, "3.00")], .25, 2),
              line([(.05, "Resumen de pago")], .4, 2),
              line([(.05, "Subtotal"), (.86, "23.00")], .45, 2),
              line([(.05, "Documento de muestra")], .9, 2)]
    table = {"name": "detalle", "fields": [{"name": "descripcion"},
        *[{"name": key, "logical_type": "DECIMAL"} for key in ("cantidad", "precio", "total")]],
        "visual": {"page": 1, "region": [.05, .2, .99, .24],
            "header_labels": ["Descripción", "Cantidad", "Precio", "Total"],
            "column_bounds": [0, .45, .65, .83, 1], "stop_label": "Documento de muestra",
            "stop_label_aliases": ["Resumen de pago"], "flowing": True, "continue_pages": True,
            "multiline_column": 0}}
    configured = definition({"fields": [{"name": "opcional", "required": False}], "tables": [table]})
    before = copy.deepcopy(source)
    result = extract(source, configured)
    assert [[row[key]["value"] for key in ("descripcion", "cantidad", "precio", "total")]
            for row in result["tables"]["detalle"]] == [
                ["A001 producto", "2", "10.00", "20.00"], ["A002 servicio", "1", "3.00", "3.00"]]
    assert result["table_unparsed"]["detalle"] == []
    assert source == before
    assert configured["tables"][0]["visual"]["stop_label_aliases"] == ["Resumen de pago"]
    # The historical single stop remains effective when aliases are absent.
    table["visual"].pop("stop_label_aliases")
    legacy = extract(source[:3], definition({"fields": [{"name": "opcional", "required": False}], "tables": [table]}))
    assert len(legacy["tables"]["detalle"]) == 1
    assert legacy["table_unparsed"]["detalle"] == []
    table["visual"].update(stop_label="", stop_label_aliases=["Documento de muestra", "Resumen de pago"])
    alias_only = extract(source, definition({"fields": [{"name": "opcional", "required": False}], "tables": [table]}))
    assert alias_only["tables"] == result["tables"]
    assert alias_only["table_unparsed"] == result["table_unparsed"]


@pytest.mark.parametrize("aliases", [
    [""], ["   "], ["Resumen", " resumen "], ["Subtotal"], ["x" * 161],
    [f"Fin {index}" for index in range(21)],
])
def test_table_stop_aliases_reject_empty_duplicate_oversize_and_overflow(aliases):
    from trackvance.document_template_builder_schemas import VisualTableRule
    with pytest.raises(ValidationError):
        VisualTableRule.model_validate({"page": 1, "region": [.05, .2, .99, .4],
            "header_labels": ["Descripción", "Total"], "column_bounds": [0, .7, 1],
            "stop_label": "Subtotal", "stop_label_aliases": aliases})


def line(values, y=.1, page=1):
    words = [{"text": text, "region": [x, y, min(.99, x + .09), y + .015], "confidence": 96}
             for x, text in values]
    return {"text": " ".join(text for _, text in values), "page": page, "words": words,
            "region": box(words), "confidence": 96, "method": "OCR"}


def ref(identity, lines):
    return SimpleNamespace(id=identity, payload={"lines": lines})


def definition(raw):
    return DocumentDefinition.model_validate(raw).model_dump(mode="json")


def test_discovery_uses_all_references_optional_fields_aliases_and_page_variations():
    references = [ref("first", [line([(.1, "Invoice:"), (.3, "000001")]), line([(.1, "Total:"), (.3, "82.00")], .2)]),
                  ref("second", [line([(.1, "Factura:"), (.3, "000002")], page=2), line([(.1, "Moneda:"), (.3, "COP")], .2, page=2),
                                 line([(.1, "Total:"), (.3, "75.50")], .3, page=2)])]
    result = discover(references)
    fields = {field["name"]: field for field in result["definition"]["fields"]}
    assert fields["moneda"]["required"] is False
    assert fields["numero_factura"]["visual_alternatives"]
    assert set(result["results"]) == {"first", "second"}
    assert result["results"]["first"]["fields"]["numero_factura"]["value"] == "000001"
    assert result["results"]["second"]["fields"]["numero_factura"]["value"] == "000002"
    assert result["results"]["first"]["fields"]["moneda"]["recognition_status"] == "NOT_APPLICABLE"
    assert "fecha_emision" in result["expected_not_located"]


def test_repeated_party_identifiers_follow_sections_and_never_silently_take_first_nit():
    lines = [line([(.1, "Emisor:")]), line([(.1, "NIT:"), (.3, "000123-4")], .14),
             line([(.1, "Adquiriente:")], .22), line([(.1, "NIT:"), (.3, "900456-7")], .26)]
    result = discover([ref("a", lines)])
    payload = result["results"]["a"]
    assert payload["fields"]["nit_emisor"]["value"] == "000123-4"
    assert payload["fields"]["nit_adquiriente"]["value"] == "900456-7"
    assert payload["fields"]["nit_emisor"]["validity"] == "VALID"
    ambiguous = discover([ref("a", [lines[1], lines[3]])])["results"]["a"]
    assert ambiguous["fields"]["nit_emisor"]["validity"] == "AMBIGUOUS"
    assert ambiguous["fields"]["nit_adquiriente"]["alternatives"] == ["000123-4", "900456-7"]


def test_exact_regional_decimals_ambiguous_grouping_dates_and_identifiers():
    money = {"logical_type": "DECIMAL", "decimal_separator": ",", "thousands_separator": ".", "currency_symbols": ["$"]}
    assert validate_value("$ 4.420.850,00", money) == ("4420850.00", "VALID")
    assert validate_value("$ 0,00", money) == ("0.00", "VALID")
    assert validate_value("- $ 1.234,50", money) == ("-1234.50", "VALID")
    assert validate_value("$ 1.2 34,50", {**money, "numeric_fragments": True}) == ("1234.50", "VALID")
    assert validate_value("$ 44.20.850,00", money)[1] == "INVALID"
    assert validate_value("1.234", {"logical_type": "DECIMAL", "decimal_format_explicit": False}) == ("1.234", "AMBIGUOUS")
    assert validate_value("1.234", {"logical_type": "DECIMAL", "decimal_separator": ".", "decimal_format_explicit": True}) == ("1.234", "VALID")
    assert validate_value("000123-4", {"logical_type": "STRING"}) == ("000123-4", "VALID")
    assert validate_value("31/02/2026", {"logical_type": "DATE", "date_format": "%d/%m/%Y"})[1] == "INVALID"
    assert validate_value("01/02/2026", {"logical_type": "DATE", "date_format": "%d/%m/%Y"}) == ("2026-02-01", "VALID")


def test_discovery_handles_side_by_side_parties_and_values_below_labels():
    lines = [line([(.1, "Fecha"), (.21, "de"), (.26, "emisión"), (.65, "Moneda")], .05),
             line([(.1, "2026-10-18"), (.65, "COP")], .085),
             line([(.1, "Emisor"), (.6, "Adquiriente")], .2),
             line([(.1, "Alpha"), (.6, "Beta")], .24),
             line([(.1, "NIT:"), (.25, "000123-4"), (.6, "NIT:"), (.75, "900456-7")], .28)]
    result = discover([ref("synthetic", lines)])["results"]["synthetic"]["fields"]
    assert result["fecha_emision"]["value"] == "2026-10-18"
    assert result["moneda"]["value"] == "COP"
    assert result["emisor"]["value"] == "Alpha"
    assert result["adquiriente"]["value"] == "Beta"
    assert result["nit_emisor"]["value"] == "000123-4"
    assert result["nit_adquiriente"]["value"] == "900456-7"


def test_flowing_tables_multiword_header_multiline_longer_pages_and_unparsed_rows():
    header = line([(.05, "Código"), (.22, "Descripción"), (.50, "Cantidad"), (.65, "Precio"), (.73, "unitario"), (.86, "Total")], .3)
    lines = [header]
    expected = []
    for index in range(8):
        value = f"A{index}"
        expected.append(value)
        lines.append(line([(.05, value), (.22, "Producto"), (.5, "2"), (.68, "10.00"), (.86, "20.00")], .34 + index * .055))
    lines.insert(2, line([(.22, "multilínea")], .365))
    # Page two continues without a repeated header; page three repeats it.
    lines.append(line([(.05, "B0"), (.22, "Continuación"), (.5, "1"), (.68, "0.00"), (.86, "0.00")], .08, 2))
    repeated = copy.deepcopy(header)
    repeated["page"] = 3
    lines.extend([repeated, line([(.05, "C0"), (.22, "Final"), (.5, "1"), (.68, "5.00"), (.86, "5.00")], .35, 3),
                  line([(.05, "Subtotal:"), (.86, "165.00")], .6, 3)])
    table = {"name": "detalle", "fields": [{"name": name, "logical_type": "STRING" if i < 2 else "DECIMAL"} for i, name in enumerate(["codigo", "descripcion", "cantidad", "precio", "total"])],
             "visual": {"page": 1, "region": [.05, .3, .99, .38], "header_labels": ["Código", "Descripción", "Cantidad", "Precio unitario", "Total"],
                        "column_bounds": [0, .18, .45, .62, .83, 1], "stop_label": "Subtotal", "flowing": True, "continue_pages": True, "multiline_column": 1}}
    configured = definition({"fields": [{"name": "opcional", "required": False}], "tables": [table]})
    payload = extract(lines, configured)
    assert [row["codigo"]["value"] for row in payload["tables"]["detalle"]] == [*expected, "B0", "C0"]
    assert payload["tables"]["detalle"][0]["descripcion"]["value"] == "Producto multilínea"
    assert payload["tables"]["detalle"][8]["precio"]["value"] == "0.00"
    assert payload["tables"]["detalle"][8]["codigo"]["origin"]["page"] == 2
    assert payload["table_unparsed"]["detalle"] == []
    broken = copy.deepcopy(lines)
    broken[4]["words"].pop()
    result = extract(broken, configured)
    assert len(result["tables"]["detalle"]) == 9
    assert len(result["table_unparsed"]["detalle"]) == 1


def test_multiline_cells_keep_reading_order_inside_grouped_row_baselines():
    """A row tolerance can join lines, but must not interleave their words by x."""
    lines = [line([(.05, "Descripción"), (.5, "Cantidad"), (.7, "Precio"), (.86, "Total")], .2),
             line([(.05, "SYN001")], .25),
             line([(.5, "2"), (.7, "11.25"), (.86, "22.50")], .26),
             line([(.05, "Material"), (.16, "ficticio")], .267),
             line([(.05, "con"), (.16, "empaque")], .282),
             line([(.05, "extendido"), (.16, "especial")], .297),
             line([(.05, "SYN002"), (.5, "0"), (.7, "3.25"), (.86, "0.00")], .35),
             line([(.05, "Segundo"), (.16, "producto")], .367),
             line([(.05, "Subtotal"), (.86, "22.50")], .45)]
    configured = definition({"fields": [{"name": "opcional", "required": False}], "tables": [{
        "name": "detalle", "fields": [{"name": "descripcion"},
            *[{"name": name, "logical_type": "DECIMAL"} for name in ("cantidad", "precio", "total")]],
        "visual": {"page": 1, "region": [.05, .2, .99, .24],
            "header_labels": ["Descripción", "Cantidad", "Precio", "Total"],
            "column_bounds": [0, .45, .65, .83, 1], "stop_label": "Subtotal",
            "flowing": True, "continue_pages": True, "multiline_column": 0, "row_tolerance": .027}}]})
    before = copy.deepcopy(lines)
    result = extract(lines, configured)
    rows = result["tables"]["detalle"]
    assert [[row[key]["value"] for key in ("descripcion", "cantidad", "precio", "total")] for row in rows] == [
        ["SYN001 Material ficticio con empaque extendido especial", "2", "11.25", "22.50"],
        ["SYN002 Segundo producto", "0", "3.25", "0.00"]]
    assert result["table_unparsed"]["detalle"] == []
    assert rows[0]["descripcion"]["origin"]["region"] == box([word for source in lines[1:6] for word in source["words"] if word["region"][0] < .45])
    assert lines == before


def test_compact_multiline_rows_keep_boundaries_and_incomplete_rows_visible():
    header = line([(.05, "Descripción"), (.5, "Cantidad"), (.7, "Precio"), (.86, "Total")], .2)
    configured = definition({"fields": [{"name": "opcional", "required": False}], "tables": [{
        "name": "detalle", "fields": [{"name": name} for name in ("descripcion", "cantidad", "precio", "total")],
        "visual": {"page": 1, "region": [.05, .2, .99, .24],
            "header_labels": ["Descripción", "Cantidad", "Precio", "Total"],
            "column_bounds": [0, .45, .65, .83, 1], "stop_label": "Subtotal",
            "flowing": True, "continue_pages": True, "multiline_column": 0, "row_tolerance": .027}}]})
    body = [line([(.05, "SYN001")], .25),
            line([(.5, "dos"), (.7, "once"), (.86, "veintidos")], .26),
            line([(.05, "Material"), (.18, "ficticio")], .267),
            line([(.05, "final")], .282),
            line([(.05, "SYN002")], .297),
            line([(.5, "cero"), (.7, "tres"), (.86, "nada")], .307),
            line([(.05, "Otro"), (.18, "producto")], .314)]
    for incomplete in (False, True):
        source = copy.deepcopy(body)
        # Realistic baseline skew, independent of scalar type and cell contents.
        for index, word in enumerate(source[1]["words"]):
            word["region"][1] += index * .0005
            word["region"][3] += index * .0005
        if incomplete:
            source[1]["words"].pop(0)
        before = copy.deepcopy(source)
        result = extract([header, *source, line([(.05, "Subtotal")], .4)], configured)
        rows = result["tables"]["detalle"]
        values = [[row[key]["value"] for key in ("descripcion", "cantidad", "precio", "total")] for row in rows]
        assert values == ([["SYN002 Otro producto", "cero", "tres", "nada"]] if incomplete else [
            ["SYN001 Material ficticio final", "dos", "once", "veintidos"],
            ["SYN002 Otro producto", "cero", "tres", "nada"]])
        assert len(result["table_unparsed"]["detalle"]) == int(incomplete)
        observed = Counter(token for row in rows for cell in row.values() for token in cell["value"].split())
        observed.update(token for region in result["table_unparsed"]["detalle"] for token in region["text"].split())
        assert observed == Counter(token for item in source for word in item["words"] for token in word["text"].split())
        if not incomplete:
            assert rows[0]["descripcion"]["origin"]["region"] == box([word for item in source[:4] for word in item["words"] if word["region"][0] < .45])
        assert source == before
    # Disjoint fragments of two close, incomplete rows cannot fabricate a row.
    ambiguous = [line([(.05, "FIRST")], .25), line([(.5, "left")], .26),
                 line([(.05, "SECOND")], .275), line([(.7, "middle"), (.86, "right")], .285)]
    result = extract([header, *ambiguous, line([(.05, "Subtotal")], .4)], configured)
    assert result["tables"]["detalle"] == []
    assert result["table_unparsed"]["detalle"]
    assert Counter(token for region in result["table_unparsed"]["detalle"] for token in region["text"].split()) == Counter(
        token for item in ambiguous for word in item["words"] for token in word["text"].split())
    # OCR baselines can overlap so that two rows compete for one later text
    # baseline. Reject the segment rather than bisecting unordered boundaries.
    overlapping = [line([(.5, "one")], .35), line([(.7, "two"), (.86, "three")], .3559),
                   line([(.5, "four"), (.7, "five"), (.86, "six")], .3561), line([(.05, "shared")], .3578)]
    for item in overlapping:
        for word in item["words"]:
            word["region"][3] = word["region"][1] + .01
    before = copy.deepcopy(overlapping)
    result = extract([header, *overlapping, line([(.05, "Subtotal")], .4)], configured)
    assert result["tables"]["detalle"] == []
    assert all(region["reason"] == "AMBIGUOUS_ROW_BOUNDARY" for region in result["table_unparsed"]["detalle"])
    assert Counter(token for region in result["table_unparsed"]["detalle"] for token in region["text"].split()) == Counter(
        token for item in overlapping for word in item["words"] for token in word["text"].split())
    assert overlapping == before


def test_later_multiline_fragment_cannot_complete_an_earlier_row():
    configured = definition({"fields": [{"name": "opcional", "required": False}], "tables": [{
        "name": "detalle", "fields": [{"name": name} for name in ("descripcion", "cantidad", "precio", "total")],
        "visual": {"page": 1, "region": [.05, .2, .99, .24],
            "header_labels": ["Descripción", "Cantidad", "Precio", "Total"],
            "column_bounds": [0, .45, .65, .83, 1], "stop_label": "Subtotal",
            "flowing": True, "continue_pages": True, "multiline_column": 0, "row_tolerance": .027}}]})
    header = line([(.05, "Descripción"), (.5, "Cantidad"), (.7, "Precio"), (.86, "Total")], .2)
    source = [line([(.5, "R1Q")], .246), line([(.7, "R1P"), (.86, "R1T")], .2505),
              line([(.5, "R2Q"), (.7, "R2P"), (.86, "R2T")], .251), line([(.05, "D2")], .2509)]
    for item in source:
        for word in item["words"]:
            word["region"][3] = word["region"][1] + .008
    before = copy.deepcopy(source)
    result = extract([header, *source, line([(.05, "Subtotal")], .4)], configured)
    assert result["tables"]["detalle"] == []
    assert all(region["reason"] == "AMBIGUOUS_ROW_BOUNDARY" for region in result["table_unparsed"]["detalle"])
    assert Counter(token for region in result["table_unparsed"]["detalle"] for token in region["text"].split()) == Counter(
        token for item in source for word in item["words"] for token in word["text"].split())
    assert source == before


def test_arithmetic_uses_decimal_rounding_and_never_rewrites_discrepancies():
    configured = definition({"fields": [{"name": name, "label": name, "logical_type": "DECIMAL"} for name in ["cantidad", "precio", "total"]],
        "arithmetic": [{"name": "cantidad por precio", "operation": "PRODUCT", "terms": ["cantidad", "precio"], "result": "total", "scale": 2, "tolerance": "0.00"}]})
    payload = extract([line([(.1, "cantidad:"), (.4, "3")]), line([(.1, "precio:"), (.4, "1.005")], .2), line([(.1, "total:"), (.4, "3.01")], .3)], configured)
    assert payload["arithmetic"][0]["expected"] == "3.02"
    assert payload["arithmetic"][0]["status"] == "DISCREPANCY"
    assert payload["fields"]["total"]["value"] == "3.01"
    assert payload["fields"]["total"]["observed"] == "3.01"


@pytest.mark.parametrize(("operation", "values", "subtract", "expected"), [
    ("PRODUCT", {"a": "10000000000000000000000000.000000000001", "b": "1"}, [],
     "10000000000000000000000000.000000000001"),
    ("SUM", {"a": "10000000000000000000000000.000000000001", "b": "1"}, [],
     "10000000000000000000000001.000000000001"),
    ("SUM", {"a": "10000000000000000000000000.000000000001", "b": "1",
             "deduction": "10000000000000000000000000.000000000001"}, ["deduction"], "1.000000000000"),
])
def test_arithmetic_preserves_38_digit_values_through_sum_product_and_cancellation(operation, values, subtract, expected):
    configured = definition({"fields": [{"name": name, "label": "VALUE_" + name.upper(), "logical_type": "DECIMAL"}
        for name in [*values, "total"]], "arithmetic": [{"name": "exactitud", "operation": operation,
            "terms": ["a", "b"], "subtract": subtract, "result": "total", "scale": 12, "tolerance": "0.00"}]})
    observed = {**values, "total": expected}
    source = [line([(.1, "VALUE_" + name.upper() + ":"), (.4, value)], .05 + .07 * index) for index, (name, value) in enumerate(observed.items())]
    payload = extract(source, configured)
    check = payload["arithmetic"][0]
    assert all(cell["validity"] == "VALID" for cell in payload["fields"].values())
    assert check["status"] == "VALID"
    assert check["expected"] == expected
    assert payload["fields"]["total"]["value"] == expected
    assert payload["fields"]["a"]["observed"] == values["a"]


def test_arithmetic_preserves_extreme_scale_and_reports_its_work_limit():
    tiny = "0." + "0" * 5000 + "1"
    configured = definition({"fields": [{"name": name, "label": "VALUE_" + name.upper(), "logical_type": "DECIMAL"}
        for name in ("a", "b", "total")], "arithmetic": [{"name": "bounded", "operation": "SUM",
            "terms": ["a", "b"], "result": "total", "scale": 12, "tolerance": "0.00"}]})
    payload = extract([line([(.1, "VALUE_A:"), (.4, tiny)]), line([(.1, "VALUE_B:"), (.4, "1")], .2),
                       line([(.1, "VALUE_TOTAL:"), (.4, "1.000000000000")], .3)], configured)
    assert payload["fields"]["a"]["value"] == tiny
    assert payload["fields"]["a"]["validity"] == "VALID"
    assert payload["arithmetic"][0]["status"] == "NOT_EVALUATED"
    assert payload["arithmetic"][0]["reason"] == "ARITHMETIC_PRECISION_LIMIT"


def create_workspace(client, tmp_path, *, holdout=True):
    from test_document_visual_builder_095 import invoice_pdf

    from trackvance.document_worker import process_once

    family = client.post("/api/v1/documents/families", json={"name": "DOC097 corpus"}).json()["id"]
    ids = []
    for index, role in enumerate(["CONFIGURATION", "HOLDOUT" if holdout else "CONFIGURATION"]):
        path = invoice_pdf(tmp_path / f"invoice-{index}.pdf", shifted=bool(index))
        result = client.post(f"/api/v1/documents/families/{family}/builder/references", files={"files": (path.name, path.read_bytes(), "application/pdf")}, data={"role": role})
        assert result.status_code == 200, result.text
        ids.append(result.json()["items"][0]["id"])
        assert process_once()
    draft = client.post(f"/api/v1/documents/families/{family}/builder/drafts", json={}).json()
    response = client.put(f"/api/v1/documents/builder/drafts/{draft['id']}", json={"expected_version": draft["version"], "reference_ids": ids,
        "definition": {"fields": [{"name": "invoice", "label": "Invoice", "pattern": "([0-9]+)", "logical_type": "STRING"}]}})
    assert response.status_code == 200, response.text
    return family, ids, response.json()


def test_draft_concurrency_recovery_and_no_operational_effects(authenticated, database, tmp_path):
    family, ids, draft = create_workspace(authenticated, tmp_path)
    url = f"/api/v1/documents/builder/drafts/{draft['id']}"
    body = {"expected_version": draft["version"], "definition": draft["definition"], "reference_ids": ids, "name": "Persona A"}
    saved = authenticated.put(url, json=body)
    assert saved.status_code == 200
    conflict = authenticated.put(url, json={**body, "name": "Persona B"})
    assert conflict.status_code == 409 and conflict.json()["error"]["code"] == "DOCUMENT_DRAFT_CONFLICT"
    recovered = authenticated.get(url).json()
    assert recovered["name"] == "Persona A" and recovered["version"] == draft["version"] + 1
    family_detail = authenticated.get(f"/api/v1/documents/families/{family}").json()
    assert family_detail["document_count"] == family_detail["pending_count"] == 0
    assert family_detail["templates"] == [] and family_detail["output_datasets"] == {}
    assert authenticated.get(f"/api/v1/documents/originals?family_id={family}").json()["total"] == 0
    with database() as db:
        assert db.scalar(select(func.count()).select_from(DocumentReference)) == 2
        assert db.scalar(select(func.count()).select_from(DocumentOriginal)) == 0


def test_snapshot_review_separation_staleness_holdout_and_idempotent_activation(authenticated, database, tmp_path):
    family, ids, draft = create_workspace(authenticated, tmp_path)
    url = f"/api/v1/documents/builder/drafts/{draft['id']}"
    result = authenticated.post(url + "/test", json={"expected_version": draft["version"]})
    assert result.status_code == 200, result.text
    tested = result.json()
    assert authenticated.post(url + "/test", json={"expected_version": draft["version"]}).json()["id"] == tested["id"]
    assert tested["automatic_results_hash"] == digest(tested["automatic_results"])
    for reference_id in ids:
        response = authenticated.post(f"/api/v1/documents/builder/tests/{tested['id']}/review", json={"reference_id": reference_id,
            "expected_version": tested["review_version"], "changes": {"headers.0.invoice": "000099"} if reference_id == ids[0] else {},
            "confirmed": ["headers.0.invoice"], "reason": "Correction checked independently"})
        assert response.status_code == 200, response.text
        tested = response.json()
    assert tested["automatic_results"][ids[0]]["fields"]["invoice"]["value"] == "000001"
    corrected = tested["reviewed_results"][ids[0]]["fields"]["invoice"]
    assert corrected["value"] == "000099" and corrected["origin"] is None and corrected["confidence"] is None
    assert tested["automatic_metrics"][ids[0]]["corrected"] == 0
    assert tested["reviewed_metrics"][ids[0]]["corrected"] == 1
    activate = f"/api/v1/documents/builder/tests/{tested['id']}/activate"
    activated = authenticated.post(activate)
    assert activated.status_code == 200, activated.text
    assert authenticated.post(activate).json()["id"] == activated.json()["id"]
    assert authenticated.post(f"/api/v1/documents/builder/tests/{tested['id']}/review", json={"reference_id": ids[0], "expected_version": tested["review_version"], "reason": "Too late"}).status_code == 409
    with database() as db:
        assert db.scalar(select(func.count()).select_from(DocumentTemplate)) == 1
        assert db.scalar(select(func.count()).select_from(DocumentBuilderTest)) == 1
    assert authenticated.put(url, json={"expected_version": draft["version"], "definition": draft["definition"], "reference_ids": ids}).status_code == 200
    assert authenticated.post(activate).json()["error"]["code"] == "DOCUMENT_TEST_STALE"
    assert authenticated.get(f"/api/v1/documents/families/{family}").json()["active_template_id"] == activated.json()["id"]


def test_activation_requires_unused_variant_and_detects_reference_changes(authenticated, database, tmp_path):
    _, ids, draft = create_workspace(authenticated, tmp_path, holdout=False)
    test_url = f"/api/v1/documents/builder/drafts/{draft['id']}/test"
    tested = authenticated.post(test_url, json={"expected_version": draft["version"]}).json()
    activate = f"/api/v1/documents/builder/tests/{tested['id']}/activate"
    assert authenticated.post(activate).json()["error"]["code"] == "DOCUMENT_HOLDOUT_REQUIRED"
    assert authenticated.patch(f"/api/v1/documents/builder/references/{ids[1]}", json={"role": "HOLDOUT", "association": "confirmed", "reason": "Confirmed separate test use"}).status_code == 200
    assert authenticated.post(activate).json()["error"]["code"] == "DOCUMENT_TEST_STALE"
    with database() as db:
        assert db.scalar(select(func.count()).select_from(DocumentTemplate)) == 0
        assert db.get(DocumentDraft, draft["id"]).version == draft["version"]


def test_exact_builder_activation_allows_native_manual_batch_without_legacy_example(authenticated, database, tmp_path):
    """The draft's independent references must authorize the operational publication flow."""
    from test_document_visual_builder_095 import invoice_pdf

    from trackvance.document_worker import process_once

    family, ids, draft = create_workspace(authenticated, tmp_path)
    configured = copy.deepcopy(draft["definition"])
    configured["publication"].update(mode="SCHEDULED", starts_at=(datetime.now(UTC) + timedelta(hours=4)).isoformat())
    url = f"/api/v1/documents/builder/drafts/{draft['id']}"
    saved = authenticated.put(url, json={"expected_version": draft["version"], "definition": configured, "reference_ids": ids})
    assert saved.status_code == 200, saved.text
    tested = authenticated.post(url + "/test", json={"expected_version": saved.json()["version"]}).json()
    for identity in ids:
        response = authenticated.post(f"/api/v1/documents/builder/tests/{tested['id']}/review", json={
            "reference_id": identity, "expected_version": tested["review_version"], "confirmed": ["headers.0.invoice"],
            "reason": "All existing independent reference values verified without edits"})
        assert response.status_code == 200, response.text
        tested = response.json()
    activated = authenticated.post(f"/api/v1/documents/builder/tests/{tested['id']}/activate")
    assert activated.status_code == 200, activated.text
    template = activated.json()
    assert template["tested_extraction_id"] is None  # References are not legacy operational examples.
    source = invoice_pdf(tmp_path / "operational-builder.pdf")
    uploaded = authenticated.post(f"/api/v1/documents/families/{family}/originals", data={"template_id": template["id"]},
        files={"files": (source.name, source.read_bytes(), "application/pdf")})
    assert uploaded.status_code == 200, uploaded.text
    original_id = uploaded.json()["items"][0]["id"]
    assert process_once()
    original = authenticated.get(f"/api/v1/documents/originals/{original_id}").json()
    assert original["extraction"]["payload"]["fields"]["invoice"]["value"] == "000001"
    reviewed = authenticated.post(f"/api/v1/documents/originals/{original_id}/review", json={
        "expected_extraction_id": original["extraction"]["id"], "confirmed": ["headers.0.invoice"],
        "reason": "Operational value checked against independent invoice"})
    assert reviewed.status_code == 200, reviewed.text
    body = {"template_id": template["id"], "document_ids": [original_id], "idempotency_key": "native-builder-published-once"}
    publication = authenticated.post(f"/api/v1/documents/families/{family}/batches", json=body)
    assert publication.status_code == 200, publication.text
    batch = publication.json()
    assert batch["status"] == "PUBLISHED" and set(batch["published_versions"]) == {"headers"}
    version_id = batch["published_versions"]["headers"]
    profile = authenticated.get(f"/api/v1/dataset-versions/{version_id}/profile").json()
    assert profile["row_count"] == profile["sampled_rows"] == 1
    assert profile["sample"][0]["invoice"] == "000001"
    assert profile["sample"][0]["document_id"] == original_id
    repeated = authenticated.post(f"/api/v1/documents/families/{family}/batches", json=body)
    assert repeated.status_code == 200 and repeated.json()["id"] == batch["id"]
    assert repeated.json()["published_versions"] == batch["published_versions"]
    untested = authenticated.post(f"/api/v1/documents/families/{family}/templates", json=configured).json()
    rejected = authenticated.post(f"/api/v1/documents/families/{family}/batches", json={**body, "template_id": untested["id"], "idempotency_key": "untested-must-remain-blocked"})
    assert rejected.status_code == 422 and rejected.json()["error"]["code"] == "DOCUMENT_TEMPLATE_TEST_REQUIRED"
    other = authenticated.post("/api/v1/documents/families", json={"name": "Other publication family"}).json()["id"]
    rejected = authenticated.post(f"/api/v1/documents/families/{other}/batches", json=body)
    assert rejected.status_code == 422 and rejected.json()["error"]["code"] == "DOCUMENT_TEMPLATE_TEST_REQUIRED"
    with database() as db:
        evidence = db.get(DocumentBuilderTest, tested["id"])
        evidence.automatic_results = {}
        db.commit()
    rejected = authenticated.post(f"/api/v1/documents/families/{family}/batches", json={**body, "idempotency_key": "changed-proof-must-remain-blocked"})
    assert rejected.status_code == 409 and rejected.json()["error"]["code"] == "DOCUMENT_TEST_INTEGRITY"


def test_reference_and_snapshot_integrity_fail_closed(authenticated, database, tmp_path):
    _, ids, draft = create_workspace(authenticated, tmp_path)
    tested = authenticated.post(f"/api/v1/documents/builder/drafts/{draft['id']}/test", json={"expected_version": draft["version"]}).json()
    with database() as db:
        reference = db.get(DocumentReference, ids[0])
        reference.payload = {**reference.payload, "lines": []}
        db.commit()
    assert authenticated.get(f"/api/v1/documents/builder/references/{ids[0]}").json()["error"]["code"] == "DOCUMENT_REFERENCE_INTEGRITY"
    with database() as db:
        test = db.get(DocumentBuilderTest, tested["id"])
        test.automatic_results = {}
        db.commit()
    response = authenticated.get(f"/api/v1/documents/builder/drafts/{draft['id']}")
    assert response.status_code == 409 and response.json()["error"]["code"] == "DOCUMENT_TEST_INTEGRITY"


def test_reserved_variant_cannot_configure_or_replace_a_consumed_reference(authenticated, tmp_path):
    _, ids, _ = create_workspace(authenticated, tmp_path)
    for identity, expected_status in [(ids[1], 422), (ids[0], 200)]:
        reference = authenticated.get(f"/api/v1/documents/builder/references/{identity}").json()
        word = next(word for source in reference["payload"]["lines"] for word in source["words"] if word["text"] in {"000001", "000002"})
        response = authenticated.post(f"/api/v1/documents/builder/references/{identity}/propose",
            json={"selection": {"page": 1, "region": word["region"]}, "kind": "FIELD"})
        assert response.status_code == expected_status, response.text
    response = authenticated.patch(f"/api/v1/documents/builder/references/{ids[0]}",
        json={"role": "HOLDOUT", "reason": "Attempt to relabel a consumed original"})
    assert response.status_code == 422 and response.json()["error"]["code"] == "DOCUMENT_HOLDOUT_CONFIGURATION"
    current = authenticated.get(f"/api/v1/documents/builder/references/{ids[0]}").json()
    assert current["role"] == "CONFIGURATION" and current["configuration_used"]


def test_legacy_builder_test_route_guides_to_draft_without_creating_evidence(authenticated, database):
    from trackvance.models import Job

    family = authenticated.post("/api/v1/documents/families", json={"name": "Legacy API transition"}).json()["id"]
    response = authenticated.post(f"/api/v1/documents/families/{family}/builder/test",
        json={"document_id": "historical-example", "definition": {"fields": [{"name": "invoice", "label": "Invoice"}]}})
    assert response.status_code == 409, response.text
    error = response.json()["error"]
    assert error["code"] == "DOCUMENT_BUILDER_DRAFT_REQUIRED"
    assert error["details"]["action"] == "CREATE_OR_SELECT_DRAFT"
    assert error["details"]["drafts_path"] == f"/api/v1/documents/families/{family}/builder/drafts"
    with database() as db:
        for model in (DocumentTemplate, DocumentDraft, DocumentReference, DocumentOriginal, Job):
            assert db.scalar(select(func.count()).select_from(model)) == 0


def test_reference_preview_rebinds_user_caches_second_page_and_rechecks_authority(authenticated, database, tmp_path, monkeypatch):
    import re

    from test_document_visual_builder_095 import invoice_pdf

    from trackvance import document_worker
    from trackvance.models import ArtifactLink, User

    source = invoice_pdf(tmp_path / "two-pages.pdf")
    objects = [body for _, body in re.findall(rb"(\d+) 0 obj\n(.*?)\nendobj", source.read_bytes(), re.DOTALL)]
    objects[1] = b"<< /Type /Pages /Count 2 /Kids [4 0 R 6 0 R] >>"
    content = b"BT /F1 18 Tf 60 710 Td (Independent synthetic second page) Tj ET"
    objects.extend([b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Resources << /Font << /F1 3 0 R >> >> /Contents 7 0 R >>",
                    f"<< /Length {len(content)} >>\nstream\n".encode() + content + b"\nendstream"])
    output, offsets = bytearray(b"%PDF-1.4\n"), [0]
    for index, body in enumerate(objects, 1):
        offsets.append(len(output))
        output.extend(f"{index} 0 obj\n".encode() + body + b"\nendobj\n")
    xref = len(output)
    output.extend(f"xref\n0 {len(offsets)}\n0000000000 65535 f \n".encode())
    for offset in offsets[1:]:
        output.extend(f"{offset:010d} 00000 n \n".encode())
    output.extend(f"trailer << /Size {len(offsets)} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF".encode())
    family = authenticated.post("/api/v1/documents/families", json={"name": "Reference page authorization"}).json()["id"]
    uploaded = authenticated.post(f"/api/v1/documents/families/{family}/builder/references",
        files={"files": (source.name, bytes(output), "application/pdf")}, data={"role": "HOLDOUT"})
    assert uploaded.status_code == 200
    identity = uploaded.json()["items"][0]["id"]
    assert document_worker.process_once()
    before = authenticated.get(f"/api/v1/documents/builder/references/{identity}").json()
    assert before["payload"]["pages"] == [1, 2]
    rendered = []
    original_render = document_worker.run_engine

    def render_and_revoke(*args, **kwargs):
        result = original_render(*args, **kwargs)
        rendered.append(kwargs["preview_page"])
        if kwargs["preview_page"] == 1:
            with database() as db:
                actor = db.get(User, "test-user")
                actor.active = False
                db.commit()
        return result

    monkeypatch.setattr(document_worker, "run_engine", render_and_revoke)
    url = f"/api/v1/documents/builder/references/{identity}/pages/"
    first = authenticated.get(url + "2")
    assert first.status_code == 200 and first.headers["content-type"] == "image/jpeg"
    assert first.content.startswith(b"\xff\xd8")
    cached = authenticated.get(url + "2")
    assert cached.status_code == 200 and cached.content == first.content and rendered == [2]
    denied = authenticated.get(url + "1")
    assert denied.status_code == 403 and denied.json()["error"]["code"] == "DOCUMENT_FORBIDDEN"
    assert rendered == [2, 1]
    with database() as db:
        reference = db.get(DocumentReference, identity)
        assert reference.payload_hash == before["payload_hash"] and reference.role == "HOLDOUT" and not reference.configuration_used
        relations = list(db.scalars(select(ArtifactLink.relation).where(ArtifactLink.source_type == "DOCUMENT_REFERENCE", ArtifactLink.source_id == identity)))
        assert "DOCUMENT_REFERENCE_PAGE_2" in relations and "DOCUMENT_REFERENCE_PAGE_1" not in relations
        for model in (DocumentTemplate, DocumentDraft, DocumentOriginal):
            assert db.scalar(select(func.count()).select_from(model)) == 0
