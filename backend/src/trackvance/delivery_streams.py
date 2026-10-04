"""Repeatable bounded scans and integrity-checked local driver payloads.

SQL transaction ownership remains in DataSink. These sequences can be traversed
several times without retaining the population in the API or delivery worker.
"""

import hashlib
import json
import os
import shutil
import sqlite3
from collections.abc import Callable, Iterable, Iterator, Sequence
from contextlib import closing
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal, localcontext
from itertools import islice
from pathlib import Path
from typing import Any, overload

import duckdb
import pyarrow.parquet as pq  # type: ignore[import-untyped]

from .artifactstore import file_hash, storage_provider


def setting(name: str, default: int, lower: int, upper: int) -> int:
    try:
        value = int(os.getenv(name, str(default)))
    except ValueError:
        raise RuntimeError(f"{name} debe ser un entero.") from None
    if not lower <= value <= upper:
        raise RuntimeError(f"{name} debe estar entre {lower} y {upper}.")
    return value


@dataclass(frozen=True)
class DeliveryLimits:
    batch_rows: int = 1000
    memory_bytes: int = 256 * 1024**2
    max_rows: int = 5_000_000
    reserve_disk_bytes: int = 512 * 1024**2
    synchronous_rows: int = 100_000
    preparation_timeout_seconds: int = 1800

    @classmethod
    def configured(cls):
        return cls(
            setting("TRACKVANCE_DELIVERY_BATCH_ROWS", 1000, 1, 10_000),
            setting("TRACKVANCE_DELIVERY_SCAN_MEMORY_BYTES", 256 * 1024**2, 64 * 1024**2, 2 * 1024**3),
            setting("TRACKVANCE_DELIVERY_MAX_ROWS", 5_000_000, 1, 100_000_000),
            setting("TRACKVANCE_DELIVERY_RESERVE_DISK_BYTES", 512 * 1024**2, 16 * 1024**2, 20 * 1024**3),
            setting("TRACKVANCE_DELIVERY_SYNCHRONOUS_ROWS", 100_000, 1, 100_000),
            setting("TRACKVANCE_DELIVERY_PREPARATION_TIMEOUT_SECONDS", 1800, 30, 86400),
        )


def batches[T](values: Iterable[T], size: int | None = None) -> Iterator[list[T]]:
    maximum = size or DeliveryLimits.configured().batch_rows
    chunk: list[T] = []
    byte_count = 0
    for value in values:
        cells = value.values() if isinstance(value, dict) else value if isinstance(value, tuple) else (value,)
        observed = sum(len(str(cell).encode("utf-8")) for cell in cells if cell is not None)
        if chunk and (len(chunk) >= maximum or byte_count + observed > 8 * 1024**2):
            yield chunk
            chunk, byte_count = [], 0
        chunk.append(value)
        byte_count += observed
    if chunk:
        yield chunk


class DatasetRecords(Sequence[dict[str, Any]]):
    def __init__(self, paths: list[Path], columns: list[str], *, limit: int | None = None, control: Callable[[], None] | None = None):
        if not paths:
            raise ValueError("Se requiere al menos una parte Parquet.")
        self.paths, self.columns, self.limit, self.control = paths, columns, limit, control
        # API publication only needs verified metadata, not a native SQL scan.
        # Read one footer at a time without DuckDB's Python parameter import cache;
        # concurrent request threads must not initialize that native cache here.
        self.height = 0
        for path in paths:
            with pq.ParquetFile(path, memory_map=False, pre_buffer=False) as parquet:
                if not set(columns) <= set(parquet.schema_arrow.names):
                    raise ValueError("La estructura física no contiene las columnas de la versión.")
                self.height += parquet.metadata.num_rows
        self.width = len(columns)

    def _connection(self):
        return duckdb.connect(":memory:", config={
            "threads": "1", "memory_limit": f"{DeliveryLimits.configured().memory_bytes}B",
            "preserve_insertion_order": "true",
        })

    def __len__(self):
        return min(self.height, self.limit) if self.limit is not None else self.height

    def __iter__(self):
        if self.limit is not None:
            # Preview is bounded to at most 100 rows by head(). Avoid native SQL
            # parameter initialization in the API thread pool. One physical row
            # per Arrow batch also bounds allocation for unusually wide records.
            count = 0
            for path in self.paths:
                if count >= self.limit:
                    return
                with pq.ParquetFile(path, memory_map=False, pre_buffer=False) as parquet:
                    for batch in parquet.iter_batches(batch_size=1, columns=self.columns, use_threads=False):
                        if self.control:
                            self.control()
                        yield batch.to_pylist()[0]
                        count += 1
                        if count >= self.limit:
                            return
            return
        names = ",".join('"' + name.replace('"', '""') + '"' for name in self.columns)
        query = f"SELECT {names} FROM read_parquet(?)"
        with self._connection() as db:
            cursor = db.execute(query, [list(map(str, self.paths))])
            count = 0
            while (row := cursor.fetchone()) is not None:
                if self.control and count % 1000 == 0:
                    self.control()
                count += 1
                yield dict(zip(self.columns, row, strict=True))

    @overload
    def __getitem__(self, index: int) -> dict[str, Any]: ...

    @overload
    def __getitem__(self, index: slice) -> list[dict[str, Any]]: ...

    def __getitem__(self, index):
        if isinstance(index, slice):
            start, stop, step = index.indices(len(self))
            if stop - start > 10_000:
                raise ValueError("La muestra solicitada supera el límite.")
            return list(islice(iter(self), start, stop, step))
        if index < 0:
            index += len(self)
        if not 0 <= index < len(self):
            raise IndexError(index)
        return next(islice(iter(self), index, index + 1))

    def select(self, columns: list[str]):
        if not set(columns) <= set(self.columns):
            raise ValueError("El mapping selecciona columnas ausentes.")
        return DatasetRecords(self.paths, columns, limit=self.limit, control=self.control)

    def head(self, limit: int):
        return DatasetRecords(self.paths, self.columns, limit=min(limit, 100), control=self.control)

    def to_dicts(self):
        if self.limit is None:
            raise ValueError("Una población completa debe recorrerse por lotes.")
        return list(self)


def _encode(value: Any):
    if isinstance(value, datetime):
        return ["datetime", value.isoformat()]
    if isinstance(value, date):
        return ["date", value.isoformat()]
    if isinstance(value, Decimal):
        return ["decimal", str(value)]
    if value is None or type(value) in {str, int, bool}:
        return ["scalar", value]
    raise ValueError("El payload contiene un tipo no admitido.")


def _decode(value):
    kind, raw = value
    if kind == "datetime":
        return datetime.fromisoformat(raw)
    if kind == "date":
        return date.fromisoformat(raw)
    if kind == "decimal":
        return Decimal(raw)
    if kind == "scalar" and (raw is None or type(raw) in {str, int, bool}):
        return raw
    raise ValueError("El payload tipado no es válido.")


class PreparedRows(Sequence[tuple[Any, ...]]):
    """A sealed SQLite spool with exact typed values and per-row digests."""

    def __init__(self, path: Path, count: int, sha256: str, binding_hash: str):
        self.path, self.row_count, self.sha256, self.binding_hash = path, count, sha256, binding_hash

    @classmethod
    def write(cls, rows: Iterable[tuple], binding: dict) -> "PreparedRows":
        path = storage_provider.temporary_path(".sqlite")
        count = 0
        binding_hash = hashlib.sha256(json.dumps(binding, sort_keys=True, default=str, separators=(",", ":")).encode()).hexdigest()
        try:
            with closing(sqlite3.connect(path)) as db, db:
                db.execute("CREATE TABLE payload (ordinal INTEGER PRIMARY KEY, value TEXT NOT NULL, sha256 TEXT NOT NULL)")
                db.execute("CREATE TABLE binding (hash TEXT NOT NULL)")
                db.execute("INSERT INTO binding VALUES (?)", [binding_hash])
                for chunk in batches(rows):
                    if shutil.disk_usage(path.parent).free < DeliveryLimits.configured().reserve_disk_bytes:
                        raise ValueError("No hay reserva de disco suficiente para preparar la entrega.")
                    entries = []
                    for row in chunk:
                        count += 1
                        value = json.dumps([_encode(v) for v in row], ensure_ascii=False, separators=(",", ":"))
                        entries.append((count, value, hashlib.sha256(value.encode()).hexdigest()))
                    db.executemany("INSERT INTO payload VALUES (?,?,?)", entries)
                db.commit()
            result = cls(path, count, file_hash(path), binding_hash)
            result.verify()
            return result
        except BaseException:
            path.unlink(missing_ok=True)
            raise

    def verify(self, expected_binding: dict | None = None):
        if expected_binding is not None:
            expected = hashlib.sha256(json.dumps(expected_binding, sort_keys=True, default=str, separators=(",", ":")).encode()).hexdigest()
            if expected != self.binding_hash:
                raise ValueError("La preparación no corresponde a la configuración efectiva de esta ejecución.")
        if not self.path.is_file() or file_hash(self.path) != self.sha256:
            raise ValueError("La preparación local perdió su integridad.")
        with closing(sqlite3.connect(self.path.as_uri() + "?mode=ro", uri=True)) as db:
            if db.execute("SELECT hash FROM binding").fetchone() != (self.binding_hash,):
                raise ValueError("La preparación no corresponde a su configuración.")
            if db.execute("SELECT count(*) FROM payload").fetchone()[0] != self.row_count:
                raise ValueError("El conteo preparado no coincide.")

    def __len__(self):
        return self.row_count

    def __iter__(self):
        with closing(sqlite3.connect(self.path.as_uri() + "?mode=ro", uri=True)) as db:
            cursor = db.execute("SELECT value,sha256 FROM payload ORDER BY ordinal")
            while (entry := cursor.fetchone()) is not None:
                value, digest = entry
                if hashlib.sha256(value.encode()).hexdigest() != digest:
                    raise ValueError("Una fila preparada perdió su integridad.")
                yield tuple(_decode(item) for item in json.loads(value))

    @overload
    def __getitem__(self, index: int) -> tuple[Any, ...]: ...

    @overload
    def __getitem__(self, index: slice) -> list[tuple[Any, ...]]: ...

    def __getitem__(self, index):
        if isinstance(index, slice):
            start, stop, step = index.indices(self.row_count)
            if stop - start > 10_000:
                raise ValueError("El lote preparado supera el límite.")
            return list(islice(iter(self), start, stop, step))
        if index < 0:
            index += self.row_count
        if not 0 <= index < self.row_count:
            raise IndexError(index)
        return next(islice(iter(self), index, index + 1))

    def remove(self):
        self.path.unlink(missing_ok=True)


def unique_keys(values: Iterable[tuple[Any, ...]]) -> bool:
    """Exact global equality probe, with disk storage rather than a population set."""
    path = storage_provider.temporary_path(".sqlite")
    valid = True
    try:
        with closing(sqlite3.connect(path)) as db, db:
            db.execute("CREATE TABLE keys (value TEXT PRIMARY KEY)")
            for chunk in batches(values):
                if shutil.disk_usage(path.parent).free < DeliveryLimits.configured().reserve_disk_bytes:
                    raise ValueError("No hay reserva de disco suficiente para validar claves globales.")
                for key in chunk:
                    if any(value is None for value in key):
                        valid = False
                    # Decimal equality ignores representation scale, as Python
                    # tuples did historically; normalization uses exact Decimal.
                    normalized = []
                    for value in key:
                        if isinstance(value, Decimal):
                            with localcontext() as context:
                                context.prec = max(len(value.as_tuple().digits), 1)
                                normalized.append("0" if value.is_zero() else str(value.normalize()))
                        elif isinstance(value, datetime):
                            normalized.append(value.astimezone(UTC).isoformat())
                        elif isinstance(value, date):
                            normalized.append(value.isoformat())
                        else:
                            normalized.append(value)
                    serialized = json.dumps(normalized, ensure_ascii=False, separators=(",", ":"))
                    try:
                        db.execute("INSERT INTO keys VALUES (?)", [serialized])
                    except sqlite3.IntegrityError:
                        valid = False
        return valid
    finally:
        path.unlink(missing_ok=True)
