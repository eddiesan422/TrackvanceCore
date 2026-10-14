"""Transactional publication and metadata-only, version-scoped lineage."""

from sqlalchemy import func, select

from trackvance.artifactstore import link_artifact
from trackvance.automation_models import OutboxEvent
from trackvance.governance_models import DatasetBlock
from trackvance.models import (
    Artifact,
    Configuration,
    Dataset,
    DatasetVersion,
    Role,
    RolePermission,
    Run,
    User,
)
from trackvance.publication import publish_version_event
from trackvance.services import create_version


def source(db, tmp_path, name, text="id,category\n001,A\n002,A\n"):
    path = tmp_path / (name + ".csv")
    path.write_text(text, encoding="utf-8")
    dataset = Dataset(name=name)
    db.add(dataset)
    db.flush()
    return dataset, create_version(db, dataset, path, path.name)


def test_publication_event_atomic_replay_and_exact_profile(database, tmp_path):
    with database() as db:
        _, version = source(db, tmp_path, "atomic", "id,category\n001,A\n001,A\n002,A\n")
        identity = version.id
        event = publish_version_event(db, version)
        assert event.payload["publication_status"] == "COMPLETE"
        assert event.payload["row_count"] == 3
        assert version.profile["duplicate_row_count"] == 1
        assert version.profile["distinct_row_count"] == 2
        db.rollback()
    with database() as db:
        assert db.get(DatasetVersion, identity) is None
        assert db.scalar(select(OutboxEvent).where(OutboxEvent.aggregate_id == identity)) is None
        _, version = source(db, tmp_path, "committed")
        first = publish_version_event(db, version)
        second = publish_version_event(db, version)
        assert first is second
        identity = version.id
        db.commit()
    with database() as db:
        assert db.scalar(select(func.count()).select_from(OutboxEvent).where(
            OutboxEvent.aggregate_id == identity, OutboxEvent.event_type == "DATASET_VERSION_PUBLISHED")) == 1


def test_graph_reverses_derivation_selected_version_and_expands_metadata(authenticated, database, tmp_path, monkeypatch):
    with database() as db:
        _, v1 = source(db, tmp_path, "parent")
        child, v2 = source(db, tmp_path, "child")
        link_artifact(db, child.organization_id, "DERIVED_FROM", "DATASET_VERSION", v2.id, "DATASET_VERSION", v1.id)
        _, v3 = source(db, tmp_path, "other")
        link_artifact(db, child.organization_id, "DERIVED_FROM", "DATASET_VERSION", v1.id, "DATASET_VERSION", v3.id)
        path = tmp_path / "new.csv"
        path.write_text("id,category\n999,Z\n", encoding="utf-8")
        latest = create_version(db, child, path, path.name)
        child_id, selected_id, parent_id, other_id, latest_id = child.id, v2.id, v1.id, v3.id, latest.id
        db.commit()
    monkeypatch.setattr("trackvance.dataset_scans.profile_paths", lambda *a, **k: (_ for _ in ()).throw(AssertionError("metadata view must not scan")))
    response = authenticated.get(f"/api/v1/catalog/datasets/{child_id}/lineage-graph?version_id={selected_id}&max_depth=1")
    assert response.status_code == 200, response.text
    graph = response.json()
    assert graph["root_id"] == f"DATASET_VERSION:{selected_id}"
    assert f"DATASET_VERSION:{latest_id}" not in {node["id"] for node in graph["nodes"]}
    edge = next(edge for edge in graph["edges"] if edge["stored_relation"] == "DERIVED_FROM" and edge["stored_source"] == f"DATASET_VERSION:{selected_id}")
    assert edge["source"] == f"DATASET_VERSION:{parent_id}"
    assert edge["target"] == f"DATASET_VERSION:{selected_id}"
    assert graph["complete"] is False
    expanded = authenticated.get(f"/api/v1/catalog/datasets/{child_id}/lineage-graph?version_id={selected_id}&max_depth=4").json()
    assert f"DATASET_VERSION:{other_id}" in {node["id"] for node in expanded["nodes"]}
    assert not any("path" in node for node in expanded["nodes"])


def test_graph_sentinel_monitoring_and_foreign_nodes_excluded(authenticated, database, tmp_path):
    with database() as db:
        dataset, version = source(db, tmp_path, "monitored")
        config = Configuration(name="monitor", module="sentinel", dataset_id=dataset.id, config={})
        db.add(config)
        db.flush()
        run = Run(name="Observation", module="sentinel", config_id=config.id,
                  dataset_version_id=version.id, initiated_by="Test", status="SUCCESS", decision="HEALTHY")
        db.add(run)
        db.flush()
        link_artifact(db, dataset.organization_id, "RUN_INPUT", "RUN", run.id, "DATASET_VERSION", version.id)
        foreign = Dataset(name="Foreign", organization_id="other-organization")
        db.add(foreign)
        db.flush()
        link_artifact(db, dataset.organization_id, "DERIVED_FROM", "DATASET_VERSION", version.id, "DATASET_VERSION", "foreign-version")
        identity = dataset.id
        db.commit()
    graph = authenticated.get(f"/api/v1/catalog/datasets/{identity}/lineage-graph").json()
    monitor = next(node for node in graph["nodes"] if node["type"] == "RUN")
    assert monitor["role"] == "MONITORING"
    assert any(edge["relation"] == "MONITOREA" for edge in graph["edges"])
    assert not any(node["resource_id"] == "foreign-version" for node in graph["nodes"])
    assert authenticated.get(f"/api/v1/catalog/datasets/{identity}/lineage-graph?max_nodes=501").status_code == 422


def test_shared_artifact_requires_all_domain_permissions_and_orphans_are_hidden(authenticated, database, tmp_path):
    with database() as db:
        dataset, version = source(db, tmp_path, "artifact-owner")
        blocked, restricted = source(db, tmp_path, "restricted-source")
        shared = db.get(Artifact, version.canonical_artifact_id)
        restricted.canonical_artifact_id = shared.id
        db.add(DatasetBlock(dataset_id=blocked.id, scope="CONTENT", reason="Restricted source",
                            created_by_id="test-user"))
        orphan = Artifact(kind="RUN_MANIFEST", name="unproven.json", path="/unproven.json",
                          sha256="a" * 64, size_bytes=1, media_type="application/json")
        db.add(orphan)
        db.flush()
        link_artifact(db, dataset.organization_id, "VERSION_ARTIFACT", "DATASET_VERSION", version.id, "ARTIFACT", shared.id)
        link_artifact(db, dataset.organization_id, "VERSION_ARTIFACT", "DATASET_VERSION", version.id, "ARTIFACT", orphan.id)
        identity, hidden = dataset.id, {shared.id, orphan.id}
        db.commit()
    response = authenticated.get(f"/api/v1/catalog/datasets/{identity}/lineage-graph")
    assert response.status_code == 200, response.text
    graph = response.json()
    assert graph["root_id"] == f"DATASET_VERSION:{version.id}"
    assert not any(node["resource_id"] in hidden for node in graph["nodes"])
    assert not any(any(item.endswith(identity) for identity in hidden) for item in graph["pending_branches"])


def test_hidden_module_nodes_and_their_artifacts_are_not_exposed(authenticated, database, tmp_path):
    with database() as db:
        dataset, version = source(db, tmp_path, "permissions")
        user = db.get(User, "test-user")
        role = Role(name="Catalog reader", normalized_name="catalog reader", organization_id=user.organization_id)
        db.add(role)
        db.flush()
        user.role_id = role.id
        for code in ("catalog:read", "datasets:read", "artifacts:download"):
            db.add(RolePermission(role_id=role.id, permission_code=code))
        config = Configuration(name="Hidden monitor", module="sentinel", dataset_id=dataset.id, config={})
        db.add(config)
        db.flush()
        run = Run(name="Hidden observation", module="sentinel", config_id=config.id,
                  dataset_version_id=version.id, initiated_by="Test", status="SUCCESS", decision="HEALTHY")
        db.add(run)
        db.flush()
        evidence = Artifact(kind="RUN_MANIFEST", name="hidden.json", path="/hidden.json", sha256="a" * 64,
                            size_bytes=1, media_type="application/json")
        db.add(evidence)
        db.flush()
        link_artifact(db, dataset.organization_id, "RUN_INPUT", "RUN", run.id, "DATASET_VERSION", version.id)
        link_artifact(db, dataset.organization_id, "RUN_OUTPUT", "RUN", run.id, "ARTIFACT", evidence.id)
        link_artifact(db, dataset.organization_id, "VERSION_ARTIFACT", "DATASET_VERSION", version.id, "ARTIFACT", evidence.id)
        identity, hidden = dataset.id, {run.id, config.id, evidence.id}
        db.commit()
    response = authenticated.get(f"/api/v1/catalog/datasets/{identity}/lineage-graph")
    assert response.status_code == 200, response.text
    graph = response.json()
    assert graph["nodes"]
    assert not any(node["resource_id"] in hidden for node in graph["nodes"])


def test_deleted_contract_preserves_versioned_history_link(authenticated, database, tmp_path):
    with database() as db:
        dataset, version = source(db, tmp_path, "deleted-history")
        config = Configuration(name="Historical contract", module="intake", dataset_id=dataset.id,
                               config={"max_error_rate": 0}, status="DELETED")
        db.add(config)
        db.flush()
        run = Run(name="Historical approved run", module="intake", config_id=config.id,
                  dataset_version_id=version.id, initiated_by="Test", status="SUCCESS", decision="APPROVED")
        db.add(run)
        db.flush()
        link_artifact(db, dataset.organization_id, "RUN_INPUT", "RUN", run.id, "DATASET_VERSION", version.id)
        link_artifact(db, dataset.organization_id, "USES_CONFIGURATION", "RUN", run.id, "CONFIGURATION", config.id)
        identity, contract_id = dataset.id, config.id
        db.commit()
    graph = authenticated.get(f"/api/v1/catalog/datasets/{identity}/lineage-graph?max_depth=3").json()
    node = next(item for item in graph["nodes"] if item["resource_id"] == contract_id)
    assert node["status"] == "DELETED"
    assert node["href"] == f"/intake?contract_id={contract_id}&include_deleted=true"
