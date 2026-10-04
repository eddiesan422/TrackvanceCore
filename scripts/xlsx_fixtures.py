"""Reproducible, diverse OOXML fixtures and an independent whole-file oracle.

Generation and verification use bounded row buffers and a disk-backed shared
string index. Neither imports the Trackvance reader. Never generates user data.
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import json
import sqlite3
import time
import zipfile
from contextlib import ExitStack, closing
from datetime import date, timedelta
from functools import lru_cache
from pathlib import Path
from tempfile import TemporaryDirectory
from xml.etree import ElementTree as ET
from xml.sax.saxutils import escape

COLUMNS = ("record_id", "amount", "region", "observed", "payload", "date", "formula", "late_type")
NS = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
SEED = b"trackvance-c01-c06-xlsx-diverse-v1"
HEADER_ROW = 4


def canonical(row):
    return (json.dumps(row, sort_keys=True, ensure_ascii=False, separators=(",", ":")) + "\n").encode()


def expected_row(number):
    payload = base64.urlsafe_b64encode(hashlib.shake_256(SEED + number.to_bytes(8, "big")).digest(72)).decode()
    observed = None if number % 100 == 0 else "" if number % 100 == 1 else (
        f"  é-ñ-{number:012d}-😀\ncontinuación  " if number % 10000 == 2 else f"  é-ñ-{number:012d}-😀  ")
    return dict(zip(COLUMNS, (f"{number:012d}", f"{number % 997 + 1}.00000001", f"R{number % 20:02d}",
        observed, payload, (date(2023, 1, 1) + timedelta(days=number % 365)).isoformat(),
        f"=A{number + HEADER_ROW}+1", str(number % 997) if number <= 100000 else f"late-text-{number}"), strict=True))


def _text(value):
    return escape(value, {"\r": "&#13;"})


def _database(path):
    db = sqlite3.connect(path)
    db.execute("PRAGMA journal_mode=OFF")
    db.execute("PRAGMA synchronous=OFF")
    db.execute("PRAGMA cache_size=-4096")
    db.execute("CREATE TABLE strings (value TEXT PRIMARY KEY, ordinal INTEGER UNIQUE)")
    return db


def generate(directory, rows, *, strings="inline", wrong_dimension=True):
    if not 1 <= rows <= 1048576 - HEADER_ROW or strings not in {"inline", "shared"}:
        raise ValueError("La población debe caber en Excel incluyendo las filas de encabezado.")
    directory = Path(directory).resolve()
    directory.mkdir(parents=True, exist_ok=True)
    name = f"xlsx-{rows}-{strings}-diverse"
    path, metadata_path = directory / f"{name}.xlsx", directory / f"{name}.json"
    if path.exists() or metadata_path.exists():
        if not (path.exists() and metadata_path.exists()):
            raise ValueError("Fixture incompleto; use una nueva ubicación propia.")
        result = json.loads(metadata_path.read_text(encoding="utf-8"))
        with path.open("rb") as source:
            if hashlib.file_digest(source, "sha256").hexdigest() != result["file_sha256"]:
                raise ValueError("Fixture existente alterado.")
        return result
    started, digest, observed_bytes = time.monotonic(), hashlib.sha256(), 0
    with TemporaryDirectory(prefix="xlsx-fixture-", dir=directory) as work, ExitStack() as stack:
        work = Path(work)
        db = stack.enter_context(closing(_database(work / "strings.sqlite")))
        next_index = 0
        with (work / "shared.xml").open("wb") as sst:
            sst.write(f'<sst xmlns="{NS}">'.encode())

            @lru_cache(maxsize=4096)
            def shared(value):
                nonlocal next_index
                found = db.execute("SELECT ordinal FROM strings WHERE value=?", (value,)).fetchone()
                if found:
                    return found[0]
                index = next_index
                db.execute("INSERT INTO strings VALUES (?,?)", (value, index))
                next_index += 1
                sst.write(f'<si><t xml:space="preserve">{_text(value)}</t></si>'.encode())
                return index

            def cell(reference, value, kind="text"):
                if value is None:
                    return ""
                if kind == "date":
                    serial = (date.fromisoformat(value) - date(1899, 12, 30)).days
                    return f'<c r="{reference}" s="1"><v>{serial}</v></c>'
                if kind == "formula":
                    return f'<c r="{reference}"><f>{_text(value[1:])}</f><v>0</v></c>'
                if kind == "number":
                    return f'<c r="{reference}"><v>{value}</v></c>'
                if strings == "shared":
                    return f'<c r="{reference}" t="s"><v>{shared(value)}</v></c>'
                return f'<c r="{reference}" t="inlineStr"><is><t xml:space="preserve">{_text(value)}</t></is></c>'

            with zipfile.ZipFile(path, "x", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as archive:
                archive.writestr("[Content_Types].xml", '''<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"><Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/><Default Extension="xml" ContentType="application/xml"/><Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/><Override PartName="/xl/worksheets/sheet1.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/><Override PartName="/xl/worksheets/sheet2.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/><Override PartName="/xl/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.styles+xml"/>''' + ('<Override PartName="/xl/sharedStrings.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sharedStrings+xml"/>' if strings == "shared" else "") + "</Types>")
                archive.writestr("_rels/.rels", '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/></Relationships>')
                archive.writestr("xl/workbook.xml", f'<workbook xmlns="{NS}" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"><workbookPr date1904="0"/><sheets><sheet name="Información" sheetId="1" r:id="rId1"/><sheet name="Datos" sheetId="2" r:id="rId2"/></sheets></workbook>')
                archive.writestr("xl/_rels/workbook.xml.rels", '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet1.xml"/><Relationship Id="rId2" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet2.xml"/><Relationship Id="rId3" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles" Target="styles.xml"/>' + ('<Relationship Id="rId4" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/sharedStrings" Target="sharedStrings.xml"/>' if strings == "shared" else "") + '</Relationships>')
                archive.writestr("xl/styles.xml", f'<styleSheet xmlns="{NS}"><fonts count="1"><font><sz val="11"/><name val="Calibri"/></font></fonts><fills count="2"><fill><patternFill patternType="none"/></fill><fill><patternFill patternType="gray125"/></fill></fills><borders count="1"><border/></borders><cellStyleXfs count="1"><xf numFmtId="0" fontId="0" fillId="0" borderId="0"/></cellStyleXfs><cellXfs count="2"><xf numFmtId="0" fontId="0" fillId="0" borderId="0" xfId="0"/><xf numFmtId="14" fontId="0" fillId="0" borderId="0" xfId="0" applyNumberFormat="1"/></cellXfs><cellStyles count="1"><cellStyle name="Normal" xfId="0" builtinId="0"/></cellStyles></styleSheet>')
                archive.writestr("xl/worksheets/sheet1.xml", f'<worksheet xmlns="{NS}"><sheetData><row r="1"><c r="A1" t="inlineStr"><is><t>metadata</t></is></c></row></sheetData></worksheet>')
                with archive.open("xl/worksheets/sheet2.xml", "w", force_zip64=True) as output:
                    dimension = "A1:A1" if wrong_dimension else f"A1:H{rows + HEADER_ROW}"
                    output.write(f'<worksheet xmlns="{NS}"><dimension ref="{dimension}"/><sheetData><row r="2"><c r="A2" s="1"/></row>'.encode())
                    output.write((f'<row r="{HEADER_ROW}">' + ''.join(cell(f'{chr(65+i)}{HEADER_ROW}', column) for i, column in enumerate(COLUMNS)) + '</row>').encode())
                    for number in range(1, rows + 1):
                        row = expected_row(number)
                        digest.update(canonical(row))
                        observed_bytes += sum(len(value.encode()) for value in row.values() if value is not None)
                        physical = number + HEADER_ROW
                        cells = []
                        for i, (column, value) in enumerate(row.items()):
                            kind = "date" if column == "date" else "formula" if column == "formula" else "number" if column == "amount" or (column == "late_type" and number <= 100000) else "text"
                            cells.append(cell(f'{chr(65+i)}{physical}', value, kind))
                        output.write((f'<row r="{physical}">' + ''.join(cells) + '</row>').encode())
                        if number % 10000 == 0:
                            db.commit()
                    # Formatting at Excel's final physical row is not a record.
                    output.write(b'<row r="1048576"><c r="A1048576" s="1"/></row></sheetData></worksheet>')
                sst.write(b"</sst>")
                sst.flush()
                if strings == "shared":
                    archive.write(work / "shared.xml", "xl/sharedStrings.xml")
            db.commit()
    with path.open("rb") as source:
        file_hash = hashlib.file_digest(source, "sha256").hexdigest()
    with zipfile.ZipFile(path) as archive:
        expanded_bytes = sum(entry.file_size for entry in archive.infolist())
    result = {"schema_version": 1, "generator": "OOXML_DIVERSE_V1", "path": str(path), "rows": rows,
        "columns": list(COLUMNS), "sheet": "Datos", "header_physical_row": HEADER_ROW,
        "first_record_physical_row": HEADER_ROW + 1, "last_record_physical_row": HEADER_ROW + rows,
        "declared_dimension_trusted": False, "strings": strings, "shared_string_count": next_index,
        "actual_bytes": path.stat().st_size, "expanded_bytes": expanded_bytes, "file_sha256": file_hash,
        "canonical_rows_sha256": digest.hexdigest(), "observed_utf8_bytes": observed_bytes,
        "expected_observed_nulls": rows // 100, "expected_observed_empty": len(range(1, rows + 1, 100)),
        "expected_cardinality": {"record_id": rows, "payload": rows, "region": min(rows, 20)},
        "seed_sha256": hashlib.sha256(SEED).hexdigest(), "payload_width_bytes": 96,
        "compressed_expanded_ratio": path.stat().st_size / expanded_bytes,
        "generation_seconds": round(time.monotonic() - started, 3), "max_buffer_rows": 1,
        "formats": {"XLSX": {"path": str(path), "actual_bytes": path.stat().st_size, "sha256": file_hash}}}
    result["oracle"] = verify(path, result)
    metadata_path.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return result


def verify(path, expected):
    """Decode every physical OOXML record independently; compare all values."""
    started, digest, count, empty, null = time.monotonic(), hashlib.sha256(), 0, 0, 0
    with TemporaryDirectory(prefix="xlsx-oracle-", dir=Path(path).parent) as temporary, ExitStack() as stack:
        db = stack.enter_context(closing(sqlite3.connect(Path(temporary) / "strings.sqlite")))
        db.execute("PRAGMA cache_size=-4096")
        db.execute("CREATE TABLE strings (ordinal INTEGER PRIMARY KEY, value TEXT)")
        with zipfile.ZipFile(path) as archive:
            if "xl/sharedStrings.xml" in archive.namelist():
                with archive.open("xl/sharedStrings.xml") as source:
                    parser = ET.iterparse(source, events=("start", "end"))
                    _, root = next(parser)
                    index = 0
                    for event, element in parser:
                        if event == "end" and element.tag == f"{{{NS}}}si":
                            value = ''.join(node.text or '' for node in element.iter(f"{{{NS}}}t"))
                            db.execute("INSERT INTO strings VALUES (?,?)", (index, value))
                            index += 1
                            if index % 10000 == 0:
                                db.commit()
                            root.clear()
                db.commit()
            with archive.open("xl/worksheets/sheet2.xml") as source:
                parser = ET.iterparse(source, events=("start", "end"))
                _, root = next(parser)
                for event, element in parser:
                    if event != "end" or element.tag != f"{{{NS}}}row":
                        continue
                    physical = int(element.attrib["r"])
                    if physical <= HEADER_ROW:
                        element.clear()
                        root.clear()
                        continue
                    row = dict.fromkeys(COLUMNS)
                    for cell in element:
                        ordinal = ord(cell.attrib["r"][0]) - 65
                        column = COLUMNS[ordinal]
                        formula, value = cell.find(f"{{{NS}}}f"), cell.find(f"{{{NS}}}v")
                        if formula is not None:
                            observed = "=" + (formula.text or "")
                        elif cell.attrib.get("t") == "inlineStr":
                            observed = ''.join(node.text or '' for node in cell.iter(f"{{{NS}}}t"))
                        elif cell.attrib.get("t") == "s":
                            observed = db.execute("SELECT value FROM strings WHERE ordinal=?", (int(value.text),)).fetchone()[0]
                        elif value is not None:
                            observed = (date(1899, 12, 30) + timedelta(days=int(value.text))).isoformat() if cell.attrib.get("s") == "1" else value.text
                        else:
                            observed = None
                        row[column] = observed
                    if any(value is not None for value in row.values()):
                        count += 1
                        if physical != count + HEADER_ROW or row != expected_row(count):
                            raise AssertionError(f"Oracle mismatch at physical row {physical}; values withheld.")
                        digest.update(canonical(row))
                        empty += row["observed"] == ""
                        null += row["observed"] is None
                    element.clear()
                    root.clear()
    result = {"rows": count, "canonical_rows_sha256": digest.hexdigest(), "null_count": null,
        "empty_count": empty, "status": "PASS", "max_buffer_rows": 1,
        "elapsed_seconds": round(time.monotonic() - started, 3), "reader": "STDLIB_XML_SQLITE_INDEPENDENT"}
    if count != expected["rows"] or digest.hexdigest() != expected["canonical_rows_sha256"]:
        raise AssertionError("Whole-file oracle mismatch.")
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--directory", type=Path, required=True)
    parser.add_argument("--rows", type=int, nargs="+", default=[100001, 400000, 1000000])
    parser.add_argument("--strings", choices=["inline", "shared"], nargs="+", default=["inline", "shared"])
    args = parser.parse_args()
    for rows in args.rows:
        for strings in args.strings:
            result = generate(args.directory, rows, strings=strings)
            print(json.dumps({key: result[key] for key in ("path", "rows", "strings", "actual_bytes", "expanded_bytes", "oracle")}), flush=True)


if __name__ == "__main__":
    main()
