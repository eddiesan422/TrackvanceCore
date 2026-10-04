"""Integrity, exact global equality and memory bounds of disk delivery preparation."""

import tracemalloc
from datetime import datetime
from decimal import Decimal

import polars as pl
import pytest

from trackvance.delivery_streams import DatasetRecords, PreparedRows, batches, unique_keys


def test_preparation_preserves_exact_types_and_rejects_other_binding_and_corruption():
    values = (Decimal("123456789012345678901234567890123456789.0000000000000000001"),
              datetime.fromisoformat("2026-10-03T12:30:40.123456-05:00"), "00123 🧪", False, None)
    binding = {"run_id": "one", "config_hash": "original", "row_count": 2}
    prepared = PreparedRows.write(iter([values, values]), binding)
    try:
        assert list(prepared) == [values, values] == list(prepared)
        prepared.verify(binding)
        with pytest.raises(ValueError, match="configuración efectiva"):
            prepared.verify({**binding, "config_hash": "other"})
        with prepared.path.open("r+b") as stream:
            stream.seek(-1, 2)
            value = stream.read(1)
            stream.seek(-1, 2)
            stream.write(bytes([value[0] ^ 1]))
        with pytest.raises(ValueError, match="integridad"):
            prepared.verify(binding)
    finally:
        prepared.remove()


def test_spool_memory_is_bounded_for_population_larger_than_a_batch(monkeypatch):
    monkeypatch.setenv("TRACKVANCE_DELIVERY_BATCH_ROWS", "97")
    tracemalloc.start()
    prepared = None
    try:
        prepared = PreparedRows.write(((str(i), f"{i:08d}" + "x" * 512) for i in range(50_000)),
                                      {"run_id": "bounded", "row_count": 50_000})
        _current, peak = tracemalloc.get_traced_memory()
        assert len(prepared) == 50_000
        assert sum(1 for _row in prepared) == 50_000
        # The logical population exceeds 25 MiB. A driver-wide list would fail.
        assert peak < 12 * 1024**2
    finally:
        tracemalloc.stop()
        if prepared:
            prepared.remove()


def test_global_keys_cover_batch_boundaries_and_exact_decimal_scales(monkeypatch):
    monkeypatch.setenv("TRACKVANCE_DELIVERY_BATCH_ROWS", "2")
    large = "123456789012345678901234567890.50"
    assert not unique_keys(iter([(Decimal(large), "A"), (Decimal(4), "B"),
                                 (Decimal(large + "0"), "A")]))
    assert unique_keys(iter([(Decimal(large), "A"), (Decimal(large + "0"), "B")]))
    assert not unique_keys(iter([(None, "A")]))


def test_records_scan_all_parts_and_only_sample_can_materialize(tmp_path):
    paths = []
    for number in range(3):
        path = tmp_path / f"part-{number}.parquet"
        pl.DataFrame({"id": [f"{number}-A", f"{number}-B"], "__tv_record_number": [number * 2 + 1, number * 2 + 2]}).write_parquet(path)
        paths.append(path)
    records = DatasetRecords(paths, ["id"])
    assert len(records) == 6
    assert records.head(3).to_dicts() == [{"id": "0-A"}, {"id": "0-B"}, {"id": "1-A"}]
    assert list(records)[-1] == {"id": "2-B"}
    with pytest.raises(ValueError, match="población completa"):
        records.to_dicts()
    called = []
    records.control = lambda: called.append(True)
    assert len(list(records)) == 6 and called == [True]


def test_batch_caps_observed_bytes_before_row_limit():
    values = [("x" * (3 * 1024**2),)] * 5
    assert [len(chunk) for chunk in batches(iter(values), size=100)] == [2, 2, 1]
