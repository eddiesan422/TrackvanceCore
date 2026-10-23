"""Independent tiny financial PDF; no private acceptance corpus or oracle."""
from trackvance.document_schemas import DocumentDefinition

HEADERS = {"gross": "22.00", "discount": "2.00", "net": "20.00", "exempt": "2.00",
           "vat5": "0.90", "vat19": "0.00", "total": "20.90", "advance": "0.90", "balance": "20.00"}
ROWS = [["AA-00001-01", "2", "10.00", "10%", "5%", "18.00"],
        ["BB-00002-02", "1", "2.00", "0%", "0%", "2.00"]]


def financial_definition():
    names = ["sku", "quantity", "price", "discount_pct", "tax_pct", "line_net"]
    return DocumentDefinition.model_validate({
        "fields": [{"name": "invoice", "label": "INVOICE", "pattern": r"(INV-[0-9]+)",
                    "critical": True, "business_key": True},
                   *[{"name": name, "label": name.upper(), "pattern": r"([0-9]+(?:\.[0-9]+)?)",
                      "logical_type": "DECIMAL"} for name in HEADERS]],
        "tables": [{"name": "items", "fields": [{"name": name,
            "logical_type": "STRING" if name in {"sku", "discount_pct", "tax_pct"} else "DECIMAL",
            "business_key": name == "sku"} for name in names],
            "visual": {"page": 1, "region": [.05, .352, .92, .382],
                "header_labels": ["SKU", "QTY", "PRICE", "DISC", "TAX", "NET"],
                "column_bounds": [.04, .27, .38, .51, .64, .75, .94], "stop_label": "END DETAILS",
                "flowing": True, "multiline_column": 0, "row_tolerance": .015,
                "row_start": {"column": 0, "segments": [
                    {"alphabet": "ASCII_ALPHA", "minimum": 2, "maximum": 4},
                    {"alphabet": "ASCII_DIGIT", "minimum": 5, "maximum": 5},
                    {"alphabet": "ASCII_DIGIT", "minimum": 2, "maximum": 2}]}}}],
        "invoice_controls": {"name": "financial_invoice", "table": "items", "quantity": "quantity",
            "unit_price": "price", "discount_percent": "discount_pct", "tax_percent": "tax_pct", "line_net": "line_net",
            "headers": {"gross_subtotal": "gross", "discount_total": "discount", "net_base": "net", "exempt_base": "exempt",
                "tax_totals": [{"rate": "5", "field": "vat5"}, {"rate": "19", "field": "vat19"}],
                "invoice_total": "total", "advance": "advance", "balance": "balance"},
            "rounding_scope": "ROW", "rounding_mode": "ROUND_HALF_UP", "scale": 2, "tolerance": "0.01"},
        "cross_totals": [{"name": "detail_net_to_header", "table": "items", "column": "line_net", "header": "net"}],
        "publication": {"review_policy": "AUTO_VERIFIED", "stop_after": "PUBLICATION"}}).model_dump(mode="json")


def financial_pdf(path, invoice="INV-000001", mutation=None):
    commands = []

    def text(x, y, value):
        commands.append(f"BT /F1 12 Tf 1 0 0 1 {x} {y} Tm ({value}) Tj ET")

    text(40, 750, "INVOICE: " + invoice)
    for index, (name, value) in enumerate(HEADERS.items()):
        text(40, 730 - index * 20, name.upper() + ": " + value)
    for x, value in zip([40, 180, 240, 320, 400, 470], ["SKU", "QTY", "PRICE", "DISC", "TAX", "NET"], strict=True):
        text(x, 500, value)
    for index, row in enumerate(ROWS):
        if mutation == "missing_row" and index == 1:
            continue
        for column, (x, value) in enumerate(zip([40, 180, 240, 320, 400, 470], row, strict=True)):
            text(x, 475 - index * 25, "3.00" if mutation == "wrong_amount" and index == 1 and column == 5 else value)
    text(40, 425, "END DETAILS")
    stream = "\n".join(commands).encode("ascii")
    objects = [b"<< /Type /Catalog /Pages 2 0 R >>", b"<< /Type /Pages /Count 1 /Kids [4 0 R] >>",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Courier >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Resources << /Font << /F1 3 0 R >> >> /Contents 5 0 R >>",
        f"<< /Length {len(stream)} >>\nstream\n".encode() + stream + b"\nendstream"]
    data, offsets = bytearray(b"%PDF-1.4\n"), [0]
    for index, item in enumerate(objects, 1):
        offsets.append(len(data))
        data.extend(f"{index} 0 obj\n".encode() + item + b"\nendobj\n")
    xref = len(data)
    data.extend(f"xref\n0 {len(offsets)}\n0000000000 65535 f \n".encode())
    for offset in offsets[1:]:
        data.extend(f"{offset:010d} 00000 n \n".encode())
    data.extend(f"trailer << /Size {len(offsets)} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF".encode())
    path.write_bytes(data)
    return path
