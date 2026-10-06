"""Export committed frontend source and built assets from a stopped build image."""
from __future__ import annotations

import argparse
import io
import json
import re
import stat
import subprocess
import tempfile
import zipfile
from pathlib import Path, PurePosixPath
from uuid import uuid4


def tracked_sources(root: Path, sha: str) -> dict[str, bytes]:
    # One immutable Git archive avoids launching git once for every source file.
    payload = subprocess.run(["git", "archive", "--format=zip", sha, "frontend"],
                             cwd=root, capture_output=True, check=True).stdout
    result = {}
    with zipfile.ZipFile(io.BytesIO(payload)) as archive:
        for info in archive.infolist():
            if info.is_dir():
                continue
            relative = PurePosixPath(info.filename).relative_to("frontend")
            if (relative.is_absolute() or ".." in relative.parts
                    or stat.S_ISLNK(info.external_attr >> 16)
                    or any(p in {"node_modules", ".env", ".codex-local", "dist"} for p in relative.parts)):
                raise ValueError("Unsafe committed frontend proof source.")
            result[relative.as_posix()] = archive.read(info)
    if not result or "package.json" not in result or "src/app/App.tsx" not in result:
        raise ValueError("Incomplete committed frontend proof source.")
    return result


def export_proof(directory: Path, sha: str, reference: str) -> dict:
    from .image_bundle import ROOT, docker, file_digest, inspect_image

    row = inspect_image(reference, sha)
    if row.get("Config", {}).get("Volumes"):
        raise ValueError("The stopped build proof image must not create implicit volumes.")
    expected = tracked_sources(ROOT, sha)
    owner = "trackvance-ci-frontend-proof-" + uuid4().hex[:12]
    identifier = docker("create", "--name", owner, "--network", "none", "--label",
                        "trackvance.ci.proof.owner=" + owner, row["Id"], "/bin/true").strip()
    if not re.fullmatch(r"[0-9a-f]{64}", identifier):
        raise ValueError("Invalid stopped proof container identity.")
    try:
        with tempfile.TemporaryDirectory(prefix=owner + "-") as private:
            root = Path(private)
            source, dist = root / "source", root / "dist"
            source.mkdir()
            # Copy top-level scopes only. node_modules never leaves the build image.
            for scope in sorted({PurePosixPath(name).parts[0] for name in expected}):
                docker("cp", identifier + ":/app/" + scope, str(source / scope))
            docker("cp", identifier + ":/app/dist", str(dist))
            for name, content in expected.items():
                path = source / name
                if path.is_symlink() or not path.is_file() or path.read_bytes() != content:
                    raise ValueError("The build image frontend source differs from committed bytes.")
            assets = []
            for path in sorted(dist.rglob("*")):
                if path.is_symlink() or not (path.is_dir() or path.is_file()):
                    raise ValueError("Invalid built frontend proof file.")
                if path.is_file():
                    assets.append(path)
            if not assets or not (dist / "index.html").is_file() or len(expected) + len(assets) > 20000:
                raise ValueError("Incomplete or excessive frontend proof asset inventory.")
            total = sum(len(content) for content in expected.values()) + sum(p.stat().st_size for p in assets)
            if total > 512 * 1024**2:
                raise ValueError("Excessive frontend proof bytes.")
            archive = directory / "frontend-build-proof.zip"
            with zipfile.ZipFile(archive, "x", compression=zipfile.ZIP_DEFLATED) as output:
                for name in sorted(expected):
                    output.write(source / name, "source/" + name)
                for path in assets:
                    output.write(path, "dist/" + path.relative_to(dist).as_posix())
            if archive.stat().st_size > 128 * 1024**2:
                raise ValueError("Excessive frontend proof archive.")
            return {"schema_version": 1, "source_sha": sha, "build_image_id": row["Id"],
                "archive": archive.name, "archive_sha256": file_digest(archive),
                "archive_bytes": archive.stat().st_size, "source_prefix": "source/", "dist_prefix": "dist/"}
    finally:
        rows = json.loads(docker("inspect", identifier))
        if (len(rows) != 1 or rows[0].get("Id") != identifier or rows[0].get("Image") != row["Id"]
                or rows[0].get("State", {}).get("Running") is not False
                or rows[0].get("Config", {}).get("Labels", {}).get("trackvance.ci.proof.owner") != owner):
            raise ValueError("Stopped proof container ownership changed; cleanup refused.")
        docker("rm", identifier)


def verify_proof(manifest_path: Path, sha: str) -> dict:
    """Verify the original build artifacts without loading or rebuilding images."""
    try:
        from .image_bundle import ROOT, file_digest, validate_manifest
    except ImportError:
        from image_bundle import ROOT, file_digest, validate_manifest

    manifest = validate_manifest(manifest_path, sha, archives=False)
    descriptor = manifest.get("frontend_build_proof")
    if descriptor is None:
        raise ValueError("Missing committed frontend build proof.")
    path = manifest_path.parent / descriptor["archive"]
    if (path.is_symlink() or not path.is_file() or path.stat().st_size != descriptor["archive_bytes"]
            or file_digest(path) != descriptor["archive_sha256"]):
        raise ValueError("Missing or altered frontend build proof archive.")
    expected = tracked_sources(ROOT, sha)
    with zipfile.ZipFile(path) as archive:
        infos = archive.infolist()
        names = [info.filename for info in infos]
        if (len(set(names)) != len(names) or len(names) > 20000
                or sum(info.file_size for info in infos) > 512 * 1024**2):
            raise ValueError("Duplicate or excessive frontend proof inventory.")
        for info in infos:
            parsed = PurePosixPath(info.filename)
            if (parsed.is_absolute() or ".." in parsed.parts or "\\" in info.filename
                    or not parsed.parts or parsed.parts[0] not in {"source", "dist"}
                    or info.is_dir() or stat.S_ISLNK(info.external_attr >> 16)):
                raise ValueError("Unsafe frontend proof entry.")
        sources = {name.removeprefix("source/") for name in names if name.startswith("source/")}
        assets = {name for name in names if name.startswith("dist/")}
        if sources != expected.keys() or "dist/index.html" not in assets or not assets:
            raise ValueError("Incomplete committed frontend proof source or assets.")
        for name, content in expected.items():
            if archive.read("source/" + name) != content:
                raise ValueError("The build proof frontend source differs from committed bytes.")
        if archive.testzip() is not None:
            raise ValueError("Corrupt frontend build proof asset.")
    return {"status": "PASS", "source_sha": sha, "archive_sha256": descriptor["archive_sha256"],
            "build_image_id": descriptor["build_image_id"], "source_count": len(expected),
            "asset_count": len(assets), "rebuilds": 0}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--sha", required=True)
    args = parser.parse_args()
    print(json.dumps(verify_proof(args.manifest, args.sha)))


if __name__ == "__main__":
    main()
