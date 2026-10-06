"""Deadline fallback limited to newly created, explicitly labelled CI resources."""
from __future__ import annotations

import json
import re
import subprocess

PROJECT = re.compile(r"trackvance-(?:v0[78]0-test-[a-z0-9-]+|bench|delivery-bench|e2e|identity-e2e|connections-e2e|delivery-e2e)-[a-f0-9]{12}")
SPARK_OWNER = "io.trackvance.spark-proof-owner"


def docker(*args: str) -> str:
    result = subprocess.run(["docker", *args], capture_output=True, text=True,
                            encoding="utf-8", check=False, timeout=90)
    if result.returncode:
        raise ValueError("Owned resource cleanup operation failed.")
    return result.stdout


def snapshot() -> dict[str, set[str]]:
    return {"containers": set(docker("ps", "-aq", "--no-trunc").split()),
            "volumes": set(docker("volume", "ls", "-q").split()),
            "networks": set(docker("network", "ls", "-q", "--no-trunc").split())}


def owner(labels: dict | None) -> str | None:
    labels = labels or {}
    project = labels.get("com.docker.compose.project") or labels.get(SPARK_OWNER)
    return project if isinstance(project, str) and PROJECT.fullmatch(project) else None


def cleanup(before: dict[str, set[str]], *, projects: set[str] | None = None) -> dict:
    after = snapshot()
    removed = {"containers": [], "volumes": [], "networks": []}
    for identifier in sorted(after["containers"] - before["containers"]):
        rows = json.loads(docker("inspect", identifier))
        if len(rows) != 1 or rows[0].get("Id") != identifier:
            raise ValueError("Container identity changed during cleanup.")
        project = owner(rows[0].get("Config", {}).get("Labels"))
        if not project or (projects is not None and project not in projects):
            continue
        # Recheck immediately before mutation; source/habitual resources cannot
        # match the UUID ownership namespace and preexisting IDs are excluded.
        check = json.loads(docker("inspect", identifier))[0]
        if check.get("Id") != identifier or owner(check.get("Config", {}).get("Labels")) != project:
            raise ValueError("Container ownership changed during cleanup.")
        if check.get("State", {}).get("Running"):
            docker("stop", "--time", "15", identifier)
        docker("rm", identifier)
        removed["containers"].append(identifier)
    for name in sorted(after["volumes"] - before["volumes"]):
        rows = json.loads(docker("volume", "inspect", name))
        if len(rows) != 1 or rows[0].get("Name") != name:
            raise ValueError("Volume identity changed during cleanup.")
        project = owner(rows[0].get("Labels"))
        if not project or not name.startswith(project) or (projects is not None and project not in projects):
            continue
        if docker("ps", "-aq", "--filter", "volume=" + name).strip():
            raise ValueError("Owned volume still has a container consumer.")
        check = json.loads(docker("volume", "inspect", name))[0]
        if check.get("Name") != name or owner(check.get("Labels")) != project:
            raise ValueError("Volume ownership changed during cleanup.")
        docker("volume", "rm", name)
        removed["volumes"].append(name)
    for identifier in sorted(after["networks"] - before["networks"]):
        rows = json.loads(docker("network", "inspect", identifier))
        if len(rows) != 1 or rows[0].get("Id") != identifier:
            raise ValueError("Network identity changed during cleanup.")
        project = owner(rows[0].get("Labels"))
        if not project or not rows[0].get("Name", "").startswith(project) or (projects is not None and project not in projects):
            continue
        if rows[0].get("Containers"):
            raise ValueError("Owned network still has live endpoints.")
        check = json.loads(docker("network", "inspect", identifier))[0]
        if check.get("Id") != identifier or owner(check.get("Labels")) != project or check.get("Containers"):
            raise ValueError("Network identity or endpoints changed during cleanup.")
        docker("network", "rm", identifier)
        removed["networks"].append(identifier)
    return {"status": "PASS", "removed": removed, "preexisting_resources_excluded": True}
