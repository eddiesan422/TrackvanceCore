import json
from datetime import UTC, datetime

import pytest
from ci.docker_retention import apply, approved_project, image_candidate, plan


def image(project="trackvance-bench-12508-82fd9d", **changes):
    return {"Id": "sha256:test", "Config": {"Labels": {"com.docker.compose.project": project}},
            "RepoTags": [project + "-api:latest"], "RepoDigests": [],
            "Created": "2026-09-01T00:00:00Z", "Size": 100, **changes}


def test_protect_installation_unknown_and_preservation_copy():
    for project in ("trackvance-certification", "bikerwash-backend", "trackvance-unknown",
                    "trackvance-v080-test-origininspection-db5e8d38c93b"):
        assert not approved_project(project)
        assert not image_candidate(image(project), set(), datetime(2026, 10, 1, tzinfo=UTC))


def test_retain_consumed_recent_aliased_or_registry_image():
    cutoff = datetime(2026, 10, 1, tzinfo=UTC)
    assert image_candidate(image(), set(), cutoff)
    assert image_candidate(image(RepoDigests=["trackvance-bench-12508-82fd9d-api@sha256:test"]), set(), cutoff)
    assert not image_candidate(image(), {"sha256:test"}, cutoff)
    assert not image_candidate(image(Created="2026-10-03T00:00:00Z"), set(), cutoff)
    assert not image_candidate(image(RepoTags=["trackvance-certification-api:latest"]), set(), cutoff)
    assert not image_candidate(image(RepoDigests=["repo@sha256:test"]), set(), cutoff)


def test_no_container_volume_or_cache_deletions_are_planned():
    state = {"containers": [], "images": [image()], "networks": [],
             "volumes": [{"Name": "unknown"}], "space": ""}
    result = plan(state, 7, datetime(2026, 10, 6, tzinfo=UTC))
    assert len(result["images"]) == 1
    assert "volumes" not in result and "containers" not in result
    assert result["inventory"]["volumes"] == [{"Name": "unknown"}]


def test_apply_rechecks_changed_image_before_any_removal(monkeypatch, tmp_path):
    calls = []
    def fake_docker(*args):
        calls.append(args)
        return json.dumps([image("trackvance-certification")]) if args[:2] == ("image", "inspect") else ""
    monkeypatch.setattr("ci.docker_retention.docker", fake_docker)
    document = {"schema_version": 1, "kind": "SELECTIVE_DOCKER_RETENTION",
                "cutoff": "2026-10-01T00:00:00Z", "images": [image()], "networks": []}
    with pytest.raises(ValueError, match="ownership/reference changed"):
        apply(document, tmp_path / "checkpoint.json")
    assert not any("rm" in args for args in calls)
    assert json.loads((tmp_path / "checkpoint.json").read_text())["removed"]["images"] == []


def test_apply_rechecks_new_network_endpoint_before_removal(monkeypatch):
    network = {"Id": "network-id", "Name": "trackvance-e2e-12508-82fd9d_default",
               "Labels": {"com.docker.compose.project": "trackvance-e2e-12508-82fd9d"}, "Containers": {}}
    calls = []
    def fake_docker(*args):
        calls.append(args)
        return json.dumps([network | {"Containers": {"new-container": {}}}]) if args[:2] == ("network", "inspect") else ""
    monkeypatch.setattr("ci.docker_retention.docker", fake_docker)
    document = {"schema_version": 1, "kind": "SELECTIVE_DOCKER_RETENTION",
                "cutoff": "2026-10-01T00:00:00Z", "images": [], "networks": [network]}
    with pytest.raises(ValueError, match="ownership/endpoints changed"):
        apply(document)
    assert not any("rm" in args for args in calls)
