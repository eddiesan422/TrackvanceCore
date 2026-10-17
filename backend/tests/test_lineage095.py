"""Native documentary and comparison lineage is metadata, with independent access."""
from datetime import timedelta

from test_publication_lineage_090 import source

from trackvance.artifactstore import link_artifact
from trackvance.comparison_models import VersionComparison
from trackvance.db import utcnow
from trackvance.document_models import (
    DocumentBatch,
    DocumentExtraction,
    DocumentFamily,
    DocumentOriginal,
    DocumentTemplate,
)
from trackvance.governance_models import DatasetBlock
from trackvance.models import Artifact, DatasetVersion, Role, RolePermission, User


def fixture_graph(db, tmp_path):
    dataset, version = source(db, tmp_path, "Synthetic document publication")
    family = DocumentFamily(name="Invoices metadata", responsible_user_id="test-user")
    db.add(family)
    db.flush()
    template = DocumentTemplate(family_id=family.id, revision=1, definition={}, definition_hash="a" * 64, created_by_id="test-user")
    original_artifact = Artifact(kind="DOCUMENT_ORIGINAL", name="PRIVATE_ORIGINAL.pdf", path="unused/original.pdf",
        sha256="b" * 64, size_bytes=10, media_type="application/pdf")
    comparison_artifact = Artifact(kind="COMPARISON_REPORT", name="comparison.sqlite", path="unused/report.sqlite",
        sha256="c" * 64, size_bytes=10, media_type="application/vnd.sqlite3")
    db.add_all([template, original_artifact, comparison_artifact])
    db.flush()
    original = DocumentOriginal(family_id=family.id, template_id=template.id, artifact_id=original_artifact.id,
        filename="Invoice metadata.pdf", sha256=original_artifact.sha256, received_at=utcnow(), uploaded_by_id="test-user", status="PUBLISHED")
    db.add(original)
    db.flush()
    extraction = DocumentExtraction(document_id=original.id, template_id=template.id, revision=1,
        payload={"fields": {"secret": "PRIVATE_EXTRACTED_VALUE"}}, payload_hash="d" * 64, engine={}, reviewed=True, actor_id="test-user")
    batch = DocumentBatch(family_id=family.id, template_id=template.id, idempotency_key="synthetic-lineage", status="PUBLISHED",
        settings={}, published_versions={"headers": version.id}, actor_id="test-user", wait_until=utcnow() + timedelta(minutes=1))
    later = DatasetVersion(dataset_id=dataset.id, version=2, filename="corrected.parquet", source_type="DOCUMENTS",
        sha256="e" * 64, schema_hash=version.schema_hash, size_bytes=1, row_count=1, column_count=1,
        original_path="unused", canonical_path="unused", schema_json=[], profile={}, ingestion_metadata={})
    db.add_all([extraction, batch, later])
    db.flush()
    original.extraction_id = extraction.id
    comparison = VersionComparison(before_version_id=version.id, after_version_id=later.id, actor_id="test-user",
        settings={}, status="SUCCESS", summary={}, artifact_id=comparison_artifact.id)
    db.add(comparison)
    db.flush()
    for relation, left_type, left, right_type, right in [
        ("DOCUMENT_ORIGINAL", "DOCUMENT", original.id, "ARTIFACT", original_artifact.id),
        ("DOCUMENT_EXTRACTION", "DOCUMENT", original.id, "DOCUMENT_EXTRACTION", extraction.id),
        ("EXTRACTION_BATCH", "DOCUMENT_EXTRACTION", extraction.id, "DOCUMENT_BATCH", batch.id),
        ("DOCUMENT_BATCH_MEMBER", "DOCUMENT", original.id, "DOCUMENT_BATCH", batch.id),
        ("DOCUMENT_PUBLICATION", "DOCUMENT_BATCH", batch.id, "DATASET_VERSION", version.id),
        ("COMPARISON_INPUT", "VERSION_COMPARISON", comparison.id, "DATASET_VERSION", version.id),
        ("COMPARISON_INPUT", "VERSION_COMPARISON", comparison.id, "DATASET_VERSION", later.id),
        ("COMPARISON_EVIDENCE", "ARTIFACT", comparison_artifact.id, "VERSION_COMPARISON", comparison.id),
    ]:
        link_artifact(db, dataset.organization_id, relation, left_type, left, right_type, right)
    return {"dataset": dataset.id, "version": version.id, "later": later.id, "document": original.id,
        "extraction": extraction.id, "batch": batch.id, "comparison": comparison.id, "original_artifact": original_artifact.id}


def custom_role(db, permissions):
    role = Role(name="Scoped lineage reader", normalized_name="scoped lineage reader")
    db.add(role)
    db.flush()
    for permission in permissions:
        db.add(RolePermission(role_id=role.id, permission_code=permission))
    db.get(User, "test-user").role_id = role.id
    db.commit()
    return role.id


def graph(client, identities, **extra):
    return client.get(f"/api/v1/catalog/datasets/{identities['dataset']}/lineage-graph",
        params={"version_id": identities["version"], "max_depth": 5, "max_nodes": 100, **extra})


def test_business_lineage_links_original_revision_batch_version_comparison_without_contents(authenticated, database, tmp_path, monkeypatch):
    with database() as db:
        ids = fixture_graph(db, tmp_path)
        db.commit()
    monkeypatch.setattr("trackvance.dataset_scans.profile_paths", lambda *_a, **_k: (_ for _ in ()).throw(AssertionError("no scans")))
    response = graph(authenticated, ids, projection="business")
    assert response.status_code == 200, response.text
    body = response.json()
    nodes = {node["id"]: node for node in body["nodes"]}
    assert {f"DOCUMENT:{ids['document']}", f"DOCUMENT_EXTRACTION:{ids['extraction']}", f"DOCUMENT_BATCH:{ids['batch']}",
        f"VERSION_COMPARISON:{ids['comparison']}"} <= nodes.keys()
    assert nodes[f"DOCUMENT:{ids['document']}"]["href"].endswith(f"?document={ids['document']}")
    assert nodes[f"VERSION_COMPARISON:{ids['comparison']}"]["href"].endswith(f"?report={ids['comparison']}")
    assert "PRIVATE_EXTRACTED_VALUE" not in response.text and "unused/" not in response.text
    assert any(edge["source"] == f"DATASET_VERSION:{ids['version']}" and edge["target"] == f"VERSION_COMPARISON:{ids['comparison']}" for edge in body["edges"])
    assert any("Publicación del lote" in edge["label"] for edge in body["edges"])


def test_dataset_access_and_global_artifact_access_never_grant_original_pdf(authenticated, database, tmp_path):
    with database() as db:
        ids = fixture_graph(db, tmp_path)
        role_id = custom_role(db, ["datasets:read", "catalog:read", "artifacts:download"])
    response = graph(authenticated, ids)
    assert response.status_code == 200, response.text
    assert not any(node["type"].startswith("DOCUMENT") for node in response.json()["nodes"])
    assert "PRIVATE_ORIGINAL.pdf" not in response.text
    with database() as db:
        db.add(RolePermission(role_id=role_id, permission_code="documents:read"))
        db.commit()
    metadata = graph(authenticated, ids).json()
    assert any(node["type"] == "DOCUMENT" for node in metadata["nodes"])
    assert not any(node["resource_id"] == ids["original_artifact"] for node in metadata["nodes"])
    with database() as db:
        db.add(RolePermission(role_id=role_id, permission_code="documents:download"))
        db.commit()
    authorized = graph(authenticated, ids).json()
    assert any(node["resource_id"] == ids["original_artifact"] for node in authorized["nodes"])


def test_comparison_node_rechecks_both_versions_and_graph_bounds(authenticated, database, tmp_path):
    with database() as db:
        ids = fixture_graph(db, tmp_path)
        db.commit()
    bounded = graph(authenticated, ids, projection="business", max_depth=1, max_nodes=10).json()
    assert len(bounded["nodes"]) <= 10 and bounded["complete"] is False
    assert bounded["pending_branches"]
    with database() as db:
        db.add(DatasetBlock(dataset_id=ids["dataset"], scope="CONTENT", reason="Synthetic restriction", created_by_id="test-user"))
        db.commit()
    response = graph(authenticated, ids)
    assert response.status_code == 200, response.text  # Dataset metadata remains available.
    assert not any(node["type"] == "VERSION_COMPARISON" for node in response.json()["nodes"])
