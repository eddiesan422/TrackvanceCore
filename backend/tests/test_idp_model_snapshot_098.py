"""Model snapshot filesystem and integrity guards using only opaque synthetic bytes."""
import hashlib
import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

SPEC = importlib.util.spec_from_file_location("idp_model_snapshot", Path(__file__).parents[2] / "deploy/idp/model_snapshot.py")
snapshot = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(snapshot)


@pytest.fixture
def fixture_models(tmp_path):
    source, destination = tmp_path / "source", tmp_path / "destination"
    source.mkdir()
    destination.mkdir()

    def write(name, raw):
        path = source / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(raw)
        return hashlib.sha256(raw).hexdigest()

    paddle = write("paddle/model.bin", b"opaque-paddle")
    layout = write("layout/weights.bin", b"opaque-layout")
    descriptors = []
    for raw in (b'{"synthetic":true}', b"opaque-quantized-weights"):
        digest = hashlib.sha256(raw).hexdigest()
        write("ollama/blobs/sha256-" + digest, raw)
        descriptors.append({"digest": "sha256:" + digest, "size": len(raw)})
    raw = json.dumps({"config": descriptors[0], "layers": [descriptors[1]]}).encode()
    manifest = write("ollama/manifests/registry.ollama.ai/library/qwen3.5/9b-q4_K_M", raw)
    (source / "ollama/manifests-v2").mkdir()
    lock = {"paddle": {"files": {"model.bin": paddle}}, "layout": {"files": {"weights.bin": layout}},
            "qwen": {"manifest_sha256": manifest}}
    return source, destination, lock


def test_complete_snapshot_preserves_all_bytes_and_empty_mountpoint(fixture_models):
    source, destination, lock = fixture_models
    original = {path.relative_to(source): path.read_bytes() for path in source.rglob("*") if path.is_file()}
    identity = {"volume_name": "synthetic-fresh", "snapshot_id": "a" * 32}
    result = snapshot.copy_snapshot(source, destination, lock, identity)
    assert result["file_count"] == 5 and result["identity"] == identity
    assert (destination / "ollama/manifests-v2").is_dir()
    assert list((destination / "ollama/manifests-v2").iterdir()) == []
    for relative, raw in original.items():
        assert (source / relative).read_bytes() == (destination / relative).read_bytes() == raw
    saved = json.loads((destination / snapshot.MANIFEST).read_text())
    claimed = saved.pop("manifest_sha256")
    assert claimed == snapshot.digest(saved)
    assert set(saved["directories"]) == snapshot.inventory(destination)[1]


@pytest.mark.parametrize("name", [None, "/etc/passwd", "../model", "a/../model", "a//model", "a\\model", "."])
def test_source_paths_cannot_escape_model_root(name):
    with pytest.raises(TypeError if name is None else ValueError, match="MODEL_SNAPSHOT_PATH"):
        snapshot.relative_parts(name)


@pytest.mark.parametrize("kind", ["file", "ancestor"])
def test_symlinks_cannot_import_files_or_directories(fixture_models, tmp_path, kind):
    source, destination, lock = fixture_models
    if kind == "file":
        (source / "paddle/model.bin").unlink()
        outside = tmp_path / "outside-model"
        outside.write_bytes(b"opaque-paddle")
        (source / "paddle/model.bin").symlink_to(outside)
    else:
        (source / "paddle").rename(tmp_path / "outside-paddle")
        (source / "paddle").symlink_to(tmp_path / "outside-paddle", target_is_directory=True)
    with pytest.raises(ValueError, match="MODEL_SNAPSHOT_SYMLINK"):
        snapshot.copy_snapshot(source, destination, lock, {})
    assert not (destination / snapshot.MANIFEST).exists()


@pytest.mark.parametrize("kind", ["file", "directory"])
def test_unlisted_source_entries_fail_closed(fixture_models, kind):
    source, destination, lock = fixture_models
    extra = source / "ollama/unlisted"
    extra.write_bytes(b"unknown") if kind == "file" else extra.mkdir()
    with pytest.raises(ValueError, match="MODEL_SNAPSHOT_SOURCE_INVENTORY"):
        snapshot.copy_snapshot(source, destination, lock, {})


def test_existing_destination_never_overwritten(fixture_models):
    source, destination, lock = fixture_models
    sentinel = destination / "existing-data"
    sentinel.write_bytes(b"retain")
    with pytest.raises(ValueError, match="MODEL_SNAPSHOT_DESTINATION_NOT_FRESH"):
        snapshot.copy_snapshot(source, destination, lock, {})
    assert sentinel.read_bytes() == b"retain"


def test_source_hash_failure_cannot_publish_complete_receipt(fixture_models):
    source, destination, lock = fixture_models
    (source / "paddle/model.bin").write_bytes(b"different-data")
    with pytest.raises(ValueError, match="MODEL_SNAPSHOT_SOURCE_INTEGRITY"):
        snapshot.copy_snapshot(source, destination, lock, {})
    assert not (destination / snapshot.MANIFEST).exists()


def test_destination_rehash_failure_cannot_publish_receipt(fixture_models, monkeypatch):
    source, destination, lock = fixture_models
    original_replace = Path.replace

    def tamper_after_publish(path, target):
        result = original_replace(path, target)
        if target == destination / "paddle/model.bin":
            target.write_bytes(b"tampered-destination")
        return result

    monkeypatch.setattr(Path, "replace", tamper_after_publish)
    with pytest.raises(ValueError, match="MODEL_SNAPSHOT_DESTINATION_INTEGRITY"):
        snapshot.copy_snapshot(source, destination, lock, {})
    assert not (destination / snapshot.MANIFEST).exists()


@pytest.mark.parametrize("limits,code", [({"max_bytes": 1}, "BYTE_LIMIT"), ({"timeout": -1}, "TIMEOUT")])
def test_copy_budgets_fail_before_completion(fixture_models, limits, code):
    source, destination, lock = fixture_models
    with pytest.raises(ValueError, match="MODEL_SNAPSHOT_" + code):
        snapshot.copy_snapshot(source, destination, lock, {}, **limits)
    assert not (destination / snapshot.MANIFEST).exists()


def test_pinned_manifest_tamper_is_rejected(fixture_models):
    source, destination, lock = fixture_models
    path = source / "ollama/manifests/registry.ollama.ai/library/qwen3.5/9b-q4_K_M"
    path.write_bytes(path.read_bytes() + b" ")
    with pytest.raises(ValueError, match="MODEL_SNAPSHOT_MANIFEST"):
        snapshot.copy_snapshot(source, destination, lock, {})


def test_fifo_leaf_cannot_block_before_type_validation(fixture_models):
    source, _, _ = fixture_models
    path = source / "ollama/manifests/registry.ollama.ai/library/qwen3.5/9b-q4_K_M"
    path.unlink()
    os.mkfifo(path)
    code = """
import importlib.util,sys
from pathlib import Path
spec=importlib.util.spec_from_file_location('snapshot_fifo_test',sys.argv[1])
module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
try:module.open_source(Path(sys.argv[2]),sys.argv[3])
except ValueError as error:
 assert str(error)=='MODEL_SNAPSHOT_SOURCE_TYPE'
else:raise AssertionError('FIFO accepted')
"""
    result = subprocess.run([sys.executable, "-c", code, str(Path(snapshot.__file__)), str(source), path.relative_to(source).as_posix()],
        capture_output=True, timeout=2, check=False)
    assert result.returncode == 0
