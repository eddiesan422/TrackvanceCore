"""Portable runtime proof parsing; no models, GPU, network or Docker operations."""
import gzip
import hashlib
import importlib.util
import io
import json
import tarfile
from pathlib import Path

import pytest

SPEC = importlib.util.spec_from_file_location("idp_runtime", Path(__file__).parents[2] / "scripts/idp_runtime.py")
runtime = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(runtime)


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
