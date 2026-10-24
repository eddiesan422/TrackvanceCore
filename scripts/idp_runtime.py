"""Build an isolated cu129 runtime; initialize its private volume offline.

The large software image is exported, never imported into the Docker daemon.
Only the small gateway image is loaded. No permanent environment is changed.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
import subprocess
import tarfile
import time
import urllib.request
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEPLOY = ROOT / "deploy/idp"
OWNER = "trackvance-idp098"
WHEEL = "paddlepaddle_gpu-3.2.1-cp312-cp312-linux_x86_64.whl"
WHEEL_URL = "https://paddle-whl.cdn.bcebos.com/stable/cu129/paddlepaddle-gpu/" + WHEEL
WHEEL_SHA = "01924dc83214a041bcc78de01c6be67ffe42e88909c7f4b6d13c31b363851b0a"
BUILDKIT = "moby/buildkit@sha256:cec9f139f45e93c5c69c60f8b07cfad9f43f4ef6b6a6cd917527fea5ff2e3dea"
SOURCES = ("server.py", "paddle_runner.py", "runtime_probe.py", "runtime_init.py",
           "pipeline.yml", "prompt-v1.txt", "prompt-v2.txt", "document_semantic_fragments.py", "models.lock.json")


def source_path(name):
    if name == "document_semantic_fragments.py":
        return ROOT / "backend/src/trackvance" / name
    return DEPLOY / name


def sha(path):
    with Path(path).open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def output(*args):
    return subprocess.check_output(["docker", *args], text=True, timeout=30).strip()


def host_available():
    """Windows host reserve, independent of the larger WSL guest cache."""
    import os
    if os.name != "nt":
        return None
    import ctypes

    class Memory(ctypes.Structure):
        _fields_ = [("length", ctypes.c_ulong), ("load", ctypes.c_ulong),
                    *[(name, ctypes.c_ulonglong) for name in
                      ("physical", "available", "page", "available_page", "virtual", "available_virtual", "extended")]]

    status = Memory()
    status.length = ctypes.sizeof(status)
    if not ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status)):
        raise OSError("HOST_MEMORY_PROBE_FAILED")
    return status.available


def require_reserve(gib=3):
    available = host_available()
    if available is not None and available < gib * 1024**3:
        raise RuntimeError("HOST_RESERVE_BELOW_3_GIB")


def run(args, log):
    require_reserve()
    print(json.dumps({"phase": args[1:3]}), flush=True)
    with log.open("ab") as stream:
        process = subprocess.Popen(args, stdout=stream, stderr=subprocess.STDOUT)
        started = time.monotonic()
        try:
            while process.poll() is None:
                require_reserve()
                if time.monotonic() - started > 2400:
                    raise TimeoutError("BOUNDED_RUNTIME_PREPARATION_TIMEOUT")
                time.sleep(1)
            if process.returncode:
                raise RuntimeError("RUNTIME_COMMAND_FAILED; inspect private preparation.log")
        finally:
            if process.poll() is None:
                process.terminate()
                process.wait(timeout=15)


def software_manifest(archive, requirements_sha):
    """Select only the two pip layers from a trusted, hash-verified own archive."""
    with tarfile.open(archive, "r:") as outer:
        manifest = json.load(outer.extractfile("manifest.json"))
        if len(manifest) != 1:
            raise ValueError("SOFTWARE_ARCHIVE_INVALID")
        config_path = manifest[0]["Config"]
        config_bytes = outer.extractfile(config_path).read(65537)
        config_sha = hashlib.sha256(config_bytes).hexdigest()
        if len(config_bytes) > 65536 or config_path != "blobs/sha256/" + config_sha:
            raise ValueError("SOFTWARE_CONFIG_INTEGRITY")
        config = json.loads(config_bytes)
        history = [item for item in config["history"] if not item.get("empty_layer")]
        layers = manifest[0]["Layers"]
        if len(layers) > 32 or len(history) != len(layers):
            raise ValueError("SOFTWARE_HISTORY_INVALID")
        selected = []
        lock_verified = False
        for entry, layer in zip(history, layers, strict=True):
            if not re.fullmatch(r"blobs/sha256/[0-9a-f]{64}", layer):
                raise ValueError("SOFTWARE_LAYER_PATH_INVALID")
            command = entry.get("created_by", "")
            if "COPY requirements.lock /install/requirements.lock" in command:
                compressed = outer.extractfile(layer)
                if hashlib.file_digest(compressed, "sha256").hexdigest() != layer.rsplit("/", 1)[1]:
                    raise ValueError("SOFTWARE_LOCK_LAYER_INTEGRITY")
                compressed.seek(0)
                with tarfile.open(fileobj=compressed, mode="r|gz") as contents:
                    for member in contents:
                        if member.name.removeprefix("./") == "install/requirements.lock":
                            payload = contents.extractfile(member).read(1024 * 1024)
                            lock_verified = hashlib.sha256(payload).hexdigest() == requirements_sha
            if "python -m pip install --no-cache-dir --require-hashes" in command:
                selected.append(layer.rsplit("/", 1)[1])
            if "python -m pip install --no-cache-dir --no-deps" in command:
                if WHEEL_SHA not in command:
                    raise ValueError("SOFTWARE_WHEEL_NOT_PINNED")
                selected.append(layer.rsplit("/", 1)[1])
        if len(selected) != 2 or not lock_verified:
            raise ValueError("SOFTWARE_DEPENDENCY_PROOF_MISSING")
        return {"schema_version": 1, "layers": selected,
                "source_image_config_sha256": config_sha,
                "requirements_sha256": requirements_sha, "paddle_wheel_sha256": WHEEL_SHA}


def prepare(args):
    cache, models = args.cache.resolve(), args.models.resolve()
    if cache == ROOT or ROOT in cache.parents:
        raise ValueError("Use a private cache outside the source checkout.")
    if any(char in str(path) for path in (cache, models) for char in (",", "\n", "\r", "$")):
        raise ValueError("Unsupported deployment path characters.")
    if bool(args.software_archive) != bool(args.software_sha256):
        raise ValueError("An adopted own archive requires its independently retained SHA-256.")
    cache.mkdir(parents=True, exist_ok=True)
    log = cache / "preparation.log"
    if output("info", "--format", "{{.OSType}}") != "linux":
        raise RuntimeError("LINUX_DOCKER_REQUIRED")
    engine = output("info", "--format", "{{.ID}}")
    requirements_sha = sha(DEPLOY / "requirements.lock")
    builder = "tv-idp098-build-" + uuid.uuid4().hex[:10]
    created = False
    helper = None
    try:
        if args.software_archive:
            archive = args.software_archive.resolve()
            if not re.fullmatch(r"[0-9a-f]{64}", args.software_sha256) or sha(archive) != args.software_sha256:
                raise ValueError("SOFTWARE_ARCHIVE_INTEGRITY")
            provenance = "explicit_verified_own_archive"
        else:
            software = cache / "software"
            software.mkdir(exist_ok=True)
            wheel = software / WHEEL
            if not wheel.exists() or sha(wheel) != WHEEL_SHA:
                temporary = wheel.with_suffix(".download")
                with urllib.request.urlopen(WHEEL_URL, timeout=120) as response, temporary.open("wb") as target:
                    shutil.copyfileobj(response, target, 1024 * 1024)
                if sha(temporary) != WHEEL_SHA:
                    temporary.unlink()
                    raise ValueError("OFFICIAL_WHEEL_INTEGRITY")
                temporary.replace(wheel)
            for source, name in ((DEPLOY / "requirements.lock", "requirements.lock"),
                                 (DEPLOY / "Dockerfile.software", "Dockerfile")):
                shutil.copyfile(source, software / name)
            run(["docker", "buildx", "create", "--name", builder, "--driver", "docker-container",
                 "--driver-opt", "image=" + BUILDKIT, "--driver-opt", "memory=3g",
                 "--driver-opt", "cpu-period=100000", "--driver-opt", "cpu-quota=200000"], log)
            created = True
            archive = cache / "software.tar"
            run(["docker", "buildx", "build", "--builder", builder, "--platform", "linux/amd64",
                 "--output", "type=docker,dest=" + str(archive), "--metadata-file", str(cache / "software-build.json"),
                 "--label", "io.trackvance.owner=" + OWNER, str(software)], log)
            provenance = "pinned_dockerfile_and_hashed_wheels"
        manifest = software_manifest(archive, requirements_sha)
        gateway = cache / "gateway"
        app = gateway / "app"
        app.mkdir(parents=True, exist_ok=True)
        hashes = {}
        for name in SOURCES:
            shutil.copyfile(source_path(name), app / name)
            hashes[name] = sha(app / name)
        (app / "runtime-layers.lock.json").write_text(json.dumps(manifest, indent=2))
        shutil.copyfile(DEPLOY / "Dockerfile.gateway", gateway / "Dockerfile")
        source_sha = hashlib.sha256(json.dumps(hashes, sort_keys=True).encode()).hexdigest()
        tag = OWNER + ":gateway-" + source_sha[:12]
        if not created:
            run(["docker", "buildx", "create", "--name", builder, "--driver", "docker-container",
                 "--driver-opt", "image=" + BUILDKIT, "--driver-opt", "memory=3g",
                 "--driver-opt", "cpu-period=100000", "--driver-opt", "cpu-quota=200000"], log)
            created = True
        run(["docker", "buildx", "build", "--builder", builder, "--platform", "linux/amd64", "--load",
             "--tag", tag, "--label", "io.trackvance.owner=" + OWNER,
             "--label", "io.trackvance.environment=idp-private",
             "--label", "io.trackvance.source-sha256=" + source_sha,
             "--label", "org.opencontainers.image.version=0.9.8", str(gateway)], log)
        run(["docker", "buildx", "stop", builder], log)
        image = json.loads(output("image", "inspect", tag))[0]
        volume = args.runtime_volume or OWNER + "-runtime-" + hashlib.sha256("".join(manifest["layers"]).encode()).hexdigest()[:12]
        if not re.fullmatch(r"(?:tv-idp098|trackvance-idp098)[a-z0-9_-]{1,100}", volume):
            raise ValueError("PRIVATE_RUNTIME_VOLUME_NAME_REQUIRED")
        known = output("volume", "ls", "--format", "{{.Name}}").splitlines()
        if volume in known:
            labels = json.loads(output("volume", "inspect", volume))[0].get("Labels") or {}
            if labels.get("io.trackvance.owner") != OWNER or labels.get("io.trackvance.environment") != "idp-private":
                raise ValueError("EXISTING_VOLUME_OWNER_MISMATCH")
        else:
            run(["docker", "volume", "create", "--label", "io.trackvance.owner=" + OWNER,
                 "--label", "io.trackvance.environment=idp-private", volume], log)
        helper = OWNER + "-init-" + uuid.uuid4().hex[:10]
        base = ["docker", "run", "--rm", "--name", helper, "--label", "io.trackvance.owner=" + OWNER,
                "--label", "io.trackvance.environment=idp-private", "--user", "65532:65532", "--cap-drop", "ALL",
                "--security-opt", "no-new-privileges:true", "--read-only", "--network", "none"]
        run([*base, "--memory", "512m", "--memory-swap", "512m", "--cpus", "1", "--pids-limit", "64",
             "--mount", f"type=volume,source={volume},target=/opt/venv",
             "--mount", f"type=bind,source={archive},target=/software/runtime.tar,readonly",
             "--tmpfs", "/tmp:size=64m,mode=1777", "--entrypoint", "python", image["Id"],
             "-I", "-B", "/app/runtime_init.py"], log)
        receipt = {"image_id": image["Id"], "image_labels": image["Config"]["Labels"], "docker_engine_id": engine,
                   "runtime_volume": volume, "source_sha256": hashes, "source_manifest_sha256": source_sha,
                   "software_archive_sha256": sha(archive), "software_archive_provenance": provenance, **manifest}
        (cache / "runtime-receipt.json").write_text(json.dumps(receipt, indent=2))
        env = f'IDP_PADDLE_IMAGE={image["Id"]}\nIDP_RUNTIME_VOLUME={volume}\nIDP_MODEL_ROOT={json.dumps(models.as_posix())}\n'
        (cache / "runtime.env").write_text(env)
        print(json.dumps(receipt, indent=2))
    finally:
        if helper:
            inspection = subprocess.run(["docker", "inspect", helper], capture_output=True, text=True, timeout=30, check=False)
            if inspection.returncode == 0:
                labels = json.loads(inspection.stdout)[0]["Config"].get("Labels") or {}
                if labels.get("io.trackvance.owner") == OWNER and labels.get("io.trackvance.environment") == "idp-private":
                    subprocess.run(["docker", "rm", "--force", helper], check=False, timeout=30,
                                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        if created:
            subprocess.run(["docker", "buildx", "rm", builder], check=False, timeout=60,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cache", type=Path, required=True)
    parser.add_argument("--models", type=Path, required=True)
    parser.add_argument("--software-archive", type=Path)
    parser.add_argument("--software-sha256")
    parser.add_argument("--runtime-volume")
    prepare(parser.parse_args())
