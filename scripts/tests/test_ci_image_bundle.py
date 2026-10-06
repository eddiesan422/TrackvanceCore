"""Image reuse fails before Docker when source identity or bytes are altered."""
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from ci import image_bundle

COMMIT = "a" * 40


@pytest.fixture
def bundle(tmp_path):
    manifest = {"schema_version": 1, "source_sha": COMMIT, "status": "PASS", "images": {},
                "digest_kind": "DOCKER_CONFIGURATION_SHA256"}
    for index, role in enumerate(("backend", "web")):
        archive = tmp_path / (role + ".tar")
        archive.write_bytes((role + " image bytes").encode())
        manifest["images"][role] = {"image_id": "sha256:" + str(index) * 64,
            "archive": archive.name, "archive_bytes": archive.stat().st_size,
            "archive_sha256": image_bundle.file_digest(archive)}
    path = tmp_path / "images.json"
    path.write_text(json.dumps(manifest), encoding="utf-8")
    return path, manifest


@pytest.mark.parametrize("damage", ["sha", "missing-role", "digest", "path", "missing", "changed", "size", "status"])
def test_invalid_bundle_cannot_mutate_docker(bundle, monkeypatch, damage):
    path, manifest = bundle
    if damage == "sha":
        manifest["source_sha"] = "b" * 40
    elif damage == "missing-role":
        del manifest["images"]["web"]
    elif damage == "digest":
        manifest["images"]["web"]["image_id"] = "latest"
    elif damage == "path":
        manifest["images"]["web"]["archive"] = "../web.tar"
    elif damage == "missing":
        (path.parent / "web.tar").unlink()
    elif damage == "changed":
        (path.parent / "web.tar").write_bytes(b"invalid archive")
    elif damage == "size":
        manifest["images"]["web"]["archive_bytes"] += 1
    else:
        manifest["status"] = "CANCELLED"
    path.write_text(json.dumps(manifest), encoding="utf-8")
    monkeypatch.setattr(image_bundle, "docker", lambda *_: pytest.fail("Invalid bundle reached Docker"))
    with pytest.raises(ValueError):
        image_bundle.load_images(path, COMMIT, path.parent / "loaded.json")


@pytest.mark.parametrize("damage", ["revision", "version", "platform", "id"])
def test_loaded_image_must_match_commit_runtime_and_digest(monkeypatch, damage):
    row = {"Id": "sha256:" + "1" * 64, "Os": "linux", "Architecture": "amd64",
        "Config": {"Labels": {"org.opencontainers.image.revision": COMMIT,
                              "org.opencontainers.image.version": "0.8.0"}}}
    if damage in {"revision", "version"}:
        row["Config"]["Labels"]["org.opencontainers.image." + damage] = "wrong"
    elif damage == "platform":
        row["Architecture"] = "arm64"
    else:
        row["Id"] = "latest"
    monkeypatch.setattr(image_bundle, "docker", lambda *_: json.dumps([row]))
    with pytest.raises(ValueError):
        image_bundle.inspect_image("test", COMMIT)


def test_valid_archives_load_exact_digests_without_rebuilds(bundle, monkeypatch):
    path, manifest = bundle
    calls = []
    monkeypatch.setattr(image_bundle, "docker", lambda *args: calls.append(args) or "")
    monkeypatch.setattr(image_bundle, "inspect_image", lambda digest, _: {"Id": digest})
    proof = image_bundle.load_images(path, COMMIT, path.parent / "loaded.json")
    assert proof["status"] == "PASS" and proof["rebuilds"] == 0
    assert [row["image_id"] for row in proof["measurements"]] == [manifest["images"][role]["image_id"] for role in ("backend", "web")]
    assert sum(call[0] == "load" for call in calls) == 2
    assert all(call[0] in {"load", "tag"} for call in calls)


@pytest.mark.parametrize('damage', ['source-sha', 'bytes', 'path'])
def test_frontend_build_proof_is_verified_before_loading_runtime_images(bundle, monkeypatch, damage):
    path, manifest = bundle
    archive = path.parent / 'frontend-build-proof.zip'
    archive.write_bytes(b'bound build proof bytes')
    proof = {'schema_version': 1, 'source_sha': COMMIT, 'build_image_id': 'sha256:' + 'c' * 64,
             'archive': archive.name, 'archive_sha256': image_bundle.file_digest(archive),
             'archive_bytes': archive.stat().st_size, 'source_prefix': 'source/', 'dist_prefix': 'dist/'}
    manifest['frontend_build_proof'] = proof
    if damage == 'source-sha':
        proof['source_sha'] = 'b' * 40
    elif damage == 'bytes':
        archive.write_bytes(b'changed proof bytes')
    else:
        proof['archive'] = '../frontend-build-proof.zip'
    path.write_text(json.dumps(manifest), encoding='utf-8')
    monkeypatch.setattr(image_bundle, 'docker', lambda *_: pytest.fail('Invalid build proof reached Docker load'))
    with pytest.raises(ValueError):
        image_bundle.load_images(path, COMMIT, path.parent / 'loaded.json')
