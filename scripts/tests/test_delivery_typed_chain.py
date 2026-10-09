"""Host rejection guards for the real typed-chain oracle, not native evidence."""
import copy
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
import delivery_cycle as runner
import delivery_typed_chain as chain


def test_full_oracle_preserves_utc_microseconds_zero_strings_and_declared_null_column():
    expected = chain.normalize_rows(chain.fixture_rows(runner))
    assert expected[1][5] == "2026-09-21T16:30:00.123456Z"
    assert [row[7] for row in expected] == ["000001", None, "000003"]
    assert all(row[8] is None for row in expected)
    canonical = [[None if value is None else str(value) for value in row] for row in chain.fixture_rows(runner)]
    canonical[1][5] = "2026-09-21T16:30:00.123456+00:00"
    canonical[0][6] = "true"
    assert chain.normalize_canonical_rows(canonical) == expected
    assert chain.content_hash(chain.normalize_canonical_rows(canonical)) == chain.content_hash(expected)


@pytest.mark.parametrize("index,value", [(3, True), (3, "1"), (6, 1), (6, "true"),
    (5, "2026-09-20T10:00:00.1234567Z"), (5, "2026-09-20T10:00:00"), (2, 10.50), (8, 0)])
def test_native_oracle_rejects_scalar_coercion_temporal_truncation_or_non_null_column(index, value):
    rows = copy.deepcopy(chain.fixture_rows(runner))
    rows[0][index] = value
    with pytest.raises(ValueError, match="TYPED_CHAIN"):
        chain.normalize_rows(rows)


@pytest.mark.parametrize("index,value", [(3, "1.0"), (3, "9223372036854775808"), (6, "1"), (8, "NULL"), (3, 1)])
def test_canonical_oracle_rejects_ambiguous_text_and_wrong_physical_families(index, value):
    rows = [[None if value is None else str(value) for value in row] for row in chain.fixture_rows(runner)]
    rows[0][index] = value
    with pytest.raises(ValueError, match="TYPED_CHAIN"):
        chain.normalize_canonical_rows(rows)


def test_source_rename_keeps_historical_target_contract_and_sql_json_is_finitely_wide():
    before = copy.deepcopy(runner.COLUMN_MAPPING)
    draft = runner.delivery_draft("final-version", {"id": "destination", "destination_version_id": "version"},
        mode="CREATE_TABLE", schema_name="existing_delivery", table_name="typed", strategy="CREATE_AND_LOAD",
        primary_key_columns=["record_id"], source_names=chain.SOURCE_NAMES)
    assert draft["columns"][-1]["source_name"] == "preserved_note"
    assert draft["columns"][-1]["target_name"] == "optional_note"
    assert len(draft["columns"]) == 8 and runner.COLUMN_MAPPING == before
    command, _ = runner.target_command("SQLSERVER", "SELECT ... FOR JSON PATH", json_oracle=True)
    assert "-y 4096 -w 4096" in command[-1] and "-W" not in command[-1]
    assert "-b" in command[-1] and "-h -1" in command[-1] and "-f 65001" in command[-1]
