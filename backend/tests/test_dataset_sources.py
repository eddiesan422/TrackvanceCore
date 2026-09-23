from contextlib import contextmanager
from datetime import date, datetime
from decimal import Decimal
from unittest.mock import MagicMock

import polars as pl
import psycopg
import pymssql
import pytest

from trackvance import dataset_sources as sources
from trackvance.dataset_readers import DatasetSource
from trackvance.dataset_sources import (
    ConnectionSettings,
    PostgreSQLDatasetSource,
    SourceError,
    SQLServerDatasetSource,
    _column,
    _logical,
    configured_snapshot_limit,
    source_registry,
)


def settings(source_type="POSTGRESQL", **overrides):
    values = {"source_type": source_type, "host": "source.example.test", "port": 5432,
              "database": "dataset", "username": "reader", "password": "private-password"}
    return ConnectionSettings(**(values | overrides))


def test_snapshot_limit_default_override_and_invalid_values(monkeypatch):
    monkeypatch.delenv("TRACKVANCE_MAX_SNAPSHOT_BYTES", raising=False)
    assert configured_snapshot_limit() == 64 * 1024 * 1024
    assert configured_snapshot_limit(str(160 * 1024 * 1024)) == 160 * 1024 * 1024
    for invalid in ("0", "-1", "not-an-integer"):
        with pytest.raises(RuntimeError, match="entero positivo"):
            configured_snapshot_limit(invalid)


@pytest.mark.parametrize("overrides", [
    {"host": "postgres://host?password=secret"}, {"host": "host;user=admin"},
    {"port": 0}, {"port": True}, {"port": 65536}, {"username": "a\npassword"},
    {"options": {"query_timeout": 0}}, {"options": {"connect_timeout": True}},
    {"options": {"sslmode": "prefer"}}, {"options": {"password": "secret"}},
    {"options": {"query": "DELETE FROM data"}}, {"source_type": "UNKNOWN"},
    {"options": {"sslmode": []}}, {"options": {"sslmode": {"password": "secret"}}},
    {"source_type": "SQLSERVER", "options": {"encryption": []}},
])
def test_connection_parameters_allow_only_controlled_options(overrides):
    with pytest.raises(SourceError) as error:
        settings(**overrides)
    assert "private-password" not in str(error.value)
    assert "DELETE" not in str(error.value)


@pytest.mark.parametrize(("native", "expected"), [
    ("bigint", "INT64"), ("numeric(24,8)", "DECIMAL"), ("double precision", "DECIMAL"),
    ("date", "DATE"), ("timestamp(6) with time zone", "TIMESTAMP"),
    ("timestamptz(3)", "TIMESTAMP"), ("datetimeoffset(6)", "TIMESTAMP"),
    ("datetimeoffset(7)", "STRING"),
    ("timestamp without time zone", "STRING"), ("timestamp(6)", "STRING"),
    ("datetime", "STRING"), ("datetime2", "STRING"), ("datetime2(6)", "STRING"),
    ("smalldatetime", "STRING"), ("rowversion", "STRING"), ("nvarchar", "STRING"),
    ("boolean", "BOOLEAN"),
])
def test_native_schema_mapping(native, expected):
    assert _logical(native)[0] == expected
    assert _column("document_id", "bigint", False)["logical_type"] == "STRING"


@pytest.mark.parametrize(
    "native",
    ["timestamp without time zone", "timestamp(6)", "datetime", "datetime2", "smalldatetime"],
)
def test_naive_database_temporals_remain_publishable_as_exact_text(native):
    logical, frame_type = _logical(native)

    assert (logical, frame_type) == ("STRING", "String")
    assert _column("observed_at", native, True)["logical_type"] == "STRING"


def fake_source(monkeypatch, rows, columns=None):
    source = source_registry.create(settings(), "source_data", "records")
    assert isinstance(source, DatasetSource)
    cursor = MagicMock()
    cursor.fetchmany.side_effect = [rows, []]

    @contextmanager
    def connection():
        yield object()

    monkeypatch.setattr(source, "_connection", connection)
    monkeypatch.setattr(source, "_objects", lambda *_: [{"name": "records", "kind": "TABLE"}])
    monkeypatch.setattr(source, "_columns", lambda *_: columns or [_column("value", "text", True)])
    select = MagicMock(return_value=cursor)
    monkeypatch.setattr(source, "_select", select)
    return source, cursor, select


def test_common_representation_preserves_observed_values_and_native_schema(monkeypatch):
    columns = [_column("document_id", "varchar", False), _column("customer", "text", True),
               _column("amount", "numeric(18,4)", True), _column("date", "date", True),
               _column("at", "timestamp", True), _column("binary", "bytea", True)]
    rows = [("001234567", "  José Ω  ", Decimal("123.4500"), date(2026, 1, 2),
             datetime.fromisoformat("2026-01-02T13:14:00"), b"\x00\xff"), ("1234567", "", None, None, None, None)]
    source, cursor, select = fake_source(monkeypatch, rows, columns)
    result = source.read()
    assert result.row_count == 2
    assert result.frame.schema == dict.fromkeys([c["name"] for c in columns], pl.String)
    assert result.frame.rows() == [
        ("001234567", "  José Ω  ", "123.4500", "2026-01-02", "2026-01-02T13:14:00", "AP8="),
        ("1234567", "", None, None, None, None),
    ]
    assert result.native_schema["amount"] == "Decimal"
    assert result.metadata["source_native_schema"] == columns
    assert result.row_numbering == "SNAPSHOT_ROW"
    assert "password" not in str(result.reader_metadata)
    assert select.call_args.args[-1] == sources.MAX_ROWS + 1
    cursor.close.assert_called_once()


def test_preview_is_bounded_at_query_and_does_not_claim_full_count(monkeypatch):
    source, _, select = fake_source(monkeypatch, [("a",)])
    result = source.read({"limit": 1}, inspect=True)
    assert select.call_args.args[-1] == 1
    assert result.frame.height == 1 and result.row_count is None
    with pytest.raises(SourceError):
        source.read({"limit": 101}, inspect=True)
    with pytest.raises(SourceError):
        source.read({"limit": 1})


def test_snapshot_rejects_oversize_without_partial_result_and_closes_cursor(monkeypatch):
    monkeypatch.setattr(sources, "MAX_ROWS", 2)
    source, cursor, _ = fake_source(monkeypatch, [("a",), ("b",), ("c",)])
    with pytest.raises(SourceError, match="no se importaron datos parciales"):
        source.read()
    cursor.close.assert_called_once()


def test_byte_and_cell_bounds(monkeypatch):
    monkeypatch.setattr(sources, "MAX_SNAPSHOT_BYTES", 3)
    source, _, _ = fake_source(monkeypatch, [("éé",)])
    with pytest.raises(SourceError, match="snapshot"):
        source.read()
    monkeypatch.setattr(sources, "MAX_CELL_TEXT_BYTES", 1)
    source, _, _ = fake_source(monkeypatch, [("é",)])
    with pytest.raises(SourceError, match="celda"):
        source.read()


def test_undiscovered_object_is_never_queried(monkeypatch):
    source, _, select = fake_source(monkeypatch, [])
    source.object_name = "records; DROP TABLE records"
    with pytest.raises(SourceError, match="no existe o no permite"):
        source.read()
    select.assert_not_called()


def test_empty_source_retains_columns_from_metadata(monkeypatch):
    source, _, _ = fake_source(monkeypatch, [], [_column("amount", "decimal", True)])
    result = source.read()
    assert result.row_count == 0 and result.frame.columns == ["amount"]
    assert result.native_schema == {"amount": "Decimal"}


def test_postgres_read_only_and_quoted_identifiers(monkeypatch):
    connect = MagicMock()
    monkeypatch.setattr(sources.psycopg, "connect", connect)
    source = PostgreSQLDatasetSource(settings())
    source.test()
    connection = connect.return_value.__enter__.return_value
    assert connection.read_only is True
    assert connect.call_args.kwargs["sslmode"] == "require"
    source._select(connection, 's"name', 't"name', [_column('c"name', "text", False)], 3)
    query, params = connection.cursor.return_value.execute.call_args.args
    assert query.as_string() == 'SELECT "c""name" FROM "s""name"."t""name" LIMIT %s'
    assert params == (3,)


def test_sqlserver_readonly_quoted_identifiers_and_datetimeoffset(monkeypatch):
    connect = MagicMock()
    monkeypatch.setattr(sources.pymssql, "connect", connect)
    source = SQLServerDatasetSource(settings("SQLSERVER", port=1433))
    source.test()
    assert connect.call_args.kwargs["read_only"] is True
    assert connect.call_args.kwargs["encryption"] == "require"
    assert connect.call_args.kwargs["timeout"] == 30
    connection = connect.return_value
    connection.close.assert_called_once()
    connection.commit.assert_not_called()
    source._select(connection, "s]name", "t]name", [_column("at]offset", "datetimeoffset(6)", False)], 3)
    assert connection.cursor.return_value.execute.call_args.args == (
        "SELECT TOP (3) CONVERT(nvarchar(50), [at]]offset], 127) AS [at]]offset] FROM [s]]name].[t]]name]",
    )


def test_sqlserver_temporal_precision_is_preserved_without_inventing_an_offset():
    source = SQLServerDatasetSource(settings("SQLSERVER", port=1433))
    connection = MagicMock()
    cursor = connection.cursor.return_value.__enter__.return_value
    cursor.fetchall.return_value = [
        ("with_offset", "datetimeoffset", True, 34, 6, 10),
        ("exact_offset", "datetimeoffset", True, 34, 7, 10),
        ("naive", "datetime2", True, 27, 7, 8),
    ]

    columns = source._columns(connection, "dbo", "events")

    assert [(column["native_type"], column["logical_type"]) for column in columns] == [
        ("datetimeoffset(6)", "TIMESTAMP"),
        ("datetimeoffset(7)", "STRING"),
        ("datetime2", "STRING"),
    ]
    source._select(connection, "dbo", "events", columns, 3)
    expected_query = (
        "SELECT TOP (3) CONVERT(nvarchar(50), [with_offset], 127) AS [with_offset], "
        "CONVERT(nvarchar(50), [exact_offset], 127) AS [exact_offset], "
        "CONVERT(nvarchar(50), [naive], 126) AS [naive] FROM [dbo].[events]"
    )
    assert connection.cursor.return_value.execute.call_args.args == (
        expected_query,
    )


@pytest.mark.parametrize(("driver", "adapter", "failure"), [
    ("psycopg", PostgreSQLDatasetSource, psycopg.OperationalError("private-password in DSN")),
    ("pymssql", SQLServerDatasetSource, pymssql.OperationalError(18456, b"private-password in DSN")),
])
def test_driver_errors_are_sanitized_and_password_excluded_from_repr(monkeypatch, driver, adapter, failure):
    monkeypatch.setattr(getattr(sources, driver), "connect", MagicMock(side_effect=failure))
    config = settings("POSTGRESQL" if driver == "psycopg" else "SQLSERVER")
    assert config.password not in repr(config)
    with pytest.raises(SourceError) as error:
        adapter(config).test()
    assert "private-password" not in str(error.value)
    assert error.value.__suppress_context__


def test_sqlserver_close_failure_is_sanitized(monkeypatch):
    connect = MagicMock()
    connect.return_value.close.side_effect = pymssql.OperationalError(20003, b"private-password during close")
    monkeypatch.setattr(sources.pymssql, "connect", connect)
    with pytest.raises(SourceError) as error:
        SQLServerDatasetSource(settings("SQLSERVER")).test()
    assert error.value.code == "SOURCE_TIMEOUT"
    assert "private-password" not in str(error.value)
    assert error.value.__suppress_context__
    connect.return_value.close.assert_called_once()
