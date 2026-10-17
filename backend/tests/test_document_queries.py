"""Global documents remain tenant scoped before search/count and page extraction."""
from datetime import timedelta

import pytest
from sqlalchemy import event

from trackvance.db import utcnow
from trackvance.document_models import (
    DocumentBatch,
    DocumentBatchMember,
    DocumentExtraction,
    DocumentFamily,
    DocumentOriginal,
    DocumentPublicationClaim,
    DocumentTemplate,
)
from trackvance.document_queries import list_originals
from trackvance.document_service import digest
from trackvance.models import Artifact, Dataset, DatasetVersion, Role, RolePermission, User
from trackvance.operations_common import OperationError


@pytest.fixture
def documents(database):
    with database() as db:
        user = db.get(User, "test-user")
        role = Role(name="Document operator", normalized_name="document-operator")
        db.add(role)
        db.flush()
        db.add_all([RolePermission(role_id=role.id, permission_code=grant)
                    for grant in ("documents:read", "documents:download", "datasets:read")])
        user.role_id = role.id
        now = utcnow()
        for index, (org, family_name, mode, filename, value) in enumerate([
            (user.organization_id, "Invoices Alpha", "INDIVIDUAL", "alpha.pdf", "INV-5001"),
            (user.organization_id, "Invoices Beta", "SCHEDULED", "beta.pdf", "INV-5002"),
            ("foreign-organization", "SECRET FOREIGN FAMILY", "SCHEDULED", "SECRET.pdf", "SECRET-IDENTIFIER"),
        ]):
            family = DocumentFamily(id=f"family-{index}", organization_id=org, name=family_name, responsible_user_id=user.id)
            definition = {"fields": [], "tables": [], "publication": {"mode": mode}}
            template = DocumentTemplate(id=f"template-{index}", organization_id=org, family_id=family.id,
                revision=1, definition=definition, definition_hash=digest(definition), created_by_id=user.id)
            artifact = Artifact(id=f"artifact-{index}", organization_id=org, kind="DOCUMENT_ORIGINAL",
                sha256="0" * 64, size_bytes=1, name=f"document-{index}.pdf", path=f"documents/{index}", media_type="application/pdf")
            db.add_all([family, template, artifact])
            db.flush()
            original = DocumentOriginal(id=f"document-{index}", organization_id=org, family_id=family.id,
                template_id=template.id, artifact_id=artifact.id, filename=filename, sha256="0" * 64,
                received_at=now - timedelta(days=index), uploaded_by_id=user.id, status="REVIEW_REQUIRED")
            db.add(original)
            db.flush()
            payload = {"fields": {"header_field_key": {"value": value, "observed": "DO-NOT-SEARCH-ORIGIN"}},
                       "tables": {"details": [{"customer": {"value": "CUSTOMER-" + str(index)}}]}}
            extraction = DocumentExtraction(id=f"extraction-{index}", organization_id=org, document_id=original.id,
                template_id=template.id, revision=1, payload=payload, payload_hash=digest(payload), engine={},
                actor_id=user.id, reviewed=index == 1)
            db.add(extraction)
            db.flush()
            original.extraction_id = extraction.id
        db.commit()
        return {"received": now, "user_id": user.id}


def test_global_search_matches_only_authorized_document_values_and_metadata(database, documents):
    with database() as db:
        user = db.get(User, documents["user_id"])
        result = list_originals(db, user)
        assert result["total"] == 2
        assert [item["id"] for item in result["items"]] == ["document-0", "document-1"]
        assert "SECRET" not in str(result)
        for search in ("alpha.pdf", "INV-5001", "CUSTOMER-0", "Invoices Alpha", "document-0", "extraction-0"):
            found = list_originals(db, user, search=search)
            assert found["total"] == 1
            assert found["items"][0]["id"] == "document-0"
            assert "payload" not in found["items"][0]["extraction"]
            assert "INV-5001" not in str(found)
        for search in ("SECRET", "SECRET-IDENTIFIER", "DO-NOT-SEARCH-ORIGIN", "header_field_key", "%", "_"):
            assert list_originals(db, user, search=search)["total"] == 0


def test_global_search_paginates_orders_and_filters_at_database(database, documents):
    with database() as db:
        user = db.get(User, documents["user_id"])
        statements = []
        def capture(conn, cursor, statement, parameters, context, executemany):
            statements.append(statement)
        event.listen(db.get_bind(), "before_cursor_execute", capture)
        try:
            first = list_originals(db, user, order_by="filename", direction="asc", limit=1)
            second = list_originals(db, user, order_by="filename", direction="asc", limit=1, offset=1)
        finally:
            event.remove(db.get_bind(), "before_cursor_execute", capture)
        assert first["total"] == second["total"] == 2
        assert [first["items"][0]["id"], second["items"][0]["id"]] == ["document-0", "document-1"]
        assert any("ORDER BY document_originals.filename ASC, document_originals.id ASC" in sql and "LIMIT" in sql for sql in statements)
        assert list_originals(db, user, family_id="family-1")["total"] == 1
        assert list_originals(db, user, mode="SCHEDULED")["items"][0]["id"] == "document-1"
        assert list_originals(db, user, status="FAILED")["total"] == 0
        assert list_originals(db, user, received_from=documents["received"] - timedelta(hours=1))["total"] == 1
        assert list_originals(db, user, received_to=documents["received"])["items"][0]["id"] == "document-1"
        with pytest.raises(OperationError):
            list_originals(db, user, received_from=documents["received"], received_to=documents["received"] - timedelta(days=1))


def test_global_search_denies_permission_and_foreign_family_before_count(database, documents):
    with database() as db:
        user = db.get(User, documents["user_id"])
        with pytest.raises(OperationError) as denied:
            list_originals(db, user, family_id="family-2")
        assert denied.value.status == 404
        grant = db.get(RolePermission, (user.role_id, "documents:read"))
        db.delete(grant)
        db.flush()
        with pytest.raises(OperationError) as forbidden:
            list_originals(db, user, search="SECRET")
        assert forbidden.value.status == 403


def test_global_references_use_latest_batch_without_duplicate_rows_and_protect_dataset_links(database, documents):
    with database() as db:
        user = db.get(User, documents["user_id"])
        dataset = Dataset(id="output-dataset", name="Invoice headers")
        db.add(dataset)
        db.flush()
        version = DatasetVersion(id="output-version", dataset_id=dataset.id, version=1, filename="headers.csv",
            sha256="0" * 64, schema_hash="0" * 64, size_bytes=1, row_count=1, column_count=1,
            original_path="unused", canonical_path="unused", schema_json=[], profile={})
        db.add(version)
        for index in range(2):
            batch = DocumentBatch(id=f"batch-{index}", family_id="family-0", template_id="template-0",
                idempotency_key=f"batch-{index}", status="PUBLISHED" if index == 0 else "BLOCKED", settings={},
                wait_until=utcnow(), actor_id=user.id, created_at=utcnow() + timedelta(seconds=index),
                published_versions={"headers": version.id} if index == 0 else {})
            db.add(batch)
            db.flush()
            db.add(DocumentBatchMember(batch_id=batch.id, document_id="document-0", status=batch.status))
        db.add(DocumentPublicationClaim(document_id="document-0", extraction_id="extraction-0", batch_id="batch-0"))
        db.commit()
        result = list_originals(db, user, publication="PUBLISHED")
        assert result["total"] == len(result["items"]) == 1
        item = result["items"][0]
        assert item["batch"]["id"] == "batch-1"
        assert item["outputs"] == [{"structure": "headers", "dataset_id": dataset.id, "dataset_name": dataset.name,
            "version_id": version.id, "version": 1, "href": "/datasets/output-dataset?version=output-version"}]
        assert item["links"]["document"] == "/documents/families/family-0?document=document-0"
        assert item["batch"]["href"] == "/documents/families/family-0?batch=batch-1"
        assert list_originals(db, user, publication="BATCHED")["total"] == 1
        assert list_originals(db, user, publication="UNPUBLISHED")["total"] == 1
        db.delete(db.get(RolePermission, (user.role_id, "datasets:read")))
        db.flush()
        assert list_originals(db, user, publication="PUBLISHED")["items"][0]["outputs"] == []


def test_global_http_validates_bounded_filter_contract(authenticated, documents):
    response = authenticated.get("/api/v1/documents/originals?search=INV-5002&limit=1&mode=SCHEDULED")
    assert response.status_code == 200
    assert response.json()["total"] == 1
    assert response.json()["items"][0]["id"] == "document-1"
    for query in ("limit=101", "offset=-1", "order_by=payload", "direction=drop", "mode=OTHER", "publication=OTHER"):
        assert authenticated.get("/api/v1/documents/originals?" + query).status_code == 422
    assert authenticated.get("/api/v1/documents/originals?family_id=family-2").status_code == 404
