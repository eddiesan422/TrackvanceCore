"""Deterministic count contracts; real engines are covered by delivery_cycle."""

from types import SimpleNamespace

import psycopg
import pytest
from test_data_sinks import FakeCursor, attach_connection, column, settings

from trackvance.data_sinks import (
    DeliveryError,
    PostgreSQLDataSink,
    SQLServerDataSink,
    _reported_rowcount,
)
from trackvance.delivery_metrics import delivery_metric_semantics


@pytest.mark.parametrize("resultsets,expected", [
    ([[(True,)], [(True,)]], (2, 0)),
    ([[(False,)], [(False,)]], (0, 2)),
    ([[(True,)], [(False,)]], (1, 1)),
    ([[], [(False,)]], (0, 1)),  # BEFORE INSERT trigger suppressed one row.
    ([[], []], (0, 0)),
])
def test_pg18_returning_classifies_only_engine_reported_actions(monkeypatch, resultsets, expected):
    adapter = PostgreSQLDataSink(settings())
    cursor = FakeCursor(fetchone=((['tenant', 'key'],),), fetchall=tuple(resultsets))
    connection, _ = attach_connection(monkeypatch, adapter, cursor)
    connection.info = SimpleNamespace(server_version=180000)
    result = adapter.deliver(
        [{"tenant": "A", "key": "1", "value": "one"},
         {"tenant": "A", "key": "2", "value": "two"}],
        [column(name, name, "STRING") for name in ("tenant", "key", "value")],
        {"mode": "EXISTING_TABLE", "schema_name": "public", "table_name": "records",
         "_upsert_constraint": "records_key"},
        "UPSERT", ["tenant", "key"],
    )
    assert (result.rows_inserted, result.rows_updated) == expected
    assert result.rows_attempted == result.rows_written == 2
    statement = cursor.executemany_calls[-1][0]
    assert "ON CONFLICT ON CONSTRAINT" in statement
    assert "RETURNING WITH (OLD AS trackvance_old)" in statement
    assert "trackvance_old IS NOT DISTINCT FROM NULL" in statement
    assert "xmax" not in statement
    assert connection.commit_count == 1


@pytest.mark.parametrize("version", [160000, 170000, 180000])
def test_empty_upsert_is_known_zero_not_unknown(monkeypatch, version):
    adapter = PostgreSQLDataSink(settings())
    connection, _ = attach_connection(monkeypatch, adapter)
    connection.info = SimpleNamespace(server_version=version)
    result = adapter.deliver([], [column("key", "key", "STRING")],
        {"mode": "EXISTING_TABLE", "schema_name": "public", "table_name": "records",
         "_upsert_constraint": "records_key"}, "UPSERT", ["key"])
    assert (result.rows_inserted, result.rows_updated) == (0, 0)
    assert result.rows_attempted == result.rows_written == 0


def test_pg18_rollback_never_returns_provisional_counts(monkeypatch):
    adapter = PostgreSQLDataSink(settings())
    cursor = FakeCursor(fetchone=((['key'],),), fetchall=([(True,)],))
    connection, _ = attach_connection(monkeypatch, adapter, cursor,
        commit_error=psycopg.errors.CheckViolation("private rollback detail"))
    connection.info = SimpleNamespace(server_version=180000)
    with pytest.raises(DeliveryError) as error:
        adapter.deliver([{"key": "A", "value": "v"}],
            [column(name, name, "STRING") for name in ("key", "value")],
            {"mode": "EXISTING_TABLE", "schema_name": "public", "table_name": "records",
             "_upsert_constraint": "records_key"}, "UPSERT", ["key"])
    assert error.value.code == "DESTINATION_CONSTRAINT_VIOLATION"
    assert not error.value.ambiguous
    assert connection.rollback_count == 1


def test_sqlserver_existing_key_only_upsert_reports_no_update(monkeypatch):
    adapter = SQLServerDataSink(settings("SQLSERVER"))
    connection, cursor = attach_connection(monkeypatch, adapter,
        FakeCursor(fetchone=((0,), (1,))))
    result = adapter.deliver([{"key": "A"}], [column("key", "key", "STRING")],
        {"mode": "EXISTING_TABLE", "schema_name": "dbo", "table_name": "records"},
        "UPSERT", ["key"])
    assert (result.rows_inserted, result.rows_updated) == (0, 0)
    assert result.rows_written == 1
    assert connection.commit_count == 1
    assert not any(statement.startswith("UPDATE ") for statement, _ in cursor.executions)


@pytest.mark.parametrize("reported,expected", [(-1, None), (0, 0), (7, 7), (None, None)])
def test_unavailable_driver_count_is_not_fabricated_zero(reported, expected):
    assert _reported_rowcount(SimpleNamespace(rowcount=reported)) == expected


def test_metric_semantics_are_explicit_additive_and_not_mutable_global_state():
    first = delivery_metric_semantics()
    assert first["physical_destination_rows"] == "NOT_MEASURED"
    assert first["rows_written"] == "SOURCE_ROWS_SUBMITTED_IN_COMMITTED_OPERATION"
    first["version"] = 99
    assert delivery_metric_semantics()["version"] == 1
