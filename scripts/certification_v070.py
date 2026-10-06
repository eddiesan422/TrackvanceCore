"""Guarded disposable stack for the 0.7.0 certification, never the user stack."""

import argparse
import json
import os
import re
import secrets
import shutil
import socket
import subprocess
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PREFIX = "trackvance-v070-test-"
SERVICES = ("postgres", "api", "worker", "acquisition-worker", "delivery-worker",
            "scheduler", "events-notifications", "events-chaining", "web")


def command(arguments, *, capture=True):
    result = subprocess.run(arguments, cwd=ROOT, text=True, encoding="utf-8", capture_output=capture, check=False)
    if result.returncode:
        # Compose output may contain interpolated secrets. Preserve it privately,
        # but surface only the operation and exit status in shared evidence.
        raise RuntimeError(f"La operación {arguments[0]} falló (exit={result.returncode}).")
    return result.stdout if capture else ""


def inventory(project):
    ids = command(["docker", "ps", "-aq", "--filter", f"label=com.docker.compose.project={project}"]).split()
    if not ids:
        return []
    inspected = json.loads(command(["docker", "inspect", *ids]))
    return sorted([{"id": row["Id"], "name": row["Name"], "image": row["Image"],
        "state": row["State"]["Status"], "service": row["Config"]["Labels"].get("com.docker.compose.service"),
        "project": row["Config"]["Labels"].get("com.docker.compose.project"),
        "mounts": sorted([{"type": mount["Type"], "name": mount.get("Name"), "source": mount["Source"],
                    "destination": mount["Destination"]} for mount in row["Mounts"]], key=lambda mount: mount["destination"]),
        "restart": row["HostConfig"]["RestartPolicy"]["Name"]} for row in inspected], key=lambda row: row["name"])


def load_context(directory):
    directory = directory.resolve()
    allowed = (ROOT / ".codex-local" / "v070").resolve()
    if not directory.is_relative_to(allowed):
        raise RuntimeError("El contexto debe estar dentro de .codex-local/v070.")
    context = json.loads((directory / "isolation.json").read_text(encoding="utf-8"))
    if not re.fullmatch(PREFIX + r"[a-z0-9-]+-[a-f0-9]{12}", context["project"]):
        raise RuntimeError("El proyecto no pertenece a la certificación aislada.")
    if context["port"] == 3100 or context["main_project"] == context["project"]:
        raise RuntimeError("El contexto comparte identidad con la instalación del usuario.")
    for name in ("test.env", "compose.json"):
        if not (directory / name).is_file():
            raise RuntimeError("El contexto de certificación está incompleto.")
    return directory, context


def compose(directory, context, arguments, *, capture=True):
    # Explicit env-file and -f suppress the repository's real .env/COMPOSE_FILE.
    args = ["docker", "compose", "--project-name", context["project"], "--env-file", str(directory / "test.env"),
            "-f", str(ROOT / "compose.yml"), "-f", str(directory / "compose.json")]
    if arguments and arguments[0] == "up":
        from ci.compose_preflight import preflight
        preflight(args, dict(os.environ), project=context["project"], directory=directory)
    return command([*args, *arguments], capture=capture)


def assert_main_unchanged(context):
    baseline = [{**row, "mounts": sorted(row["mounts"], key=lambda mount: mount["destination"])} for row in context["main_before"]]
    if inventory(context["main_project"]) != baseline:
        raise RuntimeError("El inventario de la instalación principal cambió durante la certificación.")


def init(suite, port):
    from ci_images import verified_images
    images = verified_images()
    if not re.fullmatch(r"[a-z0-9-]{1,24}", suite) or not 32000 <= port <= 32999:
        raise RuntimeError("Usa una suite corta y un puerto exclusivo 32000..32999.")
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", port))
    if shutil.disk_usage(ROOT).free < 20 * 1024**3:
        raise RuntimeError("La certificación requiere 20 GiB libres para snapshots, spill y evidencia.")
    project = PREFIX + suite + "-" + uuid.uuid4().hex[:12]
    directory = ROOT / ".codex-local" / "v070" / project
    directory.mkdir(parents=True, exist_ok=False)
    main_project = "trackvance-certification"
    context = {"project": project, "port": port, "main_project": main_project,
               "main_before": inventory(main_project),
               "image": images['backend'] if images else "trackvance-v070-isolated:backend"}
    env = {"POSTGRES_USER": "tv_v070_test", "POSTGRES_DB": "tv_v070_test",
           "POSTGRES_PASSWORD": secrets.token_urlsafe(36), "WEB_PORT": str(port),
           "TRACKVANCE_WEB_ORIGIN": f"http://localhost:{port}", "DEMO_ACCESS_ENABLED": "true",
           "DEMO_SEED_ENABLED": "false", "TRACKVANCE_SSO_MICROSOFT_ENABLED": "false",
           "TRACKVANCE_SSO_GOOGLE_ENABLED": "false"}
    (directory / "test.env").write_text("\n".join(f"{key}={value}" for key, value in env.items()) + "\n", encoding="utf-8")
    override = {"services": {}}
    resources = {"api": ("768m", 1), "worker": ("3g", 2),
                 "acquisition-worker": ("1536m", 2), "delivery-worker": ("1536m", 2),
                 "scheduler": ("192m", 0.5), "events-notifications": ("192m", 0.5),
                 "events-chaining": ("192m", 0.5)}
    for name in SERVICES:
        if name in {"postgres", "web"}:
            continue
        override["services"][name] = {"image": context["image"], "pids_limit": 512,
            "mem_limit": resources[name][0], "cpus": resources[name][1],
            "environment": {"PYTHONPATH": "/app/backend/src", "TRACKVANCE_CERTIFICATION_PROJECT": project},
            "volumes": [{"type": "bind", "source": str(ROOT / "backend" / "src"), "target": "/app/backend/src", "read_only": True},
                        {"type": "bind", "source": str(ROOT / "backend" / "migrations"), "target": "/app/backend/migrations", "read_only": True},
                        {"type": "bind", "source": str(ROOT / "scripts"), "target": "/app/scripts", "read_only": True}]}
    override["services"]["web"] = {"image": images['web'] if images else "trackvance-v070-isolated:web", "pids_limit": 128,
                                     "mem_limit": "128m", "cpus": 0.5}
    override["services"]["postgres"] = {"pids_limit": 256, "mem_limit": "512m", "cpus": 1}
    if images:
        # Historical scenarios do not enqueue report work. Keep the inherited
        # definition bounded without adding it to the explicitly started set.
        override["services"]["report-worker"] = {
            "image": images['backend'], "mem_limit": '256m', 'cpus': 0.25, 'pids_limit': 128,
            "environment": {"PYTHONPATH": "/app/backend/src", "TRACKVANCE_CERTIFICATION_PROJECT": project},
            "volumes": [{"type": "bind", "source": str(ROOT / relative),
                         "target": '/app/' + relative, "read_only": True}
                        for relative in ('backend/src', 'backend/migrations', 'scripts')]}
    from ci.local_resources import apply_limits
    apply_limits(override, project)
    (directory / "compose.json").write_text(json.dumps(override, indent=2), encoding="utf-8")
    (directory / "isolation.json").write_text(json.dumps(context, indent=2), encoding="utf-8")
    print(json.dumps({"context": str(directory), "project": project, "port": port}))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=("init", "start", "stop", "inspect", "assert-main"))
    parser.add_argument("--suite", default="core")
    parser.add_argument("--port", type=int, default=32070)
    parser.add_argument("--context", type=Path)
    parser.add_argument("--services", nargs="*", choices=SERVICES)
    args = parser.parse_args()
    if args.action == "init":
        init(args.suite, args.port)
        return
    if not args.context:
        parser.error("--context es obligatorio")
    directory, context = load_context(args.context)
    assert_main_unchanged(context)
    if args.action == "start":
        compose(directory, context, ["up", "--no-build", "--detach", "--wait", "--wait-timeout", "180", *(args.services or SERVICES)], capture=False)
    elif args.action == "stop":
        resources = inventory(context["project"])
        if any(item["project"] != context["project"] for item in resources):
            raise RuntimeError("El inventario contiene recursos ajenos al proyecto de prueba.")
        # Only this newly-created, guarded project owns these disposable volumes.
        compose(directory, context, ["down", "--volumes", "--remove-orphans"], capture=False)
    elif args.action == "inspect":
        print(json.dumps(inventory(context["project"]), indent=2))
    assert_main_unchanged(context)


if __name__ == "__main__":
    main()
