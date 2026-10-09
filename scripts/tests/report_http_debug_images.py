"""SHA-verified, finite, UUID-owned HTTP diagnostic images and cleanup.

The caller owns the Compose context. Preparation returns unique image references;
build records ownership before each mutation and replaces those references with
verified immutable IDs. No global diagnostic alias can be reused or overwritten.
"""
from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import math
import os
import re
import shutil
import subprocess
import sys
import tarfile
import threading
from pathlib import Path, PurePosixPath
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))
from ci.image_bundle import inspect_image
from ci.local_resources import (
    begin_image_build,
    cleanup_registered_images,
    docker_output,
    initialize_image_registry,
    observe_image_builds,
)
from ci.run_suite import execute
from ci_images import verified_images

GIB = 1024**3
PROOF_NAME = "report-http-debug-images.json"
BASE_CONTEXT = "tv_verified_base"
ARCHIVE_LIMIT = 8 * GIB
CHUNK_BYTES = 1024**2
MANIFEST_MEDIA = "application/vnd.oci.image.manifest.v1+json"
CONFIG_MEDIA = "application/vnd.oci.image.config.v1+json"
LAYER_MEDIA = "application/vnd.oci.image.layer.v1.tar"
INDEX_MEDIA = "application/vnd.oci.image.index.v1+json"
INDEX_TYPES = {INDEX_MEDIA, "application/vnd.docker.distribution.manifest.list.v2+json"}
MANIFEST_TYPES = {MANIFEST_MEDIA, "application/vnd.docker.distribution.manifest.v2+json"}
CONFIG_TYPES = {CONFIG_MEDIA, "application/vnd.docker.container.image.v1+json"}


def _write(path: Path, value: dict) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


def _copy_bounded(source, destination, maximum: int) -> tuple[str, int]:
    digest, size = hashlib.sha256(), 0
    while chunk := source.read(CHUNK_BYTES):
        size += len(chunk)
        if size > maximum:
            raise ValueError("Diagnostic image data exceeds its finite disk budget")
        destination.write(chunk)
        digest.update(chunk)
    return "sha256:" + digest.hexdigest(), size


def _export_archive(image: str, target: Path) -> None:
    """Child of execute(): stream a finite archive without buffering Docker output."""
    if not re.fullmatch(r"sha256:[a-f0-9]{64}", image):
        raise ValueError("Only an immutable base image can be exported")
    with target.open("xb") as output:
        process = subprocess.Popen(["docker", "image", "save", image], stdout=subprocess.PIPE)
        timer = threading.Timer(600, process.kill)
        timer.daemon = True
        timer.start()
        try:
            if process.stdout is None:
                raise ValueError("Docker image export has no stream")
            _copy_bounded(process.stdout, output, ARCHIVE_LIMIT)
            if process.wait(timeout=30):
                raise ValueError("Diagnostic base image export failed or timed out")
        finally:
            timer.cancel()
            if process.poll() is None:
                process.kill()
                process.wait(timeout=30)
            if process.stdout is not None:
                process.stdout.close()


def _json_bytes(value) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _engine_chain(read_metadata, image: str, config_digest: str, config_bytes: bytes) -> tuple[list[dict], list[bytes], list[dict] | None]:
    """Bind either a legacy config ID or a containerd target to the same config."""
    config_node = {"digest": config_digest, "mediaType": CONFIG_MEDIA, "size": len(config_bytes)}
    if image == config_digest:
        return [config_node], [config_bytes], None
    found, visited = [], 0

    def walk(digest, expected, nodes, payloads):
        nonlocal visited
        visited += 1
        if (visited > 256 or len(nodes) >= 16 or not re.fullmatch(r"sha256:[a-f0-9]{64}", digest)
                or digest in {node["digest"] for node in nodes}):
            raise ValueError("Diagnostic Engine target graph is invalid or excessive")
        payload = read_metadata("blobs/sha256/" + digest[7:], CHUNK_BYTES)
        if "sha256:" + hashlib.sha256(payload).hexdigest() != digest:
            raise ValueError("Diagnostic Engine target blob differs from its immutable digest")
        document = json.loads(payload)
        media = document.get("mediaType")
        node = {"digest": digest, "mediaType": media, "size": len(payload)}
        if (document.get("schemaVersion") != 2 or media not in INDEX_TYPES | MANIFEST_TYPES
                or expected is not None and any(node[key] != expected.get(key) for key in node)):
            raise ValueError("Diagnostic Engine target descriptor is inconsistent")
        chain, contents = [*nodes, node], [*payloads, payload]
        if media in MANIFEST_TYPES:
            descriptor = document.get("config", {})
            if descriptor.get("digest") != config_digest:
                return
            if descriptor.get("mediaType") not in CONFIG_TYPES or descriptor.get("size") != len(config_bytes):
                raise ValueError("Diagnostic Engine target configuration descriptor is inconsistent")
            layers = document.get("layers")
            if not isinstance(layers, list):
                raise ValueError("Diagnostic Engine target layers are invalid")
            found.append(([*chain, {key: descriptor[key] for key in ("digest", "mediaType", "size")}],
                          [*contents, config_bytes], layers))
            if len(found) > 1:
                raise ValueError("Diagnostic Engine target has ambiguous matching configurations")
            return
        children = document.get("manifests")
        if not isinstance(children, list) or len(children) > 256:
            raise ValueError("Diagnostic Engine target index is invalid or excessive")
        for child in children:
            platform = child.get("platform")
            if platform is not None and (platform.get("os"), platform.get("architecture")) != ("linux", "amd64"):
                continue
            walk(child.get("digest", ""), child, chain, contents)

    walk(image, None, [], [])
    if len(found) != 1:
        raise ValueError("Verified Engine target does not bind the selected Linux configuration")
    return found[0]


def archive_to_layout(archive: Path, layout: Path, image: str, sha: str) -> dict:
    """Convert Docker-save blobs to OCI while preserving the verified Engine ID.

    Layer tar files remain opaque blobs: no image filesystem paths are extracted.
    Exported gzip layers are decoded with bounded streaming and checked against
    the configuration's ordered diff_ids before exposing any OCI reference.
    """
    if (archive.is_symlink() or not archive.is_file() or not 0 < archive.stat().st_size <= ARCHIVE_LIMIT
            or not re.fullmatch(r"sha256:[a-f0-9]{64}", image)
            or not re.fullmatch(r"[a-f0-9]{40}", sha) or layout.exists() or layout.is_symlink()):
        raise ValueError("Invalid or nonfresh diagnostic OCI conversion input")
    layout.mkdir(mode=0o700)
    blobs = layout / "blobs" / "sha256"
    blobs.mkdir(parents=True, mode=0o700)
    with tarfile.open(archive, "r:") as saved:
        members = {}
        for member in saved:
            name = member.name.removeprefix("./")
            relative = PurePosixPath(name)
            if (len(members) >= 4096 or name in members or relative.is_absolute()
                    or ".." in relative.parts or "\\" in name or ":" in name
                    or not (member.isfile() or member.isdir()) or member.size < 0):
                raise ValueError("Diagnostic image archive has ambiguous or unsupported entries")
            members[name] = member

        def read_metadata(name, maximum):
            member = members.get(name)
            if member is None or not member.isfile() or member.size > maximum:
                raise ValueError("Missing or excessive diagnostic image metadata")
            source = saved.extractfile(member)
            if source is None:
                raise ValueError("Missing diagnostic image metadata stream")
            with source:
                return source.read(maximum + 1)

        manifests = json.loads(read_metadata("manifest.json", CHUNK_BYTES))
        if not isinstance(manifests, list) or len(manifests) != 1:
            raise ValueError("Diagnostic export must contain one exact image")
        config_bytes = read_metadata(manifests[0]["Config"], 16 * CHUNK_BYTES)
        config_digest = "sha256:" + hashlib.sha256(config_bytes).hexdigest()
        chain, chain_payloads, original_layers = _engine_chain(read_metadata, image, config_digest, config_bytes)
        config = json.loads(config_bytes)
        labels = config.get("config", {}).get("Labels") or {}
        if (config.get("os") != "linux" or config.get("architecture") != "amd64"
                or labels.get("org.opencontainers.image.revision") != sha
                or labels.get("org.opencontainers.image.version") != "0.8.5"
                or config.get("rootfs", {}).get("type") != "layers"):
            raise ValueError("Exported base configuration does not match the certified runtime")
        diff_ids, names = config["rootfs"]["diff_ids"], manifests[0]["Layers"]
        if (not isinstance(names, list) or not isinstance(diff_ids, list) or len(names) != len(diff_ids)
                or len(names) > 256 or not all(re.fullmatch(r"sha256:[a-f0-9]{64}", value) for value in diff_ids)):
            raise ValueError("Diagnostic exported layer order is invalid")
        for node, payload in zip(chain, chain_payloads, strict=True):
            (blobs / node["digest"][7:]).write_bytes(payload)
        layers, total = [], len(config_bytes)
        if original_layers is not None and len(original_layers) != len(names):
            raise ValueError("Diagnostic Engine manifest layer order differs from the export")
        for number, (name, expected) in enumerate(zip(names, diff_ids, strict=True)):
            member = members.get(name)
            if member is None or not member.isfile() or member.size > ARCHIVE_LIMIT:
                raise ValueError("Diagnostic exported layer is missing or excessive")
            source = saved.extractfile(member)
            if source is None:
                raise ValueError("Diagnostic exported layer stream is missing")
            temporary = blobs / ("layer-" + str(number) + ".partial")
            with source, temporary.open("xb") as output:
                if original_layers is not None:
                    original = original_layers[number]
                    if original.get("size") != member.size or not re.fullmatch(r"sha256:[a-f0-9]{64}", original.get("digest", "")):
                        raise ValueError("Diagnostic Engine layer descriptor differs from the export")
                    raw_hash = hashlib.sha256()
                    while chunk := source.read(CHUNK_BYTES):
                        raw_hash.update(chunk)
                    if "sha256:" + raw_hash.hexdigest() != original["digest"]:
                        raise ValueError("Diagnostic exported layer differs from the Engine manifest digest")
                    source.seek(0)
                prefix = source.read(4)
                source.seek(0)
                if prefix == b"\x28\xb5\x2f\xfd":
                    raise ValueError("Diagnostic zstd layer export is unsupported; no base was substituted")
                if prefix[:2] == b"\x1f\x8b":
                    with gzip.GzipFile(fileobj=source) as uncompressed:
                        digest, size = _copy_bounded(uncompressed, output, ARCHIVE_LIMIT - total)
                else:
                    digest, size = _copy_bounded(source, output, ARCHIVE_LIMIT - total)
            if digest != expected:
                raise ValueError("Exported layer differs from the verified image diff_id")
            final = blobs / digest[7:]
            if final.exists():
                temporary.unlink()
            else:
                temporary.replace(final)
            total += size
            layers.append({"mediaType": LAYER_MEDIA, "digest": digest, "size": size})
    manifest = {"schemaVersion": 2, "mediaType": MANIFEST_MEDIA,
        "config": {"mediaType": CONFIG_MEDIA, "digest": config_digest, "size": len(config_bytes)}, "layers": layers}
    payload = _json_bytes(manifest)
    manifest_digest = "sha256:" + hashlib.sha256(payload).hexdigest()
    (blobs / manifest_digest[7:]).write_bytes(payload)
    (layout / "oci-layout").write_bytes(_json_bytes({"imageLayoutVersion": "1.0.0"}))
    (layout / "index.json").write_bytes(_json_bytes({"schemaVersion": 2,
        "mediaType": INDEX_MEDIA, "manifests": [{"mediaType": MANIFEST_MEDIA,
            "digest": manifest_digest, "size": len(payload), "platform": {"os": "linux", "architecture": "amd64"}}]}))
    return {"layout": layout.name, "manifest_digest": manifest_digest, "engine_digest": image,
        "engine_digest_kind": "DOCKER_CONFIGURATION_SHA256" if image == config_digest else "OCI_TARGET_SHA256",
        "config_digest": config_digest, "source_sha": sha, "source_chain": chain,
        "layers": layers, "blob_bytes": total + len(payload) + sum(len(value) for value in chain_payloads[:-1])}


def verify_layout(layout: Path, descriptor: dict, image: str) -> None:
    if layout.is_symlink() or layout.name != descriptor["layout"] or descriptor["engine_digest"] != image:
        raise ValueError("Diagnostic OCI layout identity changed")
    digest = descriptor["manifest_digest"]
    if not re.fullmatch(r"sha256:[a-f0-9]{64}", digest):
        raise ValueError("Invalid diagnostic OCI manifest digest")
    blob_root = layout / "blobs" / "sha256"
    manifest_path = blob_root / digest[7:]
    if manifest_path.is_symlink() or manifest_path.stat().st_size > CHUNK_BYTES:
        raise ValueError("Diagnostic OCI manifest is linked or excessive")
    payload = manifest_path.read_bytes()
    if "sha256:" + hashlib.sha256(payload).hexdigest() != digest:
        raise ValueError("Diagnostic OCI manifest changed")
    manifest = json.loads(payload)
    if manifest["config"]["digest"] != descriptor["config_digest"] or manifest["layers"] != descriptor["layers"]:
        raise ValueError("Diagnostic OCI base components changed")
    expected_index = {"schemaVersion": 2, "mediaType": "application/vnd.oci.image.index.v1+json",
        "manifests": [{"mediaType": MANIFEST_MEDIA, "digest": descriptor["manifest_digest"], "size": len(payload),
                       "platform": {"os": "linux", "architecture": "amd64"}}]}
    for name, expected in (("oci-layout", {"imageLayoutVersion": "1.0.0"}), ("index.json", expected_index)):
        path = layout / name
        if path.is_symlink() or path.stat().st_size > CHUNK_BYTES or json.loads(path.read_bytes()) != expected:
            raise ValueError("Diagnostic OCI layout entry point changed")
    chain = descriptor["source_chain"]
    if (not isinstance(chain, list) or not 1 <= len(chain) <= 17 or chain[0]["digest"] != image
            or chain[-1]["digest"] != descriptor["config_digest"]):
        raise ValueError("Diagnostic Engine provenance chain changed")
    contents = []
    for component in [manifest["config"], *manifest["layers"], *chain]:
        if not re.fullmatch(r"sha256:[a-f0-9]{64}", component["digest"]):
            raise ValueError("Invalid diagnostic OCI blob digest")
        path = blob_root / component["digest"][7:]
        if (path.is_symlink() or not path.is_file() or path.stat().st_size != component["size"]
                or not path.resolve().is_relative_to(layout.resolve())):
            raise ValueError("Diagnostic OCI blob is missing, linked or changed")
        with path.open("rb") as source:
            digest = hashlib.sha256()
            while chunk := source.read(CHUNK_BYTES):
                digest.update(chunk)
        if "sha256:" + digest.hexdigest() != component["digest"]:
            raise ValueError("Diagnostic OCI blob bytes changed")
    for node in chain:
        if node["size"] > (16 * CHUNK_BYTES if node["digest"] == descriptor["config_digest"] else CHUNK_BYTES):
            raise ValueError("Diagnostic Engine provenance metadata is excessive")
        contents.append(json.loads((blob_root / node["digest"][7:]).read_bytes()))
    for position, document in enumerate(contents[:-1]):
        node, following = chain[position], chain[position + 1]
        if document.get("mediaType") != node["mediaType"] or document.get("schemaVersion") != 2:
            raise ValueError("Diagnostic Engine provenance metadata changed")
        children = document.get("manifests", []) if node["mediaType"] in INDEX_TYPES else [document.get("config", {})]
        matching = [child for child in children if all(child.get(key) == following[key] for key in following)]
        if node["mediaType"] not in INDEX_TYPES | MANIFEST_TYPES or len(matching) != 1:
            raise ValueError("Diagnostic Engine provenance link changed")
    config = contents[-1]
    labels = config.get("config", {}).get("Labels") or {}
    if (config.get("os") != "linux" or config.get("architecture") != "amd64"
            or labels.get("org.opencontainers.image.revision") != descriptor["source_sha"]
            or labels.get("org.opencontainers.image.version") != "0.8.5"
            or config.get("rootfs", {}).get("type") != "layers"
            or config["rootfs"]["diff_ids"] != [layer["digest"] for layer in manifest["layers"]]):
        raise ValueError("Diagnostic configuration labels, platform or ordered layer content changed")


def prepare(directory: Path, context: dict, *, environment: dict[str, str] | None = None) -> dict:
    values = dict(os.environ if environment is None else environment)
    images = verified_images(values)
    sha = values.get("TRACKVANCE_SOURCE_SHA", values.get("CI_SOURCE_SHA", values.get("GITHUB_SHA", "")))
    project = context["project"]
    if (not images or set(images) != {"backend", "web"} or not re.fullmatch(r"[a-f0-9]{40}", sha)
            or not re.fullmatch(r"trackvance-v080-test-reports-http-[a-f0-9]{12}", project)
            or images["backend"] != context["image"]
            or not all(re.fullmatch(r"sha256:[a-f0-9]{64}", value) for value in images.values())):
        raise ValueError("HTTP diagnostics require exact owned context and verified immutable same-SHA base images")
    path = directory / PROOF_NAME
    if path.exists():
        raise ValueError("HTTP diagnostic image preparation must be fresh")
    references = {role: project + ":" + role for role in images}
    execution = values.get("TRACKVANCE_LOCAL_EXECUTION_ID") or "local-" + uuid4().hex
    registry = Path(values.get("TRACKVANCE_LOCAL_IMAGE_REGISTRY", directory / "http-debug-owned-images.json"))
    builder_registry = Path(values.get("TRACKVANCE_LOCAL_BUILDER_REGISTRY", directory / "http-debug-owned-builders.json"))
    builder = "tv-local-build-" + uuid4().hex[:12]
    proof = {"schema_version": 1, "kind": "OWNED_HTTP_DEBUG_IMAGES", "status": "PREPARED", "source_sha": sha,
        "project": project, "ownership_execution_id": execution, "base_images": images, "references": references,
        "registry": str(registry.resolve()), "builder_registry": str(builder_registry.resolve()), "builder": builder}
    dockerfiles = {}
    for role in images:
        packages = ("RUN apt-get update && apt-get install -y --no-install-recommends strace && rm -rf /var/lib/apt/lists/*\nUSER trackvance\n"
            if role == "backend" else "RUN apk add --no-cache strace && mkdir -p /var/cache/nginx && chown -R nginx:nginx /var/cache/nginx\nUSER nginx\n")
        dockerfile = directory / (role + ".Dockerfile")
        dockerfile.write_text("FROM " + BASE_CONTEXT + "\nUSER root\n" + packages, encoding="utf-8")
        dockerfiles[role] = hashlib.sha256(dockerfile.read_bytes()).hexdigest()
    proof["dockerfile_sha256"] = dockerfiles
    # The build sends an empty context, never test.env, observers or private
    # fixture files from the enclosing certification context.
    (directory / "http-debug-build-context").mkdir(mode=0o700)
    with path.open("x", encoding="utf-8") as stream:
        json.dump(proof, stream, indent=2)
    return proof


def build(directory: Path, context: dict, *, environment: dict[str, str] | None = None) -> dict:
    values = dict(os.environ if environment is None else environment)
    proof = json.loads((directory / PROOF_NAME).read_text(encoding="utf-8"))
    images = verified_images(values)
    sha = values.get("TRACKVANCE_SOURCE_SHA", values.get("CI_SOURCE_SHA", values.get("GITHUB_SHA", "")))
    if (proof.get("status") != "PREPARED" or proof.get("project") != context["project"]
            or proof.get("source_sha") != sha or proof.get("base_images") != images
            or proof["base_images"]["backend"] != context["image"]
            or proof.get("references") != {role: context["project"] + ":" + role for role in images}
            or not re.fullmatch(r"tv-local-build-[a-f0-9]{12}", proof.get("builder", ""))
            or proof["registry"] != str(Path(values.get("TRACKVANCE_LOCAL_IMAGE_REGISTRY",
                                                       directory / "http-debug-owned-images.json")).resolve())
            or proof["builder_registry"] != str(Path(values.get("TRACKVANCE_LOCAL_BUILDER_REGISTRY",
                                                               directory / "http-debug-owned-builders.json")).resolve())):
        raise ValueError("Diagnostic build input or source identity changed")
    for role in proof["base_images"]:
        dockerfile = directory / (role + ".Dockerfile")
        if (dockerfile.is_symlink() or not dockerfile.read_text(encoding="utf-8").startswith("FROM " + BASE_CONTEXT + "\n")
                or hashlib.sha256(dockerfile.read_bytes()).hexdigest() != proof["dockerfile_sha256"][role]):
            raise ValueError("Diagnostic Dockerfile no longer pins the verified base image")
    empty_context = directory / "http-debug-build-context"
    if empty_context.is_symlink() or not empty_context.is_dir() or any(empty_context.iterdir()):
        raise ValueError("Diagnostic build context must remain empty and private")
    memory = min(3 * GIB, int(values.get("TRACKVANCE_LOCAL_MAX_MEMORY_BYTES", str(3 * GIB))))
    requested_cpus = float(values.get("TRACKVANCE_LOCAL_MAX_CPUS", "2"))
    if not math.isfinite(requested_cpus):
        raise ValueError("The diagnostic builder requires a finite usable budget")
    cpus = min(2.0, requested_cpus)
    if not 512 * 1024**2 <= memory <= 3 * GIB or not 0.25 <= cpus <= 2.0:
        raise ValueError("The diagnostic builder requires a finite usable budget")
    registry = Path(proof["registry"])
    execution = proof["ownership_execution_id"]
    initialize_image_registry(registry, execution)
    builder = proof["builder"]
    if builder in docker_output("buildx", "ls", "--format", "{{.Name}}").split():
        raise ValueError("Diagnostic builder identity is preexisting")
    config = directory / "http-debug-buildkit.toml"
    config.write_text('[worker.oci]\n max-parallelism = 1\n gc = true\n reservedSpace = "512MB"\n maxUsedSpace = "4GB"\n', encoding="utf-8")
    proof.update(status="BUILDING", builder_memory_limit_bytes=memory, builder_cpu_limit=cpus)
    _write(directory / PROOF_NAME, proof)
    builder_started = False
    try:
        proof["base_layouts"] = {}
        for role, base in images.items():
            row = inspect_image(base, sha)
            size = row.get("Size")
            if (row["Id"] != base or type(size) is not int or not 0 < size <= ARCHIVE_LIMIT
                    or shutil.disk_usage(directory).free < 2 * size + GIB):
                raise ValueError("Diagnostic base identity or finite export disk capacity is invalid")
            archive = directory / ("http-debug-base-" + role + ".tar")
            try:
                execute([sys.executable, str(Path(__file__).resolve()), "--export", base, str(archive)],
                    directory, "http-debug-base-export-" + role, 660, environment=values)
                descriptor = archive_to_layout(archive, directory / ("http-debug-" + role + "-oci"), base, sha)
            finally:
                # This fresh fixed path is created exclusively by our export child.
                if archive.is_file() and not archive.is_symlink():
                    archive.unlink()
            proof["base_layouts"][role] = descriptor
            _write(directory / PROOF_NAME, proof)
        builder_registry = Path(proof["builder_registry"])
        names = json.loads(builder_registry.read_text(encoding="utf-8")) if builder_registry.exists() else []
        builder_registry.write_text(json.dumps([*names, builder]) + "\n", encoding="utf-8")
        (directory / "builder-images.before.json").write_text(json.dumps(docker_output("image", "ls", "-aq", "--no-trunc").split()), encoding="utf-8")
        builder_started = True
        execute(["docker", "buildx", "create", "--name", builder, "--driver", "docker-container",
            "--buildkitd-config", str(config), "--driver-opt", "memory=" + str(memory),
            "--driver-opt", "memory-swap=" + str(memory), "--driver-opt", "cpu-quota=" + str(int(cpus * 100000)),
            "--driver-opt", "cpu-period=100000", "--driver-opt", "restart-policy=no"],
            directory, "http-debug-builder-create", 180, environment=values)
        built = {}
        for role, reference in proof["references"].items():
            descriptor = proof["base_layouts"][role]
            layout = directory / descriptor["layout"]
            verify_layout(layout, descriptor, images[role])
            labels = begin_image_build(registry, execution, context["project"], sha, "0.8.5", role, reference)
            # Buildx 0.30.1 splits OCI paths at the first colon, including a
            # Windows drive. Resolve the checked layout from this private cwd.
            execute(["docker", "buildx", "build", "--builder", builder, "--load", "--platform", "linux/amd64",
                "--build-context", BASE_CONTEXT + "=oci-layout://" + layout.name + "@" + descriptor["manifest_digest"],
                *[argument for key, value in labels.items() for argument in ("--label", key + "=" + value)],
                "--tag", reference, "--file", str(directory / (role + ".Dockerfile")), str(directory / "http-debug-build-context")],
                directory, "http-debug-build-" + role, 600, cwd=directory, environment=values)
            observe_image_builds(registry, execution)
            built[role] = inspect_image(reference, sha)["Id"]
        proof.update(status="PASS", images=built)
    except BaseException:
        proof["status"] = "FAIL"
        raise
    finally:
        _write(directory / PROOF_NAME, proof)
        try:
            observe_image_builds(registry, execution)
        finally:
            if builder_started:
                cleanup_environment = {key: value for key, value in values.items() if key != "TRACKVANCE_LOCAL_PROTECTED_INVENTORY"}
                execute(["docker", "buildx", "rm", builder], directory, "http-debug-builder-cleanup", 180,
                        environment=cleanup_environment)
    return proof


def cleanup(directory: Path) -> dict:
    """Call after owned diagnostic containers are gone; retain uncertain consumers."""
    proof = json.loads((directory / PROOF_NAME).read_text(encoding="utf-8"))
    registry = Path(proof["registry"])
    if registry.exists():
        result = cleanup_registered_images(registry, proof["ownership_execution_id"], projects={proof["project"]})
    else:
        result = {"status": "PASS", "removed": [], "reason": "NO_DIAGNOSTIC_BUILD_STARTED"}
    from ci.run_local import cleanup_builder_images
    result["builder_images"] = cleanup_builder_images(directory)
    _write(directory / "report-http-debug-image-cleanup.json", result)
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Stream one verified diagnostic base export")
    parser.add_argument("--export", nargs=2, metavar=("IMMUTABLE_ID", "FRESH_ARCHIVE"), required=True)
    args = parser.parse_args()
    _export_archive(args.export[0], Path(args.export[1]))
