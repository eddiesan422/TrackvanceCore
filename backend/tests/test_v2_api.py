import io
from pathlib import Path

import pytest
from openpyxl import load_workbook
from sqlalchemy import select

from trackvance.models import Artifact, AuditEvent, Configuration, DatasetVersion, Run, User
from trackvance.worker import process_once


def test_recon_effective_configuration_can_be_published_without_mutating_the_parent(authenticated):
    dataset = authenticated.post("/api/v1/datasets", json={"name": "Roundtrip"}).json()["id"]
    original = authenticated.post("/api/v1/recon/controls", json={"name": "Reusable control", "dataset_id": dataset, "target_dataset_id": dataset,
        "config": {"key_columns": ["id"], "amount_column": "amount"}})
    assert original.status_code == 201
    original = original.json()
    repeated = authenticated.post(f"/api/v1/recon/controls/{original['id']}/versions", json={"config": original["config"]})
    assert repeated.status_code == 201, repeated.text
    assert repeated.json()["version"] == 2
    invalid = authenticated.post("/api/v1/recon/controls", json={"name": "Unsupported row rule", "dataset_id": dataset, "target_dataset_id": dataset,
        "config": {**original["config"], "rules": [{"type": "required", "column": "id"}]}})
    assert invalid.status_code == 422


def test_configuration_publication_preserves_old_run_and_rejects_conflicting_replay(authenticated, database, queued_intake):
    client = authenticated
    assert process_once("version-worker")
    run_id = queued_intake["run_id"]
    evidence_before = client.get(f"/api/v1/runs/{run_id}/evidence").content
    with database() as db:
        old = db.get(Configuration, queued_intake["config_id"])
        before = dict(old.config)
    url = f"/api/v1/intake/contracts/{queued_intake['config_id']}/versions"
    published = client.post(url, json={"config": {**before, "max_error_rate": 1}})
    assert published.status_code == 201, published.text
    newer = published.json()
    assert newer["version"] == 2 and newer["previous_version_id"] == queued_intake["config_id"]
    assert newer["id"] != queued_intake["config_id"]
    with database() as db:
        assert db.get(Configuration, queued_intake["config_id"]).config == before
        assert db.get(Run, run_id).config_id == queued_intake["config_id"]
    assert client.get(f"/api/v1/runs/{run_id}/evidence").content == evidence_before
    assert client.post(url, json={"config": before}).status_code == 409
    payload = {"contract_id": newer["id"], "dataset_version_id": queued_intake["version_id"]}
    headers = {"Idempotency-Key": "immutable-replay"}
    first = client.post("/api/v1/intake/runs", json=payload, headers=headers)
    assert first.status_code == 202
    assert client.post("/api/v1/intake/runs", json=payload, headers=headers).json()["id"] == first.json()["id"]
    conflict = client.post("/api/v1/intake/runs", json={**payload, "contract_id": queued_intake["config_id"]}, headers=headers)
    assert conflict.status_code == 409 and conflict.json()["error"]["code"] == "IDEMPOTENCY_CONFLICT"


@pytest.mark.parametrize("field,endpoint", [("result_path", "export.xlsx"), ("evidence_path", "export.xlsx"), ("result_path", "results"), ("evidence_path", "evidence")])
def test_corrupt_evidence_is_never_exported(authenticated, database, queued_intake, field, endpoint):
    assert process_once("integrity-worker")
    with database() as db:
        run = db.get(Run, queued_intake["run_id"])
        Path(getattr(run, field)).write_bytes(b"tampered evidence")
    response = authenticated.get(f"/api/v1/runs/{queued_intake['run_id']}/{endpoint}")
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "ARTIFACT_INTEGRITY_ERROR"
    with database() as db:
        assert db.scalar(select(AuditEvent).where(AuditEvent.event_type == "EXPORT_DOWNLOADED")) is None
        assert db.get(Run, queued_intake["run_id"]).status == "SUCCESS"


def test_xlsx_and_artifacts_are_organization_and_permission_scoped(authenticated, database, queued_intake):
    assert process_once("scope-worker")
    url = f"/api/v1/runs/{queued_intake['run_id']}/export.xlsx"
    response = authenticated.get(url)
    assert response.status_code == 200
    artifact_id = response.headers["X-Artifact-ID"]
    with database() as db:
        user = db.get(User, "test-user")
        user.role = "unrecognized"
        db.commit()
    assert authenticated.get(url).status_code == 403
    assert authenticated.get(f"/api/v1/artifacts/{artifact_id}/download").status_code == 403
    with database() as db:
        user = db.get(User, "test-user")
        user.role, user.organization_id = "Auditor", "another-organization"
        db.commit()
    assert authenticated.get(url).status_code == 404
    assert authenticated.get(f"/api/v1/artifacts/{artifact_id}/download").status_code == 404


def test_physical_csv_lines_and_derived_record_numbers_reach_reports(authenticated, database):
    client = authenticated
    dataset = client.post("/api/v1/datasets", json={"name": "Multiline original"}).json()["id"]
    version = client.post(f"/api/v1/datasets/{dataset}/versions/upload", files={"file": ("multiline.csv", b'id,note,amount\nA,"two\nlines",12\nB,invalid,-1\n', "text/csv")}).json()
    config = client.post("/api/v1/intake/contracts", json={"name": "Physical lines", "dataset_id": dataset, "config": {"positive_columns": ["amount"]}}).json()
    run = client.post("/api/v1/intake/runs", json={"contract_id": config["id"], "dataset_version_id": version["id"]}).json()
    assert process_once("line-worker")
    detail = client.get(f"/api/v1/runs/{run['id']}").json()
    rows = client.get(f"/api/v1/runs/{run['id']}/results").json()["items"]
    assert rows[0]["original_row_number"] == 4
    assert detail["metrics"]["source_row_numbering"] == "PHYSICAL_LINE"
    book = load_workbook(io.BytesIO(client.get(f"/api/v1/runs/{run['id']}/export.xlsx").content))
    assert book["Errores"]["A6"].value == 4
    with database() as db:
        output = db.get(DatasetVersion, detail["output_version_id"])
        assert output.profile["row_numbering"] == "RECORD_NUMBER"
        assert db.get(Artifact, output.canonical_artifact_id).kind == "INTAKE_ACCEPTED"
    profile = client.get(f"/api/v1/dataset-versions/{version['id']}/profile")
    assert profile.status_code == 200
    with database() as db:
        original = db.get(DatasetVersion, version["id"])
        Path(original.canonical_path).write_bytes(b"bad parquet")
    assert client.get(f"/api/v1/dataset-versions/{version['id']}/profile").status_code == 409
