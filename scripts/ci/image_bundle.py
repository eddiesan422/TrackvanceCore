"""Transfer only immutable, same-commit Docker images between isolated CI jobs."""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import re
import subprocess
import sys
import tarfile
import tempfile
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'scripts'))
try:
    from .common import load_json
except ImportError:
    from common import load_json
from ci.image_content import (
    ARCHIVE_LIMIT,
    CHUNK,
    MAPPING_LIMIT,
    archive_identity,
    validate_identity,
)

ROLES = {"backend", "web"}
SHA = re.compile(r"[0-9a-f]{40}")
DIGEST = re.compile(r"sha256:[0-9a-f]{64}")


def file_digest(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def docker(*arguments: str) -> str:
    result = subprocess.run(["docker", *arguments], cwd=ROOT, capture_output=True,
                            encoding="utf-8", check=False, timeout=600)
    if result.returncode:
        raise ValueError("Docker image operation failed; private runner diagnostics required.")
    return result.stdout


def save_image(image: str, archive: Path) -> None:
    """Read-only Engine export, with a bounded stream and child lifetime."""
    if not DIGEST.fullmatch(image):
        raise ValueError("Only immutable images can be exported for verification.")
    with archive.open("xb") as target:
        process = subprocess.Popen(["docker", "save", image], cwd=ROOT,
                                   stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        timer = threading.Timer(600, process.kill)
        timer.daemon = True
        timer.start()
        try:
            if process.stdout is None:
                raise ValueError("Image export stream unavailable.")
            size = 0
            while chunk := process.stdout.read(CHUNK):
                size += len(chunk)
                if size > ARCHIVE_LIMIT:
                    raise ValueError("Image export exceeds finite archive budget.")
                target.write(chunk)
            if process.wait(timeout=30):
                raise ValueError("Image export failed or timed out.")
        finally:
            timer.cancel()
            if process.poll() is None:
                process.kill()
                process.wait(timeout=30)
            if process.stdout is not None:
                process.stdout.close()


def inspect_image(reference: str, sha: str) -> dict:
    rows = json.loads(docker("image", "inspect", reference))
    if len(rows) != 1 or not DIGEST.fullmatch(rows[0].get("Id", "")):
        raise ValueError("Expected exactly one immutable Docker Engine image ID.")
    row = rows[0]
    labels = row.get("Config", {}).get("Labels") or {}
    if labels.get("org.opencontainers.image.revision") != sha:
        raise ValueError("Image revision differs from the certified source commit.")
    if labels.get("org.opencontainers.image.version") != "0.8.5":
        raise ValueError("Image implementation version differs from 0.8.5.")
    if row.get("Os") != "linux" or row.get("Architecture") != "amd64":
        raise ValueError("CI images must use the certified Linux amd64 runtime.")
    return row


def validate_manifest(path: Path, sha: str, *, archives: bool = True) -> dict:
    if not SHA.fullmatch(sha) or path.is_symlink() or path.stat().st_size > 65536:
        raise ValueError("Invalid source revision or image manifest.")
    manifest = load_json(path)
    if (manifest.get("schema_version") != 1 or manifest.get("source_sha") != sha
            or manifest.get("digest_kind") not in {"DOCKER_CONFIGURATION_SHA256", "DOCKER_ENGINE_IMAGE_ID"}
            or manifest.get("status") != "PASS" or set(manifest.get("images", {})) != ROLES):
        raise ValueError("Incomplete image manifest or source revision mismatch.")
    for role, image in manifest["images"].items():
        if (not DIGEST.fullmatch(image.get("image_id", ""))
                or image.get("archive") != role + ".tar"
                or not re.fullmatch(r"[0-9a-f]{64}", image.get("archive_sha256", ""))
                or type(image.get("archive_bytes")) is not int
                or not 0 < image["archive_bytes"] <= 8 * 1024**3):
            raise ValueError("Invalid immutable image or archive descriptor.")
        if manifest["digest_kind"] == "DOCKER_ENGINE_IMAGE_ID" and (
                    image.get("image_id_kind") not in {"CONFIGURATION_SHA256", "OCI_TARGET_SHA256"}
                    or not DIGEST.fullmatch(image.get("configuration_digest", ""))
                    or not isinstance(image.get("rootfs_diff_ids"), list)
                    or len(image["rootfs_diff_ids"]) > 256
                    or not all(isinstance(v, str) and DIGEST.fullmatch(v) for v in image["rootfs_diff_ids"])):
            raise ValueError("Missing immutable configuration and layer identity.")
        if archives:
            archive = path.parent / image["archive"]
            if (archive.is_symlink() or not archive.is_file()
                    or archive.stat().st_size != image["archive_bytes"]
                    or file_digest(archive) != image["archive_sha256"]):
                raise ValueError("Missing or altered image archive.")
    proof = manifest.get('frontend_build_proof')
    if proof is not None:
        if (proof.get('schema_version') != 1 or proof.get('source_sha') != sha
                or not DIGEST.fullmatch(proof.get('build_image_id', ''))
                or proof.get('archive') != 'frontend-build-proof.zip'
                or not re.fullmatch(r'[0-9a-f]{64}', proof.get('archive_sha256', ''))
                or type(proof.get('archive_bytes')) is not int
                or not 0 < proof['archive_bytes'] <= 128 * 1024**2
                or proof.get('source_prefix') != 'source/' or proof.get('dist_prefix') != 'dist/'):
            raise ValueError('Invalid frontend build proof descriptor.')
        if archives:
            archive = path.parent / proof['archive']
            if (archive.is_symlink() or not archive.is_file() or archive.stat().st_size != proof['archive_bytes']
                    or file_digest(archive) != proof['archive_sha256']):
                raise ValueError('Missing or altered frontend build proof archive.')
    return manifest


def _archive_identity(path: Path, image: str, sha: str) -> dict:
    try:
        return archive_identity(path, image, sha)
    except (tarfile.TarError, EOFError, KeyError, TypeError, AttributeError) as error:
        raise ValueError("Invalid image content archive.") from error


def _source_identity(manifest: dict, descriptor: dict, proof: dict) -> None:
    validate_identity(proof, descriptor["image_id"], manifest["source_sha"])
    if manifest["digest_kind"] == "DOCKER_CONFIGURATION_SHA256":
        if descriptor["image_id"] != proof["configuration_digest"]:
            raise ValueError("Source manifest falsely declares a configuration digest.")
    elif any(descriptor.get(key) != proof.get(key) for key in (
            "image_id_kind", "configuration_digest", "rootfs_diff_ids")):
        raise ValueError("Source archive differs from its declared content identity.")


def _host_matches(source: dict, host: dict, row: dict) -> None:
    if (source["configuration_digest"] != host["configuration_digest"]
            or source["rootfs_diff_ids"] != host["rootfs_diff_ids"]
            or row["Id"] != host["engine_id"]
            or row.get("RootFS", {}).get("Type") != "layers"
            or row["RootFS"].get("Layers") != source["rootfs_diff_ids"]):
        raise ValueError("Loaded host image differs from certified configuration or layers.")


def validated_host_mapping(path: Path, sha: str, mapping_path: Path) -> dict[str, str]:
    """Revalidate hash-linked source/host trees; never rewrite the CI manifest."""
    manifest = validate_manifest(path, sha, archives=False)
    if (mapping_path.is_symlink() or not mapping_path.is_file()
            or not 0 < mapping_path.stat().st_size <= MAPPING_LIMIT):
        raise ValueError("Missing or excessive verified host image mapping.")
    mapping = load_json(mapping_path)
    images = mapping_images(manifest, file_digest(path), sha, mapping)
    for role, identifier in images.items():
        image = mapping["images"][role]
        _host_matches(image["source_identity"], image["host_identity"], inspect_image(identifier, sha))
    return images


def mapping_images(manifest: dict, manifest_hash: str, sha: str, mapping: dict) -> dict[str, str]:
    """Pure verification for a downloaded CI gate; no Engine access required."""
    if (mapping.get("schema_version") != 1 or mapping.get("kind") != "HOST_IMAGE_MAPPING"
            or mapping.get("status") != "PASS" or mapping.get("source_sha") != sha
            or mapping.get("source_manifest_sha256") != manifest_hash
            or not isinstance(mapping.get("images"), dict) or set(mapping["images"]) != ROLES):
        raise ValueError("Host mapping differs from original CI image manifest.")
    images = {}
    for role, original in manifest["images"].items():
        image = mapping["images"][role]
        if (image.get("source_image_id") != original["image_id"]
                or image.get("archive_sha256") != original["archive_sha256"]
                or image.get("archive_bytes") != original["archive_bytes"]
                or not DIGEST.fullmatch(image.get("host_image_id", ""))):
            raise ValueError("Host mapping source or archive binding changed.")
        source, host = image.get("source_identity"), image.get("host_identity")
        _source_identity(manifest, original, source)
        validate_identity(host, image["host_image_id"], sha)
        if (source["configuration_digest"] != host["configuration_digest"]
                or source["rootfs_diff_ids"] != host["rootfs_diff_ids"]):
            raise ValueError("Host mapping changed certified configuration or layers.")
        images[role] = image["host_image_id"]
    return images


def export_images(directory: Path, sha: str, backend: str, web: str,
                  frontend_build: str | None = None) -> dict:
    if not SHA.fullmatch(sha):
        raise ValueError("Expected a full source commit SHA.")
    directory.mkdir(parents=True, exist_ok=True)
    if any((directory / name).exists() or (directory / name).is_symlink() for name in (
            "images.json", "backend.tar", "web.tar", "frontend-build-proof.zip")):
        raise ValueError("Image export cannot replace original CI evidence.")
    manifest = {"schema_version": 1, "status": "PASS", "source_sha": sha,
                "digest_kind": "DOCKER_ENGINE_IMAGE_ID", "images": {}}
    for role, reference in (("backend", backend), ("web", web)):
        row = inspect_image(reference, sha)
        archive = directory / (role + ".tar")
        began = time.monotonic()
        save_image(row["Id"], archive)
        content = _archive_identity(archive, row["Id"], sha)
        manifest["images"][role] = {"image_id": row["Id"], "archive": archive.name,
            "archive_bytes": archive.stat().st_size, "archive_sha256": file_digest(archive),
            "image_bytes": row["Size"], "export_seconds": round(time.monotonic() - began, 3),
            **{key: content[key] for key in ("image_id_kind", "configuration_digest", "rootfs_diff_ids")}}
    if frontend_build:
        from ci.frontend_build_proof import export_proof
        manifest['frontend_build_proof'] = export_proof(directory, sha, frontend_build)
    path = directory / "images.json"
    path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    validate_manifest(path, sha)
    return manifest


def load_images(path: Path, sha: str, output: Path, host_mapping: Path | None = None) -> dict:
    # Verify every archive before the first Docker mutation.
    manifest = validate_manifest(path, sha)
    original_manifest_hash = file_digest(path)
    host_mapping = path.parent / "host-images.json" if host_mapping is None else host_mapping
    protected = {path.resolve(), *(path.parent / image["archive"] for image in manifest["images"].values())}
    protected = {p.resolve() for p in protected}
    protected.add((path.parent / "frontend-build-proof.zip").resolve())
    if (output.resolve() in protected or host_mapping.resolve() in protected
            or output.resolve() == host_mapping.resolve() or output.exists() or output.is_symlink()
            or host_mapping.exists() or host_mapping.is_symlink()):
        raise ValueError("Transfer receipts must be fresh and separate from original CI evidence.")
    sources = {}
    references = set()
    for role, image in manifest["images"].items():
        source = _archive_identity(path.parent / image["archive"], image["image_id"], sha)
        _source_identity(manifest, image, source)
        if references.intersection(source["references"]):
            raise ValueError("Runtime roles share an ambiguous archive reference.")
        references.update(source["references"])
        sources[role] = source
    if file_digest(path) != original_manifest_hash:
        raise ValueError("Original CI manifest changed before image transfer.")
    mapping = {"schema_version": 1, "kind": "HOST_IMAGE_MAPPING", "status": "PASS",
               "source_sha": sha, "source_manifest_sha256": original_manifest_hash, "images": {}}
    measurements = []
    output.parent.mkdir(parents=True, exist_ok=True)
    for role, image in manifest["images"].items():
        began = time.monotonic()
        loaded = docker("load", "--input", str(path.parent / image["archive"]))
        candidates = sources[role]["references"] or re.findall(
            r"^Loaded image ID: (sha256:[a-f0-9]{64})\s*$", loaded, re.MULTILINE)
        if not candidates or len(candidates) > 16:
            raise ValueError("Loaded archive did not identify its exact host image.")
        rows = [inspect_image(reference, sha) for reference in candidates]
        if len({row["Id"] for row in rows}) != 1:
            raise ValueError("Loaded archive resolves to multiple host images.")
        row = rows[0]
        # A second save is read-only to the Engine. It proves actual loaded config
        # bytes and every layer, including stores whose Id is a manifest/index.
        with tempfile.TemporaryDirectory(prefix="trackvance-image-proof-", dir=output.parent) as scratch:
            saved = Path(scratch) / "host.tar"
            save_image(row["Id"], saved)
            host = _archive_identity(saved, row["Id"], sha)
        _host_matches(sources[role], host, row)
        mapping["images"][role] = {"source_image_id": image["image_id"], "host_image_id": row["Id"],
            "archive_sha256": image["archive_sha256"], "archive_bytes": image["archive_bytes"],
            "source_identity": sources[role], "host_identity": host}
        measurements.append({"role": role, "image_id": row["Id"],
            "source_image_id": image["image_id"], "configuration_digest": host["configuration_digest"],
            "archive_bytes": image["archive_bytes"], "load_seconds": round(time.monotonic() - began, 3)})
    proof = {"schema_version": 1, "kind": "IMAGE_TRANSFER_PROOF", "status": "PASS", "source_sha": sha, "manifest_sha256": original_manifest_hash,
             "measurements": measurements, "rebuilds": 0}
    start_file = path.parent.parent / "image-transfer-start.txt"
    if start_file.is_file():
        stamp = float(start_file.read_text(encoding="utf-8").strip())
        elapsed = time.time() - stamp
        if not math.isfinite(elapsed) or not 0 <= elapsed <= 3600:
            raise ValueError("Invalid measured image transfer interval.")
        proof["download_verify_and_load_seconds"] = round(elapsed, 3)
    if file_digest(path) != mapping["source_manifest_sha256"]:
        raise ValueError("Original CI manifest changed during image transfer.")
    validate_manifest(path, sha)  # Also detect archive drift before a PASS receipt.
    mapping_bytes = (json.dumps(mapping, indent=2) + "\n").encode("utf-8")
    if len(mapping_bytes) > MAPPING_LIMIT:
        raise ValueError("Host image identity mapping exceeds finite metadata budget.")
    host_mapping.parent.mkdir(parents=True, exist_ok=True)
    with host_mapping.open("xb") as stream:
        stream.write(mapping_bytes)
    proof["host_mapping"] = str(host_mapping.resolve())
    proof["host_mapping_sha256"] = hashlib.sha256(mapping_bytes).hexdigest()
    with output.open("x", encoding="utf-8") as stream:
        stream.write(json.dumps(proof, indent=2) + "\n")
    return proof


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("export", "load"))
    parser.add_argument("--sha", required=True)
    parser.add_argument("--directory", type=Path, required=True)
    parser.add_argument("--backend", default="trackvance-ci:backend")
    parser.add_argument("--web", default="trackvance-ci:web")
    parser.add_argument("--frontend-build")
    parser.add_argument("--output", type=Path)
    parser.add_argument("--host-mapping", type=Path)
    args = parser.parse_args()
    if args.action == "export":
        proof = export_images(args.directory, args.sha, args.backend, args.web, args.frontend_build)
    else:
        if not args.output:
            parser.error("--output is mandatory when loading images")
        proof = load_images(args.directory / "images.json", args.sha, args.output, args.host_mapping)
    print(json.dumps({"status": proof["status"], "source_sha": args.sha}))


if __name__ == "__main__":
    main()
