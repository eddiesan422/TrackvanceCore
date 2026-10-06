"""Guard disposable certification against the observed installation's resources.

The resolved Compose document stays private: it can contain interpolated secrets.
Only the guarded resource inventory is suitable for shared evidence.
"""

from __future__ import annotations

import argparse
import json
import re
import secrets
import shutil
import socket
import subprocess
from pathlib import Path
from typing import Any
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
PRIVATE_ROOT = ROOT / ".codex-local" / "v080"
PREFIX = "trackvance-v080-test-"
PROJECT = re.compile(PREFIX + r"[a-z0-9-]{1,24}-[a-f0-9]{12}")
CORE_SERVICES = (
    "postgres", "api", "worker", "acquisition-worker", "delivery-worker",
    "report-worker", "scheduler", "events-notifications", "events-chaining", "web",
)


class IsolationError(ValueError):
    """Public error with no interpolated configuration or private data."""


def command(arguments: list[str], *, timeout: int = 300) -> str:
    result = subprocess.run(arguments, cwd=ROOT, text=True, encoding="utf-8",
                            capture_output=True, timeout=timeout, check=False)
    if result.returncode:
        raise IsolationError(f"La operación {arguments[0]} falló (exit={result.returncode}).")
    return result.stdout


def inventory(project: str) -> dict[str, Any]:
    if not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,100}", project):
        raise IsolationError("Identidad Compose inválida.")
    ids = command(["docker", "ps", "-aq", "--filter",
                   f"label=com.docker.compose.project={project}"]).split()
    containers = json.loads(command(["docker", "inspect", *ids])) if ids else []
    volume_names = command(["docker", "volume", "ls", "-q", "--filter",
                            f"label=com.docker.compose.project={project}"]).split()
    return {
        "project": project,
        "volumes": sorted(volume_names),
        "containers": sorted([
            {"id": row["Id"], "name": row["Name"], "image": row["Image"],
             "state": row["State"]["Status"],
             "service": row["Config"]["Labels"].get("com.docker.compose.service"),
             "project": row["Config"]["Labels"].get("com.docker.compose.project"),
             "mounts": sorted([
                 {"type": mount["Type"], "name": mount.get("Name"),
                  "source": mount["Source"], "target": mount["Destination"]}
                 for mount in row["Mounts"]], key=lambda mount: mount["target"]),
             "ports": row["HostConfig"].get("PortBindings") or {},
             "networks": sorted(row["NetworkSettings"]["Networks"]),
             "restart": row["HostConfig"]["RestartPolicy"]["Name"]}
            for row in containers], key=lambda row: row["name"]),
    }


def discover_main() -> str:
    projects = json.loads(command(["docker", "compose", "ls", "--all", "--format", "json"]))
    candidates = [row["Name"] for row in projects
                  if not row["Name"].startswith(("trackvance-v070-test-", PREFIX))
                  and "TrackvanceCore" in row.get("ConfigFiles", "")]
    if len(candidates) != 1:
        raise IsolationError("Selecciona --main-project desde el inventario Docker real.")
    return candidates[0]


def _paths_overlap(left: Path, right: Path) -> bool:
    left, right = left.resolve(), right.resolve()
    return left == right or left.is_relative_to(right) or right.is_relative_to(left)


def validate_resolved(config: dict[str, Any], context: dict[str, Any], directory: Path) -> None:
    """Validate *before* up, including named/external resources and bind mounts."""
    project = context["project"]
    if not PROJECT.fullmatch(project) or config.get("name") != project:
        raise IsolationError("La configuración resuelta no pertenece al proyecto aislado.")
    baseline = context["main_before"]
    protected_volumes = set(baseline["volumes"])
    protected_paths: list[Path] = []
    protected_networks: set[str] = set()
    protected_ports: set[int] = set()
    for container in baseline["containers"]:
        protected_networks.update(container["networks"])
        for bindings in container["ports"].values():
            for binding in bindings or []:
                protected_ports.add(int(binding["HostPort"]))
        for mount in container["mounts"]:
            if mount["type"] == "volume" and mount.get("name"):
                protected_volumes.add(mount["name"])
            elif mount["type"] == "bind":
                protected_paths.append(Path(mount["source"]))
    for entry in config.get("volumes", {}).values():
        name = entry.get("name", "")
        if entry.get("external") or name in protected_volumes or not name.startswith(project + "_"):
            raise IsolationError("Volumen compartido o ajeno a la certificación.")
    for entry in config.get("networks", {}).values():
        name = entry.get("name", "")
        if entry.get("external") or name in protected_networks or not name.startswith(project + "_"):
            raise IsolationError("Red compartida o ajena a la certificación.")
    # Compose configs/secrets may themselves be files on the usual installation.
    if config.get("secrets") or config.get("configs"):
        raise IsolationError("La certificación usa únicamente secretos sintéticos propios.")
    total_memory = 0
    for service in config.get("services", {}).values():
        if service.get("privileged") or service.get("network_mode") not in {None, "none"}:
            raise IsolationError("El entorno aislado no admite privilegios ni red del host.")
        if service.get("restart", "no") != "no":
            raise IsolationError("La certificación no modifica la política de autoarranque.")
        if not service.get("mem_limit") or not service.get("cpus"):
            raise IsolationError("Cada servicio de certificación requiere límites CPU/RAM.")
        total_memory += int(service["mem_limit"])
        for port in service.get("ports", []):
            published = int(port.get("published", 0))
            if published in protected_ports or not 32000 <= published <= 32999:
                raise IsolationError("Puerto compartido o fuera del rango aislado.")
            if port.get("host_ip") not in {"127.0.0.1", "::1"}:
                raise IsolationError("Los puertos de certificación sólo se publican en loopback.")
        for mount in service.get("volumes", []):
            source = mount.get("source", "")
            if "docker.sock" in source or "docker.sock" in mount.get("target", ""):
                raise IsolationError("La certificación no monta el socket Docker.")
            if mount["type"] == "volume":
                if source in protected_volumes or source not in config.get("volumes", {}):
                    raise IsolationError("Un servicio monta un volumen protegido o desconocido.")
            elif mount["type"] == "bind":
                candidate = Path(source).resolve()
                if any(_paths_overlap(candidate, path) for path in protected_paths):
                    raise IsolationError("Un bind mount solapa la instalación habitual.")
                permitted_code = (ROOT / "backend" / "src", ROOT / "backend" / "migrations", ROOT / "scripts")
                is_code = candidate in [path.resolve() for path in permitted_code]
                if not candidate.is_relative_to(directory.resolve()) and not (is_code and mount.get("read_only")):
                    raise IsolationError("Bind mount fuera del contexto privado o código de sólo lectura.")
            else:
                raise IsolationError("Tipo de montaje no admitido por el preflight.")
        env = service.get("environment", {})
        for key in ("TRACKVANCE_SSO_MICROSOFT_ENABLED", "TRACKVANCE_SSO_GOOGLE_ENABLED"):
            if str(env.get(key, "false")).lower() != "false":
                raise IsolationError("SSO real no está permitido en la certificación sintética.")
    if total_memory > 9 * 1024**3:
        raise IsolationError("El proyecto supera el presupuesto conjunto de 9 GiB.")


def load_context(directory: Path) -> tuple[Path, dict[str, Any]]:
    directory = directory.resolve(strict=True)
    if not directory.is_relative_to(PRIVATE_ROOT.resolve()):
        raise IsolationError("El contexto debe estar dentro de .codex-local/v080.")
    context = json.loads((directory / "isolation.json").read_text(encoding="utf-8"))
    if not PROJECT.fullmatch(context["project"]) or context["project"] == context["main_project"]:
        raise IsolationError("Identidad aislada inválida.")
    return directory, context


def compose_args(directory: Path, context: dict[str, Any]) -> list[str]:
    return ["docker", "compose", "--project-name", context["project"], "--env-file",
            str(directory / "test.env"), "-f", str(ROOT / "compose.yml"),
            "-f", str(directory / "compose.json")]


def preflight(directory: Path, context: dict[str, Any]) -> dict[str, Any]:
    if inventory(context["main_project"]) != context["main_before"]:
        raise IsolationError("La instalación habitual cambió; renueva el inventario antes de continuar.")
    config = json.loads(command([*compose_args(directory, context), "config", "--format", "json"]))
    validate_resolved(config, context, directory)
    return {"status": "PASS", "project": context["project"],
            "services": sorted(config["services"]), "main_unchanged": True}


def init(suite: str, port: int, main_project: str | None) -> Path:
    from ci_images import verified_images
    images = verified_images()
    if not re.fullmatch(r"[a-z0-9-]{1,24}", suite) or not 32000 <= port <= 32999:
        raise IsolationError("Suite/puerto fuera de la certificación aislada.")
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", port))
    if shutil.disk_usage(ROOT).free < 20 * 1024**3:
        raise IsolationError("Se requieren al menos 20 GiB libres.")
    project = PREFIX + suite + "-" + uuid4().hex[:12]
    directory = PRIVATE_ROOT / project
    directory.mkdir(parents=True, exist_ok=False)
    directory.chmod(0o700)
    main_project = main_project or discover_main()
    context = {"project": project, "main_project": main_project, "port": port,
               "main_before": inventory(main_project),
               "image": images['backend'] if images else "trackvance-v080-isolated:backend"}
    env = {"POSTGRES_USER": "tv_v080_test", "POSTGRES_DB": "tv_v080_test",
           "POSTGRES_PASSWORD": secrets.token_urlsafe(36), "WEB_PORT": str(port),
           "TRACKVANCE_WEB_ORIGIN": f"http://localhost:{port}",
           "DEMO_ACCESS_ENABLED": "true", "DEMO_SEED_ENABLED": "false",
           "TRACKVANCE_SSO_MICROSOFT_ENABLED": "false", "TRACKVANCE_SSO_GOOGLE_ENABLED": "false"}
    (directory / "test.env").write_text("\n".join(f"{k}={v}" for k, v in env.items()) + "\n", encoding="utf-8")
    (directory / "test.env").chmod(0o600)
    resources = {"postgres": (512, 1), "api": (1024, 1), "worker": (2048, 2),
                 "acquisition-worker": (1024, 1), "delivery-worker": (1024, 1),
                 "report-worker": (2048, 2),
                 "scheduler": (192, 0.25), "events-notifications": (192, 0.25),
                 "events-chaining": (192, 0.25), "web": (128, 0.5)}
    override: dict[str, Any] = {"services": {}}
    for name, (mib, cpus) in resources.items():
        service: dict[str, Any] = {"mem_limit": f"{mib}m", "cpus": cpus, "pids_limit": 512}
        if name not in {"postgres", "web"}:
            service["image"] = context["image"]
            service["environment"] = {"PYTHONPATH": "/app/backend/src",
                                      "TRACKVANCE_CERTIFICATION_PROJECT": project}
            service["volumes"] = [
                {"type": "bind", "source": str(ROOT / relative), "target": "/app/" + relative,
                 "read_only": True} for relative in ("backend/src", "backend/migrations", "scripts")]
        elif name == "web":
            service["image"] = images['web'] if images else "trackvance-v080-isolated:web"
        override["services"][name] = service
    (directory / "compose.json").write_text(json.dumps(override, indent=2), encoding="utf-8")
    (directory / "isolation.json").write_text(json.dumps(context, indent=2), encoding="utf-8")
    preflight(directory, context)
    return directory


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("init", "preflight", "start", "stop", "inspect"))
    parser.add_argument("--suite", default="core")
    parser.add_argument("--port", type=int, default=32080)
    parser.add_argument("--main-project")
    parser.add_argument("--context", type=Path)
    parser.add_argument("--services", nargs="*")
    args = parser.parse_args()
    if args.action == "init":
        print(json.dumps({"context": str(init(args.suite, args.port, args.main_project))}))
        return
    if not args.context:
        parser.error("--context es obligatorio")
    directory, context = load_context(args.context)
    result = preflight(directory, context)
    if args.action == "start":
        config = json.loads(command([*compose_args(directory, context), "config", "--format", "json"]))
        services = args.services or list(CORE_SERVICES)
        if not set(services).issubset(config["services"]):
            raise IsolationError("Servicios desconocidos.")
        command([*compose_args(directory, context), "up", "--no-build", "--detach", "--wait",
                 "--wait-timeout", "180", *services])
    elif args.action == "stop":
        owned = inventory(context["project"])
        if any(row["project"] != context["project"] for row in owned["containers"]):
            raise IsolationError("La limpieza encontró recursos ajenos.")
        # The resolved names and labels were checked immediately above; only
        # this cycle's UUID project can own these disposable resources.
        command([*compose_args(directory, context), "down", "--volumes", "--remove-orphans"])
    elif args.action == "inspect":
        result = inventory(context["project"])
    if inventory(context["main_project"]) != context["main_before"]:
        raise IsolationError("La instalación habitual cambió durante la operación.")
    print(json.dumps(result))


if __name__ == "__main__":
    main()
