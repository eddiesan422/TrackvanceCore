import json


def create_dataset(authenticated, name: str = "Schema overrides") -> str:
    response = authenticated.post("/api/v1/datasets", json={"name": name})
    assert response.status_code == 201
    return response.json()["id"]


def upload_with_overrides(authenticated, dataset_id: str, csv_text: str, overrides: dict):
    response = authenticated.post(
        f"/api/v1/datasets/{dataset_id}/versions/upload",
        files={"file": ("schema.csv", csv_text.encode(), "text/csv")},
        data={"column_overrides": json.dumps(overrides)},
    )
    assert response.status_code == 201, response.text
    return response.json()


def test_logical_type_and_identifier_overrides_persist_on_initial_upload(authenticated):
    dataset_id = create_dataset(authenticated)

    version = upload_with_overrides(
        authenticated,
        dataset_id,
        "document_code,quantity,transaction_date\n001234,10,2026-09-15\n1234,20,2026-09-16\n",
        {
            "document_code": {
                "logical_type": "STRING",
                "semantic_tag": "IDENTIFIER",
            },
            "quantity": {"logical_type": "STRING"},
            "transaction_date": {"logical_type": "STRING"},
        },
    )

    schema = {column["name"]: column for column in version["schema"]}
    assert schema["document_code"]["logical_type"] == "STRING"
    assert schema["document_code"]["semantic_tag"] == "IDENTIFIER"
    assert schema["quantity"]["logical_type"] == "STRING"
    assert schema["transaction_date"]["logical_type"] == "STRING"
    profile = authenticated.get(
        f"/api/v1/dataset-versions/{version['id']}/profile"
    ).json()
    assert profile["sample"][0]["document_code"] == "001234"
    assert {
        column["name"]: column["inference_method"]
        for column in profile["profile"]["columns"]
    } == {
        "document_code": "EXPLICIT_OVERRIDE",
        "quantity": "EXPLICIT_OVERRIDE",
        "transaction_date": "EXPLICIT_OVERRIDE",
    }


def test_logical_type_overrides_are_available_on_each_new_version(authenticated):
    dataset_id = create_dataset(authenticated, "Schema overrides versions")
    first = upload_with_overrides(
        authenticated,
        dataset_id,
        "record_code,active\nA-01,true\nA-02,false\n",
        {"active": {"logical_type": "STRING"}},
    )
    second = upload_with_overrides(
        authenticated,
        dataset_id,
        "record_code,active\nA-01,true\nA-02,false\n",
        {"active": {"logical_type": "BOOLEAN"}},
    )

    assert first["version"] == 1
    assert second["version"] == 2
    first_schema = {column["name"]: column for column in first["schema"]}
    second_schema = {column["name"]: column for column in second["schema"]}
    assert first_schema["active"]["logical_type"] == "STRING"
    assert second_schema["active"]["logical_type"] == "BOOLEAN"
