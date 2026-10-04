import tracemalloc
from contextlib import contextmanager
from decimal import Decimal
from unittest.mock import MagicMock

import pytest

from trackvance.batch_readers import FileBatchReader
from trackvance.dataset_readers import (
    MAX_CELL_TEXT_BYTES,
    DatasetCellLimit,
    _cell_text,
    read_dataset,
)
from trackvance.dataset_sources import (
    ConnectionSettings,
    PostgreSQLDatasetSource,
    SourceError,
    SQLServerDatasetSource,
    _column,
)


class DecimalWithoutFormatting(Decimal):
    def __format__(self, _specification):
        raise AssertionError("An oversized Decimal must be rejected before fixed formatting")


@pytest.mark.parametrize("literal", ["1e200000", "-1e200000", "1e-200000", "-1e-200000",
                                     "1e999999999", "0e-999999999"])
def test_scientific_decimal_bomb_rejects_before_formatting(literal):
    with pytest.raises(DatasetCellLimit, match="ACQUISITION_CELL_LIMIT"):
        _cell_text(DecimalWithoutFormatting(literal))


@pytest.mark.parametrize("literal", [
    "1e65535", "-1e65534", "1e-65534", "-1e-65533", "0e-65534", "-0e-65533",
    "0e999999999", "-0e999999999", "12345678901234567890.12345678", "1.2300", "-0.00",
    "Infinity", "-Infinity", "NaN", "sNaN42",
])
def test_exact_decimal_cell_budget_preserves_sign_scale_and_values(literal):
    value = Decimal(literal)
    rendered = _cell_text(value)
    assert rendered == format(value, "f")
    assert len(rendered.encode("utf-8")) <= MAX_CELL_TEXT_BYTES


@pytest.mark.parametrize("literal", ["1e65536", "-1e65535", "1e-65535", "-1e-65534"])
def test_decimal_one_byte_over_the_exact_cell_limit_rejects(literal):
    with pytest.raises(DatasetCellLimit):
        _cell_text(DecimalWithoutFormatting(literal))


@pytest.mark.parametrize("suffix", ["json", "jsonl"])
def test_short_json_decimal_exponent_rejects_with_bounded_allocation(tmp_path, suffix):
    path = tmp_path / f"scientific.{suffix}"
    record = '{"amount":1e999999999}'
    path.write_text(f"[{record}]" if suffix == "json" else record + "\n", encoding="utf-8")
    tracemalloc.start()
    try:
        with pytest.raises(DatasetCellLimit):
            if suffix == "json":
                read_dataset(path, path.name)
            else:
                list(FileBatchReader(path, path.name))
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert peak < 1024 * 1024


@pytest.mark.parametrize("source_type", ["POSTGRESQL", "SQLSERVER"])
@pytest.mark.parametrize("streaming", [False, True])
def test_sql_decimal_expansion_is_source_size_limit_and_closes_resources(
    monkeypatch, source_type, streaming,
):
    settings = ConnectionSettings(source_type, "example.test", 5432, "db", "reader", "secret")
    source = (PostgreSQLDatasetSource if source_type == "POSTGRESQL" else SQLServerDatasetSource)(
        settings, "source_data", "records",
    )
    connection, cursor = MagicMock(), MagicMock()
    cursor.fetchmany.side_effect = [[(DecimalWithoutFormatting("1e999999999"),)], []]
    closed = []

    @contextmanager
    def connect():
        try:
            yield connection
        finally:
            closed.append(True)

    monkeypatch.setattr(source, "_connection", connect)
    monkeypatch.setattr(source, "_checked_columns", lambda *_: [_column("amount", "numeric", True)])
    monkeypatch.setattr(source, "_select", lambda *_: cursor)
    monkeypatch.setattr(source, "_select_volume", lambda *_: (cursor, False))
    with pytest.raises(SourceError) as error:
        if streaming:
            list(source.read_batches())
        else:
            source.read()
    assert error.value.code == "SOURCE_SIZE_LIMIT"
    cursor.close.assert_called_once()
    assert closed == [True]
