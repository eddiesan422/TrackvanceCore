"""Bounded observed-value readers, with deterministic source record positions.

CSV/TXT, Parquet and NDJSON support the volume contract. XLSX and ordinary JSON
retain the explicitly smaller legacy format bounds. Complete inference/profile
is performed later over all materialized parts, never over the inspection sample.
"""

from __future__ import annotations

import csv
import json
from collections.abc import Callable, Generator, Iterator
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from typing import Any

import duckdb
import polars as pl

from .acquisition_config import AcquisitionLimits
from .dataset_readers import (
    DatasetReadResult,
    ReaderOptions,
    _cell_text,
    _detect_delimiter,
    _flatten_json,
    _frame_from_rows,
    _reject_json_constant,
    _text_sample,
    _unique_json_object,
    _validate_headers,
    dataset_reader_registry,
)
from .processing import ProcessingError

RECORD_NUMBER_COLUMN = "__tv_record_number"
MAX_CELL_BYTES = 65_536
MAX_RECORD_BYTES = 100 * MAX_CELL_BYTES + 4096


@dataclass
class DatasetBatch:
    frame: pl.DataFrame
    record_numbers: list[int]
    source_format: str
    format_label: str
    media_type: str
    row_numbering: str
    native_schema: dict[str, str] = field(default_factory=dict)
    metadata: dict[str, Any] = field(default_factory=dict)

    def physical_frame(self) -> pl.DataFrame:
        return self.frame.with_columns(pl.Series(RECORD_NUMBER_COLUMN, self.record_numbers, dtype=pl.Int64))


def observed_row(values: list[Any]) -> tuple[list[str | None], int]:
    normalized, size = [], 0
    for value in values:
        rendered = _cell_text(value)
        count = len(rendered.encode("utf-8")) if rendered is not None else 0
        if count > MAX_CELL_BYTES:
            raise ProcessingError("ACQUISITION_CELL_LIMIT: Una celda supera 64 KiB.")
        size += count
        normalized.append(rendered)
    return normalized, size


class _RecordLines:
    """csv.reader pulls physical lines; retain at most the current record."""

    def __init__(self, stream):
        self.stream, self.lines, self.bytes = stream, [], 0

    def __iter__(self):
        return self

    def __next__(self):
        line = self.stream.readline(MAX_RECORD_BYTES + 1)
        if not line:
            raise StopIteration
        self.bytes += len(line.encode("utf-8"))
        if self.bytes > MAX_RECORD_BYTES:
            raise ProcessingError("ACQUISITION_RECORD_LIMIT: Un registro supera el límite de lectura.")
        self.lines.append(line)
        return line

    def take(self) -> str:
        value = "".join(self.lines)
        self.lines.clear()
        self.bytes = 0
        return value


def _quoted_fields(raw: str, delimiter: str) -> list[bool]:
    """Empty quoted text differs from an unquoted missing CSV value."""
    flags, inside, start, index = [], False, True, 0
    while index < len(raw):
        character = raw[index]
        if start:
            flags.append(character == '"')
            inside = character == '"'
            start = False
            index += 1
            continue
        if character == '"' and inside:
            if index + 1 < len(raw) and raw[index + 1] == '"':
                index += 2
                continue
            inside = False
        elif character == delimiter and not inside:
            start = True
        index += 1
    if start:
        flags.append(False)
    return flags


def delimited_rows(path: Path, delimiter: str) -> Iterator[tuple[list[str | None], int]]:
    with path.open(encoding="utf-8-sig", newline="") as stream:
        tracker = _RecordLines(stream)
        parser = csv.reader(tracker, delimiter=delimiter, strict=True)
        try:
            headers = next(parser, [])
            tracker.take()
            _validate_headers(headers)
            while True:
                number = parser.line_num + 1
                row = next(parser, None)
                if row is None:
                    break
                raw = tracker.take()
                if not row:  # Empty physical lines have no logical record.
                    continue
                if len(row) != len(headers):
                    raise ProcessingError("ACQUISITION_SCHEMA_MISMATCH: Un registro no coincide con el encabezado.")
                flags = _quoted_fields(raw, delimiter)
                yield [value if value != "" or flags[index] else None
                       for index, value in enumerate(row)], number
        except (csv.Error, UnicodeDecodeError):
            raise ProcessingError("ACQUISITION_INVALID_TEXT: Revisa el delimitador, las comillas y UTF-8.") from None


def _json_line_records(path: Path, check: Callable[[], None]) -> Iterator[tuple[dict, int]]:
    with path.open(encoding="utf-8-sig") as source:
        number = 0
        while raw := source.readline(MAX_RECORD_BYTES + 1):
            if len(raw.encode("utf-8")) > MAX_RECORD_BYTES:
                raise ProcessingError("ACQUISITION_RECORD_LIMIT: Un registro JSON supera el límite.")
            if not raw.strip():
                continue
            number += 1
            if number % 1000 == 1:
                check()
            try:
                value = json.loads(raw, parse_float=Decimal, parse_constant=_reject_json_constant,
                                   object_pairs_hook=_unique_json_object)
                if not isinstance(value, dict):
                    raise TypeError()
            except (ValueError, TypeError):
                raise ProcessingError("ACQUISITION_INVALID_JSONL: Cada registro debe ser un objeto JSON válido.") from None
            yield _flatten_json(value), number


def _is_json_lines(path: Path, filename: str) -> bool:
    if Path(filename).suffix.casefold() in {".jsonl", ".ndjson"}:
        return True
    with path.open(encoding="utf-8-sig") as stream:
        found = 0
        for _ in range(10):
            line = stream.readline(MAX_RECORD_BYTES + 1)
            if not line:
                break
            if not line.strip():
                continue
            try:
                record = json.loads(line, parse_float=Decimal, parse_constant=_reject_json_constant,
                                    object_pairs_hook=_unique_json_object)
            except ValueError:
                return False
            if not isinstance(record, dict):
                return False
            found += 1
            if found == 2:
                return True
    return False


def _native_kind(value: Any) -> str:
    if isinstance(value, bool):
        return "Boolean"
    if isinstance(value, int):
        return "Int64"
    if isinstance(value, Decimal):
        return "Decimal"
    return "String"


class FileBatchReader:
    def __init__(self, path: Path, filename: str, options: dict | None = None,
                 limits: AcquisitionLimits | None = None, check: Callable[[], None] | None = None,
                 *, inspection: bool = False):
        self.path, self.filename = path, filename
        self.options = ReaderOptions.from_mapping(options)
        self.limits, self.check = limits or AcquisitionLimits.configured(), check or (lambda: None)
        self.inspection = inspection
        self.reader = dataset_reader_registry.resolve(path, filename)
        self.metadata: dict[str, Any] = {}
        self.headers: list[str] = []
        self.native_schema: dict[str, str] = {}
        self.total_rows: int | None = None
        self.format = self.reader.format
        self.row_numbering = "RECORD_NUMBER"

    def _batches(self, rows: Iterator[tuple[list[Any], int]]) -> Iterator[DatasetBatch]:
        batch: list[list[str | None]] = []
        numbers: list[int] = []
        batch_bytes = total_bytes = total_rows = 0
        for raw, number in rows:
            normalized, size = observed_row(raw)
            if size > self.limits.batch_bytes:
                raise ProcessingError("ACQUISITION_BATCH_LIMIT: Un registro supera el presupuesto del lote.")
            if batch and (len(batch) >= self.limits.batch_rows or batch_bytes + size > self.limits.batch_bytes):
                self.check()
                yield self._batch(batch, numbers)
                batch, numbers, batch_bytes = [], [], 0
            total_rows += 1
            total_bytes += size
            if total_rows > self.limits.max_rows or total_bytes > self.limits.max_observed_bytes:
                raise ProcessingError("ACQUISITION_SIZE_LIMIT: La fuente supera los límites efectivos; no se publicaron datos parciales.")
            batch.append(normalized)
            numbers.append(number)
            batch_bytes += size
            if self.inspection and total_rows >= 100:
                break
        self.check()
        if batch or total_rows == 0:
            yield self._batch(batch, numbers)
        if not self.inspection:
            self.total_rows = total_rows

    def _batch(self, rows: list, numbers: list[int]) -> DatasetBatch:
        return DatasetBatch(_frame_from_rows(self.headers, rows), numbers, self.format,
                            self.reader.format_label, self.reader.media_type, self.row_numbering,
                            self.native_schema, dict(self.metadata))

    def __iter__(self) -> Generator[DatasetBatch]:
        self.check()
        if self.path.stat().st_size > self.limits.max_upload_bytes:
            raise ProcessingError("ACQUISITION_UPLOAD_LIMIT: El archivo supera el límite efectivo.")
        if self.format in {"CSV", "TXT"}:
            sample = _text_sample(self.path)
            delimiter = self.options.delimiter or _detect_delimiter(sample, csv_legacy_default=self.format == "CSV")
            # Read headers using the same multiline-aware parser as records.
            with self.path.open(encoding="utf-8-sig", newline="") as source:
                self.headers = next(csv.reader(_RecordLines(source), delimiter=delimiter, strict=True), [])
            _validate_headers(self.headers)
            self.native_schema = {column: "String" for column in self.headers}
            self.row_numbering = "PHYSICAL_LINE"
            self.metadata = {"reader_options": {"delimiter": delimiter}}
            yield from self._batches(delimited_rows(self.path, delimiter))
        elif self.format == "JSON" and _is_json_lines(self.path, self.filename):
            self.metadata = {"variant": "JSON_LINES"}
            kinds: dict[str, set[str]] = {}
            nonnull = set()
            count = size = 0
            # Full schema discovery keeps at most 100 column names, not records.
            for record, _ in _json_line_records(self.path, self.check):
                count += 1
                if count > self.limits.max_rows:
                    raise ProcessingError("ACQUISITION_SIZE_LIMIT: El archivo supera el límite de registros.")
                _, record_size = observed_row(list(record.values()))
                size += record_size
                if size > self.limits.max_observed_bytes:
                    raise ProcessingError("ACQUISITION_SIZE_LIMIT: El archivo supera el límite de valores observados.")
                for name, value in record.items():
                    if name not in kinds:
                        self.headers.append(name)
                        kinds[name] = set()
                        _validate_headers(self.headers)
                    if value is not None:
                        nonnull.add(name)
                        kinds[name].add(_native_kind(value))
                if self.inspection and count >= 100:
                    break
            parents = {name for name in self.headers if any(child.startswith(name + ".") for child in self.headers)}
            if parents & nonnull:
                raise ProcessingError("ACQUISITION_JSON_SCHEMA_CONFLICT: Un campo alterna objeto y escalar.")
            self.headers = [name for name in self.headers if name not in parents]
            _validate_headers(self.headers)
            self.native_schema = {name: next(iter(kinds[name])) if len(kinds[name]) == 1 else
                                  "Decimal" if kinds[name] and kinds[name] <= {"Int64", "Decimal"} else "String"
                                  for name in self.headers}
            self.total_rows = None if self.inspection else count
            yield from self._batches((([record.get(name) for name in self.headers], number)
                                      for record, number in _json_line_records(self.path, self.check)))
        elif self.format == "PARQUET":
            schema = pl.read_parquet_schema(self.path)
            self.headers = list(schema)
            _validate_headers(self.headers)
            self.native_schema = {name: str(dtype) for name, dtype in schema.items()}
            connection = duckdb.connect(":memory:", config={"memory_limit": str(self.limits.memory_bytes) + "B", "threads": "1"})
            try:
                footer = connection.execute("SELECT num_rows FROM parquet_file_metadata(?)", [str(self.path)]).fetchone()
                expanded = connection.execute("SELECT COALESCE(SUM(total_uncompressed_size),0) FROM parquet_metadata(?)", [str(self.path)]).fetchone()
                largest_group = connection.execute("SELECT COALESCE(MAX(bytes),0) FROM (SELECT SUM(total_uncompressed_size) AS bytes FROM parquet_metadata(?) GROUP BY row_group_id)", [str(self.path)]).fetchone()
            finally:
                connection.close()
            if not footer or not expanded or footer[0] > self.limits.max_rows or expanded[0] > self.limits.max_observed_bytes:
                raise ProcessingError("ACQUISITION_PARQUET_LIMIT: El contenido expandido supera el presupuesto.")
            if not largest_group or largest_group[0] > self.limits.memory_bytes // 2:
                raise ProcessingError("ACQUISITION_PARQUET_ROW_GROUP_LIMIT: Un grupo Parquet supera el presupuesto de descompresión; divide los grupos de origen.")
            self.total_rows = int(footer[0])
            self.metadata = {"expanded_size_bytes": int(expanded[0])}
            def parquet_rows():
                import pyarrow.parquet as pq  # type: ignore[import-untyped]

                position = 0
                read_rows = min(self.limits.batch_rows, max(1, self.limits.batch_bytes // (max(1, len(schema)) * MAX_CELL_BYTES)))
                # One decompression per row group rather than one per slice.
                # The complete footer above limits each decoded group before
                # Arrow opens it; pre-buffering and reader threads stay disabled.
                with pq.ParquetFile(self.path, memory_map=False, pre_buffer=False) as parquet:
                    for arrow_batch in parquet.iter_batches(batch_size=read_rows, use_threads=False):
                        self.check()
                        chunk = pl.from_arrow(arrow_batch)
                        assert isinstance(chunk, pl.DataFrame)
                        # Python datetime is microsecond precision; render ns
                        # timestamps while the Arrow/Polars scalar remains exact.
                        for column, dtype in schema.items():
                            if isinstance(dtype, pl.Datetime) and dtype.time_unit == "ns":
                                fmt = "%Y-%m-%dT%H:%M:%S%.9f%:z" if dtype.time_zone else "%Y-%m-%dT%H:%M:%S%.9f"
                                chunk = chunk.with_columns(pl.col(column).dt.strftime(fmt).alias(column))
                                self.native_schema[column] = "String"
                        for row in chunk.iter_rows():
                            position += 1
                            yield list(row), position
            yield from self._batches(parquet_rows())
        else:
            if self.path.stat().st_size > self.limits.bounded_format_bytes:
                raise ProcessingError("ACQUISITION_FORMAT_LIMIT: XLSX y JSON no lineal tienen un límite explícito menor; utiliza CSV, Parquet o NDJSON para volumen.")
            result: DatasetReadResult = self.reader.inspect(self.path, self.options) if self.inspection else self.reader.read(self.path, self.options)
            if result.frame.height > self.limits.bounded_format_rows:
                raise ProcessingError("ACQUISITION_FORMAT_LIMIT: El formato supera el límite de registros permitido.")
            self.headers, self.native_schema = result.frame.columns, result.native_schema
            self.metadata = {**result.metadata, "reader_options": self.options.as_dict(),
                             "sheet_name": result.selected_sheet, "sheets": result.sheets}
            self.total_rows, self.row_numbering = result.row_count, result.row_numbering
            yield from self._batches(((list(row), number) for number, row in enumerate(result.frame.iter_rows(), 1)))


def inspect_file(path: Path, filename: str, options: dict | None = None,
                 limits: AcquisitionLimits | None = None) -> dict:
    reader = FileBatchReader(path, filename, options, limits, inspection=True)
    batches = iter(reader)
    try:
        first = next(batches)
        from .processing import profile_frame
        schema, _, _ = profile_frame(first.frame, native_types=first.native_schema)
        return {"format": first.source_format, "format_label": first.format_label,
                "filename": filename, "sheets": first.metadata.get("sheets", []),
                "selected_sheet": first.metadata.get("sheet_name"),
                "detected_delimiter": first.metadata.get("reader_options", {}).get("delimiter"),
                "reader_options": first.metadata.get("reader_options", {}),
                "columns": [{**column, "numeric": column["logical_type"] in {"INT64", "DECIMAL"}
                             and column.get("semantic_tag") != "IDENTIFIER",
                             "native_type": first.native_schema.get(column["name"])} for column in schema],
                "sample": first.frame.head(20).to_dicts(), "sampled_rows": first.frame.height,
                "row_count": reader.total_rows, "sampled": True,
                "row_numbering": first.row_numbering}
    finally:
        batches.close()
