"""Real no-GPU financial Builder activation and automatic native publication."""
import hashlib
from copy import deepcopy

from document_invoice_fixture import HEADERS, ROWS, financial_definition, financial_pdf

from trackvance.document_worker import process_once


def test_real_financial_invoice_builder_auto_publication_and_negative_amount_or_row(authenticated, database, tmp_path):
    client = authenticated
    family_response = client.post("/api/v1/documents/families", json={"name": "Synthetic financial AUTO invoice098"})
    assert family_response.status_code == 200
    family = family_response.json()["id"]
    definition = financial_definition()
    frozen = deepcopy(definition)
    references = []
    for name, invoice, role in [("configuration", "INV-000000", "CONFIGURATION"), ("holdout", "INV-000099", "HOLDOUT")]:
        source = financial_pdf(tmp_path / (name + ".pdf"), invoice)
        uploaded = client.post(f"/api/v1/documents/families/{family}/builder/references",
            files={"files": (source.name, source.read_bytes(), "application/pdf")}, data={"role": role})
        assert uploaded.status_code == 200, uploaded.text
        references.append(uploaded.json()["items"][0]["id"])
        assert process_once("financial-reference098")
    draft = client.post(f"/api/v1/documents/families/{family}/builder/drafts", json={}).json()
    response = client.put(f"/api/v1/documents/builder/drafts/{draft['id']}", json={
        "expected_version": draft["version"], "definition": definition, "reference_ids": references})
    assert response.status_code == 200, response.text
    draft = response.json()
    response = client.post(f"/api/v1/documents/builder/drafts/{draft['id']}/test", json={"expected_version": draft["version"]})
    assert response.status_code == 200, response.text
    tested = response.json()
    activation = f"/api/v1/documents/builder/tests/{tested['id']}/activate"
    assert client.post(activation).status_code == 422
    for reference in references:
        payload = tested["automatic_results"][reference]
        assert payload["fields"]["total"]["value"] == "20.90"
        assert [[row[name]["value"] for name in ("sku", "quantity", "price", "discount_pct", "tax_pct", "line_net")]
                for row in payload["tables"]["items"]] == ROWS
        assert payload["table_unparsed"]["items"] == []
        assert all(check["status"] == "VALID" for check in payload["arithmetic"])
        confirmed = [f"headers.0.{name}" for name in payload["fields"]]
        confirmed += [f"items.{index}.{name}" for index, row in enumerate(payload["tables"]["items"]) for name in row]
        response = client.post(f"/api/v1/documents/builder/tests/{tested['id']}/review", json={
            "reference_id": reference, "expected_version": tested["review_version"], "confirmed": confirmed,
            "reason": "Checked against independently declared synthetic PDF source; no operational correction."})
        assert response.status_code == 200, response.text
        tested = response.json()
    response = client.post(activation)
    assert response.status_code == 200, response.text
    template = response.json()
    assert template["definition"] == definition == frozen

    def upload(identity, mutation=None):
        source = financial_pdf(tmp_path / (identity + ".pdf"), identity, mutation)
        response = client.post(f"/api/v1/documents/families/{family}/originals",
            files={"files": (source.name, source.read_bytes(), "application/pdf")})
        assert response.status_code == 200, response.text
        original = response.json()["items"][0]
        assert original["sha256"] == hashlib.sha256(source.read_bytes()).hexdigest()
        assert process_once("financial-operational098")
        response = client.get("/api/v1/documents/originals/" + original["id"])
        assert response.status_code == 200, response.text
        downloaded = client.get("/api/v1/documents/originals/" + original["id"] + "/file")
        assert downloaded.status_code == 200 and downloaded.content == source.read_bytes()
        return response.json()

    published = upload("INV-000001")
    assert published["status"] == "PUBLISHED", published
    assert published["quality"]["status"] == "AUTO_VERIFIED"
    assert all(published["quality"]["checks"].values())
    assert not published["extraction"]["reviewed"] and len(published["revisions"]) == 1
    assert len(published["publications"]) == 1
    extraction = published["extraction"]
    assert all(not cell["reviewed"] for cell in extraction["payload"]["fields"].values())
    assert all(not cell["reviewed"] for row in extraction["payload"]["tables"]["items"] for cell in row.values())
    versions = published["publications"][0]["versions"]
    assert set(versions) == {"headers", "items"}
    for name, expected_count in [("headers", 1), ("items", 2)]:
        profile = client.get(f"/api/v1/dataset-versions/{versions[name]}/profile").json()
        assert profile["row_count"] == len(profile["sample"]) == expected_count
        metadata = profile["ingestion_metadata"]["document_publication"]
        assert metadata["family_id"] == family and metadata["structure"] == name
        assert metadata["scope"] == "PUBLICATION_BATCH_ONLY"
        assert not metadata["automatic_processing_allowed"] and not metadata["automatic_delivery_allowed"]
        assert all(row["document_id"] == published["id"] and row["extraction_id"] == extraction["id"]
                   and row["batch_id"] == published["publications"][0]["batch_id"] for row in profile["sample"])
        if name == "headers":
            assert {key: profile["sample"][0][key] for key in HEADERS} == HEADERS
        else:
            assert [[row[key] for key in ("sku", "quantity", "price", "discount_pct", "tax_pct", "line_net")]
                    for row in profile["sample"]] == ROWS
            schema = {field["name"]: field["logical_type"] for field in profile["schema"]}
            assert {name: schema[name] for name in ("sku", "quantity", "price", "discount_pct", "tax_pct", "line_net")} == {
                "sku": "STRING", "quantity": "DECIMAL", "price": "DECIMAL", "discount_pct": "STRING", "tax_pct": "STRING", "line_net": "DECIMAL"}
    family_after = client.get(f"/api/v1/documents/families/{family}").json()
    for identity, mutation in [("INV-000002", "wrong_amount"), ("INV-000003", "missing_row")]:
        blocked = upload(identity, mutation)
        assert blocked["status"] == "REVIEW_REQUIRED"
        assert blocked["quality"]["status"] == "REVIEW_REQUIRED" and not blocked["quality"]["checks"]["arithmetic"]
        assert not blocked["extraction"]["reviewed"] and not blocked["publications"]
    for dataset in family_after["output_datasets"].values():
        assert len(client.get("/api/v1/datasets/" + dataset).json()["versions"]) == 1
