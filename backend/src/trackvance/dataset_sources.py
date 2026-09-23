"""Read-only database acquisition adapters. No business rule imports or delivery API.

Only server-discovered identifiers may be selected. Queries are owned by the
adapter, bounded at the server and parameterized/identifier-quoted. SQL Server's
read-only application intent is advisory: use a SELECT-only database principal.
"""

from __future__ import annotations

import base64
import ipaddress
import os
import re
import threading
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any, ClassVar

import polars as pl
import psycopg
import pymssql
from psycopg import sql

from .config import MAX_ROWS
from .dataset_readers import (
    INSPECTION_ROWS,
    MAX_CELL_TEXT_BYTES,
    MAX_COLUMNS,
    DatasetReadResult,
    ReaderOptions,
    _cell_text,
)


def configured_snapshot_limit(raw: str | None = None) -> int:
    value = raw if raw is not None else os.getenv(
        "TRACKVANCE_MAX_SNAPSHOT_BYTES", str(64 * 1024 * 1024)
    )
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        raise RuntimeError("TRACKVANCE_MAX_SNAPSHOT_BYTES debe ser un entero positivo.") from None
    if parsed <= 0:
        raise RuntimeError("TRACKVANCE_MAX_SNAPSHOT_BYTES debe ser un entero positivo.")
    return parsed


MAX_SNAPSHOT_BYTES = configured_snapshot_limit()
MAX_DISCOVERY_OBJECTS = 5000
_mssql_lock = threading.RLock()  # FreeTDS timeouts are process-wide.


class SourceError(Exception):
    def __init__(self, code: str, message: str):
        self.code, self.message = code, message
        super().__init__(message)


@dataclass(frozen=True)
class ConnectionSettings:
    source_type: str
    host: str
    port: int
    database: str
    username: str
    password: str = field(repr=False)
    options: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.source_type not in {"POSTGRESQL", "SQLSERVER"}:
            raise SourceError("UNSUPPORTED_SOURCE", "Tipo de conexión no soportado.")
        try:
            ipaddress.ip_address(self.host)
            valid_host = True
        except ValueError:
            valid_host = bool(re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9_.-]{0,251}[A-Za-z0-9])?", self.host))
        if not valid_host or isinstance(self.port, bool) or not 1 <= self.port <= 65535:
            raise SourceError("INVALID_CONNECTION", "Revisa el host y el puerto de la conexión.")
        for value in (self.database, self.username):
            if not value or len(value) > 128 or any(ord(c) < 32 for c in value):
                raise SourceError("INVALID_CONNECTION", "La base de datos o el usuario no son válidos.")
        if not self.password or len(self.password.encode()) > 8192:
            raise SourceError("INVALID_CONNECTION", "Indica una contraseña válida.")
        allowed = {"connect_timeout", "query_timeout"} | (
            {"sslmode"} if self.source_type == "POSTGRESQL" else {"encryption"}
        )
        if set(self.options) - allowed:
            raise SourceError("INVALID_SOURCE_OPTIONS", "Parámetros de conexión no soportados.")
        for name, maximum in (("connect_timeout", 15), ("query_timeout", 60)):
            value = self.options.get(name, 5 if name == "connect_timeout" else 30)
            if type(value) is not int or not 1 <= value <= maximum:
                raise SourceError("INVALID_SOURCE_OPTIONS", "Los tiempos de espera están fuera del rango permitido.")
        tls_key = "sslmode" if self.source_type == "POSTGRESQL" else "encryption"
        tls_choices = {"disable", "require", "verify-ca", "verify-full"} if tls_key == "sslmode" else {"off", "require"}
        tls_mode = self.options.get(tls_key, "require")
        if not isinstance(tls_mode, str) or tls_mode not in tls_choices:
            raise SourceError("INVALID_SOURCE_OPTIONS", "Modo de cifrado de transporte no válido.")


def _safe_error(exc: Exception) -> SourceError:
    # Inspect driver codes only; do not stringify errors/DSNs or chain them.
    state = getattr(exc, "sqlstate", None)
    number = exc.args[0] if exc.args and isinstance(exc.args[0], int) else None
    if state in {"28P01", "28000"} or number == 18456:
        return SourceError("SOURCE_AUTH_FAILED", "No se pudo autenticar con las credenciales indicadas.")
    if state == "42501" or number in {229, 230, 916}:
        return SourceError("SOURCE_PERMISSION_DENIED", "La cuenta no tiene permisos de lectura sobre la fuente.")
    if state == "57014" or number in {1222, 20003}:
        return SourceError("SOURCE_TIMEOUT", "La consulta excedió el tiempo de espera.")
    return SourceError("SOURCE_UNAVAILABLE", "No fue posible acceder a la fuente. Revisa disponibilidad, host, puerto y permisos.")


def _logical(native: str) -> tuple[str, str]:
    # Preserve the timezone qualifier while removing precision/scale parameters.
    # Trackvance TIMESTAMP has an explicit-offset contract; treating a naïve
    # database datetime as TIMESTAMP would create a DatasetVersion that Delivery
    # cannot publish without inventing a timezone.
    raw = native.casefold().strip()
    datetimeoffset = re.fullmatch(r"datetimeoffset\s*\(\s*(\d+)\s*\)", raw)
    if datetimeoffset and int(datetimeoffset.group(1)) > 6:
        # Delivery's exact TIMESTAMP grammar is capped at microseconds.
        return "STRING", "String"
    value = re.sub(r"\s*\([^)]*\)", "", raw).strip()
    if value in {"int", "int2", "int4", "int8", "integer", "bigint", "smallint", "tinyint"}:
        return "INT64", "Int64"
    if value in {"decimal", "numeric", "money", "smallmoney", "real", "float", "float4", "float8", "double precision"}:
        return "DECIMAL", "Decimal"
    if value in {"bit", "boolean", "bool"}:
        return "BOOLEAN", "Boolean"
    if value == "date":
        return "DATE", "Date"
    if value in {"datetimeoffset", "timestamptz", "timestamp with time zone"}:
        return "TIMESTAMP", "Datetime"
    return "STRING", "String"


def _column(name: str, native: str, nullable: bool) -> dict[str, Any]:
    logical, _ = _logical(native)
    if name.lower() == "id" or name.lower().endswith("_id"):
        logical = "STRING"
    return {"name": name, "native_type": native, "logical_type": logical,
            "nullable": nullable, "numeric": logical in {"INT64", "DECIMAL"}}


class DatabaseDatasetSource:
    source_kind: ClassVar[str]
    format_label: ClassVar[str]

    def __init__(self, settings: ConnectionSettings, schema_name: str | None = None,
                 object_name: str | None = None):
        self.settings = settings
        self.schema_name, self.object_name = schema_name, object_name

    @contextmanager
    def _connection(self) -> Iterator[Any]:
        raise NotImplementedError
        yield

    def _schemas(self, connection: Any) -> list[str]:
        raise NotImplementedError

    def _objects(self, connection: Any, schema_name: str) -> list[dict[str, str]]:
        raise NotImplementedError

    def _columns(self, connection: Any, schema_name: str, object_name: str) -> list[dict[str, Any]]:
        raise NotImplementedError

    def _select(self, connection: Any, schema_name: str, object_name: str,
                columns: list[dict[str, Any]], limit: int) -> Any:
        raise NotImplementedError

    def test(self) -> None:
        with self._connection() as connection, connection.cursor() as cursor:
            cursor.execute("SELECT 1")
            cursor.fetchone()

    def schemas(self) -> list[str]:
        with self._connection() as connection:
            return self._schemas(connection)

    def objects(self, schema_name: str) -> list[dict[str, str]]:
        with self._connection() as connection:
            return self._objects(connection, schema_name)

    def _checked_columns(self, connection: Any, schema_name: str, object_name: str) -> list[dict[str, Any]]:
        if not any(item["name"] == object_name for item in self._objects(connection, schema_name)):
            raise SourceError("SOURCE_OBJECT_UNAVAILABLE", "La tabla o vista no existe o no permite lectura.")
        columns = self._columns(connection, schema_name, object_name)
        if not columns or len(columns) > MAX_COLUMNS or any(
            not c["name"].strip() or c["name"].startswith("__tv_") for c in columns
        ):
            raise SourceError("SOURCE_SCHEMA_UNSUPPORTED", "El esquema debe contener de 1 a 100 columnas con nombres válidos.")
        return columns

    def columns(self, schema_name: str, object_name: str) -> list[dict[str, Any]]:
        with self._connection() as connection:
            return self._checked_columns(connection, schema_name, object_name)

    def read(self, options: ReaderOptions | Mapping[str, Any] | None = None,
             *, inspect: bool = False) -> DatasetReadResult:
        raw = dict(options) if isinstance(options, Mapping) else {}
        if options is not None and not isinstance(options, Mapping):
            raise SourceError("INVALID_SOURCE_OPTIONS", "La fuente de base de datos no admite opciones de archivo.")
        if set(raw) - {"limit"} or (raw and not inspect):
            raise SourceError("INVALID_SOURCE_OPTIONS", "Opciones de lectura no soportadas.")
        limit = raw.get("limit", INSPECTION_ROWS) if inspect else MAX_ROWS + 1
        if type(limit) is not int or limit < 1 or (inspect and limit > INSPECTION_ROWS):
            raise SourceError("INVALID_SOURCE_OPTIONS", "La vista previa admite entre 1 y 100 registros.")
        if not self.schema_name or not self.object_name:
            raise SourceError("SOURCE_SELECTION_REQUIRED", "Selecciona un esquema y una tabla o vista.")
        with self._connection() as connection:
            columns = self._checked_columns(connection, self.schema_name, self.object_name)
            cursor = self._select(connection, self.schema_name, self.object_name, columns, limit)
            rows: list[list[str | None]] = []
            size = 0
            try:
                while batch := cursor.fetchmany(min(100, limit)):
                    for row in batch:
                        normalized: list[str | None] = []
                        for value in row:
                            rendered = (base64.b64encode(bytes(value)).decode("ascii")
                                        if isinstance(value, (bytes, bytearray, memoryview)) else _cell_text(value))
                            cell_size = len(rendered.encode()) if rendered is not None else 0
                            if cell_size > MAX_CELL_TEXT_BYTES:
                                raise SourceError("SOURCE_SIZE_LIMIT", "Una celda supera el límite local de 64 KiB.")
                            size += cell_size
                            normalized.append(rendered)
                        rows.append(normalized)
                        if size > MAX_SNAPSHOT_BYTES or (not inspect and len(rows) > MAX_ROWS):
                            raise SourceError("SOURCE_SIZE_LIMIT", "La fuente supera el límite local del snapshot; no se importaron datos parciales.")
            finally:
                cursor.close()
        names = [c["name"] for c in columns]
        frame = pl.DataFrame(rows, schema={name: pl.String for name in names}, orient="row")
        return DatasetReadResult(
            frame=frame, source_format=self.source_kind, format_label=self.format_label,
            media_type="application/vnd.apache.parquet", row_numbering="SNAPSHOT_ROW",
            native_schema={c["name"]: _logical(c["native_type"])[1] for c in columns},
            row_count=None if inspect else frame.height,
            metadata={"source_native_schema": columns, "schema_name": self.schema_name,
                      "object_name": self.object_name, "snapshot_policy": "FULL_BOUNDED_V1",
                      "ordering": "DATABASE_UNSPECIFIED", "binary_encoding": "BASE64"},
        )


class PostgreSQLDatasetSource(DatabaseDatasetSource):
    source_kind = "POSTGRESQL"
    format_label = "PostgreSQL"

    @contextmanager
    def _connection(self) -> Iterator[Any]:
        s = self.settings
        try:
            with psycopg.connect(host=s.host, port=s.port, dbname=s.database, user=s.username,
                                 password=s.password, connect_timeout=s.options.get("connect_timeout", 5),
                                 sslmode=s.options.get("sslmode", "require"),
                                 application_name="Trackvance reader",
                                 options=f"-c statement_timeout={s.options.get('query_timeout', 30) * 1000}") as connection:
                connection.read_only = True
                yield connection
        except psycopg.Error as exc:
            raise _safe_error(exc) from None

    def _schemas(self, connection: Any) -> list[str]:
        with connection.cursor() as cursor:
            cursor.execute("""SELECT nspname FROM pg_namespace
                WHERE left(nspname,3) <> 'pg_' AND nspname <> 'information_schema'
                  AND has_schema_privilege(oid, 'USAGE') ORDER BY nspname LIMIT %s""", (MAX_DISCOVERY_OBJECTS,))
            return [r[0] for r in cursor.fetchall()]

    def _objects(self, connection: Any, schema_name: str) -> list[dict[str, str]]:
        with connection.cursor() as cursor:
            cursor.execute("""SELECT c.relname, CASE WHEN c.relkind IN ('v','m') THEN 'VIEW' ELSE 'TABLE' END
                FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
                WHERE n.nspname=%s AND c.relkind IN ('r','p','v','m')
                  AND has_schema_privilege(n.oid, 'USAGE') AND has_table_privilege(c.oid, 'SELECT')
                ORDER BY c.relname LIMIT %s""", (schema_name, MAX_DISCOVERY_OBJECTS))
            return [{"name": r[0], "kind": r[1]} for r in cursor.fetchall()]

    def _columns(self, connection: Any, schema_name: str, object_name: str) -> list[dict[str, Any]]:
        with connection.cursor() as cursor:
            cursor.execute("""SELECT a.attname, format_type(a.atttypid,a.atttypmod), NOT a.attnotnull
                FROM pg_attribute a JOIN pg_class c ON c.oid=a.attrelid
                JOIN pg_namespace n ON n.oid=c.relnamespace
                WHERE n.nspname=%s AND c.relname=%s AND a.attnum>0 AND NOT a.attisdropped
                ORDER BY a.attnum""", (schema_name, object_name))
            return [_column(*row) for row in cursor.fetchall()]

    def _select(self, connection: Any, schema_name: str, object_name: str,
                columns: list[dict[str, Any]], limit: int) -> Any:
        cursor = connection.cursor(name="trackvance_snapshot")
        cursor.execute(sql.SQL("SELECT {} FROM {}.{} LIMIT %s").format(
            sql.SQL(", ").join(sql.Identifier(c["name"]) for c in columns),
            sql.Identifier(schema_name), sql.Identifier(object_name)), (limit,))
        return cursor


def _quote_mssql(identifier: str) -> str:
    return "[" + identifier.replace("]", "]]") + "]"


class SQLServerDatasetSource(DatabaseDatasetSource):
    source_kind = "SQLSERVER"
    format_label = "SQL Server"

    @contextmanager
    def _connection(self) -> Iterator[Any]:
        s = self.settings
        with _mssql_lock:
            try:
                connection = pymssql.connect(
                    server=s.host, port=str(s.port), database=s.database, user=s.username,
                    password=s.password, login_timeout=s.options.get("connect_timeout", 5),
                    timeout=s.options.get("query_timeout", 30), charset="UTF-8", tds_version="7.4",
                    appname="Trackvance reader", read_only=True,
                    encryption=s.options.get("encryption", "require"), use_datetime2=True,
                )
                try:
                    yield connection
                finally:
                    connection.close()  # Rollback any open transaction, never write/commit.
            except pymssql.Error as exc:
                raise _safe_error(exc) from None

    def _schemas(self, connection: Any) -> list[str]:
        with connection.cursor() as cursor:
            cursor.execute("""SELECT DISTINCT TOP (5000) s.name FROM sys.schemas s
                JOIN sys.objects o ON o.schema_id=s.schema_id
                WHERE o.type IN ('U','V') AND o.is_ms_shipped=0
                  AND HAS_PERMS_BY_NAME(QUOTENAME(s.name)+'.'+QUOTENAME(o.name), 'OBJECT', 'SELECT')=1
                ORDER BY s.name""")
            return [r[0] for r in cursor.fetchall()]

    def _objects(self, connection: Any, schema_name: str) -> list[dict[str, str]]:
        with connection.cursor() as cursor:
            cursor.execute("""SELECT TOP (5000) o.name, CASE WHEN o.type='V' THEN 'VIEW' ELSE 'TABLE' END
                FROM sys.objects o JOIN sys.schemas s ON s.schema_id=o.schema_id
                WHERE s.name=%s AND o.type IN ('U','V') AND o.is_ms_shipped=0
                  AND HAS_PERMS_BY_NAME(QUOTENAME(s.name)+'.'+QUOTENAME(o.name), 'OBJECT', 'SELECT')=1
                ORDER BY o.name""", (schema_name,))
            return [{"name": r[0], "kind": r[1]} for r in cursor.fetchall()]

    def _columns(self, connection: Any, schema_name: str, object_name: str) -> list[dict[str, Any]]:
        with connection.cursor() as cursor:
            cursor.execute("""SELECT c.name, t.name, c.is_nullable, c.precision, c.scale, c.max_length FROM sys.columns c
                JOIN sys.types t ON t.user_type_id=c.user_type_id
                JOIN sys.objects o ON o.object_id=c.object_id JOIN sys.schemas s ON s.schema_id=o.schema_id
                WHERE s.name=%s AND o.name=%s ORDER BY c.column_id""", (schema_name, object_name))
            columns = []
            for row in cursor.fetchall():
                # SQL Server timestamp is binary rowversion, never a datetime.
                native = "rowversion" if row[1] == "timestamp" else row[1]
                if native == "datetimeoffset":
                    native = f"datetimeoffset({row[4]})"
                columns.append({**_column(row[0], native, bool(row[2])),
                                "precision": row[3], "scale": row[4],
                                "max_length_bytes": row[5]})
            return columns

    def _select(self, connection: Any, schema_name: str, object_name: str,
                columns: list[dict[str, Any]], limit: int) -> Any:
        selected = []
        for column in columns:
            name = _quote_mssql(column["name"])
            native = re.sub(r"\s*\([^)]*\)", "", column["native_type"].casefold()).strip()
            # Ask SQL Server for canonical text so datetime2(7)/datetimeoffset(7)
            # never pass through Python's microsecond-limited datetime object.
            if native == "datetimeoffset":
                selected.append(f"CONVERT(nvarchar(50), {name}, 127) AS {name}")
            elif native in {"datetime", "datetime2", "smalldatetime"}:
                selected.append(f"CONVERT(nvarchar(50), {name}, 126) AS {name}")
            else:
                selected.append(name)
        cursor = connection.cursor()
        # limit is a range-checked integer; identifiers are metadata-validated and quoted.
        cursor.execute(f"SELECT TOP ({limit}) {', '.join(selected)} FROM "
                       f"{_quote_mssql(schema_name)}.{_quote_mssql(object_name)}")
        return cursor


class DatasetSourceRegistry:
    def __init__(self) -> None:
        self._adapters: dict[str, type[DatabaseDatasetSource]] = {}

    def register(self, source_type: str, adapter: type[DatabaseDatasetSource]) -> None:
        self._adapters[source_type] = adapter

    def create(self, settings: ConnectionSettings, schema_name: str | None = None,
               object_name: str | None = None) -> DatabaseDatasetSource:
        adapter = self._adapters.get(settings.source_type)
        if adapter is None:
            raise SourceError("UNSUPPORTED_SOURCE", "No existe un adaptador para esta fuente.")
        return adapter(settings, schema_name, object_name)


source_registry = DatasetSourceRegistry()
source_registry.register("POSTGRESQL", PostgreSQLDatasetSource)
source_registry.register("SQLSERVER", SQLServerDatasetSource)
