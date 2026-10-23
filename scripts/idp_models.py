"""Download only pinned official local IDP artifacts; never contact cloud services.

Caches live outside Git. Atomic writes and SHA-256 verification precede use.
The generated acquisition report also records hashes of small configuration files.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
LOCK = ROOT / "deploy/idp/models.lock.json"


def fetch(url, path, expected=None):
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists() and expected and hashlib.file_digest(path.open("rb"), "sha256").hexdigest() == expected:
        return {"file": str(path), "sha256": expected, "bytes": path.stat().st_size, "cached": True}
    temporary = path.with_suffix(path.suffix + ".download")
    digest = hashlib.sha256()
    try:
        with urllib.request.urlopen(url, timeout=120) as response, temporary.open("wb") as output:
            while chunk := response.read(1024 * 1024):
                digest.update(chunk)
                output.write(chunk)
        actual = digest.hexdigest()
        if expected and actual != expected:
            raise ValueError(f"HASH_MISMATCH: {path.name}: {actual}")
        temporary.replace(path)
        return {"file": str(path), "sha256": actual, "bytes": path.stat().st_size, "source": url}
    finally:
        temporary.unlink(missing_ok=True)


def download(destination):
    lock = json.loads(LOCK.read_text(encoding="utf-8"))
    destination = destination.resolve()
    tasks = []
    for key in ("paddle", "layout"):
        entry = lock[key]
        base = f"https://huggingface.co/{entry['repository']}/resolve/{entry['revision']}/"
        with urllib.request.urlopen(f"https://huggingface.co/api/models/{entry['repository']}/revision/{entry['revision']}", timeout=30) as response:
            names = json.load(response)["siblings"]
        for item in names:
            name = item["rfilename"]
            # Native Paddle runtime loads local data; custom remote Python is not executed.
            if "/" in name or name.startswith(".") or name.endswith(".py") or name == "README.md":
                continue
            tasks.append((base + urllib.parse.quote(name), destination / key / name, entry["files"].get(name)))
    manifest_url = "https://registry.ollama.ai/v2/library/qwen3.5/manifests/9b-q4_K_M"
    manifest_file = destination / "ollama/manifests/registry.ollama.ai/library/qwen3.5/9b-q4_K_M"
    (destination / "ollama/manifests-v2").mkdir(parents=True, exist_ok=True)
    report = [fetch(manifest_url, manifest_file, lock["qwen"]["manifest_sha256"])]
    manifest = json.loads(manifest_file.read_text(encoding="utf-8"))
    for blob in [manifest["config"], *manifest["layers"]]:
        digest = blob["digest"]
        tasks.append((f"https://registry.ollama.ai/v2/library/qwen3.5/blobs/{digest}",
                      destination / "ollama/blobs" / digest.replace(":", "-"), digest.split(":")[1]))
    with ThreadPoolExecutor(max_workers=3) as executor:
        report.extend(executor.map(lambda args: fetch(*args), tasks))
    result = {"lock_sha256": hashlib.sha256(LOCK.read_bytes()).hexdigest(), "files": report}
    (destination / "acquisition.json").write_text(json.dumps(result, indent=2), encoding="utf-8")
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--destination", type=Path, required=True)
    args = parser.parse_args()
    if os.environ.get("AWS_ACCESS_KEY_ID") or os.environ.get("AWS_SECRET_ACCESS_KEY"):
        raise SystemExit("Run the model downloader without cloud credential environment variables.")
    print(json.dumps(download(args.destination), indent=2))
