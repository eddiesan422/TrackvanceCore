"""Incremental OOXML acquisition, independent of the legacy workbook reader.

ZIP XML is consumed through a bounded SAX feed. Shared strings and shared
formula leaders live in an attempt-private SQLite index, not a Python list.
Worksheet dimensions are advisory and are never used to stop reading records.
"""

from __future__ import annotations

import posixpath
import re
import shutil
import sqlite3
import struct
import time
import zipfile
from collections import OrderedDict, deque
from collections.abc import Callable, Generator, Iterator
from dataclasses import dataclass
from datetime import date, datetime
from datetime import time as datetime_time
from decimal import Decimal, InvalidOperation
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any
from xml.parsers import expat

from openpyxl.formula.translate import Translator
from openpyxl.styles.numbers import BUILTIN_FORMATS, is_date_format, is_timedelta_format
from openpyxl.utils.datetime import (
    CALENDAR_MAC_1904,
    CALENDAR_WINDOWS_1900,
    from_excel,
)

from .acquisition_config import AcquisitionLimits
from .acquisition_errors import AcquisitionReadError, limit_error
from .dataset_readers import (
    DatasetCellLimit,
    ReaderOptions,
    _cell_text,
    _validate_headers,
)
from .processing import ProcessingError

CHUNK_BYTES = 65_536
CELL_BYTES = 65_536
PHYSICAL_ROWS = 1_048_576
PHYSICAL_COLUMNS = 16_384
_CELL = re.compile(r"([A-Za-z]{1,3})([1-9][0-9]{0,6})\Z")


def _invalid(message: str = "El paquete XLSX o su estructura XML no son válidos.") -> AcquisitionReadError:
    return AcquisitionReadError("ACQUISITION_XLSX_INVALID_STRUCTURE", message)


class _InspectionBound(Exception):
    pass


class _SampleComplete(Exception):
    pass


@dataclass(frozen=True)
class _SharedString:
    position: int


def _local(name: str) -> str:
    return name.rsplit("}", 1)[-1]


def _coordinate(value: str) -> tuple[int, int]:
    match = _CELL.fullmatch(value)
    if match is None:
        raise _invalid("Una coordenada de celda XLSX no es válida.")
    column = 0
    for letter in match.group(1).upper():
        column = column * 26 + ord(letter) - 64
    row = int(match.group(2))
    if row > PHYSICAL_ROWS or column > PHYSICAL_COLUMNS:
        raise _invalid("Una celda excede las dimensiones físicas permitidas de una hoja Excel.")
    return column, row


def _column_label(column: int) -> str:
    label = ""
    while column:
        column, remaining = divmod(column - 1, 26)
        label = chr(65 + remaining) + label
    return label


def _observed_iso_date(raw: str) -> date | str:
    """Validate ISO cell syntax without rounding or removing its UTC offset.

    Python's parsers are used only to validate calendar/range constraints. The
    canonical datetime/time scalar is the original XML text, including every
    fractional digit. Complete profiling decides whether it fits the portable
    TIMESTAMP policy (explicit offset, at most six fractional digits).
    """
    if re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", raw):
        return date.fromisoformat(raw)
    suffix = r"(?:\.[0-9]+)?(?:Z|[+-][0-9]{2}:[0-9]{2})?"
    if re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}" + suffix, raw):
        datetime.fromisoformat(raw)
        return raw
    if re.fullmatch(r"[0-9]{2}:[0-9]{2}:[0-9]{2}" + suffix, raw):
        datetime_time.fromisoformat(raw)
        return raw
    raise _invalid("Una celda de fecha ISO XLSX no tiene una representación válida.")


def _header_names(values: dict[int, Any]) -> list[str] | None:
    # Match the legacy header convention only here: empty preamble rows are
    # skipped and trailing empty/whitespace header cells do not add columns.
    # Data values keep their exact empty/whitespace/null distinctions.
    if not values or not any(value is not None and str(value) != "" for value in values.values()):
        return None
    headers = [str(_cell_text(values.get(column)) or "") for column in range(1, max(values) + 1)]
    while headers and not headers[-1].strip():
        headers.pop()
    try:
        _validate_headers(headers)
    except ProcessingError:
        raise AcquisitionReadError("ACQUISITION_HEADER_INVALID", "El encabezado XLSX contiene nombres vacíos, repetidos o reservados.") from None
    return headers


def _central_directory(path: Path, limits: AcquisitionLimits) -> None:
    # ZipFile reads the complete central directory during construction. Check
    # its EOCD/ZIP64 size before that allocation, not after infolist().
    size = path.stat().st_size
    with path.open("rb") as source:
        source.seek(max(0, size - 65_557))
        tail = source.read(65_557)
        offset = tail.rfind(b"PK\x05\x06")
        if offset < 0 or len(tail) - offset < 22:
            raise _invalid()
        _, disk, directory_disk, entries_disk, entries, directory_size, directory_offset, comment = struct.unpack_from("<4s4H2LH", tail, offset)
        if offset + 22 + comment != len(tail) or disk or directory_disk:
            raise _invalid("El paquete XLSX no puede estar dividido en varios discos.")
        if entries == 65_535 or directory_size == 0xFFFFFFFF or directory_offset == 0xFFFFFFFF:
            absolute = max(0, size - 65_557) + offset
            if absolute < 20:
                raise _invalid()
            source.seek(absolute - 20)
            locator = source.read(20)
            if len(locator) != 20 or locator[:4] != b"PK\x06\x07":
                raise _invalid()
            _, zip_disk, zip_offset, disks = struct.unpack("<4sLQL", locator)
            if zip_disk or disks != 1 or zip_offset > size - 56:
                raise _invalid()
            source.seek(zip_offset)
            record = source.read(56)
            if len(record) != 56 or record[:4] != b"PK\x06\x06":
                raise _invalid()
            values = struct.unpack("<4sQ2H2L4Q", record)
            if values[4] or values[5] or values[6] != values[7]:
                raise _invalid()
            entries, directory_size, directory_offset = values[7:10]
        elif entries_disk != entries:
            raise _invalid()
        if directory_size > limits.xlsx_metadata_bytes:
            raise limit_error("ACQUISITION_XLSX_METADATA_LIMIT", "metadata_bytes", limits.xlsx_metadata_bytes, directory_size)
        if entries > limits.xlsx_max_entries:
            raise limit_error("ACQUISITION_XLSX_PACKAGE_LIMIT", "entries", limits.xlsx_max_entries, entries)
        if directory_offset + directory_size > size:
            raise _invalid()


class _TokenBound:
    """Reject huge incomplete XML tags before Expat buffers their attributes."""

    def __init__(self):
        self.open = False
        self.quote: int | None = None
        self.size = 0

    def feed(self, chunk: bytes) -> None:
        position = 0
        while position < len(chunk):
            if not self.open:
                found = chunk.find(b"<", position)
                if found < 0:
                    return
                self.open, self.size, self.quote, position = True, 1, None, found + 1
                continue
            if self.quote is not None:
                found = chunk.find(bytes([self.quote]), position)
                next_position = len(chunk) if found < 0 else found + 1
                self.size += next_position - position
                if found >= 0:
                    self.quote = None
            else:
                endings = [(chunk.find(character, position), character) for character in (b">", b"'", b'"')]
                endings = [(index, character) for index, character in endings if index >= 0]
                found, character = min(endings) if endings else (-1, b"")
                next_position = len(chunk) if found < 0 else found + 1
                self.size += next_position - position
                if character == b">":
                    self.open = False
                elif found >= 0:
                    self.quote = character[0]
            if self.size > CELL_BYTES:
                raise _invalid("Un token XML excede el presupuesto de metadatos por nodo.")
            position = next_position


class _Index:
    def __init__(self, directory: Path, cache_bytes: int):
        self.path = directory / "xlsx-index.sqlite"
        self.connection = sqlite3.connect(self.path)
        self.connection.execute("PRAGMA journal_mode=OFF")
        self.connection.execute("PRAGMA synchronous=OFF")
        self.connection.execute("PRAGMA temp_store=FILE")
        # Split the advertised cache between SQLite and decoded Python strings.
        self.connection.execute(f"PRAGMA cache_size=-{max(1, cache_bytes // 2048)}")
        self.connection.execute("CREATE TABLE strings (position INTEGER PRIMARY KEY, value TEXT NOT NULL)")
        self.connection.execute("CREATE TABLE formulas (identity TEXT PRIMARY KEY, origin TEXT NOT NULL, value TEXT NOT NULL)")
        self.cache: OrderedDict[int, tuple[str, int]] = OrderedDict()
        self.cache_bytes, self.cache_limit = 0, max(1, cache_bytes // 2)

    def put(self, position: int, value: str) -> None:
        self.connection.execute("INSERT INTO strings VALUES (?, ?)", (position, value))

    def get(self, position: int) -> str:
        cached = self.cache.get(position)
        if cached is not None:
            self.cache.move_to_end(position)
            return cached[0]
        result = self.connection.execute("SELECT value FROM strings WHERE position=?", (position,)).fetchone()
        if result is None:
            raise _invalid("Una celda referencia una cadena compartida inexistente.")
        value = result[0]
        # A Unicode scalar occupies at most four bytes per code point. Include
        # conservative Python object/key overhead, not just UTF-8 payload bytes.
        size = 4 * len(value) + 256
        if size <= self.cache_limit:
            while self.cache and self.cache_bytes + size > self.cache_limit:
                _, (_, removed) = self.cache.popitem(last=False)
                self.cache_bytes -= removed
            self.cache[position] = value, size
            self.cache_bytes += size
        return value

    def formula(self, identity: str, origin: str, value: str) -> str:
        if len(identity) > 32 or not identity.isdigit():
            raise _invalid("La identidad de fórmula compartida no es válida.")
        if value:
            self.connection.execute("INSERT INTO formulas VALUES (?, ?, ?)", (identity, origin, value))
            return value
        item = self.connection.execute("SELECT origin,value FROM formulas WHERE identity=?", (identity,)).fetchone()
        if item is None:
            raise _invalid("Una fórmula compartida no tiene definición anterior.")
        try:
            translated = Translator("=" + item[1], origin=item[0]).translate_formula(origin)
        except (ValueError, IndexError):
            raise _invalid("La fórmula compartida no se puede interpretar como texto observado.") from None
        _cell_text(translated)
        return translated[1:]

    def close(self) -> None:
        self.connection.close()
        self.cache.clear()


class _Strings:
    def __init__(self, put: Callable[[int, str], None]):
        self.put, self.position = put, 0
        self.parts: list[str] = []
        self.size, self.in_item, self.in_text, self.phonetic = 0, False, False, 0

    def start(self, name: str, attrs: dict[str, str]) -> None:
        if name == "si":
            self.in_item, self.parts, self.size = True, [], 0
        elif name == "rPh":
            self.phonetic += 1
        elif name == "t" and self.in_item and not self.phonetic:
            self.in_text = True

    def text(self, value: str) -> None:
        if self.in_text:
            self.size += len(value.encode("utf-8"))
            if self.size > CELL_BYTES:
                raise limit_error("ACQUISITION_CELL_LIMIT", "cell_bytes", CELL_BYTES, self.size)
            self.parts.append(value)

    def end(self, name: str) -> None:
        if name == "t":
            self.in_text = False
        elif name == "rPh":
            self.phonetic -= 1
        elif name == "si":
            self.put(self.position, "".join(self.parts))
            self.position += 1
            self.parts, self.in_item = [], False


class _Metadata:
    def __init__(self, start: Callable[[str, dict[str, str]], None]):
        self.start = start

    def end(self, name: str) -> None:
        pass

    def text(self, value: str) -> None:
        pass


class _Styles:
    def __init__(self, limits: AcquisitionLimits):
        self.limits = limits
        self.formats: dict[int, str] = {}
        self.cell_formats: list[tuple[bool, bool, str]] = []
        self.in_cell_formats = False

    def start(self, name: str, attrs: dict[str, str]) -> None:
        if name == "numFmt":
            if len(self.formats) >= self.limits.xlsx_max_styles:
                raise limit_error("ACQUISITION_XLSX_STYLE_LIMIT", "styles", self.limits.xlsx_max_styles)
            self.formats[int(attrs["numFmtId"])] = attrs["formatCode"]
        elif name == "cellXfs":
            self.in_cell_formats = True
        elif name == "xf" and self.in_cell_formats:
            if len(self.cell_formats) >= self.limits.xlsx_max_styles:
                raise limit_error("ACQUISITION_XLSX_STYLE_LIMIT", "styles", self.limits.xlsx_max_styles)
            identity = int(attrs.get("numFmtId", "0"))
            code = self.formats.get(identity, BUILTIN_FORMATS.get(identity, "General"))
            self.cell_formats.append((is_date_format(code), is_timedelta_format(code), code))

    def end(self, name: str) -> None:
        if name == "cellXfs":
            self.in_cell_formats = False

    def text(self, value: str) -> None:
        pass


class _Worksheet:
    def __init__(self, source: XlsxStreamingReader):
        self.source = source
        self.rows: deque[tuple[dict[int, Any], int]] = deque()
        self.values: dict[int, Any] = {}
        self.previous_row, self.row_number, self.last_column = 0, 0, 0
        self.cell: dict[str, str] | None = None
        self.value_parts: list[str] = []
        self.formula_parts: list[str] = []
        self.text_parts: list[str] = []
        self.collect: str | None = None
        self.text_present, self.formula_present = False, False
        self.phonetic, self.cell_size, self.row_size, self.parsed_cells = 0, 0, 0, 0
        self.formula_attributes: dict[str, str] = {}
        self.row_explicit = False

    def start(self, name: str, attrs: dict[str, str]) -> None:
        if name == "row":
            self.row_explicit = "r" in attrs
            self.row_number = int(attrs.get("r", str(self.previous_row + 1)))
            if not self.previous_row < self.row_number <= PHYSICAL_ROWS:
                raise _invalid("Las filas físicas XLSX no están en orden o exceden el límite de la hoja.")
            self.values, self.last_column, self.row_size = {}, 0, 0
        elif name == "c":
            coordinate = attrs.get("r")
            if coordinate:
                column, row = _coordinate(coordinate)
                if not self.row_explicit and self.last_column == 0 and row > self.previous_row:
                    self.row_number = row
                if row != self.row_number:
                    raise _invalid("Una celda no pertenece a su fila física.")
            else:
                column = self.last_column + 1
                coordinate = f"{_column_label(column)}{self.row_number}"
            if column <= self.last_column or column > PHYSICAL_COLUMNS:
                raise _invalid("Las celdas XLSX no están en orden o se repiten.")
            self.last_column = column
            self.cell = {**attrs, "r": coordinate, "column": str(column)}
            self.value_parts, self.formula_parts, self.text_parts = [], [], []
            self.text_present, self.formula_present, self.cell_size = False, False, 0
            self.parsed_cells += 1
            if self.parsed_cells > PHYSICAL_ROWS * 100:
                raise limit_error("ACQUISITION_XLSX_CELL_LIMIT", "materialized_cells", PHYSICAL_ROWS * 100, self.parsed_cells)
        elif self.cell is not None:
            if name == "f":
                self.collect, self.formula_present, self.formula_attributes = "f", True, attrs
            elif name == "v":
                self.collect = "v"
            elif name == "rPh":
                self.phonetic += 1
            elif name == "t" and not self.phonetic:
                self.collect, self.text_present = "t", True

    def text(self, value: str) -> None:
        if self.cell is None or self.collect is None:
            return
        self.cell_size += len(value.encode("utf-8"))
        if self.cell_size > CELL_BYTES:
            raise limit_error("ACQUISITION_CELL_LIMIT", "cell_bytes", CELL_BYTES, self.cell_size)
        {"v": self.value_parts, "f": self.formula_parts, "t": self.text_parts}[self.collect].append(value)

    def end(self, name: str) -> None:
        if name in {"v", "f", "t"}:
            self.collect = None
        elif name == "rPh":
            self.phonetic -= 1
        elif name == "c" and self.cell is not None:
            value = self._value()
            if value is not None:
                normalized = None if isinstance(value, _SharedString) else _cell_text(value)
                size = len(normalized.encode("utf-8")) if normalized is not None else 0
                self.row_size += size
                if self.row_size > self.source.limits.xlsx_max_record_bytes:
                    raise limit_error("ACQUISITION_RECORD_LIMIT", "record_bytes", self.source.limits.xlsx_max_record_bytes, self.row_size)
                column = int(self.cell["column"])
                if column > 100:
                    raise limit_error("ACQUISITION_COLUMN_LIMIT", "columns", 100, column)
                self.values[column] = value
            self.cell = None
        elif name == "row":
            self.previous_row = self.row_number
            self.rows.append((self.values, self.row_number))
            self.values = {}

    def _value(self) -> Any:
        assert self.cell is not None
        if self.formula_present:
            formula = "".join(self.formula_parts)
            if self.formula_attributes.get("t") == "shared":
                if self.source.index is None:
                    # Inspection does not create a complete formula index.
                    if not formula:
                        raise _InspectionBound()
                else:
                    formula = self.source.index.formula(self.formula_attributes.get("si", ""), self.cell["r"], formula)
            return "=" + formula
        kind, raw = self.cell.get("t", "n"), "".join(self.value_parts)
        if kind == "inlineStr":
            return "".join(self.text_parts) if self.text_present else None
        if kind == "s":
            if len(raw) > 12 or not raw.isdigit():
                raise _invalid("La referencia a cadena compartida no es válida.")
            return self.source.string(int(raw))
        if kind in {"str", "e"}:
            return raw if self.value_parts else None
        if not self.value_parts:
            return None
        if kind == "b":
            if raw not in {"0", "1"}:
                raise _invalid("Un booleano XLSX no es válido.")
            return raw == "1"
        if kind == "d":
            return _observed_iso_date(raw)
        if kind != "n":
            raise AcquisitionReadError("ACQUISITION_XLSX_UNSUPPORTED_CONTENT", "El libro contiene un tipo de celda no soportado.")
        try:
            value = Decimal(raw)
        except InvalidOperation:
            raise _invalid("Una celda numérica XLSX no contiene un número válido.") from None
        if not value.is_finite():
            raise _invalid("Una celda numérica XLSX no es finita.")
        _cell_text(value)  # Check expansion before converting/formatting a scalar.
        style = int(self.cell.get("s", "0"))
        if style < 0 or style >= len(self.source.styles):
            if style:
                raise _invalid("Una celda referencia un estilo inexistente.")
        elif self.source.styles[style][0]:
            _, duration, code = self.source.styles[style]
            if duration:
                # A duration is not a date/timestamp. Preserve its observed
                # numeric serial instead of inventing an epoch or timezone.
                return value
            try:
                temporal = from_excel(float(value), self.source.epoch)
            except (OverflowError, ValueError):
                raise _invalid("Una fecha serial XLSX está fuera del rango permitido.") from None
            if isinstance(temporal, datetime) and not re.search(r"[hHsS]", code):
                return temporal.date()
            return temporal
        return int(value) if len(raw) <= 18 and value == value.to_integral_value() and "e" not in raw.lower() and "." not in raw else value


class XlsxStreamingReader:
    def __init__(self, path: Path, options: ReaderOptions, limits: AcquisitionLimits,
                 check: Callable[[], None], *, inspection: bool = False, work_dir: Path | None = None):
        self.path, self.options, self.limits, self.check = path, options, limits, check
        self.inspection, self.work_dir = inspection, work_dir
        self.headers: list[str] = []
        self.native_schema: dict[str, str] = {}
        self.kinds: dict[str, set[str]] = {}
        self.metadata: dict[str, Any] = {}
        self.sheets: list[tuple[str, str]] = []
        self.styles: list[tuple[bool, bool, str]] = []
        self.epoch = CALENDAR_WINDOWS_1900
        self.index: _Index | None = None
        self.package: zipfile.ZipFile | None = None
        self.header_row: int | None = None
        self.total_rows: int | None = None
        self.inspection_limited = False
        self.inspection_bytes, self.metadata_bytes = 0, 0
        self.inspection_started = time.monotonic()
        self.metadata_started = self.inspection_started
        self.expanded_seen: dict[str, int] = {}
        self.current_path: str | None = None
        self.temporary: TemporaryDirectory | None = None

    def string(self, position: int) -> str | _SharedString:
        if self.inspection:
            return _SharedString(position)
        if self.index is None:
            raise _invalid("No está disponible el índice de cadenas compartidas.")
        return self.index.get(position)

    def _resource_check(self) -> None:
        self.check()
        if time.monotonic() - self.inspection_started > self.limits.timeout_seconds:
            raise AcquisitionReadError("ACQUISITION_TIMEOUT", "La lectura XLSX excedió su presupuesto de tiempo.", {"maximum_seconds": self.limits.timeout_seconds})
        if self.inspection:
            if time.monotonic() - self.inspection_started > self.limits.xlsx_inspection_seconds:
                raise _InspectionBound()
            return
        if self.work_dir is not None:
            used = sum(item.stat().st_size for item in self.work_dir.iterdir() if item.is_file())
            if used > self.limits.xlsx_temp_bytes:
                raise limit_error("ACQUISITION_TEMP_DISK_LIMIT", "temporary_bytes", self.limits.xlsx_temp_bytes, used)
            if shutil.disk_usage(self.work_dir).free < self.limits.min_free_bytes:
                raise AcquisitionReadError("RESOURCE_DISK_INSUFFICIENT", "No queda la reserva de disco necesaria para adquirir XLSX.",
                                           {"minimum_free_bytes": self.limits.min_free_bytes})

    def _xml(self, name: str, handler: Any, *, metadata: bool = False) -> Iterator[None]:
        assert self.package is not None
        if name not in self.package.namelist():
            raise _invalid("Falta un componente obligatorio del paquete XLSX.")
        info = self.package.getinfo(name)
        if metadata and info.file_size + self.metadata_bytes > self.limits.xlsx_metadata_bytes:
            raise limit_error("ACQUISITION_XLSX_METADATA_LIMIT", "metadata_bytes", self.limits.xlsx_metadata_bytes, info.file_size + self.metadata_bytes)
        parser = expat.ParserCreate(namespace_separator="}")
        tokens = _TokenBound()
        depth = 0

        def start(name: str, attrs: dict[str, str]) -> None:
            nonlocal depth
            depth += 1
            if depth > 64 or len(attrs) > 128 or sum(len(key.encode()) + len(value.encode()) for key, value in attrs.items()) > CELL_BYTES:
                raise _invalid("La complejidad de un nodo XML excede el presupuesto permitido.")
            handler.start(_local(name), {_local(key): value for key, value in attrs.items()})

        def end(name: str) -> None:
            nonlocal depth
            handler.end(_local(name))
            depth -= 1

        def forbidden(*args: Any) -> None:
            raise _invalid("XLSX no admite DTD ni declaraciones de entidades XML.")

        parser.StartElementHandler, parser.EndElementHandler, parser.CharacterDataHandler = start, end, handler.text
        parser.StartDoctypeDeclHandler = forbidden
        parser.EntityDeclHandler = forbidden
        parser.ExternalEntityRefHandler = lambda *args: 0
        read = 0
        with self.package.open(info) as stream:
            while True:
                self._resource_check()
                if metadata and time.monotonic() - self.metadata_started > self.limits.xlsx_metadata_seconds:
                    raise AcquisitionReadError("ACQUISITION_METADATA_TIMEOUT", "La lectura de metadatos excedió su presupuesto de tiempo.",
                        {"maximum_seconds": self.limits.xlsx_metadata_seconds})
                chunk = stream.read(CHUNK_BYTES)
                if self.inspection:
                    self.inspection_bytes += len(chunk)
                    if self.inspection_bytes > self.limits.xlsx_inspection_bytes:
                        raise _InspectionBound()
                read += len(chunk)
                if metadata:
                    self.metadata_bytes += len(chunk)
                previous = self.expanded_seen.get(name, 0)
                self.expanded_seen[name] = max(previous, read)
                expanded = sum(self.expanded_seen.values())
                if expanded > self.limits.xlsx_max_expanded_bytes:
                    raise limit_error("ACQUISITION_EXPANDED_SIZE_LIMIT", "expanded_bytes", self.limits.xlsx_max_expanded_bytes, expanded)
                tokens.feed(chunk)
                if isinstance(handler, _Worksheet):
                    # A short SST reference can expand into a 64-KiB value.
                    # Yield decoded rows between small SAX feeds, so a 64-KiB
                    # XML read cannot queue thousands of wide Python rows.
                    feed_bytes = min(1024, max(16, self.limits.batch_bytes // (2 * CELL_BYTES) * 16))
                    for offset in range(0, len(chunk), feed_bytes):
                        parser.Parse(chunk[offset:offset + feed_bytes], False)
                        yield None
                    if not chunk:
                        parser.Parse(b"", True)
                        yield None
                else:
                    parser.Parse(chunk, not chunk)
                    yield None
                if not chunk:
                    break

    def _parse(self, name: str, handler: Any, *, metadata: bool = False) -> None:
        for _ in self._xml(name, handler, metadata=metadata):
            pass

    def _open(self) -> None:
        compressed = min(self.limits.max_upload_bytes, self.limits.xlsx_max_upload_bytes)
        if self.path.stat().st_size > compressed:
            raise limit_error("ACQUISITION_COMPRESSED_SIZE_LIMIT", "compressed_bytes", compressed, self.path.stat().st_size)
        _central_directory(self.path, self.limits)
        self.package = zipfile.ZipFile(self.path)
        members = self.package.infolist()
        if len(members) > self.limits.xlsx_max_entries:
            raise limit_error("ACQUISITION_XLSX_PACKAGE_LIMIT", "entries", self.limits.xlsx_max_entries, len(members))
        names = set()
        expanded = 0
        for member in members:
            self.check()
            name = member.filename
            if name in names or "\\" in name or name.startswith("/") or ".." in name.split("/") or member.flag_bits & 1:
                raise _invalid("El paquete XLSX contiene miembros ambiguos, cifrados o rutas no válidas.")
            names.add(name)
            if member.compress_type not in {zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED}:
                raise AcquisitionReadError("ACQUISITION_XLSX_UNSUPPORTED_CONTENT", "El método de compresión XLSX no está soportado.")
            if "vbaproject" in name.casefold() or name.casefold().endswith(".xlsb"):
                raise AcquisitionReadError("ACQUISITION_XLSX_UNSUPPORTED_CONTENT", "Los libros con macros o contenido binario no están soportados.")
            expanded += member.file_size
        if expanded > self.limits.xlsx_max_expanded_bytes:
            raise limit_error("ACQUISITION_EXPANDED_SIZE_LIMIT", "expanded_bytes", self.limits.xlsx_max_expanded_bytes, expanded)
        self.metadata["expanded_size_bytes"] = expanded

        def content_type(name: str, attrs: dict[str, str]) -> None:
            if "macroenabled" in attrs.get("ContentType", "").casefold() or "vbaproject" in attrs.get("ContentType", "").casefold():
                raise AcquisitionReadError("ACQUISITION_XLSX_UNSUPPORTED_CONTENT", "Los libros con macros no están soportados.")

        self._parse("[Content_Types].xml", _Metadata(content_type), metadata=True)
        relationships: dict[str, str] = {}

        def relationship(name: str, attrs: dict[str, str]) -> None:
            if name != "Relationship" or attrs.get("TargetMode") == "External":
                return
            target = attrs.get("Target", "")
            resolved = posixpath.normpath(target.lstrip("/")) if target.startswith("/") else posixpath.normpath(posixpath.join("xl", target))
            if "\\" in target or not resolved.startswith("xl/") or ".." in resolved.split("/"):
                raise _invalid("Una relación de libro XLSX apunta fuera de sus componentes internos.")
            relationships[attrs["Id"]] = resolved

        self._parse("xl/_rels/workbook.xml.rels", _Metadata(relationship), metadata=True)

        def workbook(name: str, attrs: dict[str, str]) -> None:
            if name == "workbookPr" and attrs.get("date1904") in {"1", "true"}:
                self.epoch = CALENDAR_MAC_1904
            if name == "sheet":
                title, relation = attrs.get("name", ""), attrs.get("id", "")
                if not title or relation not in relationships or len(self.sheets) >= self.limits.xlsx_max_entries:
                    raise _invalid("La definición de hojas XLSX no es válida.")
                self.sheets.append((title, relationships[relation]))

        self._parse("xl/workbook.xml", _Metadata(workbook), metadata=True)
        if not self.sheets:
            raise _invalid("El libro XLSX no contiene hojas.")
        self.metadata["sheets"] = [name for name, _ in self.sheets]
        self.metadata.update(reader_key="xlsx", reader_version=2, inference_scope="COMPLETE_POPULATION",
                             zip_xml_policy="BOUNDED_SAX_NO_DTD_V1", shared_strings_policy="SQLITE_BOUNDED_CACHE_V1")
        if self.options.sheet_name and self.options.sheet_name not in self.metadata["sheets"]:
            raise AcquisitionReadError("ACQUISITION_SHEET_NOT_FOUND", "La hoja seleccionada no existe en el libro.", {"available_sheet_count": len(self.sheets)})
        if "xl/styles.xml" in names:
            styles = _Styles(self.limits)
            self._parse("xl/styles.xml", styles, metadata=True)
            self.styles = styles.cell_formats

    def _strings(self) -> None:
        assert self.package is not None and self.work_dir is not None
        self.index = _Index(self.work_dir, self.limits.xlsx_cache_bytes)
        if "xl/sharedStrings.xml" in self.package.namelist():
            handler = _Strings(self.index.put)
            self._parse("xl/sharedStrings.xml", handler)
            self.index.connection.commit()
            self.metadata["shared_string_count"] = handler.position
        self._resource_check()

    def _inspection_rows(self) -> Iterator[tuple[list[Any], int]]:
        """Read only bounded sheet XML and the SST prefix needed by that sample.

        A high SST ordinal is not permission to scan all shared strings inside
        an HTTP request. Missing values remain outside the returned sample;
        they are never changed to null or treated as an empty source.
        """
        assert self.package is not None
        candidates = [(name, path) for name, path in self.sheets if not self.options.sheet_name or name == self.options.sheet_name]
        resolved: dict[int, str] = {}
        def resolve(values: dict[int, Any]) -> dict[int, Any] | None:
            if any(isinstance(value, _SharedString) and value.position not in resolved for value in values.values()):
                return None
            return {column: resolved[value.position] if isinstance(value, _SharedString) else value for column, value in values.items()}

        header_index = None
        for selected, path in candidates:
            sampled: list[tuple[dict[int, Any], int]] = []
            raw = self._raw_rows(path)
            try:
                for values, physical in raw:
                    if values:
                        sampled.append((values, physical))
                        if len(sampled) >= 101:
                            break
            except _InspectionBound:
                self.inspection_limited = True
            finally:
                raw.close()
            needed = {value.position for values, _ in sampled for value in values.values()
                      if isinstance(value, _SharedString)} - resolved.keys()
            if needed:
                if "xl/sharedStrings.xml" not in self.package.namelist():
                    raise _invalid("El libro referencia cadenas compartidas sin su componente XML.")
                def remember(position: int, value: str, wanted: set[int] = needed) -> None:
                    if position in wanted:
                        resolved[position] = value
                        if not wanted - resolved.keys():
                            raise _SampleComplete()
                try:
                    self._parse("xl/sharedStrings.xml", _Strings(remember))
                except _SampleComplete:
                    pass
                except _InspectionBound:
                    self.inspection_limited = True
                if needed - resolved.keys() and not self.inspection_limited:
                    raise _invalid("Una celda referencia una cadena compartida inexistente.")
            for index, (values, physical) in enumerate(sampled):
                header_values = resolve(values)
                if header_values is None:
                    self.inspection_limited = True
                    return
                headers = _header_names(header_values)
                if headers is not None:
                    self.headers, self.header_row, header_index = headers, physical, index
                    break
            if header_index is not None:
                break
            if len(sampled) >= 101 or self.inspection_limited:
                self.inspection_limited = True
                return
        if header_index is None:
            raise AcquisitionReadError("ACQUISITION_XLSX_NO_HEADER", "La hoja no contiene un encabezado tabular dentro del límite de búsqueda.")
        self.native_schema.update({column: "String" for column in self.headers})
        self.kinds = {column: set() for column in self.headers}
        self.metadata.update(sheet_name=selected, physical_header_row_number=self.header_row, reader_options={"sheet_name": selected})
        observed_bytes = 0
        for values, physical in sampled[header_index + 1:]:
            resolved_values = resolve(values)
            if resolved_values is None:
                self.inspection_limited = True
                break
            if max(resolved_values) > len(self.headers):
                raise AcquisitionReadError("ACQUISITION_XLSX_SCHEMA_DRIFT", "Una fila XLSX contiene valores fuera de las columnas del encabezado.")
            row = [resolved_values.get(column) for column in range(1, len(self.headers) + 1)]
            size = sum(len((_cell_text(value) or "").encode("utf-8")) for value in row)
            if observed_bytes + size > self.limits.batch_bytes:
                self.inspection_limited = True
                break
            observed_bytes += size
            self._infer(row)
            yield row, physical

    def _raw_rows(self, path: str) -> Generator[tuple[dict[int, Any], int]]:
        handler = _Worksheet(self)
        for _ in self._xml(path, handler):
            while handler.rows:
                yield handler.rows.popleft()

    def _infer(self, row: list[Any]) -> None:
        for name, value in zip(self.headers, row, strict=True):
            if value is None:
                continue
            kind = ("Boolean" if isinstance(value, bool) else "Int64" if isinstance(value, int) else
                    "Decimal" if isinstance(value, Decimal) else
                    ("Datetime" if value.utcoffset() is not None else "String") if isinstance(value, datetime) else
                    "Date" if isinstance(value, date) else "String")
            kinds = self.kinds[name]
            kinds.add(kind)
            self.native_schema[name] = next(iter(kinds)) if len(kinds) == 1 else "Decimal" if kinds <= {"Int64", "Decimal"} else "String"

    def _sheet_rows(self) -> Iterator[tuple[list[Any], int]]:
        candidates = [(name, path) for name, path in self.sheets if not self.options.sheet_name or name == self.options.sheet_name]
        for name, path in candidates:
            header = False
            count = 0
            for values, physical in self._raw_rows(path):
                if not values:
                    continue
                if not header:
                    headers = _header_names(values)
                    if headers is None:
                        continue
                    self.headers = headers
                    self.header_row = physical
                    self.native_schema.update({column: "String" for column in self.headers})
                    self.kinds = {column: set() for column in self.headers}
                    self.metadata.update(sheet_name=name, physical_header_row_number=physical, reader_options={"sheet_name": name})
                    header = True
                    continue
                if max(values) > len(self.headers):
                    raise AcquisitionReadError("ACQUISITION_XLSX_SCHEMA_DRIFT", "Una fila XLSX contiene valores fuera de las columnas del encabezado.")
                row = [values.get(column) for column in range(1, len(self.headers) + 1)]
                count += 1
                assert self.header_row is not None
                maximum = min(self.limits.max_rows, self.limits.xlsx_max_rows, PHYSICAL_ROWS - self.header_row)
                if count > maximum:
                    raise limit_error("ACQUISITION_ROW_LIMIT", "data_rows", maximum, count)
                if count * len(self.headers) > self.limits.xlsx_max_cells:
                    raise limit_error("ACQUISITION_XLSX_CELL_LIMIT", "materialized_cells", self.limits.xlsx_max_cells, count * len(self.headers))
                self._infer(row)
                yield row, physical
                if self.inspection and count >= 100:
                    return
            if header:
                if not self.inspection:
                    self.total_rows = count
                return
            if self.options.sheet_name:
                raise AcquisitionReadError("ACQUISITION_XLSX_NO_HEADER", "La hoja seleccionada no tiene un encabezado tabular dentro del límite de búsqueda.")
        raise AcquisitionReadError("ACQUISITION_XLSX_NO_HEADER", "El libro no contiene una hoja con encabezado tabular dentro del límite de búsqueda.")

    def __iter__(self) -> Generator[tuple[list[Any], int]]:
        try:
            self._open()
            if self.inspection:
                yield from self._inspection_rows()
                return
            if self.work_dir is None:
                self.temporary = TemporaryDirectory(prefix="trackvance-xlsx-")
                self.work_dir = Path(self.temporary.name)
            self._strings()
            yield from self._sheet_rows()
        except _InspectionBound:
            self.inspection_limited = True
        except DatasetCellLimit:
            raise limit_error("ACQUISITION_CELL_LIMIT", "cell_bytes", CELL_BYTES) from None
        except AcquisitionReadError:
            raise
        except sqlite3.Error as exc:
            if getattr(exc, "sqlite_errorcode", None) == sqlite3.SQLITE_FULL:
                raise AcquisitionReadError("RESOURCE_DISK_INSUFFICIENT", "No hay espacio para el índice temporal XLSX.") from None
            if getattr(exc, "sqlite_errorcode", None) == sqlite3.SQLITE_NOMEM:
                raise AcquisitionReadError("ACQUISITION_MEMORY_LIMIT", "No hay memoria para el índice acotado XLSX.") from None
            raise AcquisitionReadError("ACQUISITION_XLSX_INDEX_FAILED", "No se pudo completar el índice temporal XLSX.") from None
        except (zipfile.BadZipFile, expat.ExpatError, KeyError, ValueError, UnicodeError, OverflowError):
            raise _invalid() from None
        finally:
            if self.index is not None:
                self.index.close()
                self.index.path.unlink(missing_ok=True)
            if self.package is not None:
                self.package.close()
            if self.temporary is not None:
                self.temporary.cleanup()
