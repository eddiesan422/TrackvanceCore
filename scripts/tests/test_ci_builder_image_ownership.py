"""Infrastructure image cleanup requires a recorded builder/container chain."""
import json

import pytest
from ci import local_resources, run_local

EXECUTION = "local-" + "a" * 32
BUILDER = "tv-local-build-" + "a" * 12
OWNED = "sha256:" + "b" * 64
EXTERNAL = "sha256:" + "c" * 64
TAG = "moby/buildkit:buildx-stable-1"


def registry(directory, *, owned=False):
    (directory / "builder-images.before.json").write_text("[]")
    (directory / "owned-builders.json").write_text(json.dumps([BUILDER] if owned else []))
    value = {"schema_version": 1, "execution_id": EXECUTION, "builders": []}
    if owned:
        value["builders"].append({"name": BUILDER, "execution_id": EXECUTION,
            "source_sha": "d" * 40, "container_name": "buildx_buildkit_" + BUILDER + "0",
            "baseline_image_ids": [], "status": "OWNED", "container_id": "e" * 64,
            "image": {"Id": OWNED, "RepoTags": [TAG], "RepoDigests": ["moby/buildkit@sha256:" + "f" * 64]}})
    (directory / "owned-builder-images.json").write_text(json.dumps(value))


class Docker:
    def __init__(self, *, owned=False, consumer=False, drift=False, acknowledge_only=False):
        self.images = {EXTERNAL: {"Id": EXTERNAL, "RepoTags": [TAG], "RepoDigests": []}}
        if owned:
            self.images[OWNED] = {"Id": OWNED, "RepoTags": [],
                "RepoDigests": ["moby/buildkit@sha256:" + "f" * 64]}
        self.consumer, self.drift, self.removed = consumer, drift, []
        self.acknowledge_only = acknowledge_only
        self.inspections = 0

    def __call__(self, *args):
        if args[:3] == ("docker", "image", "ls"):
            return "\n".join(self.images if "-aq" in args else [EXTERNAL])
        if args[:3] == ("docker", "image", "inspect"):
            self.inspections += 1
            row = dict(self.images[args[-1]])
            if self.drift and args[-1] == OWNED and self.inspections > 1:
                row["RepoTags"] = ["protected:foreign"]
            return json.dumps([row])
        if args[:2] == ("docker", "ps"):
            return "foreign-container" if self.consumer else ""
        if args[:3] == ("docker", "image", "rm"):
            self.removed.append(args[-1])
            if args[-1] in self.images and not self.acknowledge_only:
                self.images.pop(args[-1])
            return ""
        raise AssertionError(args)


@pytest.mark.parametrize("consumer", [False, True])
def test_new_external_buildkit_image_is_preserved_without_own_build_intent(tmp_path, monkeypatch, consumer):
    registry(tmp_path)
    docker = Docker(consumer=consumer)
    monkeypatch.setattr(run_local, "command", docker)
    result = run_local.cleanup_builder_images(tmp_path)
    assert result["status"] == "PASS" and not docker.removed
    assert result["skipped"] == [{"image_id": EXTERNAL, "reason": "NOT_OWNED_EXTERNAL", "ownership": "NONE"}]


def test_owned_image_is_removed_by_exact_id_and_external_replacement_tag_is_preserved(tmp_path, monkeypatch):
    registry(tmp_path, owned=True)
    docker = Docker(owned=True)
    monkeypatch.setattr(run_local, "command", docker)
    result = run_local.cleanup_builder_images(tmp_path)
    assert result["status"] == "PASS" and docker.removed == [OWNED]
    assert EXTERNAL in docker.images


def test_owned_image_with_a_consumer_is_preserved_and_cleanup_fails(tmp_path, monkeypatch):
    registry(tmp_path, owned=True)
    docker = Docker(owned=True, consumer=True)
    monkeypatch.setattr(run_local, "command", docker)
    result = run_local.cleanup_builder_images(tmp_path)
    assert result["status"] == "FAIL" and not docker.removed
    assert any(item["image_id"] == OWNED and item["reason"] == "OWNED_IMAGE_CONSUMER" for item in result["skipped"])


def test_owned_image_reference_drift_before_removal_is_preserved_and_cleanup_fails(tmp_path, monkeypatch):
    registry(tmp_path, owned=True)
    docker = Docker(owned=True, drift=True)
    monkeypatch.setattr(run_local, "command", docker)
    result = run_local.cleanup_builder_images(tmp_path)
    assert result["status"] == "FAIL" and not docker.removed
    assert any(item["image_id"] == OWNED and item["reason"] == "OWNED_REFERENCES_CHANGED" for item in result["skipped"])


@pytest.mark.parametrize("preexisting", [False, True])
def test_real_builder_observation_chain_persists_intent_before_claiming_exact_container_image(tmp_path, monkeypatch, preexisting):
    names = tmp_path / "owned-builders.json"
    names.write_text(json.dumps([BUILDER]))
    container = {"Id": "e" * 64, "Name": "/buildx_buildkit_" + BUILDER + "0",
        "Image": OWNED, "Config": {"Image": TAG}}
    exists, events = False, []
    original_fsync = local_resources.os.fsync
    monkeypatch.setattr(local_resources.os, "fsync", lambda descriptor: events.append("fsync") or original_fsync(descriptor))
    def command(*args):
        if args[:2] == ("buildx", "ls"):
            return "foreign-builder"
        if args[0] == "ps":
            return container["Id"] if exists else ""
        if args[:2] == ("image", "ls"):
            return OWNED if preexisting else ""
        if args[0] == "inspect":
            return json.dumps([container])
        if args[:2] == ("image", "inspect"):
            return json.dumps([{"Id": OWNED, "RepoTags": [TAG], "RepoDigests": []}])
        raise AssertionError(args)
    local_resources.begin_builder_observation(names, EXECUTION, BUILDER, "d" * 40, command)
    pending = json.loads((tmp_path / "owned-builder-images.json").read_text())["builders"][0]
    assert events and pending["status"] == "CREATE_PENDING" and pending["image"] is None
    exists = True  # Simulate the one fresh builder created after the durable intent.
    local_resources.observe_builder_image(names, BUILDER, command)
    observed = json.loads((tmp_path / "owned-builder-images.json").read_text())["builders"][0]
    assert observed["container_id"] == container["Id"] and observed["image"]["Id"] == OWNED
    assert observed["status"] == ("PREEXISTING_NOT_OWNED" if preexisting else "OWNED")
    assert len(events) >= 2


@pytest.mark.parametrize("change", ["name", "image", "container_id"])
def test_builder_observation_rejects_foreign_identity_without_fabricating_ownership(tmp_path, change):
    names = tmp_path / "owned-builders.json"
    names.write_text(json.dumps([BUILDER]))
    container = {"Id": "e" * 64, "Name": "/buildx_buildkit_" + BUILDER + "0",
        "Image": OWNED, "Config": {"Image": TAG}}
    exists = False
    def command(*args):
        if args[0] == "ps":
            return "e" * 64 if exists else ""
        if args[0] == "inspect":
            return json.dumps([container])
        if args[:2] == ("image", "inspect"):
            return json.dumps([{"Id": OWNED, "RepoTags": [TAG], "RepoDigests": []}])
        return ""
    local_resources.begin_builder_observation(names, EXECUTION, BUILDER, "d" * 40, command)
    exists = True
    if change == "name":
        container["Name"] = "/foreign-builder"
    elif change == "image":
        container["Config"]["Image"] = "foreign:image"
    else:
        container["Id"] = "f" * 64
    with pytest.raises(ValueError, match="creation intent"):
        local_resources.observe_builder_image(names, BUILDER, command)
    pending = json.loads((tmp_path / "owned-builder-images.json").read_text())["builders"][0]
    assert pending["status"] == "CREATE_PENDING" and pending["image"] is None


def test_later_owned_builder_reusing_image_does_not_hide_initial_ownership(tmp_path, monkeypatch):
    registry(tmp_path, owned=True)
    path = tmp_path / "owned-builder-images.json"
    value = json.loads(path.read_text())
    later = {**value["builders"][0], "name": "tv-local-build-" + "f" * 12,
        "container_name": "buildx_buildkit_tv-local-build-" + "f" * 12 + "0",
        "status": "PREEXISTING_NOT_OWNED", "baseline_image_ids": [OWNED]}
    value["builders"].append(later)
    path.write_text(json.dumps(value))
    (tmp_path / "owned-builders.json").write_text(json.dumps([BUILDER, later["name"]]))
    docker = Docker(owned=True)
    monkeypatch.setattr(run_local, "command", docker)
    result = run_local.cleanup_builder_images(tmp_path)
    assert result["status"] == "PASS" and docker.removed == [OWNED] and EXTERNAL in docker.images


def test_remove_acknowledgement_with_image_still_present_cannot_claim_cleanup(tmp_path, monkeypatch):
    registry(tmp_path, owned=True)
    docker = Docker(owned=True, acknowledge_only=True)
    monkeypatch.setattr(run_local, "command", docker)
    previous = tmp_path / "builder-image-cleanup.json"
    previous.write_bytes(b'{"status":"PREVIOUS_PROOF"}\n')
    with pytest.raises(ValueError, match="still exists after removal"):
        run_local.cleanup_builder_images(tmp_path)
    assert docker.removed == [OWNED] and OWNED in docker.images and EXTERNAL in docker.images
    assert previous.read_bytes() == b'{"status":"PREVIOUS_PROOF"}\n'
