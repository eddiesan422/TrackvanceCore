from datetime import timedelta
from itertools import product

import pytest
from sqlalchemy import event, literal, select
from test_exception_validation import make_case

from trackvance.acquisition_models import AcquisitionRun
from trackvance.db import engine, utcnow
from trackvance.governance_models import DatasetBlock
from trackvance.models import (
    DatasetVersion,
    ExceptionCase,
    ExternalConnection,
    ExternalConnectionVersion,
    Role,
    RolePermission,
    Run,
    User,
)
from trackvance.operation_tracking import _queue_expression, operational_queue
from trackvance.services import assess_exception_validation


@pytest.mark.parametrize("kind,status,decision,stage,expected", [
    ("acquisition", "QUEUED", None, "QUEUED", "waiting"),
    ("acquisition", "RUNNING", None, "RETRY_WAIT", "waiting"),
    ("acquisition", "RUNNING", None, "READING", "running"),
    ("acquisition", "FAILED", None, "FAILED", "intervention"),
    ("run", "SUCCESS", "UNKNOWN", "COMPLETED", "intervention"),
    ("run", "SUCCESS", "COMMITTED", "COMPLETED", "finished"),
    ("exception", "PENDING_VALIDATION", None, "", "intervention"),
    ("exception", "WAITING_EXTERNAL", None, "", "waiting"),
    ("exception", "INVESTIGATING", None, "", "running"),
    ("exception", "ACCEPTED", None, "", "finished"),
])
def test_native_states_determine_operational_queue(kind, status, decision, stage, expected):
    assert operational_queue(kind, status, decision=decision, stage=stage) == expected


def test_native_queue_sql_prefilter_matches_all_status_decision_and_retry_combinations(database):
    statuses = {"QUEUED", "RUNNING", "SUCCESS", "FAILED", "FAILED_PRECONDITION", "UNKNOWN", "CANCELLED", "WAITING", "RETRY_WAIT",
        "PUBLISHED", "EXTRACTED", "REVIEW_REQUIRED", "REVIEWED", "OPEN", "BLOCKED", "SEALED", "EMPTY", "WAITING_EXTERNAL",
        "INVESTIGATING", "PENDING_VALIDATION", "RESOLVED", "DISCARDED", "ACCEPTED", "NOT_APPLICABLE", "FALSE_POSITIVE"}
    with database() as db:
        for kind in ("exception", "document", "document_batch", "acquisition", "run"):
            combinations = product(statuses, (None, "APPROVED", "UNKNOWN", "PARTIAL"), ("", "COMPLETED", "RETRY_WAIT", "WAITING_RETRY"))
            for status, decision, stage in combinations:
                sql_queue = db.scalar(select(_queue_expression(kind, literal(status), literal(decision), literal(stage))))
                assert sql_queue == operational_queue(kind, status, decision=decision, stage=stage), (kind, status, decision, stage)


def test_waiting_feed_query_growth_excludes_unrelated_completed_history(authenticated, database):
    with database() as db:
        case, config, _, _, version = make_case(db, "sentinel", candidate_decision="HEALTHY")
        case.state, case.assigned_user_id = "WAITING_EXTERNAL", "test-user"
        db.commit()
        identity, config_id, version_id = case.id, config.id, version.id

    def waiting_queries():
        statements = []

        def observe(connection, cursor, statement, parameters, context, executemany):
            if statement.lstrip().upper().startswith("SELECT"):
                statements.append(statement)

        event.listen(engine, "before_cursor_execute", observe)
        try:
            response = authenticated.get("/api/v1/operations?queue=waiting")
        finally:
            event.remove(engine, "before_cursor_execute", observe)
        assert response.status_code == 200, response.text
        return response.json(), len(statements)

    before, initial_queries = waiting_queries()
    with database() as db:
        db.add_all([Run(module="sentinel", name=f"Irrelevant completed history {index}", status="SUCCESS", decision="HEALTHY",
            config_id=config_id, dataset_version_id=version_id, initiated_by="Test User") for index in range(250)])
        db.commit()
    after, expanded_queries = waiting_queries()
    assert [item["id"] for item in before["items"]] == [f"exception:{identity}"]
    assert [item["id"] for item in after["items"]] == [f"exception:{identity}"]
    assert after["scanned_records"] == before["scanned_records"] == 1
    assert expanded_queries <= initial_queries + 2


def test_search_filters_before_full_projection_but_still_authorizes_matching_content(authenticated, database, monkeypatch):
    from trackvance import operation_tracking

    with database() as db:
        case, config, _, _, version = make_case(db, "sentinel", candidate_decision="HEALTHY")
        case.title = "Distinct synthetic search target"
        case.state = "WAITING_EXTERNAL"
        db.add_all([Run(module="sentinel", name=f"Unmatched waiting operation {index}", status="QUEUED",
            config_id=config.id, dataset_version_id=version.id, initiated_by="Test User") for index in range(120)])
        db.commit()
        identity, origin_id, dataset_id = case.id, case.run_id, version.dataset_id
    authorize = operation_tracking.authorize_run_content
    authorized_ids = []

    def observed_authorization(db, user, run):
        authorized_ids.append(run.id)
        authorize(db, user, run)

    monkeypatch.setattr(operation_tracking, "authorize_run_content", observed_authorization)
    path = "/api/v1/operations?queue=waiting&search=Distinct%20synthetic%20search%20target"
    result = authenticated.get(path)
    assert result.status_code == 200, result.text
    assert [item["id"] for item in result.json()["items"]] == [f"exception:{identity}"]
    assert authorized_ids == [origin_id]
    with database() as db:
        db.add(DatasetBlock(dataset_id=dataset_id, scope="CONTENT", reason="Synthetic source restriction", created_by_id="test-user"))
        db.commit()
    denied = authenticated.get(path)
    assert denied.status_code == 200, denied.text
    assert denied.json()["items"] == []
    assert "Distinct synthetic search target" not in denied.text
    assert authorized_ids == [origin_id, origin_id]


def test_distinct_successful_lot_cannot_resolve_origin_population(database):
    with database() as db:
        case, _, candidate, _, version = make_case(db, "intake", candidate_decision="APPROVED")
        later = DatasetVersion(dataset_id=version.dataset_id, version=2, filename="next-cut.parquet", source_type="DOCUMENT_OCR",
            sha256="c" * 64, schema_hash=version.schema_hash, size_bytes=1, row_count=1, column_count=1,
            original_path="", canonical_path="unused-next.parquet", schema_json=[], profile={},
            ingestion_metadata={"batch_id": "independent-batch"})
        db.add(later)
        db.flush()
        candidate.dataset_version_id = later.id
        assessment = assess_exception_validation(db, case)
        assert not assessment["validated"] and not assessment["can_resolve"]
        assert assessment["evidence"]["criterion"] == "VALIDATION_POPULATION_MISMATCH"
        assert assessment["evidence"]["population_match"] is False


def test_changed_version_requires_frozen_population_identity_and_membership(database):
    with database() as db:
        case, _, candidate, _, version = make_case(db, "intake", candidate_decision="APPROVED")
        scope = {"schema_version": 1, "kind": "DOCUMENT_SET", "identity": "receipt-window-001", "membership_sha256": "a" * 64}
        version.ingestion_metadata = {"population_scope": scope}
        corrected = DatasetVersion(dataset_id=version.dataset_id, version=2, filename="corrected.parquet", source_type="DOCUMENT_OCR",
            sha256="c" * 64, schema_hash=version.schema_hash, size_bytes=1, row_count=1, column_count=1,
            original_path="", canonical_path="unused-corrected.parquet", schema_json=[], profile={},
            ingestion_metadata={"population_scope": scope})
        db.add(corrected)
        db.flush()
        candidate.dataset_version_id = corrected.id
        assert assess_exception_validation(db, case)["validated"]
        corrected.ingestion_metadata = {"population_scope": {**scope, "membership_sha256": "b" * 64}}
        assert not assess_exception_validation(db, case)["validated"]


def test_waiting_requires_reason_preserves_evidence_and_queues(authenticated, database):
    with database() as db:
        case, *_ = make_case(db, "sentinel", candidate_decision="HEALTHY")
        case.state = "INVESTIGATING"
        db.commit()
        identity = case.id
    path = f"/api/v1/exceptions/{identity}"
    invalid = authenticated.patch(path, json={"version": 1, "state": "WAITING_EXTERNAL"})
    assert invalid.status_code == 422 and invalid.json()["error"]["code"] == "WAIT_REASON_REQUIRED"
    waiting = authenticated.patch(path, json={"version": 1, "state": "WAITING_EXTERNAL", "comment": "La fuente debe confirmar la corrección."})
    assert waiting.status_code == 200, waiting.text
    assert waiting.json()["events"][-1]["comment"] == "La fuente debe confirmar la corrección."
    assert authenticated.get("/api/v1/exceptions?queue=waiting").json()["total"] == 1
    assert authenticated.get("/api/v1/exceptions?queue=running").json()["total"] == 0
    resumed = authenticated.patch(path, json={"version": 2, "state": "INVESTIGATING", "comment": "Fuente disponible"})
    assert resumed.status_code == 200
    assert authenticated.get("/api/v1/exceptions?queue=running").json()["total"] == 1


def test_feed_paginates_real_authorized_ids_without_fake_stages(authenticated, database):
    with database() as db:
        case, *_ = make_case(db, "sentinel", candidate_decision="HEALTHY")
        case.assigned_user_id = "test-user"
        db.commit()
        identity = case.id
    response = authenticated.get("/api/v1/operations?limit=1")
    assert response.status_code == 200, response.text
    page = response.json()
    assert len(page["items"]) == 1 and page["has_more"]
    first_id = page["items"][0]["id"]
    second = authenticated.get(f"/api/v1/operations?limit=1&offset={page['next_offset']}").json()
    assert second["items"][0]["id"] != first_id
    attention = authenticated.get("/api/v1/operations?queue=intervention").json()
    assert any(item["id"] == f"exception:{identity}" for item in attention["items"])
    for item in attention["items"]:
        assert not any(stage["title"] == "Entrega completada" for stage in item["stages"])
    with database() as db:
        db.get(ExceptionCase, identity).organization_id = "other-organization"
        db.commit()
    assert not any(item["id"] == f"exception:{identity}" for item in authenticated.get("/api/v1/operations").json()["items"])


def test_newest_unrelated_population_does_not_reuse_prior_validation(database):
    with database() as db:
        case, _, candidate, _, version = make_case(db, "intake", candidate_decision="APPROVED")
        assert assess_exception_validation(db, case)["validated"]
        candidate.created_at = utcnow() - timedelta(seconds=1)
        version.ingestion_metadata = {"batch_id": "origin"}
        db.flush()
        # Counts and dataset identity do not replace a frozen population proof.
        other = DatasetVersion(dataset_id=version.dataset_id, version=2, filename="same-counts.parquet", source_type="REST",
            sha256=version.sha256, schema_hash=version.schema_hash, size_bytes=1, row_count=version.row_count,
            column_count=1, original_path="", canonical_path="unused.parquet", schema_json=[], profile={},
            ingestion_metadata={"batch_id": "other"})
        db.add(other)
        db.flush()
        candidate.dataset_version_id = other.id
        assert not assess_exception_validation(db, case)["can_resolve"]


def test_failed_first_acquisition_is_visible_without_an_operational_dataset(authenticated, database):
    with database() as db:
        connection = ExternalConnection(name="Synthetic REST", source_type="REST")
        db.add(connection)
        db.flush()
        revision = ExternalConnectionVersion(connection_id=connection.id, version=1, host="fixture.invalid", port=443,
            database="REST", username="", options={}, secret_reference="synthetic-only", config_hash="a" * 64)
        db.add(revision)
        db.flush()
        acquisition = AcquisitionRun(dataset_id=None, connection_version_id=revision.id, source_type="REST",
            filename="REST.collection.parquet", source_snapshot={"pending_dataset": {"name": "Clientes API sintéticos"}},
            request_hash="b" * 64, initiated_by_id="test-user", initiated_by_name="Test User", status="FAILED",
            stage="FAILED", error_code="REST_REQUEST_TIMEOUT", error_message="La petición excedió su tiempo máximo.")
        db.add(acquisition)
        db.commit()
        identity = acquisition.id
    response = authenticated.get("/api/v1/operations?queue=intervention")
    assert response.status_code == 200, response.text
    item = next(item for item in response.json()["items"] if item["id"] == f"acquisition:{identity}")
    assert item["tracking_id"] == f"acquisition:{identity}"
    assert "Clientes API sintéticos" in item["title"]
    assert item["error_code"] == "REST_REQUEST_TIMEOUT"
    assert not any(stage["state"] == "PUBLISHED" for stage in item["stages"])
    assert authenticated.get("/api/v1/datasets").json()["total"] == 0


def test_original_document_tracking_requires_its_independent_read_permission(authenticated, database, tmp_path):
    from test_documents_095 import LINES, create_family, pdf_file, upload

    family, template = create_family(authenticated, name="Synthetic invoices tracking")
    original = upload(authenticated, family, template, pdf_file(tmp_path / "tracking.pdf", [(LINES, None)]))
    result = authenticated.get("/api/v1/operations").json()
    item = next(item for item in result["items"] if item["id"] == f"document:{original['id']}")
    assert len(item["stages"]) == 1  # No extraction or publication has happened.
    assert item["stages"][0]["href"] == f"/documents/families/{family['id']}?document={original['id']}"
    with database() as db:
        role = Role(name="Tracking without originals", normalized_name="tracking without originals")
        db.add(role)
        db.flush()
        for permission in ("exceptions:read", "datasets:read"):
            db.add(RolePermission(role_id=role.id, permission_code=permission))
        db.get(User, "test-user").role_id = role.id
        db.commit()
    result = authenticated.get("/api/v1/operations")
    assert result.status_code == 200, result.text
    assert not result.json()["items"]
    assert original["filename"] not in result.text
