import json

import polars as pl
from spark_cycle import (
    LogicalMultiset,
    duplicate_index,
    expected_intake_errors,
    expected_recon_results,
    fixture_record,
    logical_result,
)
from trackvance.processing import intake, reconcile


def test_logical_multiset_preserves_values_lineage_and_duplicate_multiplicity():
    records = [{"value": "e\u0301-😀", "position": 1}, {"value": "0.00000000000000000001", "position": 2}]

    def fingerprint(values):
        result = LogicalMultiset()
        for value in values:
            result.update(value)
        return result.evidence()

    assert fingerprint(records) == fingerprint(reversed(records))
    assert fingerprint([records[0], records[0]]) != fingerprint(records)
    assert fingerprint([{**records[0], "position": 2}, records[1]]) != fingerprint(records)
    assert fingerprint([{**records[0], "value": "é-😀"}, records[1]]) != fingerprint(records)
    first = {"classification": "MATCH", "payload": '{"b":2,"a":1}', "__tv_sort_key": "position"}
    second = {**first, "payload": json.dumps({"a": 1, "b": 2})}
    assert fingerprint([first]) == fingerprint([second])


def test_full_fixture_oracle_matches_polars_for_each_value_and_position():
    rows = 50001  # Includes every failure category and a cross-part duplicate pair.
    source = pl.DataFrame([{key: value for key, value in fixture_record(index).items()
                           if not key.startswith("__tv_")} for index in range(rows)])
    target = pl.DataFrame([{key: value for key, value in fixture_record(index, "target").items()
                           if not key.startswith("__tv_")} for index in range(rows)])
    config = {"key_columns": ["id"], "comparison_rules": [{"type": "numeric_tolerance",
              "source_column": "amount", "target_column": "amount",
              "parameters": {"abs": "0", "percent": "0", "null_policy": "INVALID"}}]}
    records, _ = reconcile(source, target, config, list(range(2, rows + 2)), list(range(2, rows + 2)))
    fingerprint = LogicalMultiset()
    for record in records:
        fingerprint.update(logical_result(record, "recon"))
    assert fingerprint.evidence() == expected_recon_results(rows)
    assert [index for index in range(rows) if duplicate_index(index, rows)] == [49999, 50000]
    config = {"rules": [{"type": "required", "column": "id"},
              {"type": "unique", "column": "id", "severity": "WARNING"},
              {"type": "numeric", "column": "amount"}, {"type": "not_null", "column": "label"}]}
    records, _, accepted = intake(source, config, input_row_numbers=list(range(2, rows + 2)))
    fingerprint = LogicalMultiset()
    for record in records:
        ordinal = {"UNIQUE": 1, "NUMERIC": 2, "NOT_NULL": 3}[record["rule_code"]]
        fingerprint.update(logical_result(record, "intake", ordinal))
    assert fingerprint.evidence() == expected_intake_errors(rows)
    assert accepted.height == rows - len(range(0, rows, 10000))
