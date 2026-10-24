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
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEPLOY = ROOT / "deploy/idp"
OWNER = "trackvance-idp098"
WHEEL = "paddlepaddle_gpu-3.2.1-cp312-cp312-linux_x86_64.whl"
WHEEL_URL = "https://paddle-whl.cdn.bcebos.com/stable/cu129/paddlepaddle-gpu/" + WHEEL
WHEEL_SHA = "01924dc83214a041bcc78de01c6be67ffe42e88909c7f4b6d13c31b363851b0a"
BUILDKIT = "moby/buildkit@sha256:cec9f139f45e93c5c69c60f8b07cfad9f43f4ef6b6a6cd917527fea5ff2e3dea"
SOURCES = ("server.py", "paddle_runner.py", "runtime_probe.py", "runtime_init.py",
           "pipeline.yml", "prompt-v1.txt", "prompt-v2.txt", "document_semantic_fragments.py",
           "document_literal_tables.py", "document_template_builder_rules.py", "model_snapshot.py", "models.lock.json")
BACKEND_SOURCES = {"document_semantic_fragments.py", "document_literal_tables.py", "document_template_builder_rules.py"}


def source_path(name):
    if name in BACKEND_SOURCES:
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


def run(args, log, *, reserve_gib=3, timeout=2400):
    require_reserve(reserve_gib)
    minimum = host_available()
    print(json.dumps({"phase": args[1:3]}), flush=True)
    with log.open("ab") as stream:
        process = subprocess.Popen(args, stdout=stream, stderr=subprocess.STDOUT)
        started = time.monotonic()
        try:
            while process.poll() is None:
                require_reserve(reserve_gib)
                available = host_available()
                if available is not None:
                    minimum = min(minimum, available) if minimum is not None else available
                if time.monotonic() - started > timeout:
                    raise TimeoutError("BOUNDED_RUNTIME_PREPARATION_TIMEOUT")
                time.sleep(1)
            if process.returncode:
                raise RuntimeError("RUNTIME_COMMAND_FAILED; inspect private preparation.log")
            return {"elapsed_seconds": round(time.monotonic() - started, 4), "minimum_host_available_bytes": minimum}
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


def snapshot_overlay(volume):
    def mount(target, subpath):
        return {"type": "volume", "source": "verified-model-snapshot", "target": target,
                "read_only": True, "volume": {"subpath": subpath, "nocopy": True}}
    return {"services": {
        "idp-gateway": {"volumes": [mount("/models/paddle", "paddle"), mount("/models/layout", "layout")]},
        "idp-ollama": {"volumes": [mount("/models", "ollama")]}},
        "volumes": {"verified-model-snapshot": {"external": True, "name": volume}}}


def private_labels(labels):
    return (isinstance(labels, dict) and labels.get("io.trackvance.owner") == OWNER
            and labels.get("io.trackvance.environment") == "idp-private" and "io.trackvance.permanent" not in labels)


def volume_identity(volume):
    return {key: volume.get(key) for key in ("Name", "CreatedAt", "Driver", "Labels", "Options", "Scope")}


def verify_new_snapshot_volume(volume, name, labels):
    try:
        created = datetime.fromisoformat(volume.get("CreatedAt", ""))
    except (TypeError, ValueError, AttributeError):
        raise ValueError("MODEL_SNAPSHOT_VOLUME_IDENTITY") from None
    if (volume.get("Name") != name or volume.get("Labels") != labels or not private_labels(labels)
            or volume.get("Driver") != "local" or volume.get("Scope") != "local"
            or volume.get("Options") not in (None, {}) or created.tzinfo is None):
        raise ValueError("MODEL_SNAPSHOT_VOLUME_IDENTITY")


def verify_snapshot_volume(initial, current):
    if not private_labels(current.get("Labels")) or volume_identity(initial) != volume_identity(current):
        raise ValueError("MODEL_SNAPSHOT_VOLUME_REPLACED")


def runtime_initializer_owned(saved, helper, name, image):
    labels = saved.get("Config", {}).get("Labels")
    return (saved.get("Id") == helper and saved.get("Name") == "/" + name and saved.get("Image") == image
            and private_labels(labels) and labels.get("io.trackvance.initializer-id") == name)


def initialize_runtime(image, volume, archive, log):
    """Only a successfully created immutable container ID is eligible for cleanup."""
    name = OWNER + "-init-" + uuid.uuid4().hex[:10]
    helper = output("create", "--name", name, "--label", "io.trackvance.owner=" + OWNER,
        "--label", "io.trackvance.environment=idp-private", "--label", "io.trackvance.initializer-id=" + name,
        "--user", "65532:65532", "--cap-drop", "ALL", "--security-opt", "no-new-privileges:true",
        "--read-only", "--network", "none", "--memory", "512m", "--memory-swap", "512m",
        "--cpus", "1", "--pids-limit", "64", "--mount", f"type=volume,source={volume},target=/opt/venv",
        "--mount", f"type=bind,source={archive},target=/software/runtime.tar,readonly",
        "--tmpfs", "/tmp:size=64m,mode=1777", "--entrypoint", "/usr/local/bin/python", image,
        "-I", "-B", "/app/runtime_init.py")
    if not re.fullmatch(r"[0-9a-f]{64}", helper):
        raise ValueError("PRIVATE_RUNTIME_INITIALIZER_CID_INVALID")
    try:
        saved = json.loads(output("inspect", helper))[0]
        if not runtime_initializer_owned(saved, helper, name, image):
            raise ValueError("PRIVATE_RUNTIME_INITIALIZER_IDENTITY")
        run(["docker", "start", "--attach", helper], log)
    finally:
        inspection = subprocess.run(["docker", "inspect", helper], capture_output=True, text=True, timeout=30, check=False)
        if inspection.returncode == 0:
            saved = json.loads(inspection.stdout)[0]
            if runtime_initializer_owned(saved, helper, name, image):
                subprocess.run(["docker", "rm", "--force", helper], check=False, timeout=30,
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def prepare_model_snapshot(cache, models, image, log):
    """Always fresh. A failed own volume remains labeled and never deployed."""
    require_reserve(3.25)
    if shutil.disk_usage(cache).free < 13 * 1024**3:
        raise ValueError("MODEL_SNAPSHOT_HOST_DISK_RESERVE")
    identity = uuid.uuid4().hex
    volume = OWNER + "-models-" + identity[:12]
    if volume in output("volume", "ls", "--format", "{{.Name}}").splitlines():
        raise ValueError("MODEL_SNAPSHOT_DESTINATION_EXISTS")
    labels = {"io.trackvance.owner": OWNER, "io.trackvance.environment": "idp-private",
        "io.trackvance.purpose": "verified-readonly-model-snapshot",
        "io.trackvance.retention": "private-task-reproducibility", "io.trackvance.snapshot-id": identity}
    label_arguments = [argument for key, value in labels.items() for argument in ("--label", key + "=" + value)]
    output("volume", "create", *label_arguments, volume)
    inspected_volume = json.loads(output("volume", "inspect", volume))[0]
    verify_new_snapshot_volume(inspected_volume, volume, labels)
    if output("ps", "-a", "--filter", "volume=" + volume, "--format", "{{.ID}}"):
        raise ValueError("MODEL_SNAPSHOT_VOLUME_CONSUMER")
    helper = None
    offset = log.stat().st_size if log.exists() else 0
    try:
        lock_sha = sha(cache / "gateway/app/models.lock.json")
        helper = output("create", "--name", OWNER + "-model-copy-" + identity[:12], *label_arguments,
            "--memory", "512m", "--memory-swap", "512m", "--cpus", "1", "--pids-limit", "64",
            "--network", "none", "--read-only", "--user", "65532:65532", "--cap-drop", "ALL",
            "--security-opt", "no-new-privileges:true", "--tmpfs", "/tmp:size=16m,mode=1777",
            "--mount", f"type=volume,source={volume},target=/opt/venv",
            "--mount", f"type=bind,source={models.as_posix()},target=/source-models,readonly",
            "--entrypoint", "/usr/local/bin/python", image, "-I", "-B", "/app/model_snapshot.py",
            "--snapshot-id", identity, "--volume-name", volume, "--image-id", image, "--lock-sha256", lock_sha)
        inspected = json.loads(output("inspect", helper))[0]
        if (not private_labels(inspected["Config"].get("Labels")) or inspected["Image"] != image
                or inspected["Config"]["Labels"].get("io.trackvance.snapshot-id") != identity
                or inspected["HostConfig"]["NetworkMode"] != "none" or inspected["HostConfig"]["DeviceRequests"]):
            raise ValueError("MODEL_SNAPSHOT_HELPER_IDENTITY")
        measured = run(["docker", "start", "--attach", helper], log, reserve_gib=3.25, timeout=930)
        with log.open("rb") as stream:
            stream.seek(offset)
            raw = stream.read(1024 * 1024 + 1)
        if len(raw) > 1024 * 1024:
            raise ValueError("MODEL_SNAPSHOT_LOG_LIMIT")
        lines = [json.loads(line) for line in raw.decode().splitlines() if line.startswith('{"phase": "MODEL_SNAPSHOT_COMPLETE"')]
        if len(lines) != 1:
            raise ValueError("MODEL_SNAPSHOT_RESULT_MISSING")
        result = lines[0]
        if (result.get("status") != "COMPLETE" or result.get("identity") != {
                "snapshot_id": identity, "volume_name": volume, "image_id": image, "model_lock_sha256": lock_sha}):
            raise ValueError("MODEL_SNAPSHOT_RESULT_IDENTITY")
        result.pop("phase")
        claimed = result.pop("manifest_sha256")
        actual = hashlib.sha256(json.dumps(result, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()).hexdigest()
        if claimed != actual:
            raise ValueError("MODEL_SNAPSHOT_RESULT_INTEGRITY")
        result["manifest_sha256"] = claimed
        proof = {"volume": inspected_volume, "helper_id": helper, "helper_image": image, "result": result, "resources": measured}
        (cache / "model-snapshot-receipt.json").write_text(json.dumps(proof, indent=2))
    finally:
        if helper:
            saved = json.loads(output("inspect", helper))[0]
            if (private_labels(saved["Config"].get("Labels")) and saved["Image"] == image
                    and saved["Config"]["Labels"].get("io.trackvance.snapshot-id") == identity):
                output("rm", "--force", helper)
    if output("ps", "-a", "--filter", "volume=" + volume, "--format", "{{.ID}}"):
        raise ValueError("MODEL_SNAPSHOT_WRITER_REMAINS")
    verify_snapshot_volume(inspected_volume, json.loads(output("volume", "inspect", volume))[0])
    overlay = cache / "models.ext4.override.json"
    overlay.write_text(json.dumps(snapshot_overlay(volume), indent=2))
    return {"volume": volume, "manifest_sha256": claimed, "receipt": "model-snapshot-receipt.json", "overlay": overlay.name}


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
        if not private_labels(image["Config"].get("Labels")):
            raise ValueError("PRIVATE_RUNTIME_IMAGE_IDENTITY")
        volume = args.runtime_volume or OWNER + "-runtime-" + hashlib.sha256("".join(manifest["layers"]).encode()).hexdigest()[:12]
        if not re.fullmatch(r"(?:tv-idp098|trackvance-idp098)[a-z0-9_-]{1,100}", volume):
            raise ValueError("PRIVATE_RUNTIME_VOLUME_NAME_REQUIRED")
        known = output("volume", "ls", "--format", "{{.Name}}").splitlines()
        if volume in known:
            labels = json.loads(output("volume", "inspect", volume))[0].get("Labels") or {}
            if not private_labels(labels):
                raise ValueError("EXISTING_VOLUME_OWNER_MISMATCH")
        else:
            run(["docker", "volume", "create", "--label", "io.trackvance.owner=" + OWNER,
                 "--label", "io.trackvance.environment=idp-private", volume], log)
        initialize_runtime(image["Id"], volume, archive, log)
        receipt = {"image_id": image["Id"], "image_labels": image["Config"]["Labels"], "docker_engine_id": engine,
                   "runtime_volume": volume, "source_sha256": hashes, "source_manifest_sha256": source_sha,
                   "software_archive_sha256": sha(archive), "software_archive_provenance": provenance, **manifest}
        if getattr(args, "snapshot_models", False):
            receipt["model_snapshot"] = prepare_model_snapshot(cache, models, image["Id"], log)
        (cache / "runtime-receipt.json").write_text(json.dumps(receipt, indent=2))
        env = f'IDP_PADDLE_IMAGE={image["Id"]}\nIDP_RUNTIME_VOLUME={volume}\nIDP_MODEL_ROOT={json.dumps(models.as_posix())}\n'
        (cache / "runtime.env").write_text(env)
        print(json.dumps(receipt, indent=2))
    finally:
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
    parser.add_argument("--snapshot-models", action="store_true", help="Copy verified models into a fresh private ext4 volume and emit a read-only overlay.")
    prepare(parser.parse_args())
