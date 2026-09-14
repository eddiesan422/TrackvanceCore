from collections import Counter
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import polars as pl
import pytest

from trackvance.processing import (
    ProcessingError,
    intake,
    money,
    profile_frame,
    read_csv,
    reconcile,
    sentinel,
)

RECON_CONFIG = {"key_columns": ["id"], "amount_column": "amount", "tolerance": "0.01"}


def test_recon_exact_decimal_boundary_and_deterministic_order():
    source = pl.DataFrame({"id": ["C", "B", "A"], "amount": ["0.30", "10.0101", "9007199254740993.01"]})
    target = pl.DataFrame({"id": ["A", "C", "B"], "amount": ["9007199254740993.00", "0.29", "10.00"]})
    rows, metrics = reconcile(source, target, RECON_CONFIG)
    assert [row["key"] for row in rows] == ["A", "B", "C"]
    assert [row["classification"] for row in rows] == ["MATCH", "VALUE_MISMATCH", "MATCH"]
    assert [Decimal(row["difference"]) for row in rows] == [Decimal("0.01"), Decimal("0.0101"), Decimal("0.01")]
    assert metrics["matched"] == 2
    assert metrics["mismatched"] == 1
    assert reconcile(source, target, RECON_CONFIG) == (rows, metrics)
    reordered, new_metrics = reconcile(source.reverse(), target.reverse(), RECON_CONFIG)
    fields = ("key", "classification", "source_value", "target_value", "difference")
    assert [[row[k] for k in fields] for row in reordered] == [[row[k] for k in fields] for row in rows]
    assert new_metrics == metrics


def test_recon_null_keys_and_duplicates_are_excluded_before_join():
    source = pl.DataFrame({"id": [None, "D", "D", "E", "S"], "amount": ["1", "2", "3", None, "4"]})
    target = pl.DataFrame({"id": [None, "D", "D", "E", "T"], "amount": ["1", "2", "3", "5", "6"]})
    rows, metrics = reconcile(source, target, RECON_CONFIG)
    assert Counter(row["classification"] for row in rows) == {
        "INVALID": 3, "DUPLICATE_SOURCE": 2, "DUPLICATE_TARGET": 2,
        "SOURCE_ONLY": 1, "TARGET_ONLY": 1}
    assert metrics["matched"] == 0
    assert metrics["total_rows"] == 9
    duplicates = [row for row in rows if row["key"] == "D"]
    assert len(duplicates) == 4
    assert all(row["source_row"] is None or row["target_row"] is None for row in duplicates)
    assert {row["key"] for row in rows if row["key"].startswith("fila-")} == {"fila-0-2", "fila-1-2"}


@pytest.mark.parametrize("value", [None, "", "NaN", "Infinity", "1,23", "1e3", "--1"])
def test_money_rejects_non_canonical_or_non_finite_values(value):
    assert money(value) is None


@pytest.mark.parametrize("tolerance", ["-0.01", "NaN", "1,00"])
def test_recon_rejects_invalid_tolerance(tolerance):
    frame = pl.DataFrame({"id": ["A"], "amount": ["1"]})
    with pytest.raises(ProcessingError, match="tolerancia"):
        reconcile(frame, frame, {**RECON_CONFIG, "tolerance": tolerance})


def test_positive_rule_rejects_invalid_and_empty_values_without_numeric_rule():
    frame = pl.DataFrame({"id": ["A", "B", "C", "D", "E", "F"],
                          "amount": ["1.01", "0", "-1", "abc", None, "  "]})
    rows, metrics, accepted = intake(frame, {"positive_columns": ["amount"], "max_error_rate": 1})
    assert [row["original_row_number"] for row in rows] == [3, 4, 5, 6, 7]
    assert {row["rule_code"] for row in rows} == {"POSITIVE"}
    assert metrics["error_rows"] == 5
    assert metrics["decision"] == "APPROVED_WITH_WARNINGS"
    assert accepted.to_dicts() == [{"id": "A", "amount": "1.01"}]


def test_intake_counts_bad_rows_once_and_rejects_all_duplicate_keys():
    frame = pl.DataFrame({"id": ["A", " A ", "B"], "amount": ["bad", "2", "3"]})
    rows, metrics, accepted = intake(frame, {"unique_columns": ["id"], "numeric_columns": ["amount"],
                                           "transforms": [{"type": "trim", "column": "id"}]})
    assert metrics["error_count"] == len(rows) == 3
    assert metrics["error_rows"] == 2
    assert metrics["decision"] == "REJECTED"
    assert accepted["id"].to_list() == ["B"]


@pytest.mark.parametrize("content", ["id,id\n1,2\n", "id;id\n1;2\n", ",amount\n1,2\n", "__tv_hidden,amount\n1,2\n", "id,amount\n1,\x00\n"])
def test_csv_rejects_duplicate_empty_reserved_and_binary_headers(tmp_path, content):
    path = tmp_path / "bad.csv"
    path.write_text(content, encoding="utf-8")
    with pytest.raises(ProcessingError):
        read_csv(path)


def test_csv_accepts_bom_semicolon_and_preserves_decimal_text(tmp_path):
    path = tmp_path / "good.csv"
    path.write_text("id;amount\n001;9007199254740993.01\n", encoding="utf-8-sig")
    assert read_csv(path).to_dicts() == [{"id": "001", "amount": "9007199254740993.01"}]


def test_sentinel_detects_volume_null_and_freshness_changes_with_fixed_observation():
    schema, profile, _ = profile_frame(pl.DataFrame({"id": ["A", None]}))
    now = datetime(2026, 9, 11, tzinfo=UTC)
    config = {"required_columns": ["id"], "null_columns": ["id"], "max_null_rate": .1,
              "max_volume_change_pct": 15, "max_age_hours": 48}
    checks, metrics = sentinel(profile, schema, config, now - timedelta(hours=49),
                               {"row_count": 10}, observed_at=now)
    assert {c["code"] for c in checks if c["status"] == "FAIL"} == {
        "NULL_RATE_ID", "FRESHNESS", "VOLUME_CHANGE"}
    assert metrics["health_score"] == 25
