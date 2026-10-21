"""Generic two-page reversed-band invoice; independent values, no private corpus."""
import subprocess
from decimal import Decimal
from pathlib import Path

EXPECTED = {
    "invoice": "QA-003957",
    "rows": [
        ("SKU-A Service alpha", "2", "4.35", "8.70"),
        ("SKU-B Service beta", "3", "12.20", "36.60"),
        ("SKU-C Service gamma", "1", "0.50", "0.50"),
        ("SKU-D Service delta", "4", "3.25", "13.00"),
        ("SKU-E Service epsilon", "2", "7.90", "15.80"),
        ("SKU-F Service zeta", "5", "1.20", "6.00"),
        ("SKU-G Service eta", "6", "0.25", "1.50"),
    ],
    "total": "82.10",
}
assert all(Decimal(q) * Decimal(price) == Decimal(total) for _, q, price, total in EXPECTED["rows"])
assert sum((Decimal(row[3]) for row in EXPECTED["rows"]), Decimal(0)) == Decimal(EXPECTED["total"])


def reversed_band_pdf(path: Path):
    pages = []
    for page, rows in enumerate([EXPECTED["rows"][:3], EXPECTED["rows"][3:]], 1):
        commands = []

        def rectangle(x, top, width, height, *, output=commands):
            output.append(f"0.08 0.14 0.24 rg {x} {792-top-height} {width} {height} re f")

        def text(x, top, value, size=13, *, white=False, output=commands):
            value = str(value).replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
            output.append(f"{'1 1 1' if white else '0 0 0'} rg BT /F1 {size} Tf 1 0 0 1 {x} {792-top} Tm ({value}) Tj ET")

        if page == 1:
            rectangle(0, 0, 612, 145)
            text(45, 48, "QA SERVICE LAB - SYNTHETIC TEST ONLY", 18, white=True)
            text(45, 100, "Invoice:", 19, white=True)
            text(395, 100, EXPECTED["invoice"], 19, white=True)
            text(45, 205, "Issuer: Example QA Company", 13)
            text(330, 205, "Buyer: Example QA Client", 13)
            text(45, 235, "Date: 2026-10-21", 13)
            text(330, 235, "Currency: USD", 13)
        else:
            text(45, 42, "QA SERVICE LAB - CONTINUED TEST DETAIL", 16)
        top = 320 if page == 1 else 95
        rectangle(40, top-24, 535, 40)
        for x, label in [(45, "Description"), (315, "Qty"), (400, "Unit price"), (510, "Amount")]:
            text(x, top, label, 12, white=True)
        for index, row in enumerate(rows):
            y = top + 65 + index * 70
            for x, value in zip([45, 315, 400, 510], row, strict=True):
                text(x, y, value, 13)
        if page == 2:
            text(45, 600, "Payment summary", 14)
            text(45, 645, "Invoice total:", 16)
            text(505, 645, EXPECTED["total"], 16)
        text(45, 755, f"Footer - synthetic test page{page}", 11)
        pages.append("\n".join(commands).encode("ascii"))
    objects = [b"<< /Type /Catalog /Pages 2 0 R >>", b"<< /Type /Pages /Count 2 /Kids [4 0 R 6 0 R] >>",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>"]
    for index, stream in enumerate(pages):
        objects.extend([f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Resources << /Font << /F1 3 0 R >> >> /Contents {5+index*2} 0 R >>".encode(),
            f"<< /Length {len(stream)} >>\nstream\n".encode()+stream+b"\nendstream"])
    data = bytearray(b"%PDF-1.4\n");offsets = [0]
    for index, value in enumerate(objects, 1):
        offsets.append(len(data));data.extend(f"{index} 0 obj\n".encode()+value+b"\nendobj\n")
    start = len(data);data.extend(f"xref\n0 {len(offsets)}\n0000000000 65535 f \n".encode())
    for offset in offsets[1:]:data.extend(f"{offset:010d} 00000 n \n".encode())
    data.extend(f"trailer << /Size {len(offsets)} /Root 1 0 R >>\nstartxref\n{start}\n%%EOF".encode())
    path.write_bytes(data)
    return path


def rasterize(path: Path, folder: Path):
    prefix = folder / "reversed-band"
    subprocess.run(["/usr/bin/pdftoppm", "-png", "-scale-to", "4000", str(path), str(prefix)], check=True, capture_output=True, timeout=30)
    return [folder / "reversed-band-1.png", folder / "reversed-band-2.png"]


def image_only_pdf(path: Path, folder: Path):
    from test_documents_095 import pdf_file

    source = reversed_band_pdf(folder / "digital-master.pdf")
    subprocess.run(["/usr/bin/pdftoppm", "-scale-to", "4000", str(source), str(folder / "pixels")],
                   check=True, capture_output=True, timeout=30)
    pages = []
    for page in (1, 2):
        with (folder / f"pixels-{page}.ppm").open("rb") as stream:
            assert stream.readline().strip() == b"P6"
            size = stream.readline()
            while size.startswith(b"#"):
                size = stream.readline()
            width, height = map(int, size.split())
            assert stream.readline().strip() == b"255"
            pixels = stream.read()
            assert len(pixels) == width * height * 3
        pages.append(([], (width, height, pixels)))
    return pdf_file(path, pages)


def definition():
    from trackvance.document_schemas import DocumentDefinition
    return DocumentDefinition.model_validate({"fields": [
        {"name": "invoice", "logical_type": "STRING", "visual": {"page": 1, "region": [0, 0, 1, .2], "label": "Invoice", "direction": "RIGHT"}},
        {"name": "invoice_total", "logical_type": "DECIMAL", "visual": {"page": 2, "region": [0, .7, 1, .9], "label": "Invoice total", "direction": "RIGHT"}}],
        "tables": [{"name": "detail", "fields": [{"name": "description"},
            *[{"name": key, "logical_type": "DECIMAL"} for key in ("quantity", "unit_price", "amount")]],
            "visual": {"page": 1, "region": [45/612, 290/792, .99, 340/792], "header_labels": ["Description", "Qty", "Unit price", "Amount"],
                "column_bounds": [0, .49, .63, .8, 1], "flowing": True, "continue_pages": True, "multiline_column": 0,
                "row_tolerance": .027, "stop_label": "Footer", "stop_label_aliases": ["Payment summary"]}}]}).model_dump(mode="json")
