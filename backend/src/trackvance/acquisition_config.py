"""Validated, effective bounds for acquisition, independent of legacy HTTP limits."""

import os
from dataclasses import asdict, dataclass


def _integer(name: str, default: int, minimum: int, maximum: int) -> int:
    try:
        value = int(os.getenv(name, str(default)))
    except ValueError:
        raise RuntimeError(f"{name} debe ser un entero.") from None
    if not minimum <= value <= maximum:
        raise RuntimeError(f"{name} debe estar entre {minimum} y {maximum}.")
    return value


@dataclass(frozen=True)
class AcquisitionLimits:
    # A batch is capped by BOTH records and observed UTF-8 bytes. No inferred
    # percentage or whole-result materialization is implied by these budgets.
    max_upload_bytes: int = 1024 * 1024 * 1024
    max_rows: int = 5_000_000
    max_observed_bytes: int = 2 * 1024 * 1024 * 1024
    batch_rows: int = 5_000
    batch_bytes: int = 8 * 1024 * 1024
    bounded_format_bytes: int = 10 * 1024 * 1024
    bounded_format_rows: int = 100_000
    memory_bytes: int = 256 * 1024 * 1024
    min_free_bytes: int = 512 * 1024 * 1024
    timeout_seconds: int = 1_800
    upload_ttl_seconds: int = 86_400
    xlsx_max_rows: int = 1_000_000
    xlsx_max_upload_bytes: int = 1024**3
    xlsx_max_expanded_bytes: int = 4 * 1024**3
    xlsx_metadata_bytes: int = 8 * 1024**2
    xlsx_inspection_bytes: int = 4 * 1024**2
    xlsx_cache_bytes: int = 8 * 1024**2
    xlsx_max_entries: int = 4096
    xlsx_max_styles: int = 65_536
    xlsx_max_cells: int = 100_000_000
    xlsx_max_record_bytes: int = 1024**2
    xlsx_temp_bytes: int = 8 * 1024**3
    xlsx_metadata_seconds: int = 10
    xlsx_inspection_seconds: int = 5

    @classmethod
    def configured(cls) -> "AcquisitionLimits":
        return cls(
            max_upload_bytes=_integer("TRACKVANCE_ACQUISITION_MAX_UPLOAD_BYTES", 1024**3, 1024, 5 * 1024**3),
            max_rows=_integer("TRACKVANCE_ACQUISITION_MAX_ROWS", 5_000_000, 1, 100_000_000),
            max_observed_bytes=_integer("TRACKVANCE_ACQUISITION_MAX_OBSERVED_BYTES", 2 * 1024**3, 1024, 20 * 1024**3),
            batch_rows=_integer("TRACKVANCE_ACQUISITION_BATCH_ROWS", 5_000, 1, 50_000),
            batch_bytes=_integer("TRACKVANCE_ACQUISITION_BATCH_BYTES", 8 * 1024**2, 65_536, 64 * 1024**2),
            bounded_format_bytes=_integer("TRACKVANCE_ACQUISITION_BOUNDED_FORMAT_BYTES", 10 * 1024**2, 1024, 64 * 1024**2),
            bounded_format_rows=_integer("TRACKVANCE_ACQUISITION_BOUNDED_FORMAT_ROWS", 100_000, 1, 100_000),
            memory_bytes=_integer("TRACKVANCE_ACQUISITION_MEMORY_BYTES", 256 * 1024**2, 64 * 1024**2, 4 * 1024**3),
            min_free_bytes=_integer("TRACKVANCE_ACQUISITION_MIN_FREE_BYTES", 512 * 1024**2, 16 * 1024**2, 20 * 1024**3),
            timeout_seconds=_integer("TRACKVANCE_ACQUISITION_TIMEOUT_SECONDS", 1_800, 30, 86_400),
            upload_ttl_seconds=_integer("TRACKVANCE_ACQUISITION_UPLOAD_TTL_SECONDS", 86_400, 300, 604_800),
            xlsx_max_rows=_integer("TRACKVANCE_ACQUISITION_XLSX_MAX_ROWS", 1_000_000, 1, 1_048_575),
            xlsx_max_upload_bytes=_integer("TRACKVANCE_ACQUISITION_XLSX_MAX_UPLOAD_BYTES", 1024**3, 1024, 5 * 1024**3),
            xlsx_max_expanded_bytes=_integer("TRACKVANCE_ACQUISITION_XLSX_MAX_EXPANDED_BYTES", 4 * 1024**3, 1024, 20 * 1024**3),
            xlsx_metadata_bytes=_integer("TRACKVANCE_ACQUISITION_XLSX_METADATA_BYTES", 8 * 1024**2, 64 * 1024, 64 * 1024**2),
            xlsx_inspection_bytes=_integer("TRACKVANCE_ACQUISITION_XLSX_INSPECTION_BYTES", 4 * 1024**2, 64 * 1024, 16 * 1024**2),
            xlsx_cache_bytes=_integer("TRACKVANCE_ACQUISITION_XLSX_CACHE_BYTES", 8 * 1024**2, 64 * 1024, 16 * 1024**2),
            xlsx_max_entries=_integer("TRACKVANCE_ACQUISITION_XLSX_MAX_ENTRIES", 4096, 4, 16_384),
            xlsx_max_styles=_integer("TRACKVANCE_ACQUISITION_XLSX_MAX_STYLES", 65_536, 1, 65_536),
            xlsx_max_cells=_integer("TRACKVANCE_ACQUISITION_XLSX_MAX_CELLS", 100_000_000, 1, 104_857_600),
            xlsx_max_record_bytes=_integer("TRACKVANCE_ACQUISITION_XLSX_MAX_RECORD_BYTES", 1024**2, 1, 8 * 1024**2),
            xlsx_temp_bytes=_integer("TRACKVANCE_ACQUISITION_XLSX_TEMP_BYTES", 8 * 1024**3, 1024, 40 * 1024**3),
            xlsx_metadata_seconds=_integer("TRACKVANCE_ACQUISITION_XLSX_METADATA_SECONDS", 10, 1, 60),
            xlsx_inspection_seconds=_integer("TRACKVANCE_ACQUISITION_XLSX_INSPECTION_SECONDS", 5, 1, 30),
        )

    def as_dict(self) -> dict[str, int]:
        return asdict(self)

    def describe(self, source_format: str, *, header_row: int | None = None, inspection_limited: bool = False,
                 route: str = "ASYNC_ACQUISITION") -> dict:
        format_name = source_format.upper()
        rows, compressed = self.max_rows, self.max_upload_bytes
        if format_name == "XLSX":
            rows = min(rows, self.xlsx_max_rows, 1_048_576 - (header_row or 1))
            compressed = min(compressed, self.xlsx_max_upload_bytes)
        elif format_name == "JSON":
            rows, compressed = min(rows, self.bounded_format_rows), min(compressed, self.bounded_format_bytes)
        if route == "LEGACY_UPLOAD":
            from .config import MAX_ROWS, MAX_UPLOAD_BYTES

            rows, compressed = MAX_ROWS, MAX_UPLOAD_BYTES
        values = {
            "data_rows": (rows, "records"), "compressed_bytes": (compressed, "bytes"),
            "observed_bytes": (self.max_observed_bytes, "bytes"), "columns": (100, "columns"),
            "cell_bytes": (65_536, "bytes"), "record_bytes": (100 * 65_536 + 4096, "bytes"),
            "batch_rows": (self.batch_rows, "records"), "batch_bytes": (self.batch_bytes, "bytes"),
            "memory_bytes": (self.memory_bytes, "bytes"), "disk_reserve_bytes": (self.min_free_bytes, "bytes"),
            "timeout_seconds": (self.timeout_seconds, "seconds"),
        }
        if format_name == "XLSX" and route == "ASYNC_ACQUISITION":
            values.update(expanded_bytes=(self.xlsx_max_expanded_bytes, "bytes"),
                metadata_bytes=(self.xlsx_metadata_bytes, "bytes"), inspection_bytes=(self.xlsx_inspection_bytes, "bytes"),
                cache_bytes=(self.xlsx_cache_bytes, "bytes"), entries=(self.xlsx_max_entries, "entries"),
                styles=(self.xlsx_max_styles, "entries"), materialized_cells=(self.xlsx_max_cells, "cells"),
                record_bytes=(self.xlsx_max_record_bytes, "bytes"), temporary_bytes=(self.xlsx_temp_bytes, "bytes"),
                metadata_seconds=(self.xlsx_metadata_seconds, "seconds"), inspection_seconds=(self.xlsx_inspection_seconds, "seconds"))
        result = {"route": route, "format": format_name,
                  "limits": {name: {"max": value, "unit": unit} for name, (value, unit) in values.items()},
                  "header_row_number": header_row, "inspection_limited": inspection_limited}
        if format_name == "XLSX":
            result["physical_sheet_rows"] = 1_048_576
        return result

