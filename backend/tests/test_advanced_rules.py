"""Advanced declarative rules keep both compilers and immutable evidence aligned."""

import hashlib
import io
from datetime import UTC, datetime

import polars as pl
import pytest
from openpyxl import load_workbook
from pydantic import ValidationError
from sqlalchemy import select

from trackvance.config_semantics import (
    ConfigurationError,
    RuleDefinition,
    effective_config,
    validate_config,
)
from trackvance.models import ArtifactLink, Finding, User
from trackvance.portable_engine import DuckDBCompiler, PolarsCompiler, compile_rule
from trackvance.processing import intake, profile_frame, reconcile, sentinel
from trackvance.worker import process_once

NOW = datetime(2026, 9, 20, tzinfo=UTC)


@pytest.mark.parametrize(
    "rule, data, expected",
    [
        (
            {
                "type": "compound_unique",
                "parameters": {"columns": ["a", "b"], "null_policy": "FAIL"},
            },
            {"a": ["A", "A", "A|B", "A", None], "b": ["B", "B", "C", "B|C", "B"]},
            [False, False, True, True, False],
        ),
        (
            {"type": "length", "column": "a", "parameters": {"min": 2, "max": 3}},
            {"a": ["é", "e\u0301", "abc", "abcd", "", None]},
            [False, True, True, False, False, True],
        ),
        (
            {
                "type": "column_compare",
                "column": "a",
                "parameters": {
                    "other_column": "b",
                    "logical_type": "DECIMAL",
                    "operator": "gte",
                    "null_policy": "FAIL",
                },
            },
            {
                "a": ["1.00000000000000000001", "1", "bad", None],
                "b": ["1", "1.00000000000000000001", "1", None],
            },
            [True, False, False, False],
        ),
        (
            {
                "type": "column_compare",
                "column": "a",
                "parameters": {"other_column": "b", "logical_type": "DATE", "operator": "lte"},
            },
            {
                "a": ["2024-02-29", "2023-02-29", "2025-01-02"],
                "b": ["2024-03-01", "2024-03-01", "2025-01-01"],
            },
            [True, False, False],
        ),
        (
            {
                "type": "required",
                "column": "a",
                "when": {"column": "country", "operator": "eq", "value": "CO"},
            },
            {"a": [None, None, "", "Antioquia"], "country": ["CO", "US", "CO", "CO"]},
            [False, True, False, True],
        ),
        (
            {
                "type": "reference",
                "parameters": {
                    "columns": ["a", "b"],
                    "reference_columns": ["x", "y"],
                    "dataset_version_id": "reference",
                    "null_policy": "FAIL",
                },
            },
            {"a": ["001", "1", "A|B", None], "b": ["CO", "CO", "C", "CO"]},
            [True, False, False, False],
        ),
    ],
)
def test_advanced_rule_parity(rule, data, expected):
    frame = pl.DataFrame(data, schema={key: pl.String for key in data})
    refs = {"reference": pl.DataFrame({"x": ["001", "A"], "y": ["CO", "B|C"]})}
    expression = compile_rule(rule, NOW)
    for compiler in (PolarsCompiler(), DuckDBCompiler()):
        assert compiler.validate(frame, expression, refs) == expected
        assert compiler.validate(frame.head(0), expression, refs) == []


def test_condition_and_ignore_count_only_evaluated_rows_and_unique_population():
    config = {
        "rules": [
            {
                "type": "unique",
                "column": "value",
                "parameters": {"null_policy": "IGNORE"},
                "when": {
                    "all": [
                        {"column": "country", "operator": "eq", "value": "CO"},
                        {"column": "value", "operator": "not_null"},
                    ]
                },
            }
        ]
    }
    frame = pl.DataFrame({"value": ["A", "A", None, "B"], "country": ["CO", "US", "CO", "CO"]})
    errors, metrics, accepted = intake(frame, config, NOW)
    assert not errors and accepted.height == 4
    assert metrics["rules"][0]["evaluated_count"] == 2
    assert metrics["rules"][0]["skipped_count"] == 2
    _, profile, _ = profile_frame(frame)
    schema, _, _ = profile_frame(frame)
    checks, _ = sentinel(profile, schema, config, NOW, None, NOW, frame)
    assert checks[-1]["evaluated_count"] == 2


@pytest.mark.parametrize(
    "rule",
    [
        {"type": "length", "column": "a", "parameters": {"min": -1}},
        {"type": "length", "column": "a", "parameters": {"min": 2, "max": 1}},
        {"type": "compound_unique", "parameters": {"columns": ["a"]}},
        {
            "type": "reference",
            "parameters": {
                "columns": ["a"],
                "reference_columns": ["b", "c"],
                "dataset_version_id": "x",
            },
        },
        {
            "type": "column_compare",
            "column": "a",
            "parameters": {"other_column": "b", "operator": "exec"},
        },
        {
            "type": "required",
            "column": "a",
            "when": {"column": "a", "operator": "eq", "value": {"code": "import os"}},
        },
        {"type": "required", "column": "a", "when": {"all": []}},
        {
            "type": "required",
            "column": "a",
            "when": {"column": "a", "operator": "eq", "value": "x", "exec": "malicious"},
        },
        {
            "type": "required",
            "column": "a",
            "when": {"column": "a", "operator": "gt", "logical_type": "DECIMAL", "value": "abc"},
        },
        {
            "type": "required",
            "column": "a",
            "when": {
                "column": "a",
                "operator": "eq",
                "logical_type": "DATE",
                "value": "2023-02-29",
            },
        },
        {
            "type": "required",
            "column": "a",
            "when": {
                "column": "a",
                "operator": "eq",
                "logical_type": "TIMESTAMP",
                "value": "2026-01-01T12:00:00",
            },
        },
        {
            "type": "required",
            "column": "a",
            "when": {"column": "a", "operator": "eq", "value": None},
        },
    ],
)
def test_unsafe_or_ambiguous_advanced_rules_are_rejected(rule):
    with pytest.raises((ValidationError, ConfigurationError)):
        RuleDefinition.model_validate(rule)


def test_new_rule_ids_are_unique_and_reader_does_not_mutate_legacy():
    legacy = {"rules": [{"type": "range", "column": "amount", "parameters": {"gte": 0}}]}
    assert "rule_id" not in effective_config("INTAKE", legacy)["rules"][0]
    published = validate_config("INTAKE", {"rules": legacy["rules"] * 2})
    assert len({r["rule_id"] for r in published["rules"]}) == 2
    assert [r["rule_id"] for r in validate_config("INTAKE", published)["rules"]] == [
        r["rule_id"] for r in published["rules"]
    ]


def test_recon_multiple_aggregates_n_to_one_transforms_and_null_policy():
    config = validate_config(
        "RECON",
        {
            "key_columns": ["id", "country"],
            "key_normalization": {"trim": True},
            "source_transforms": [
                {
                    "column": "amount",
                    "type": "decimal_parse",
                    "parameters": {"decimal_separator": ",", "thousands_separator": "."},
                }
            ],
            "aggregations": [
                {
                    "side": "SOURCE",
                    "operation": "sum",
                    "column": "amount",
                    "output_column": "total",
                },
                {"side": "SOURCE", "operation": "count", "output_column": "records"},
            ],
            "comparison_rules": [
                {
                    "type": "numeric_tolerance",
                    "source_column": "total",
                    "target_column": "amount",
                    "parameters": {"abs": "0"},
                },
                {
                    "type": "numeric_tolerance",
                    "source_column": "records",
                    "target_column": "records",
                    "parameters": {"abs": "0"},
                },
            ],
        },
    )
    source = pl.DataFrame({"id": [" A ", "A"], "country": ["CO", "CO"], "amount": ["1,10", "2,20"]})
    target = pl.DataFrame({"id": ["A"], "country": ["CO"], "amount": ["3.30"], "records": ["2"]})
    rows, metrics = reconcile(source, target, config)
    assert metrics["matched"] == 1 and rows[0]["source_rows"] == [2, 3]
    assert len(rows[0]["comparisons"]) == 2
    assert all(item["failed_count"] == 0 for item in metrics["comparisons"])
    assert source["amount"].to_list() == ["1,10", "2,20"]
    config["comparison_rules"].append(
        {"type": "exact_compare", "source_column": "amount", "target_column": "amount"}
    )
    with pytest.raises(ConfigurationError, match="lado agrupado"):
        validate_config("RECON", config)


@pytest.mark.parametrize(
    "policy,classification",
    [("MATCH_NULLS", "MATCH"), ("MISMATCH", "VALUE_MISMATCH"), ("INVALID", "INVALID")],
)
@pytest.mark.parametrize("kind", ["exact_compare", "numeric_tolerance", "date_tolerance"])
def test_recon_explicit_null_policies(policy, classification, kind):
    frame = pl.DataFrame(
        {"id": ["A"], "value": [None]}, schema={"id": pl.String, "value": pl.String}
    )
    config = validate_config(
        "RECON",
        {
            "key_columns": ["id"],
            "comparison_rules": [
                {
                    "type": kind,
                    "source_column": "value",
                    "target_column": "value",
                    "parameters": {"null_policy": policy},
                }
            ],
        },
    )
    rows, _ = reconcile(frame, frame, config)
    assert rows[0]["classification"] == classification


def upload(client, name, content):
    dataset = client.post("/api/v1/datasets", json={"name": name}).json()
    version = client.post(
        f"/api/v1/datasets/{dataset['id']}/versions/upload",
        files={"file": ("rules.csv", content.encode(), "text/csv")},
    )
    assert version.status_code == 201, version.text
    return dataset, version.json()


def test_reference_artifact_lineage_finding_identity_and_xlsx(authenticated, database):
    client = authenticated
    _, reference = upload(client, "Countries", "code\nCO\nUS\n")
    dataset, version = upload(
        client, "Customers", "country,department,amount\nCO,,1\nXX,West,2\nUS,,3\n"
    )
    rules = [
        {
            "type": "reference",
            "parameters": {
                "columns": ["country"],
                "reference_columns": ["code"],
                "dataset_version_id": reference["id"],
            },
        },
        {
            "type": "required",
            "column": "department",
            "when": {"column": "country", "operator": "eq", "value": "CO"},
        },
        {"type": "range", "column": "amount", "parameters": {"gte": "3"}},
        {"type": "range", "column": "amount", "parameters": {"lte": "1"}, "severity": "WARNING"},
    ]
    response = client.post(
        "/api/v1/intake/contracts",
        json={"name": "Advanced", "dataset_id": dataset["id"], "config": {"rules": rules}},
    )
    assert response.status_code == 201, response.text
    config = response.json()
    run = client.post(
        "/api/v1/intake/runs",
        json={"contract_id": config["id"], "dataset_version_id": version["id"]},
    ).json()
    assert process_once("advanced-worker")
    detail = client.get(f"/api/v1/runs/{run['id']}").json()
    assert detail["status"] == "SUCCESS", detail
    assert detail["metrics"]["rules"][1]["evaluated_count"] == 1
    evidence = client.get(f"/api/v1/runs/{run['id']}/evidence").json()
    assert evidence["references"][0]["dataset_version_id"] == reference["id"]
    assert evidence["references"][0]["canonical_sha256"]
    with database() as db:
        assert db.scalar(
            select(ArtifactLink).where(
                ArtifactLink.relation == "RUN_REFERENCE",
                ArtifactLink.source_id == run["id"],
                ArtifactLink.target_id == reference["id"],
            )
        )
        findings = db.scalars(select(Finding).where(Finding.run_id == run["id"])).all()
        assert len(findings) == 4
        assert all(
            f.fingerprint == hashlib.sha256(("rule:" + f.details["rule_id"]).encode()).hexdigest()
            for f in findings
        )
    report = client.get(f"/api/v1/runs/{run['id']}/export.xlsx")
    assert report.status_code == 200
    workbook = load_workbook(io.BytesIO(report.content))
    trace_values = [cell.value for row in workbook["Trazabilidad"] for cell in row]
    assert reference["id"] in trace_values
    assert "Excluidas" in [cell.value for row in workbook["Resumen"] for cell in row]
    with database() as db:
        db.get(User, "test-user").organization_id = "other-org"
        db.commit()
    assert client.get(f"/api/v1/runs/{run['id']}/evidence").status_code == 404


def test_reference_publication_rejects_missing_columns_and_cross_org(authenticated, database):
    client = authenticated
    dataset, version = upload(client, "Reference scope", "code\nCO\n")
    body = {
        "name": "Invalid reference",
        "dataset_id": dataset["id"],
        "config": {
            "rules": [
                {
                    "type": "reference",
                    "parameters": {
                        "columns": ["code"],
                        "reference_columns": ["missing"],
                        "dataset_version_id": version["id"],
                    },
                }
            ]
        },
    }
    assert client.post("/api/v1/intake/contracts", json=body).status_code == 422
    with database() as db:
        from trackvance.models import DatasetVersion

        db.get(DatasetVersion, version["id"]).organization_id = "other-org"
        db.commit()
    body["config"]["rules"][0]["parameters"]["reference_columns"] = ["code"]
    assert client.post("/api/v1/intake/contracts", json=body).status_code == 404


@pytest.mark.parametrize(
    "policy,evaluated,failed", [("ALLOW", 3, 1), ("IGNORE", 2, 1), ("FAIL", 3, 2)]
)
def test_reference_null_policy_and_warning_output(policy, evaluated, failed):
    rule = {
        "type": "reference",
        "severity": "WARNING",
        "parameters": {
            "columns": ["code"],
            "reference_columns": ["id"],
            "dataset_version_id": "v1",
            "null_policy": policy,
        },
    }
    frame = pl.DataFrame(
        {"code": [None, "A", "'); DROP TABLE users; --"]}, schema={"code": pl.String}
    )
    refs = {"v1": pl.DataFrame({"id": ["A"]})}
    for compiler in (PolarsCompiler(), DuckDBCompiler()):
        result = compiler.evaluate(frame, compile_rule(rule, NOW), refs)
        assert sum(result.evaluated) == evaluated
        assert sum(not passed for passed in result.passed) == failed
    _, metrics, accepted = intake(frame, {"rules": [rule]}, NOW, references=refs)
    assert metrics["rules"][0]["evaluated_count"] == evaluated
    assert metrics["error_rows"] == 0 and metrics["warning_rows"] == failed
    assert accepted.height == 3 and metrics["decision"] == "APPROVED_WITH_WARNINGS"


def test_sentinel_warning_is_retained_in_check_and_export():
    frame = pl.DataFrame({"code": ["X"]})
    schema, profile, _ = profile_frame(frame)
    checks, _ = sentinel(
        profile,
        schema,
        {
            "rules": [
                {
                    "type": "allowed_values",
                    "column": "code",
                    "severity": "WARNING",
                    "parameters": {"values": ["A"]},
                }
            ]
        },
        NOW,
        None,
        NOW,
        frame,
    )
    assert checks[-1]["status"] == "FAIL" and checks[-1]["severity"] == "WARNING"


@pytest.mark.parametrize(
    "parameters", [{"width": True}, {"width": 4, "fill": []}, {"width": 4, "case": {}}, []]
)
def test_transform_bad_parameter_types_reject_before_execution(parameters):
    with pytest.raises((ConfigurationError, ValueError)):
        validate_config(
            "RECON",
            {
                "key_columns": ["id"],
                "comparison_rules": [
                    {"type": "exact_compare", "source_column": "id", "target_column": "id"}
                ],
                "source_transforms": [
                    {"type": "id_padding", "column": "id", "parameters": parameters}
                ],
            },
        )


def test_invalid_sum_cannot_match_null_even_when_nulls_are_equal():
    config = validate_config(
        "RECON",
        {
            "key_columns": ["id"],
            "aggregations": [
                {"side": "SOURCE", "operation": "sum", "column": "amount", "output_column": "total"}
            ],
            "comparison_rules": [
                {
                    "type": "numeric_tolerance",
                    "source_column": "total",
                    "target_column": "amount",
                    "parameters": {"null_policy": "MATCH_NULLS"},
                }
            ],
        },
    )
    source = pl.DataFrame({"id": ["A", "A"], "amount": ["bad", "1"]})
    target = pl.DataFrame(
        {"id": ["A"], "amount": [None]}, schema={"id": pl.String, "amount": pl.String}
    )
    rows, metrics = reconcile(source, target, config)
    assert rows[0]["classification"] == "INVALID"
    assert metrics["comparisons"][0]["invalid_count"] == 1


@pytest.mark.parametrize("parameters", [[], {"null_policy": []}, {"denominator": []}])
def test_recon_api_rejects_malformed_parameter_types(authenticated, parameters):
    dataset = authenticated.post("/api/v1/datasets", json={"name": "Malformed config"}).json()
    response = authenticated.post(
        "/api/v1/recon/controls",
        json={
            "name": "Malformed",
            "dataset_id": dataset["id"],
            "target_dataset_id": dataset["id"],
            "config": {
                "key_columns": ["id"],
                "comparison_rules": [
                    {
                        "type": "numeric_tolerance",
                        "source_column": "amount",
                        "target_column": "amount",
                        "parameters": parameters,
                    }
                ],
            },
        },
    )
    assert response.status_code == 422, response.text
