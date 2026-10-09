"""Image reuse fails before Docker when source identity or bytes are altered."""
import io
import json
import shutil
import sys
import tarfile
from copy import deepcopy
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from ci import image_bundle
from ci.image_content import archive_identity
from image_transport_fixtures import image_archive, write_members

COMMIT = "a" * 40


@pytest.fixture
def bundle(tmp_path):
    manifest = {"schema_version": 1, "source_sha": COMMIT, "status": "PASS", "images": {},
                "digest_kind": "DOCKER_CONFIGURATION_SHA256"}
    for role in ("backend", "web"):
        archive = tmp_path / (role + ".tar")
        content = image_archive(archive, COMMIT, role)
        manifest["images"][role] = {"image_id": content["engine_id"],
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
                              "org.opencontainers.image.version": "0.8.5"}}}
    if damage in {"revision", "version"}:
        row["Config"]["Labels"]["org.opencontainers.image." + damage] = "wrong"
    elif damage == "platform":
        row["Architecture"] = "arm64"
    else:
        row["Id"] = "latest"
    monkeypatch.setattr(image_bundle, "docker", lambda *_: json.dumps([row]))
    with pytest.raises(ValueError):
        image_bundle.inspect_image("test", COMMIT)


def fake_transport(bundle, monkeypatch, *, host_kind="configuration", changed_config=False,
                   host_compressed=False):
    path, _manifest = bundle
    calls, hosts = [], {}
    folder = path.parent / "synthetic-host"
    folder.mkdir()
    for role in ("backend", "web"):
        saved = folder / (role + ".tar")
        change = None
        if changed_config:
            original = image_archive(saved, COMMIT, role)
            change = {"config": {**original["config"]["config"], "Env": ["changed-with-same-labels"]}}
        hosts[role] = {**image_archive(saved, COMMIT, role, kind=host_kind,
                                      compressed=host_compressed, config_change=change), "archive": saved}

    def docker(*arguments):
        calls.append(arguments)
        if arguments[0] == "load":
            role = Path(arguments[-1]).stem
            return "Loaded image ID: " + hosts[role]["engine_id"] + "\n"
        if arguments[0] == "save":
            row = next(host for host in hosts.values() if arguments[-1] in {host["engine_id"], host["tag"]})
            shutil.copyfile(row["archive"], arguments[arguments.index("--output") + 1])
            return ""
        assert arguments[:2] == ("image", "inspect")
        host = next(row for row in hosts.values() if arguments[-1] in {row["engine_id"], row["tag"]})
        row = {"Id": host["engine_id"], "Os": "linux", "Architecture": "amd64", "Size": 1000,
               "Config": host["config"]["config"], "RootFS": {"Type": "layers", "Layers": host["rootfs_diff_ids"]}}
        if host_kind != "configuration":
            row["Descriptor"] = {"digest": host["engine_id"]}
        return json.dumps([row])

    monkeypatch.setattr(image_bundle, "docker", docker)
    monkeypatch.setattr(image_bundle, "save_image", lambda image, archive: image_bundle.docker(
        "save", "--output", str(archive), image))
    return calls, hosts


def test_valid_archives_load_exact_digests_without_rebuilds(bundle, monkeypatch):
    path, manifest = bundle
    calls, _hosts = fake_transport(bundle, monkeypatch)
    proof = image_bundle.load_images(path, COMMIT, path.parent / "loaded.json")
    assert proof["status"] == "PASS" and proof["rebuilds"] == 0
    assert [row["image_id"] for row in proof["measurements"]] == [manifest["images"][role]["image_id"] for role in ("backend", "web")]
    assert sum(call[0] == "load" for call in calls) == 2
    assert all(call[0] in {"load", "save", "image"} for call in calls)
    assert sum(call[0] == "save" for call in calls) == 2
    assert image_bundle.validated_host_mapping(path, COMMIT, path.parent / "host-images.json") == {
        role: descriptor["image_id"] for role, descriptor in manifest["images"].items()}


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


@pytest.mark.parametrize("kind", ["manifest", "index"])
@pytest.mark.parametrize("compressed", [False, True])
def test_classic_configuration_id_loads_into_distinct_host_tree_without_rewriting_ci(
        bundle, monkeypatch, kind, compressed):
    path, original = bundle
    before = {p.name: p.read_bytes() for p in (path, path.parent / "backend.tar", path.parent / "web.tar")}
    calls, hosts = fake_transport(bundle, monkeypatch, host_kind=kind, host_compressed=compressed)
    receipt = image_bundle.load_images(path, COMMIT, path.parent / "loaded.json")
    assert all(hosts[role]["engine_id"] != image["image_id"]
               for role, image in original["images"].items())
    assert receipt["manifest_sha256"] == image_bundle.file_digest(path)
    assert {p.name: p.read_bytes() for p in (path, path.parent / "backend.tar", path.parent / "web.tar")} == before
    assert receipt["rebuilds"] == 0
    assert not any(call[0] in {"tag", "build", "run", "create"} for call in calls)
    mapping_path = Path(receipt["host_mapping"])
    assert receipt["host_mapping_sha256"] == image_bundle.file_digest(mapping_path)
    assert image_bundle.validated_host_mapping(path, COMMIT, mapping_path) == {
        role: hosts[role]["engine_id"] for role in hosts}
    import ci_images
    assert ci_images.verified_images({"TRACKVANCE_CI_IMAGE_MANIFEST": str(path), "CI_SOURCE_SHA": COMMIT}) == {
        role: hosts[role]["engine_id"] for role in hosts}


def test_matching_labels_and_diff_ids_cannot_hide_changed_configuration_bytes(bundle, monkeypatch):
    path, _manifest = bundle
    calls, _hosts = fake_transport(bundle, monkeypatch, host_kind="index", changed_config=True)
    with pytest.raises(ValueError, match="certified configuration"):
        image_bundle.load_images(path, COMMIT, path.parent / "loaded.json")
    assert sum(call[0] == "load" for call in calls) == 1
    assert not (path.parent / "host-images.json").exists()
    assert not (path.parent / "loaded.json").exists()
    assert not list(path.parent.glob("trackvance-image-proof-*"))


@pytest.mark.parametrize("kind", ["configuration", "manifest", "index"])
def test_export_declares_engine_and_content_identities_accurately(bundle, monkeypatch, kind):
    path, _manifest = bundle
    _calls, hosts = fake_transport(bundle, monkeypatch, host_kind=kind)
    directory = path.parent / "exported"
    exported = image_bundle.export_images(directory, COMMIT, hosts["backend"]["tag"], hosts["web"]["tag"])
    assert exported["digest_kind"] == "DOCKER_ENGINE_IMAGE_ID"
    expected = "CONFIGURATION_SHA256" if kind == "configuration" else "OCI_TARGET_SHA256"
    for role, content in hosts.items():
        descriptor = exported["images"][role]
        assert descriptor["image_id_kind"] == expected
        assert descriptor["image_id"] == content["engine_id"]
        assert descriptor["configuration_digest"] == content["configuration_digest"]
        assert descriptor["rootfs_diff_ids"] == content["rootfs_diff_ids"]


@pytest.mark.parametrize("damage", ["layer", "duplicate", "symlink", "escape", "false-config-kind", "invalid-tar"])
def test_content_damage_is_rejected_before_any_docker_operation(bundle, monkeypatch, damage):
    path, manifest = bundle
    archive = path.parent / "web.tar"
    source = image_archive(archive, COMMIT, "web", kind="index" if damage == "false-config-kind" else "configuration")
    if damage == "layer":
        source["members"][source["layer_name"]] = b"changed layer"
        write_members(archive, source["members"])
    elif damage in {"duplicate", "symlink", "escape"}:
        with tarfile.open(archive, "a") as saved:
            item = tarfile.TarInfo("manifest.json" if damage == "duplicate" else "../outside" if damage == "escape" else "linked")
            if damage == "symlink":
                item.type, item.linkname = tarfile.SYMTYPE, "outside"
            saved.addfile(item, io.BytesIO())
    elif damage == "invalid-tar":
        archive.write_bytes(b"not a tar")
    manifest["images"]["web"].update(image_id=source["engine_id"], archive_bytes=archive.stat().st_size,
                                    archive_sha256=image_bundle.file_digest(archive))
    path.write_text(json.dumps(manifest))
    monkeypatch.setattr(image_bundle, "docker", lambda *_: pytest.fail("Bad content reached Docker"))
    with pytest.raises(ValueError):
        image_bundle.load_images(path, COMMIT, path.parent / "loaded.json")
    assert not (path.parent / "loaded.json").exists()


@pytest.mark.parametrize("damage", ["source-sha", "manifest-bytes", "source-id", "archive-hash", "config-proof", "host-tree", "host-layer", "host-id"])
def test_host_mapping_cannot_be_forged_or_detached_from_original_ci(bundle, monkeypatch, damage):
    path, _manifest = bundle
    _calls, hosts = fake_transport(bundle, monkeypatch, host_kind="index")
    image_bundle.load_images(path, COMMIT, path.parent / "loaded.json")
    mapping_path = path.parent / "host-images.json"
    mapping = json.loads(mapping_path.read_text())
    image = mapping["images"]["backend"]
    if damage == "source-sha":
        mapping["source_sha"] = "b" * 40
    elif damage == "manifest-bytes":
        path.write_text(path.read_text() + " ")
    elif damage == "source-id":
        image["source_image_id"] = hosts["backend"]["engine_id"]
    elif damage == "archive-hash":
        image["archive_sha256"] = "b" * 64
    elif damage == "config-proof":
        image["host_identity"]["chain"][-1]["payload_base64"] = "e30="
    elif damage == "host-tree":
        image["host_identity"]["chain"][0]["payload_base64"] = "e30="
    elif damage == "host-layer":
        image["host_identity"]["layers"][0]["digest"] = "sha256:" + "b" * 64
    else:
        image["host_image_id"] = "sha256:" + "f" * 64
    mapping_path.write_text(json.dumps(mapping))
    with pytest.raises(ValueError):
        image_bundle.validated_host_mapping(path, COMMIT, mapping_path)


@pytest.mark.parametrize("target", ["images.json", "backend.tar", "web.tar", "frontend-build-proof.zip", "existing.json"])
def test_receipts_never_replace_original_or_existing_evidence(bundle, monkeypatch, target):
    path, _manifest = bundle
    output = path.parent / target
    if not output.exists():
        output.write_bytes(b"retained original")
    before = output.read_bytes()
    monkeypatch.setattr(image_bundle, "docker", lambda *_: pytest.fail("Unsafe receipt reached Docker"))
    with pytest.raises(ValueError, match="fresh and separate"):
        image_bundle.load_images(path, COMMIT, output)
    assert output.read_bytes() == before


def test_new_manifest_requires_mapping_and_legacy_cannot_use_a_target_id_as_config(bundle, monkeypatch):
    import ci_images
    path, manifest = bundle
    original_manifest = deepcopy(manifest)
    _calls, hosts = fake_transport(bundle, monkeypatch, host_kind="index")
    values = {"TRACKVANCE_CI_IMAGE_MANIFEST": str(path), "CI_SOURCE_SHA": COMMIT}
    for role, descriptor in manifest["images"].items():
        descriptor["image_id"] = hosts[role]["engine_id"]
    path.write_text(json.dumps(manifest))
    with pytest.raises(ValueError, match="Cross-store"):
        ci_images.verified_images(values)
    manifest = original_manifest
    manifest["digest_kind"] = "DOCKER_ENGINE_IMAGE_ID"
    for role, descriptor in manifest["images"].items():
        content = archive_identity(path.parent / descriptor["archive"], descriptor["image_id"], COMMIT)
        descriptor.update({key: content[key] for key in ("image_id_kind", "configuration_digest", "rootfs_diff_ids")})
    path.write_text(json.dumps(manifest))
    with pytest.raises(ValueError, match="require a verified host"):
        ci_images.verified_images(values)


def test_archive_drift_during_loading_never_produces_a_pass_receipt(bundle, monkeypatch):
    path, _manifest = bundle
    calls, _hosts = fake_transport(bundle, monkeypatch)
    original = image_bundle.docker

    def docker(*arguments):
        result = original(*arguments)
        if arguments[0] == "save" and sum(call[0] == "save" for call in calls) == 2:
            (path.parent / "web.tar").write_bytes(b"changed during transfer")
        return result

    monkeypatch.setattr(image_bundle, "docker", docker)
    with pytest.raises(ValueError, match="Missing or altered image archive"):
        image_bundle.load_images(path, COMMIT, path.parent / "loaded.json")
    assert not (path.parent / "loaded.json").exists()
    assert not (path.parent / "host-images.json").exists()


@pytest.mark.parametrize("source_kind", ["manifest", "index"])
@pytest.mark.parametrize("host_kind", ["configuration", "manifest", "index"])
def test_containerd_source_tree_transfers_to_classic_or_target_without_config_identity_lies(
        bundle, monkeypatch, source_kind, host_kind):
    path, manifest = bundle
    manifest["digest_kind"] = "DOCKER_ENGINE_IMAGE_ID"
    for role, descriptor in manifest["images"].items():
        archive = path.parent / descriptor["archive"]
        data = image_archive(archive, COMMIT, role, kind=source_kind, compressed=True)
        content = archive_identity(archive, data["engine_id"], COMMIT)
        descriptor.update(image_id=data["engine_id"], archive_bytes=archive.stat().st_size,
                          archive_sha256=image_bundle.file_digest(archive))
        descriptor.update({key: content[key] for key in ("image_id_kind", "configuration_digest", "rootfs_diff_ids")})
    path.write_text(json.dumps(manifest))
    _calls, hosts = fake_transport(bundle, monkeypatch, host_kind=host_kind)
    before = path.read_bytes()
    image_bundle.load_images(path, COMMIT, path.parent / "loaded.json")
    assert path.read_bytes() == before
    assert image_bundle.validated_host_mapping(path, COMMIT, path.parent / "host-images.json") == {
        role: hosts[role]["engine_id"] for role in hosts}


@pytest.mark.parametrize("damage", ["over-limit", "exit-code", "exception", "existing"])
def test_verification_export_is_bounded_fresh_and_reaps_only_its_child(tmp_path, monkeypatch, damage):
    archive = tmp_path / "host.tar"
    monkeypatch.setattr(image_bundle, "ARCHIVE_LIMIT", 4)
    process = type("Child", (), {})()
    process.stdout = io.BytesIO(b"oversized" if damage == "over-limit" else b"data")
    process.killed, process.waits, process.exit = False, 0, None

    def wait(**_kwargs):
        process.waits += 1
        process.exit = 1 if damage == "exit-code" or process.killed else 0
        return process.exit

    def kill():
        process.killed = True

    process.wait, process.kill, process.poll = wait, kill, lambda: process.exit
    monkeypatch.setattr(image_bundle.subprocess, "Popen", lambda *_a, **_kw: process)
    if damage == "exception":
        monkeypatch.setattr(process.stdout, "read", lambda *_: (_ for _ in ()).throw(OSError("stream failed")))
    if damage == "existing":
        archive.write_bytes(b"original")
        monkeypatch.setattr(image_bundle.subprocess, "Popen", lambda *_a, **_kw: pytest.fail("Existing evidence reached export"))
        with pytest.raises(FileExistsError):
            image_bundle.save_image("sha256:" + "a" * 64, archive)
        assert archive.read_bytes() == b"original"
        return
    with pytest.raises((ValueError, OSError)):
        image_bundle.save_image("sha256:" + "a" * 64, archive)
    assert process.stdout.closed and process.waits >= 1
    assert process.killed is (damage != "exit-code")
    assert archive.stat().st_size <= 4


def test_export_refuses_existing_ci_evidence_before_any_engine_operation(bundle, monkeypatch):
    path, _manifest = bundle
    before = path.read_bytes()
    monkeypatch.setattr(image_bundle, "docker", lambda *_: pytest.fail("Existing bundle reached Docker"))
    with pytest.raises(ValueError, match="cannot replace original"):
        image_bundle.export_images(path.parent, COMMIT, "backend", "web")
    assert path.read_bytes() == before


@pytest.mark.parametrize("damage", [None, "manifest-hash", "mapping-hash", "measurement", "duplicate-role"])
def test_suite_resources_use_verified_host_mapping_and_preserve_source_hash(bundle, monkeypatch, damage):
    from ci.run_suite import ci_transfer_metadata

    path, _manifest = bundle
    _calls, hosts = fake_transport(bundle, monkeypatch, host_kind="index")
    proof_path = path.parent / "loaded.json"
    image_bundle.load_images(path, COMMIT, proof_path)
    receipt = json.loads(proof_path.read_text())
    if damage == "manifest-hash":
        receipt["manifest_sha256"] = "b" * 64
    elif damage == "mapping-hash":
        receipt["host_mapping_sha256"] = "b" * 64
    elif damage == "measurement":
        receipt["measurements"][0]["image_id"] = "sha256:" + "b" * 64
    elif damage == "duplicate-role":
        receipt["measurements"][1] = receipt["measurements"][0]
    proof_path.write_text(json.dumps(receipt))
    if damage:
        with pytest.raises(ValueError):
            ci_transfer_metadata(proof_path, path, COMMIT)
    else:
        resources, mapping_path = ci_transfer_metadata(proof_path, path, COMMIT)
        assert resources["runtime_images"] == {role: host["engine_id"] for role, host in hosts.items()}
        assert resources["image_bundle_sha256"] == image_bundle.file_digest(path)
        assert resources["host_image_mapping_sha256"] == image_bundle.file_digest(mapping_path)
