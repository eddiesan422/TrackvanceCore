"""Transfer only immutable, same-commit Docker images between isolated CI jobs."""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import re
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'scripts'))
try:
    from .common import load_json
except ImportError:
    from common import load_json
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


def inspect_image(reference: str, sha: str) -> dict:
    rows = json.loads(docker("image", "inspect", reference))
    if len(rows) != 1 or not DIGEST.fullmatch(rows[0].get("Id", "")):
        raise ValueError("Expected exactly one immutable Docker configuration digest.")
    row = rows[0]
    labels = row.get("Config", {}).get("Labels") or {}
    if labels.get("org.opencontainers.image.revision") != sha:
        raise ValueError("Image revision differs from the certified source commit.")
    if labels.get("org.opencontainers.image.version") != "0.8.0":
        raise ValueError("Image implementation version differs from 0.8.0.")
    if row.get("Os") != "linux" or row.get("Architecture") != "amd64":
        raise ValueError("CI images must use the certified Linux amd64 runtime.")
    return row


def validate_manifest(path: Path, sha: str, *, archives: bool = True) -> dict:
    if not SHA.fullmatch(sha) or path.is_symlink() or path.stat().st_size > 65536:
        raise ValueError("Invalid source revision or image manifest.")
    manifest = load_json(path)
    if (manifest.get("schema_version") != 1 or manifest.get("source_sha") != sha
            or manifest.get("digest_kind") != "DOCKER_CONFIGURATION_SHA256"
            or manifest.get("status") != "PASS" or set(manifest.get("images", {})) != ROLES):
        raise ValueError("Incomplete image manifest or source revision mismatch.")
    for role, image in manifest["images"].items():
        if (not DIGEST.fullmatch(image.get("image_id", ""))
                or image.get("archive") != role + ".tar"
                or not re.fullmatch(r"[0-9a-f]{64}", image.get("archive_sha256", ""))
                or type(image.get("archive_bytes")) is not int
                or not 0 < image["archive_bytes"] <= 8 * 1024**3):
            raise ValueError("Invalid immutable image or archive descriptor.")
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


def export_images(directory: Path, sha: str, backend: str, web: str,
                  frontend_build: str | None = None) -> dict:
    if not SHA.fullmatch(sha):
        raise ValueError("Expected a full source commit SHA.")
    directory.mkdir(parents=True, exist_ok=True)
    manifest = {"schema_version": 1, "status": "PASS", "source_sha": sha,
                "digest_kind": "DOCKER_CONFIGURATION_SHA256", "images": {}}
    for role, reference in (("backend", backend), ("web", web)):
        row = inspect_image(reference, sha)
        archive = directory / (role + ".tar")
        began = time.monotonic()
        docker("save", "--output", str(archive), reference)
        manifest["images"][role] = {"image_id": row["Id"], "archive": archive.name,
            "archive_bytes": archive.stat().st_size, "archive_sha256": file_digest(archive),
            "image_bytes": row["Size"], "export_seconds": round(time.monotonic() - began, 3)}
    if frontend_build:
        from ci.frontend_build_proof import export_proof
        manifest['frontend_build_proof'] = export_proof(directory, sha, frontend_build)
    path = directory / "images.json"
    path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    validate_manifest(path, sha)
    return manifest


def load_images(path: Path, sha: str, output: Path) -> dict:
    # Verify every archive before the first Docker mutation.
    manifest = validate_manifest(path, sha)
    measurements = []
    for role, image in manifest["images"].items():
        began = time.monotonic()
        docker("load", "--input", str(path.parent / image["archive"]))
        row = inspect_image(image["image_id"], sha)
        if row["Id"] != image["image_id"]:
            raise ValueError("Loaded image differs from its immutable digest.")
        for prefix in ("trackvance-v070-isolated", "trackvance-v080-isolated"):
            docker("tag", row["Id"], f"{prefix}:{role}")
        measurements.append({"role": role, "image_id": row["Id"],
            "archive_bytes": image["archive_bytes"], "load_seconds": round(time.monotonic() - began, 3)})
    proof = {"status": "PASS", "source_sha": sha, "manifest_sha256": file_digest(path),
             "measurements": measurements, "rebuilds": 0}
    start_file = path.parent.parent / "image-transfer-start.txt"
    if start_file.is_file():
        stamp = float(start_file.read_text(encoding="utf-8").strip())
        elapsed = time.time() - stamp
        if not math.isfinite(elapsed) or not 0 <= elapsed <= 3600:
            raise ValueError("Invalid measured image transfer interval.")
        proof["download_verify_and_load_seconds"] = round(elapsed, 3)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(proof, indent=2) + "\n", encoding="utf-8")
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
    args = parser.parse_args()
    if args.action == "export":
        proof = export_images(args.directory, args.sha, args.backend, args.web, args.frontend_build)
    else:
        if not args.output:
            parser.error("--output is mandatory when loading images")
        proof = load_images(args.directory / "images.json", args.sha, args.output)
    print(json.dumps({"status": proof["status"], "source_sha": args.sha}))


if __name__ == "__main__":
    main()
