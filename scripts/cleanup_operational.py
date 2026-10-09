"""Exceptional selective cleanup, supervised by live Docker inventory.

No default project, dataset wildcard, reset, prune, volume removal, connection
probe or external target operation is provided. Each one-shot helper uses an
explicit certified image digest, finite resources and a private stdin protocol.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
from pathlib import Path
from threading import Timer
from urllib.parse import quote
from uuid import uuid4

import docker_state

ROOT = Path(__file__).resolve().parent.parent


def require_quiescence(project: str, baseline: dict | None = None) -> dict:
    state = docker_state.inventory(project)
    docker_state.require_backup_inventory(state)
    if baseline is not None and docker_state._resource_identity(state) != docker_state._resource_identity(baseline):
        raise ValueError("Cleanup project resource IDs changed; regenerate the plan.")
    active = [item for item in state["containers"] if item["running"]]
    if len(active) != 1 or active[0]["service"] != "postgres":
        raise ValueError("Only PostgreSQL may run; stop API, web and every dispatcher before planning.")
    return state


def inspect_one(kind: str, identity: str) -> dict:
    rows = docker_state.docker_json([kind, "inspect", identity])
    if len(rows) != 1:
        raise ValueError("Exactly one Docker resource was expected.")
    return rows[0]


def helper_options(project: str, image: str, source_commit: str, state: dict, quarantine_parent: Path | None,
                   backup: Path | None, *, write: bool) -> tuple[list[str], str, str]:
    if not re.fullmatch(r"sha256:[a-f0-9]{64}", image) or not re.fullmatch(r"[a-f0-9]{40}", source_commit):
        raise ValueError("An immutable certified image and source commit are required.")
    labels = inspect_one("image", image).get("Config", {}).get("Labels") or {}
    if labels.get("org.opencontainers.image.version") != "0.8.5" or labels.get("org.opencontainers.image.revision") != source_commit:
        raise ValueError("The helper image is not the specified certified 0.8.5 commit.")
    postgres = docker_state._container_for(state, "postgres")
    detail = inspect_one("container", str(postgres["id"]))
    values = dict(item.split("=", 1) for item in detail["Config"]["Env"] if "=" in item)
    db_url = "postgresql+psycopg://" + quote(values["POSTGRES_USER"], safe="") + ":" + quote(values["POSTGRES_PASSWORD"], safe="")
    db_url += "@postgres:5432/" + quote(values["POSTGRES_DB"], safe="")
    if len(state["networks"]) != 1:
        raise ValueError("A single owned project network is required.")
    network = state["networks"][0]["name"]
    api = inspect_one("container", str(docker_state._container_for(state, "api")["id"]))
    volume = docker_state._volume_for(state, "trackvance_data")
    mounts = [item for item in api["Mounts"] if item.get("Type") == "volume" and item.get("Name") == volume]
    if len(mounts) != 1:
        raise ValueError("The API does not have exactly the owned operational data volume.")
    storage = mounts[0]["Destination"]
    arguments = ["--network", network, "--memory", "512m", "--cpus", "0.5", "--pids-limit", "128",
        "--read-only", "--cap-drop", "ALL", "--security-opt", "no-new-privileges", "--tmpfs", "/tmp:rw,size=64m",
        "--mount", f"type=volume,src={volume},dst={storage}" + ("" if write else ",readonly"),
        "--mount", f"type=bind,src={ROOT / 'scripts'},dst=/cleanup-scripts,readonly"]
    if backup:
        arguments += ["--mount", f"type=bind,src={backup.resolve(strict=True)},dst=/cleanup-backup,readonly"]
    if quarantine_parent:
        parent = quarantine_parent.resolve(strict=True)
        if not parent.is_dir() or parent.is_symlink():
            raise ValueError("Quarantine parent must be an existing explicit directory.")
        arguments += ["--mount", f"type=bind,src={parent},dst=/cleanup-quarantine" + ("" if write else ",readonly")]
    return arguments, db_url, storage


def execute(request: dict, *, image: str, source_commit: str, backup: Path | None = None,
            quarantine_parent: Path | None = None) -> dict:
    if quarantine_parent:
        match = re.fullmatch(r"/cleanup-quarantine/([a-zA-Z0-9_-]{1,100})", request.get("quarantine", ""))
        if not match:
            raise ValueError("An explicit simple quarantine target is required.")
        target = (quarantine_parent.resolve(strict=True) / match[1]).resolve()
        if backup:
            source = backup.resolve(strict=True)
            if target.is_relative_to(source) or source.is_relative_to(target):
                raise ValueError("Backup and quarantine host paths overlap; preserve the backup outside quarantine.")
    project = request["project"]
    baseline = require_quiescence(project)
    if request["action"] == "apply" and request["plan"].get("host_inventory") != docker_state._resource_identity(baseline):
        raise ValueError("The plan's host resource IDs do not match the live installation.")
    if backup:
        manifest = docker_state.verify_backup(backup)
        if manifest.get("source_project") != project:
            raise ValueError("The verified backup belongs to another project.")
    options, db_url, storage = helper_options(project, image, source_commit, baseline, quarantine_parent, backup,
        write=request["action"] in {"apply", "recover"})
    nonce = uuid4().hex
    name = "trackvance-cleanup-" + nonce
    arguments = ["docker", "run", "--rm", "--pull", "never", "-i", "--name", name,
        "--label", "trackvance.cleanup.owner=" + nonce, *options, "--entrypoint", "python", image,
        "-m", "trackvance.operational_cleanup_cli"]
    # Credentials travel only through this pipe, never Docker argv/env or logs.
    process = subprocess.Popen(arguments, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
    deadline = Timer(900, process.kill)
    deadline.daemon = True
    deadline.start()
    result = None
    try:
        assert process.stdin is not None and process.stdout is not None
        process.stdin.write(json.dumps({**request, "database_url": db_url, "storage": storage}) + "\n")
        process.stdin.flush()
        for line in process.stdout:
            value = json.loads(line)
            if value.get("kind") == "quiescence_request":
                require_quiescence(project, baseline)
                process.stdin.write(json.dumps({"nonce": value["nonce"], "quiescent": True}) + "\n")
                process.stdin.flush()
            elif value.get("kind") == "result":
                result = value["result"]
            elif value.get("kind") == "failure":
                raise ValueError("Native cleanup failed: " + value.get("error_code", "CLEANUP_FAILED"))
            else:
                raise ValueError("Unexpected helper protocol output.")
        if process.wait(timeout=30) != 0 or result is None:
            raise ValueError("The cleanup helper did not return a successful result.")
        if request["action"] == "plan":
            result["host_inventory"] = docker_state._resource_identity(baseline)
            result["plan_sha256"] = docker_state.canonical_hash({key: value for key, value in result.items() if key != "plan_sha256"})
        return result
    finally:
        deadline.cancel()
        if process.poll() is None:
            process.kill()
            process.wait(timeout=30)
        # This is the only removable Docker resource: a verified tool-owned
        # one-shot helper, never a Compose service, volume, network or image.
        found = subprocess.run(["docker", "container", "inspect", name], capture_output=True, text=True, check=False)
        if found.returncode == 0:
            helper = json.loads(found.stdout)[0]
            if helper["Name"].lstrip("/") != name or (helper["Config"].get("Labels") or {}).get("trackvance.cleanup.owner") != nonce:
                raise ValueError("Helper identity changed; refusing automatic removal.")
            subprocess.run(["docker", "rm", "--force", helper["Id"]], capture_output=True, check=True)
        require_quiescence(project, baseline)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("plan", "apply", "recover"))
    parser.add_argument("--project", required=True)
    parser.add_argument("--image", required=True)
    parser.add_argument("--source-commit", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--organization-id")
    parser.add_argument("--dataset-id", action="append", default=[])
    parser.add_argument("--plan", type=Path)
    parser.add_argument("--backup", type=Path)
    parser.add_argument("--actor-id")
    parser.add_argument("--restore-receipt", type=Path)
    parser.add_argument("--quarantine-parent", type=Path)
    parser.add_argument("--quarantine-name")
    options = parser.parse_args()
    request = {"action": options.action, "project": options.project}
    if options.output.exists() or options.output.is_symlink():
        parser.error("Output must be a fresh explicit file.")
    if options.action == "plan":
        if not options.organization_id or not options.dataset_id:
            parser.error("Plan requires organization and explicit confirmed test dataset IDs.")
        request.update(organization_id=options.organization_id, dataset_ids=options.dataset_id)
    else:
        if not options.quarantine_parent or not re.fullmatch(r"[a-zA-Z0-9_-]{1,100}", options.quarantine_name or ""):
            parser.error("Apply/recover require an existing quarantine parent and a simple unique name.")
        request["quarantine"] = "/cleanup-quarantine/" + options.quarantine_name
        if options.action == "apply":
            if not options.plan or not options.backup or not options.actor_id or not options.restore_receipt:
                parser.error("Apply requires a sealed plan, verified backup, real restore receipt and active administrator ID.")
            request.update(plan=json.loads(options.plan.read_text(encoding="utf-8-sig")), actor_id=options.actor_id,
                restore_receipt=json.loads(options.restore_receipt.read_text(encoding="utf-8-sig")))
    result = execute(request, image=options.image, source_commit=options.source_commit,
        backup=options.backup, quarantine_parent=options.quarantine_parent)
    with os.fdopen(os.open(options.output, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600), "w", encoding="utf-8") as stream:
        json.dump(result, stream, indent=2, ensure_ascii=False)
    print(json.dumps({"status": "PASS", "action": options.action, "result": str(options.output)}))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (ValueError, OSError, KeyError, subprocess.SubprocessError, docker_state.OperationError) as exc:
        print(json.dumps({"status": "FAIL", "error_type": type(exc).__name__}))
        raise SystemExit(1) from None
