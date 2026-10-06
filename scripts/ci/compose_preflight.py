"""Opt-in, read-only isolation checks for resolved historical CI stacks.

Resolved environments stay in memory. Errors contain safe codes, never values.
Image verification belongs to ci_images; this module does not change policy.
"""
from __future__ import annotations

import json
import math
import os
import re
import subprocess
from collections.abc import Callable
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse

ROOT = Path(__file__).resolve().parents[2]
MAIN_PROJECT = "trackvance-certification"
PROJECT = re.compile(
    r"trackvance-(?:v070-test-[a-z0-9-]+-[a-f0-9]{12}"
    r"|connections-e2e-[0-9]+-[a-f0-9]{12}"
    r"|(?:delivery-e2e|bench)-[0-9]+-[a-f0-9]{6}"
    r"|delivery-bench-[0-9]+-[a-f0-9]{8}"
    r"|(?:connections-e2e|delivery-e2e|bench|delivery-bench)-[a-f0-9]{12}"
    r"|(?:e2e|identity-e2e)-[a-f0-9]{8,32})")
APP_SERVICES = {"api", "worker", "acquisition-worker", "delivery-worker", "report-worker",
                "scheduler", "events-notifications", "events-chaining"}
PREFLIGHT_CODES = frozenset({
    "AMBIENT_ENV_FILE", "APP_PROJECT_MARKER", "AUTOMATIC_RESTART", "COMPOSE_ARGUMENTS",
    "COMPOSE_PROJECT_ARGUMENT", "CONTAINER_NAMESPACE", "EMPTY_SERVICES", "EXISTING_CONTAINER_NOT_OWNED",
    "EXISTING_PROTECTED_BIND", "EXISTING_PROTECTED_VOLUME", "EXISTING_RESOURCE_NOT_OWNED",
    "EXISTING_SERVICE_NOT_OWNED", "EXISTING_UNOWNED_BIND", "EXISTING_UNOWNED_NETWORK", "EXPLICIT_PRIVATE_ENV",
    "HOST_CONFIG_OR_SECRET", "HOST_CONTROL_SOCKET", "HOST_NAMESPACE", "HOST_NETWORK_DRIVER",
    "IMPLICIT_MOCK_OIDC", "INVALID_MOCK_OIDC", "INVALID_OR_UNAVAILABLE_RESOLVED_CONFIG",
    "LINKED_PRIVATE_CONTEXT", "LIVE_CREDENTIAL_INHERITANCE", "LIVE_DATABASE_IDENTITY", "LIVE_SMTP",
    "LIVE_SMTP_CONFIGURATION", "LIVE_SSO", "LIVE_SSO_CONFIGURATION", "PRIVATE_CONTEXT_PATH",
    "PRIVILEGED_SERVICE", "PROJECT_NAMESPACE", "PROTECTED_BIND_PATH", "READ_ONLY_DOCKER_COMMAND_FAILED",
    "RESOURCE_LABEL", "SERVICE_LABEL", "SHARED_NETWORKS", "SHARED_OR_PUBLIC_PORT", "SHARED_VOLUMES",
    "UNBOUNDED_MOUNT", "UNBOUNDED_SERVICE", "UNKNOWN_NETWORK", "UNKNOWN_OR_PROTECTED_VOLUME",
    "UNOWNED_BIND_PATH", "UNRESOLVED_ENVIRONMENT",
})


class ComposePreflightError(ValueError):
    """A diagnostic code without resolved secrets or host paths."""

    def __init__(self, code: str) -> None:
        if code not in PREFLIGHT_CODES:
            raise ValueError("UNKNOWN_PREFLIGHT_CODE")
        self.code = code
        super().__init__(code)


def require(condition: Any, code: str) -> None:
    if not condition:
        raise ComposePreflightError(code)


def overlaps(left: Path, right: Path) -> bool:
    left, right = left.resolve(), right.resolve()
    return left == right or left.is_relative_to(right) or right.is_relative_to(left)


def _bounded(value: Any) -> bool:
    try:
        return not isinstance(value, bool) and math.isfinite(float(value)) and float(value) > 0
    except (ValueError, TypeError, OverflowError):
        return False


def _inventory(run: Callable[[list[str]], str], project: str, *, existing: bool) -> dict:
    container_filter = f"name={project}" if existing else f"label=com.docker.compose.project={project}"
    identifiers = run(["docker", "ps", "-aq", "--filter", container_filter]).split()
    containers = json.loads(run(["docker", "inspect", *identifiers])) if identifiers else []
    inventories = {"containers": containers}
    for kind in ("volume", "network"):
        resource_filter = f"name=^{project}_" if existing else f"label=com.docker.compose.project={project}"
        names = run(["docker", kind, "ls", "-q", "--filter", resource_filter]).split()
        inventories[kind + "s"] = json.loads(run(["docker", kind, "inspect", *names])) if names else []
    return inventories


def _credential_value(key: str, value: str) -> bool:
    """Distinguish credential material from named location/configuration metadata.

    The same logical key-file path is expected in separate private volumes.
    Resource and mount isolation are validated independently below. Unknown
    secret-related settings remain protected unless their metadata semantics
    and literal value are both recognized.
    """
    key = key.upper()
    if not re.search(r"PASSWORD|PASSWD|SECRET|TOKEN|PRIVATE_KEY|CLIENT_ID|API_KEY|ACCESS_KEY|AUTH_KEY", key):
        return False
    suffix = key.rsplit("_", 1)[-1]
    if suffix in {"DIR", "DIRECTORY", "FILE", "PATH"} and (
        value.startswith("/") or re.match(r"^[A-Za-z]:[\\/]", value)
    ):
        return False
    if suffix in {"ENABLED", "DISABLED", "REQUIRED"} and value.lower() in {"true", "false", "yes", "no", "on", "off", "0", "1"}:
        return False
    if suffix in {"TTL", "EXPIRY", "EXPIRES", "SECONDS", "TIMEOUT", "LIMIT", "COUNT", "LENGTH"} and re.fullmatch(r"[0-9]+(?:\.[0-9]+)?", value):
        return False
    if suffix == "SCHEME" and value.lower() in {"bearer", "basic", "digest", "none"}:
        return False
    return not (suffix == "MODE" and value.lower() in {"strict", "optional", "disabled", "enabled", "development", "production", "test", "local", "read", "write", "auto"})


def _url_passwords(value: str) -> set[str]:
    """Include decoded URL userinfo so renaming/encoding cannot hide reuse."""
    if "://" not in value:
        return set()
    try:
        password = urlparse(value).password
    except ValueError:
        return set()
    if not password:
        return set()
    result = {password}
    try:
        result.add(unquote(password, errors="strict"))
    except UnicodeDecodeError:
        pass  # Preserve the encoded credential even when decoding is invalid.
    return result


def _protected(baseline: dict) -> tuple[set[str], set[str], set[int], list[Path], set[str]]:
    volumes = {item if isinstance(item, str) else item["Name"] for item in baseline.get("volumes", [])}
    networks = {item if isinstance(item, str) else item["Name"] for item in baseline.get("networks", [])}
    ports, paths, secrets = {3100}, [], set()
    for container in baseline.get("containers", []):
        networks.update(container.get("networks", container.get("NetworkSettings", {}).get("Networks", {})))
        bindings = container.get("ports", container.get("HostConfig", {}).get("PortBindings", {})) or {}
        ports.update(int(item["HostPort"]) for values in bindings.values() for item in values or [])
        for mount in container.get("mounts", container.get("Mounts", [])):
            kind, source = mount.get("type", mount.get("Type")), mount.get("source", mount.get("Source"))
            name = mount.get("name", mount.get("Name"))
            if kind == "volume" and name:
                volumes.add(name)
            elif kind == "bind" and source:
                paths.append(Path(source))
        for entry in container.get("Config", {}).get("Env", []):
            key, _, value = entry.partition("=")
            if value and _credential_value(key, value):
                secrets.add(value)
            secrets.update(_url_passwords(value))
    return volumes, networks, ports, paths, secrets


def _environment(service: str, values: dict, project: str, protected_secrets: set[str], *, mock: bool) -> None:
    require(isinstance(values, dict), "UNRESOLVED_ENVIRONMENT")
    candidates = {str(value) for value in values.values() if value is not None and str(value)}
    require(not any(({value} | _url_passwords(value)) & protected_secrets for value in candidates), "LIVE_CREDENTIAL_INHERITANCE")
    require(str(values.get("TRACKVANCE_SMTP_ENABLED", "false")).lower() == "false", "LIVE_SMTP")
    require(not any(value for key, value in values.items() if key.startswith("TRACKVANCE_SMTP_") and key != "TRACKVANCE_SMTP_ENABLED"), "LIVE_SMTP_CONFIGURATION")
    if service in APP_SERVICES:
        require(values.get("TRACKVANCE_CERTIFICATION_PROJECT") == project, "APP_PROJECT_MARKER")
    if service == "postgres":
        require(values.get("POSTGRES_USER") == "tv_v070_test"
                and values.get("POSTGRES_DB") == "tv_v070_test", "LIVE_DATABASE_IDENTITY")
    if values.get("DATABASE_URL"):
        database = urlparse(str(values["DATABASE_URL"]))
        require(database.hostname == "postgres" and database.username == "tv_v070_test"
                and database.path == "/tv_v070_test", "LIVE_DATABASE_IDENTITY")
    for provider in ("MICROSOFT", "GOOGLE"):
        prefix = "TRACKVANCE_SSO_" + provider + "_"
        enabled = str(values.get(prefix + "ENABLED", "false")).lower() == "true"
        if enabled:
            require(mock and service == "api" and str(values.get("TRACKVANCE_SSO_TEST_MODE")).lower() == "true", "LIVE_SSO")
            discovery = urlparse(str(values.get(prefix + "DISCOVERY_URL", "")))
            require(discovery.scheme == "http" and discovery.hostname == "mock-oidc" and discovery.port == 9000
                    and discovery.path == "/" + provider.lower() + "/.well-known/openid-configuration"
                    and values.get(prefix + "CLIENT_ID") == "trackvance-disposable-client", "LIVE_SSO_CONFIGURATION")
            permitted = {prefix + suffix for suffix in ("ENABLED", "CLIENT_ID", "CLIENT_SECRET", "DISCOVERY_URL")}
            require(not any(value for key, value in values.items() if key.startswith(prefix) and key not in permitted), "LIVE_SSO_CONFIGURATION")
        else:
            require(not any(value for key, value in values.items() if key.startswith(prefix) and key != prefix + "ENABLED"), "LIVE_SSO_CONFIGURATION")
    require(mock or str(values.get("TRACKVANCE_SSO_TEST_MODE", "false")).lower() == "false", "IMPLICIT_MOCK_OIDC")


def validate_resolved(config: dict, *, project: str, directory: Path, main_inventory: dict,
                      existing: dict | None = None, allow_mock_oidc: bool = False, root: Path = ROOT) -> None:
    """Validate every resolved service/resource; accept only owned existing restarts."""
    require(PROJECT.fullmatch(project) and config.get("name") == project, "PROJECT_NAMESPACE")
    root = root.resolve()
    require(not directory.is_symlink() and not getattr(directory, "is_junction", lambda: False)(), "LINKED_PRIVATE_CONTEXT")
    directory = directory.resolve()
    require(directory.is_dir() and directory.is_relative_to(root / ".codex-local"), "PRIVATE_CONTEXT_PATH")
    current = root / ".codex-local"
    for component in directory.relative_to(current).parts:
        current = current / component
        require(not current.is_symlink() and not getattr(current, "is_junction", lambda: False)(), "LINKED_PRIVATE_CONTEXT")
    protected_volumes, protected_networks, protected_ports, protected_paths, protected_secrets = _protected(main_inventory)
    require(not config.get("secrets") and not config.get("configs"), "HOST_CONFIG_OR_SECRET")
    for kind, protected in (("volumes", protected_volumes), ("networks", protected_networks)):
        seen = set()
        for descriptor in config.get(kind, {}).values():
            name = descriptor.get("name", "")
            require(name.startswith(project + "_") and name not in protected and name not in seen
                    and not descriptor.get("external") and not descriptor.get("driver_opts"), "SHARED_" + kind.upper())
            require(descriptor.get("labels", {}).get("com.docker.compose.project", project) == project, "RESOURCE_LABEL")
            if kind == "networks":
                require(descriptor.get("driver") in {None, "bridge"}, "HOST_NETWORK_DRIVER")
            seen.add(name)
    services = config.get("services", {})
    require(services, "EMPTY_SERVICES")
    mock = allow_mock_oidc and "mock-oidc" in services
    require("mock-oidc" not in services or mock, "IMPLICIT_MOCK_OIDC")
    if mock:
        mock_service = services["mock-oidc"]
        mock_environment = mock_service.get("environment", {})
        require(mock_service.get("command") == ["python", "/test/mock_oidc.py"]
                and mock_environment.get("MOCK_OIDC_INTERNAL_URL") == "http://mock-oidc:9000", "INVALID_MOCK_OIDC")
        public = urlparse(str(mock_environment.get("MOCK_OIDC_PUBLIC_URL", "")))
        require(public.scheme == "http" and public.hostname in {"localhost", "127.0.0.1", "::1"}, "INVALID_MOCK_OIDC")
        secret = mock_environment.get("MOCK_OIDC_CLIENT_SECRET")
        values = services.get("api", {}).get("environment", {})
        require(secret and all(values.get("TRACKVANCE_SSO_" + provider + "_CLIENT_SECRET") == secret
                for provider in ("GOOGLE", "MICROSOFT")
                if str(values.get("TRACKVANCE_SSO_" + provider + "_ENABLED", "false")).lower() == "true"), "INVALID_MOCK_OIDC")
    allowed_code = [root / relative for relative in ("backend/src", "backend/migrations", "scripts")]

    def readonly_code(candidate: Path, name: str, target: str) -> bool:
        return any(candidate.is_relative_to(code.resolve()) for code in allowed_code) or (
            name == "web" and candidate == (root / "deploy/docker/nginx.benchmark.conf").resolve()
            and target == "/etc/nginx/conf.d/default.conf")

    for name, service in services.items():
        require(not service.get("privileged") and not service.get("cap_add") and not service.get("devices"), "PRIVILEGED_SERVICE")
        require(service.get("network_mode") in {None, "none"} and service.get("pid") is None
                and service.get("ipc") in {None, "private"}, "HOST_NAMESPACE")
        require(service.get("restart", "no") == "no", "AUTOMATIC_RESTART")
        require(all(_bounded(service.get(key)) for key in ("cpus", "mem_limit", "pids_limit")), "UNBOUNDED_SERVICE")
        require(not service.get("container_name") or service["container_name"].startswith(project + "-"), "CONTAINER_NAMESPACE")
        require(service.get("labels", {}).get("com.docker.compose.project", project) == project, "SERVICE_LABEL")
        require(not service.get("env_file"), "AMBIENT_ENV_FILE")
        require(set(service.get("networks", {})) <= set(config.get("networks", {})), "UNKNOWN_NETWORK")
        for port in service.get("ports", []):
            published = int(port.get("published", 0))
            require(1024 <= published <= 65535 and published not in protected_ports
                    and port.get("host_ip") in {"127.0.0.1", "::1"}, "SHARED_OR_PUBLIC_PORT")
        for mount in service.get("volumes", []):
            source, target = mount.get("source", ""), mount.get("target", "")
            require(not any(token in (source + " " + target).lower() for token in ("docker.sock", "docker_engine", "containerd.sock")), "HOST_CONTROL_SOCKET")
            if mount.get("type") == "volume":
                require(source in config.get("volumes", {}) and source not in protected_volumes, "UNKNOWN_OR_PROTECTED_VOLUME")
            elif mount.get("type") == "bind":
                candidate = Path(source).resolve()
                require(not any(overlaps(candidate, path) for path in protected_paths), "PROTECTED_BIND_PATH")
                require(candidate.is_relative_to(directory) or
                        (mount.get("read_only") is True and readonly_code(candidate, name, target)), "UNOWNED_BIND_PATH")
            else:
                require(mount.get("type") == "tmpfs" and mount.get("tmpfs", {}).get("size", 0) > 0, "UNBOUNDED_MOUNT")
        _environment(name, service.get("environment", {}), project, protected_secrets, mock=mock)
    for kind in ("volumes", "networks"):
        for resource in (existing or {}).get(kind, []):
            require(resource.get("Labels", {}).get("com.docker.compose.project") == project
                    and resource.get("Name", "").startswith(project + "_"), "EXISTING_RESOURCE_NOT_OWNED")
    for container in (existing or {}).get("containers", []):
        labels = container.get("Config", {}).get("Labels", {})
        require(labels.get("com.docker.compose.project") == project
                and container.get("Name", "").lstrip("/").startswith(project + "-"), "EXISTING_CONTAINER_NOT_OWNED")
        require(labels.get("com.docker.compose.service") in services, "EXISTING_SERVICE_NOT_OWNED")
        for mount in container.get("Mounts", []):
            if mount.get("Type") == "volume":
                require(mount.get("Name", "").startswith(project + "_")
                        and mount.get("Name") not in protected_volumes, "EXISTING_PROTECTED_VOLUME")
            elif mount.get("Type") == "bind":
                candidate = Path(mount.get("Source", "")).resolve()
                require(not any(overlaps(candidate, path) for path in protected_paths), "EXISTING_PROTECTED_BIND")
                require(candidate.is_relative_to(directory) or
                        (mount.get("RW") is False and readonly_code(candidate, labels["com.docker.compose.service"],
                                                                    mount.get("Destination", ""))), "EXISTING_UNOWNED_BIND")
        permitted_networks = {item["name"] for item in config.get("networks", {}).values()}
        require(set(container.get("NetworkSettings", {}).get("Networks", {})) <= permitted_networks, "EXISTING_UNOWNED_NETWORK")
        environment = dict(entry.split("=", 1) for entry in container.get("Config", {}).get("Env", []))
        _environment(labels["com.docker.compose.service"], environment, project, protected_secrets, mock=mock)


def preflight(compose: list[str], environment: dict[str, str] | None = None, *, project: str,
              directory: Path, allow_mock_oidc: bool = False,
              run: Callable[[list[str]], str] | None = None) -> None:
    """Resolve and inspect only when a caller explicitly opts into CI images."""
    values = os.environ if environment is None else environment
    if not values.get("TRACKVANCE_CI_IMAGE_MANIFEST"):
        return
    require(compose[:2] == ["docker", "compose"], "COMPOSE_ARGUMENTS")
    positions = [index for index, token in enumerate(compose) if token in {"-p", "--project-name"}]
    require(len(positions) == 1 and compose[positions[0] + 1] == project, "COMPOSE_PROJECT_ARGUMENT")
    require(compose.count("--env-file") == 1, "EXPLICIT_PRIVATE_ENV")
    env_file = Path(compose[compose.index("--env-file") + 1])
    require(env_file.is_file() and not env_file.is_symlink() and env_file.resolve().is_relative_to(directory.resolve()), "EXPLICIT_PRIVATE_ENV")

    def invoke(arguments: list[str]) -> str:
        if run is not None:
            return run(arguments)
        result = subprocess.run(arguments, cwd=ROOT, env=values, text=True, encoding="utf-8",
                                capture_output=True, check=False, timeout=60)
        require(result.returncode == 0, "READ_ONLY_DOCKER_COMMAND_FAILED")
        return result.stdout

    try:
        config = json.loads(invoke([*compose, "config", "--format", "json"]))
        baseline = _inventory(invoke, MAIN_PROJECT, existing=False)
        owned = _inventory(invoke, project, existing=True)
        validate_resolved(config, project=project, directory=directory, main_inventory=baseline,
                          existing=owned, allow_mock_oidc=allow_mock_oidc, root=ROOT)
    except ComposePreflightError:
        raise
    except (ValueError, KeyError, TypeError, IndexError, subprocess.TimeoutExpired):
        raise ComposePreflightError("INVALID_OR_UNAVAILABLE_RESOLVED_CONFIG") from None
