"""R080-01/02/03/04 and R085-03 exact semantic regression oracles."""
import copy
from datetime import datetime

import duckdb
import polars as pl
import pytest

from trackvance.governance import strict_approval
from trackvance.models import Configuration, Dataset, DatasetVersion
from trackvance.operations_common import OperationError
from trackvance.portable_temporal import exact_timestamp
from trackvance.processing import intake, intake_output_overrides
from trackvance.report_query import compile_draft, typed_parameters, validate_sql
from trackvance.report_worker import _logical_columns
from trackvance.services import create_version, enqueue, execute_run


def rows(plan, values):
    """Independent DuckDB evaluation of compiled predicates and final query."""
    with duckdb.connect() as connection:
        connection.execute('CREATE TABLE a (code VARCHAR, n BIGINT, __tv_source_pos BIGINT)')
        connection.executemany('INSERT INTO a VALUES (?,?,?)', [(value, n, index) for index, (value, n) in enumerate(values)])
        if predicate := plan["source_filters"].get("a"):
            connection.execute('ALTER TABLE a RENAME TO raw')
            # Only placeholders used in this predicate are accepted by DuckDB.
            import sqlglot
            names = {n.name for n in sqlglot.parse_one(predicate, read="duckdb").find_all(sqlglot.exp.Placeholder)}
            connection.execute('CREATE TEMP TABLE a AS SELECT * FROM raw a WHERE ' + predicate,
                               {k: v for k, v in plan["parameters"].items() if k in names})
        import sqlglot
        names = {n.name for n in sqlglot.parse_one(plan["sql"], read="duckdb").find_all(sqlglot.exp.Placeholder)}
        return connection.execute(plan["sql"], {k: v for k, v in plan["parameters"].items() if k in names}).fetchall()


SCHEMA = {"a": [{"name": "code", "logical_type": "STRING"}, {"name": "n", "logical_type": "INT64"}]}


@pytest.mark.parametrize("mode", ["GUIDED", "SQL"])
def test_r08001_internal_parameters_preserve_explicit_values_groups_and_in(mode):
    draft = {"sources": [{"alias": "a"}], "mode": mode,
             "parameters": [{"name": "minimum", "type": "INTEGER", "value": "2"}],
             "source_filters": {"a": {"operator": "OR", "conditions": [
                 {"column": "code", "operator": "IN", "value": ["001", "002"]},
                 {"column": "code", "operator": "EQ", "value": "004"}]}},
             "columns": [{"source_alias": "a", "column": "code"}],
             "post_filter": {"operator": "AND", "conditions": [
                 {"source_alias": "a", "column": "n", "operator": "GE", "value": "2"},
                 {"source_alias": "a", "column": "code", "operator": "NE", "value": "004"}]},
             "sql": "SELECT a.code FROM a WHERE a.n >= $minimum"}
    stored = copy.deepcopy(draft)
    plan = compile_draft(draft, SCHEMA)
    assert plan["parameters"]["minimum"] == 2
    assert len(plan["parameters"]) == len(set(plan["parameters"]))
    assert rows(plan, [("001", 1), ("002", 2), ("003", 3), ("004", 4)]) == ([("002",)] if mode == "GUIDED" else [("002",), ("004",)])
    assert compile_draft(stored, SCHEMA)["query_hash"] == plan["query_hash"]
    if mode == "SQL":
        stored["parameters"][0]["value"] = "4"
        assert rows(compile_draft(stored, SCHEMA), [("001", 1), ("002", 2), ("003", 3), ("004", 4)]) == [("004",)]


def test_r08001_legacy_collision_is_diagnosed_without_reinterpreting_saved_definition():
    draft = {"sources": [{"alias": "a"}], "columns": [{"source_alias": "a", "column": "code"}],
             "parameters": [{"name": "tv_filter_1", "type": "TEXT", "value": "original"}],
             "source_filters": {"a": {"column": "code", "operator": "EQ", "value": "different"}}}
    with pytest.raises(OperationError) as error:
        compile_draft(draft, SCHEMA)
    assert error.value.code == "REPORT_LEGACY_PARAMETER_COLLISION"
    assert draft["parameters"][0]["value"] == "original"
    with pytest.raises(OperationError):
        typed_parameters([{"name": "tv_internal_filter_0", "type": "TEXT", "value": "user"}])


def test_r08001_duckdb_case_insensitive_names_cannot_overwrite_values():
    with duckdb.connect() as connection:
        # Independent reproduction of the underlying binding semantics.
        assert connection.execute("SELECT $value, $VALUE", {"value": 1, "VALUE": 2}).fetchone() == (2, 2)
    with pytest.raises(OperationError, match="únicos"):
        typed_parameters([{"name": name, "type": "INTEGER", "value": value} for name, value in [("value", 1), ("VALUE", 2)]])
    with pytest.raises(OperationError) as error:
        typed_parameters([{"name": "TV_INTERNAL_filter_0", "type": "TEXT", "value": "user"}])
    assert error.value.code == "REPORT_PARAMETER_RESERVED"
    with pytest.raises(OperationError) as error:
        compile_draft({"sources": [{"alias": "a"}], "parameters": [{"name": "TV_FILTER_1", "type": "TEXT", "value": "old"}],
                       "source_filters": {"a": {"column": "code", "operator": "EQ", "value": "new"}}}, SCHEMA)
    assert error.value.code == "REPORT_LEGACY_PARAMETER_COLLISION"


@pytest.mark.parametrize("fraction", ["", ".1", ".12", ".123", ".1234", ".12345", ".123456"])
@pytest.mark.parametrize("zone", ["Z", "+00:00", "-05:00", "+14:00"])
def test_r08002_exact_timestamp_admitted_precision_and_offsets(fraction, zone):
    value = "2026-01-01T00:00:00" + fraction + zone
    bound = typed_parameters([{"name": "instant", "type": "TIMESTAMP", "value": value}])
    assert datetime.fromisoformat(bound["instant"]["value"]) == exact_timestamp(value)
    schema = {"a": [{"name": "at", "logical_type": "TIMESTAMP"}]}
    compile_draft({"sources": [{"alias": "a"}], "columns": [{"source_alias": "a", "column": "at"}],
                   "post_filter": {"source_alias": "a", "column": "at", "operator": "GE", "value": value}}, schema)
    validate_sql(f"SELECT CAST('{value}' AS TIMESTAMP) AS instant FROM a", schema, [], {})


@pytest.mark.parametrize("value", ["2026-01-01T00:00:00", "2026-01-01T00:00:00.1234567Z",
                                  "2026-02-30T00:00:00Z", "2026-01-01", "2026-01-01T00:00:60Z",
                                  "2026-01-01T00:00:00+24:00", "2026-01-01 00:00:00Z"])
def test_r08002_invalid_timestamp_cannot_bypass_parameter_filter_or_sql(value):
    schema = {"a": [{"name": "at", "logical_type": "TIMESTAMP"}]}
    with pytest.raises(OperationError):
        typed_parameters([{"name": "instant", "type": "TIMESTAMP", "value": value}])
    with pytest.raises(OperationError):
        compile_draft({"sources": [{"alias": "a"}], "columns": [{"source_alias": "a", "column": "at"}],
                       "source_filters": {"a": {"column": "at", "operator": "EQ", "value": value}}}, schema)
    for sql in [f"SELECT CAST('{value}' AS TIMESTAMP) AS instant FROM a", f"SELECT a.at FROM a WHERE a.at='{value}'"]:
        with pytest.raises(OperationError):
            validate_sql(sql, schema, [], {})
    # A temporal-looking STRING and an independent DATE keep their contracts.
    assert typed_parameters([{"name": "text", "type": "TEXT", "value": value}])["text"] == value
    assert typed_parameters([{"name": "day", "type": "DATE", "value": "2026-01-01"}])["day"]["value"] == "2026-01-01"


def test_r08002_temporal_case_coalesce_and_dynamic_cast_do_not_bypass_exact_grammar():
    schema = {"a": [{"name": "at", "logical_type": "TIMESTAMP"}, {"name": "label", "logical_type": "STRING"}]}
    for sql in ["SELECT CASE WHEN a.label='business' THEN a.at ELSE '2026-01-01T00:00:00' END AS instant FROM a",
                "SELECT COALESCE(a.at,'2026-01-01T00:00:00.1234567Z') AS instant FROM a",
                "SELECT CAST(a.label AS TIMESTAMP) AS instant FROM a"]:
        with pytest.raises(OperationError):
            validate_sql(sql, schema, [], {})
    validate_sql("SELECT CASE WHEN a.label='business' THEN a.at ELSE '2026-01-01T00:00:00Z' END AS instant FROM a", schema, [], {})
    validate_sql("SELECT a.at FROM a WHERE CASE WHEN a.label='business' THEN a.at ELSE CAST('2026-01-01T00:00:00Z' AS TIMESTAMP) END >= $instant", schema, [],
                 typed_parameters([{"name": "instant", "type": "TIMESTAMP", "value": "2026-01-01T00:00:00Z"}]))
    validate_sql("SELECT COALESCE(a.at, CASE WHEN a.label='business' THEN CAST('2026-01-01T00:00:00Z' AS TIMESTAMP) ELSE NULL END) AS instant FROM a", schema, [], {})
    bound = typed_parameters([{"name": "instant", "type": "TIMESTAMP", "value": "2026-01-01T00:00:00Z"}])
    validate_sql("SELECT a.at FROM a WHERE $instant='2026-01-01T00:00:00Z'", schema, [], bound)
    with pytest.raises(OperationError):
        validate_sql("SELECT a.at FROM a WHERE $instant='2026-01-01T00:00:00'", schema, [], bound)


@pytest.mark.parametrize("operator,needle,expected", [
    ("CONTAINS", "%_", ["a%_b", "%_start"]), ("STARTS_WITH", "%_", ["%_start"]),
    ("CONTAINS", "'\\", ["q'\\x"]), ("CONTAINS", "A", ["A"]),
    ("CONTAINS", "", ["a%_b", "%_start", "q'\\x", "A", "a", ""]),
])
def test_r08003_text_matching_is_literal_case_sensitive_and_null_explicit(operator, needle, expected):
    draft = {"sources": [{"alias": "a"}], "columns": [{"source_alias": "a", "column": "code"}],
             "post_filter": {"source_alias": "a", "column": "code", "operator": operator, "value": needle}}
    plan = compile_draft(draft, SCHEMA)
    values = ["a%_b", "%_start", "q'\\x", "A", "a", "", None]
    assert rows(plan, [(value, 1) for value in values]) == [(value,) for value in expected]
    draft["post_filter"]["column"] = "n"
    with pytest.raises(OperationError):
        compile_draft(draft, SCHEMA)


@pytest.mark.parametrize("complete", [False, True])
def test_r08004_union_coverage_is_required_for_new_eligibility(database, tmp_path, complete):
    path = tmp_path / "conditional.csv"
    path.write_text("country,value\nCO,yes\nUS,yes\nCO,yes\n")
    rules = [{"type": "required", "column": "value", "rule_id": "co", "when": {"column": "country", "operator": "eq", "value": "CO"}},
             {"type": "required", "column": "value", "rule_id": "overlap", "when": {"column": "country", "operator": "eq", "value": "CO"}}]
    if complete:
        rules.append({"type": "required", "column": "value", "rule_id": "us", "when": {"column": "country", "operator": "eq", "value": "US"}})
    with database() as db:
        dataset = Dataset(name="Conditional coverage")
        db.add(dataset)
        db.flush()
        source = create_version(db, dataset, path, path.name)
        configuration = Configuration(name="Coverage", module="intake", dataset_id=dataset.id, config={"rules": rules})
        db.add(configuration)
        db.flush()
        run = enqueue(db, configuration, source, None, "Tester")
        db.commit()
        execute_run(db, run)
        db.commit()
        historical = copy.deepcopy(run.metrics)
        assessment = strict_approval(db, run)
        assert historical["validation_coverage_rows"] == (3 if complete else 2)
        assert assessment["approved"] is complete
        assert assessment["criterion_version"] == 2
        assert run.metrics == historical and run.decision == "APPROVED"


def test_r08004_ignore_and_nonapplicable_rows_never_add_coverage():
    frame = pl.DataFrame({"value": ["1", None, "3"], "country": ["CO", "US", "CO"]})
    _, metrics, _ = intake(frame, {"rules": [{"type": "numeric", "column": "value", "parameters": {"null_policy": "IGNORE"}},
                                           {"type": "required", "column": "value", "when": {"column": "country", "operator": "eq", "value": "NONE"}}]})
    assert metrics["validation_coverage_rows"] == 2
    assert metrics["rules"][1]["evaluated_count"] == 0


def test_r08503_intake_schema_and_tags_survive_statistics_and_direct_report_alias(database, tmp_path):
    path = tmp_path / "typed.csv"
    path.write_text("case,num,amount,day,instant,flag,empty\n0001,42,1.20,2026-01-01,2025-12-31T19:00:00-05:00,true,\n0002,43,2.30,2026-01-02,2026-01-02T00:00:00Z,false,\n")
    overrides = {"case": {"logical_type": "STRING", "semantic_tag": "IDENTIFIER"}, "num": "INT64", "amount": "DECIMAL",
                 "day": "DATE", "instant": "TIMESTAMP", "flag": "BOOLEAN", "empty": "INT64"}
    with database() as db:
        dataset = Dataset(name="Schema declared")
        db.add(dataset)
        db.flush()
        source = create_version(db, dataset, path, path.name, column_overrides=overrides)
        config = Configuration(name="All validated", module="intake", dataset_id=dataset.id, config={"required_columns": ["case"]})
        db.add(config)
        db.flush()
        run = enqueue(db, config, source, None, "Tester")
        db.commit()
        execute_run(db, run)
        db.commit()
        assert run.status == "SUCCESS"
        output = db.get(DatasetVersion, run.output_version_id)
        assert [(c["logical_type"], c["semantic_tag"]) for c in output.schema_json] == [(c["logical_type"], c["semantic_tag"]) for c in source.schema_json]
        plan = compile_draft({"sources": [{"alias": "a"}], "mode": "SQL", "sql": "SELECT a.case AS renamed, a.num AS numeric, a.empty AS empty FROM a"}, {"a": output.schema_json})
        result = _logical_columns([{"name": "renamed", "type": "VARCHAR"}, {"name": "numeric", "type": "BIGINT"}, {"name": "empty", "type": "BIGINT"}], plan["projection_schema"])
        assert result == {"renamed": {"logical_type": "STRING", "semantic_tag": "IDENTIFIER"}, "numeric": {"logical_type": "INT64", "semantic_tag": None}, "empty": {"logical_type": "INT64", "semantic_tag": None}}
        assert _logical_columns([{"name": "text", "type": "VARCHAR"}])["text"]["semantic_tag"] is None


def test_r08503_transforms_derive_only_affected_columns():
    schema = [{"name": "text", "logical_type": "STRING", "semantic_tag": "IDENTIFIER"}, {"name": "count", "logical_type": "INT64", "semantic_tag": None}]
    assert intake_output_overrides(schema, {"transforms": [{"column": "text", "type": "decimal_parse"}]}) == {"text": {"logical_type": "DECIMAL", "semantic_tag": None}, "count": {"logical_type": "INT64", "semantic_tag": None}}
