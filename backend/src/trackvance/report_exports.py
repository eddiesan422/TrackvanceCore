"""Incremental CSV and OOXML ZIP writers with no temporary files or spool.

CSV is a lossless escaped-text interchange contract: null is \\N, original
backslash prefixes double, and potentially executable spreadsheet text prefixes
one apostrophe (original leading apostrophes double). XLSX writes strings as
inlineStr and exact decimals/large integers as text; no formulas are created.
"""
from __future__ import annotations

import csv
import io
import zipfile
from collections import deque
from xml.sax.saxutils import escape

from .operations_common import OperationError
from .report_config import ReportLimits


def scalar(value):
    return value["value"] if isinstance(value, dict) else value


def preview_row(names, row):
    values = [scalar(value) for value in row]
    # JSON clients commonly parse numeric tokens as IEEE754 doubles. Keep
    # integers outside that exact range textual, with the schema carrying type.
    return dict(zip(names, [str(value) if type(value) is int and abs(value) > 2**53 - 1 else value
                            for value in values], strict=True))


def csv_value(value):
    value = scalar(value)
    if value is None:
        return r"\N"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, str):
        if value.startswith("\\"):
            return "\\" + value
        if value.startswith("'"):
            return "'" + value
        if value.lstrip(" \t\r\n").startswith(("=", "+", "-", "@")):
            return "'" + value
    return str(value)


def csv_stream(columns, batches):
    memory = io.StringIO(newline="")
    writer = csv.writer(memory, lineterminator="\r\n", quoting=csv.QUOTE_ALL)
    writer.writerow([csv_value(c["name"]) for c in columns])
    yield memory.getvalue().encode("utf-8")
    for rows in batches:
        memory.seek(0)
        memory.truncate()
        for row in rows:
            writer.writerow([csv_value(value) for value in row])
        yield memory.getvalue().encode("utf-8")


class ChunkSink(io.RawIOBase):
    """ZIP data descriptors replace seek; only the caller's next chunk is retained."""
    def __init__(self):
        super().__init__()
        self.chunks: deque[bytes] = deque()
        self.position = 0

    def write(self, content):
        self.position += len(content)
        if content:
            self.chunks.append(bytes(content))
        return len(content)

    def tell(self):
        return self.position

    def seek(self, *_):
        raise OSError("Incremental ZIP is unseekable")

    def flush(self):
        pass

    def drain(self):
        while self.chunks:
            yield self.chunks.popleft()


def _letter(index):
    value = ""
    while index:
        index, remainder = divmod(index - 1, 26)
        value = chr(65 + remainder) + value
    return value


def _text(value):
    if any((ord(char) < 32 and char not in "\t\n\r") or 0xD800 <= ord(char) <= 0xDFFF
           or ord(char) in {0xFFFE, 0xFFFF} for char in value):
        raise OperationError(422, "REPORT_XLSX_CHARACTER", "XLSX no admite un control XML presente en los datos; elige CSV.")
    if len(value.encode("utf-16-le")) // 2 > 32767:
        raise OperationError(422, "REPORT_XLSX_CELL_LIMIT", "Una celda excede 32.767 caracteres Excel; elige CSV.")
    return escape(value).replace("\r", "&#13;")


def _xlsx_row(number, values):
    cells = []
    for ordinal, tagged in enumerate(values, 1):
        value = scalar(tagged)
        if value is None:
            continue  # Null is an absent cell; empty string is explicit inlineStr.
        ref = f"{_letter(ordinal)}{number}"
        if type(value) is bool:
            cells.append(f'<c r="{ref}" t="b"><v>{int(value)}</v></c>')
        elif type(value) is int and abs(value) <= 999999999999999:
            cells.append(f'<c r="{ref}" t="n"><v>{value}</v></c>')
        else:
            cells.append(f'<c r="{ref}" t="inlineStr"><is><t xml:space="preserve">{_text(str(value))}</t></is></c>')
    return (f'<row r="{number}">' + "".join(cells) + "</row>").encode()


def xlsx_stream(columns, batches):
    sink = ChunkSink()
    limits = ReportLimits.configured("XLSX")
    namespace = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
    files = {
        "[Content_Types].xml": '<?xml version="1.0" encoding="UTF-8"?><Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"><Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/><Default Extension="xml" ContentType="application/xml"/><Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/><Override PartName="/xl/worksheets/sheet1.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/></Types>',
        "_rels/.rels": '<?xml version="1.0"?><Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/></Relationships>',
        "xl/workbook.xml": f'<?xml version="1.0"?><workbook xmlns="{namespace}" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"><sheets><sheet name="Reporte" sheetId="1" r:id="rId1"/></sheets></workbook>',
        "xl/_rels/workbook.xml.rels": '<?xml version="1.0"?><Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet1.xml"/></Relationships>',
    }
    with zipfile.ZipFile(sink, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as package:
        for name, content in files.items():
            package.writestr(name, content.encode())
            yield from sink.drain()
        # Excel rejects a streamed ZIP64 member whose local sizes are unknown,
        # even though independent ZIP readers accept it. The effective expanded
        # budget is below Python's ZIP32 boundary, so no member needs ZIP64.
        with package.open("xl/worksheets/sheet1.xml", "w", force_zip64=False) as sheet:
            expanded_bytes = 0
            def write(content):
                nonlocal expanded_bytes
                expanded_bytes += len(content)
                if expanded_bytes > limits.expanded_max_bytes:
                    raise OperationError(422, "REPORT_XLSX_EXPANDED_BYTES", "El XML expandido excede el presupuesto autorizado.")
                sheet.write(content)
            write(f'<?xml version="1.0" encoding="UTF-8"?><worksheet xmlns="{namespace}"><sheetData>'.encode())
            write(_xlsx_row(1, [c["name"] for c in columns]))
            row_number = 1
            for rows in batches:
                for row in rows:
                    row_number += 1
                    if row_number > min(1048576, limits.max_rows + 1):
                        raise OperationError(422, "REPORT_XLSX_ROWS", "XLSX excede su límite de filas; elige CSV.")
                    write(_xlsx_row(row_number, row))
                yield from sink.drain()
            write(b"</sheetData></worksheet>")
        yield from sink.drain()
    yield from sink.drain()
