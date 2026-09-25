"""Write-only database delivery adapters behind the independent DataSink port.

The adapters own every SQL statement. Callers provide structured identifiers,
column mappings and values from an immutable DatasetVersion; arbitrary SQL is
never accepted. DatasetSource deliberately remains a read-only acquisition port.
"""

from __future__ import annotations

import ipaddress
import re
import threading
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from decimal import Decimal, InvalidOperation
from typing import Any, ClassVar, Protocol, runtime_checkable

import psycopg
import pymssql
from psycopg import sql

SUPPORTED_SINKS = frozenset({"POSTGRESQL", "SQLSERVER"})
LOGICAL_TYPES = frozenset({"STRING", "INT64", "DECIMAL", "DATE", "TIMESTAMP", "BOOLEAN"})
MAX_IDENTIFIER_LENGTH = 128
POSTGRESQL_MAX_IDENTIFIER_BYTES = 63
SQLSERVER_UNICODE_COLLATION = "Latin1_General_100_CI_AS_SC"
DELIVERY_TIMESTAMP_PATTERN = (
    r"^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}"
    r"(?:\.[0-9]{1,6})?(?:Z|[+-](?:[01][0-9]|2[0-3]):[0-5][0-9])$"
)
_mssql_write_lock = threading.RLock()


class DeliveryError(Exception):
    """Sanitized error safe for API, audit and persisted attempt evidence."""

    def __init__(self, code: str, message: str, *, ambiguous: bool = False):
        self.code = code
        self.message = message
        self.ambiguous = ambiguous
        super().__init__(message)


@dataclass(frozen=True)
class DestinationSettings:
    sink_type: str
    host: str
    port: int
    database: str
    username: str
    password: str = field(repr=False)
    options: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.sink_type not in SUPPORTED_SINKS:
            raise DeliveryError("UNSUPPORTED_SINK", "Tipo de destino no soportado.")
        try:
            ipaddress.ip_address(self.host)
            valid_host = True
        except ValueError:
            valid_host = bool(
                re.fullmatch(
                    r"[A-Za-z0-9](?:[A-Za-z0-9_.-]{0,251}[A-Za-z0-9])?", self.host
                )
            )
        if not valid_host or isinstance(self.port, bool) or not 1 <= self.port <= 65535:
            raise DeliveryError("INVALID_DESTINATION", "Revisa el host y el puerto del destino.")
        for value in (self.database, self.username):
            if not value or len(value) > 128 or any(ord(character) < 32 for character in value):
                raise DeliveryError(
                    "INVALID_DESTINATION", "La base de datos o el usuario no son válidos."
                )
        if not self.password or len(self.password.encode()) > 8192:
            raise DeliveryError("INVALID_DESTINATION", "Indica una contraseña válida.")
        allowed = {"connect_timeout", "query_timeout"} | (
            {"sslmode"} if self.sink_type == "POSTGRESQL" else {"encryption"}
        )
        if set(self.options) - allowed:
            raise DeliveryError(
                "INVALID_DESTINATION_OPTIONS", "Parámetros de destino no soportados."
            )
        for name, maximum in (("connect_timeout", 15), ("query_timeout", 300)):
            value = self.options.get(name, 5 if name == "connect_timeout" else 60)
            if type(value) is not int or not 1 <= value <= maximum:
                raise DeliveryError(
                    "INVALID_DESTINATION_OPTIONS",
                    "Los tiempos de espera están fuera del rango permitido.",
                )
        tls_key = "sslmode" if self.sink_type == "POSTGRESQL" else "encryption"
        choices = (
            {"disable", "require", "verify-ca", "verify-full"}
            if tls_key == "sslmode"
            else {"off", "require"}
        )
        tls_value = self.options.get(tls_key, "require")
        if not isinstance(tls_value, str) or tls_value not in choices:
            raise DeliveryError(
                "INVALID_DESTINATION_OPTIONS", "Modo de cifrado de transporte no válido."
            )


@dataclass(frozen=True)
class DeliveryResult:
    """Confirmed-operation metrics, never a post-trigger physical row inventory.

    ``rows_written`` preserves the 0.5.0 contract: submitted source rows, even
    when the engine suppresses DML. Insert/update counters describe top-level
    DML actions reported by the adapter, or None if not reliably available.
    """

    rows_attempted: int
    rows_written: int
    rows_inserted: int | None
    rows_updated: int | None
    bytes_sent: int
    remote_reference: str | None = None


@dataclass(frozen=True)
class PreparedDelivery:
    """Driver-ready payload whose local validation finished before STARTED."""

    schema_name: str
    table_name: str
    columns: list[dict[str, Any]]
    rows: tuple[tuple[Any, ...], ...]
    target: dict[str, Any]
    strategy: str
    upsert_keys: list[str]
    bytes_sent: int


def validate_identifier(value: str) -> str:
    """Accept Unicode database identifiers while excluding controls and separators."""
    if (
        not isinstance(value, str)
        or not value
        or len(value) > MAX_IDENTIFIER_LENGTH
        or value != value.strip()
        or any(ord(character) < 32 or character == "\x7f" for character in value)
        or "\x00" in value
    ):
        raise DeliveryError("INVALID_IDENTIFIER", "El identificador SQL no es válido.")
    return value


def validate_postgresql_identifier(value: str) -> str:
    """Reject names PostgreSQL would silently truncate at ``NAMEDATALEN - 1``."""
    checked = validate_identifier(value)
    if len(checked.encode("utf-8")) > POSTGRESQL_MAX_IDENTIFIER_BYTES:
        raise DeliveryError(
            "INVALID_IDENTIFIER",
            "El identificador SQL supera el límite seguro de PostgreSQL.",
        )
    return checked


def quote_sqlserver_identifier(value: str) -> str:
    return "[" + validate_identifier(value).replace("]", "]]") + "]"


def _base_native_type(value: str) -> str:
    return re.sub(r"\s*\([^)]*\)", "", value.casefold()).strip()


def logical_type_for_native(sink_type: str, native_type: str) -> str:
    base = _base_native_type(native_type)
    integers = {"smallint", "integer", "int", "bigint", "int2", "int4", "int8", "tinyint"}
    decimals = {
        "decimal",
        "numeric",
        "money",
        "smallmoney",
        "real",
        "float",
        "float4",
        "float8",
        "double precision",
    }
    booleans = {"boolean", "bool", "bit"}
    timestamps = {
        "timestamp",
        "timestamp without time zone",
        "timestamp with time zone",
        "timestamptz",
        "datetime",
        "datetime2",
        "smalldatetime",
        "datetimeoffset",
    }
    if base in integers:
        return "INT64"
    if base in decimals:
        return "DECIMAL"
    if base in booleans:
        return "BOOLEAN"
    if base == "date":
        return "DATE"
    if base in timestamps and not (sink_type == "SQLSERVER" and base == "timestamp"):
        return "TIMESTAMP"
    if string_storage_is_exact(sink_type, native_type):
        return "STRING"
    return "UNSUPPORTED"


def string_storage_is_exact(sink_type: str, native_type: str) -> bool:
    """Allow only variable-width, Unicode-safe native string families."""

    base = _base_native_type(native_type)
    if sink_type == "POSTGRESQL":
        return base in {"text", "varchar", "character varying"}
    if sink_type == "SQLSERVER":
        return base == "nvarchar"
    return False


def sqlserver_collation_supports_supplementary(collation: Any) -> bool:
    if not isinstance(collation, str):
        return False
    normalized = collation.upper()
    return "_SC" in normalized or "_UTF8" in normalized


def utf16_code_units(value: str) -> int:
    """Measure the public STRING length contract used by both destinations."""

    try:
        return len(value.encode("utf-16-le", errors="strict")) // 2
    except UnicodeError:
        raise DeliveryError(
            "VALUE_TYPE_MISMATCH",
            "El texto contiene una secuencia Unicode no válida.",
        ) from None


def decimal_capacity_for_native(
    sink_type: str,
    native_type: str,
    precision: Any,
    scale: Any,
) -> tuple[int, int] | None:
    """Return exact decimal capacity; approximate IEEE types are never accepted."""

    base = _base_native_type(native_type)
    if sink_type == "POSTGRESQL":
        if base in {"numeric", "decimal"}:
            if precision is None and scale is None:
                # An unconstrained PostgreSQL NUMERIC safely exceeds every
                # Trackvance mapping (whose precision and scale are <= 38).
                return 76, 38
        elif base == "money":
            # MONEY is a signed 64-bit scaled integer. Its fractional digits
            # follow lc_monetary, so accept it only when metadata states them.
            if type(scale) is int and 0 <= scale <= 18:
                return 19, scale
            return None
        else:
            return None
    elif sink_type == "SQLSERVER":
        if base in {"numeric", "decimal"}:
            pass
        elif base == "money":
            return 19, 4
        elif base == "smallmoney":
            return 10, 4
        else:
            return None
    else:
        return None
    if type(precision) is not int or type(scale) is not int:
        return None
    if not 1 <= precision <= 38 or not 0 <= scale <= precision:
        return None
    return precision, scale


def timestamp_policy_for_native(
    sink_type: str,
    native_type: str,
    datetime_precision: Any,
) -> tuple[int, bool] | None:
    """Return fractional-second precision and whether an offset is required.

    Native families that round at irregular intervals are rejected because
    Delivery promises not to transform values on the way to the destination.
    """

    base = _base_native_type(native_type)
    if type(datetime_precision) is not int:
        return None
    if sink_type == "POSTGRESQL":
        if not 0 <= datetime_precision <= 6:
            return None
        if base in {"timestamp with time zone", "timestamptz"}:
            return datetime_precision, True
        return None
    if sink_type == "SQLSERVER":
        if not 0 <= datetime_precision <= 7:
            return None
        if base == "datetimeoffset":
            return datetime_precision, True
        # datetime2 drops offsets, while datetime and smalldatetime also round
        # values. None provide the offset-aware TIMESTAMP contract.
        return None
    return None


def integer_bounds_for_native(
    sink_type: str, native_type: str
) -> tuple[int, int] | None:
    """Return the real integral range for supported destination-native types."""
    base = _base_native_type(native_type)
    if sink_type == "POSTGRESQL":
        bits = {"smallint": 16, "int2": 16, "integer": 32, "int": 32, "int4": 32,
                "bigint": 64, "int8": 64}.get(base)
        return (-(2 ** (bits - 1)), 2 ** (bits - 1) - 1) if bits else None
    if sink_type == "SQLSERVER":
        if base == "tinyint":
            return 0, 255
        bits = {"smallint": 16, "integer": 32, "int": 32, "bigint": 64}.get(base)
        return (-(2 ** (bits - 1)), 2 ** (bits - 1) - 1) if bits else None
    return None


def decimal_parameters(column: dict[str, Any]) -> tuple[int, int]:
    """Resolve omitted Pydantic fields to the public DECIMAL defaults."""
    precision = column.get("precision")
    scale = column.get("scale")
    precision = 38 if precision is None else precision
    scale = 10 if scale is None else scale
    if (
        type(precision) is not int
        or type(scale) is not int
        or not 1 <= precision <= 38
        or not 0 <= scale <= precision
    ):
        raise DeliveryError(
            "INVALID_TYPE_PARAMETERS", "La precisión o escala DECIMAL no es válida."
        )
    return precision, scale


def technical_type(sink_type: str, column: dict[str, Any]) -> str:
    target_type = str(column.get("target_type", "")).upper()
    if target_type not in LOGICAL_TYPES:
        raise DeliveryError("UNSUPPORTED_TARGET_TYPE", "El tipo técnico seleccionado no es válido.")
    if target_type == "STRING":
        length = column.get("length")
        if length is not None and (type(length) is not int or not 1 <= length <= 1_000_000):
            raise DeliveryError("INVALID_TYPE_PARAMETERS", "La longitud configurada no es válida.")
        if sink_type == "POSTGRESQL":
            return f"VARCHAR({length})" if length else "TEXT"
        return f"NVARCHAR({length})" if length and length <= 4000 else "NVARCHAR(MAX)"
    if target_type == "INT64":
        return "BIGINT"
    if target_type == "DECIMAL":
        precision, scale = decimal_parameters(column)
        return f"{'NUMERIC' if sink_type == 'POSTGRESQL' else 'DECIMAL'}({precision},{scale})"
    if target_type == "DATE":
        return "DATE"
    if target_type == "TIMESTAMP":
        return "TIMESTAMPTZ(6)" if sink_type == "POSTGRESQL" else "DATETIMEOFFSET(6)"
    return "BOOLEAN" if sink_type == "POSTGRESQL" else "BIT"


def convert_value(value: Any, column: dict[str, Any]) -> Any:
    if value is None:
        if not column.get("nullable", True):
            raise DeliveryError(
                "NULLABILITY_MISMATCH",
                f"La columna {column['source_name']} contiene null y fue declarada no nula.",
            )
        return None
    target_type = str(column["target_type"]).upper()
    text = str(value)
    try:
        if target_type == "STRING":
            length = column.get("length")
            units = utf16_code_units(text)
            if "\x00" in text:
                raise DeliveryError(
                    "VALUE_TYPE_MISMATCH",
                    f"La columna {column['source_name']} contiene un carácter no almacenable.",
                )
            if length and units > length:
                raise DeliveryError(
                    "STRING_LENGTH_EXCEEDED",
                    f"La columna {column['source_name']} supera la longitud configurada.",
                )
            return text
        if target_type == "INT64":
            if not re.fullmatch(r"[+-]?\d+", text):
                raise ValueError("integer")
            number = int(text)
            if not -(2**63) <= number < 2**63:
                raise ValueError("range")
            return number
        if target_type == "DECIMAL":
            decimal_value = Decimal(text)
            if not decimal_value.is_finite():
                raise InvalidOperation
            precision, scale = decimal_parameters(column)
            sign, digits, exponent = decimal_value.as_tuple()
            del sign
            exponent_value = int(exponent)
            fractional = max(-exponent_value, 0)
            integer = max(len(digits) + exponent_value, 0)
            if fractional > scale or integer + fractional > precision:
                raise DeliveryError(
                    "DECIMAL_PRECISION_EXCEEDED",
                    f"La columna {column['source_name']} excede precisión/escala.",
                )
            return decimal_value
        if target_type == "DATE":
            return date.fromisoformat(text)
        if target_type == "TIMESTAMP":
            if isinstance(value, datetime):
                parsed = value
            elif isinstance(value, str) and re.fullmatch(
                DELIVERY_TIMESTAMP_PATTERN, value
            ):
                parsed = datetime.fromisoformat(value)
            else:
                raise ValueError("timestamp format")
            offset = parsed.utcoffset() if parsed.tzinfo is not None else None
            if offset is None:
                raise ValueError("timestamp offset")
            offset_seconds = offset.total_seconds()
            if offset_seconds % 60 or abs(offset_seconds) > 14 * 60 * 60:
                raise ValueError("timestamp offset range")
            parsed.astimezone(UTC)  # Also validates the SQL Server UTC date range.
            return parsed
        if target_type == "BOOLEAN":
            normalized = text.casefold()
            if normalized in {"true", "1"}:
                return True
            if normalized in {"false", "0"}:
                return False
            raise ValueError("boolean")
    except (ValueError, TypeError, OverflowError, InvalidOperation):
        raise DeliveryError(
            "VALUE_TYPE_MISMATCH",
            f"La columna {column['source_name']} contiene un valor incompatible con {target_type}.",
        ) from None
    raise DeliveryError("UNSUPPORTED_TARGET_TYPE", "El tipo técnico seleccionado no es válido.")


def prepare_rows(records: Sequence[dict[str, Any]], columns: list[dict[str, Any]]) -> list[tuple]:
    rows: list[tuple] = []
    for record in records:
        rows.append(tuple(convert_value(record.get(column["source_name"]), column) for column in columns))
    return rows


def serialized_size(rows: Sequence[tuple]) -> int:
    return sum(
        len(str(value).encode("utf-8"))
        for row in rows
        for value in row
        if value is not None
    )


def _reported_rowcount(cursor: Any) -> int | None:
    count = cursor.rowcount
    return count if type(count) is int and count >= 0 else None


def _safe_driver_error(exc: Exception, *, ambiguous: bool = False) -> DeliveryError:
    state = getattr(exc, "sqlstate", None)
    number = exc.args[0] if exc.args and isinstance(exc.args[0], int) else None
    if state in {"28P01", "28000"} or number == 18456:
        return DeliveryError(
            "DESTINATION_AUTH_FAILED", "No se pudo autenticar con las credenciales indicadas."
        )
    if state == "42501" or number in {229, 230, 262, 2760, 916}:
        return DeliveryError(
            "DESTINATION_PERMISSION_DENIED",
            "La cuenta no tiene los permisos requeridos sobre el destino.",
        )
    if (isinstance(state, str) and state.startswith("23")) or number in {
        547,
        2601,
        2627,
    }:
        return DeliveryError(
            "DESTINATION_CONSTRAINT_VIOLATION",
            "El destino rechazó la transacción por una restricción de datos.",
        )
    if state in {"57014", "40001", "40P01"} or number in {1205, 1222}:
        # These replies prove that the server rejected or rolled back the
        # transaction. Even when they happen during commit, the outcome is not
        # ambiguous and must remain eligible for an intentional new Run.
        return DeliveryError(
            "DESTINATION_TIMEOUT",
            "El destino canceló o revirtió la transacción; no confirmó ningún cambio.",
        )
    if number == 20003:
        return DeliveryError(
            "DESTINATION_COMMIT_UNKNOWN" if ambiguous else "DESTINATION_TIMEOUT",
            (
                "Trackvance no pudo confirmar el resultado de la transacción remota."
                if ambiguous
                else "La operación remota agotó el tiempo disponible."
            ),
            ambiguous=ambiguous,
        )
    return DeliveryError(
        "DESTINATION_COMMIT_UNKNOWN" if ambiguous else "DESTINATION_UNAVAILABLE",
        (
            "Trackvance no pudo confirmar el resultado de la transacción remota."
            if ambiguous
            else "No fue posible operar sobre el destino. Revisa disponibilidad y permisos."
        ),
        ambiguous=ambiguous,
    )


@runtime_checkable
class DataSink(Protocol):
    sink_type: str

    def test(self) -> None: ...

    def schemas(self) -> list[str]: ...

    def tables(self, schema_name: str) -> list[str]: ...

    def table_metadata(self, schema_name: str, table_name: str) -> dict[str, Any]: ...

    def permissions(self, target: dict[str, Any], strategy: str) -> dict[str, bool]: ...

    def prepare(
        self,
        records: Sequence[dict[str, Any]],
        columns: list[dict[str, Any]],
        target: dict[str, Any],
        strategy: str,
        upsert_keys: list[str],
    ) -> PreparedDelivery: ...

    def deliver_prepared(self, payload: PreparedDelivery) -> DeliveryResult: ...

    def deliver(
        self,
        records: Sequence[dict[str, Any]],
        columns: list[dict[str, Any]],
        target: dict[str, Any],
        strategy: str,
        upsert_keys: list[str],
    ) -> DeliveryResult: ...


class DatabaseDataSink:
    sink_type: ClassVar[str]

    def __init__(self, settings: DestinationSettings):
        self.settings = settings

    @contextmanager
    def _connection(self) -> Iterator[Any]:
        raise NotImplementedError
        yield

    def test(self) -> None:
        with self._connection() as connection:
            try:
                with connection.cursor() as cursor:
                    cursor.execute("SELECT 1")
                    cursor.fetchone()
                connection.rollback()
            except (psycopg.Error, pymssql.Error) as exc:
                raise _safe_driver_error(exc) from None

    def schemas(self) -> list[str]:
        raise NotImplementedError

    def tables(self, schema_name: str) -> list[str]:
        raise NotImplementedError

    def table_metadata(self, schema_name: str, table_name: str) -> dict[str, Any]:
        raise NotImplementedError

    def permissions(self, target: dict[str, Any], strategy: str) -> dict[str, bool]:
        raise NotImplementedError

    def prepare(
        self,
        records: Sequence[dict[str, Any]],
        columns: list[dict[str, Any]],
        target: dict[str, Any],
        strategy: str,
        upsert_keys: list[str],
    ) -> PreparedDelivery:
        validator = (
            validate_postgresql_identifier
            if self.sink_type == "POSTGRESQL"
            else validate_identifier
        )
        schema_name = validator(target["schema_name"])
        table_name = validator(target["table_name"])
        prepared_target = dict(target)
        if self.sink_type == "POSTGRESQL" and strategy == "UPSERT":
            constraint_name = prepared_target.get("_upsert_constraint")
            if not isinstance(constraint_name, str) or not constraint_name:
                raise DeliveryError(
                    "UPSERT_CONSTRAINT_MISSING",
                    "La restricción UPSERT exacta no está disponible.",
                )
            validator(constraint_name)
        prepared_columns = [dict(column) for column in columns]
        for column in prepared_columns:
            validator(column["target_name"])
            # Keep every deterministic DDL/type error before the durable marker.
            technical_type(self.sink_type, column)
        prepared_keys = [validator(name) for name in upsert_keys]
        rows = tuple(prepare_rows(records, prepared_columns))
        return PreparedDelivery(
            schema_name=schema_name,
            table_name=table_name,
            columns=prepared_columns,
            rows=rows,
            target=prepared_target,
            strategy=strategy,
            upsert_keys=prepared_keys,
            bytes_sent=serialized_size(rows),
        )

    def deliver(
        self,
        records: Sequence[dict[str, Any]],
        columns: list[dict[str, Any]],
        target: dict[str, Any],
        strategy: str,
        upsert_keys: list[str],
    ) -> DeliveryResult:
        return self.deliver_prepared(
            self.prepare(records, columns, target, strategy, upsert_keys)
        )

    def deliver_prepared(self, payload: PreparedDelivery) -> DeliveryResult:
        raise NotImplementedError


class PostgreSQLDataSink(DatabaseDataSink):
    sink_type = "POSTGRESQL"

    @contextmanager
    def _connection(self) -> Iterator[Any]:
        settings = self.settings
        connection = None
        try:
            connection = psycopg.connect(
                host=settings.host,
                port=settings.port,
                dbname=settings.database,
                user=settings.username,
                password=settings.password,
                connect_timeout=settings.options.get("connect_timeout", 5),
                sslmode=settings.options.get("sslmode", "require"),
                application_name="Trackvance delivery",
                options=(
                    f"-c statement_timeout={settings.options.get('query_timeout', 60) * 1000}"
                ),
            )
            yield connection
        except DeliveryError:
            raise
        except psycopg.Error as exc:
            raise _safe_driver_error(exc) from None
        finally:
            if connection is not None:
                try:
                    connection.close()
                except psycopg.Error:
                    # Closing is cleanup, not part of the remote transaction outcome.
                    # In particular, a close failure after commit must not turn a
                    # confirmed write into FAILED and invite a duplicate retry.
                    pass

    def schemas(self) -> list[str]:
        with self._connection() as connection, connection.cursor() as cursor:
            cursor.execute(
                """SELECT nspname FROM pg_namespace
                WHERE left(nspname, 3) <> 'pg_' AND nspname <> 'information_schema'
                  AND has_schema_privilege(oid, 'USAGE') ORDER BY nspname LIMIT 5000"""
            )
            return [str(row[0]) for row in cursor.fetchall()]

    def tables(self, schema_name: str) -> list[str]:
        validate_postgresql_identifier(schema_name)
        with self._connection() as connection, connection.cursor() as cursor:
            cursor.execute(
                """SELECT c.relname FROM pg_class c
                JOIN pg_namespace n ON n.oid = c.relnamespace
                WHERE n.nspname = %s AND c.relkind IN ('r', 'p')
                  AND has_schema_privilege(n.oid, 'USAGE')
                ORDER BY c.relname LIMIT 5000""",
                (schema_name,),
            )
            return [str(row[0]) for row in cursor.fetchall()]

    def table_metadata(self, schema_name: str, table_name: str) -> dict[str, Any]:
        validate_postgresql_identifier(schema_name)
        validate_postgresql_identifier(table_name)
        with self._connection() as connection, connection.cursor() as cursor:
            cursor.execute(
                """SELECT c.column_name, c.data_type, c.udt_name, c.is_nullable = 'YES',
                          c.column_default, c.is_identity = 'YES', c.is_generated <> 'NEVER',
                          c.character_maximum_length, c.numeric_precision, c.numeric_scale,
                          c.datetime_precision
                   FROM information_schema.columns c
                   WHERE c.table_schema = %s AND c.table_name = %s
                   ORDER BY c.ordinal_position""",
                (schema_name, table_name),
            )
            columns = []
            for row in cursor.fetchall():
                native = str(row[1] or row[2])
                columns.append(
                    {
                        "name": row[0],
                        "native_type": native,
                        "logical_type": logical_type_for_native(self.sink_type, native),
                        "nullable": bool(row[3]),
                        "has_default": row[4] is not None,
                        "identity": bool(row[5]),
                        "generated": bool(row[6]),
                        "length": row[7],
                        "precision": row[8],
                        "scale": row[9],
                        "datetime_precision": row[10],
                    }
                )
            if not columns:
                raise DeliveryError("TARGET_NOT_FOUND", "No se encontró la tabla seleccionada.")
            cursor.execute(
                """SELECT CASE con.contype WHEN 'p' THEN 'PRIMARY KEY' ELSE 'UNIQUE' END,
                          array_agg(att.attname ORDER BY keys.ordinality), con.conname
                   FROM pg_constraint con
                   JOIN pg_class cls ON cls.oid = con.conrelid
                   JOIN pg_namespace nsp ON nsp.oid = cls.relnamespace
                   CROSS JOIN LATERAL unnest(con.conkey) WITH ORDINALITY keys(attnum, ordinality)
                   JOIN pg_attribute att ON att.attrelid = cls.oid AND att.attnum = keys.attnum
                   WHERE nsp.nspname = %s AND cls.relname = %s AND con.contype IN ('p', 'u')
                     AND NOT con.condeferrable
                   GROUP BY con.oid, con.contype, con.conname
                   ORDER BY con.contype, con.conname""",
                (schema_name, table_name),
            )
            constraints = [
                {
                    "type": str(row[0]).replace(" ", "_"),
                    "columns": list(row[1]),
                    "name": str(row[2]),
                }
                for row in cursor.fetchall()
            ]
            cursor.execute(
                """SELECT row_security_active(cls.oid)
                   FROM pg_class cls
                   JOIN pg_namespace nsp ON nsp.oid = cls.relnamespace
                   WHERE nsp.nspname = %s AND cls.relname = %s
                     AND cls.relkind IN ('r', 'p')""",
                (schema_name, table_name),
            )
            security_row = cursor.fetchone()
            return {
                "schema_name": schema_name,
                "table_name": table_name,
                "columns": columns,
                "constraints": constraints,
                "row_security_active": bool(security_row and security_row[0]),
            }

    def permissions(self, target: dict[str, Any], strategy: str) -> dict[str, bool]:
        schema_name = validate_postgresql_identifier(target["schema_name"])
        table_name = validate_postgresql_identifier(target["table_name"])
        mode = target["mode"]
        with self._connection() as connection, connection.cursor() as cursor:
            if mode == "CREATE_TABLE" and target.get("create_schema"):
                cursor.execute("SELECT has_database_privilege(current_database(), 'CREATE')")
                allowed = bool(cursor.fetchone()[0])
                return {"connect": True, "create_schema": allowed, "create_table": allowed}
            cursor.execute(
                """SELECT has_schema_privilege(n.oid, 'USAGE'),
                          has_schema_privilege(n.oid, 'CREATE')
                   FROM pg_namespace n WHERE n.nspname = %s""",
                (schema_name,),
            )
            schema_row = cursor.fetchone()
            if not schema_row:
                return {"connect": True, "schema_exists": False, "allowed": False}
            if mode == "CREATE_TABLE":
                return {
                    "connect": True,
                    "schema_exists": True,
                    "create_table": bool(schema_row[0] and schema_row[1]),
                }
            privilege = {
                "APPEND": "INSERT",
                "OVERWRITE": "INSERT,DELETE",
                "UPSERT": "INSERT,UPDATE,SELECT",
            }[strategy]
            cursor.execute(
                """SELECT bool_and(has_table_privilege(c.oid, permission))
                   FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace,
                        unnest(string_to_array(%s, ',')) permission
                   WHERE n.nspname = %s AND c.relname = %s AND c.relkind IN ('r','p')""",
                (privilege, schema_name, table_name),
            )
            allowed = cursor.fetchone()[0]
            temporary_allowed = True
            if strategy == "UPSERT":
                cursor.execute(
                    "SELECT has_database_privilege(current_database(), 'TEMPORARY')"
                )
                temporary_allowed = bool(cursor.fetchone()[0])
            return {
                "connect": True,
                "schema_exists": True,
                "target_exists": allowed is not None,
                "allowed": bool(allowed) and temporary_allowed,
            }

    @staticmethod
    def _qualified(schema_name: str, table_name: str):
        return sql.SQL("{}.{}").format(
            sql.Identifier(validate_postgresql_identifier(schema_name)),
            sql.Identifier(validate_postgresql_identifier(table_name)),
        )

    def _insert(
        self,
        cursor: Any,
        schema_name: str,
        table_name: str,
        columns: list[dict[str, Any]],
        rows: Sequence[tuple],
    ) -> int | None:
        if not rows:
            return 0
        for column in columns:
            validate_postgresql_identifier(column["target_name"])
        query = sql.SQL("INSERT INTO {} ({}) VALUES ({})").format(
            self._qualified(schema_name, table_name),
            sql.SQL(", ").join(sql.Identifier(column["target_name"]) for column in columns),
            sql.SQL(", ").join(sql.Placeholder() for _ in columns),
        )
        cursor.executemany(query, rows)
        # psycopg sums command-tag counts for executemany without RETURNING.
        # BEFORE triggers may suppress a row; len(rows) would fabricate inserts.
        return _reported_rowcount(cursor)

    @staticmethod
    def _reject_active_row_security(
        cursor: Any, schema_name: str, table_name: str
    ) -> None:
        cursor.execute(
            """SELECT row_security_active(cls.oid)
               FROM pg_class cls
               JOIN pg_namespace nsp ON nsp.oid = cls.relnamespace
               WHERE nsp.nspname = %s AND cls.relname = %s
                 AND cls.relkind IN ('r', 'p')""",
            (schema_name, table_name),
        )
        security_row = cursor.fetchone()
        if security_row and bool(security_row[0]):
            raise DeliveryError(
                "UNSAFE_ROW_SECURITY",
                "OVERWRITE no se ejecuta con row-level security activa porque DELETE podría ocultar filas.",
            )

    def _upsert(
        self,
        cursor: Any,
        schema_name: str,
        table_name: str,
        columns: list[dict[str, Any]],
        rows: Sequence[tuple],
        upsert_keys: list[str],
        constraint_name: str,
        server_version: int = 0,
    ) -> tuple[int | None, int | None]:
        for column in columns:
            validate_postgresql_identifier(column["target_name"])
        for name in upsert_keys:
            validate_postgresql_identifier(name)
        validate_postgresql_identifier(constraint_name)
        key_set = set(upsert_keys)
        updates = [column["target_name"] for column in columns if column["target_name"] not in key_set]
        if rows:
            # Reuse the destination's native key types and collations in a
            # transaction-local uniqueness probe. This catches source keys
            # that Python considers distinct but a nondeterministic ICU
            # collation considers equal, before the target is touched.
            temporary = sql.Identifier("trackvance_upsert_keys")
            key_columns = sql.SQL(", ").join(
                sql.Identifier(name) for name in upsert_keys
            )
            cursor.execute(
                sql.SQL(
                    "CREATE TEMP TABLE {} ON COMMIT DROP AS "
                    "SELECT {} FROM {} WITH NO DATA"
                ).format(
                    temporary,
                    key_columns,
                    self._qualified(schema_name, table_name),
                )
            )
            cursor.execute(
                sql.SQL("CREATE UNIQUE INDEX ON {} ({})").format(
                    temporary, key_columns
                )
            )
            positions = {
                column["target_name"]: index for index, column in enumerate(columns)
            }
            key_rows = [
                tuple(row[positions[name]] for name in upsert_keys) for row in rows
            ]
            cursor.executemany(
                sql.SQL("INSERT INTO {} ({}) VALUES ({})").format(
                    temporary,
                    key_columns,
                    sql.SQL(", ").join(
                        sql.Placeholder() for _ in upsert_keys
                    ),
                ),
                key_rows,
            )
            # CTAS took an AccessShare lock on the target. Revalidate the
            # named arbiter only after that lock is held so a homonymous
            # DROP/CREATE cannot redirect ON CONFLICT to different columns.
            cursor.execute(
                """SELECT array_agg(att.attname ORDER BY keys.ordinality)
                   FROM pg_constraint con
                   JOIN pg_class cls ON cls.oid = con.conrelid
                   JOIN pg_namespace nsp ON nsp.oid = cls.relnamespace
                   CROSS JOIN LATERAL unnest(con.conkey)
                       WITH ORDINALITY keys(attnum, ordinality)
                   JOIN pg_attribute att
                     ON att.attrelid = cls.oid AND att.attnum = keys.attnum
                   WHERE nsp.nspname = %s AND cls.relname = %s
                     AND con.conname = %s AND con.contype IN ('p', 'u')
                     AND NOT con.condeferrable
                   GROUP BY con.oid""",
                (schema_name, table_name, constraint_name),
            )
            arbiter = cursor.fetchone()
            if (
                not arbiter
                or len(arbiter[0]) != len(upsert_keys)
                or set(arbiter[0]) != set(upsert_keys)
            ):
                raise DeliveryError(
                    "UPSERT_CONSTRAINT_DRIFT",
                    "La restricción UPSERT cambió después del preflight; no se modificó el target.",
                )
        action = (
            sql.SQL("DO UPDATE SET {}").format(
                sql.SQL(", ").join(
                    sql.SQL("{} = EXCLUDED.{}").format(sql.Identifier(name), sql.Identifier(name))
                    for name in updates
                )
            )
            if updates
            else sql.SQL("DO NOTHING")
        )
        query = sql.SQL(
            "INSERT INTO {} ({}) VALUES ({}) "
            "ON CONFLICT ON CONSTRAINT {} {}"
        ).format(
            self._qualified(schema_name, table_name),
            sql.SQL(", ").join(sql.Identifier(column["target_name"]) for column in columns),
            sql.SQL(", ").join(sql.Placeholder() for _ in columns),
            sql.Identifier(constraint_name),
            action,
        )
        if not rows:
            return 0, 0
        if not updates:
            cursor.executemany(query, rows)
            return _reported_rowcount(cursor), 0
        if server_version < 180000:
            cursor.executemany(query, rows)
            # PostgreSQL <=17's command tag merges INSERT and UPDATE. Do not
            # infer an action from xmax or from a racy pre-write lookup.
            return None, None
        # PostgreSQL 18 documents OLD/NEW in RETURNING, including ON CONFLICT.
        # Test the whole OLD row against scalar NULL: `old.key IS NULL` and
        # `old IS NULL` misclassify a real all-null row / nullable unique key.
        query += sql.SQL(
            " RETURNING WITH (OLD AS trackvance_old) "
            "trackvance_old IS NOT DISTINCT FROM NULL"
        )
        cursor.executemany(query, rows, returning=True)
        inserted = updated = 0
        while True:
            for (was_inserted,) in cursor.fetchall():
                if was_inserted:
                    inserted += 1
                else:
                    updated += 1
            if not cursor.nextset():
                break
        return inserted, updated

    def deliver_prepared(self, payload: PreparedDelivery) -> DeliveryResult:
        schema_name = payload.schema_name
        table_name = payload.table_name
        columns = payload.columns
        rows = payload.rows
        target = payload.target
        strategy = payload.strategy
        upsert_keys = payload.upsert_keys
        inserted: int | None
        updated: int | None
        connection = None
        try:
            with self._connection() as connection, connection.cursor() as cursor:
                if target["mode"] == "CREATE_TABLE":
                    cursor.execute(
                        "SELECT EXISTS (SELECT 1 FROM pg_namespace WHERE nspname = %s)",
                        (schema_name,),
                    )
                    schema_exists = bool(cursor.fetchone()[0])
                    if target.get("create_schema"):
                        if schema_exists:
                            raise DeliveryError(
                                "TARGET_ALREADY_EXISTS",
                                "El schema apareció después del preflight; no se modificó.",
                            )
                        cursor.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema_name)))
                    elif not schema_exists:
                        raise DeliveryError("SCHEMA_NOT_FOUND", "El schema seleccionado ya no existe.")
                    cursor.execute(
                        """SELECT EXISTS (
                           SELECT 1 FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
                           WHERE n.nspname=%s AND c.relname=%s AND c.relkind IN ('r','p'))""",
                        (schema_name, table_name),
                    )
                    if cursor.fetchone()[0]:
                        raise DeliveryError(
                            "TARGET_ALREADY_EXISTS",
                            "La tabla apareció después del preflight; no se sobrescribió.",
                        )
                    definitions = sql.SQL(", ").join(
                        sql.SQL("{} {} {}").format(
                            sql.Identifier(column["target_name"]),
                            sql.SQL(technical_type(self.sink_type, column)),
                            sql.SQL("NULL" if column.get("nullable", True) else "NOT NULL"),
                        )
                        for column in columns
                    )
                    cursor.execute(
                        sql.SQL("CREATE TABLE {} ({})").format(
                            self._qualified(schema_name, table_name), definitions
                        )
                    )
                    inserted = self._insert(cursor, schema_name, table_name, columns, rows)
                    updated = 0
                else:
                    # Establish target existence and freeze DDL even for an
                    # empty DatasetVersion, where INSERT/UPSERT would otherwise
                    # never reference the relation.
                    cursor.execute(
                        sql.SQL("LOCK TABLE {} IN {} MODE").format(
                            self._qualified(schema_name, table_name),
                            sql.SQL(
                                "SHARE ROW EXCLUSIVE"
                                if strategy == "OVERWRITE"
                                else "ROW EXCLUSIVE"
                            ),
                        )
                    )
                    if strategy == "APPEND":
                        inserted = self._insert(cursor, schema_name, table_name, columns, rows)
                        updated = 0
                    elif strategy == "OVERWRITE":
                        self._reject_active_row_security(
                            cursor, schema_name, table_name
                        )
                        cursor.execute(
                            sql.SQL("DELETE FROM {}").format(
                                self._qualified(schema_name, table_name)
                            )
                        )
                        inserted = self._insert(cursor, schema_name, table_name, columns, rows)
                        updated = 0
                    else:
                        inserted, updated = self._upsert(
                            cursor,
                            schema_name,
                            table_name,
                            columns,
                            rows,
                            upsert_keys,
                            str(target.get("_upsert_constraint", "")),
                            getattr(getattr(connection, "info", None), "server_version", 0),
                        )
                try:
                    connection.commit()
                except psycopg.Error as exc:
                    raise _safe_driver_error(exc, ambiguous=True) from None
        except DeliveryError:
            if connection is not None:
                try:
                    connection.rollback()
                except psycopg.Error:
                    pass
            raise
        except psycopg.Error as exc:
            if connection is not None:
                try:
                    connection.rollback()
                except psycopg.Error:
                    pass
            raise _safe_driver_error(exc) from None
        return DeliveryResult(
            rows_attempted=len(rows),
            rows_written=len(rows),
            rows_inserted=inserted,
            rows_updated=updated,
            bytes_sent=payload.bytes_sent,
        )


class SQLServerDataSink(DatabaseDataSink):
    sink_type = "SQLSERVER"

    @contextmanager
    def _connection(self) -> Iterator[Any]:
        settings = self.settings
        connection = None
        with _mssql_write_lock:
            try:
                connection = pymssql.connect(
                    server=settings.host,
                    port=str(settings.port),
                    database=settings.database,
                    user=settings.username,
                    password=settings.password,
                    login_timeout=settings.options.get("connect_timeout", 5),
                    timeout=settings.options.get("query_timeout", 60),
                    charset="UTF-8",
                    tds_version="7.4",
                    appname="Trackvance delivery",
                    encryption=settings.options.get("encryption", "require"),
                    use_datetime2=True,
                )
                yield connection
            except DeliveryError:
                raise
            except pymssql.Error as exc:
                raise _safe_driver_error(exc) from None
            finally:
                if connection is not None:
                    try:
                        connection.close()
                    except pymssql.Error:
                        # A successful query/commit remains successful even when
                        # disposal of the already-used socket reports an error.
                        pass

    def schemas(self) -> list[str]:
        with self._connection() as connection, connection.cursor() as cursor:
            cursor.execute(
                """SELECT TOP (5000) s.name FROM sys.schemas s
                   WHERE s.name NOT IN ('sys', 'INFORMATION_SCHEMA')
                   ORDER BY s.name"""
            )
            return [str(row[0]) for row in cursor.fetchall()]

    def tables(self, schema_name: str) -> list[str]:
        validate_identifier(schema_name)
        with self._connection() as connection, connection.cursor() as cursor:
            cursor.execute(
                """SELECT TOP (5000) o.name FROM sys.objects o
                   JOIN sys.schemas s ON s.schema_id = o.schema_id
                   WHERE s.name = %s AND o.type = 'U' AND o.is_ms_shipped = 0
                   ORDER BY o.name""",
                (schema_name,),
            )
            return [str(row[0]) for row in cursor.fetchall()]

    def table_metadata(self, schema_name: str, table_name: str) -> dict[str, Any]:
        validate_identifier(schema_name)
        validate_identifier(table_name)
        with self._connection() as connection, connection.cursor() as cursor:
            cursor.execute(
                """SELECT c.name, t.name, c.is_nullable, dc.definition,
                          c.is_identity,
                          CASE WHEN c.is_computed = 1 OR c.generated_always_type <> 0
                               THEN 1 ELSE 0 END,
                          c.max_length,
                          c.precision, c.scale, c.collation_name
                   FROM sys.columns c
                   JOIN sys.types t ON t.user_type_id = c.user_type_id
                   JOIN sys.objects o ON o.object_id = c.object_id
                   JOIN sys.schemas s ON s.schema_id = o.schema_id
                   LEFT JOIN sys.default_constraints dc ON dc.object_id = c.default_object_id
                   WHERE s.name = %s AND o.name = %s AND o.type = 'U'
                   ORDER BY c.column_id""",
                (schema_name, table_name),
            )
            columns = []
            for row in cursor.fetchall():
                native = "rowversion" if str(row[1]).casefold() == "timestamp" else str(row[1])
                raw_max_length = row[6]
                length = None
                if native.casefold() in {"nvarchar", "nchar"}:
                    length = (
                        None
                        if raw_max_length == -1
                        else int(raw_max_length) // 2
                    )
                elif native.casefold() in {"varchar", "char"}:
                    length = None if raw_max_length == -1 else int(raw_max_length)
                columns.append(
                    {
                        "name": row[0],
                        "native_type": native,
                        "logical_type": logical_type_for_native(self.sink_type, native),
                        "nullable": bool(row[2]),
                        "has_default": row[3] is not None,
                        "identity": bool(row[4]),
                        "generated": bool(row[5]) or native == "rowversion",
                        # sys.columns.max_length is storage bytes for every native
                        # type (for example DATE=3). It is a character limit only
                        # for the character families handled above.
                        "length": length,
                        "precision": row[7],
                        "scale": row[8],
                        "datetime_precision": (
                            row[8]
                            if native.casefold() in {"datetime2", "datetimeoffset"}
                            else None
                        ),
                        "collation": row[9],
                    }
                )
            if not columns:
                raise DeliveryError("TARGET_NOT_FOUND", "No se encontró la tabla seleccionada.")
            cursor.execute(
                """SELECT i.is_primary_key, i.is_unique, ic.key_ordinal, c.name, i.name,
                          i.ignore_dup_key
                   FROM sys.indexes i
                   JOIN sys.index_columns ic
                     ON ic.object_id = i.object_id AND ic.index_id = i.index_id
                   JOIN sys.columns c
                     ON c.object_id = ic.object_id AND c.column_id = ic.column_id
                   JOIN sys.objects o ON o.object_id = i.object_id
                   JOIN sys.schemas s ON s.schema_id = o.schema_id
                   WHERE s.name = %s AND o.name = %s
                     AND (i.is_primary_key = 1 OR i.is_unique = 1)
                     AND i.is_disabled = 0 AND i.is_hypothetical = 0
                     AND i.has_filter = 0
                     AND ic.is_included_column = 0
                   ORDER BY i.index_id, ic.key_ordinal""",
                (schema_name, table_name),
            )
            grouped: dict[str, dict[str, Any]] = {}
            for primary, unique, _ordinal, column_name, index_name, ignore_dup_key in cursor.fetchall():
                item = grouped.setdefault(
                    str(index_name),
                    {
                        "type": "PRIMARY_KEY" if primary else "UNIQUE",
                        "columns": [],
                        "unique": bool(unique),
                        "ignore_duplicate_keys": bool(ignore_dup_key),
                    },
                )
                item["columns"].append(str(column_name))
            cursor.execute(
                """SELECT COUNT(*)
                   FROM sys.security_predicates sp
                   JOIN sys.security_policies policy ON policy.object_id = sp.object_id
                   JOIN sys.objects o ON o.object_id = sp.target_object_id
                   JOIN sys.schemas s ON s.schema_id = o.schema_id
                   WHERE s.name = %s AND o.name = %s AND o.type = 'U'
                     AND policy.is_enabled = 1
                     AND sp.predicate_type_desc = 'FILTER'""",
                (schema_name, table_name),
            )
            security_row = cursor.fetchone()
            return {
                "schema_name": schema_name,
                "table_name": table_name,
                "columns": columns,
                "constraints": list(grouped.values()),
                "filter_security_policy": bool(security_row and security_row[0]),
            }

    def permissions(self, target: dict[str, Any], strategy: str) -> dict[str, bool]:
        schema_name = validate_identifier(target["schema_name"])
        table_name = validate_identifier(target["table_name"])
        mode = target["mode"]
        with self._connection() as connection, connection.cursor() as cursor:
            cursor.execute("SELECT SCHEMA_ID(%s)", (schema_name,))
            schema_exists = cursor.fetchone()[0] is not None
            if mode == "CREATE_TABLE" and target.get("create_schema"):
                cursor.execute(
                    """SELECT HAS_PERMS_BY_NAME(DB_NAME(), 'DATABASE', 'CREATE SCHEMA'),
                              HAS_PERMS_BY_NAME(DB_NAME(), 'DATABASE', 'CREATE TABLE')"""
                )
                create_schema, create_table = cursor.fetchone()
                return {
                    "connect": True,
                    "create_schema": bool(create_schema),
                    "create_table": bool(create_table),
                }
            if not schema_exists:
                return {"connect": True, "schema_exists": False, "allowed": False}
            if mode == "CREATE_TABLE":
                cursor.execute(
                    "SELECT HAS_PERMS_BY_NAME(%s, 'SCHEMA', 'ALTER')", (schema_name,)
                )
                alter_schema = bool(cursor.fetchone()[0])
                cursor.execute(
                    "SELECT HAS_PERMS_BY_NAME(DB_NAME(), 'DATABASE', 'CREATE TABLE')"
                )
                return {
                    "connect": True,
                    "schema_exists": True,
                    "create_table": alter_schema and bool(cursor.fetchone()[0]),
                }
            required = {
                # SELECT is needed for the transaction-scoped table lock that
                # freezes index/security-policy metadata before inserting.
                "APPEND": ["INSERT", "SELECT"],
                "OVERWRITE": ["INSERT", "DELETE", "SELECT"],
                # UPDATE predicates and key-only existence checks read target keys.
                "UPSERT": ["INSERT", "UPDATE", "SELECT"],
            }[strategy]
            locator = f"{quote_sqlserver_identifier(schema_name)}.{quote_sqlserver_identifier(table_name)}"
            allowed = True
            for permission in required:
                cursor.execute(
                    "SELECT HAS_PERMS_BY_NAME(%s, 'OBJECT', %s)", (locator, permission)
                )
                allowed = allowed and bool(cursor.fetchone()[0])
            security_metadata_visible = True
            if strategy == "OVERWRITE":
                cursor.execute(
                    "SELECT HAS_PERMS_BY_NAME(DB_NAME(), 'DATABASE', 'VIEW DEFINITION')"
                )
                security_metadata_visible = bool(cursor.fetchone()[0])
            cursor.execute(
                """SELECT COUNT(*) FROM sys.objects o
                   JOIN sys.schemas s ON s.schema_id=o.schema_id
                   WHERE s.name=%s AND o.name=%s AND o.type='U'""",
                (schema_name, table_name),
            )
            exists = bool(cursor.fetchone()[0])
            return {
                "connect": True,
                "schema_exists": True,
                "target_exists": exists,
                "security_metadata_visible": security_metadata_visible,
                "allowed": allowed and exists and security_metadata_visible,
            }

    @staticmethod
    def _qualified(schema_name: str, table_name: str) -> str:
        return (
            f"{quote_sqlserver_identifier(schema_name)}."
            f"{quote_sqlserver_identifier(table_name)}"
        )

    @staticmethod
    def _reject_ignore_duplicate_keys(
        cursor: Any, schema_name: str, table_name: str
    ) -> None:
        """Reject SQL Server indexes that can silently discard inserted rows.

        This check deliberately runs inside the same remote transaction as the
        write. Preflight remains useful feedback, but cannot be the authority
        because an index may drift between preflight and execution.
        """
        cursor.execute(
            """SELECT COUNT(*) FROM sys.indexes i
               JOIN sys.objects o ON o.object_id = i.object_id
               JOIN sys.schemas s ON s.schema_id = o.schema_id
               WHERE s.name = %s AND o.name = %s AND o.type = 'U'
                 AND i.is_unique = 1 AND i.ignore_dup_key = 1
                 AND i.is_disabled = 0 AND i.is_hypothetical = 0""",
            (schema_name, table_name),
        )
        if int(cursor.fetchone()[0]):
            raise DeliveryError(
                "UNSAFE_IGNORE_DUP_KEY",
                "El target tiene un índice UNIQUE con IGNORE_DUP_KEY; podría omitir filas sin error.",
            )

    @staticmethod
    def _reject_filter_security_policy(
        cursor: Any, schema_name: str, table_name: str
    ) -> None:
        cursor.execute(
            "SELECT HAS_PERMS_BY_NAME(DB_NAME(), 'DATABASE', 'VIEW DEFINITION')"
        )
        if not bool(cursor.fetchone()[0]):
            raise DeliveryError(
                "SECURITY_METADATA_NOT_VISIBLE",
                "OVERWRITE requiere VIEW DEFINITION para descartar políticas de seguridad invisibles.",
            )
        cursor.execute(
            """SELECT COUNT(*)
               FROM sys.security_predicates sp
               JOIN sys.security_policies policy ON policy.object_id = sp.object_id
               JOIN sys.objects o ON o.object_id = sp.target_object_id
               JOIN sys.schemas s ON s.schema_id = o.schema_id
               WHERE s.name = %s AND o.name = %s AND o.type = 'U'
                 AND policy.is_enabled = 1
                 AND sp.predicate_type_desc = 'FILTER'""",
            (schema_name, table_name),
        )
        if int(cursor.fetchone()[0]):
            raise DeliveryError(
                "UNSAFE_ROW_SECURITY",
                "OVERWRITE no se ejecuta con una FILTER security policy activa porque DELETE podría ocultar filas.",
            )

    def _lock_existing_target(
        self, cursor: Any, schema_name: str, table_name: str
    ) -> None:
        cursor.execute(
            f"SELECT TOP (1) 1 FROM {self._qualified(schema_name, table_name)} "
            "WITH (TABLOCKX, HOLDLOCK)"
        )

    def _insert(
        self,
        cursor: Any,
        schema_name: str,
        table_name: str,
        columns: list[dict[str, Any]],
        rows: Sequence[tuple],
    ) -> int | None:
        if not rows:
            return 0
        names = ", ".join(quote_sqlserver_identifier(column["target_name"]) for column in columns)
        placeholders = ", ".join("%s" for _ in columns)
        cursor.executemany(
            f"INSERT INTO {self._qualified(schema_name, table_name)} ({names}) "
            f"VALUES ({placeholders})",
            rows,
        )
        # pymssql reports -1 when it cannot aggregate affected-row counts (for
        # example larger executemany batches). Unknown must remain null.
        return _reported_rowcount(cursor)

    def _upsert(
        self,
        cursor: Any,
        schema_name: str,
        table_name: str,
        columns: list[dict[str, Any]],
        rows: Sequence[tuple],
        upsert_keys: list[str],
    ) -> tuple[int, int]:
        names = [column["target_name"] for column in columns]
        positions = {name: index for index, name in enumerate(names)}
        key_set = set(upsert_keys)
        update_names = [name for name in names if name not in key_set]
        locator = self._qualified(schema_name, table_name)
        predicates = " AND ".join(
            f"{quote_sqlserver_identifier(name)} = %s" for name in upsert_keys
        )
        assignments = ", ".join(
            f"{quote_sqlserver_identifier(name)} = %s" for name in update_names
        )
        insert_names = ", ".join(quote_sqlserver_identifier(name) for name in names)
        insert_values = ", ".join("%s" for _ in names)
        if rows:
            # Clone the target key types and collations into a transaction-local
            # table, then enforce uniqueness before touching the target. The
            # self join deliberately prevents SQL Server from propagating an
            # IDENTITY property while preserving type/collation semantics.
            key_names = ", ".join(
                quote_sqlserver_identifier(name) for name in upsert_keys
            )
            projected_keys = ", ".join(
                f"source.{quote_sqlserver_identifier(name)}"
                for name in upsert_keys
            )
            cursor.execute(
                f"SELECT TOP (0) {projected_keys} INTO #trackvance_upsert_keys "
                f"FROM {locator} AS source LEFT JOIN {locator} AS no_identity ON 1 = 0"
            )
            cursor.execute(
                "CREATE UNIQUE INDEX [UX_trackvance_upsert_keys] "
                f"ON #trackvance_upsert_keys ({key_names})"
            )
            key_rows = [
                tuple(row[positions[name]] for name in upsert_keys) for row in rows
            ]
            cursor.executemany(
                f"INSERT INTO #trackvance_upsert_keys ({key_names}) "
                f"VALUES ({', '.join('%s' for _ in upsert_keys)})",
                key_rows,
            )
        inserted = updated = 0
        for row in rows:
            keys = tuple(row[positions[name]] for name in upsert_keys)
            if update_names:
                values = tuple(row[positions[name]] for name in update_names) + keys
                cursor.execute(
                    f"UPDATE {locator} WITH (UPDLOCK, HOLDLOCK) "
                    f"SET {assignments} WHERE {predicates}; SELECT @@ROWCOUNT",
                    values,
                )
                matched = int(cursor.fetchone()[0])
            else:
                cursor.execute(
                    f"SELECT COUNT_BIG(*) FROM {locator} WITH (UPDLOCK, HOLDLOCK) "
                    f"WHERE {predicates}",
                    keys,
                )
                matched = int(cursor.fetchone()[0])
            if matched > 1:
                raise DeliveryError(
                    "TARGET_KEY_NOT_UNIQUE",
                    "La clave UPSERT dejó de identificar una única fila; no se confirmó ningún cambio.",
                )
            if matched:
                # A key-only UPSERT performs no UPDATE for an existing key.
                updated += int(bool(update_names))
            else:
                cursor.execute(
                    f"INSERT INTO {locator} ({insert_names}) VALUES ({insert_values}); "
                    "SELECT @@ROWCOUNT", row
                )
                inserted += int(cursor.fetchone()[0])
        return inserted, updated

    def deliver_prepared(self, payload: PreparedDelivery) -> DeliveryResult:
        schema_name = payload.schema_name
        table_name = payload.table_name
        columns = payload.columns
        rows = payload.rows
        target = payload.target
        strategy = payload.strategy
        upsert_keys = payload.upsert_keys
        connection = None
        try:
            with self._connection() as connection, connection.cursor() as cursor:
                if target["mode"] == "CREATE_TABLE":
                    cursor.execute("SELECT SCHEMA_ID(%s)", (schema_name,))
                    schema_exists = cursor.fetchone()[0] is not None
                    if target.get("create_schema"):
                        if schema_exists:
                            raise DeliveryError(
                                "TARGET_ALREADY_EXISTS",
                                "El schema apareció después del preflight; no se modificó.",
                            )
                        cursor.execute(
                            f"CREATE SCHEMA {quote_sqlserver_identifier(schema_name)}"
                        )
                    elif not schema_exists:
                        raise DeliveryError("SCHEMA_NOT_FOUND", "El schema seleccionado ya no existe.")
                    cursor.execute(
                        """SELECT COUNT(*) FROM sys.objects o
                           JOIN sys.schemas s ON s.schema_id=o.schema_id
                           WHERE s.name=%s AND o.name=%s AND o.type='U'""",
                        (schema_name, table_name),
                    )
                    if cursor.fetchone()[0]:
                        raise DeliveryError(
                            "TARGET_ALREADY_EXISTS",
                            "La tabla apareció después del preflight; no se sobrescribió.",
                        )
                    definitions = ", ".join(
                        f"{quote_sqlserver_identifier(column['target_name'])} "
                        f"{technical_type(self.sink_type, column)} "
                        f"{'COLLATE ' + SQLSERVER_UNICODE_COLLATION + ' ' if column['target_type'] == 'STRING' else ''}"
                        f"{'NULL' if column.get('nullable', True) else 'NOT NULL'}"
                        for column in columns
                    )
                    cursor.execute(
                        f"CREATE TABLE {self._qualified(schema_name, table_name)} ({definitions})"
                    )
                    inserted = self._insert(cursor, schema_name, table_name, columns, rows)
                    updated = 0
                else:
                    self._lock_existing_target(cursor, schema_name, table_name)
                    self._reject_ignore_duplicate_keys(
                        cursor, schema_name, table_name
                    )
                    if strategy == "APPEND":
                        inserted = self._insert(cursor, schema_name, table_name, columns, rows)
                        updated = 0
                    elif strategy == "OVERWRITE":
                        self._reject_filter_security_policy(
                            cursor, schema_name, table_name
                        )
                        cursor.execute(
                            f"DELETE FROM {self._qualified(schema_name, table_name)}"
                        )
                        inserted = self._insert(cursor, schema_name, table_name, columns, rows)
                        updated = 0
                    else:
                        inserted, updated = self._upsert(
                            cursor,
                            schema_name,
                            table_name,
                            columns,
                            rows,
                            upsert_keys,
                        )
                try:
                    connection.commit()
                except pymssql.Error as exc:
                    raise _safe_driver_error(exc, ambiguous=True) from None
        except DeliveryError:
            if connection is not None:
                try:
                    connection.rollback()
                except pymssql.Error:
                    pass
            raise
        except pymssql.Error as exc:
            if connection is not None:
                try:
                    connection.rollback()
                except pymssql.Error:
                    pass
            raise _safe_driver_error(exc) from None
        return DeliveryResult(
            rows_attempted=len(rows),
            rows_written=len(rows),
            rows_inserted=inserted,
            rows_updated=updated,
            bytes_sent=payload.bytes_sent,
        )


class DataSinkRegistry:
    def __init__(self) -> None:
        self._adapters: dict[str, type[DatabaseDataSink]] = {}

    def register(self, sink_type: str, adapter: type[DatabaseDataSink]) -> None:
        self._adapters[sink_type] = adapter

    def create(self, settings: DestinationSettings) -> DatabaseDataSink:
        adapter = self._adapters.get(settings.sink_type)
        if adapter is None:
            raise DeliveryError("UNSUPPORTED_SINK", "No existe un adaptador para este destino.")
        return adapter(settings)


sink_registry = DataSinkRegistry()
sink_registry.register("POSTGRESQL", PostgreSQLDataSink)
sink_registry.register("SQLSERVER", SQLServerDataSink)
