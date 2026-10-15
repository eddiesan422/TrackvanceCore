"""Business projection protects finite budgets and keeps genuine continuations."""

import base64

from sqlalchemy import event
from test_publication_lineage_090 import source

from trackvance.artifactstore import link_artifact
from trackvance.db import utcnow
from trackvance.models import (
    Artifact,
    Configuration,
    DeliveryDestination,
    DeliveryDestinationVersion,
    Role,
    RolePermission,
    Run,
    User,
)
from trackvance.report_models import ReportContext, ReportExecution


def process(db, dataset, version, module="intake", decision="APPROVED", name="Proceso"):
    config = Configuration(name=name, module=module, dataset_id=dataset.id, config={})
    db.add(config)
    db.flush()
    run = Run(name=name, module=module, config_id=config.id, dataset_version_id=version.id,
              initiated_by="Test", status="SUCCESS", decision=decision)
    db.add(run)
    db.flush()
    link_artifact(db, dataset.organization_id, "RUN_INPUT", "RUN", run.id, "DATASET_VERSION", version.id)
    return run


def business(client, dataset_id, **parameters):
    return client.get(f"/api/v1/catalog/datasets/{dataset_id}/lineage-graph",
                      params={"projection": "business", **parameters})


def test_thousand_payloads_and_parts_do_not_spend_business_budget(authenticated, database, tmp_path, monkeypatch):
    with database() as db:
        dataset, version = source(db, tmp_path, "Rest population")
        intake = process(db, dataset, version)
        _, output = source(db, tmp_path, "Approved output")
        link_artifact(db, dataset.organization_id, "RUN_OUTPUT", "RUN", intake.id, "DATASET_VERSION", output.id)
        delivery = process(db, dataset, output, "delivery", "COMMITTED", "Entrega")
        destination = DeliveryDestination(name="Receptor verificable", sink_type="REST")
        db.add(destination)
        db.flush()
        target = DeliveryDestinationVersion(destination_id=destination.id, version=1, config={},
                                            secret_reference="unused", config_hash="a" * 64)
        db.add(target)
        db.flush()
        link_artifact(db, dataset.organization_id, "DELIVERED_TO", "RUN", delivery.id,
                      "DELIVERY_DESTINATION_VERSION", target.id)
        for index in range(1000):
            artifact = Artifact(id=f"payload-{index:04}", kind="REST_BATCH_PAYLOAD", name="Payload",
                                path=f"/unused/{index}", sha256="a" * 64, size_bytes=100,
                                media_type="application/json")
            db.add(artifact)
            db.flush()
            link_artifact(db, dataset.organization_id, "REST_BATCH_PAYLOAD", "RUN", delivery.id, "ARTIFACT", artifact.id)
            if index < 150:
                link_artifact(db, dataset.organization_id, "DATASET_PART", "ARTIFACT",
                              version.canonical_artifact_id, "ARTIFACT", artifact.id)
        dataset_id, destination_id, delivery_id = dataset.id, target.id, delivery.id
        db.commit()
    monkeypatch.setattr("trackvance.dataset_scans.profile_paths", lambda *a, **k: (_ for _ in ()).throw(AssertionError("no scans")))
    queries = []
    from trackvance.db import engine
    def record(_connection, _cursor, statement, _parameters, _context, _many):
        queries.append(statement)
    event.listen(engine, "before_cursor_execute", record)
    try:
        response = business(authenticated, dataset_id, max_depth=4, max_nodes=10)
    finally:
        event.remove(engine, "before_cursor_execute", record)
    assert response.status_code == 200, response.text
    graph = response.json()
    assert graph["complete"] is True
    assert f"DELIVERY_DESTINATION_VERSION:{destination_id}" in {node["id"] for node in graph["nodes"]}
    assert len(graph["nodes"]) == 5
    assert not any(node["technical"] for node in graph["nodes"])
    assert graph["metadata_relationships_scanned"] == 8
    assert len(queries) < 100, "Artifact population must not cause an authorization N+1 in the business graph"
    delivery_node = next(node for node in graph["nodes"] if node["resource_id"] == delivery_id)
    assert delivery_node["technical_groups"] == [{"relation": "REST_BATCH_PAYLOAD",
        "label": "Payloads de lotes REST", "relationship_count": 1000}]
    root = next(node for node in graph["nodes"] if node["id"] == graph["root_id"])
    assert next(group for group in root["technical_groups"] if group["relation"] == "DATASET_PART")["relationship_count"] == 150
    seen, cursor = set(), delivery_node["evidence_cursor"]
    while cursor:
        evidence = business(authenticated, dataset_id, cursor=cursor, evidence="true").json()
        assert evidence["scanned_relationships"] <= 80
        for item in evidence["items"]:
            assert item["artifact"]["id"] not in seen
            assert "path" not in item["artifact"]
            seen.add(item["artifact"]["id"])
        cursor = evidence["next_cursor"]
    assert len(seen) == 1000
    assert any(edge["state"] == "CONFIRMED" for edge in graph["edges"])


def test_directed_continuation_uses_keyset_and_retains_identity(authenticated, database, tmp_path):
    with database() as db:
        dataset, version = source(db, tmp_path, "Many consumers")
        expected = set()
        for index in range(105):
            run = process(db, dataset, version, name="Mismo nombre")
            expected.add(f"RUN:{run.id}")
        dataset_id = dataset.id
        db.commit()
    graph = business(authenticated, dataset_id, max_depth=1, max_nodes=10).json()
    seen = {node["id"] for node in graph["nodes"] if node["type"] == "RUN"}
    root = next(node for node in graph["nodes"] if node["id"] == graph["root_id"])
    upstream = business(authenticated, dataset_id, cursor=root["expand"]["upstream"], max_depth=1, max_nodes=10).json()
    assert len(upstream["nodes"]) == 1
    cursor = next(branch["cursor"] for branch in graph["branches"] if branch["node_id"] == graph["root_id"])
    for _ in range(20):
        page = business(authenticated, dataset_id, cursor=cursor, max_depth=1, max_nodes=10).json()
        assert len(page["nodes"]) <= 10
        current = {node["id"] for node in page["nodes"] if node["type"] == "RUN"}
        assert not current.intersection(seen)
        seen.update(current)
        branch = next((branch for branch in page["branches"] if branch["node_id"] == graph["root_id"]), None)
        if not branch:
            break
        assert branch["cursor"] != cursor
        cursor = branch["cursor"]
    assert seen == expected


def test_cursor_is_scoped_tamper_protected_and_permissions_are_revalidated(authenticated, database, tmp_path):
    with database() as db:
        dataset, version = source(db, tmp_path, "Scope")
        run = process(db, dataset, version)
        other, _ = source(db, tmp_path, "Other root")
        dataset_id, other_id, run_id = dataset.id, other.id, run.id
        db.commit()
    graph = business(authenticated, dataset_id).json()
    run_node = next(node for node in graph["nodes"] if node["resource_id"] == run_id)
    cursor = run_node["expand"]["downstream"]
    assert business(authenticated, other_id, cursor=cursor).status_code == 422
    raw = bytearray(base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4)))
    raw[10] ^= 1
    assert business(authenticated, dataset_id, cursor=base64.urlsafe_b64encode(raw).decode()).status_code == 422
    with database() as db:
        user = db.get(User, "test-user")
        role = Role(name="Catalog only", normalized_name="catalog only", organization_id=user.organization_id)
        db.add(role)
        db.flush()
        user.role_id = role.id
        for permission in ("catalog:read", "datasets:read"):
            db.add(RolePermission(role_id=role.id, permission_code=permission))
        db.commit()
    assert business(authenticated, dataset_id, cursor=cursor).status_code == 422
    visible = business(authenticated, dataset_id).json()
    assert all(node["resource_id"] != run_id for node in visible["nodes"])


def test_duplicate_semantics_group_evidence_and_delivery_states_are_real(authenticated, database, tmp_path):
    with database() as db:
        dataset, version = source(db, tmp_path, "Input")
        _, output = source(db, tmp_path, "Output")
        for relation in ("DERIVED_FROM", "INTAKE_ACCEPTED_FROM"):
            link_artifact(db, dataset.organization_id, relation, "DATASET_VERSION", output.id, "DATASET_VERSION", version.id)
        destination = DeliveryDestination(name="Target", sink_type="REST")
        db.add(destination)
        db.flush()
        target = DeliveryDestinationVersion(destination_id=destination.id, version=1, config={},
                                            secret_reference="unused", config_hash="a" * 64)
        db.add(target)
        db.flush()
        for decision in ("COMMITTED", "PARTIAL", "UNKNOWN"):
            run = process(db, dataset, output, "delivery", decision, decision)
            if decision == "UNKNOWN":
                run.status = "UNKNOWN"
            link_artifact(db, dataset.organization_id, "DELIVERED_TO", "RUN", run.id,
                          "DELIVERY_DESTINATION_VERSION", target.id)
        dataset_id = dataset.id
        db.commit()
    graph = business(authenticated, dataset_id, max_depth=4).json()
    grouped = next(edge for edge in graph["edges"] if edge["evidence_count"] == 2)
    assert set(grouped["stored_relations"]) == {"DERIVED_FROM", "INTAKE_ACCEPTED_FROM"}
    assert len(grouped["evidence_ids"]) == 2
    assert {edge["state"] for edge in graph["edges"] if edge["stored_relation"] == "DELIVERED_TO"} == {"CONFIRMED", "PARTIAL", "UNKNOWN"}


def test_report_recon_multi_input_and_monitoring_remain_distinct(authenticated, database, tmp_path):
    with database() as db:
        dataset, left = source(db, tmp_path, "Left")
        _, right = source(db, tmp_path, "Right")
        _, output = source(db, tmp_path, "Report output")
        recon = process(db, dataset, left, "recon", "CONFORME", "Conciliación")
        recon.target_version_id = right.id
        link_artifact(db, dataset.organization_id, "RUN_REFERENCE", "RUN", recon.id, "DATASET_VERSION", right.id)
        monitor = process(db, dataset, left, "sentinel", "HEALTHY", "Monitoreo")
        context = ReportContext(user_id="test-user", expires_at=utcnow(), snapshot={}, integrity_hash="a" * 64)
        db.add(context)
        db.flush()
        report = ReportExecution(context_id=context.id, user_id="test-user", profile="DATASET", status="SUCCESS")
        db.add(report)
        db.flush()
        for version in (left, right):
            link_artifact(db, dataset.organization_id, "REPORT_SOURCE", "REPORT_EXECUTION", report.id,
                          "DATASET_VERSION", version.id)
        link_artifact(db, dataset.organization_id, "REPORT_OUTPUT", "REPORT_EXECUTION", report.id,
                      "DATASET_VERSION", output.id)
        dataset_id, report_id, recon_id, monitor_id, left_id, right_id = dataset.id, report.id, recon.id, monitor.id, left.id, right.id
        db.commit()
    graph = business(authenticated, dataset_id, max_depth=4).json()
    for target in (f"RUN:{recon_id}", f"REPORT_EXECUTION:{report_id}"):
        assert {edge["source"] for edge in graph["edges"] if edge["target"] == target} == {
            f"DATASET_VERSION:{left_id}", f"DATASET_VERSION:{right_id}"}
    monitoring = next(edge for edge in graph["edges"] if edge["target"] == f"RUN:{monitor_id}")
    assert monitoring["relation"] == "MONITOREA"
    assert next(node for node in graph["nodes"] if node["id"] == monitoring["target"])["role"] == "MONITORING"


def test_empty_business_projection_has_explicit_continuation_contract(authenticated, database):
    from trackvance.models import Dataset
    with database() as db:
        dataset = Dataset(name="Empty")
        db.add(dataset)
        db.flush()
        dataset_id = dataset.id
        db.commit()
    graph = business(authenticated, dataset_id).json()
    assert graph["projection"] == "business"
    assert graph["nodes"] == graph["edges"] == graph["branches"] == []
    assert graph["complete"] is True
    assert graph["anchor_id"] is None
    assert graph["metadata_relationships_scanned"] == 0
    assert graph["limits"]["adjacency_page_size"] == 80
