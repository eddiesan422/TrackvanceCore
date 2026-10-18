"""LIN-097-01: producer ancestry is selected before the finite graph budget."""
from sqlalchemy import func, select
from test_lineage095 import fixture_graph
from test_lineage_business_090 import process
from test_publication_lineage_090 import source

from trackvance.acquisition_models import AcquisitionRun
from trackvance.artifactstore import link_artifact
from trackvance.models import ArtifactLink, ExternalConnection, ExternalConnectionVersion


def origin(client, dataset_id, **params):
    response = client.get(f"/api/v1/catalog/datasets/{dataset_id}/lineage-graph",
        params={"projection": "origin", "max_depth": 4, **params})
    assert response.status_code == 200, response.text
    return response.json()


def test_only_real_producers_and_all_recon_inputs_are_ancestors(authenticated, database, tmp_path):
    with database() as db:
        left, lv = source(db, tmp_path, "left input")
        _, rv = source(db, tmp_path, "right input")
        output, ov = source(db, tmp_path, "output")
        producer = process(db, left, lv, "recon", "CONFORME", "Producer")
        link_artifact(db, left.organization_id, "RUN_REFERENCE", "RUN", producer.id, "DATASET_VERSION", rv.id)
        link_artifact(db, left.organization_id, "RUN_OUTPUT", "RUN", producer.id, "DATASET_VERSION", ov.id)
        observer = process(db, output, ov, "sentinel", "HEALTHY", "Observer")
        consumer = process(db, output, ov, "intake", "APPROVED", "Consumer")
        identities = {"producer": producer.id, "observer": observer.id, "consumer": consumer.id}
        dataset_id, root_id, left_id, right_id = output.id, ov.id, lv.id, rv.id
        count = db.scalar(select(func.count()).select_from(ArtifactLink))
        db.commit()
    graph = origin(authenticated, dataset_id)
    assert {node["resource_id"] for node in graph["nodes"]} == {
        root_id, left_id, right_id, identities["producer"]}
    assert all(edge["source"] != graph["root_id"] for edge in graph["edges"])
    assert graph["complete"] is True
    with database() as db:
        assert db.scalar(select(func.count()).select_from(ArtifactLink)) == count


def test_document_original_extraction_batch_and_version_remain(authenticated, database, tmp_path):
    with database() as db:
        result = fixture_graph(db, tmp_path)
        dataset_id, version_id = result["dataset"], result["version"]
        db.commit()
    graph = origin(authenticated, dataset_id, version_id=version_id)
    kinds = {node["type"] for node in graph["nodes"]}
    assert {"DOCUMENT", "DOCUMENT_EXTRACTION", "DOCUMENT_BATCH", "DATASET_VERSION"} <= kinds
    assert "VERSION_COMPARISON" not in kinds


def test_snapshot_does_not_consume_previous_version_and_consumers_do_not_spend_limit(authenticated, database, tmp_path):
    with database() as db:
        dataset, version = source(db, tmp_path, "snapshot")
        _, previous = source(db, tmp_path, "previous query")
        version.source_type = "REST"
        for relation in ("REFRESH_OF", "DERIVED_FROM"):
            link_artifact(db, dataset.organization_id, relation, "DATASET_VERSION", version.id,
                "DATASET_VERSION", previous.id)
        for index in range(20):
            process(db, dataset, version, name=f"Independent consumer {index}")
        dataset_id, version_id = dataset.id, version.id
        db.commit()
    graph = origin(authenticated, dataset_id, max_nodes=10)
    assert [node["resource_id"] for node in graph["nodes"]] == [version_id]
    assert graph["metadata_relationships_scanned"] == 0
    assert graph["complete"] is True


def test_rest_source_acquisition_and_selected_version_follow_recorded_foreign_key(authenticated, database, tmp_path):
    with database() as db:
        dataset, version = source(db, tmp_path, "rest source")
        version.source_type = "REST"
        connection = ExternalConnection(name="Source", source_type="REST")
        db.add(connection)
        db.flush()
        revision = ExternalConnectionVersion(connection_id=connection.id, version=1, host="synthetic.invalid",
            port=443, database="", username="", secret_reference="never-resolved", config_hash="a" * 64)
        db.add(revision)
        db.flush()
        acquisition = AcquisitionRun(dataset_id=dataset.id, connection_version_id=revision.id, source_type="REST",
            filename="collection", request_hash="b" * 64, initiated_by_id="test-user", initiated_by_name="Test",
            status="SUCCESS", output_version_id=version.id)
        db.add(acquisition)
        db.flush()
        link_artifact(db, dataset.organization_id, "SOURCE_SNAPSHOT", "DATASET_VERSION", version.id,
            "CONNECTION_VERSION", revision.id)
        link_artifact(db, dataset.organization_id, "ACQUISITION_OUTPUT", "ACQUISITION", acquisition.id,
            "DATASET_VERSION", version.id)
        dataset_id, acquisition_id, connection_id, version_id = dataset.id, acquisition.id, revision.id, version.id
        db.commit()
    graph = origin(authenticated, dataset_id)
    assert {node["resource_id"] for node in graph["nodes"]} == {connection_id, acquisition_id, version_id}
    assert {(edge["source"], edge["target"]) for edge in graph["edges"]} == {
        (f"CONNECTION_VERSION:{connection_id}", f"ACQUISITION:{acquisition_id}"),
        (f"ACQUISITION:{acquisition_id}", f"DATASET_VERSION:{version_id}"),
    }
    # The derived projection is backed by a recorded FK and never fabricates a stored link.
    assert any(edge["id"].startswith("FK:ACQUISITION_SOURCE:") for edge in graph["edges"])


def test_continuations_are_scoped_to_origin_and_cannot_expand_consumers(authenticated, database, tmp_path):
    with database() as db:
        dataset, version = source(db, tmp_path, "cursor child")
        _, parent = source(db, tmp_path, "cursor parent")
        _, grandparent = source(db, tmp_path, "cursor ancestor")
        link_artifact(db, dataset.organization_id, "DERIVED_FROM", "DATASET_VERSION", version.id, "DATASET_VERSION", parent.id)
        link_artifact(db, dataset.organization_id, "DERIVED_FROM", "DATASET_VERSION", parent.id, "DATASET_VERSION", grandparent.id)
        dataset_id, grandparent_id = dataset.id, grandparent.id
        db.commit()
    graph = origin(authenticated, dataset_id, max_depth=1)
    assert graph["complete"] is False
    assert all(set(node["expand"] or {}) <= {"upstream"} for node in graph["nodes"])
    cursor = graph["branches"][0]["cursor"]
    assert grandparent_id in {node["resource_id"] for node in origin(authenticated, dataset_id, cursor=cursor)["nodes"]}
    assert authenticated.get(f"/api/v1/catalog/datasets/{dataset_id}/lineage-graph",
        params={"projection": "business", "cursor": cursor}).status_code == 422
