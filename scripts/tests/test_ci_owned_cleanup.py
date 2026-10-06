"""A deadline cannot authorize deletion of preexisting or unlabelled resources."""
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from ci import owned_cleanup


@pytest.mark.parametrize("labels", [None, {}, {"com.docker.compose.project": "trackvance-certification"},
    {"com.docker.compose.project": "trackvance-v070-test-foo"},
    {"io.trackvance.spark-proof-owner": "trackvance-certification"}])
def test_foreign_or_incomplete_ownership_is_rejected(labels):
    assert owned_cleanup.owner(labels) is None


def test_cleanup_skips_existing_and_foreign_new_containers(monkeypatch):
    before = {"containers": {"existing"}, "volumes": set(), "networks": set()}
    after = {**before, "containers": {"existing", "foreign"}}
    monkeypatch.setattr(owned_cleanup, "snapshot", lambda: after)
    calls = []

    def command(*args):
        calls.append(args)
        assert args == ("inspect", "foreign")
        return json.dumps([{"Id": "foreign", "Config": {"Labels": {"com.docker.compose.project": "user-stack"}}}])

    monkeypatch.setattr(owned_cleanup, "docker", command)
    assert owned_cleanup.cleanup(before)["removed"]["containers"] == []
    assert calls == [("inspect", "foreign")]


def test_changed_owner_cannot_reach_stop_or_removal(monkeypatch):
    before = {"containers": set(), "volumes": set(), "networks": set()}
    monkeypatch.setattr(owned_cleanup, "snapshot", lambda: {**before, "containers": {"new"}})
    iterations = iter(["trackvance-v070-test-deadline-0123456789ab", "trackvance-certification"])

    def command(*args):
        assert args == ("inspect", "new")
        return json.dumps([{"Id": "new", "Config": {"Labels": {"com.docker.compose.project": next(iterations)}}}])

    monkeypatch.setattr(owned_cleanup, "docker", command)
    with pytest.raises(ValueError, match="ownership"):
        owned_cleanup.cleanup(before)


def test_mount_consumer_blocks_owned_volume_deletion(monkeypatch):
    before = {"containers": set(), "volumes": set(), "networks": set()}
    project = "trackvance-v080-test-deadline-0123456789ab"
    name = project + "_data"
    monkeypatch.setattr(owned_cleanup, "snapshot", lambda: {**before, "volumes": {name}})

    def command(*args):
        if args == ("volume", "inspect", name):
            return json.dumps([{"Name": name, "Labels": {"com.docker.compose.project": project}}])
        assert args == ("ps", "-aq", "--filter", "volume=" + name)
        return "foreign-consumer"

    monkeypatch.setattr(owned_cleanup, "docker", command)
    with pytest.raises(ValueError, match="consumer"):
        owned_cleanup.cleanup(before)
