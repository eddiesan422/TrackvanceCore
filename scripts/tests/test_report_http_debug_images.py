"""Diagnostic bases and image ownership fail closed before unrelated mutations."""
from __future__ import annotations

import gzip
import hashlib
import io
import json
import shutil
import sys
import tarfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))
import report_http_debug_images as debug
from ci import local_resources as resources

SHA = "a" * 40
PROJECT = "trackvance-v080-test-reports-http-" + "b" * 12


def archive_fixture(path, *, compressed=False, damage=None, engine_kind="legacy"):
    layer = io.BytesIO()
    with tarfile.open(fileobj=layer, mode="w") as stream:
        payload = b"real opaque layer file\n"
        entry = tarfile.TarInfo("app/value.txt")
        entry.size = len(payload)
        stream.addfile(entry, io.BytesIO(payload))
    layer_bytes = layer.getvalue()
    diff_id = "sha256:" + hashlib.sha256(layer_bytes).hexdigest()
    config = {"os": "linux", "architecture": "amd64", "config": {"Labels": {
        "org.opencontainers.image.revision": SHA, "org.opencontainers.image.version": "0.8.5"}},
        "rootfs": {"type": "layers", "diff_ids": [diff_id]}}
    if damage == "revision":
        config["config"]["Labels"]["org.opencontainers.image.revision"] = "f" * 40
    if damage == "platform":
        config["architecture"] = "arm64"
    config_bytes = json.dumps(config).encode()
    config_digest = "sha256:" + hashlib.sha256(config_bytes).hexdigest()
    identifier = config_digest
    manifests = [{"Config": "config.json", "Layers": ["layer/layer.tar"], "RepoTags": None}]
    archived_layer = gzip.compress(layer_bytes, mtime=1) if compressed else layer_bytes
    values = {"config.json": config_bytes, "layer/layer.tar": archived_layer}
    if engine_kind != "legacy":
        raw_digest = "sha256:" + hashlib.sha256(archived_layer).hexdigest()
        source_manifest = {"schemaVersion": 2, "mediaType": debug.MANIFEST_MEDIA,
            "config": {"mediaType": debug.CONFIG_MEDIA, "digest": config_digest, "size": len(config_bytes)},
            "layers": [{"mediaType": debug.LAYER_MEDIA + ("+gzip" if compressed else ""),
                        "digest": raw_digest, "size": len(archived_layer)}]}
        if damage == "target-config":
            source_manifest["config"]["digest"] = "sha256:" + "f" * 64
        source_payload = debug._json_bytes(source_manifest)
        identifier = "sha256:" + hashlib.sha256(source_payload).hexdigest()
        values = {"blobs/sha256/" + config_digest[7:]: config_bytes,
                  "blobs/sha256/" + raw_digest[7:]: archived_layer,
                  "blobs/sha256/" + identifier[7:]: source_payload}
        manifests[0].update(Config="blobs/sha256/" + config_digest[7:], Layers=["blobs/sha256/" + raw_digest[7:]])
        if engine_kind in {"index", "nested-index"}:
            node = {"mediaType": debug.MANIFEST_MEDIA, "digest": identifier, "size": len(source_payload),
                    "platform": {"os": "linux", "architecture": "amd64"}}
            # Real containerd exports may retain descriptors for unexported
            # platforms; none may replace the selected amd64 config.
            for _ in range(2 if engine_kind == "nested-index" else 1):
                index = {"schemaVersion": 2, "mediaType": debug.INDEX_MEDIA,
                    "manifests": [node, {"mediaType": debug.MANIFEST_MEDIA, "digest": "sha256:" + "e" * 64,
                        "size": 999, "platform": {"os": "linux", "architecture": "arm64"}}]}
                payload = debug._json_bytes(index)
                identifier = "sha256:" + hashlib.sha256(payload).hexdigest()
                values["blobs/sha256/" + identifier[7:]] = payload
                node = {"mediaType": debug.INDEX_MEDIA, "digest": identifier, "size": len(payload)}
        if damage == "target-bytes":
            values["blobs/sha256/" + identifier[7:]] += b" "
        if damage == "raw-layer-bytes":
            altered = bytearray(archived_layer)
            altered[4] ^= 1  # gzip mtime only: decoded filesystem is identical.
            values["blobs/sha256/" + raw_digest[7:]] = bytes(altered)
    if damage == "image-count":
        manifests *= 2
    if damage == "missing-layer":
        manifests[0]["Layers"] = ["missing/layer.tar"]
    if damage == "config-bytes":
        values[manifests[0]["Config"]] += b" "
    if damage == "layer-bytes":
        values[manifests[0]["Layers"][0]] += b"corrupted"
    with tarfile.open(path, "w") as saved:
        values["manifest.json"] = json.dumps(manifests).encode()
        if damage == "path":
            values["../outside"] = b"forbidden"
        for name, payload in values.items():
            entry = tarfile.TarInfo(name)
            entry.size = len(payload)
            saved.addfile(entry, io.BytesIO(payload))
        if damage == "duplicate":
            entry = tarfile.TarInfo("manifest.json")
            saved.addfile(entry, io.BytesIO())
        if damage == "link":
            entry = tarfile.TarInfo("linked-layer")
            entry.type, entry.linkname = tarfile.SYMTYPE, "layer/layer.tar"
            saved.addfile(entry)
    return identifier, diff_id, layer_bytes


@pytest.mark.parametrize("compressed", [False, True])
def test_real_tar_conversion_preserves_config_id_and_ordered_layer_bytes(tmp_path, compressed):
    archive, layout = tmp_path / "base.tar", tmp_path / "base-oci"
    identifier, diff_id, payload = archive_fixture(archive, compressed=compressed)
    descriptor = debug.archive_to_layout(archive, layout, identifier, SHA)
    debug.verify_layout(layout, descriptor, identifier)
    manifest = json.loads((layout / "blobs/sha256" / descriptor["manifest_digest"][7:]).read_bytes())
    assert manifest["config"]["digest"] == identifier
    assert manifest["layers"] == [{"mediaType": debug.LAYER_MEDIA, "digest": diff_id, "size": len(payload)}]
    assert (layout / "blobs/sha256" / diff_id[7:]).read_bytes() == payload
    assert json.loads((layout / "oci-layout").read_bytes()) == {"imageLayoutVersion": "1.0.0"}
    assert not (tmp_path / "app").exists()


@pytest.mark.parametrize("engine_kind", ["manifest", "index", "nested-index"])
@pytest.mark.parametrize("compressed", [False, True])
def test_containerd_engine_target_binds_exact_manifest_config_and_ordered_layers(tmp_path, engine_kind, compressed):
    archive, layout = tmp_path / "containerd.tar", tmp_path / "containerd-oci"
    identifier, diff_id, payload = archive_fixture(archive, compressed=compressed, engine_kind=engine_kind)
    descriptor = debug.archive_to_layout(archive, layout, identifier, SHA)
    debug.verify_layout(layout, descriptor, identifier)
    assert descriptor["engine_digest"] == identifier != descriptor["config_digest"]
    assert descriptor["engine_digest_kind"] == "OCI_TARGET_SHA256"
    assert descriptor["source_chain"][0]["digest"] == identifier
    assert descriptor["source_chain"][-1]["digest"] == descriptor["config_digest"]
    assert len(descriptor["source_chain"]) == {"manifest": 2, "index": 3, "nested-index": 4}[engine_kind]
    assert descriptor["layers"] == [{"mediaType": debug.LAYER_MEDIA, "digest": diff_id, "size": len(payload)}]


@pytest.mark.parametrize("engine_kind", ["manifest", "index"])
@pytest.mark.parametrize("damage", ["target-config", "target-bytes", "config-bytes", "raw-layer-bytes"])
def test_containerd_target_chain_and_original_compressed_layer_hash_cannot_be_substituted(tmp_path, engine_kind, damage):
    archive, layout = tmp_path / "containerd.tar", tmp_path / "containerd-oci"
    identifier, _, _ = archive_fixture(archive, compressed=True, engine_kind=engine_kind, damage=damage)
    with pytest.raises(ValueError):
        debug.archive_to_layout(archive, layout, identifier, SHA)
    assert not (layout / "index.json").exists()


def test_containerd_provenance_blob_drift_is_rejected_before_build(tmp_path):
    archive, layout = tmp_path / "containerd.tar", tmp_path / "containerd-oci"
    identifier, _, _ = archive_fixture(archive, compressed=True, engine_kind="index")
    descriptor = debug.archive_to_layout(archive, layout, identifier, SHA)
    target = layout / "blobs/sha256" / identifier[7:]
    target.write_bytes(b" " + target.read_bytes()[1:])
    with pytest.raises(ValueError, match="blob bytes changed"):
        debug.verify_layout(layout, descriptor, identifier)


@pytest.mark.parametrize("damage", ["revision", "platform", "config-bytes", "layer-bytes", "missing-layer",
                                    "image-count", "path", "duplicate", "link"])
def test_invalid_tar_cannot_expose_a_substituted_oci_base(tmp_path, damage):
    archive, layout = tmp_path / "base.tar", tmp_path / "base-oci"
    identifier, _, _ = archive_fixture(archive, damage=damage)
    with pytest.raises(ValueError):
        debug.archive_to_layout(archive, layout, identifier, SHA)
    assert not (layout / "index.json").exists() and not (tmp_path / "outside").exists()


def test_copy_budget_and_post_conversion_blob_drift_are_rejected(tmp_path):
    output = io.BytesIO()
    with pytest.raises(ValueError, match="finite disk budget"):
        debug._copy_bounded(io.BytesIO(b"12345"), output, 4)
    assert output.getvalue() == b""
    archive, layout = tmp_path / "base.tar", tmp_path / "base-oci"
    identifier, diff_id, _ = archive_fixture(archive)
    descriptor = debug.archive_to_layout(archive, layout, identifier, SHA)
    blob = layout / "blobs/sha256" / diff_id[7:]
    blob.write_bytes(b"corrupt" + blob.read_bytes()[7:])
    with pytest.raises(ValueError, match="blob bytes changed"):
        debug.verify_layout(layout, descriptor, identifier)


@pytest.fixture
def prepared(tmp_path, monkeypatch):
    inputs = tmp_path / "inputs"
    inputs.mkdir()
    images = {role: archive_fixture(inputs / (role + ".tar"))[0] for role in ("backend", "web")}
    monkeypatch.setattr(debug, "verified_images", lambda _: images)
    environment = {"TRACKVANCE_SOURCE_SHA": SHA, "TRACKVANCE_LOCAL_MAX_CPUS": "1.25",
                   "TRACKVANCE_LOCAL_MAX_MEMORY_BYTES": str(2 * debug.GIB)}
    context = {"project": PROJECT, "image": images["backend"]}
    proof = debug.prepare(tmp_path, context, environment=environment)
    return tmp_path, inputs, images, environment, context, proof


@pytest.mark.parametrize("damage", ["source", "context", "dockerfile", "private-context", "cpu", "reference", "builder", "registry"])
def test_changed_build_inputs_fail_before_export_or_builder_creation(prepared, monkeypatch, damage):
    directory, _, _, environment, context, _ = prepared
    if damage == "source":
        environment["TRACKVANCE_SOURCE_SHA"] = "f" * 40
    elif damage == "context":
        context["image"] = "sha256:" + "f" * 64
    elif damage == "dockerfile":
        with (directory / "backend.Dockerfile").open("a") as stream:
            stream.write("RUN unexpected\n")
    elif damage == "private-context":
        (directory / "http-debug-build-context" / "test.env").write_text("private=must-not-upload")
    elif damage == "cpu":
        environment["TRACKVANCE_LOCAL_MAX_CPUS"] = "nan"
    else:
        proof_path = directory / debug.PROOF_NAME
        proof = json.loads(proof_path.read_text())
        if damage == "reference":
            proof["references"]["backend"] = "trackvance-usual:backend"
        elif damage == "builder":
            proof["builder"] = "default"
        else:
            proof["registry"] = str(directory / "foreign.json")
        proof_path.write_text(json.dumps(proof))
    monkeypatch.setattr(debug, "execute", lambda *_args, **_kwargs: pytest.fail("Changed input mutated Docker"))
    monkeypatch.setattr(debug, "initialize_image_registry", lambda *_: pytest.fail("Changed input reached inventory"))
    with pytest.raises(ValueError):
        debug.build(directory, context, environment=environment)


def diagnostic_build_fixture(prepared, monkeypatch, *, fail=False):
    directory, inputs, bases, environment, context, proof = prepared
    images, events = {}, []

    def inventory(*arguments):
        if arguments[0] == "buildx":
            return "default"
        if arguments[:2] == ("image", "ls"):
            return "\n".join(identifier for identifier, row in images.items()
                if len(arguments) != 5 or arguments[-1] in row["RepoTags"])
        if arguments[:2] == ("image", "inspect"):
            return json.dumps([images[identifier] for identifier in arguments[2:]])
        if arguments[:2] == ("ps", "-aq"):
            return ""
        pytest.fail("Unexpected inventory operation: " + repr(arguments))

    monkeypatch.setattr(debug, "docker_output", inventory)
    monkeypatch.setattr(debug, "initialize_image_registry", lambda path, execution:
        resources.initialize_image_registry(path, execution, inventory))
    monkeypatch.setattr(debug, "begin_image_build", lambda *args:
        resources.begin_image_build(*args, command=inventory))
    monkeypatch.setattr(debug, "observe_image_builds", lambda path, execution:
        resources.observe_image_builds(path, execution, inventory))
    monkeypatch.setattr(debug, "inspect_image", lambda reference, _sha:
        {"Id": reference if reference in bases.values() else next(identifier for identifier, row in images.items()
            if reference in row["RepoTags"]), "Size": 100000})

    def execute(command, output, phase, deadline, **kwargs):
        events.append((command, phase, kwargs))
        if "--export" in command:
            role = phase.rsplit("-", 1)[-1]
            shutil.copyfile(inputs / (role + ".tar"), command[-1])
        elif command[:3] == ["docker", "buildx", "create"]:
            assert proof["builder"] in json.loads(Path(proof["builder_registry"]).read_text())
        elif command[:3] == ["docker", "buildx", "build"]:
            pending = json.loads(Path(proof["registry"]).read_text())["builds"][-1]
            assert pending["status"] == "BUILD_PENDING" and pending["image"] is None
            identifier = "sha256:" + ("c" if pending["role"] == "backend" else "d") * 64
            images[identifier] = {"Id": identifier, "RepoTags": [pending["reference"]], "RepoDigests": [],
                "Config": {"Labels": pending["expected_labels"]}, "RootFS": {}, "Size": 100,
                "Created": "2026-10-09T00:00:00Z"}
            if fail:
                raise RuntimeError("Partial diagnostic load")
        return {"status": "PASS"}

    monkeypatch.setattr(debug, "execute", execute)
    return directory, environment, context, proof, events


def test_owned_build_uses_pinned_oci_empty_context_and_finite_private_builder(prepared, monkeypatch):
    directory, environment, context, proof, events = diagnostic_build_fixture(prepared, monkeypatch)
    result = debug.build(directory, context, environment=environment)
    assert result["status"] == "PASS" and set(result["images"]) == {"backend", "web"}
    creates = [command for command, _, _ in events if command[:3] == ["docker", "buildx", "create"]]
    assert len(creates) == 1
    assert "memory=" + str(2 * debug.GIB) in creates[0] and "cpu-quota=125000" in creates[0]
    builds = [command for command, _, _ in events if command[:3] == ["docker", "buildx", "build"]]
    assert len(builds) == 2
    for command in builds:
        role = command[command.index("--tag") + 1].rsplit(":", 1)[-1]
        assert command[command.index("--build-context") + 1].endswith("@" + result["base_layouts"][role]["manifest_digest"])
        assert command[command.index("--build-context") + 1].startswith(debug.BASE_CONTEXT + "=oci-layout://")
        assert Path(command[-1]) == directory / "http-debug-build-context"
        assert command[command.index("--tag") + 1] == context["project"] + ":" + role
    assert events[-1][0] == ["docker", "buildx", "rm", proof["builder"]]
    assert all(record["status"] == "OWNED" for record in json.loads(Path(proof["registry"]).read_text())["builds"])
    assert not list(directory.glob("http-debug-base-*.tar"))


def test_buildx_0301_oci_path_has_no_windows_drive_colon_and_resolves_from_private_cwd(prepared, monkeypatch):
    directory, environment, context, _, events = diagnostic_build_fixture(prepared, monkeypatch)
    result = debug.build(directory, context, environment=environment)
    for command, _, options in events:
        if command[:3] != ["docker", "buildx", "build"]:
            continue
        reference = command[command.index("--build-context") + 1]
        local_path = reference.split("=oci-layout://", 1)[1].split("@", 1)[0]
        # The installed client's first-colon split must preserve the whole
        # path; absolute C:/... would become C and address an invalid store.
        assert local_path.split(":", 1)[0] == local_path
        role = command[command.index("--tag") + 1].rsplit(":", 1)[-1]
        assert local_path == result["base_layouts"][role]["layout"]
        assert options["cwd"] == directory
        assert (options["cwd"] / local_path).resolve() == (directory / ("http-debug-" + role + "-oci")).resolve()


def test_failed_partial_build_retains_exact_durable_owned_id_and_removes_only_its_builder(prepared, monkeypatch):
    directory, environment, context, proof, events = diagnostic_build_fixture(prepared, monkeypatch, fail=True)
    with pytest.raises(RuntimeError, match="Partial diagnostic load"):
        debug.build(directory, context, environment=environment)
    assert json.loads((directory / debug.PROOF_NAME).read_text())["status"] == "FAIL"
    records = json.loads(Path(proof["registry"]).read_text())["builds"]
    assert len(records) == 1 and records[0]["status"] == "OWNED"
    assert records[0]["image"]["image_id"] == "sha256:" + "c" * 64
    assert events[-1][0] == ["docker", "buildx", "rm", proof["builder"]]
    assert not any(command[:2] == ["docker", "build"] for command, _, _ in events)
