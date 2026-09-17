import json

import polars as pl
import pytest


def create_dataset(authenticated, name: str = "Schema source") -> str:
    response = authenticated.post("/api/v1/datasets", json={"name": name})
    assert response.status_code == 201
    return response.json()["id"]


def upload(authenticated, dataset_id: str, csv_text: str):
    response = authenticated.post(
        f"/api/v1/datasets/{dataset_id}/versions/upload",
        files={"file": ("schema.csv", csv_text.encode(), "text/csv")},
    )
    assert response.status_code == 201, response.text
    return response.json()


def test_latest_dataset_schema_uses_persisted_profile_without_reading_rows(authenticated):
    dataset_id = create_dataset(authenticated)
    version = upload(
        authenticated,
        dataset_id,
        "document_id,quantity,amount,transaction_date,description\n"
        "001234567,1,10.50,2026-09-01,Alpha\n"
        "1234567,2,20.00,2026-09-02,Beta\n",
    )

    response = authenticated.get(f"/api/v1/datasets/{dataset_id}/schema")

    assert response.status_code == 200
    payload = response.json()
    assert payload["version_id"] == version["id"]
    assert payload["scan_mode"] == "PERSISTED_PROFILE"
    assert payload["scanned_rows"] == 0
    columns = {column["name"]: column for column in payload["columns"]}
    assert list(columns) == [
        "document_id", "quantity", "amount", "transaction_date", "description"
    ]
    assert columns["document_id"] == {
        "name": "document_id",
        "logical_type": "STRING",
        "native_type": None,
        "nullable": False,
        "semantic_tag": "IDENTIFIER",
        "numeric": False,
    }
    assert columns["quantity"]["logical_type"] == "DECIMAL"
    assert columns["quantity"]["numeric"] is True
    assert columns["amount"]["numeric"] is True
    assert columns["transaction_date"]["logical_type"] == "DATE"
    assert columns["transaction_date"]["numeric"] is False


def test_explicit_schema_refresh_reads_parquet_metadata_only(authenticated, monkeypatch):
    dataset_id = create_dataset(authenticated)
    version = upload(authenticated, dataset_id, "order_id,amount\nA-01,10.50\n")

    def fail_full_read(*_args, **_kwargs):
        raise AssertionError("schema refresh must not read dataset rows")

    monkeypatch.setattr(pl, "read_parquet", fail_full_read)
    response = authenticated.get(f"/api/v1/datasets/{dataset_id}/schema?refresh=true")

    assert response.status_code == 200
    payload = response.json()
    assert payload["version_id"] == version["id"]
    assert payload["scan_mode"] == "CANONICAL_PARQUET_METADATA"
    assert payload["scanned_rows"] == 0
    assert [column["name"] for column in payload["columns"]] == ["order_id", "amount"]
    amount = next(column for column in payload["columns"] if column["name"] == "amount")
    assert amount["native_type"] == "String"
    assert amount["logical_type"] == "DECIMAL"
    assert amount["numeric"] is True


def test_schema_refresh_resolves_the_newest_immutable_version(authenticated):
    dataset_id = create_dataset(authenticated)
    first = upload(authenticated, dataset_id, "order_id,amount\nA-01,10.50\n")
    second = upload(
        authenticated,
        dataset_id,
        "order_id,amount,currency\nA-01,10.50,COP\n",
    )

    response = authenticated.get(f"/api/v1/datasets/{dataset_id}/schema?refresh=true")

    assert response.status_code == 200
    payload = response.json()
    assert payload["version_id"] == second["id"]
    assert payload["version_id"] != first["id"]
    assert payload["version"] == 2
    assert [column["name"] for column in payload["columns"]] == [
        "order_id", "amount", "currency"
    ]


def test_dataset_without_versions_has_an_empty_schema(authenticated):
    dataset_id = create_dataset(authenticated)

    response = authenticated.get(f"/api/v1/datasets/{dataset_id}/schema")

    assert response.status_code == 200
    assert response.json()["scan_mode"] == "NO_VERSION"
    assert response.json()["columns"] == []


def test_duplicate_dataset_name_returns_actionable_conflict(authenticated):
    dataset_id = create_dataset(authenticated, "Existing dataset")

    response = authenticated.post(
        "/api/v1/datasets",
        json={"name": "Existing dataset"},
    )

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "DATASET_NAME_EXISTS"
    assert response.json()["error"]["details"] == {
        "dataset_id": dataset_id,
        "dataset_name": "Existing dataset",
    }
    assert "nueva versión" in response.json()["error"]["message"]


@pytest.mark.parametrize(
    "logical_type",
    [
        "INTEGER", "LONG", "INT64", "UINT32", "FLOAT", "FLOAT64", "DOUBLE",
        "DECIMAL(18,2)", "NUMERIC", "NUMBER", "REAL", "BIGINT", "SMALLINT",
    ],
)
def test_numeric_schema_type_families_are_recognized(logical_type):
    from trackvance.api import schema_type_is_numeric

    assert schema_type_is_numeric(logical_type) is True


@pytest.mark.parametrize("logical_type", ["STRING", "DATE", "TIMESTAMP", "BOOLEAN", None])
def test_non_numeric_schema_types_are_not_promoted(logical_type):
    from trackvance.api import schema_type_is_numeric

    assert schema_type_is_numeric(logical_type) is False


@pytest.mark.parametrize(
    ("override", "message"),
    [
        ({"amount": ["DECIMAL"]}, "debe ser un objeto"),
        ({"amount": {"logical_type": "MONEY"}}, "Tipo de override no soportado"),
        ({"amount": {"semantic_tag": "MEASURE"}}, "Etiqueta semántica no soportada"),
        (
            {"amount": {"logical_type": "DECIMAL", "semantic_tag": "IDENTIFIER"}},
            "debe usar el tipo lógico STRING",
        ),
    ],
)
def test_invalid_column_override_contract_returns_actionable_422(
    authenticated, override, message
):
    dataset_id = create_dataset(authenticated, f"Invalid override {message}")

    response = authenticated.post(
        f"/api/v1/datasets/{dataset_id}/versions/upload",
        files={"file": ("schema.csv", b"amount\n001234567\n", "text/csv")},
        data={"column_overrides": json.dumps(override)},
    )

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "INVALID_DATA"
    assert message in response.json()["error"]["message"]
    detail = authenticated.get(f"/api/v1/datasets/{dataset_id}").json()
    assert detail["versions"] == []
