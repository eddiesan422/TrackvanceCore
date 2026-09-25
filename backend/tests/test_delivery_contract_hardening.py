"""Golden checks for additive HTTP metadata and the unchanged directed graph."""

import json
from pathlib import Path

from sqlalchemy import select
from test_delivery_service_api import (
    delivery_case as delivery_case,  # noqa: PLC0414
)
from test_delivery_service_api import (
    delivery_runtime as delivery_runtime,  # noqa: PLC0414
)
from test_delivery_service_api import execute_queued, publish_configuration, queue_run

from trackvance import __version__
from trackvance.api import app
from trackvance.models import ArtifactLink


def test_openapi_snapshot_matches_current_runtime_without_encoding_loss():
    snapshot = Path(__file__).resolve().parents[1] / "openapi.json"
    text = snapshot.read_text(encoding="utf-8")
    assert "\ufffd" not in text
    document = json.loads(text)
    assert document == app.openapi()
    assert document["info"]["version"] == __version__
    assert set(document["paths"]["/api/v1/delivery/runs/{run_id}/reviews"]) == {"get", "post"}
    assert "post" in document["paths"]["/api/v1/delivery/runs/{run_id}/repair-evidence"]


def test_delivery_graph_has_canonical_relations_and_endpoint_types(
    authenticated, database, delivery_case,
):
    configuration = publish_configuration(authenticated, delivery_case)
    run = queue_run(authenticated, configuration["id"], delivery_case.source["version_id"], "graph")
    execute_queued(database, run["id"])
    with database() as db:
        links = db.scalars(select(ArtifactLink)).all()
        triples = {(item.source_type, item.relation, item.target_type) for item in links}
        assert {
            ("DATASET_VERSION", "DELIVERY_INPUT", "RUN"),
            ("RUN", "DELIVERED_TO", "DELIVERY_DESTINATION_VERSION"),
            ("RUN", "DELIVERY_RECEIPT", "ARTIFACT"),
            ("ARTIFACT", "EVIDENCE_OF", "DELIVERY_ATTEMPT"),
            ("RUN", "RUN_OUTPUT", "ARTIFACT"),
        } <= triples
        assert all(item.relation != "DELIVERY_DESTINATION_VERSION" for item in links)
        outgoing = [item for item in links if item.source_type == "RUN" and item.source_id == run["id"]]
        assert sum(item.relation == "RUN_OUTPUT" for item in outgoing) == 2
        assert sum(item.relation == "DELIVERY_RECEIPT" for item in outgoing) == 1
