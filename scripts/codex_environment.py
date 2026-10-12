"""Manage the one permanent synthetic Codex stack without changing the user stack.

init writes only ignored local configuration. plan is read-only. start/update
preserve volumes; stop never removes resources. test c08503 reuses its fixture.
"""
from __future__ import annotations

import argparse
import hashlib
import http.cookiejar
import json
import os
import re
import secrets
import shutil
import socket
import subprocess
import tomllib
import urllib.request
from pathlib import Path
from uuid import uuid4

from ci.local_resources import (
    OWNER_LABEL,
    PERMANENT_LABEL,
    canonical_hash,
    memory_bytes,
)

ROOT = Path(__file__).resolve().parents[1]
PROJECT = "trackvance-codex"
ACTIVE = ("postgres", "api", "web", "worker", "acquisition-worker", "delivery-worker", "report-worker", "events-notifications", "test-postgres")
OPTIONAL = ("scheduler", "events-chaining")
BACKEND = frozenset(set(ACTIVE + OPTIONAL) - {"postgres", "web", "test-postgres"})
APP_VOLUMES = ("postgres_data", "trackvance_data", "connection_credentials", "connection_keys", "delivery_credentials", "delivery_keys")
VOLUMES = (*APP_VOLUMES, "test_destination_data")
ENVIRONMENT_KEYS = {"POSTGRES_USER", "POSTGRES_DB", "POSTGRES_PASSWORD", "WEB_PORT", "CODEX_API_PORT",
    "TRACKVANCE_WEB_ORIGIN", "TRACKVANCE_PUBLIC_URL", "CODEX_ADMIN_PASSWORD", "CODEX_DESTINATION_PASSWORD", "CODEX_BACKEND_IMAGE", "CODEX_WEB_IMAGE", "CODEX_POSTGRES_IMAGE"}


def require(value, code):
    if not value:
        raise ValueError(code)


def command(arguments, *, environment=None, timeout=90):
    try:
        result = subprocess.run(arguments, cwd=ROOT, env=environment, capture_output=True,
            text=True, encoding="utf-8", errors="strict", check=False, timeout=timeout)
    except (subprocess.TimeoutExpired, OSError) as error:
        command_failure(arguments, error=error)
        raise ValueError("CODEX_COMMAND_FAILED_" + Path(arguments[0]).stem) from None
    if result.returncode:
        command_failure(arguments, result=result)
        raise ValueError("CODEX_COMMAND_FAILED_" + Path(arguments[0]).stem)
    return result.stdout.strip()


def command_failure(arguments, *, result=None, error=None):
    directory = ROOT / ".codex-local" / "codex-command-failures"
    for candidate in (directory, directory.parent):
        require(not candidate.is_symlink() and not getattr(candidate, "is_junction", lambda: False)(), "LINKED_DIAGNOSTIC_DIRECTORY_FORBIDDEN")
    directory.mkdir(parents=True, exist_ok=True)
    def private_text(value):
        return value.decode("utf-8", errors="replace") if isinstance(value, bytes) else value or ""
    save(directory / ("command-" + uuid4().hex + ".private.json"), {
        "status": "FAIL", "arguments": [str(item) for item in arguments],
        "returncode": result.returncode if result is not None else None,
        "error_type": type(error).__name__ if error is not None else None,
        "stdout": private_text(result.stdout if result is not None else getattr(error, "stdout", None)),
        "stderr": private_text(result.stderr if result is not None else getattr(error, "stderr", None)),
    })


def reference(path):
    raw = path.read_bytes()
    return {"path": str(path.resolve()), "sha256": hashlib.sha256(raw).hexdigest(), "bytes": len(raw)}


def save(path, value):
    temporary = path.with_name(path.name + "." + uuid4().hex + ".tmp")
    with temporary.open("x", encoding="utf-8") as stream:
        json.dump(value, stream, indent=2, sort_keys=True)
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())
    temporary.chmod(0o600)
    temporary.replace(path)


def private_root():
    common = Path(command(["git", "rev-parse", "--path-format=absolute", "--git-common-dir"]))
    require(common.is_absolute() and common.is_dir(), "EXACT_COMMON_GIT_DIRECTORY_REQUIRED")
    for candidate in (common, *common.parents):
        require(not candidate.is_symlink() and not getattr(candidate, "is_junction", lambda: False)(), "LINKED_COMMON_GIT_DIRECTORY_FORBIDDEN")
    return common.parent / ".codex-local" / "codex-permanent"


def private_directory(path):
    expected = private_root()
    path = expected if path is None else path
    require(path.resolve() == expected.resolve(), "EXACT_PERMANENT_CODEX_DIRECTORY_REQUIRED")
    for candidate in (path, *path.parents):
        require(not candidate.is_symlink() and not getattr(candidate, "is_junction", lambda: False)(), "LINKED_CODEX_DIRECTORY_FORBIDDEN")
    return path.resolve()


def runtime_input(role, name):
    if role == "backend":
        return (name.startswith(("backend/src/", "backend/migrations/", "demo/"))
            or name in {"backend/pyproject.toml", "backend/uv.lock", "backend/alembic.ini", "backend/Dockerfile", ".dockerignore"})
    return (name.startswith(("frontend/src/", "frontend/public/")) and not re.search(r"(?:/__tests__/|\.(?:test|spec)\.)", name)
        or name.startswith("frontend/") and Path(name).name in {"package.json", "pnpm-lock.yaml", "pnpm-workspace.yaml", "index.html", "vite.config.ts", "tsconfig.json", "tsconfig.app.json", "tsconfig.node.json"}
        or name in {"deploy/docker/frontend.Dockerfile", "deploy/nginx/default.conf", ".dockerignore"})


def source_version(sha):
    backend = tomllib.loads(command(["git", "show", sha + ":backend/pyproject.toml"]))["project"]["version"]
    frontend = json.loads(command(["git", "show", sha + ":frontend/package.json"]))["version"]
    require(backend == frontend and re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+", backend), "COHERENT_COMPONENT_PACKAGE_VERSION_REQUIRED")
    return backend


def fingerprint(role, sha):
    require(role in {"backend", "web"} and re.fullmatch(r"[a-f0-9]{40}", sha), "EXACT_COMPONENT_SOURCE_REQUIRED")
    rows = []
    for record in command(["git", "ls-tree", "-rz", sha]).split("\0"):
        if not record:
            continue
        metadata, name = record.split("\t", 1)
        mode, kind, blob = metadata.split()
        if runtime_input(role, name):
            require(kind == "blob" and mode in {"100644", "100755"}, "UNSUPPORTED_COMPONENT_BUILD_INPUT")
            rows.append({"path": name, "mode": mode, "blob": blob})
    require(rows, "COMPLETE_COMPONENT_INPUTS_REQUIRED")
    return {"sha256": canonical_hash(rows), "files": len(rows), "source_sha": sha}


def source_identity():
    sha = command(["git", "rev-parse", "HEAD"])
    require(re.fullmatch(r"[a-f0-9]{40}", sha), "EXACT_REPOSITORY_HEAD_REQUIRED")
    require(not command(["git", "status", "--porcelain", "--untracked-files=normal"]), "COMMIT_REVIEWED_CHANGES_BEFORE_CODEX_OPERATION")
    return sha


def process_environment(values):
    # Shell values must never win over the explicit generated env-file.
    environment = {key: value for key, value in os.environ.items()
        if key.upper() in {"PATH", "PATHEXT", "SYSTEMROOT", "WINDIR", "TEMP", "TMP", "HOME", "USERPROFILE", "APPDATA", "LOCALAPPDATA", "PROGRAMFILES", "PROGRAMFILES(X86)", "PROGRAMW6432", "DOCKER_CONFIG", "DOCKER_HOST", "DOCKER_CONTEXT"}}
    environment.update(values)
    environment["COMPOSE_DISABLE_ENV_FILE"] = "1"
    return environment


def load(directory):
    require((directory / "environment.json").is_file() and not (directory / "environment.json").is_symlink(), "CODEX_INIT_REQUIRED")
    value = json.loads((directory / "environment.json").read_text(encoding="utf-8"))
    require(value["schema_version"] == 1 and value["project"] == PROJECT
        and value["owner"] == PROJECT and value["permanent"] is True, "EXACT_PERMANENT_OWNER_REQUIRED")
    env_file = directory / "codex.env"
    require(env_file.is_file() and not env_file.is_symlink() and reference(env_file) == value["environment_file"], "CODEX_ENVIRONMENT_FILE_DRIFT")
    values = dict(line.split("=", 1) for line in env_file.read_text(encoding="utf-8").splitlines() if line)
    require(set(values) == ENVIRONMENT_KEYS and values["POSTGRES_DB"] == values["POSTGRES_USER"] == "tv_codex"
        and values["TRACKVANCE_WEB_ORIGIN"] == values["TRACKVANCE_PUBLIC_URL"] == f"http://127.0.0.1:{values['WEB_PORT']}", "FRESH_CANONICAL_CODEX_CONFIG_REQUIRED")
    for role in ("backend", "web", "postgres"):
        require(values["CODEX_" + role.upper() + "_IMAGE"] == value["images"][role]["id"], "EXACT_IMMUTABLE_ROLE_PIN_REQUIRED")
    return value, values


def compose(directory, values):
    return ["docker", "compose", "--project-name", PROJECT, "--env-file", str(directory / "codex.env"),
        "-f", str(ROOT / "compose.yml"), "-f", str(ROOT / "compose.codex.yml")], process_environment(values)


def inventory(project, environment):
    ids = command(["docker", "ps", "-aq", "--no-trunc", "--filter", "label=com.docker.compose.project=" + project], environment=environment).split()
    rows = json.loads(command(["docker", "inspect", *ids], environment=environment)) if ids else []
    resources = {"containers": rows}
    for kind in ("volume", "network"):
        ids = command(["docker", kind, "ls", "-q", "--filter", "label=com.docker.compose.project=" + project], environment=environment).split()
        resources[kind + "s"] = json.loads(command(["docker", kind, "inspect", *ids], environment=environment)) if ids else []
    return resources


def stable_inventory(value):
    return {"containers": {row["Id"]: {"image": row["Image"], "config": canonical_hash(row["Config"]),
        "host": canonical_hash(row["HostConfig"]), "mounts": sorted(row["Mounts"], key=lambda m: m["Destination"]),
        "running": row["State"]["Running"], "started_at": row["State"].get("StartedAt")} for row in value["containers"]},
        "volumes": {row["Name"]: json.loads(json.dumps(row)) for row in value["volumes"]},
        "networks": {row["Id"]: {k: row.get(k) for k in ("Name", "Driver", "Internal", "Labels", "IPAM", "Options", "Scope", "EnableIPv6", "Attachable", "Ingress")} for row in value["networks"]}}


def labels_owned(labels):
    return labels.get(OWNER_LABEL) == PROJECT and labels.get(PERMANENT_LABEL) == "true"


def validate_resolved(config, values, protected):
    require(config["name"] == PROJECT and set(config["services"]) == set(ACTIVE + OPTIONAL), "EXACT_CODEX_TOPOLOGY_REQUIRED")
    require(set(config["volumes"]) == set(VOLUMES) and set(config["networks"]) == {"default"}, "EXACT_SIX_APP_VOLUMES_TEST_DESTINATION_VOLUME_AND_ONE_NETWORK_REQUIRED")
    protected_volumes = {row["Name"] for row in protected["volumes"]}
    protected_networks = {row["Name"] for row in protected["networks"]}
    protected_ports = {int(p["HostPort"]) for row in protected["containers"] for ports in (row["HostConfig"].get("PortBindings") or {}).values() for p in ports or []}
    protected_secrets = {entry.partition("=")[2] for row in protected["containers"] for entry in row["Config"].get("Env", [])
        if re.search(r"PASSWORD|CLIENT_SECRET|TOKEN", entry.partition("=")[0]) and entry.partition("=")[2]}
    fresh_secrets = [values[key] for key in ("POSTGRES_PASSWORD", "CODEX_ADMIN_PASSWORD", "CODEX_DESTINATION_PASSWORD")]
    require(len(set(fresh_secrets)) == 3 and all(secret not in protected_secrets for secret in fresh_secrets), "USER_CREDENTIAL_REUSE_FORBIDDEN")
    for kind, reserved in (("volumes", protected_volumes), ("networks", protected_networks)):
        for name, entry in config[kind].items():
            require(entry["name"] == PROJECT + "_" + name and entry["name"] not in reserved
                and not entry.get("external") and not entry.get("driver_opts") and labels_owned(entry.get("labels", {})), "SHARED_OR_UNOWNED_CODEX_RESOURCE")
    require(config["networks"]["default"].get("internal", False) is False
        and config["networks"]["default"].get("driver", "bridge") == "bridge", "ISOLATED_CODEX_BRIDGE_REQUIRED")
    cpu = memory = 0
    for name, service in config["services"].items():
        role = "postgres" if name in {"postgres", "test-postgres"} else "web" if name == "web" else "backend"
        require(service["image"] == values["CODEX_" + role.upper() + "_IMAGE"] and not service.get("build")
            and service.get("restart", "no") == "no" and labels_owned(service.get("labels", {})), "EXACT_CODEX_IMAGE_OWNER_AND_RESTART_REQUIRED")
        require(not service.get("privileged") and not service.get("cap_add") and not service.get("devices")
            and not service.get("network_mode") and not service.get("pid") and not service.get("env_file"), "UNSAFE_CODEX_HOST_OR_ENVIRONMENT_ACCESS")
        require(float(service["cpus"]) > 0 and memory_bytes(service["mem_limit"]) > 0 and 0 < int(service["pids_limit"]) <= 128, "FINITE_CODEX_SERVICE_BUDGET_REQUIRED")
        cpu += float(service["cpus"]); memory += memory_bytes(service["mem_limit"])
        for mount in service.get("volumes", []):
            require(mount["type"] == "volume" and mount["source"] in VOLUMES, "CODEX_BIND_OR_SHARED_MOUNT_FORBIDDEN")
        for port in service.get("ports", []):
            expected = values["WEB_PORT"] if name == "web" else values["CODEX_API_PORT"] if name == "api" else None
            require(port["host_ip"] == "127.0.0.1" and str(port["published"]) == expected
                and int(port["published"]) not in protected_ports, "CANONICAL_EXCLUSIVE_CODEX_PORT_REQUIRED")
        if name in BACKEND:
            env = service["environment"]
            require(env["DEMO_SEED_ENABLED"] == "false" and env["DEMO_ACCESS_ENABLED"] == "true"
                and env["TRACKVANCE_WEB_ORIGIN"] == values["TRACKVANCE_WEB_ORIGIN"]
                and env["DATABASE_URL"] == f"postgresql+psycopg://tv_codex:{values['POSTGRES_PASSWORD']}@postgres:5432/tv_codex", "OWN_SYNTHETIC_CODEX_APPLICATION_CONFIG_REQUIRED")
        if name == "test-postgres":
            require(not service.get("ports") and service["environment"] == {"POSTGRES_USER": "tv_codex_destination", "POSTGRES_DB": "tv_codex_destination", "POSTGRES_PASSWORD": values["CODEX_DESTINATION_PASSWORD"]}, "SEPARATE_SYNTHETIC_DELIVERY_DESTINATION_REQUIRED")
    require(cpu <= 4 and memory <= 6 * 1024**3, "AGGREGATE_CODEX_BUDGET_EXCEEDED")
    return {"all_services_cpus": cpu, "all_services_memory_bytes": memory}


def validate_owned(existing, config, value, *, unchanged_roles=None):
    require(len(existing["containers"]) == len({row["Id"] for row in existing["containers"]}), "DUPLICATE_CODEX_CONTAINER_ID")
    services = set()
    for row in existing["containers"]:
        labels = row["Config"].get("Labels") or {}; name = labels.get("com.docker.compose.service")
        require(labels_owned(labels) and name in config["services"] and name not in services
            and labels.get("com.docker.compose.project") == PROJECT, "FOREIGN_OR_AMBIGUOUS_CODEX_CONTAINER")
        services.add(name)
        service = config["services"][name]
        require(row["Image"] == config["services"][name]["image"], "CODEX_RUNTIME_IMAGE_DRIFT")
        require(row["Config"]["Image"] == service["image"], "CODEX_RUNTIME_IMAGE_REFERENCE_DRIFT")
        role = "postgres" if name in {"postgres", "test-postgres"} else "web" if name == "web" else "backend"
        defaults = value.get("_image_defaults", {}).get(role)
        require(defaults is not None, "EXACT_INSPECTED_IMAGE_DEFAULTS_REQUIRED")
        expected_env = dict(item.split("=", 1) for item in defaults.get("Env") or [])
        expected_env.update({key: str(item) for key, item in service.get("environment", {}).items()})
        actual_env = row["Config"].get("Env") or []
        require(len(actual_env) == len({entry.split("=", 1)[0] for entry in actual_env})
            and dict(entry.split("=", 1) for entry in actual_env) == expected_env, "CODEX_RUNTIME_ENVIRONMENT_DRIFT")
        expected_command = service.get("command") if service.get("command") is not None else defaults.get("Cmd")
        expected_entrypoint = service.get("entrypoint") if service.get("entrypoint") is not None else defaults.get("Entrypoint")
        require(row["Config"].get("Cmd") == expected_command, "CODEX_RUNTIME_COMMAND_DRIFT")
        require(row["Config"].get("Entrypoint") == expected_entrypoint
            and row["Config"].get("User", "") == str(service.get("user", defaults.get("User", ""))), "CODEX_RUNTIME_PROCESS_IDENTITY_DRIFT")
        expected_mounts = {entry["target"]: (config["volumes"][entry["source"]]["name"], not entry.get("read_only", False)) for entry in service.get("volumes", [])}
        require(len(row["Mounts"]) == len(expected_mounts) and {m["Destination"]: (m.get("Name"), m.get("RW")) for m in row["Mounts"]} == expected_mounts
            and all(m.get("Type") == "volume" for m in row["Mounts"]), "FOREIGN_CODEX_RUNTIME_MOUNT")
        host = row["HostConfig"]
        require(host.get("Memory") == memory_bytes(service["mem_limit"]) and host.get("NanoCpus") == int(float(service["cpus"]) * 10**9)
            and host.get("PidsLimit") == int(service["pids_limit"]) and host.get("Privileged") is False
            and not host.get("CapAdd") and host.get("PidMode", "") == "" and host.get("NetworkMode") == PROJECT + "_default",
            "CODEX_RUNTIME_HOST_OR_RESOURCE_LIMIT_DRIFT")
        require(set(host.get("CapDrop") or []) == set(service.get("cap_drop") or [])
            and set(host.get("SecurityOpt") or []) == set(service.get("security_opt") or [])
            and host.get("ReadonlyRootfs", False) == bool(service.get("read_only", False))
            and not host.get("VolumesFrom") and not host.get("Devices"), "CODEX_RUNTIME_CONFINEMENT_DRIFT")
        expected_ports = {str(port["target"]) + "/tcp": [{"HostIp": port["host_ip"], "HostPort": str(port["published"])}] for port in service.get("ports", [])}
        require((host.get("PortBindings") or {}) == expected_ports, "CODEX_RUNTIME_PORT_DRIFT")
        require(set(row.get("NetworkSettings", {}).get("Networks", {})) <= {PROJECT + "_default"}, "SHARED_CODEX_RUNTIME_NETWORK")
        baseline = value.get("runtime_identities", {}).get(name)
        if baseline and (unchanged_roles is None or name in unchanged_roles):
            require(baseline == runtime_identity(row), "FULL_CODEX_CONFIG_HOST_MOUNT_OR_ID_DRIFT")
    for kind in ("volumes", "networks"):
        for row in existing[kind]:
            require(labels_owned(row.get("Labels") or {}) and row["Name"] in {entry["name"] for entry in config[kind].values()}, "FOREIGN_CODEX_RESOURCE_IDENTITY")
            if kind == "networks":
                require(row.get("Driver") == "bridge" and row.get("Internal") is False, "CODEX_RUNTIME_NETWORK_MODE_DRIFT")
    if value.get("resources"):
        stable = stable_inventory(existing)
        require(stable["volumes"] == value["resources"]["volumes"]
            and stable["networks"] == value["resources"]["networks"], "PERMANENT_CODEX_VOLUMES_OR_NETWORK_CHANGED")


def runtime_identity(row):
    return {"id": row["Id"], "image": row["Image"], "config_sha256": canonical_hash(row["Config"]),
        "host_config_sha256": canonical_hash(row["HostConfig"]), "mounts": sorted(row["Mounts"], key=lambda m: m["Destination"])}


def prepare(directory, value, values):
    prefix, environment = compose(directory, values)
    require(command(["docker", "info", "--format", "{{.ID}}"], environment=environment) == value["engine_id"], "APPROVED_DOCKER_ENGINE_CHANGED")
    protected = inventory("trackvance-certification", environment)
    config = json.loads(command([*prefix, "--profile", "codex-automatic", "config", "--format", "json"], environment=environment))
    budgets = validate_resolved(config, values, protected)
    existing = inventory(PROJECT, environment)
    # Include exact reserved names even when an unknown owner omitted Compose labels.
    for kind in ("volume", "network"):
        reserved = {entry["name"] for entry in config[kind + "s"].values()}
        names = command(["docker", kind, "ls", "--format", "{{.Name}}"], environment=environment).split()
        exact = sorted(reserved & set(names))
        existing[kind + "s"] = json.loads(command(["docker", kind, "inspect", *exact], environment=environment)) if exact else []
    value["_image_defaults"] = {}
    for role, image in value["images"].items():
        rows = json.loads(command(["docker", "image", "inspect", image["id"]], environment=environment))
        require(len(rows) == 1 and rows[0]["Id"] == image["id"], "EXACT_LOCAL_IMMUTABLE_IMAGE_REQUIRED")
        labels = rows[0]["Config"].get("Labels") or {}
        if role != "postgres":
            require(labels.get("org.opencontainers.image.revision") == image["source_sha"]
                and labels.get("org.opencontainers.image.version") == image["version"], "TRUE_ORIGINAL_COMPONENT_OCI_SOURCE_REQUIRED")
        else:
            require("PG_MAJOR=16" in (rows[0]["Config"].get("Env") or []), "EXACT_EXISTING_POSTGRES16_RUNTIME_REQUIRED")
        value["_image_defaults"][role] = rows[0]["Config"]
    validate_owned(existing, config, value)
    return prefix, environment, config, existing, stable_inventory(protected), budgets


def update_pins(directory, value, values):
    env_file = directory / "codex.env"
    temporary = directory / ("codex.env." + uuid4().hex + ".tmp")
    with temporary.open("x", encoding="utf-8") as stream:
        stream.write("\n".join(f"{key}={values[key]}" for key in sorted(values)) + "\n")
        stream.flush(); os.fsync(stream.fileno())
    temporary.chmod(0o600)
    temporary.replace(env_file)
    value["environment_file"] = reference(env_file)
    save(directory / "environment.json", {key: item for key, item in value.items() if not key.startswith("_")})


def initialize(args, directory):
    require(not directory.exists(), "EXISTING_PERMANENT_CODEX_ENVIRONMENT_MUST_NOT_BE_OVERWRITTEN")
    require(re.fullmatch(r"[a-f0-9]{40}", args.image_source_sha or "") and args.engine_id, "EXACT_EXISTING_IMAGE_SOURCE_AND_ENGINE_REQUIRED")
    require(1024 <= args.web_port <= 65535 and 1024 <= args.api_port <= 65535 and args.web_port != args.api_port, "EXCLUSIVE_CODEX_PORTS_REQUIRED")
    images = {}
    for role in ("backend", "web", "postgres"):
        identifier = getattr(args, role + "_image")
        require(re.fullmatch(r"sha256:[a-f0-9]{64}", identifier or ""), "IMMUTABLE_IMAGE_ID_REQUIRED")
        images[role] = {"id": identifier, **({"source_sha": args.image_source_sha, "version": source_version(args.image_source_sha), "fingerprint": fingerprint(role, args.image_source_sha)} if role != "postgres" else {})}
    directory.mkdir(parents=True, mode=0o700)
    (directory / "fixtures").mkdir(mode=0o700)
    values = {"POSTGRES_USER": "tv_codex", "POSTGRES_DB": "tv_codex", "POSTGRES_PASSWORD": secrets.token_urlsafe(48),
        "CODEX_ADMIN_PASSWORD": secrets.token_urlsafe(48), "CODEX_DESTINATION_PASSWORD": secrets.token_urlsafe(48), "WEB_PORT": str(args.web_port), "CODEX_API_PORT": str(args.api_port),
        "TRACKVANCE_WEB_ORIGIN": f"http://127.0.0.1:{args.web_port}", "TRACKVANCE_PUBLIC_URL": f"http://127.0.0.1:{args.web_port}",
        **{"CODEX_" + role.upper() + "_IMAGE": image["id"] for role, image in images.items()}}
    value = {"schema_version": 1, "project": PROJECT, "owner": PROJECT, "permanent": True,
        "synthetic_only": True, "engine_id": args.engine_id, "images": images, "resources": None,
        "active_default": list(ACTIVE), "automatic_default": False, "fixtures_directory": str(directory / "fixtures"), "runtime_version": source_version(args.image_source_sha)}
    update_pins(directory, value, values)
    return {"status": "CONFIGURED_NOT_STARTED", "project": PROJECT, "directory": str(directory), "web_url": values["TRACKVANCE_WEB_ORIGIN"]}


def plan(value, sha):
    changes = [role for role in ("backend", "web") if fingerprint(role, sha)["sha256"] != value["images"][role]["fingerprint"]["sha256"]]
    return {"project": PROJECT, "source_sha": sha, "changed_components": changes,
        "preserved_original_oci_sources": {role: item.get("source_sha") for role, item in value["images"].items()},
        "default_services": list(ACTIVE), "optional_services": list(OPTIONAL), "volumes_preserved": list(VOLUMES)}


def check_health(values, version):
    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, *args, **kwargs):
            raise ValueError("CODEX_HEALTH_REDIRECT_FORBIDDEN")
    opener = urllib.request.build_opener(NoRedirect)
    base = values["TRACKVANCE_WEB_ORIGIN"]
    results = {}
    for path in ("/api/v1/health", "/api/v1/health/ready"):
        with opener.open(base + path, timeout=15) as response:
            require(response.status == 200 and response.url == base + path, "EXACT_CODEX_HEALTH_URL_REQUIRED")
            results[path] = json.loads(response.read(1024 * 1024))
    require(results["/api/v1/health"]["version"] == version and results["/api/v1/health"]["demo_seed_enabled"] is False
        and results["/api/v1/health/ready"] == {"status": "ready", "version": version, "database": "ready", "storage": "ready", "migrations": "head"}, "EXACT_CODEX_RUNTIME_VERSION_READY_REQUIRED")
    return results


def execute_stage(args, directory, value, values, sha):
    change = plan(value, sha)
    prefix, environment, config, existing, protected, budgets = prepare(directory, value, values)
    selected = tuple(args.services or ACTIVE)
    require(set(selected) <= set(ACTIVE + OPTIONAL), "EXACT_CODEX_SERVICE_SELECTION_REQUIRED")
    intent = {"stage": args.stage, "source_sha": sha, "images": json.loads(json.dumps(value["images"])), "selected_services": selected,
        "budget": budgets, "protected_inventory_sha256": canonical_hash(protected), "configuration_sha256": canonical_hash(config)}
    receipt = directory / ("operation-" + args.stage + "-" + uuid4().hex + ".json")
    intent_path = receipt.with_suffix(".intent.json")
    save(intent_path, {"status": "INTENT", **intent})
    try:
        if args.stage == "stop":
            command([*prefix, "stop", "--timeout", "30", *selected], environment=environment, timeout=180)
        elif args.stage in {"start", "update"}:
            if args.stage == "start":
                require(not change["changed_components"], "UPDATE_CHANGED_COMPONENTS_BEFORE_START")
                if not existing["containers"]:
                    for key in ("WEB_PORT", "CODEX_API_PORT"):
                        with socket.socket() as probe:probe.bind(("127.0.0.1", int(values[key])))
                command([*prefix, "up", "--no-build", "--pull", "never", "--detach", "--wait", "--wait-timeout", "180", *selected], environment=environment, timeout=240)
            elif change["changed_components"]:
                require(not command(["git", "diff", "--name-only", value["images"]["backend"]["source_sha"], sha, "--", "backend/migrations"]), "SCHEMA_CHANGE_REQUIRES_SEPARATE_BACKUP_RESTORE_REVIEW")
                for role in change["changed_components"]:
                    supplied = getattr(args, role + "_image")
                    image_sha = args.image_source_sha or sha
                    if supplied:
                        identifier = supplied
                        require(fingerprint(role, image_sha)["sha256"] == fingerprint(role, sha)["sha256"], "SUPPLIED_IMAGE_COMPONENT_EXECUTABLES_DIFFER_FROM_SOURCE")
                    else:
                        require(args.build_changed, "EXPLICIT_BUILD_CHANGED_COMPONENTS_OR_CERTIFIED_IMAGE_REQUIRED")
                        command(["docker", "buildx", "inspect", "default"], environment=environment)
                        tag = PROJECT + ":" + role + "-" + sha[:12]
                        require(not command(["docker", "image", "ls", "-q", "--no-trunc", tag], environment=environment), "EXISTING_CODEX_COMPONENT_TAG_MUST_NOT_BE_OVERWRITTEN")
                        dockerfile = "backend/Dockerfile" if role == "backend" else "deploy/docker/frontend.Dockerfile"
                        command(["docker", "buildx", "build", "--builder", "default", "--load", "--pull=false", "--progress", "plain",
                            "--label", "org.opencontainers.image.revision=" + sha, "--label", "org.opencontainers.image.version=" + source_version(sha),
                            "--label", OWNER_LABEL + "=" + PROJECT, "--label", PERMANENT_LABEL + "=true", "--tag", tag, "--file", dockerfile, "."], environment=environment, timeout=1200)
                        identifier = command(["docker", "image", "inspect", "--format", "{{.Id}}", tag], environment=environment)
                        image_sha = sha
                    require(re.fullmatch(r"sha256:[a-f0-9]{64}", identifier), "EXACT_NEW_COMPONENT_IMAGE_REQUIRED")
                    inspection = json.loads(command(["docker", "image", "inspect", identifier], environment=environment))
                    image_labels = inspection[0]["Config"].get("Labels") or {}
                    require(len(inspection) == 1 and inspection[0]["Id"] == identifier
                        and image_labels.get("org.opencontainers.image.revision") == image_sha
                        and image_labels.get("org.opencontainers.image.version") == source_version(image_sha), "EXACT_NEW_IMAGE_TRUE_OCI_SOURCE_AND_VERSION_REQUIRED")
                    value["images"][role] = {"id": identifier, "source_sha": image_sha, "version": source_version(image_sha), "fingerprint": fingerprint(role, sha)}
                    value["_image_defaults"][role] = inspection[0]["Config"]
                    values["CODEX_" + role.upper() + "_IMAGE"] = identifier
                value["runtime_version"] = source_version(sha)
                update_pins(directory, value, values); prefix, environment = compose(directory, values)
                roles = (set(BACKEND) if "backend" in change["changed_components"] else set()) | ({"web"} if "web" in change["changed_components"] else set())
                was_running = {row["Config"]["Labels"]["com.docker.compose.service"] for row in existing["containers"] if row["State"]["Running"]}
                restart = sorted(roles & was_running); stopped = sorted(roles - was_running)
                if not existing["containers"]:
                    for key in ("WEB_PORT", "CODEX_API_PORT"):
                        with socket.socket() as probe:probe.bind(("127.0.0.1", int(values[key])))
                    command([*prefix, "up", "--no-build", "--pull", "never", "--detach", "--wait", "--wait-timeout", "180", *ACTIVE], environment=environment, timeout=240)
                else:
                    if stopped:command([*prefix, "up", "--no-start", "--no-deps", "--no-build", "--pull", "never", *stopped], environment=environment, timeout=240)
                    if restart:command([*prefix, "up", "--no-deps", "--no-build", "--pull", "never", "--detach", "--wait", "--wait-timeout", "180", *restart], environment=environment, timeout=240)
            else:
                require(existing["containers"], "IMAGES_UNCHANGED_USE_START_FOR_INITIAL_PROVISIONING")
        else:
            require(not change["changed_components"], "CODEX_DEPLOYED_EXECUTABLE_COMPONENTS_DIFFER_FROM_SOURCE")
            require({row["Config"]["Labels"]["com.docker.compose.service"] for row in existing["containers"] if row["State"]["Running"]} >= set(ACTIVE), "DEFAULT_CODEX_SERVICES_NOT_RUNNING")
        final = inventory(PROJECT, environment)
        updated_config = json.loads(command([*prefix, "--profile", "codex-automatic", "config", "--format", "json"], environment=environment))
        validate_resolved(updated_config, values, inventory("trackvance-certification", environment))
        updated_roles = ((set(BACKEND) if "backend" in change["changed_components"] else set()) | ({"web"} if "web" in change["changed_components"] else set())) if args.stage == "update" else set()
        validate_owned(final, updated_config, value, unchanged_roles=set(ACTIVE + OPTIONAL) - updated_roles)
        require(stable_inventory(inventory("trackvance-certification", environment)) == protected, "USER_INSTALLATION_IDENTITY_CONFIG_OR_STATE_CHANGED")
        stable = stable_inventory(final)
        value["resources"] = {key: stable[key] for key in ("volumes", "networks")}
        require(len(value["resources"]["volumes"]) == len(VOLUMES) and len(value["resources"]["networks"]) == 1, "ALL_PERMANENT_RESOURCES_REQUIRED")
        value["runtime_identities"] = {row["Config"]["Labels"]["com.docker.compose.service"]: runtime_identity(row) for row in final["containers"]}
        value["verified_repository_sha"] = sha; save(directory / "environment.json", {key: item for key, item in value.items() if not key.startswith("_")})
        health = check_health(values, value["runtime_version"]) if args.stage != "stop" else None
        save(receipt, {"status": "PASS", **intent, "images_after": value["images"], "resources": value["resources"], "health": health,
            "intent": reference(intent_path), "user_installation_unchanged": True, "volumes_removed": 0, "permanent": True})
        return {"status": "PASS_NOOP" if args.stage == "update" and not change["changed_components"] else "PASS", "project": PROJECT, "stage": args.stage, "receipt": reference(receipt), "web_url": values["TRACKVANCE_WEB_ORIGIN"]}
    except BaseException as error:
        save(receipt, {"status": "FAIL", **intent, "intent": reference(intent_path), "error_type": type(error).__name__, "error_code": str(error) if isinstance(error, ValueError) else "CODEX_OPERATION_FAILED"})
        raise


def test_c08503(directory, value, values):
    _prefix, environment, _config, existing, protected, _budget = prepare(directory, value, values)
    require({row["Config"]["Labels"]["com.docker.compose.service"] for row in existing["containers"] if row["State"]["Running"]} >= set(ACTIVE), "START_AND_VERIFY_CODEX_BEFORE_TEST")
    check_health(values, value["runtime_version"])
    fixture = directory / "fixtures" / "c08503.json"
    require(not fixture.is_symlink() and not getattr(fixture.parent, "is_junction", lambda: False)(), "EXACT_PRIVATE_PERSISTENT_FIXTURE_REQUIRED")
    environment = process_environment({})
    environment.update(TV_E2E_URL=values["TRACKVANCE_WEB_ORIGIN"], PLAYWRIGHT_BASE_URL=values["TRACKVANCE_WEB_ORIGIN"],
        COMPOSE_PROJECT_NAME=PROJECT, TV_C08503_FIXTURE_MANIFEST=str(fixture), TV_CODEX_SOURCE_SHA=command(["git", "rev-parse", "HEAD"]), TV_E2E_PRIVATE_ARTIFACTS="1")
    pnpm = shutil.which("pnpm.cmd" if os.name == "nt" else "pnpm")
    require(pnpm, "PNPM_EXECUTABLE_REQUIRED")
    command([pnpm, "--dir", "frontend", "exec", "playwright", "test", "tests-e2e/catalog-reports.spec.ts", "--project", "local-chrome", "--workers", "1", "--grep", "C085-03"], environment=environment, timeout=240)
    require(fixture.is_file() and stable_inventory(inventory("trackvance-certification", environment)) == protected, "PERSISTENT_FIXTURE_OR_PROTECTED_INSTALLATION_CHANGED")
    return {"status": "PASS", "project": PROJECT, "scope": "C085-03_EXISTING_PERMANENT_FIXTURE", "fixture": reference(fixture), "no_stack_created_or_removed": True}


def fixture_destination(directory, value, values):
    _prefix, environment, _config, existing, protected, _budget = prepare(directory, value, values)
    require({row["Config"]["Labels"]["com.docker.compose.service"] for row in existing["containers"] if row["State"]["Running"]} >= set(ACTIVE), "START_AND_VERIFY_CODEX_BEFORE_FIXTURE")
    check_health(values, value["runtime_version"])
    path = directory / "fixtures" / "test-destination.json"
    require(not path.is_symlink(), "REGULAR_PRIVATE_DESTINATION_MANIFEST_REQUIRED")
    cookies = http.cookiejar.CookieJar()
    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, *args, **kwargs):
            raise ValueError("CODEX_FIXTURE_REDIRECT_FORBIDDEN")
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cookies), NoRedirect())
    csrf = ""
    def api(relative, body=None):
        nonlocal csrf
        headers = {"X-CSRF-Token": csrf}
        if body is not None:
            headers["Content-Type"] = "application/json"
        url = values["TRACKVANCE_WEB_ORIGIN"] + "/api/v1" + relative
        request = urllib.request.Request(url, data=None if body is None else json.dumps(body).encode(), headers=headers)
        with opener.open(request, timeout=30) as response:
            require(response.status in {200, 201} and response.url == url, "EXACT_CODEX_FIXTURE_API_RESPONSE_REQUIRED")
            result = json.loads(response.read(1024 * 1024))
        if "csrf_token" in result:
            csrf = result["csrf_token"]
        return result
    api("/auth/demo", {})
    actor = api("/me")
    require(actor["organization"]["id"] == "org-trackvance-demo" and "destinations:manage" in actor["user"]["permissions"], "CODEX_SYNTHETIC_DESTINATION_ACTOR_REQUIRED")
    expected = {"name": "codex_test_postgres", "sink_type": "POSTGRESQL", "host": "test-postgres", "port": 5432,
        "database": "tv_codex_destination", "username": "tv_codex_destination"}
    if path.exists():
        record = json.loads(path.read_text(encoding="utf-8"))
        require(record["status"] == "PASS" and record["project"] == PROJECT and record["api_origin"] == values["TRACKVANCE_WEB_ORIGIN"]
            and record["organization_id"] == actor["organization"]["id"], "EXACT_PERSISTENT_DESTINATION_FIXTURE_REQUIRED")
        destination = api("/delivery/destinations/" + record["destination_id"])
        require(all(destination[key] == item for key, item in expected.items()) and destination["version"] == record["version"], "PERSISTENT_DESTINATION_DRIFT_NO_RECREATE")
    else:
        intent = path.with_suffix(".creation-intent.json")
        require(not intent.exists(), "INCOMPLETE_DESTINATION_CREATION_REQUIRES_RECONCILIATION")
        listed = api("/delivery/destinations")
        require(not any(item["name"] == expected["name"] for item in listed["items"]), "UNREGISTERED_EXISTING_TEST_DESTINATION_NO_ADOPTION")
        save(intent, {"status": "INTENT", "project": PROJECT, "api_origin": values["TRACKVANCE_WEB_ORIGIN"], "destination": expected})
        destination = api("/delivery/destinations", {**expected, "password": values["CODEX_DESTINATION_PASSWORD"],
            "options": {"connect_timeout": 5, "query_timeout": 30, "sslmode": "disable"}})
        require(all(destination[key] == item for key, item in expected.items()), "EXACT_SYNTHETIC_DESTINATION_CREATED_REQUIRED")
        save(path, {"status": "PASS", "project": PROJECT, "api_origin": values["TRACKVANCE_WEB_ORIGIN"],
            "organization_id": actor["organization"]["id"], "destination_id": destination["id"], "version": destination["version"], "intent": reference(intent)})
    tested = api("/delivery/destinations/" + destination["id"] + "/test", {})
    require(tested["status"] == "SUCCESS" and stable_inventory(inventory("trackvance-certification", environment)) == protected, "CODEX_DESTINATION_TEST_OR_PROTECTED_INSTALLATION_DRIFT")
    return {"status": "PASS", "project": PROJECT, "fixture": reference(path), "destination_id": destination["id"], "only_synthetic_target": True}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("stage", choices=["init", "plan", "start", "verify", "update", "stop", "test", "fixture-destination"])
    parser.add_argument("case", nargs="?", choices=["c08503"])
    parser.add_argument("--directory", type=Path, help="Exact Git common-directory parent/.codex-local/codex-permanent (default; shared across worktrees)")
    for role in ("backend", "web", "postgres"):parser.add_argument("--" + role + "-image")
    parser.add_argument("--image-source-sha"); parser.add_argument("--engine-id")
    parser.add_argument("--web-port", type=int, default=3205); parser.add_argument("--api-port", type=int, default=3206)
    parser.add_argument("--services", nargs="+"); parser.add_argument("--build-changed", action="store_true")
    args = parser.parse_args(); directory = private_directory(args.directory)
    if args.stage == "init":result = initialize(args, directory)
    else:
        value, values = load(directory); sha = source_identity()
        if args.stage == "plan":result = plan(value, sha)
        elif args.stage == "test":require(args.case == "c08503", "EXPLICIT_PERSISTENT_TEST_CASE_REQUIRED"); result = test_c08503(directory, value, values)
        elif args.stage == "fixture-destination":result = fixture_destination(directory, value, values)
        else:result = execute_stage(args, directory, value, values, sha)
    print(json.dumps(result))
    return 0


if __name__ == "__main__":
    try:raise SystemExit(main())
    except ValueError as error:
        print(json.dumps({"status": "FAIL", "code": str(error)}))
        raise SystemExit(1) from None
