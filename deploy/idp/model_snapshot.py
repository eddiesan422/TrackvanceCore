"""Opaque offline model copy into one new volume; never execute model code."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import resource
import stat
import time
from pathlib import Path, PurePosixPath

MAX_BYTES = 10 * 1024**3
MANIFEST = ".trackvance-model-snapshot.json"
EMPTY_DIRECTORIES = {"ollama/manifests-v2"}


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()).hexdigest()


def relative_parts(name):
    if not isinstance(name, str):
        raise TypeError("MODEL_SNAPSHOT_PATH")
    path = PurePosixPath(name)
    if (not name or "\\" in name or path.is_absolute()
            or any(part in {"", ".", ".."} for part in name.split("/"))):
        raise ValueError("MODEL_SNAPSHOT_PATH")
    return path.parts


def open_source(root, relative):
    parts = relative_parts(relative)
    descriptor = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        for part in parts[:-1]:
            following = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = following
        result = os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=descriptor)
        if not stat.S_ISREG(os.fstat(result).st_mode):
            os.close(result)
            raise ValueError("MODEL_SNAPSHOT_SOURCE_TYPE")
        return result
    finally:
        os.close(descriptor)


def signature(value):
    return [value.st_dev, value.st_ino, value.st_size, value.st_mtime_ns, value.st_ctime_ns]


def check_deadline(deadline):
    if deadline is not None and time.monotonic() > deadline:
        raise ValueError("MODEL_SNAPSHOT_TIMEOUT")


def hash_stream(stream, deadline):
    hasher = hashlib.sha256()
    while True:
        check_deadline(deadline)
        chunk = stream.read(1024 * 1024)
        if not chunk:
            return hasher.hexdigest()
        hasher.update(chunk)


def inventory(root, *, deadline=None):
    files, directories = set(), set()
    for path in root.rglob("*"):
        check_deadline(deadline)
        if len(files) + len(directories) >= 512:
            raise ValueError("MODEL_SNAPSHOT_FILE_LIMIT")
        if path.is_symlink():
            raise ValueError("MODEL_SNAPSHOT_SYMLINK")
        name = path.relative_to(root).as_posix()
        if path.is_file():
            files.add(name)
        elif path.is_dir():
            directories.add(name)
        else:
            raise ValueError("MODEL_SNAPSHOT_SOURCE_TYPE")
    return files, directories


def model_plan(source, lock, *, deadline=None):
    check_deadline(deadline)
    expected = {kind + "/" + name: value for kind in ("paddle", "layout") for name, value in lock[kind]["files"].items()}
    path = "ollama/manifests/registry.ollama.ai/library/qwen3.5/9b-q4_K_M"
    with os.fdopen(open_source(source, path), "rb") as stream:
        before = signature(os.fstat(stream.fileno()))
        raw = stream.read(1024 * 1024 + 1)
        if len(raw) > 1024 * 1024 or before != signature(os.fstat(stream.fileno())):
            raise ValueError("MODEL_SNAPSHOT_MANIFEST")
    if hashlib.sha256(raw).hexdigest() != lock["qwen"]["manifest_sha256"]:
        raise ValueError("MODEL_SNAPSHOT_MANIFEST")
    manifest = json.loads(raw)
    expected[path] = lock["qwen"]["manifest_sha256"]
    for item in [manifest["config"], *manifest["layers"]]:
        if (not re.fullmatch(r"sha256:[a-f0-9]{64}", item["digest"])
                or type(item["size"]) is not int or not 0 < item["size"] <= MAX_BYTES):
            raise ValueError("MODEL_SNAPSHOT_MANIFEST")
        expected["ollama/blobs/" + item["digest"].replace(":", "-")] = item["digest"].split(":")[1]
    if not 1 <= len(expected) <= 128:
        raise ValueError("MODEL_SNAPSHOT_FILE_LIMIT")
    directories = set(EMPTY_DIRECTORIES)
    for name, value in expected.items():
        relative_parts(name)
        if not re.fullmatch(r"[a-f0-9]{64}", value):
            raise ValueError("MODEL_SNAPSHOT_DIGEST")
        directories.update(parent.as_posix() for parent in PurePosixPath(name).parents if parent.as_posix() != ".")
    # The model root may also contain the separate software cache. Only these
    # three exact model trees are inventoried or read.
    actual_files, actual_directories = set(), set()
    for kind in ("paddle", "layout", "ollama"):
        root = source / kind
        if root.is_symlink():
            raise ValueError("MODEL_SNAPSHOT_SYMLINK")
        files, folders = inventory(root, deadline=deadline)
        actual_files.update(kind + "/" + name for name in files)
        actual_directories.update({kind, *(kind + "/" + name for name in folders)})
    if actual_files != set(expected) or actual_directories != directories:
        raise ValueError("MODEL_SNAPSHOT_SOURCE_INVENTORY")
    return expected, directories


def copy_snapshot(source, destination, lock, identity, *, max_bytes=MAX_BYTES, timeout=900):
    began = time.monotonic()
    deadline = began + timeout
    if destination.is_symlink() or destination.stat().st_uid != os.getuid() or list(destination.iterdir()):
        raise ValueError("MODEL_SNAPSHOT_DESTINATION_NOT_FRESH")
    expected, directories = model_plan(source, lock, deadline=deadline)
    files, total = [], 0
    for name, expected_sha in sorted(expected.items()):
        check_deadline(deadline)
        path = destination / name
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(path.name + ".part")
        with os.fdopen(open_source(source, name), "rb") as original:
            before = signature(os.fstat(original.fileno()))
            if not 0 <= before[2] <= max_bytes - total:
                raise ValueError("MODEL_SNAPSHOT_BYTE_LIMIT")
            hasher, written = hashlib.sha256(), 0
            with temporary.open("xb") as target:
                while chunk := original.read(1024 * 1024):
                    written += len(chunk)
                    check_deadline(deadline)
                    if written > before[2]:
                        raise ValueError("MODEL_SNAPSHOT_SOURCE_CHANGED")
                    hasher.update(chunk)
                    target.write(chunk)
                target.flush()
                os.fsync(target.fileno())
            if before != signature(os.fstat(original.fileno())) or written != before[2]:
                raise ValueError("MODEL_SNAPSHOT_SOURCE_CHANGED")
        if hasher.hexdigest() != expected_sha:
            raise ValueError("MODEL_SNAPSHOT_SOURCE_INTEGRITY")
        temporary.replace(path)
        total += written
        files.append({"path": name, "bytes": written, "sha256": expected_sha, "source_fstat": before})
    for name in sorted(directories):
        (destination / name).mkdir(exist_ok=True)
    verification = time.monotonic()
    for item in files:
        with (destination / item["path"]).open("rb") as stream:
            if hash_stream(stream, deadline) != item["sha256"]:
                raise ValueError("MODEL_SNAPSHOT_DESTINATION_INTEGRITY")
    if inventory(destination, deadline=deadline) != (set(expected), directories):
        raise ValueError("MODEL_SNAPSHOT_DESTINATION_INVENTORY")
    try:
        cgroup_peak = int(Path("/sys/fs/cgroup/memory.peak").read_text().strip())
    except (OSError, ValueError):
        cgroup_peak = None
    result = {"status": "COMPLETE", "identity": identity, "files": files, "directories": sorted(directories),
        "bytes": total, "file_count": len(files), "destination_rehash_seconds": round(time.monotonic() - verification, 4),
        "elapsed_seconds": round(time.monotonic() - began, 4), "max_rss_bytes": resource.getrusage(resource.RUSAGE_SELF).ru_maxrss * 1024,
        "cgroup_memory_peak_bytes": cgroup_peak}
    result["manifest_sha256"] = digest(result)
    temporary = destination / (MANIFEST + ".part")
    with temporary.open("x", encoding="utf-8") as stream:
        json.dump(result, stream, indent=2)
        stream.flush()
        os.fsync(stream.fileno())
    temporary.replace(destination / MANIFEST)
    descriptor = os.open(destination, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("snapshot-id", "volume-name", "image-id", "lock-sha256"):
        parser.add_argument("--" + name, required=True)
    args = parser.parse_args()
    if (os.getuid() != 65532 or os.getgid() != 65532 or not re.fullmatch(r"[a-f0-9]{32}", args.snapshot_id)
            or not re.fullmatch(r"(?:tv-idp098|trackvance-idp098)[a-z0-9_-]{1,100}", args.volume_name)
            or not re.fullmatch(r"sha256:[a-f0-9]{64}", args.image_id)):
        raise ValueError("MODEL_SNAPSHOT_IDENTITY")
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    resource.setrlimit(resource.RLIMIT_FSIZE, (8 * 1024**3,) * 2)
    raw = Path("/app/models.lock.json").read_bytes()
    if hashlib.sha256(raw).hexdigest() != args.lock_sha256:
        raise ValueError("MODEL_SNAPSHOT_LOCK_INTEGRITY")
    identity = {"snapshot_id": args.snapshot_id, "volume_name": args.volume_name,
        "image_id": args.image_id, "model_lock_sha256": args.lock_sha256}
    filesystem = os.statvfs("/opt/venv")
    if filesystem.f_bavail * filesystem.f_frsize < MAX_BYTES + 3 * 1024**3:
        raise ValueError("MODEL_SNAPSHOT_DOCKER_DISK_RESERVE")
    result = copy_snapshot(Path("/source-models"), Path("/opt/venv"), json.loads(raw), identity)
    print(json.dumps({"phase": "MODEL_SNAPSHOT_COMPLETE", **result}), flush=True)


if __name__ == "__main__":
    main()
