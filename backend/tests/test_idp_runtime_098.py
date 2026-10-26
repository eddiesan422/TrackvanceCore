"""Portable runtime proof parsing; no models, GPU, network or Docker operations."""
import gzip
import hashlib
import importlib.util
import io
import json
import subprocess
import tarfile
from pathlib import Path
from types import SimpleNamespace

import pytest

SPEC = importlib.util.spec_from_file_location("idp_runtime", Path(__file__).parents[2] / "scripts/idp_runtime.py")
runtime = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(runtime)


def test_packaging_includes_shared_source_catalog_and_both_immutable_prompts():
    assert len(runtime.SOURCES) == len(set(runtime.SOURCES)) == 17
    assert {"prompt-v1.txt", "prompt-v2.txt", "document_semantic_fragments.py", "model_snapshot.py", "idp_observability.py",
            "idp_memory_lifecycle.py", "idp_cache_advice.py", "recognition.py", "prompt-visual-v1.txt"} <= set(runtime.SOURCES)
    assert runtime.source_path("document_semantic_fragments.py") == runtime.ROOT / "backend/src/trackvance/document_semantic_fragments.py"
    assert runtime.source_path("prompt-v1.txt") == runtime.DEPLOY / "prompt-v1.txt"
    for name in ("idp_memory_lifecycle.py", "idp_cache_advice.py"):
        assert runtime.source_path(name) == runtime.DEPLOY / name
    for name in ("document_literal_tables.py", "document_template_builder_rules.py"):
        assert runtime.source_path(name) == runtime.ROOT / "backend/src/trackvance" / name


def test_model_overlay_contains_only_readonly_owned_model_mounts():
    overlay = runtime.snapshot_overlay("trackvance-idp098-models-synthetic")
    assert set(overlay["services"]) == {"idp-gateway", "idp-ollama"}
    assert overlay["volumes"]["verified-model-snapshot"] == {"external": True, "name": "trackvance-idp098-models-synthetic"}
    for service in overlay["services"].values():
        assert set(service) == {"volumes"}
        for mount in service["volumes"]:
            assert mount["type"] == "volume" and mount["read_only"] is True and mount["volume"]["nocopy"] is True
            assert mount["volume"]["subpath"] in {"paddle", "layout", "ollama"}


def test_model_snapshot_rejects_existing_volume_before_any_write(tmp_path, monkeypatch):
    monkeypatch.setattr(runtime, "host_available", lambda: 10 * 1024**3)
    monkeypatch.setattr(runtime.shutil, "disk_usage", lambda _: SimpleNamespace(free=100 * 1024**3))
    monkeypatch.setattr(runtime.uuid, "uuid4", lambda: SimpleNamespace(hex="a" * 32))
    calls = []

    def output(*arguments):
        calls.append(arguments)
        assert arguments[:2] == ("volume", "ls")
        return runtime.OWNER + "-models-" + "a" * 12

    monkeypatch.setattr(runtime, "output", output)
    with pytest.raises(ValueError, match="MODEL_SNAPSHOT_DESTINATION_EXISTS"):
        runtime.prepare_model_snapshot(tmp_path, tmp_path, "sha256:" + "a" * 64, tmp_path / "log")
    assert len(calls) == 1


@pytest.mark.parametrize("marker", ["true", "false", "", False, None])
def test_any_permanent_label_protects_even_an_otherwise_owned_resource(marker):
    labels = {"io.trackvance.owner": runtime.OWNER, "io.trackvance.environment": "idp-private"}
    assert runtime.private_labels(labels)
    assert not runtime.private_labels({**labels, "io.trackvance.permanent": marker})


def initializer_fixture(monkeypatch):
    monkeypatch.setattr(runtime.uuid, "uuid4", lambda: SimpleNamespace(hex="a" * 32))
    name = runtime.OWNER + "-init-" + "a" * 10
    helper, image = "b" * 64, "sha256:" + "c" * 64
    saved = {"Id": helper, "Name": "/" + name, "Image": image,
        "Config": {"Labels": {"io.trackvance.owner": runtime.OWNER,
            "io.trackvance.environment": "idp-private", "io.trackvance.initializer-id": name}}}
    return helper, image, saved


def test_initializer_name_collision_never_inspects_starts_or_removes_existing_container(tmp_path, monkeypatch):
    helper, image, existing = initializer_fixture(monkeypatch)
    assert runtime.runtime_initializer_owned(existing, helper, existing["Name"][1:], image)
    calls = []

    def collision(*arguments):
        calls.append(arguments)
        assert arguments[0] == "create"
        raise subprocess.CalledProcessError(1, ["docker", *arguments], stderr="name already in use")

    def forbidden(*_args, **_kwargs):
        pytest.fail("Failed creation must not inspect, start or remove any container")

    monkeypatch.setattr(runtime, "output", collision)
    monkeypatch.setattr(runtime, "run", forbidden)
    monkeypatch.setattr(runtime.subprocess, "run", forbidden)
    with pytest.raises(subprocess.CalledProcessError):
        runtime.initialize_runtime(image, "private-volume", tmp_path / "archive", tmp_path / "log")
    assert len(calls) == 1


@pytest.mark.parametrize("identity_change", [None, "Id", "Image", "Name", "permanent", "initializer"])
def test_initializer_cleanup_requires_unchanged_created_cid_and_ownership(tmp_path, monkeypatch, identity_change):
    helper, image, saved = initializer_fixture(monkeypatch)
    calls = []

    def output(*arguments):
        calls.append(arguments)
        if arguments[0] == "create":
            return helper
        assert arguments == ("inspect", helper)
        return json.dumps([saved])

    def run(arguments, _log):
        assert arguments == ["docker", "start", "--attach", helper]
        calls.append(tuple(arguments))

    def cleanup(arguments, **_kwargs):
        calls.append(tuple(arguments))
        if arguments == ["docker", "inspect", helper]:
            changed = json.loads(json.dumps(saved))
            if identity_change in {"Id", "Image", "Name"}:
                changed[identity_change] = "foreign"
            elif identity_change == "permanent":
                changed["Config"]["Labels"]["io.trackvance.permanent"] = "false"
            elif identity_change == "initializer":
                changed["Config"]["Labels"]["io.trackvance.initializer-id"] = "other"
            return SimpleNamespace(returncode=0, stdout=json.dumps([changed]))
        assert arguments == ["docker", "rm", "--force", helper]
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(runtime, "output", output)
    monkeypatch.setattr(runtime, "run", run)
    monkeypatch.setattr(runtime.subprocess, "run", cleanup)
    runtime.initialize_runtime(image, "private-volume", tmp_path / "archive", tmp_path / "log")
    assert (("docker", "rm", "--force", helper) in calls) is (identity_change is None)


def test_initializer_rejects_unverified_create_result_without_cleanup(tmp_path, monkeypatch):
    monkeypatch.setattr(runtime, "output", lambda *_args: "generated-name")
    monkeypatch.setattr(runtime.subprocess, "run", lambda *_args, **_kwargs: pytest.fail("No verified CID to clean up"))
    with pytest.raises(ValueError, match="INITIALIZER_CID_INVALID"):
        runtime.initialize_runtime("sha256:" + "c" * 64, "private-volume", tmp_path / "archive", tmp_path / "log")


@pytest.mark.parametrize("key,value", [("CreatedAt", "replaced"), ("Driver", "other"),
    ("Options", {"device": "/foreign"}), ("Labels", {}), ("Name", "other")])
def test_same_name_snapshot_replacement_cannot_publish_overlay(key, value):
    original = {"Name": "trackvance-idp098-models-test", "CreatedAt": "created-once", "Driver": "local",
        "Options": None, "Scope": "local", "Labels": {"io.trackvance.owner": runtime.OWNER,
            "io.trackvance.environment": "idp-private", "io.trackvance.snapshot-id": "a" * 32}}
    runtime.verify_snapshot_volume(original, dict(original))
    with pytest.raises(ValueError, match="MODEL_SNAPSHOT_VOLUME_REPLACED"):
        runtime.verify_snapshot_volume(original, {**original, key: value})


@pytest.mark.parametrize("key,value", [("Name", "foreign"), ("CreatedAt", ""), ("CreatedAt", "2026-10-01T00:00:00"),
    ("Options", {"device": "/foreign", "type": "none", "o": "bind"}), ("Scope", "global"), ("Driver", "foreign")])
def test_initial_snapshot_admission_rejects_device_backing_and_wrong_identity(key, value):
    labels = {"io.trackvance.owner": runtime.OWNER, "io.trackvance.environment": "idp-private"}
    volume = {"Name": "new-owned-volume", "CreatedAt": "2026-10-01T00:00:00Z", "Driver": "local",
              "Options": None, "Scope": "local", "Labels": labels}
    runtime.verify_new_snapshot_volume(volume, volume["Name"], labels)
    with pytest.raises(ValueError, match="MODEL_SNAPSHOT_VOLUME_IDENTITY"):
        runtime.verify_new_snapshot_volume({**volume, key: value}, volume["Name"], labels)


def tar_bytes(files):
    target = io.BytesIO()
    with tarfile.open(fileobj=target, mode="w") as archive:
        for name, data in files.items():
            entry = tarfile.TarInfo(name)
            entry.size = len(data)
            archive.addfile(entry, io.BytesIO(data))
    return target.getvalue()


def archive_fixture(path, *, wheel_sha=None, corrupted_config=False):
    lock = b"test-package==1.0 --hash=sha256:synthetic-only\n"
    lock_sha = hashlib.sha256(lock).hexdigest()
    layers = [gzip.compress(tar_bytes(files)) for files in (
        {"install/requirements.lock": lock}, {"usr/local/lib/python3.12/site-packages/test.py": b"dependency"},
        {"usr/local/lib/python3.12/site-packages/paddle/test.py": b"synthetic-wheel"})]
    names = ["blobs/sha256/" + hashlib.sha256(data).hexdigest() for data in layers]
    config = json.dumps({"history": [
        {"created_by": "COPY requirements.lock /install/requirements.lock # buildkit"},
        {"created_by": "RUN python -m pip install --no-cache-dir --require-hashes -r /install/requirements.lock"},
        {"created_by": "RUN echo " + (wheel_sha or runtime.WHEEL_SHA)
            + " && python -m pip install --no-cache-dir --no-deps official.whl"}]}).encode()
    config_name = "blobs/sha256/" + hashlib.sha256(config).hexdigest()
    files = dict(zip(names, layers, strict=True))
    files[config_name] = config + (b" " if corrupted_config else b"")
    files["manifest.json"] = json.dumps([{"Config": config_name, "Layers": names}]).encode()
    path.write_bytes(tar_bytes(files))
    return lock_sha, names


def test_selects_only_hash_locked_pip_layers(tmp_path):
    archive = tmp_path / "software.tar"
    lock_sha, layers = archive_fixture(archive)
    proof = runtime.software_manifest(archive, lock_sha)
    assert proof["layers"] == [value.rsplit("/", 1)[1] for value in layers[1:]]
    assert proof["requirements_sha256"] == lock_sha
    assert proof["paddle_wheel_sha256"] == runtime.WHEEL_SHA


def test_rejects_different_embedded_requirements(tmp_path):
    archive = tmp_path / "software.tar"
    archive_fixture(archive)
    with pytest.raises(ValueError, match="DEPENDENCY_PROOF_MISSING"):
        runtime.software_manifest(archive, "0" * 64)


def test_rejects_changed_config_bytes(tmp_path):
    archive = tmp_path / "software.tar"
    lock_sha, _ = archive_fixture(archive, corrupted_config=True)
    with pytest.raises(ValueError, match="CONFIG_INTEGRITY"):
        runtime.software_manifest(archive, lock_sha)


def test_rejects_unpinned_wheel_history(tmp_path):
    archive = tmp_path / "software.tar"
    lock_sha, _ = archive_fixture(archive, wheel_sha="0" * 64)
    with pytest.raises(ValueError, match="WHEEL_NOT_PINNED"):
        runtime.software_manifest(archive, lock_sha)


def test_windows_host_floor_blocks_start(monkeypatch):
    monkeypatch.setattr(runtime, "host_available", lambda: 2 * 1024**3)
    with pytest.raises(RuntimeError, match="HOST_RESERVE"):
        runtime.require_reserve()
