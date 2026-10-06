"""Export committed frontend source and built assets from a stopped build image."""
from __future__ import annotations

import re
import subprocess
import tempfile
import zipfile
from pathlib import Path, PurePosixPath
from uuid import uuid4


def tracked_sources(root: Path, sha: str) -> dict[str, bytes]:
    listed = subprocess.run(["git", "ls-tree", "-r", "--name-only", sha, "--", "frontend"],
                            cwd=root, capture_output=True, text=True, check=True).stdout.splitlines()
    result = {}
    for name in listed:
        relative = PurePosixPath(name).relative_to("frontend")
        if (relative.is_absolute() or ".." in relative.parts
                or any(p in {"node_modules", ".env", ".codex-local", "dist"} for p in relative.parts)):
            raise ValueError("Unsafe committed frontend proof source.")
        result[relative.as_posix()] = subprocess.run(["git", "show", sha + ":" + name],
            cwd=root, capture_output=True, check=True).stdout
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
        import json
        rows = json.loads(docker("inspect", identifier))
        if (len(rows) != 1 or rows[0].get("Id") != identifier or rows[0].get("Image") != row["Id"]
                or rows[0].get("State", {}).get("Running") is not False
                or rows[0].get("Config", {}).get("Labels", {}).get("trackvance.ci.proof.owner") != owner):
            raise ValueError("Stopped proof container ownership changed; cleanup refused.")
        docker("rm", identifier)
