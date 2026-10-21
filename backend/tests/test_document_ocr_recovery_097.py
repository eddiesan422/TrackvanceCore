"""Independent synthetic OCR regression: contrasting headers and long detail."""
import copy

from document_ocr_fixture import EXPECTED, definition, image_only_pdf, rasterize, reversed_band_pdf

from trackvance import document_engine as engine


def test_automatic_ocr_recovers_reversed_headers_without_omitting_or_duplicating_rows(tmp_path):
    # Neither these constants nor this generated invoice comes from the private
    # acceptance corpus or the OCR output being evaluated.
    images = rasterize(reversed_band_pdf(tmp_path / "contrast-invoice.pdf"), tmp_path)
    lines = [line for page, path in enumerate(images, 1) for line in engine.ocr(path, page)]
    result = engine.extract(lines, definition())
    actual = [[row[key]["value"] for key in ("description", "quantity", "unit_price", "amount")]
              for row in result["tables"]["detail"]]
    assert actual == [list(row) for row in EXPECTED["rows"]]
    assert result["fields"]["invoice"]["value"] == EXPECTED["invoice"]
    assert result["fields"]["invoice_total"]["value"] == EXPECTED["total"]
    assert result["table_unparsed"]["detail"] == []
    assert all(cell["validity"] == "VALID" and cell["reviewed"] is False
               for row in result["tables"]["detail"] for cell in row.values())


def test_mixed_page_keeps_digital_words_once_when_ocr_groups_the_same_words_differently(tmp_path, monkeypatch):
    pdf = tmp_path / "mixed.pdf"
    pdf.write_bytes(b"Fixture supplied through bounded native-tool responses")
    xml = '<doc><page width="1000" height="1000"><line xMin="100" yMin="300" xMax="290" yMax="320"><word xMin="100" yMin="300" xMax="180" yMax="320">Known</word><word xMin="190" yMin="300" xMax="290" yMax="320">Product</word></line></page></doc>'

    def command(args, timeout=30):
        return {"pdfinfo": "Pages: 1\n", "pdftotext": xml, "pdfimages": "1 0 image\n", "pdftoppm": ""}[args[0].split("/")[-1]]

    words = [{"text": text, "region": region, "confidence": 95} for text, region in
             [("Known", [.101, .3, .18, .32]), ("Product", [.19, .3, .291, .32]),
              ("2", [.51, .3, .52, .32]), ("4.35", [.8, .3, .85, .32])]]
    monkeypatch.setattr(engine, "command", command)
    monkeypatch.setattr(engine, "ocr", lambda path, page: [{"text": "Known Product 2 4.35", "page": page,
        "region": [.101, .3, .85, .32], "words": words, "confidence": 95, "method": "OCR"}])
    lines, count = engine.lines_from_file(pdf, tmp_path, tmp_path / "progress")
    assert count == 1
    assert [word["text"] for line in lines for word in line["words"]] == ["Known", "Product", "2", "4.35"]
    assert [line["method"] for line in lines] == ["DIGITAL", "OCR"]


def test_confined_native_process_extracts_the_complete_two_page_scanned_invoice(database, tmp_path):
    from trackvance.document_worker import run_engine

    source = image_only_pdf(tmp_path / "scan.pdf", tmp_path)
    result = run_engine(source, definition())
    payload = result["payload"]
    assert [[row[key]["value"] for key in ("description", "quantity", "unit_price", "amount")]
            for row in payload["tables"]["detail"]] == [list(row) for row in EXPECTED["rows"]]
    assert {key: cell["value"] for key, cell in payload["fields"].items()} == {
        "invoice": EXPECTED["invoice"], "invoice_total": EXPECTED["total"]}
    assert payload["table_unparsed"]["detail"] == []
    assert result["engine"]["pages"] == 2
    assert payload["page_geometry"] == {
        "1": {"width": 612, "height": 792, "unit": "PDF_POINT"},
        "2": {"width": 612, "height": 792, "unit": "PDF_POINT"}}
    assert result["engine"]["processing_sha256"] == engine.PROCESSOR_HASHES


def test_spatial_merge_keeps_legitimate_repeated_values_and_does_not_rewrite_first_source():
    def line(text, region, page=1, method="OCR"):
        word = {"text": text, "region": region, "confidence": 92}
        return {"text": text, "words": [word], "region": region, "page": page, "method": method, "confidence": 92}

    primary = [line("002", [.1, .2, .2, .22])]
    supplemental = [line("2", [.101, .201, .199, .221]),  # Same physical cell, conflicting recognition.
                    line("002", [.5, .2, .6, .22]), line("002", [.1, .3, .2, .32]),
                    line("002", [.1, .2, .2, .22], page=2)]
    before = copy.deepcopy((primary, supplemental))
    merged = engine.merge_lines(primary, supplemental)
    assert [item["text"] for item in merged] == ["002", "002", "002", "002"]
    assert [item["region"] for item in merged] == [primary[0]["region"], *[item["region"] for item in supplemental[1:]]]
    assert (primary, supplemental) == before
