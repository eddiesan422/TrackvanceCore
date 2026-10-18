from datetime import timedelta
from itertools import product

import pytest
from argon2 import PasswordHasher
from fastapi.testclient import TestClient
from sqlalchemy import delete, event, literal, select
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


def test_grouped_uuid_stage_search_preselects_population_and_pages_each_manual_process_once(authenticated, database, monkeypatch):
    from trackvance import operation_tracking

    with database() as db:
        case, config, candidate, _, version = make_case(db, "sentinel", candidate_decision="ALERT")
        case.assigned_user_id = "test-user"
        db.get(Run, case.run_id).initiated_by_id = "test-user"
        candidate.initiated_by_id = "test-user"
        noise_version = DatasetVersion(dataset_id=version.dataset_id, version=2, filename="unrelated.parquet", source_type="UPLOAD",
            sha256="c" * 64, schema_hash=version.schema_hash, size_bytes=1, row_count=1, column_count=1,
            original_path="", canonical_path="unused-noise.parquet", schema_json=[], profile={}, ingestion_metadata={})
        db.add(noise_version)
        db.flush()
        db.add_all([Run(module="sentinel", name=f"Unrelated synthetic history {index}", status="SUCCESS", decision="ALERT",
            config_id=config.id, dataset_version_id=noise_version.id, initiated_by="Test User", initiated_by_id="test-user") for index in range(925)])
        literal_title = Run(module="sentinel", name=f"Literal diagnosis identity {version.id}", status="SUCCESS", decision="ALERT",
            config_id=config.id, dataset_version_id=noise_version.id, initiated_by="Test User", initiated_by_id="test-user")
        db.add(literal_title)
        db.commit()
        expected = {f"run:{case.run_id}", f"run:{candidate.id}", f"run:{literal_title.id}"}
        version_id = version.id
    projected, statements = [], []
    original = operation_tracking.group_item

    def observe_group(db, user, item, grants):
        projected.append(item["id"])
        return original(db, user, item, grants)

    def observe_sql(*args):
        statements.append(args[2])

    monkeypatch.setattr(operation_tracking, "group_item", observe_group)
    event.listen(engine, "before_cursor_execute", observe_sql)
    try:
        offset, identities, scans = 0, [], []
        for page in range(3):
            response = authenticated.get(f"/api/v1/operations?grouped=true&queue=intervention&search={version_id}&limit=1&offset={offset}")
            assert response.status_code == 200, response.text
            value = response.json()
            assert len(value["items"]) == 1, (page, value, projected, expected)
            identities.append(value["items"][0]["process_id"])
            scans.append(value["scanned_records"])
            assert value["has_more"] is (page < 2)
            if page < 2:
                assert value["next_offset"] > offset
                offset = value["next_offset"]
            else:
                assert value["next_offset"] is None
    finally:
        event.remove(engine, "before_cursor_execute", observe_sql)
    assert set(identities) == expected and len(set(identities)) == 3
    assert scans == [1, 1, 1]
    assert len(projected) == 3  # Metadata matching never hydrates the 925 unrelated groups.
    assert len(statements) < 450
    assert sum(s.lstrip().startswith("WITH RECURSIVE") for s in statements) == 3


def test_grouped_operation_read_snapshot_rechecks_actor_role_and_content_between_http_requests(authenticated, database):
    with database() as db:
        case, _, _, _, version = make_case(db, "intake", candidate_decision="APPROVED")
        roles = [Role(name=f"Operations reader {i}", normalized_name=f"operations reader {i}") for i in range(2)]
        db.add_all(roles)
        db.flush()
        for role in roles:
            for permission in ("datasets:read", "intake:read"):
                db.add(RolePermission(role_id=role.id, permission_code=permission))
        db.get(User, "test-user").role_id = roles[0].id
        db.add(User(id="operations-other-reader", role_id=roles[1].id, name="Other operations reader", username="operations-other-reader",
            email="operations-other@example.test", password_hash=PasswordHasher(time_cost=1, memory_cost=8192).hash("other-test-password")))
        db.commit()
        version_id, dataset_id, case_id, first_role_id = version.id, version.dataset_id, case.id, roles[0].id
    path = f"/api/v1/operations?grouped=true&search={version_id}"
    first = authenticated.get(path)
    assert first.status_code == 200 and len(first.json()["items"]) == 2
    assert case_id not in first.text and "/exceptions" not in first.text
    other = TestClient(authenticated.app)
    try:
        assert other.post("/api/v1/auth/login", json={"email": "operations-other@example.test", "password": "other-test-password"}).status_code == 200
        assert len(other.get(path).json()["items"]) == 2
        with database() as db:
            db.execute(delete(RolePermission).where(RolePermission.role_id == first_role_id, RolePermission.permission_code == "intake:read"))
            db.commit()
        denied = authenticated.get(path)
        assert denied.status_code == 200 and denied.json()["items"] == [] and denied.json()["scanned_records"] == 0
        assert version_id not in denied.text and case_id not in denied.text
        assert len(other.get(path).json()["items"]) == 2
        with database() as db:
            db.add(DatasetBlock(dataset_id=dataset_id, scope="CONTENT", reason="New request restriction", created_by_id="test-user"))
            db.commit()
        blocked = other.get(path)
        assert blocked.status_code == 200 and blocked.json()["items"] == [] and blocked.json()["scanned_records"] == 0
        assert version_id not in blocked.text and case_id not in blocked.text
    finally:
        other.close()


def test_operation_get_read_scope_closes_on_projection_error(database, monkeypatch):
    from trackvance import operation_tracking

    def fail(*args):
        raise RuntimeError("synthetic projection failure")

    monkeypatch.setattr(operation_tracking, "group_item", fail)
    with database() as db:
        case, *_ = make_case(db, "intake", candidate_decision="APPROVED")
        db.flush()
        with pytest.raises(RuntimeError, match="synthetic projection failure"):
            operation_tracking.operations(queue="all", search="", offset=0, limit=25, grouped=True,
                process=f"run:{case.run_id}", db=db, user=db.get(User, "test-user"))
        assert all(key not in db.info for key in ("read_content_authorization", "operation_process_identities", "read_content_metadata",
            "read_content_records", "read_run_references"))


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


def test_grouped_uuid_search_keeps_independent_multibatch_document_stages_and_permissions(authenticated, database):
    from trackvance.document_models import (
        DocumentBatch,
        DocumentBatchMember,
        DocumentExtraction,
        DocumentFamily,
        DocumentOriginal,
        DocumentTemplate,
    )
    from trackvance.models import Artifact

    with database() as db:
        family = DocumentFamily(name="UUID search family", responsible_user_id="test-user")
        artifact = Artifact(kind="DOCUMENT_ORIGINAL", name="search.pdf", path="unused-operations-search.pdf",
            sha256="e" * 64, size_bytes=1, media_type="application/pdf")
        db.add_all([family, artifact])
        db.flush()
        template = DocumentTemplate(family_id=family.id, revision=1, definition={"fields": [], "tables": []},
            definition_hash="a" * 64, created_by_id="test-user")
        db.add(template)
        db.flush()
        original = DocumentOriginal(family_id=family.id, template_id=template.id, artifact_id=artifact.id,
            filename="independent-multibatch.pdf", sha256=artifact.sha256, received_at=utcnow(),
            uploaded_by_id="test-user", status="REVIEW_REQUIRED")
        db.add(original)
        db.flush()
        old = DocumentExtraction(document_id=original.id, template_id=template.id, revision=1,
            payload={}, payload_hash="b" * 64, engine={}, actor_id="test-user")
        db.add(old)
        db.flush()
        current = DocumentExtraction(document_id=original.id, template_id=template.id, revision=2,
            previous_id=old.id, payload={}, payload_hash="c" * 64, engine={}, actor_id="test-user")
        db.add(current)
        db.flush()
        original.extraction_id = current.id
        batches = [DocumentBatch(family_id=family.id, template_id=template.id, idempotency_key=f"search-batch-{index}",
            status="OPEN", settings={}, actor_id="test-user", wait_until=utcnow() + timedelta(hours=1)) for index in range(3)]
        db.add_all(batches)
        db.flush()
        db.add_all([DocumentBatchMember(batch_id=batch.id, document_id=original.id,
            extraction_id=old.id if index < 2 else None, status="READY") for index, batch in enumerate(batches)])
        role = Role(name="Documents operations only", normalized_name="documents operations only")
        db.add(role)
        db.flush()
        document_permission = RolePermission(role_id=role.id, permission_code="documents:read")
        db.add(document_permission)
        db.get(User, "test-user").role_id = role.id
        db.commit()
        original_id, extraction_id, old_id = original.id, current.id, old.id
        batch_ids, role_id = [batch.id for batch in batches], role.id

    def pages(search):
        items, offset = [], 0
        for _ in range(4):
            response = authenticated.get("/api/v1/operations", params={"grouped": "true", "search": search,
                "offset": offset, "limit": 1})
            assert response.status_code == 200, response.text
            page = response.json()
            items.extend(page["items"])
            if not page["has_more"]:
                assert page["next_offset"] is None
                break
            assert page["next_offset"] > offset
            offset = page["next_offset"]
        else:
            pytest.fail("Document search did not exhaust its four native groups")
        assert len(items) == len({item["process_id"] for item in items})
        return items

    expected = {f"document:{original_id}", *(f"document_batch:{identity}" for identity in batch_ids)}
    by_document = pages(original_id)
    assert {item["process_id"] for item in by_document} == expected
    assert {item["process_id"] for item in pages(extraction_id)} == {f"document:{original_id}"}
    historical = pages(old_id)
    assert {item["process_id"] for item in historical} == {f"document_batch:{identity}" for identity in batch_ids[:2]}
    for item in historical:
        frozen = next(stage for stage in item["stages"] if stage["id"] == f"extraction:{old_id}")
        assert frozen["title"] == "Extracción · revisión 1"
        assert frozen["href"].endswith("?batch=" + item["resource_id"])
        assert f"extraction:{extraction_id}" not in {stage["id"] for stage in item["stages"]}
    independent = next(item for item in by_document if item["process_id"] == f"document:{original_id}")
    assert {f"document-batch:{identity}" for identity in batch_ids} <= {stage["id"] for stage in independent["stages"]}
    assert f"extraction:{extraction_id}" in {stage["id"] for stage in independent["stages"]}
    null_member = next(item for item in by_document if item["process_id"] == f"document_batch:{batch_ids[-1]}")
    assert not any(stage["id"].startswith("extraction:") for stage in null_member["stages"])
    for identity in batch_ids:
        assert {item["process_id"] for item in pages(identity.upper())} == {
            f"document:{original_id}", f"document_batch:{identity}"}

    # A corrupt cross-document pointer must not turn a member into evidence
    # for another original, even though the UUID preselector can find that FK.
    with database() as db:
        member = db.scalar(select(DocumentBatchMember).where(DocumentBatchMember.batch_id == batch_ids[-1]))
        unrelated = DocumentOriginal(family_id=family.id, template_id=template.id, artifact_id=artifact.id,
            filename="unrelated-source.pdf", sha256=artifact.sha256, received_at=utcnow(), uploaded_by_id="test-user")
        db.add(unrelated)
        db.flush()
        wrong = DocumentExtraction(document_id=unrelated.id, template_id=template.id, revision=1,
            payload={}, payload_hash="d" * 64, engine={}, actor_id="test-user")
        db.add(wrong)
        db.flush()
        member.extraction_id = wrong.id
        db.commit()
        wrong_id = wrong.id
    assert pages(wrong_id) == []
    with database() as db:
        wrong = db.get(DocumentExtraction, wrong_id)
        wrong.document_id, wrong.organization_id, wrong.revision = original_id, "org-other", 3
        db.commit()
    assert pages(wrong_id) == []

    with database() as db:
        db.query(RolePermission).filter_by(role_id=role_id, permission_code="documents:read").delete()
        db.add(RolePermission(role_id=role_id, permission_code="datasets:read"))
        db.commit()
    denied = authenticated.get("/api/v1/operations", params={"grouped": "true", "search": original_id})
    assert denied.status_code == 200 and denied.json()["items"] == [] and denied.json()["scanned_records"] == 0
    assert "independent-multibatch.pdf" not in denied.text and "UUID search family" not in denied.text
    with database() as db:
        db.add(RolePermission(role_id=role_id, permission_code="documents:read"))
        db.commit()
    assert {item["process_id"] for item in pages(original_id)} == expected


def test_original_document_tracking_requires_its_independent_read_permission(authenticated, database, tmp_path):
    from test_documents_095 import LINES, create_family, pdf_file, upload

    family, template = create_family(authenticated, name="Synthetic invoices tracking")
    from trackvance.document_models import DocumentFamily

    with database() as db:
        db.get(DocumentFamily, family["id"]).active_template_id = template["id"]
        db.commit()
    original = upload(authenticated, family, template, pdf_file(tmp_path / "tracking.pdf", [(LINES, None)]), example=False)
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
