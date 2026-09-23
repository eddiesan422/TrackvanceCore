from collections import deque
from contextlib import contextmanager
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any
from unittest.mock import MagicMock

import psycopg
import pymssql
import pytest

from trackvance import data_sinks as sinks
from trackvance.data_sinks import (
    DatabaseDataSink,
    DataSink,
    DataSinkRegistry,
    DeliveryError,
    DestinationSettings,
    PostgreSQLDataSink,
    SQLServerDataSink,
    convert_value,
    integer_bounds_for_native,
    logical_type_for_native,
    prepare_rows,
    quote_sqlserver_identifier,
    sink_registry,
    technical_type,
    validate_identifier,
)


def settings(sink_type: str = "POSTGRESQL", **overrides: Any) -> DestinationSettings:
    values = {
        "sink_type": sink_type,
        "host": "destination.example.test",
        "port": 5432 if sink_type == "POSTGRESQL" else 1433,
        "database": "warehouse",
        "username": "writer",
        "password": "private-password",
    }
    return DestinationSettings(**(values | overrides))


def column(
    source_name: str,
    target_name: str,
    target_type: str,
    *,
    nullable: bool = True,
    **parameters: Any,
) -> dict[str, Any]:
    return {
        "source_name": source_name,
        "target_name": target_name,
        "target_type": target_type,
        "nullable": nullable,
        **parameters,
    }


def render_sql(statement: Any) -> str:
    if hasattr(statement, "as_string"):
        return statement.as_string()
    return str(statement)


class FakeCursor:
    def __init__(
        self,
        *,
        fetchone: tuple[Any, ...] = (),
        fetchall: tuple[Any, ...] = (),
        rowcounts: tuple[int, ...] = (),
        executemany_error: Exception | None = None,
    ) -> None:
        self.executions: list[tuple[str, Any]] = []
        self.executemany_calls: list[tuple[str, list[tuple[Any, ...]]]] = []
        self._fetchone = deque(fetchone)
        self._fetchall = deque(fetchall)
        self._rowcounts = deque(rowcounts)
        self.executemany_error = executemany_error
        self.rowcount = 0

    def __enter__(self):
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def execute(self, statement: Any, params: Any = None) -> None:
        self.executions.append((render_sql(statement), params))
        if self._rowcounts:
            self.rowcount = self._rowcounts.popleft()

    def executemany(self, statement: Any, rows: Any) -> None:
        self.executemany_calls.append((render_sql(statement), list(rows)))
        if self.executemany_error is not None:
            raise self.executemany_error

    def fetchone(self) -> Any:
        return self._fetchone.popleft()

    def fetchall(self) -> Any:
        return self._fetchall.popleft()


class FakeConnection:
    def __init__(self, cursor: FakeCursor, *, commit_error: Exception | None = None) -> None:
        self.fake_cursor = cursor
        self.commit_error = commit_error
        self.commit_count = 0
        self.rollback_count = 0

    def cursor(self) -> FakeCursor:
        return self.fake_cursor

    def commit(self) -> None:
        self.commit_count += 1
        if self.commit_error is not None:
            raise self.commit_error

    def rollback(self) -> None:
        self.rollback_count += 1


def attach_connection(
    monkeypatch: pytest.MonkeyPatch,
    adapter: DatabaseDataSink,
    cursor: FakeCursor | None = None,
    *,
    commit_error: Exception | None = None,
) -> tuple[FakeConnection, FakeCursor]:
    fake_cursor = cursor or FakeCursor()
    connection = FakeConnection(fake_cursor, commit_error=commit_error)

    @contextmanager
    def connection_context():
        yield connection

    monkeypatch.setattr(adapter, "_connection", connection_context)
    return connection, fake_cursor


@pytest.mark.parametrize(
    ("sink_type", "adapter_type"),
    [("POSTGRESQL", PostgreSQLDataSink), ("SQLSERVER", SQLServerDataSink)],
)
def test_registry_creates_runtime_data_sink_contract(sink_type, adapter_type):
    destination = settings(sink_type)
    adapter = sink_registry.create(destination)

    assert isinstance(adapter, adapter_type)
    assert isinstance(adapter, DataSink)
    assert adapter.settings is destination


def test_registry_is_explicit_and_rejects_unregistered_sink():
    registry = DataSinkRegistry()

    with pytest.raises(DeliveryError) as error:
        registry.create(settings())

    assert error.value.code == "UNSUPPORTED_SINK"
    assert "private-password" not in str(error.value)
    registry.register("POSTGRESQL", PostgreSQLDataSink)
    assert isinstance(registry.create(settings()), PostgreSQLDataSink)


@pytest.mark.parametrize(
    "overrides",
    [
        {"sink_type": "ORACLE"},
        {"host": "postgres://host?password=secret"},
        {"host": "host;user=admin"},
        {"port": 0},
        {"port": True},
        {"port": 65536},
        {"database": ""},
        {"database": "warehouse\npassword=secret"},
        {"username": ""},
        {"username": "writer\x00admin"},
        {"password": ""},
        {"password": "x" * 8193},
        {"options": {"password": "secret"}},
        {"options": {"query": "DELETE FROM private_table"}},
        {"options": {"connect_timeout": True}},
        {"options": {"connect_timeout": 16}},
        {"options": {"query_timeout": 0}},
        {"options": {"query_timeout": 301}},
        {"options": {"sslmode": "prefer"}},
        {"options": {"sslmode": []}},
    ],
)
def test_destination_settings_reject_invalid_or_uncontrolled_values_without_echo(overrides):
    with pytest.raises(DeliveryError) as error:
        settings(**overrides)

    message = str(error.value)
    assert error.value.code in {
        "UNSUPPORTED_SINK",
        "INVALID_DESTINATION",
        "INVALID_DESTINATION_OPTIONS",
    }
    assert "private-password" not in message
    assert "DELETE" not in message
    assert "secret" not in message


@pytest.mark.parametrize(
    "overrides",
    [
        {"options": {"encryption": "verify-full"}},
        {"options": {"encryption": []}},
        {"options": {"sslmode": "require"}},
    ],
)
def test_sqlserver_settings_allow_only_sqlserver_transport_options(overrides):
    with pytest.raises(DeliveryError) as error:
        settings("SQLSERVER", **overrides)
    assert error.value.code == "INVALID_DESTINATION_OPTIONS"


def test_destination_settings_accept_ip_hosts_controlled_tls_and_hide_password():
    postgres = settings(
        host="2001:db8::1",
        options={"connect_timeout": 15, "query_timeout": 300, "sslmode": "verify-full"},
    )
    sqlserver = settings(
        "SQLSERVER",
        host="192.0.2.8",
        options={"connect_timeout": 1, "query_timeout": 1, "encryption": "off"},
    )

    assert postgres.host == "2001:db8::1"
    assert sqlserver.options["encryption"] == "off"
    assert postgres.password not in repr(postgres)
    assert sqlserver.password not in repr(sqlserver)


def test_postgresql_connection_forwards_only_controlled_settings(monkeypatch):
    connect = MagicMock()
    connection = connect.return_value
    cursor = connection.cursor.return_value.__enter__.return_value
    cursor.fetchone.return_value = (1,)
    monkeypatch.setattr(sinks.psycopg, "connect", connect)
    destination = settings(
        options={"connect_timeout": 11, "query_timeout": 123, "sslmode": "verify-full"}
    )

    PostgreSQLDataSink(destination).test()

    assert connect.call_args.kwargs == {
        "host": "destination.example.test",
        "port": 5432,
        "dbname": "warehouse",
        "user": "writer",
        "password": "private-password",
        "connect_timeout": 11,
        "sslmode": "verify-full",
        "application_name": "Trackvance delivery",
        "options": "-c statement_timeout=123000",
    }
    cursor.execute.assert_called_once_with("SELECT 1")
    connection.rollback.assert_called_once_with()
    connection.close.assert_called_once_with()


def test_sqlserver_connection_forwards_only_controlled_settings(monkeypatch):
    connect = MagicMock()
    connection = connect.return_value
    cursor = connection.cursor.return_value.__enter__.return_value
    cursor.fetchone.return_value = (1,)
    monkeypatch.setattr(sinks.pymssql, "connect", connect)
    destination = settings(
        "SQLSERVER",
        options={"connect_timeout": 12, "query_timeout": 234, "encryption": "require"},
    )

    SQLServerDataSink(destination).test()

    assert connect.call_args.kwargs == {
        "server": "destination.example.test",
        "port": "1433",
        "database": "warehouse",
        "user": "writer",
        "password": "private-password",
        "login_timeout": 12,
        "timeout": 234,
        "charset": "UTF-8",
        "tds_version": "7.4",
        "appname": "Trackvance delivery",
        "encryption": "require",
        "use_datetime2": True,
    }
    cursor.execute.assert_called_once_with("SELECT 1")
    connection.rollback.assert_called_once_with()
    connection.close.assert_called_once_with()


@pytest.mark.parametrize(
    ("state", "number", "ambiguous", "expected_code", "expected_ambiguous"),
    [
        ("28P01", None, False, "DESTINATION_AUTH_FAILED", False),
        ("42501", None, False, "DESTINATION_PERMISSION_DENIED", False),
        ("57014", None, False, "DESTINATION_TIMEOUT", False),
        ("40001", None, True, "DESTINATION_TIMEOUT", False),
        ("23505", None, True, "DESTINATION_CONSTRAINT_VIOLATION", False),
        (None, 18456, False, "DESTINATION_AUTH_FAILED", False),
        (None, 229, False, "DESTINATION_PERMISSION_DENIED", False),
        (None, 1205, True, "DESTINATION_TIMEOUT", False),
        (None, 2627, True, "DESTINATION_CONSTRAINT_VIOLATION", False),
        (None, 20003, True, "DESTINATION_COMMIT_UNKNOWN", True),
        (None, None, False, "DESTINATION_UNAVAILABLE", False),
        (None, None, True, "DESTINATION_COMMIT_UNKNOWN", True),
    ],
)
def test_driver_errors_are_classified_and_sanitized(
    state, number, ambiguous, expected_code, expected_ambiguous
):
    class DriverFailure(Exception):
        sqlstate = state

    arguments = (number, "private-password in remote error") if number is not None else (
        "private-password in remote error",
    )
    result = sinks._safe_driver_error(DriverFailure(*arguments), ambiguous=ambiguous)

    assert result.code == expected_code
    assert result.ambiguous is expected_ambiguous
    assert "private-password" not in str(result)


@pytest.mark.parametrize(
    ("driver_name", "adapter_type", "sink_type", "failure"),
    [
        (
            "psycopg",
            PostgreSQLDataSink,
            "POSTGRESQL",
            psycopg.OperationalError("private-password in DSN"),
        ),
        (
            "pymssql",
            SQLServerDataSink,
            "SQLSERVER",
            pymssql.OperationalError("private-password in DSN"),
        ),
    ],
)
def test_connection_failures_never_expose_driver_details(
    monkeypatch, driver_name, adapter_type, sink_type, failure
):
    monkeypatch.setattr(getattr(sinks, driver_name), "connect", MagicMock(side_effect=failure))

    with pytest.raises(DeliveryError) as error:
        adapter_type(settings(sink_type)).test()

    assert error.value.code == "DESTINATION_UNAVAILABLE"
    assert "private-password" not in str(error.value)
    assert error.value.__suppress_context__


@pytest.mark.parametrize(
    ("driver_name", "adapter_type", "sink_type", "failure"),
    [
        (
            "psycopg",
            PostgreSQLDataSink,
            "POSTGRESQL",
            psycopg.OperationalError("private-password while closing"),
        ),
        (
            "pymssql",
            SQLServerDataSink,
            "SQLSERVER",
            pymssql.OperationalError("private-password while closing"),
        ),
    ],
)
def test_close_failures_after_success_do_not_change_the_operation_outcome(
    monkeypatch, driver_name, adapter_type, sink_type, failure
):
    connection = MagicMock()
    cursor = connection.cursor.return_value.__enter__.return_value
    cursor.fetchone.return_value = (1,)
    connection.close.side_effect = failure
    monkeypatch.setattr(getattr(sinks, driver_name), "connect", MagicMock(return_value=connection))

    adapter_type(settings(sink_type)).test()

    connection.rollback.assert_called_once_with()
    connection.close.assert_called_once()


@pytest.mark.parametrize(
    ("driver_name", "adapter_type", "sink_type", "failure"),
    [
        (
            "psycopg",
            PostgreSQLDataSink,
            "POSTGRESQL",
            psycopg.OperationalError("private-password while closing after commit"),
        ),
        (
            "pymssql",
            SQLServerDataSink,
            "SQLSERVER",
            pymssql.OperationalError("private-password while closing after commit"),
        ),
    ],
)
def test_close_failure_after_confirmed_commit_cannot_turn_delivery_into_failed(
    monkeypatch, driver_name, adapter_type, sink_type, failure
):
    connection = MagicMock()
    connection.close.side_effect = failure
    if sink_type == "SQLSERVER":
        connection.cursor.return_value.__enter__.return_value.fetchone.return_value = (0,)
    monkeypatch.setattr(
        getattr(sinks, driver_name), "connect", MagicMock(return_value=connection)
    )

    result = adapter_type(settings(sink_type)).deliver(
        [{"id": 1}],
        [column("id", "id", "INT64", nullable=False)],
        {"mode": "EXISTING_TABLE", "schema_name": "public", "table_name": "facts"},
        "APPEND",
        [],
    )

    assert result.rows_written == 1
    connection.commit.assert_called_once_with()
    connection.close.assert_called_once_with()


@pytest.mark.parametrize(
    ("driver_name", "adapter_type", "sink_type", "operation_failure", "close_failure"),
    [
        (
            "psycopg",
            PostgreSQLDataSink,
            "POSTGRESQL",
            psycopg.OperationalError("private-password during SELECT"),
            psycopg.OperationalError("private-password while closing"),
        ),
        (
            "pymssql",
            SQLServerDataSink,
            "SQLSERVER",
            pymssql.OperationalError("private-password during SELECT"),
            pymssql.OperationalError("private-password while closing"),
        ),
    ],
)
def test_close_failure_does_not_replace_active_sanitized_error(
    monkeypatch,
    driver_name,
    adapter_type,
    sink_type,
    operation_failure,
    close_failure,
):
    connection = MagicMock()
    cursor = connection.cursor.return_value.__enter__.return_value
    cursor.execute.side_effect = operation_failure
    connection.close.side_effect = close_failure
    monkeypatch.setattr(getattr(sinks, driver_name), "connect", MagicMock(return_value=connection))

    with pytest.raises(DeliveryError) as error:
        adapter_type(settings(sink_type)).test()

    assert error.value.code == "DESTINATION_UNAVAILABLE"
    assert "private-password" not in str(error.value)
    assert error.value.__suppress_context__
    connection.close.assert_called_once()


@pytest.mark.parametrize(
    ("native", "expected"),
    [
        ("smallint", "INT64"),
        ("integer", "INT64"),
        ("bigint", "INT64"),
        ("int8", "INT64"),
        ("numeric(24,8)", "DECIMAL"),
        ("double precision", "DECIMAL"),
        ("money", "DECIMAL"),
        ("boolean", "BOOLEAN"),
        ("date", "DATE"),
        ("timestamp(6) without time zone", "TIMESTAMP"),
        ("timestamp with time zone", "TIMESTAMP"),
        ("timestamptz", "TIMESTAMP"),
        ("text", "STRING"),
        ("varchar(80)", "STRING"),
        ("uuid", "UNSUPPORTED"),
        ("jsonb", "UNSUPPORTED"),
        ("bytea", "UNSUPPORTED"),
    ],
)
def test_postgresql_native_types_map_explicitly(native, expected):
    assert logical_type_for_native("POSTGRESQL", native) == expected


@pytest.mark.parametrize(
    ("native", "expected"),
    [
        ("tinyint", "INT64"),
        ("smallint", "INT64"),
        ("int", "INT64"),
        ("bigint", "INT64"),
        ("decimal(24,8)", "DECIMAL"),
        ("numeric", "DECIMAL"),
        ("smallmoney", "DECIMAL"),
        ("float", "DECIMAL"),
        ("bit", "BOOLEAN"),
        ("date", "DATE"),
        ("datetime", "TIMESTAMP"),
        ("datetime2", "TIMESTAMP"),
        ("datetimeoffset", "TIMESTAMP"),
        ("timestamp", "UNSUPPORTED"),
        ("rowversion", "UNSUPPORTED"),
        ("nvarchar(max)", "STRING"),
        ("varchar(max)", "UNSUPPORTED"),
        ("nchar(10)", "UNSUPPORTED"),
        ("uniqueidentifier", "UNSUPPORTED"),
        ("xml", "UNSUPPORTED"),
        ("varbinary(max)", "UNSUPPORTED"),
    ],
)
def test_sqlserver_native_types_map_explicitly(native, expected):
    assert logical_type_for_native("SQLSERVER", native) == expected


@pytest.mark.parametrize(
    ("sink_type", "native", "precision", "scale", "expected"),
    [
        ("POSTGRESQL", "numeric", 18, 4, (18, 4)),
        ("POSTGRESQL", "numeric", None, None, (76, 38)),
        ("POSTGRESQL", "money", 64, 2, (19, 2)),
        ("POSTGRESQL", "money", 64, None, None),
        ("POSTGRESQL", "double precision", 53, None, None),
        ("POSTGRESQL", "real", 24, None, None),
        ("SQLSERVER", "decimal", 18, 4, (18, 4)),
        ("SQLSERVER", "money", 19, 4, (19, 4)),
        ("SQLSERVER", "smallmoney", 10, 4, (10, 4)),
        ("SQLSERVER", "float", 53, 0, None),
        ("SQLSERVER", "real", 24, 0, None),
    ],
)
def test_decimal_capacity_accepts_only_exact_native_types(
    sink_type, native, precision, scale, expected
):
    assert sinks.decimal_capacity_for_native(
        sink_type, native, precision, scale
    ) == expected


@pytest.mark.parametrize(
    ("sink_type", "native", "precision", "expected"),
    [
        ("POSTGRESQL", "timestamp without time zone", 0, None),
        ("POSTGRESQL", "timestamp(6) with time zone", 6, (6, True)),
        ("POSTGRESQL", "timestamp", None, None),
        ("SQLSERVER", "datetime2", 3, None),
        ("SQLSERVER", "datetimeoffset", 7, (7, True)),
        ("SQLSERVER", "datetime", 3, None),
        ("SQLSERVER", "smalldatetime", 0, None),
    ],
)
def test_timestamp_policy_accepts_only_exact_native_families(
    sink_type, native, precision, expected
):
    assert sinks.timestamp_policy_for_native(
        sink_type, native, precision
    ) == expected


@pytest.mark.parametrize(
    ("sink_type", "native", "expected"),
    [
        ("POSTGRESQL", "smallint", (-(2**15), 2**15 - 1)),
        ("POSTGRESQL", "integer", (-(2**31), 2**31 - 1)),
        ("POSTGRESQL", "bigint", (-(2**63), 2**63 - 1)),
        ("SQLSERVER", "tinyint", (0, 255)),
        ("SQLSERVER", "smallint", (-(2**15), 2**15 - 1)),
        ("SQLSERVER", "int", (-(2**31), 2**31 - 1)),
        ("SQLSERVER", "bigint", (-(2**63), 2**63 - 1)),
        ("SQLSERVER", "nvarchar", None),
    ],
)
def test_integral_native_ranges_are_explicit(sink_type, native, expected):
    assert integer_bounds_for_native(sink_type, native) == expected


@pytest.mark.parametrize(
    ("target_type", "parameters", "postgresql", "sqlserver"),
    [
        ("STRING", {}, "TEXT", "NVARCHAR(MAX)"),
        ("STRING", {"length": 17}, "VARCHAR(17)", "NVARCHAR(17)"),
        ("STRING", {"length": 4001}, "VARCHAR(4001)", "NVARCHAR(MAX)"),
        ("INT64", {}, "BIGINT", "BIGINT"),
        ("DECIMAL", {}, "NUMERIC(38,10)", "DECIMAL(38,10)"),
        ("DECIMAL", {"precision": None, "scale": None}, "NUMERIC(38,10)", "DECIMAL(38,10)"),
        ("DECIMAL", {"precision": 18, "scale": 4}, "NUMERIC(18,4)", "DECIMAL(18,4)"),
        ("DATE", {}, "DATE", "DATE"),
        ("TIMESTAMP", {}, "TIMESTAMPTZ(6)", "DATETIMEOFFSET(6)"),
        ("BOOLEAN", {}, "BOOLEAN", "BIT"),
    ],
)
def test_technical_types_are_explicit_per_engine(target_type, parameters, postgresql, sqlserver):
    specification = {"target_type": target_type, **parameters}
    assert technical_type("POSTGRESQL", specification) == postgresql
    assert technical_type("SQLSERVER", specification) == sqlserver


@pytest.mark.parametrize(
    "specification",
    [
        {"target_type": "STRING", "length": 0},
        {"target_type": "STRING", "length": True},
        {"target_type": "STRING", "length": 1_000_001},
        {"target_type": "DECIMAL", "precision": 0, "scale": 0},
        {"target_type": "DECIMAL", "precision": 39, "scale": 0},
        {"target_type": "DECIMAL", "precision": 4, "scale": 5},
        {"target_type": "DECIMAL", "precision": True, "scale": 0},
        {"target_type": "BINARY"},
    ],
)
def test_technical_type_rejects_invalid_length_precision_scale_or_type(specification):
    with pytest.raises(DeliveryError) as error:
        technical_type("POSTGRESQL", specification)
    assert error.value.code in {"INVALID_TYPE_PARAMETERS", "UNSUPPORTED_TARGET_TYPE"}


def test_decimal_conversion_never_round_trips_through_float():
    specification = column(
        "amount", "amount", "DECIMAL", nullable=False, precision=20, scale=10
    )

    from_text = convert_value("1234567890.1234567890", specification)
    from_float = convert_value(0.1, specification)

    assert type(from_text) is Decimal
    assert from_text == Decimal("1234567890.1234567890")
    assert type(from_float) is Decimal
    assert from_float == Decimal("0.1")
    assert from_float != Decimal.from_float(0.1)


def test_decimal_conversion_uses_defaults_when_optional_model_fields_are_none():
    specification = column(
        "amount", "amount", "DECIMAL", nullable=False, precision=None, scale=None
    )

    assert convert_value("123.45", specification) == Decimal("123.45")


@pytest.mark.parametrize(
    ("value", "precision", "scale", "expected"),
    [
        ("-12.30", 4, 2, Decimal("-12.30")),
        ("999", 3, 0, Decimal(999)),
        ("0.001", 3, 3, Decimal("0.001")),
        ("1E+3", 4, 0, Decimal("1E+3")),
    ],
)
def test_decimal_conversion_honors_precision_and_scale(value, precision, scale, expected):
    specification = column(
        "amount", "amount", "DECIMAL", nullable=False, precision=precision, scale=scale
    )
    assert convert_value(value, specification) == expected


@pytest.mark.parametrize(
    ("value", "precision", "scale", "expected_code"),
    [
        ("1000", 3, 0, "DECIMAL_PRECISION_EXCEEDED"),
        ("1.234", 4, 2, "DECIMAL_PRECISION_EXCEEDED"),
        ("NaN", 38, 10, "VALUE_TYPE_MISMATCH"),
        ("Infinity", 38, 10, "VALUE_TYPE_MISMATCH"),
        ("not-a-decimal", 38, 10, "VALUE_TYPE_MISMATCH"),
    ],
)
def test_decimal_conversion_rejects_overflow_nonfinite_and_invalid_values(
    value, precision, scale, expected_code
):
    specification = column(
        "amount", "amount", "DECIMAL", nullable=False, precision=precision, scale=scale
    )
    with pytest.raises(DeliveryError) as error:
        convert_value(value, specification)
    assert error.value.code == expected_code


def test_length_and_nullability_are_enforced_before_driver_calls():
    text = column("name", "name", "STRING", nullable=False, length=4)
    assert convert_value("José", text) == "José"
    with pytest.raises(DeliveryError) as length_error:
        convert_value("Joséphine", text)
    assert length_error.value.code == "STRING_LENGTH_EXCEEDED"

    assert convert_value(None, text | {"nullable": True}) is None
    with pytest.raises(DeliveryError) as null_error:
        convert_value(None, text)
    assert null_error.value.code == "NULLABILITY_MISMATCH"


def test_string_length_uses_utf16_units_and_rejects_unpaired_surrogates():
    one_unit = column("name", "name", "STRING", nullable=False, length=1)
    two_units = one_unit | {"length": 2}

    with pytest.raises(DeliveryError) as too_long:
        convert_value("😀", one_unit)
    assert too_long.value.code == "STRING_LENGTH_EXCEEDED"
    assert convert_value("😀", two_units) == "😀"

    with pytest.raises(DeliveryError) as invalid_unicode:
        convert_value("\ud800", two_units)
    assert invalid_unicode.value.code == "VALUE_TYPE_MISMATCH"


def test_timestamp_conversion_requires_an_explicit_offset():
    specification = column(
        "observed_at", "event_time", "TIMESTAMP", nullable=False
    )

    assert convert_value(
        "2026-09-23T01:02:03.789123+00:00", specification
    ) == datetime(2026, 9, 23, 1, 2, 3, 789123, tzinfo=UTC)
    with pytest.raises(DeliveryError) as error:
        convert_value("2026-09-23T01:02:03.789123", specification)
    assert error.value.code == "VALUE_TYPE_MISMATCH"


@pytest.mark.parametrize(
    "value",
    [
        "2026-09-23",
        "2026-09-23 01:02:03+00:00",
        "2026-09-23T01:02:03.1234567+00:00",
        "2026-09-23T01:02:03+14:01",
        "2026-09-23T01:02:03+05:30:15",
        "0001-01-01T00:00:00+14:00",
    ],
)
def test_timestamp_conversion_rejects_noncanonical_or_nonportable_values(value):
    specification = column(
        "observed_at", "event_time", "TIMESTAMP", nullable=False
    )

    with pytest.raises(DeliveryError) as error:
        convert_value(value, specification)
    assert error.value.code == "VALUE_TYPE_MISMATCH"


def test_timestamp_conversion_accepts_z_and_preserves_nonzero_offset():
    specification = column(
        "observed_at", "event_time", "TIMESTAMP", nullable=False
    )

    assert convert_value("2026-09-23T01:02:03Z", specification).utcoffset() == UTC.utcoffset(
        None
    )
    converted = convert_value("2026-09-23T01:02:03.123456-05:00", specification)
    assert converted.isoformat() == "2026-09-23T01:02:03.123456-05:00"


def test_prepare_rows_uses_mapping_order_and_typed_driver_values():
    columns = [
        column("quantity", "units", "INT64", nullable=False),
        column("amount", "total", "DECIMAL", nullable=False, precision=8, scale=2),
        column("ordered_on", "order_date", "DATE", nullable=False),
        column("observed_at", "event_time", "TIMESTAMP", nullable=False),
        column("enabled", "is_enabled", "BOOLEAN", nullable=False),
        column("note", "note", "STRING", length=20),
    ]
    records = [
        {
            "note": "  café Ω  ",
            "enabled": "1",
            "observed_at": "2026-09-22T12:34:56+00:00",
            "ordered_on": "2026-09-22",
            "amount": "12.30",
            "quantity": "+7",
        },
        {
            "quantity": 0,
            "amount": Decimal("0.00"),
            "ordered_on": date(2026, 9, 23),
            "observed_at": datetime(2026, 9, 23, 1, 2, 3, tzinfo=UTC),
            "enabled": False,
        },
    ]

    rows = prepare_rows(records, columns)

    assert rows == [
        (
            7,
            Decimal("12.30"),
            date(2026, 9, 22),
            datetime.fromisoformat("2026-09-22T12:34:56+00:00"),
            True,
            "  café Ω  ",
        ),
        (
            0,
            Decimal("0.00"),
            date(2026, 9, 23),
            datetime(2026, 9, 23, 1, 2, 3, tzinfo=UTC),
            False,
            None,
        ),
    ]
    assert type(rows[0][1]) is Decimal


def test_prepare_rows_treats_missing_source_as_null_and_enforces_nullability():
    nullable = column("optional", "optional", "STRING")
    required = column("required", "required", "STRING", nullable=False)

    assert prepare_rows([{}], [nullable]) == [(None,)]
    with pytest.raises(DeliveryError) as error:
        prepare_rows([{}], [required])
    assert error.value.code == "NULLABILITY_MISMATCH"


@pytest.mark.parametrize(
    "value",
    ["", " leading", "trailing ", "line\nbreak", "null\x00byte", "delete\x7f", "x" * 129, 7],
)
def test_identifier_validation_rejects_empty_controls_padding_length_and_non_strings(value):
    with pytest.raises(DeliveryError) as error:
        validate_identifier(value)
    assert error.value.code == "INVALID_IDENTIFIER"


@pytest.mark.parametrize("identifier", ["a" * 64, "é" * 32])
def test_postgresql_rejects_identifiers_the_server_would_silently_truncate(
    monkeypatch, identifier
):
    adapter = PostgreSQLDataSink(settings())
    connection, cursor = attach_connection(monkeypatch, adapter)

    with pytest.raises(DeliveryError) as error:
        adapter.deliver(
            [{"id": 1}],
            [column("id", "id", "INT64", nullable=False)],
            {
                "mode": "EXISTING_TABLE",
                "schema_name": identifier,
                "table_name": "facts",
            },
            "APPEND",
            [],
        )

    assert error.value.code == "INVALID_IDENTIFIER"
    assert connection.commit_count == 0
    assert cursor.executions == []
    assert cursor.executemany_calls == []


def test_postgresql_local_prepare_rejects_identifier_before_opening_connection(
    monkeypatch,
):
    adapter = PostgreSQLDataSink(settings())
    opened = False

    @contextmanager
    def forbidden_connection():
        nonlocal opened
        opened = True
        yield MagicMock()

    monkeypatch.setattr(adapter, "_connection", forbidden_connection)

    with pytest.raises(DeliveryError) as error:
        adapter.deliver(
            [{"label": "safe"}],
            [column("label", "é" * 40, "STRING")],
            {
                "mode": "CREATE_TABLE",
                "schema_name": "analytics",
                "table_name": "facts",
                "create_schema": False,
            },
            "CREATE_AND_LOAD",
            [],
        )

    assert error.value.code == "INVALID_IDENTIFIER"
    assert opened is False


def test_postgresql_rejects_overlong_target_column_before_writing(monkeypatch):
    adapter = PostgreSQLDataSink(settings())
    connection, cursor = attach_connection(monkeypatch, adapter)

    with pytest.raises(DeliveryError) as error:
        adapter.deliver(
            [{"id": 1}],
            [column("id", "é" * 32, "INT64", nullable=False)],
            {"mode": "EXISTING_TABLE", "schema_name": "public", "table_name": "facts"},
            "APPEND",
            [],
        )

    assert error.value.code == "INVALID_IDENTIFIER"
    assert connection.commit_count == 0
    assert cursor.executemany_calls == []


def test_postgresql_identifier_quoting_escapes_every_identifier_boundary():
    adapter = PostgreSQLDataSink(settings())
    cursor = FakeCursor()
    adapter._insert(
        cursor,
        'sch"ema',
        'orders"; DROP TABLE audit; --',
        [column("id", 'id"number', "INT64")],
        [(7,)],
    )

    assert cursor.executemany_calls == [
        (
            (
                'INSERT INTO "sch""ema"."orders""; DROP TABLE audit; --" '
                '("id""number") VALUES (%s)'
            ),
            [(7,)],
        )
    ]


def test_sqlserver_identifier_quoting_escapes_every_identifier_boundary():
    assert quote_sqlserver_identifier("schema]name") == "[schema]]name]"
    adapter = SQLServerDataSink(settings("SQLSERVER"))
    cursor = FakeCursor()
    adapter._insert(
        cursor,
        "schema]name",
        "orders]; DROP TABLE audit; --",
        [column("id", "id]number", "INT64")],
        [(7,)],
    )

    assert cursor.executemany_calls == [
        (
            (
                "INSERT INTO [schema]]name].[orders]]; DROP TABLE audit; --] "
                "([id]]number]) VALUES (%s)"
            ),
            [(7,)],
        )
    ]


@pytest.mark.parametrize(
    ("sink_type", "adapter_type", "expected_sql"),
    [
        (
            "POSTGRESQL",
            PostgreSQLDataSink,
            'INSERT INTO "sales"."order""lines" ("line""id", "amount") VALUES (%s, %s)',
        ),
        (
            "SQLSERVER",
            SQLServerDataSink,
            "INSERT INTO [sales].[order]]lines] ([line]]id], [amount]) VALUES (%s, %s)",
        ),
    ],
)
def test_append_parameterizes_rows_commits_and_returns_evidence(
    monkeypatch, sink_type, adapter_type, expected_sql
):
    adapter = adapter_type(settings(sink_type))
    cursor = FakeCursor(fetchone=((0,),) if sink_type == "SQLSERVER" else ())
    connection, cursor = attach_connection(monkeypatch, adapter, cursor)
    columns = [
        column("line_id", 'line"id' if sink_type == "POSTGRESQL" else "line]id", "INT64"),
        column("amount", "amount", "DECIMAL", precision=8, scale=2),
    ]
    table_name = 'order"lines' if sink_type == "POSTGRESQL" else "order]lines"

    result = adapter.deliver(
        [{"line_id": "7", "amount": "12.30"}],
        columns,
        {"mode": "EXISTING_TABLE", "schema_name": "sales", "table_name": table_name},
        "APPEND",
        [],
    )

    if sink_type == "POSTGRESQL":
        assert len(cursor.executions) == 1
        assert cursor.executions[0][0] == 'LOCK TABLE "sales"."order""lines" IN ROW EXCLUSIVE MODE'
    else:
        assert "TABLOCKX" in cursor.executions[0][0]
        assert "i.ignore_dup_key = 1" in cursor.executions[1][0]
    assert cursor.executemany_calls == [(expected_sql, [(7, Decimal("12.30"))])]
    assert connection.commit_count == 1
    assert connection.rollback_count == 0
    assert result.rows_attempted == result.rows_written == result.rows_inserted == 1
    assert result.rows_updated == 0
    assert result.bytes_sent == len(b"7") + len(b"12.30")


@pytest.mark.parametrize(
    ("sink_type", "adapter_type", "expected_delete", "expected_insert"),
    [
        (
            "POSTGRESQL",
            PostgreSQLDataSink,
            'DELETE FROM "staging"."events"',
            'INSERT INTO "staging"."events" ("event_id") VALUES (%s)',
        ),
        (
            "SQLSERVER",
            SQLServerDataSink,
            "DELETE FROM [staging].[events]",
            "INSERT INTO [staging].[events] ([event_id]) VALUES (%s)",
        ),
    ],
)
def test_overwrite_deletes_then_inserts_in_one_transaction(
    monkeypatch, sink_type, adapter_type, expected_delete, expected_insert
):
    adapter = adapter_type(settings(sink_type))
    guard_results = (
        ((False,),) if sink_type == "POSTGRESQL" else ((0,), (1,), (0,))
    )
    cursor = FakeCursor(fetchone=guard_results)
    connection, cursor = attach_connection(monkeypatch, adapter, cursor)

    result = adapter.deliver(
        [{"event_id": "42"}],
        [column("event_id", "event_id", "INT64", nullable=False)],
        {"mode": "EXISTING_TABLE", "schema_name": "staging", "table_name": "events"},
        "OVERWRITE",
        [],
    )

    assert (expected_delete, None) in cursor.executions
    assert cursor.executemany_calls == [(expected_insert, [(42,)])]
    assert connection.commit_count == 1
    assert result.rows_inserted == 1
    assert result.rows_updated == 0


def test_postgresql_upsert_uses_composite_conflict_key_and_updates_non_keys(monkeypatch):
    adapter = PostgreSQLDataSink(settings())
    cursor = FakeCursor(fetchone=((['tenant_id', 'external_id'],),))
    connection, cursor = attach_connection(monkeypatch, adapter, cursor)
    columns = [
        column("tenant", "tenant_id", "INT64", nullable=False),
        column("external", "external_id", "STRING", nullable=False),
        column("name", "display_name", "STRING"),
    ]

    result = adapter.deliver(
        [{"tenant": 7, "external": "A-1", "name": "Ana"}],
        columns,
        {
            "mode": "EXISTING_TABLE",
            "schema_name": "crm",
            "table_name": "customers",
            "_upsert_constraint": "customers_tenant_external_key",
        },
        "UPSERT",
        ["tenant_id", "external_id"],
    )

    assert cursor.executions[1:3] == [
        (
            (
                'CREATE TEMP TABLE "trackvance_upsert_keys" ON COMMIT DROP AS '
                'SELECT "tenant_id", "external_id" FROM "crm"."customers" WITH NO DATA'
            ),
            None,
        ),
        (
            (
                'CREATE UNIQUE INDEX ON "trackvance_upsert_keys" '
                '("tenant_id", "external_id")'
            ),
            None,
        ),
    ]
    assert "FROM pg_constraint con" in cursor.executions[3][0]
    assert cursor.executemany_calls == [
        (
            (
                'INSERT INTO "trackvance_upsert_keys" '
                '("tenant_id", "external_id") VALUES (%s, %s)'
            ),
            [(7, "A-1")],
        ),
        (
            (
                'INSERT INTO "crm"."customers" '
                '("tenant_id", "external_id", "display_name") '
                'VALUES (%s, %s, %s) ON CONFLICT ON CONSTRAINT '
                '"customers_tenant_external_key" '
                'DO UPDATE SET "display_name" = EXCLUDED."display_name"'
            ),
            [(7, "A-1", "Ana")],
        )
    ]
    assert connection.commit_count == 1
    assert result.rows_inserted is None
    assert result.rows_updated is None


def test_postgresql_upsert_with_only_composite_keys_uses_do_nothing(monkeypatch):
    adapter = PostgreSQLDataSink(settings())
    cursor = FakeCursor(fetchone=((['tenant_id', 'external_id'],),))
    connection, cursor = attach_connection(monkeypatch, adapter, cursor)
    columns = [
        column("tenant", "tenant_id", "INT64", nullable=False),
        column("external", "external_id", "STRING", nullable=False),
    ]

    adapter.deliver(
        [{"tenant": 7, "external": "A-1"}],
        columns,
        {
            "mode": "EXISTING_TABLE",
            "schema_name": "crm",
            "table_name": "customers",
            "_upsert_constraint": "customers_tenant_external_key",
        },
        "UPSERT",
        ["tenant_id", "external_id"],
    )

    assert len(cursor.executemany_calls) == 2
    statement, rows = cursor.executemany_calls[1]
    assert statement.endswith(
        'ON CONFLICT ON CONSTRAINT "customers_tenant_external_key" DO NOTHING'
    )
    assert rows == [(7, "A-1")]
    assert connection.commit_count == 1


def test_postgresql_upsert_rejects_homonymous_constraint_drift_before_target_write(
    monkeypatch,
):
    adapter = PostgreSQLDataSink(settings())
    cursor = FakeCursor(fetchone=((['different_key'],),))
    connection, cursor = attach_connection(monkeypatch, adapter, cursor)

    with pytest.raises(DeliveryError) as error:
        adapter.deliver(
            [{"tenant": 7, "external": "A-1"}],
            [
                column("tenant", "tenant_id", "INT64", nullable=False),
                column("external", "external_id", "STRING", nullable=False),
            ],
            {
                "mode": "EXISTING_TABLE",
                "schema_name": "crm",
                "table_name": "customers",
                "_upsert_constraint": "customers_tenant_external_key",
            },
            "UPSERT",
            ["tenant_id", "external_id"],
        )

    assert error.value.code == "UPSERT_CONSTRAINT_DRIFT"
    assert connection.commit_count == 0
    assert connection.rollback_count == 1
    assert not any(
        'INSERT INTO "crm"."customers"' in statement
        for statement, _rows in cursor.executemany_calls
    )


def test_postgresql_empty_append_still_locks_and_resolves_existing_target(monkeypatch):
    adapter = PostgreSQLDataSink(settings())
    connection, cursor = attach_connection(monkeypatch, adapter)

    result = adapter.deliver(
        [],
        [column("id", "id", "INT64", nullable=False)],
        {
            "mode": "EXISTING_TABLE",
            "schema_name": "delivery",
            "table_name": "facts",
        },
        "APPEND",
        [],
    )

    assert cursor.executions == [
        ('LOCK TABLE "delivery"."facts" IN ROW EXCLUSIVE MODE', None)
    ]
    assert cursor.executemany_calls == []
    assert connection.commit_count == 1
    assert result.rows_written == 0


def test_postgresql_upsert_collation_collision_rolls_back_before_target_mutation(
    monkeypatch,
):
    adapter = PostgreSQLDataSink(settings())
    cursor = FakeCursor(
        executemany_error=psycopg.errors.UniqueViolation(
            "duplicate key under target collation private-password"
        )
    )
    connection, cursor = attach_connection(monkeypatch, adapter, cursor)

    with pytest.raises(DeliveryError) as error:
        adapter.deliver(
            [
                {"external": "A", "name": "First"},
                {"external": "a", "name": "Second"},
            ],
            [
                column("external", "external_id", "STRING", nullable=False),
                column("name", "display_name", "STRING"),
            ],
            {
                "mode": "EXISTING_TABLE",
                "schema_name": "crm",
                "table_name": "customers",
                "_upsert_constraint": "customers_external_key",
            },
            "UPSERT",
            ["external_id"],
        )

    assert error.value.code == "DESTINATION_CONSTRAINT_VIOLATION"
    assert "private-password" not in str(error.value)
    assert connection.commit_count == 0
    assert connection.rollback_count == 1
    assert not any(
        'INSERT INTO "crm"."customers"' in statement
        for statement, _rows in cursor.executemany_calls
    )


def test_sqlserver_upsert_uses_composite_keys_and_reports_update_insert_counts(monkeypatch):
    adapter = SQLServerDataSink(settings("SQLSERVER"))
    cursor = FakeCursor(
        fetchone=((0,), (1,), (0,)),
        # A trigger using SET NOCOUNT ON may leave DB-API rowcount at -1;
        # the adapter must use its explicit SELECT @@ROWCOUNT result instead.
        rowcounts=(-1, -1, -1, -1, -1),
    )
    connection, cursor = attach_connection(monkeypatch, adapter, cursor)
    columns = [
        column("tenant", "tenant_id", "INT64", nullable=False),
        column("external", "external_id", "STRING", nullable=False),
        column("name", "display_name", "STRING"),
    ]

    result = adapter.deliver(
        [
            {"tenant": 7, "external": "A-1", "name": "Existing"},
            {"tenant": 7, "external": "A-2", "name": "New"},
        ],
        columns,
        {"mode": "EXISTING_TABLE", "schema_name": "crm", "table_name": "customers"},
        "UPSERT",
        ["tenant_id", "external_id"],
    )

    update = (
        "UPDATE [crm].[customers] WITH (UPDLOCK, HOLDLOCK) SET [display_name] = %s "
        "WHERE [tenant_id] = %s AND [external_id] = %s; SELECT @@ROWCOUNT"
    )
    insert = (
        "INSERT INTO [crm].[customers] ([tenant_id], [external_id], [display_name]) "
        "VALUES (%s, %s, %s)"
    )
    assert cursor.executions[2:4] == [
        (
            (
                "SELECT TOP (0) source.[tenant_id], source.[external_id] "
                "INTO #trackvance_upsert_keys FROM [crm].[customers] AS source "
                "LEFT JOIN [crm].[customers] AS no_identity ON 1 = 0"
            ),
            None,
        ),
        (
            (
                "CREATE UNIQUE INDEX [UX_trackvance_upsert_keys] "
                "ON #trackvance_upsert_keys ([tenant_id], [external_id])"
            ),
            None,
        ),
    ]
    assert cursor.executemany_calls == [
        (
            (
                "INSERT INTO #trackvance_upsert_keys ([tenant_id], [external_id]) "
                "VALUES (%s, %s)"
            ),
            [(7, "A-1"), (7, "A-2")],
        )
    ]
    assert cursor.executions[4:] == [
        (update, ("Existing", 7, "A-1")),
        (update, ("New", 7, "A-2")),
        (insert, (7, "A-2", "New")),
    ]
    assert connection.commit_count == 1
    assert result.rows_inserted == 1
    assert result.rows_updated == 1


def test_sqlserver_upsert_collation_collision_rolls_back_before_target_mutation(
    monkeypatch,
):
    adapter = SQLServerDataSink(settings("SQLSERVER"))
    cursor = FakeCursor(
        fetchone=((0,),),
        executemany_error=pymssql.IntegrityError(
            2601, "duplicate key under destination collation private-password"
        )
    )
    connection, cursor = attach_connection(monkeypatch, adapter, cursor)
    columns = [
        column("external", "external_id", "STRING", nullable=False),
        column("name", "display_name", "STRING"),
    ]

    with pytest.raises(DeliveryError) as error:
        adapter.deliver(
            [
                {"external": "A", "name": "First"},
                {"external": "a", "name": "Second"},
            ],
            columns,
            {
                "mode": "EXISTING_TABLE",
                "schema_name": "crm",
                "table_name": "customers",
            },
            "UPSERT",
            ["external_id"],
        )

    assert error.value.code == "DESTINATION_CONSTRAINT_VIOLATION"
    assert error.value.ambiguous is False
    assert "private-password" not in str(error.value)
    assert connection.commit_count == 0
    assert connection.rollback_count == 1
    assert not any(
        statement.startswith(("UPDATE ", "INSERT INTO [crm].[customers]"))
        for statement, _params in cursor.executions
    )


def test_sqlserver_rejects_ignore_dup_key_inside_write_transaction(monkeypatch):
    adapter = SQLServerDataSink(settings("SQLSERVER"))
    cursor = FakeCursor(fetchone=((1,),))
    connection, cursor = attach_connection(monkeypatch, adapter, cursor)

    with pytest.raises(DeliveryError) as error:
        adapter.deliver(
            [{"id": 7}],
            [column("id", "id", "INT64", nullable=False)],
            {
                "mode": "EXISTING_TABLE",
                "schema_name": "delivery",
                "table_name": "facts",
            },
            "APPEND",
            [],
        )

    assert error.value.code == "UNSAFE_IGNORE_DUP_KEY"
    assert connection.commit_count == 0
    assert connection.rollback_count == 1
    assert cursor.executemany_calls == []
    assert "i.ignore_dup_key = 1" in cursor.executions[1][0]


def test_postgresql_overwrite_rejects_active_row_security_before_delete(monkeypatch):
    adapter = PostgreSQLDataSink(settings())
    cursor = FakeCursor(fetchone=((True,),))
    connection, cursor = attach_connection(monkeypatch, adapter, cursor)

    with pytest.raises(DeliveryError) as error:
        adapter.deliver(
            [{"id": 7}],
            [column("id", "id", "INT64", nullable=False)],
            {
                "mode": "EXISTING_TABLE",
                "schema_name": "delivery",
                "table_name": "facts",
            },
            "OVERWRITE",
            [],
        )

    assert error.value.code == "UNSAFE_ROW_SECURITY"
    assert connection.commit_count == 0
    assert connection.rollback_count == 1
    assert not any(
        statement.startswith("DELETE FROM") for statement, _params in cursor.executions
    )


def test_sqlserver_overwrite_rejects_filter_security_policy_before_delete(
    monkeypatch,
):
    adapter = SQLServerDataSink(settings("SQLSERVER"))
    cursor = FakeCursor(fetchone=((0,), (1,), (1,)))
    connection, cursor = attach_connection(monkeypatch, adapter, cursor)

    with pytest.raises(DeliveryError) as error:
        adapter.deliver(
            [{"id": 7}],
            [column("id", "id", "INT64", nullable=False)],
            {
                "mode": "EXISTING_TABLE",
                "schema_name": "delivery",
                "table_name": "facts",
            },
            "OVERWRITE",
            [],
        )

    assert error.value.code == "UNSAFE_ROW_SECURITY"
    assert connection.commit_count == 0
    assert connection.rollback_count == 1
    assert not any(
        statement.startswith("DELETE FROM") for statement, _params in cursor.executions
    )


def test_sqlserver_overwrite_fails_closed_without_security_metadata_visibility(
    monkeypatch,
):
    adapter = SQLServerDataSink(settings("SQLSERVER"))
    cursor = FakeCursor(fetchone=((0,), (0,)))
    connection, cursor = attach_connection(monkeypatch, adapter, cursor)

    with pytest.raises(DeliveryError) as error:
        adapter.deliver(
            [],
            [column("id", "id", "INT64", nullable=False)],
            {
                "mode": "EXISTING_TABLE",
                "schema_name": "delivery",
                "table_name": "facts",
            },
            "OVERWRITE",
            [],
        )

    assert error.value.code == "SECURITY_METADATA_NOT_VISIBLE"
    assert connection.commit_count == 0
    assert connection.rollback_count == 1
    assert not any(
        "sys.security_predicates" in statement
        for statement, _params in cursor.executions
    )


def test_postgresql_metadata_excludes_deferrable_upsert_arbiters(monkeypatch):
    adapter = PostgreSQLDataSink(settings())
    cursor = FakeCursor(
        fetchall=(
            (
                (
                    "id",
                    "bigint",
                    "int8",
                    False,
                    None,
                    False,
                    False,
                    None,
                    64,
                    0,
                    None,
                ),
            ),
            (("PRIMARY KEY", ["id"], "records_pkey"),),
        ),
        fetchone=((False,),),
    )
    attach_connection(monkeypatch, adapter, cursor)

    metadata = adapter.table_metadata("delivery", "records")

    assert metadata["constraints"] == [
        {"type": "PRIMARY_KEY", "columns": ["id"], "name": "records_pkey"}
    ]
    assert metadata["row_security_active"] is False
    assert "NOT con.condeferrable" in cursor.executions[1][0]
    assert "c.datetime_precision" in cursor.executions[0][0]


def test_sqlserver_metadata_exposes_character_length_not_native_storage_bytes(monkeypatch):
    adapter = SQLServerDataSink(settings("SQLSERVER"))
    cursor = FakeCursor(
        fetchall=(
            (
                ("happened_on", "date", False, None, False, False, 3, 10, 0, None),
                (
                    "label",
                    "nvarchar",
                    True,
                    None,
                    False,
                    False,
                    320,
                    0,
                    0,
                    "Latin1_General_100_CI_AS_SC",
                ),
                (
                    "description",
                    "nvarchar",
                    True,
                    None,
                    False,
                    False,
                    -1,
                    0,
                    0,
                    "Latin1_General_100_CI_AS_SC",
                ),
                (
                    "recorded_at",
                    "datetime2",
                    False,
                    None,
                    False,
                    False,
                    8,
                    27,
                    6,
                    None,
                ),
            ),
            (),
        ),
        fetchone=((0,),),
    )
    attach_connection(monkeypatch, adapter, cursor)

    metadata = adapter.table_metadata("delivery", "records")

    assert [item["length"] for item in metadata["columns"]] == [
        None,
        160,
        None,
        None,
    ]
    assert [item["logical_type"] for item in metadata["columns"]] == [
        "DATE",
        "STRING",
        "STRING",
        "TIMESTAMP",
    ]
    assert [item["datetime_precision"] for item in metadata["columns"]] == [
        None,
        None,
        None,
        6,
    ]
    assert metadata["columns"][1]["collation"] == "Latin1_General_100_CI_AS_SC"
    assert metadata["filter_security_policy"] is False
    assert "c.generated_always_type <> 0" in cursor.executions[0][0]
    constraint_query = cursor.executions[1][0]
    assert "i.is_disabled = 0" in constraint_query
    assert "i.is_hypothetical = 0" in constraint_query
    assert "i.has_filter = 0" in constraint_query


def test_sqlserver_metadata_exposes_ignore_dup_key_unique_index(monkeypatch):
    adapter = SQLServerDataSink(settings("SQLSERVER"))
    cursor = FakeCursor(
        fetchall=(
            (("id", "bigint", False, None, False, False, 8, 19, 0, None),),
            ((False, True, 1, "id", "ux_facts_id", True),),
        ),
        fetchone=((0,),),
    )
    attach_connection(monkeypatch, adapter, cursor)

    metadata = adapter.table_metadata("delivery", "facts")

    assert metadata["constraints"] == [
        {
            "type": "UNIQUE",
            "columns": ["id"],
            "unique": True,
            "ignore_duplicate_keys": True,
        }
    ]


def test_sqlserver_schema_listing_does_not_hide_object_level_grants(monkeypatch):
    adapter = SQLServerDataSink(settings("SQLSERVER"))
    cursor = FakeCursor(fetchall=((('object_grant_only',),),))
    attach_connection(monkeypatch, adapter, cursor)

    assert adapter.schemas() == ["object_grant_only"]
    statement, parameters = cursor.executions[0]
    assert "HAS_PERMS_BY_NAME" not in statement
    assert parameters is None


@pytest.mark.parametrize(
    ("table_allowed", "temporary_allowed", "expected"),
    [(1, 1, True), (0, 1, False), (1, 0, False)],
)
def test_postgresql_upsert_requires_select_and_temporary_privileges(
    monkeypatch, table_allowed, temporary_allowed, expected
):
    adapter = PostgreSQLDataSink(settings())
    cursor = FakeCursor(
        fetchone=((1, 1), (table_allowed,), (temporary_allowed,))
    )
    attach_connection(monkeypatch, adapter, cursor)

    result = adapter.permissions(
        {
            "mode": "EXISTING_TABLE",
            "schema_name": "delivery",
            "table_name": "facts",
            "create_schema": False,
        },
        "UPSERT",
    )

    assert result["target_exists"] is True
    assert result["allowed"] is expected
    assert cursor.executions[1][1][0] == "INSERT,UPDATE,SELECT"
    assert "TEMPORARY" in cursor.executions[2][0]


def test_sqlserver_new_schema_requires_create_schema_and_create_table(monkeypatch):
    adapter = SQLServerDataSink(settings("SQLSERVER"))
    cursor = FakeCursor(fetchone=((None,), (1, 0)))
    attach_connection(monkeypatch, adapter, cursor)

    result = adapter.permissions(
        {
            "mode": "CREATE_TABLE",
            "schema_name": "new_delivery",
            "table_name": "facts",
            "create_schema": True,
        },
        "CREATE_AND_LOAD",
    )

    assert result["create_schema"] is True
    assert result["create_table"] is False


def test_sqlserver_existing_schema_needs_database_create_table_permission(monkeypatch):
    adapter = SQLServerDataSink(settings("SQLSERVER"))
    cursor = FakeCursor(fetchone=((7,), (1,), (0,)))
    attach_connection(monkeypatch, adapter, cursor)

    result = adapter.permissions(
        {
            "mode": "CREATE_TABLE",
            "schema_name": "delivery",
            "table_name": "facts",
            "create_schema": False,
        },
        "CREATE_AND_LOAD",
    )

    assert result["schema_exists"] is True
    assert result["create_table"] is False


def test_sqlserver_upsert_preflight_requires_select_for_key_predicates(monkeypatch):
    adapter = SQLServerDataSink(settings("SQLSERVER"))
    cursor = FakeCursor(fetchone=((7,), (1,), (1,), (0,), (1,)))
    attach_connection(monkeypatch, adapter, cursor)

    result = adapter.permissions(
        {
            "mode": "EXISTING_TABLE",
            "schema_name": "delivery",
            "table_name": "facts",
            "create_schema": False,
        },
        "UPSERT",
    )

    assert result["target_exists"] is True
    assert result["allowed"] is False
    assert any(
        parameters == ("[delivery].[facts]", "SELECT")
        for _statement, parameters in cursor.executions
    )


def test_sqlserver_overwrite_preflight_requires_security_metadata_visibility(
    monkeypatch,
):
    adapter = SQLServerDataSink(settings("SQLSERVER"))
    cursor = FakeCursor(fetchone=((7,), (1,), (1,), (1,), (0,), (1,)))
    attach_connection(monkeypatch, adapter, cursor)

    result = adapter.permissions(
        {
            "mode": "EXISTING_TABLE",
            "schema_name": "delivery",
            "table_name": "facts",
            "create_schema": False,
        },
        "OVERWRITE",
    )

    assert result["target_exists"] is True
    assert result["security_metadata_visible"] is False
    assert result["allowed"] is False


@pytest.mark.parametrize(
    ("sink_type", "adapter_type", "expected_create", "expected_insert"),
    [
        (
            "POSTGRESQL",
            PostgreSQLDataSink,
            (
                'CREATE TABLE "analytics"."daily facts" ("id" BIGINT NOT NULL, '
                '"amount" NUMERIC(12,2) NULL, "label" VARCHAR(20) NULL)'
            ),
            (
                'INSERT INTO "analytics"."daily facts" ("id", "amount", "label") '
                "VALUES (%s, %s, %s)"
            ),
        ),
        (
            "SQLSERVER",
            SQLServerDataSink,
            (
                "CREATE TABLE [analytics].[daily facts] ([id] BIGINT NOT NULL, "
                "[amount] DECIMAL(12,2) NULL, [label] NVARCHAR(20) "
                "COLLATE Latin1_General_100_CI_AS_SC NULL)"
            ),
            (
                "INSERT INTO [analytics].[daily facts] ([id], [amount], [label]) "
                "VALUES (%s, %s, %s)"
            ),
        ),
    ],
)
def test_create_and_load_checks_existence_creates_typed_nullable_table_and_loads(
    monkeypatch, sink_type, adapter_type, expected_create, expected_insert
):
    adapter = adapter_type(settings(sink_type))
    cursor = FakeCursor(fetchone=((True,), (False,)))
    connection, cursor = attach_connection(monkeypatch, adapter, cursor)
    columns = [
        column("id", "id", "INT64", nullable=False),
        column("amount", "amount", "DECIMAL", precision=12, scale=2),
        column("label", "label", "STRING", length=20),
    ]

    result = adapter.deliver(
        [{"id": "1", "amount": "9.50", "label": None}],
        columns,
        {
            "mode": "CREATE_TABLE",
            "schema_name": "analytics",
            "table_name": "daily facts",
            "create_schema": False,
        },
        "CREATE_AND_LOAD",
        [],
    )

    assert expected_create in [statement for statement, _params in cursor.executions]
    assert cursor.executemany_calls == [(expected_insert, [(1, Decimal("9.50"), None)])]
    assert connection.commit_count == 1
    assert result.rows_inserted == 1
    assert result.rows_updated == 0


@pytest.mark.parametrize(
    ("sink_type", "adapter_type", "expected_schema"),
    [
        ("POSTGRESQL", PostgreSQLDataSink, 'CREATE SCHEMA "new""schema"'),
        ("SQLSERVER", SQLServerDataSink, "CREATE SCHEMA [new]]schema]"),
    ],
)
def test_create_and_load_quotes_new_schema(monkeypatch, sink_type, adapter_type, expected_schema):
    adapter = adapter_type(settings(sink_type))
    schema_absent = False if sink_type == "POSTGRESQL" else None
    cursor = FakeCursor(fetchone=((schema_absent,), (False,)))
    connection, cursor = attach_connection(monkeypatch, adapter, cursor)
    schema_name = 'new"schema' if sink_type == "POSTGRESQL" else "new]schema"

    adapter.deliver(
        [{"id": 1}],
        [column("id", "id", "INT64", nullable=False)],
        {
            "mode": "CREATE_TABLE",
            "schema_name": schema_name,
            "table_name": "facts",
            "create_schema": True,
        },
        "CREATE_AND_LOAD",
        [],
    )

    assert expected_schema in [statement for statement, _params in cursor.executions]
    assert connection.commit_count == 1


@pytest.mark.parametrize(
    ("sink_type", "adapter_type", "commit_error"),
    [
        (
            "POSTGRESQL",
            PostgreSQLDataSink,
            psycopg.OperationalError("private-password while committing"),
        ),
        (
            "SQLSERVER",
            SQLServerDataSink,
            pymssql.OperationalError("private-password while committing"),
        ),
    ],
)
def test_commit_failure_is_ambiguous_sanitized_and_rolled_back(
    monkeypatch, sink_type, adapter_type, commit_error
):
    adapter = adapter_type(settings(sink_type))
    cursor = FakeCursor(fetchone=((0,),) if sink_type == "SQLSERVER" else ())
    connection, _cursor = attach_connection(
        monkeypatch, adapter, cursor, commit_error=commit_error
    )

    with pytest.raises(DeliveryError) as error:
        adapter.deliver(
            [{"id": 1}],
            [column("id", "id", "INT64", nullable=False)],
            {"mode": "EXISTING_TABLE", "schema_name": "public", "table_name": "facts"},
            "APPEND",
            [],
        )

    assert error.value.code == "DESTINATION_COMMIT_UNKNOWN"
    assert error.value.ambiguous is True
    assert "private-password" not in str(error.value)
    assert error.value.__suppress_context__
    assert connection.commit_count == 1
    assert connection.rollback_count == 1


@pytest.mark.parametrize(
    ("sink_type", "adapter_type", "commit_error", "expected_code"),
    [
        (
            "POSTGRESQL",
            PostgreSQLDataSink,
            type(
                "SerializationRejected",
                (psycopg.Error,),
                {"sqlstate": "40001"},
            )("private-password while committing"),
            "DESTINATION_TIMEOUT",
        ),
        (
            "SQLSERVER",
            SQLServerDataSink,
            pymssql.OperationalError(1205, "private-password while committing"),
            "DESTINATION_TIMEOUT",
        ),
    ],
)
def test_commit_rejection_is_failed_not_unknown(
    monkeypatch, sink_type, adapter_type, commit_error, expected_code
):
    adapter = adapter_type(settings(sink_type))
    cursor = FakeCursor(fetchone=((0,),) if sink_type == "SQLSERVER" else ())
    connection, _cursor = attach_connection(
        monkeypatch, adapter, cursor, commit_error=commit_error
    )

    with pytest.raises(DeliveryError) as error:
        adapter.deliver(
            [{"id": 1}],
            [column("id", "id", "INT64", nullable=False)],
            {
                "mode": "EXISTING_TABLE",
                "schema_name": "public",
                "table_name": "facts",
            },
            "APPEND",
            [],
        )

    assert error.value.code == expected_code
    assert error.value.ambiguous is False
    assert "private-password" not in str(error.value)
    assert connection.commit_count == 1
    assert connection.rollback_count == 1
