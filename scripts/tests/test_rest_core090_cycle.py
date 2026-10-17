"""The real synthetic receiver rejects corrupt late values and atomic duplicates."""

import copy
import hashlib
import importlib.util
import json
import threading
import urllib.error
import urllib.request
from pathlib import Path

import pytest

module_spec = importlib.util.spec_from_file_location("rest_core090_cycle", Path(__file__).with_name("rest_core090_cycle.py"))
cycle = importlib.util.module_from_spec(module_spec)
module_spec.loader.exec_module(cycle)


@pytest.fixture
def server(monkeypatch, tmp_path):
    monkeypatch.setenv("TRACKVANCE_CERTIFICATION_PROJECT", "trackvance-codex")
    monkeypatch.setenv("TRACKVANCE_REST_FIXTURE_DIR", str(tmp_path))
    http = cycle.receiver(25, 0, str(tmp_path / "rest-core090-unit.sqlite"), serve_forever=False)
    thread = threading.Thread(target=http.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{http.server_port}"
    http.shutdown()
    http.server_close()
    thread.join(timeout=2)


def send(server, identity, records):
    body = json.dumps({"batch_id": identity, "records": records}, separators=(",", ":")).encode()
    digest = hashlib.sha256(body).hexdigest()
    request = urllib.request.Request(server + "/write", data=body, headers={"Content-Type": "application/json",
        "Idempotency-Key": identity, "X-Trackvance-Payload-SHA256": digest})
    try:
        with urllib.request.urlopen(request, timeout=5) as response:
            return response.status, json.load(response)
    except urllib.error.HTTPError as error:
        return error.code, json.load(error)


def receipt(server):
    with urllib.request.urlopen(server + "/receipt", timeout=5) as response:
        return json.load(response)


def test_receiver_guard_rejects_permanent_installation_and_unowned_path(monkeypatch, tmp_path):
    for project in ("trackvance-certification", "bikerwash", "", "trackvance-codex-copy"):
        monkeypatch.setenv("TRACKVANCE_CERTIFICATION_PROJECT", project)
        with pytest.raises(RuntimeError):
            cycle.receiver(1, 0, str(tmp_path / "rest-core090-guard.sqlite"), serve_forever=False)
    monkeypatch.setenv("TRACKVANCE_CERTIFICATION_PROJECT", "trackvance-codex")
    monkeypatch.setenv("TRACKVANCE_REST_FIXTURE_DIR", str(tmp_path))
    with pytest.raises(RuntimeError):
        cycle.receiver(1, 0, str(tmp_path / "metadata.sqlite"), serve_forever=False)
    with pytest.raises(RuntimeError):
        cycle.receiver(1, 0, str(tmp_path.parent / "rest-core090-other.sqlite"), serve_forever=False)
    assert not (tmp_path / "metadata.sqlite").exists()


def test_receiver_real_idempotency_reuses_identity_and_payload(server):
    records = [cycle.synthetic_row(i) for i in range(1, 26)]
    first, replay = send(server, "same-batch", records), send(server, "same-batch", records)
    assert first == replay and first[0] == 200
    state = receipt(server)
    assert state["received_rows"] == 25 and state["batches"] == 1
    assert state["population_sha256"] == state["expected_population_sha256"]
    status, response = send(server, "same-batch", records[:-1])
    assert status == 422 and response["accepted"] is False
    assert receipt(server)["received_rows"] == 25


def test_duplicate_ids_roll_back_whole_later_batch(server):
    assert send(server, "first", [cycle.synthetic_row(1)])[0] == 200
    status, response = send(server, "duplicate", [cycle.synthetic_row(2), cycle.synthetic_row(1)])
    assert status == 422 and response["rows"] == 0
    state = receipt(server)
    assert state["received_rows"] == 1 and state["batches"] == 1
    assert send(server, "second", [cycle.synthetic_row(2)])[0] == 200


def test_full_population_oracle_detects_last_row_mutation(server):
    rows = [cycle.synthetic_row(i) for i in range(1, 26)]
    rows[-1]["amount"] = "0.0000"
    assert send(server, "late-corruption", rows)[0] == 422
    assert receipt(server)["received_rows"] == 0
    rows[-1] = cycle.synthetic_row(25)
    assert send(server, "complete", rows)[0] == 200
    state = receipt(server)
    assert state["received_rows"] == 25 and state["population_sha256"] == state["expected_population_sha256"]


def test_source_pagination_preserves_complete_population_and_empty_terminal_page(server):
    observed = []
    for page in range(1, 5):
        with urllib.request.urlopen(server + f"/source?page={page}&page_size=10", timeout=5) as response:
            rows = json.load(response)["records"]
        assert len(rows) == [10, 10, 5, 0][page - 1]
        observed.extend(rows)
    assert observed == [cycle.synthetic_row(i) for i in range(1, 26)]


def test_core095_controlled_get_faults_have_durable_same_page_sequence_and_exact_population(monkeypatch, tmp_path):
    monkeypatch.setenv("TRACKVANCE_CERTIFICATION_PROJECT", "trackvance-codex")
    monkeypatch.setenv("TRACKVANCE_REST_FIXTURE_DIR", str(tmp_path))
    http = cycle.receiver(25, 0, str(tmp_path / "rest-core090-faults.sqlite"), serve_forever=False, transient_faults=True)
    thread = threading.Thread(target=http.serve_forever, daemon=True)
    thread.start()
    base, population = f"http://127.0.0.1:{http.server_port}", []
    try:
        page = 1
        while True:
            try:
                with urllib.request.urlopen(base + f"/source?page={page}&page_size=5", timeout=5) as response:
                    records = json.load(response)["records"]
            except urllib.error.HTTPError as error:
                assert error.code == 503
                error.close()
                continue
            if not records:
                break
            population.extend(records)
            page += 1
        assert population == [cycle.synthetic_row(i) for i in range(1, 26)]
        value = cycle.receiver_json(base, "/source-receipt")
        proof = cycle.source_request_proof(value, 25, 5)
        assert proof["transient_failures"] == 3 and proof["successful_pages"] == 6
        assert [item["page"] for item in proof["requests"]] == [1, 2, 2, 3, 4, 5, 5, 5, 6]
        assert value["faults"] == [{"page": 2, "remaining": 0, "configured": 1}, {"page": 5, "remaining": 0, "configured": 2}]
        source_cursor = value["last_seq"]
        assert cycle.receiver_json(base, f"/source-receipt?after_seq={source_cursor}")["requests"] == []
        # Fault state is consumed durably; a new logical read does not inject a new write or reset it.
        with urllib.request.urlopen(base + "/source?page=2&page_size=5", timeout=5) as response:
            assert response.status == 200
        assert receipt(base)["post_calls"] == 0
    finally:
        http.shutdown(); http.server_close(); thread.join(timeout=2)


@pytest.mark.parametrize("damage", ["skip", "repeat_success", "parameters", "truncated", "false_faults"])
def test_core095_page_oracle_rejects_skips_duplicates_parameter_changes_or_incomplete_logs(damage):
    requests = [{"seq": index, "endpoint": "/source", "page": page, "page_size": 10, "status": status, "at": "2026-10-16T12:00:00Z"}
        for index, (page, status) in enumerate([(1, 200), (2, 503), (2, 200), (3, 200), (4, 200)], 1)]
    value = {"complete": True, "transient_faults": True, "requests": requests}
    cycle.source_request_proof(value, 25, 10)
    if damage == "skip":
        del requests[2]
    elif damage == "repeat_success":
        requests.append({**requests[-1], "seq": 6})
    elif damage == "parameters":
        requests[1]["page_size"] = 20
    elif damage == "truncated":
        value["complete"] = False
    else:
        value["transient_faults"] = False
    with pytest.raises(AssertionError):
        cycle.source_request_proof(value, 25, 10)


def committed_batches(server):
    result = []
    for identity, first, last in (("z-first", 1, 11), ("a-last", 11, 26)):
        records = [cycle.synthetic_row(i) for i in range(first, last)]
        body = json.dumps({"batch_id": identity, "records": records}, separators=(",", ":")).encode()
        status, confirmation = send(server, identity, records)
        assert status == 200
        result.append({"id": identity, "payload_sha256": confirmation["payload_sha256"],
                       "rows": confirmation["rows"], "payload_bytes": len(body)})
    return result


def test_batch_metadata_matches_independent_durable_receiver_in_id_order(server):
    batches = committed_batches(server)
    state = receipt(server)
    assert state["received_rows"] == state["confirmed_rows"] == 25
    assert state["batches"] == 2 and state["payload_bytes"] == sum(batch["payload_bytes"] for batch in batches)
    assert state["batch_metadata_sha256"] == cycle.batch_metadata_digest(batches)
    assert state["batch_metadata_sha256"] == cycle.batch_metadata_digest(list(reversed(batches)))
    assert state["population_sha256"] == state["expected_population_sha256"]


@pytest.mark.parametrize(("field", "corrupt"), [("id", "other-batch"), ("payload_sha256", "0" * 64),
                                               ("rows", 1), ("payload_bytes", 1)])
def test_batch_metadata_oracle_detects_altered_api_evidence(server, field, corrupt):
    batches = committed_batches(server)
    batches[-1][field] = corrupt
    state = receipt(server)
    assert state["confirmed_rows"] == 25 and state["batches"] == 2
    assert state["batch_metadata_sha256"] != cycle.batch_metadata_digest(batches)


def partial_lineage_views():
    def node(kind, identity, **fields):
        return {"id": f"{kind}:{identity}", "resource_id": identity, "type": kind, **fields}
    core_nodes = [node("DATASET_VERSION", "source", dataset_id="source-dataset"),
                  node("DATASET_VERSION", "output", dataset_id="output-dataset"),
                  node("RUN", "intake", status="SUCCESS", decision="APPROVED", module="intake"),
                  node("RUN", "delivery", status="SUCCESS", decision="COMMITTED", module="DELIVERY")]
    def edge(identity, relation, stored_source, stored_target, source, target, state="RECORDED"):
        return {"id": identity, "stored_relation": relation, "stored_source": stored_source,
                "stored_target": stored_target, "source": source, "target": target, "state": state}
    core_edges = [edge("input", "RUN_INPUT", "RUN:intake", "DATASET_VERSION:source", "DATASET_VERSION:source", "RUN:intake"),
                  edge("output", "RUN_OUTPUT", "RUN:intake", "DATASET_VERSION:output", "RUN:intake", "DATASET_VERSION:output"),
                  edge("derived", "DERIVED_FROM", "DATASET_VERSION:output", "DATASET_VERSION:source", "DATASET_VERSION:source", "DATASET_VERSION:output"),
                  edge("delivery", "DELIVERY_INPUT", "DATASET_VERSION:output", "RUN:delivery", "DATASET_VERSION:output", "RUN:delivery")]
    source = {"root_id": "DATASET_VERSION:source", "version_id": "source", "nodes": core_nodes + [node("ARTIFACT", f"source-{i}") for i in range(496)],
              "edges": core_edges, "complete": False, "pending_branches": ["RUN:delivery"], "limits": {"max_depth": 8, "max_nodes": 500}}
    output = {"root_id": "DATASET_VERSION:output", "version_id": "output",
              "nodes": core_nodes + [node("DELIVERY_DESTINATION_VERSION", "destination")] + [node("ARTIFACT", f"output-{i}") for i in range(495)],
              "edges": core_edges + [edge("confirmed", "DELIVERED_TO", "RUN:delivery", "DELIVERY_DESTINATION_VERSION:destination",
                                          "RUN:delivery", "DELIVERY_DESTINATION_VERSION:destination", "CONFIRMED")],
              "complete": False, "pending_branches": ["ARTIFACT:output-0"], "limits": {"max_depth": 8, "max_nodes": 500}}
    return source, output


def prove_views(source, output):
    class ReadOnlyClient:
        def __init__(self):
            self.calls = []
        def call(self, path):
            self.calls.append(path)
            assert path in ["/catalog/datasets/source-dataset/lineage-graph?version_id=source&max_nodes=500&max_depth=8",
                            "/catalog/datasets/output-dataset/lineage-graph?version_id=output&max_nodes=500&max_depth=8"]
            return copy.deepcopy(source if "version_id=source&" in path else output)
    client = ReadOnlyClient()
    proof = cycle.lineage_proof(client, "source-dataset", "source", "output-dataset", "output", "intake", "delivery", "destination")
    return proof, client.calls


def test_lineage_combines_two_actual_bounded_views_without_claiming_full_traversal():
    proof, calls = prove_views(*partial_lineage_views())
    assert len(calls) == proof["view_count"] == 2
    assert proof["result"] == "PASS" and proof["core_links_complete"] is True and proof["core_edge_count"] == 5
    assert proof["complete"] is False and proof["pending_branches"] == ["ARTIFACT:output-0", "RUN:delivery"]
    assert all(view["nodes"] == 500 and view["complete"] is False for view in proof["views"])
    assert proof["missing_critical_node_ids"] == [] and len(proof["critical_node_ids"]) == 5
    assert len(proof["metadata_sha256"]) == len(proof["core_edges_sha256"]) == 64


@pytest.mark.parametrize("damage", ["missing-destination", "unconfirmed", "wrong-stored-id", "wrong-display-id"])
def test_lineage_refuses_missing_or_uncertain_or_unrelated_delivery(damage):
    source, output = partial_lineage_views()
    if damage == "missing-destination":
        output["nodes"] = [node for node in output["nodes"] if node["type"] != "DELIVERY_DESTINATION_VERSION"]
        output["edges"] = output["edges"][:-1]
    elif damage == "unconfirmed":
        output["edges"][-1]["state"] = "UNCONFIRMED"
    elif damage == "wrong-stored-id":
        output["edges"][-1]["stored_target"] = "DELIVERY_DESTINATION_VERSION:foreign"
    else:
        output["edges"][-1]["target"] = "ARTIFACT:output-0"
    proof, _ = prove_views(source, output)
    assert proof["result"] == "FAIL" and proof["core_links_complete"] is False and proof["core_edge_count"] == 4


def test_lineage_refuses_response_root_from_another_dataset():
    source, output = partial_lineage_views()
    output["nodes"][1]["dataset_id"] = "foreign"
    with pytest.raises(AssertionError):
        prove_views(source, output)


def test_lineage_never_skips_authorization_when_selecting_approved_output():
    source, _ = partial_lineage_views()
    class DeniedClient:
        def call(self, path):
            if "version_id=output&" in path:
                raise AssertionError("HTTP 403 expected 200")
            return source
    with pytest.raises(AssertionError, match="HTTP 403"):
        cycle.lineage_proof(DeniedClient(), "source-dataset", "source", "output-dataset", "output", "intake", "delivery", "destination")


def test_lineage_refuses_product_response_above_existing_node_limit():
    source, output = partial_lineage_views()
    source["nodes"].append({"id": "ARTIFACT:too-many", "type": "ARTIFACT", "resource_id": "too-many"})
    with pytest.raises(AssertionError):
        prove_views(source, output)


def business_pages():
    source, output = partial_lineage_views()
    source_nodes = [item for item in source["nodes"] if item["type"] != "ARTIFACT"]
    destination = next(item for item in output["nodes"] if item["type"] == "DELIVERY_DESTINATION_VERSION")
    for item in source_nodes:
        item["technical_groups"] = ([{"relation": "REST_BATCH_PAYLOAD", "relationship_count": 1000}]
                                    if item["id"] == "RUN:delivery" else [])
    def page(anchor, nodes, edges, pending):
        return {"projection": "business", "root_id": "DATASET_VERSION:source", "anchor_id": anchor,
                "version_id": "source", "nodes": nodes, "edges": [{**edge, "stored_relations": [edge["stored_relation"]],
                    "evidence_count": 1, "evidence_ids": [edge["id"]]} for edge in edges],
                "complete": not pending, "pending_branches": ["RUN:delivery"] if pending else [],
                "branches": [{"node_id": "RUN:delivery", "direction": "downstream", "cursor": "signed-branch"}] if pending else [],
                "limits": {"max_nodes": 100, "max_depth": 4, "adjacency_page_size": 80, "max_path": 64},
                "metadata_relationships_scanned": len(edges)}
    return [page("DATASET_VERSION:source", source_nodes, source["edges"], True),
            page("RUN:delivery", [source_nodes[-1], destination], output["edges"][-1:], False)]


def prove_business(pages):
    class Client:
        def __init__(self):
            self.calls = []
        def call(self, path):
            self.calls.append(path)
            assert path.startswith("/catalog/datasets/source-dataset/lineage-graph?projection=business&version_id=source&")
            assert "max_nodes=100&max_depth=4" in path
            assert ("cursor=signed-branch" in path) == (len(self.calls) > 1)
            return copy.deepcopy(pages[min(len(self.calls) - 1, len(pages) - 1)])
    client = Client()
    proof = cycle.business_lineage_proof(client, "source-dataset", "source", "output", "intake", "delivery", "destination", 1000)
    return proof, client.calls


def test_business_oracle_follows_real_branch_without_rerooting_or_increasing_global_limits():
    proof, calls = prove_business(business_pages())
    assert proof["result"] == "PASS" and proof["single_source_root"] is True
    assert len(calls) == proof["view_count"] == 2
    assert proof["grouped_batch_payloads"] == 1000 and proof["artifact_nodes"] == 0
    assert proof["browser_review"] == "SEPARATE_REQUIRED_EVIDENCE"


@pytest.mark.parametrize("damage", ["no-cursor", "wrong-root", "artifact-spends-budget", "unknown-delivery", "wrong-payload-count"])
def test_business_oracle_refuses_incomplete_or_untruthful_interface_projection(damage):
    pages = business_pages()
    if damage == "no-cursor":
        pages[0]["branches"] = []
    elif damage == "wrong-root":
        pages[1]["root_id"] = "DATASET_VERSION:output"
    elif damage == "artifact-spends-budget":
        pages[0]["nodes"].append({"id": "ARTIFACT:hidden", "type": "ARTIFACT", "resource_id": "hidden"})
    elif damage == "unknown-delivery":
        pages[1]["edges"][0]["state"] = "UNKNOWN"
    else:
        pages[0]["nodes"][-1]["technical_groups"][0]["relationship_count"] = 999
    with pytest.raises(AssertionError):
        prove_business(pages)
