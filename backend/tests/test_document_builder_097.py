"""DOC-097: independent references, exact snapshots and independent value oracles."""
import copy
from collections import Counter
from types import SimpleNamespace

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
