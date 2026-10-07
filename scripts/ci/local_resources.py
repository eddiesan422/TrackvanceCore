"""Explicit local runner ownership and an aggregate per-stack resource budget."""
from __future__ import annotations

import hashlib
import io
import json
import os
import re
import subprocess
import time
import tomllib
import zipfile
from pathlib import Path, PurePosixPath

IMAGE_LABELS = ("io.trackvance.local-proof", "io.trackvance.local-execution",
    "io.trackvance.local-project", "io.trackvance.local-role",
    "org.opencontainers.image.revision", "org.opencontainers.image.version")
HISTORICAL_SOURCES = {
    "0.5.1": "4519ed354202ea8f220682758da234e07b6df3ed",
    "0.6.0": "587909bc4462683e87e403dd2ea29a1d6d4afe08",
    "0.6.1": "6fac26b3648cb4a4b50c094ef12c1e103bc97ddd",
}


def canonical_hash(value) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def historical_source_proof(repository: Path, context: Path, source_sha: str, version: str) -> dict:
    """Verify the complete extracted Git tree before any historical build mutation."""
    if HISTORICAL_SOURCES.get(version) != source_sha:
        raise ValueError("Historical version does not match its authentic fixed commit")

    def git(*arguments):
        return subprocess.run(["git", "-C", str(repository), *arguments], check=True,
            capture_output=True, text=True, encoding="utf-8", timeout=90).stdout

    if Path(git("rev-parse", "--show-toplevel").strip()).resolve() != repository.resolve():
        raise ValueError("Historical source repository root is ambiguous")
    remote = git("remote", "get-url", "origin").strip()
    if not re.fullmatch(r"(?:https://github\.com/|git@github\.com:)eddiesan422/TrackvanceCore(?:\.git)?", remote):
        raise ValueError("Historical source is not from the expected repository")
    tree = git("rev-parse", source_sha + "^{tree}").strip()
    archive_bytes = subprocess.run(["git", "-C", str(repository), "archive", "--format=zip", source_sha],
        check=True, capture_output=True, timeout=90).stdout
    archive = zipfile.ZipFile(io.BytesIO(archive_bytes))
    rows, expected = [], set()
    for entry in git("ls-tree", "-rz", source_sha).split("\0"):
        if not entry:
            continue
        metadata, name = entry.split("\t", 1)
        mode, kind, blob = metadata.split()
        relative = PurePosixPath(name)
        if mode not in {"100644", "100755"} or kind != "blob" or relative.is_absolute() or ".." in relative.parts:
            raise ValueError("Historical build tree contains an unsupported mode or path")
        target = context.joinpath(*relative.parts)
        if target.is_symlink() or not target.is_file() or not target.resolve().is_relative_to(context.resolve()):
            raise ValueError("Historical build tree contains a missing or linked source")
        payload = target.read_bytes()
        # Git archive can apply the repository's checkout newline settings on
        # Windows. Compare exact authenticated archive bytes, never a permissive
        # text normalization; retain original canonical blob identities as well.
        if payload != archive.read(name):
            raise ValueError("Historical extracted source differs from its committed blob")
        expected.add(name)
        rows.append({"path": name, "mode": mode, "blob": blob,
            "archive_bytes_sha256": hashlib.sha256(payload).hexdigest()})
    actual = {file.relative_to(context).as_posix() for file in context.rglob("*") if file.is_file() or file.is_symlink()}
    if actual != expected:
        raise ValueError("Historical context has uncommitted or missing build input paths")
    if tomllib.loads((context / "backend/pyproject.toml").read_text(encoding="utf-8"))["project"]["version"] != version:
        raise ValueError("Historical runtime metadata differs from its authentic version")
    dockerfiles = {row["path"]: row["blob"] for row in rows
        if row["path"] in {"backend/Dockerfile", "deploy/docker/frontend.Dockerfile", "compose.yml"}}
    if len(dockerfiles) != 3:
        raise ValueError("Historical build definitions are incomplete")
    return {"repository": "eddiesan422/TrackvanceCore", "source_sha": source_sha,
        "source_version": version, "tree_sha": tree, "tracked_files": len(rows),
        "verified_git_archive_sha256": hashlib.sha256(archive_bytes).hexdigest(),
        "tracked_blobs_sha256": canonical_hash(rows), "build_definition_blobs": dockerfiles}


def docker_output(*arguments) -> str:
    return subprocess.run(["docker", *arguments], check=True, capture_output=True,
        text=True, encoding="utf-8", timeout=90).stdout


def save_image_registry(path: Path, value: dict) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


def image_identity(row: dict) -> dict:
    identifier = row["Id"]
    if not re.fullmatch(r"sha256:[a-f0-9]{64}", identifier):
        raise ValueError("Image ownership requires an immutable Docker ID")
    labels = row.get("Config", {}).get("Labels") or {}
    return {"image_id": identifier, "tags": sorted(row.get("RepoTags") or []),
        "digests": sorted(row.get("RepoDigests") or []),
        "labels": {key: labels[key] for key in IMAGE_LABELS if key in labels},
        "config_sha256": canonical_hash(row.get("Config")),
        "rootfs_sha256": canonical_hash(row.get("RootFS")),
        "size_bytes": row.get("Size"), "created": row.get("Created")}


def image_consumers(command=docker_output) -> list[dict]:
    identifiers = command("ps", "-aq", "--no-trunc").split()
    rows = json.loads(command("inspect", *identifiers)) if identifiers else []
    return [{"container_id": row["Id"], "image_id": row["Image"],
        "image_reference": row.get("Config", {}).get("Image"),
        "project": (row.get("Config", {}).get("Labels") or {}).get("com.docker.compose.project"),
        "status": row["State"]["Status"]} for row in rows]


def initialize_image_registry(path: Path, execution: str, command=docker_output) -> dict:
    if not re.fullmatch(r"local-[a-f0-9]{32}", execution):
        raise ValueError("Image ownership requires a local execution UUID")
    if path.exists():
        value = json.loads(path.read_text(encoding="utf-8"))
        if value.get("schema_version") != 1 or value.get("execution_id") != execution:
            raise ValueError("Image registry belongs to another execution")
        return value
    identifiers = list(dict.fromkeys(command("image", "ls", "-aq", "--no-trunc").split()))
    rows = json.loads(command("image", "inspect", *identifiers)) if identifiers else []
    value = {"schema_version": 1, "execution_id": execution,
        "baseline_images": [image_identity(row) for row in rows],
        "baseline_consumers": image_consumers(command), "builds": []}
    save_image_registry(path, value)
    return value


def inspect_reference(reference: str, command=docker_output) -> dict | None:
    identifiers = list(dict.fromkeys(command("image", "ls", "-q", "--no-trunc", reference).split()))
    rows = json.loads(command("image", "inspect", *identifiers)) if identifiers else []
    matching = [image_identity(row) for row in rows if reference in (row.get("RepoTags") or [])]
    if len(matching) > 1:
        raise ValueError("Image reference has ambiguous immutable ownership")
    return matching[0] if matching else None


def inspect_owned_identity(record: dict, command=docker_output) -> dict | None:
    identifiers = list(dict.fromkeys(command("image", "ls", "-aq", "--no-trunc").split()))
    if record["image"]:
        identifier = record["image"]["image_id"]
        return image_identity(json.loads(command("image", "inspect", identifier))[0]) if identifier in identifiers else None
    rows = json.loads(command("image", "inspect", *identifiers)) if identifiers else []
    matching = [image_identity(row) for row in rows
        if image_identity(row)["labels"] == record["expected_labels"]]
    if len(matching) > 1:
        raise ValueError("More than one immutable ID matches a single durable build intent")
    return matching[0] if matching else None


def begin_image_build(path: Path, execution: str, project: str, source_sha: str,
                      version: str, role: str, reference: str, command=docker_output, *, source_proof=None) -> dict:
    value = initialize_image_registry(path, execution, command)
    if (not re.fullmatch(r"trackvance-[a-z0-9-]+-[a-f0-9]{12}", project)
            or not re.fullmatch(r"[a-f0-9]{40}", source_sha)
            or version not in {"0.5.1", "0.6.0", "0.6.1", "0.8.0"}
            or role not in {"backend", "web", "backend-tests"}):
        raise ValueError("Image intent needs authentic source/version and explicit project ownership")
    if not (reference == f"{project}:{role}" or re.fullmatch(
            r"trackvance-local-proof:[a-f0-9]{12}-[a-f0-9]{12}-(?:backend|web|backend-tests)", reference)):
        raise ValueError("Image intent reference is outside the disposable build")
    if any(reference == record["reference"] for record in value["builds"]):
        raise ValueError("Image reference was already claimed by this execution")
    if inspect_reference(reference, command) is not None:
        raise ValueError("Refusing to overwrite a preexisting image reference")
    labels = dict(zip(IMAGE_LABELS, ("true", execution, project, role, source_sha, version), strict=True))
    record = {"execution_id": execution, "project": project, "source_sha": source_sha,
        "version": version, "role": role, "reference": reference, "expected_labels": labels,
        "status": "BUILD_PENDING", "image": None}
    if source_proof is not None:
        record["source_proof_sha256"] = canonical_hash(source_proof)
        record["source_tree_sha"] = source_proof["tree_sha"]
    value["builds"].append(record)
    save_image_registry(path, value)  # Durable before any build/load can partly succeed.
    return labels


def observe_image_builds(path: Path, execution: str, command=docker_output) -> dict:
    value = initialize_image_registry(path, execution, command)
    baseline_ids = {row["image_id"] for row in value["baseline_images"]}
    for record in value["builds"]:
        if record["status"] == "REMOVED" or "observed_change" in record:
            continue
        image = inspect_reference(record["reference"], command) or inspect_owned_identity(record, command)
        if image is None:
            record["status"] = "NOT_LOADED" if record["image"] is None else "ALREADY_ABSENT"
            continue
        if record["image"] and record["image"]["image_id"] != image["image_id"]:
            record["status"] = "REFERENCE_REASSIGNED"
            record["observed_change"] = image
            continue
        if record["image"] is not None:
            original = record["image"]
            # Losing the single owned tag is recoverable by immutable ID. New
            # references or any Config/RootFS change are never adopted as ours.
            without_tags = {key: item for key, item in image.items() if key != "tags"}
            original_without_tags = {key: item for key, item in original.items() if key != "tags"}
            if without_tags != original_without_tags or not set(image["tags"]) <= set(original["tags"]):
                record["status"] = "IMAGE_IDENTITY_OR_REFERENCES_CHANGED"
                record["observed_change"] = image
                continue
        else:
            record["image"] = image  # First load is the immutable ownership boundary.
        record["status"] = ("PREEXISTING_IMAGE" if image["image_id"] in baseline_ids else
            "OWNED" if image["labels"] == record["expected_labels"] else "OWNERSHIP_UNVERIFIED")
    save_image_registry(path, value)
    return value


def cleanup_registered_images(path: Path, execution: str, *, projects=None,
                              retain_for_seconds=0, command=docker_output) -> dict:
    if not 0 <= retain_for_seconds <= 86400:
        raise ValueError("Image retention must be explicitly bounded")
    value = observe_image_builds(path, execution, command)
    baseline_ids = {row["image_id"] for row in value["baseline_images"]}
    baseline_references = {ref for row in value["baseline_images"] for ref in (*row["tags"], *row["digests"])}
    removed, skipped = [], []
    for record in value["builds"]:
        if projects is not None and record["project"] not in projects:
            continue
        image = record["image"]
        reason = None
        if record["status"] in {"REMOVED", "NOT_LOADED", "ALREADY_ABSENT"}:
            continue
        if record["status"] != "OWNED" or image["image_id"] in baseline_ids:
            reason = record["status"]
        elif set(image["tags"]) - {record["reference"]} or image["digests"] or set(image["tags"] + image["digests"]) & baseline_references:
            reason = "SHARED_OR_PREEXISTING_REFERENCES"
        elif retain_for_seconds:
            reason = "EXPLICIT_TEMPORARY_REUSE"
        if reason:
            skipped.append({"image_id": image["image_id"] if image else None, "reference": record["reference"],
                "reason": reason, "size_bytes": image["size_bytes"] if image else None,
                **({"purpose": "Explicit bounded verified fixture reuse", "expires_at_unix": int(time.time()) + retain_for_seconds} if retain_for_seconds else {})})
            continue
        consumers = [row for row in image_consumers(command) if row["image_id"] == image["image_id"]
            or row["image_reference"] in image["tags"] + image["digests"]]
        if consumers:
            skipped.append({"image_id": image["image_id"], "reference": record["reference"],
                "reason": "CONTAINER_CONSUMER", "consumers": consumers})
            continue
        current = inspect_owned_identity(record, command)
        if current is None or any(current[key] != image[key] for key in image if key != "tags") or not set(current["tags"]) <= set(image["tags"]):
            record["status"] = "IMAGE_IDENTITY_OR_REFERENCES_CHANGED"
            record["observed_change"] = current
            save_image_registry(path, value)
            skipped.append({"image_id": image["image_id"], "reference": record["reference"], "reason": "IMAGE_IDENTITY_OR_REFERENCES_CHANGED"})
            continue
        # One recorded reference only. Full ID deletion is non-forced and cannot
        # unlink a ref that was concurrently reassigned to a different image.
        command("image", "rm", image["image_id"])
        if inspect_reference(record["reference"], command) is not None:
            raise ValueError("Owned image reference survived exact-ID cleanup")
        if image["image_id"] in command("image", "ls", "-aq", "--no-trunc").split():
            raise ValueError("Owned immutable image survived cleanup without its tag")
        record["status"] = "REMOVED"
        removed.append({"image_id": image["image_id"], "reference": record["reference"],
            "project": record["project"], "execution_id": execution,
            "source_sha": record["source_sha"], "version": record["version"]})
        save_image_registry(path, value)
    return {"status": "PASS", "removed": removed, "skipped": skipped,
        "retain_for_seconds": retain_for_seconds, "registry": str(path),
        "policy": "Exact recorded IDs; preserve all preexisting, shared, consumed or unverified images; no force/prune"}


def register_project(project: str, environment=None) -> None:
    registry = (environment or os.environ).get("TRACKVANCE_LOCAL_PROJECT_REGISTRY")
    if not registry:
        return
    if not re.fullmatch(r"trackvance-[a-z0-9-]+-[a-f0-9]{12}", project):
        raise ValueError("Local ownership requires a fresh Trackvance UUID project")
    path = Path(registry)
    projects = json.loads(path.read_text(encoding="utf-8")) if path.is_file() else []
    if project not in projects:
        projects.append(project)
        path.write_text(json.dumps(projects, indent=2) + "\n", encoding="utf-8")


def memory_bytes(value):
    if isinstance(value, int):
        return value
    match = re.fullmatch(r"([0-9.]+)([kmg]?)b?", str(value).lower())
    if not match:
        raise ValueError("Unrecognized explicit service memory budget")
    return int(float(match[1]) * 1024 ** {"": 0, "k": 1, "m": 2, "g": 3}[match[2]])


def service_closure(services: dict, requested) -> set[str]:
    pending, closure = list(requested), set()
    while pending:
        name = pending.pop()
        if name in closure:
            continue
        if name not in services:
            raise ValueError("Selected local service/dependency is absent from resolved Compose")
        closure.add(name)
        pending.extend(services[name].get("depends_on", {}))
    return closure


def apply_limits(override: dict, project: str, *, active_services=None) -> None:
    """Serial runner caps all services in the actual startup dependency closure.

    Every resolved connector database is included before applying this cap.
    When a caller selects a subset, its generated profile disables the rest.
    Limits affect only generated test Compose.
    """
    if not os.environ.get("TRACKVANCE_LOCAL_EXECUTION_ID"):
        return
    register_project(project)
    services = override["services"]
    if active_services is not None:
        if not set(active_services) <= services.keys():
            raise ValueError("The selected resource topology must be complete")
        services = {name: service for name, service in services.items() if name in active_services}
    cpu_budget = float(os.environ["TRACKVANCE_LOCAL_MAX_CPUS"])
    memory_budget = int(os.environ["TRACKVANCE_LOCAL_MAX_MEMORY_BYTES"])
    if os.environ.get("TRACKVANCE_LOCAL_GROUP") == "backup-restore":
        # Native restore owns source+target stacks and a separate 512 MiB DB.
        memory_budget = max(1024**3, (memory_budget - 512 * 1024**2) // 2)
        cpu_budget = max(0.25, (cpu_budget - 0.5) / 2)
    cpu_total = sum(float(s.get("cpus", 1)) for s in services.values())
    memory_total = sum(memory_bytes(s.get("mem_limit", "256m")) for s in services.values())
    for service in services.values():
        service["cpus"] = round(float(service.get("cpus", 1)) * min(1, cpu_budget / cpu_total), 4)
        service["mem_limit"] = int(memory_bytes(service.get("mem_limit", "256m")) * min(1, memory_budget / memory_total))
        service.setdefault("labels", {})["io.trackvance.local-execution"] = os.environ["TRACKVANCE_LOCAL_EXECUTION_ID"]


def build_legacy(compose: list[str], directory: Path, project: str, environment: dict[str, str]) -> None:
    """Cold historical images use a bounded builder in an owned local run."""
    from ci.run_suite import execute
    execution = environment["TRACKVANCE_LOCAL_EXECUTION_ID"]
    image_registry = Path(environment["TRACKVANCE_LOCAL_IMAGE_REGISTRY"])
    source_sha = environment["TRACKVANCE_LOCAL_HISTORICAL_SOURCE_SHA"]
    source_version = environment["TRACKVANCE_LOCAL_HISTORICAL_SOURCE_VERSION"]
    if source_version not in {"0.5.1", "0.6.0", "0.6.1"}:
        raise ValueError("Historical builds cannot claim the current product version")
    source_context = directory / "baseline"
    first_compose_file = Path(compose[compose.index("-f") + 1]).resolve()
    if first_compose_file != (source_context / "compose.yml").resolve():
        raise ValueError("Historical Compose root must use the verified private archive")
    proof = historical_source_proof(Path(__file__).resolve().parents[2], source_context, source_sha, source_version)
    resolved = json.loads(subprocess.run([*compose, "config", "--format", "json"], cwd=source_context,
        env=environment, check=True, capture_output=True, text=True, encoding="utf-8", timeout=90).stdout)
    for service, role, dockerfile in (("api", "backend", "backend/Dockerfile"),
                                      ("web", "web", "deploy/docker/frontend.Dockerfile")):
        service_config = resolved["services"][service]
        build_config = service_config["build"]
        if (Path(build_config["context"]).resolve() != source_context.resolve()
                or build_config["dockerfile"] != dockerfile
                or service_config["image"] != f"{project}:{role}"):
            raise ValueError("Historical resolved build escaped its verified source or owned image reference")
    save_image_registry(directory / "local-historical-source-proof.json", proof)
    owner_override = {"services": {}}
    for service, role in (("api", "backend"), ("web", "web")):
        labels = begin_image_build(image_registry, execution, project, source_sha,
            source_version, role, f"{project}:{role}", source_proof=proof)
        owner_override["services"][service] = {"build": {"labels": labels}}
    ownership_file = directory / "local-legacy-image-ownership.json"
    ownership_file.write_text(json.dumps(owner_override), encoding="utf-8")
    name = "tv-local-legacy-" + execution[-12:] + project[-12:]
    registry = Path(os.environ["TRACKVANCE_LOCAL_BUILDER_REGISTRY"])
    names = json.loads(registry.read_text(encoding="utf-8")) if registry.exists() else []
    names.append(name)
    registry.write_text(json.dumps(names) + "\n", encoding="utf-8")
    config = directory / "local-buildkit.toml"
    config.write_text('[worker.oci]\n max-parallelism = 1\n', encoding="utf-8")
    memory = min(3 * 1024**3, int(os.environ["TRACKVANCE_LOCAL_MAX_MEMORY_BYTES"]) // 2)
    quota = int(min(2, float(os.environ["TRACKVANCE_LOCAL_MAX_CPUS"]) / 2) * 100000)
    try:
        execute(["docker", "buildx", "create", "--name", name, "--driver", "docker-container",
            "--buildkitd-config", str(config), "--driver-opt", "memory=" + str(memory),
            "--driver-opt", "memory-swap=" + str(memory), "--driver-opt", "cpu-quota=" + str(quota),
            "--driver-opt", "cpu-period=100000", "--driver-opt", "restart-policy=no"], directory,
            "local-builder-create", 180, environment=environment)
        execute([*compose, "-f", str(ownership_file), "build", "--builder", name, "api", "web"], directory,
            "local-authentic-source-build", 1800, environment=environment, cwd=source_context)
    finally:
        try:
            observe_image_builds(image_registry, execution)
        finally:
            with (directory / "local-builder-cleanup.private.log").open("w", encoding="utf-8") as output:
                subprocess.run(["docker", "buildx", "rm", name], stdout=output, stderr=subprocess.STDOUT, check=False, timeout=180)
