"""Visual proposals, exact synthetic invoice oracle and unchanged legacy rules."""
import copy
import json
import subprocess

import pytest
from pydantic import ValidationError

from trackvance.document_engine import extract
from trackvance.document_schemas import DocumentDefinition
from trackvance.document_template_builder import propose
from trackvance.document_template_builder_rules import box, visual_lines
from trackvance.document_template_builder_schemas import BuilderProposal, VisualRegion
from trackvance.document_worker import run_engine
from trackvance.operations_common import OperationError


def line(texts, y=.1, x=.1):
    tokens = [{"text": text, "region": [x + index * .16, y, x + index * .16 + .1, y + .02], "confidence": 98}
              for index, text in enumerate(texts)]
    return {"text": " ".join(texts), "page": 1, "region": box(tokens), "confidence": 98, "method": "OCR", "words": tokens}


def selection(region, kind="FIELD"):
    return BuilderProposal.model_validate({"kind": kind, "selection": {"page": 1, "region": region}})


@pytest.mark.parametrize("region", [[0, 0, 0, 1], [-.1, 0, 1, 1], [0, 0, float("nan"), 1], [0, 1, 1, 0]])
def test_visual_regions_reject_invalid_or_unbounded_geometry(region):
    with pytest.raises(ValidationError):
        VisualRegion(region=region)


def test_visual_field_uses_text_anchor_across_shift_and_retains_ambiguity():
    lines = [line(["Factura:", "000001"])]
    proposal = propose(lines, selection(lines[0]["words"][1]["region"]))
    field = proposal["field"]
    assert field["name"] == "factura" and field["logical_type"] == "STRING"
    assert field["visual"]["label"] == "Factura"
    definition = DocumentDefinition.model_validate({"fields": [field]}).model_dump(mode="json")
    shifted = [line(["Factura:", "000002"], y=.17, x=.2)]
    assert extract(shifted, definition)["fields"]["factura"]["value"] == "000002"
    duplicate = shifted + [line(["Factura:", "000003"], y=.25, x=.2)]
    cell = extract(duplicate, definition)["fields"]["factura"]
    assert cell["validity"] == "AMBIGUOUS" and cell["alternatives"] == ["000002", "000003"]
    assert not cell["reviewed"]


def test_visual_table_rejects_missing_cells_and_does_not_read_past_stop():
    lines = [line(["Codigo", "Cantidad", "Importe"], y=.3), line(["A100", "2", "10.25"], y=.35),
             line(["B200", "3", "20.50"], y=.4), line(["Total:", "82.00"], y=.46)]
    proposal = propose(lines, selection([.08, .29, .58, .43], "TABLE"))
    assert proposal["sample_rows"] == 2
    definition = DocumentDefinition.model_validate({"fields": [{"name": "unused", "required": False}],
                                                   "tables": [proposal["table"]]}).model_dump(mode="json")
    payload = extract(lines, definition)
    assert [[cell["value"] for cell in row.values()] for row in payload["tables"]["detalle"]] == [["A100", "2", "10.25"], ["B200", "3", "20.50"]]
    incomplete = copy.deepcopy(lines)
    incomplete[2]["words"].pop()
    payload = extract(incomplete, definition)
    assert len(payload["tables"]["detalle"]) == 1
    assert len(payload["table_unparsed"]["detalle"]) == 1


def test_visual_builder_requires_actual_word_geometry_and_context():
    with pytest.raises(OperationError) as caught:
        propose([{k: v for k, v in line(["Invoice:", "000001"]).items() if k != "words"}], selection([0, 0, 1, 1]))
    assert caught.value.code == "DOCUMENT_BUILDER_GEOMETRY"
    with pytest.raises(OperationError):
        propose([line(["000001"])], selection([0, 0, 1, 1]))


def test_legacy_regex_definition_preserves_values_and_table_behavior():
    raw = {"fields": [{"name": "invoice", "label": "Invoice", "pattern": r"([0-9]+)"}],
           "tables": [{"name": "details", "row_pattern": r"ITEM (?P<sku>\S+) (?P<qty>\d+)",
                       "fields": [{"name": "sku"}, {"name": "qty", "logical_type": "INT64"}]}]}
    definition = DocumentDefinition.model_validate(raw).model_dump(mode="json")
    lines = [line(["Invoice:", "000001"]), line(["ITEM", "A100", "2"], y=.2)]
    before = copy.deepcopy(lines)
    result = extract(lines, definition)
    assert result["fields"]["invoice"]["value"] == "000001"
    assert result["tables"]["details"][0]["sku"]["value"] == "A100"
    assert result["tables"]["details"][0]["qty"]["value"] == "2"
    assert lines == before and raw["tables"][0]["row_pattern"] == r"ITEM (?P<sku>\S+) (?P<qty>\d+)"
    with pytest.raises(ValidationError):
        DocumentDefinition.model_validate({"fields": [{"name": "a"}], "tables": [{"name": "b", "fields": [{"name": "c"}]}]})


def test_adjacent_header_labels_do_not_capture_other_fields_or_values():
    lines = [line(["Invoice:", "000001", "Date:", "2026-10-17"])]
    first = propose(lines, selection(lines[0]["words"][1]["region"]))["field"]
    second = propose(lines, selection(lines[0]["words"][3]["region"]))["field"]
    assert first["visual"]["end_label"] == "Date"
    assert second["visual"]["label"] == "Date"
    definition = DocumentDefinition.model_validate({"fields": [first, second]}).model_dump(mode="json")
    changed = [line(["Invoice:", "000002", "Date:", "2026-10-18"], x=.15)]
    assert {key: cell["value"] for key, cell in extract(changed, definition)["fields"].items()} == {"invoice": "000002", "date": "2026-10-18"}


def invoice_pdf(path, *, shifted=False):
    """Independent PDF fixture: separate text runs exercise real PDF geometry."""
    dx, dy = (15, -23) if shifted else (0, 0)
    contents = []
    values = {"invoice": "000002" if shifted else "000001", "date": "2026-10-18" if shifted else "2026-10-17"}
    def text(x, y, value):
        contents.append(f"BT /F1 18 Tf 1 0 0 1 {x + dx} {y + dy} Tm ({value}) Tj ET")
    text(60, 720, "Invoice: " + values["invoice"])
    text(60, 680, "Date: " + values["date"])
    detail = [["A100", "4", "11.25"], ["B200", "1", "30.50"]] if shifted else [["A100", "2", "10.25"], ["B200", "3", "20.50"]]
    for y, cells in [(600, ["Code", "Qty", "Amount"]), (565, detail[0]), (530, detail[1])]:
        for x, value in zip([60, 240 + (5 if shifted else 0), 360], cells, strict=True):
            text(x, y, value)
    text(60, 470, "Total: " + ("75.50" if shifted else "82.00"))
    stream = "\n".join(contents).encode("ascii")
    objects = [b"<< /Type /Catalog /Pages 2 0 R >>", b"<< /Type /Pages /Count 1 /Kids [4 0 R] >>",
               b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
               b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Resources << /Font << /F1 3 0 R >> >> /Contents 5 0 R >>",
               f"<< /Length {len(stream)} >>\nstream\n".encode() + stream + b"\nendstream"]
    output, offsets = bytearray(b"%PDF-1.4\n"), [0]
    for index, obj in enumerate(objects, 1):
        offsets.append(len(output))
        output.extend(f"{index} 0 obj\n".encode() + obj + b"\nendobj\n")
    xref = len(output)
    output.extend(f"xref\n0 {len(offsets)}\n0000000000 65535 f \n".encode())
    for offset in offsets[1:]:
        output.extend(f"{offset:010d} 00000 n \n".encode())
    output.extend(f"trailer << /Size {len(offsets)} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF".encode())
    path.write_bytes(output)
    return path


@pytest.mark.parametrize("format", ["digital", "ocr"])
def test_real_visual_invoice_and_shifted_second_invoice_exact_oracle(database, tmp_path, format):
    paths = [invoice_pdf(tmp_path / "first.pdf"), invoice_pdf(tmp_path / "second.pdf", shifted=True)]
    if format == "ocr":
        for source in paths:
            subprocess.run(["pdftoppm", "-r", "180", "-singlefile", "-png", str(source), str(source.with_suffix(""))], check=True, capture_output=True)
        paths = [path.with_suffix(".png") for path in paths]
    bootstrap = DocumentDefinition.model_validate({"fields": [{"name": "pending", "label": "__not_configured__"}]}).model_dump(mode="json")
    first = run_engine(paths[0], bootstrap)
    lines = visual_lines(first["payload"]["lines"])
    assert all(line["words"] for line in lines)
    fields = []
    for label, value in [("invoice", "000001"), ("date", "2026-10-17"), ("total", "82.00")]:
        word = next(word for line in lines for word in line["words"] if word["text"] == value)
        field = propose(first["payload"]["lines"], selection(word["region"]))["field"]
        assert field["name"] == label
        fields.append(field)
    table_lines = [line for line in lines if any(word["text"] in {"Code", "A100", "B200"} for word in line["words"])]
    region = box([word for line in table_lines for word in line["words"]])
    region = [max(0, region[0] - .01), max(0, region[1] - .005), min(1, region[2] + .01), min(1, region[3] + .005)]
    table = propose(first["payload"]["lines"], selection(region, "TABLE"))["table"]
    definition = DocumentDefinition.model_validate({"fields": fields, "tables": [table]}).model_dump(mode="json")
    frozen = copy.deepcopy(definition)
    results = [run_engine(path, definition) for path in paths]
    for index, result in enumerate(results):
        payload = result["payload"]
        assert {key: value["value"] for key, value in payload["fields"].items()} == {
            "invoice": f"00000{index + 1}", "date": f"2026-10-{17 + index}", "total": "75.50" if index else "82.00"}
        expected = [["A100", "4", "11.25"], ["B200", "1", "30.50"]] if index else [["A100", "2", "10.25"], ["B200", "3", "20.50"]]
        assert [[row[name]["value"] for name in ("code", "qty", "amount")] for row in payload["tables"]["detalle"]] == expected
        assert not payload["table_unparsed"]["detalle"]
        assert all(cell["validity"] == "VALID" and not cell["reviewed"] for cell in payload["fields"].values())
        assert all(cell["origin"]["method"] == ("OCR" if format == "ocr" else "DIGITAL") for row in payload["tables"]["detalle"] for cell in row.values())
    assert definition == frozen
    print("VISUAL_BUILDER_INVOICE_EVIDENCE " + json.dumps({"format": format, "definition": definition,
          "engine": [result["engine"] for result in results], "documents": 2, "header_values": 6, "detail_values": 12,
          "all_values_exact": True, "shift_pdf_points": [15, -23], "quantity_column_shift_points": 5}))


def test_builder_api_creates_immutable_example_and_test_before_activation(authenticated, database, tmp_path):
    """0.9.7 supersedes revision-per-attempt with the same real local engine."""
    from sqlalchemy import func, select

    from trackvance.document_models import DocumentOriginal, DocumentTemplate
    from trackvance.document_worker import process_once

    source = invoice_pdf(tmp_path / "invoice.pdf")
    holdout = invoice_pdf(tmp_path / "holdout.pdf", shifted=True)
    family = authenticated.post("/api/v1/documents/families", json={"name": "Visual invoice"}).json()["id"]
    references = []
    for path, role in [(source, "CONFIGURATION"), (holdout, "HOLDOUT")]:
        response = authenticated.post(f"/api/v1/documents/families/{family}/builder/references",
            files={"files": (path.name, path.read_bytes(), "application/pdf")}, data={"role": role})
        assert response.status_code == 200, response.text
        reference = response.json()["items"][0]
        assert reference["kind"] == "REFERENCE" and reference["template_id"] is None
        assert process_once()
        references.append(reference["id"])
    with database() as db:
        assert db.scalar(select(func.count()).select_from(DocumentTemplate)) == 0
        assert db.scalar(select(func.count()).select_from(DocumentOriginal)) == 0
    draft = authenticated.post(f"/api/v1/documents/families/{family}/builder/drafts", json={}).json()
    draft = authenticated.put(f"/api/v1/documents/builder/drafts/{draft['id']}", json={
        "expected_version": draft["version"], "definition": draft["definition"], "reference_ids": references}).json()
    response = authenticated.post(f"/api/v1/documents/builder/drafts/{draft['id']}/discover", json={"expected_version": draft["version"]})
    assert response.status_code == 200, response.text
    draft = response.json()["draft"]
    assert draft["discovered_from"] == [references[0]]
    assert any(field["name"] == "numero_factura" for field in draft["definition"]["fields"])
    response = authenticated.post(f"/api/v1/documents/builder/drafts/{draft['id']}/test", json={"expected_version": draft["version"]})
    assert response.status_code == 200, response.text
    tested = response.json()
    assert set(tested["automatic_results"]) == set(references)
    assert tested["automatic_results"][references[0]]["fields"]["numero_factura"]["value"] == "000001"
    assert tested["automatic_results"][references[1]]["fields"]["numero_factura"]["value"] == "000002"
    activate = f"/api/v1/documents/builder/tests/{tested['id']}/activate"
    assert authenticated.post(activate).status_code == 422
    for reference_id in references:
        payload = tested["automatic_results"][reference_id]
        confirmed = [f"headers.0.{name}" for name in payload["fields"]]
        confirmed += [f"{table}.{index}.{name}" for table, rows in payload["tables"].items() for index, row in enumerate(rows) for name in row]
        response = authenticated.post(f"/api/v1/documents/builder/tests/{tested['id']}/review", json={
            "reference_id": reference_id, "expected_version": tested["review_version"],
            "confirmed": confirmed, "reason": "Independent PDF oracle checked"})
        assert response.status_code == 200, response.text
        tested = response.json()
    response = authenticated.post(activate)
    assert response.status_code == 200, response.text
    assert authenticated.post(activate).json()["id"] == response.json()["id"]
    with database() as db:
        assert db.scalar(select(func.count()).select_from(DocumentTemplate)) == 1
        assert db.scalar(select(func.count()).select_from(DocumentOriginal)) == 0


@pytest.mark.parametrize("missing", ["documents:configure", "documents:write", "documents:download"])
def test_builder_authorizes_geometry_and_creation_independently(authenticated, database, missing):
    from trackvance.models import Role, RolePermission, User

    with database() as db:
        role = Role(name="Builder restricted", normalized_name="builder-restricted")
        db.add(role)
        db.flush()
        for code in {"documents:read", "documents:configure", "documents:write", "documents:download"} - {missing}:
            db.add(RolePermission(role_id=role.id, permission_code=code))
        db.get(User, "test-user").role_id = role.id
        db.commit()
    assert authenticated.get("/api/v1/documents/originals/unknown/builder").status_code == 403
    assert authenticated.post("/api/v1/documents/families/unknown/builder/examples", files={"file": ("fake.pdf", b"%PDF-synthetic", "application/pdf")}).status_code == 403
