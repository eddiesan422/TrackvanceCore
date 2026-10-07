"""Durable image intents preserve immutable ownership through failed builds."""
from __future__ import annotations

import copy
import json
import subprocess

import pytest
from ci import local_resources as resources

EXECUTION = "local-" + "a" * 32
PROJECT = "trackvance-v070-test-legacy061-src-" + "b" * 12
SOURCE = resources.HISTORICAL_SOURCES["0.6.1"]
REFERENCE = PROJECT + ":backend"
IDENTIFIER = "sha256:" + "c" * 64


class DockerInventory:
    def __init__(self):
        self.images = {}
        self.containers = []
        self.removed = []

    def image(self, identifier, tags, labels):
        self.images[identifier] = {"Id": identifier, "RepoTags": tags, "RepoDigests": [],
            "Config": {"Labels": copy.deepcopy(labels), "Env": ["private=test-value"]},
            "RootFS": {"Type": "layers", "Layers": ["sha256:" + "d" * 64]},
            "Created": "2026-10-07T00:00:00Z", "Size": 1024}

    def __call__(self, *args):
        if args[:2] == ("image", "ls"):
            values = list(self.images)
            if len(args) == 5:
                values = [identifier for identifier in values if args[-1] in self.images[identifier]["RepoTags"]]
            return "\n".join(values)
        if args[:2] == ("image", "inspect"):
            return json.dumps([self.images[identifier] for identifier in args[2:]])
        if args[:3] == ("ps", "-aq", "--no-trunc"):
            return "\n".join(row["Id"] for row in self.containers)
        if args[0] == "inspect":
            return json.dumps(self.containers)
        assert args == ("image", "rm", args[-1]) and args[-1].startswith("sha256:")
        self.removed.append(args[-1])
        del self.images[args[-1]]
        return ""


def intent(path, docker, *, version="0.6.1", source=SOURCE):
    labels = resources.begin_image_build(path, EXECUTION, PROJECT, source, version,
        "backend", REFERENCE, docker)
    docker.image(IDENTIFIER, [REFERENCE], labels)
    return labels


@pytest.mark.parametrize("version", resources.HISTORICAL_SOURCES)
def test_partial_build_intent_recovers_exact_id_and_authentic_version(tmp_path, version):
    docker, path = DockerInventory(), tmp_path / "images.json"
    intent(path, docker, version=version, source=resources.HISTORICAL_SOURCES[version])
    pending = json.loads(path.read_text())
    assert pending["builds"][0]["status"] == "BUILD_PENDING" and pending["builds"][0]["image"] is None
    audit = resources.cleanup_registered_images(path, EXECUTION, command=docker)
    assert docker.removed == [IDENTIFIER]
    assert audit["removed"][0]["version"] == version
    assert audit["removed"][0]["source_sha"] == resources.HISTORICAL_SOURCES[version]


@pytest.mark.parametrize("loaded", [False, True])
def test_lost_owned_tag_recovered_using_intent_and_exact_id(tmp_path, loaded):
    docker, path = DockerInventory(), tmp_path / "images.json"
    intent(path, docker)
    if loaded:
        resources.observe_image_builds(path, EXECUTION, docker)
    docker.images[IDENTIFIER]["RepoTags"] = []
    assert resources.cleanup_registered_images(path, EXECUTION, command=docker)["removed"][0]["image_id"] == IDENTIFIER
    assert docker.removed == [IDENTIFIER]


@pytest.mark.parametrize("change", ["tag", "digest", "config", "label", "rootfs"])
def test_first_loaded_identity_never_adopts_new_refs_or_full_configuration(tmp_path, change):
    docker, path = DockerInventory(), tmp_path / "images.json"
    intent(path, docker)
    first = resources.observe_image_builds(path, EXECUTION, docker)["builds"][0]["image"]
    row = docker.images[IDENTIFIER]
    if change == "tag":
        row["RepoTags"].append("foreign/shared:latest")
    elif change == "digest":
        row["RepoDigests"].append("foreign/shared@sha256:" + "e" * 64)
    elif change == "config":
        row["Config"]["Env"].append("another-private=value")
    elif change == "label":
        row["Config"]["Labels"]["unselected.label"] = "changed"
    else:
        row["RootFS"]["Layers"].append("sha256:" + "f" * 64)
    audit = resources.cleanup_registered_images(path, EXECUTION, command=docker)
    record = json.loads(path.read_text())["builds"][0]
    assert record["image"] == first and record["observed_change"] != first
    assert audit["skipped"][0]["reason"] == "IMAGE_IDENTITY_OR_REFERENCES_CHANGED" and not docker.removed
    assert "private=test-value" not in path.read_text() and "another-private" not in path.read_text()


def test_identity_exception_remains_conserved_if_foreign_reference_later_disappears(tmp_path):
    docker, path = DockerInventory(), tmp_path / "images.json"
    intent(path, docker)
    resources.observe_image_builds(path, EXECUTION, docker)
    docker.images[IDENTIFIER]["RepoTags"].append("foreign/shared:latest")
    resources.observe_image_builds(path, EXECUTION, docker)
    docker.images[IDENTIFIER]["RepoTags"] = [REFERENCE]
    assert resources.cleanup_registered_images(path, EXECUTION, command=docker)["skipped"]
    assert not docker.removed


@pytest.mark.parametrize("status", ["running", "exited", "created", "paused"])
@pytest.mark.parametrize("by_ref", [False, True])
def test_every_container_consumer_protects_image_including_stopped_and_exact_reference(tmp_path, status, by_ref):
    docker, path = DockerInventory(), tmp_path / "images.json"
    intent(path, docker)
    docker.containers = [{"Id": "consumer", "Image": "sha256:" + "f" * 64 if by_ref else IDENTIFIER,
        "Config": {"Image": REFERENCE, "Labels": {"com.docker.compose.project": "bikerwash"}},
        "State": {"Status": status}}]
    audit = resources.cleanup_registered_images(path, EXECUTION, command=docker)
    assert audit["skipped"][0]["reason"] == "CONTAINER_CONSUMER" and not docker.removed
    assert audit["skipped"][0]["consumers"][0]["status"] == status


@pytest.mark.parametrize("shared", ["baseline", "additional-tag", "digest"])
def test_preexisting_id_or_unregistered_reference_is_never_removed(tmp_path, shared):
    docker, path = DockerInventory(), tmp_path / "images.json"
    if shared == "baseline":
        docker.image(IDENTIFIER, ["unrelated/preexisting:stable"], {})
    labels = intent(path, docker)
    if shared == "additional-tag":
        docker.images[IDENTIFIER]["RepoTags"].append("another/project:latest")
    elif shared == "digest":
        docker.images[IDENTIFIER]["RepoDigests"] = ["registry/foreign@sha256:" + "e" * 64]
    audit = resources.cleanup_registered_images(path, EXECUTION, command=docker)
    assert audit["skipped"] and not docker.removed and labels["org.opencontainers.image.version"] == "0.6.1"


def test_preexisting_reference_fails_before_build_intent_or_image_mutation(tmp_path):
    docker, path = DockerInventory(), tmp_path / "images.json"
    docker.image(IDENTIFIER, [REFERENCE], {})
    with pytest.raises(ValueError, match="overwrite"):
        resources.begin_image_build(path, EXECUTION, PROJECT, SOURCE, "0.6.1", "backend", REFERENCE, docker)
    assert json.loads(path.read_text())["builds"] == [] and not docker.removed


def test_reassigned_reference_preserves_both_old_exact_id_and_new_image(tmp_path):
    docker, path = DockerInventory(), tmp_path / "images.json"
    labels = intent(path, docker)
    first = resources.observe_image_builds(path, EXECUTION, docker)["builds"][0]["image"]
    docker.images[IDENTIFIER]["RepoTags"] = []
    other = "sha256:" + "f" * 64
    docker.image(other, [REFERENCE], labels)
    audit = resources.cleanup_registered_images(path, EXECUTION, command=docker)
    assert json.loads(path.read_text())["builds"][0]["image"] == first
    assert audit["skipped"][0]["reason"] == "REFERENCE_REASSIGNED" and len(docker.images) == 2


def git_fixture(tmp_path, monkeypatch):
    repository, context = tmp_path / "repository", tmp_path / "context"
    repository.mkdir()
    def git(*args):
        return subprocess.run(["git", "-C", str(repository), *args], check=True,
            capture_output=True, text=True, encoding="utf-8").stdout.strip()
    git("init", "--quiet")
    git("config", "user.name", "Fixture")
    git("config", "user.email", "fixture@trackvance.test")
    git("config", "core.autocrlf", "false")
    git("remote", "add", "origin", "https://github.com/eddiesan422/TrackvanceCore.git")
    for name, text in {"compose.yml": "services: {}\n", "backend/Dockerfile": "FROM scratch\n",
        "deploy/docker/frontend.Dockerfile": "FROM scratch\n",
        "backend/pyproject.toml": '[project]\nversion="0.6.1"\n'}.items():
        target = repository / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")
    git("add", ".")
    git("commit", "--quiet", "-m", "Authentic source fixture")
    source = git("rev-parse", "HEAD")
    import shutil
    shutil.copytree(repository, context, ignore=shutil.ignore_patterns(".git"))
    monkeypatch.setitem(resources.HISTORICAL_SOURCES, "0.6.1", source)
    return repository, context, source


def test_historical_proof_binds_full_git_tree_and_genuine_version_before_build(tmp_path, monkeypatch):
    repository, context, source = git_fixture(tmp_path, monkeypatch)
    proof = resources.historical_source_proof(repository, context, source, "0.6.1")
    assert proof["repository"] == "eddiesan422/TrackvanceCore" and proof["tracked_files"] == 4
    assert len(proof["build_definition_blobs"]) == 3 and len(proof["tree_sha"]) == 40
    assert len(proof["tracked_blobs_sha256"]) == 64


@pytest.mark.parametrize("change", ["blob", "missing", "extra", "repository", "version"])
def test_historical_proof_rejects_changed_source_roots_and_uncommitted_inputs(tmp_path, monkeypatch, change):
    repository, context, source = git_fixture(tmp_path, monkeypatch)
    if change == "blob":
        (context / "backend/Dockerfile").write_text("FROM different\n")
    elif change == "missing":
        (context / "compose.yml").unlink()
    elif change == "extra":
        (context / "unknown-secret.env").write_text("must-not-be-built")
    elif change == "repository":
        subprocess.run(["git", "-C", str(repository), "remote", "set-url", "origin", "https://github.com/foreign/project.git"], check=True)
    else:
        source = "a" * 40
    with pytest.raises(ValueError):
        resources.historical_source_proof(repository, context, source, "0.6.1")
