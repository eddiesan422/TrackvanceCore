"""Plan/selectively retire old labelled test images and empty test networks.

Never prunes the daemon. Application, rollback, backup and unknown resources
are retained. Runtime cleanup is handled separately by the owning test runner.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

PROTECTED = ("trackvance-certification", "bikerwash", "origininspection")
TEST_PROJECT = re.compile(
    r"trackvance-(?:v0[78]0-test-[a-z0-9-]+|bench|delivery-bench|e2e|identity-e2e|"
    r"connections-e2e|delivery-e2e|recovery-(?:src|dst)(?:-0\d\d)?)-"
    r"(?:[a-f0-9]{12}|\d+-[a-f0-9]{6,8})"
)


def docker(*arguments: str) -> str:
    response = subprocess.run(["docker", *arguments], capture_output=True, text=True,
                              encoding="utf-8", timeout=120, check=False)
    if response.returncode:
        raise ValueError("Docker operation failed: " + " ".join(arguments[:2]))
    return response.stdout


def approved_project(project: str) -> bool:
    return not any(value in project for value in PROTECTED) and bool(TEST_PROJECT.fullmatch(project))


def timestamp(value: str) -> datetime:
    # Docker may emit nanoseconds while datetime supports microseconds.
    return datetime.fromisoformat(re.sub(r"(\.\d{6})\d+", r"\1", value))


def image_candidate(image: dict[str, Any], consumers: set[str], cutoff: datetime) -> bool:
    project = (image.get("Config", {}).get("Labels") or {}).get("com.docker.compose.project", "")
    tags = image.get("RepoTags") or []
    return (approved_project(project) and image["Id"] not in consumers
            and timestamp(image["Created"]) < cutoff and bool(tags)
            and all(tag.split(":", 1)[0] == project or tag.split(":", 1)[0].startswith(project + "-")
                    for tag in tags)
            and all(ref.split("@", 1)[0] == project or ref.split("@", 1)[0].startswith(project + "-")
                    for ref in image.get("RepoDigests") or []))


def inventory() -> dict[str, Any]:
    container_ids = docker("ps", "-aq", "--no-trunc").split()
    image_ids = sorted(set(docker("image", "ls", "-aq", "--no-trunc").split()))
    network_ids = docker("network", "ls", "-q", "--no-trunc").split()
    volumes = docker("volume", "ls", "-q").split()
    containers = json.loads(docker("inspect", *container_ids)) if container_ids else []
    images = json.loads(docker("image", "inspect", *image_ids)) if image_ids else []
    networks = json.loads(docker("network", "inspect", *network_ids)) if network_ids else []
    volume_info = json.loads(docker("volume", "inspect", *volumes)) if volumes else []
    return {
        "containers": [{"Id": c["Id"], "Name": c["Name"], "Image": c["Image"],
                        "Running": c["State"]["Running"],
                        "labels": c["Config"].get("Labels") or {},
                        "mounts": [{"type": m["Type"], "name": m.get("Name"), "destination": m["Destination"]}
                                   for m in c.get("Mounts", [])]}
                       for c in containers],
        "images": [{"Id": i["Id"], "RepoTags": i.get("RepoTags") or [],
                    "RepoDigests": i.get("RepoDigests") or [], "Created": i["Created"],
                    "Size": i["Size"], "Config": {"Labels": i.get("Config", {}).get("Labels") or {}}}
                   for i in images],
        "networks": [{"Id": n["Id"], "Name": n["Name"], "Created": n["Created"],
                      "Labels": n.get("Labels") or {}, "Containers": n.get("Containers") or {}}
                     for n in networks],
        "volumes": [{"Name": v["Name"], "Labels": v.get("Labels") or {}} for v in volume_info],
        "space": docker("system", "df"),
    }


def plan(state: dict[str, Any], keep_days: int, now: datetime) -> dict[str, Any]:
    if keep_days < 1:
        raise ValueError("Retention must preserve at least one day of recent cache.")
    cutoff = now - timedelta(days=keep_days)
    consumers = {c["Image"] for c in state["containers"]}
    running_projects = {c["labels"].get("com.docker.compose.project") for c in state["containers"]}
    images = [i for i in state["images"] if image_candidate(i, consumers, cutoff)]
    networks = [n for n in state["networks"]
                if approved_project(n["Labels"].get("com.docker.compose.project", ""))
                and not n["Containers"] and timestamp(n["Created"]) < cutoff
                and n["Labels"]["com.docker.compose.project"] not in running_projects
                and n["Name"].startswith(n["Labels"]["com.docker.compose.project"] + "_")]
    return {"schema_version": 1, "kind": "SELECTIVE_DOCKER_RETENTION", "created_at": now.isoformat(),
            "keep_days": keep_days, "cutoff": cutoff.isoformat(), "images": images, "networks": networks,
            "inventory": state, "excluded": ["all containers", "all volumes", "unknown resources",
                                              "protected installation and rollback images", "global build cache"]}


def apply(document: dict[str, Any], checkpoint: Path | None = None) -> dict[str, Any]:
    if document.get("kind") != "SELECTIVE_DOCKER_RETENTION" or document.get("schema_version") != 1:
        raise ValueError("Invalid retention plan.")
    removed: dict[str, list[str]] = {"images": [], "image_tags": [], "networks": []}
    def preserve() -> None:
        if checkpoint:
            checkpoint.parent.mkdir(parents=True, exist_ok=True)
            checkpoint.write_text(json.dumps({"status": "RUNNING", "removed": removed}, indent=2) + "\n",
                                  encoding="utf-8")
    preserve()
    cutoff = timestamp(document["cutoff"])
    # Each candidate is checked again immediately before the exact-ID operation.
    for expected in document["images"]:
        current = json.loads(docker("image", "inspect", expected["Id"]))[0]
        ids = docker("ps", "-aq", "--no-trunc").split()
        consumers = {c["Image"] for c in json.loads(docker("inspect", *ids))} if ids else set()
        if (not image_candidate(current, consumers, cutoff)
                or sorted(current.get("RepoTags") or []) != sorted(expected["RepoTags"])
                or current["Created"] != expected["Created"]
                or current["Config"].get("Labels") != expected["Config"]["Labels"]):
            raise ValueError("Image ownership/reference changed; regenerate the plan.")
        for tag in expected["RepoTags"]:
            # No force: Docker also enforces current container/child references.
            docker("image", "rm", tag)
            removed["image_tags"].append(tag)
            preserve()
        removed["images"].append(expected["Id"])
        preserve()
    for expected in document["networks"]:
        current = json.loads(docker("network", "inspect", expected["Id"]))[0]
        project = current.get("Labels", {}).get("com.docker.compose.project", "")
        if docker("ps", "-aq", "--filter", "label=com.docker.compose.project=" + project).strip():
            raise ValueError("Test project acquired containers; regenerate the plan.")
        if (not approved_project(project) or current.get("Containers")
                or current["Name"] != expected["Name"] or current.get("Labels") != expected["Labels"]):
            raise ValueError("Network ownership/endpoints changed; regenerate the plan.")
        docker("network", "rm", expected["Id"])
        removed["networks"].append(expected["Id"])
        preserve()
    return {"status": "PASS", "removed": removed, "after": inventory(),
            "note": "Image sizes share layers; compare actual Docker space before/after, never sum virtual sizes."}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--keep-days", type=int, default=7)
    parser.add_argument("--apply-plan", type=Path)
    parser.add_argument("--plan-sha256")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.apply_plan:
        raw = args.apply_plan.read_bytes()
        if hashlib.sha256(raw).hexdigest() != args.plan_sha256:
            raise ValueError("Reviewed plan SHA256 differs.")
        try:
            result = apply(json.loads(raw), args.output)
        except (ValueError, subprocess.TimeoutExpired) as error:
            result = json.loads(args.output.read_text(encoding="utf-8")) if args.output.exists() else {}
            result.update({"status": "FAIL", "error": str(error)})
    else:
        result = plan(inventory(), args.keep_days, datetime.now(UTC))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"output": str(args.output), "status": result.get("status", "PLAN"),
                      "images": len(result.get("images", result.get("removed", {}).get("images", []))),
                      "networks": len(result.get("networks", result.get("removed", {}).get("networks", [])))}))
    return 1 if result.get("status") == "FAIL" else 0


if __name__ == "__main__":
    raise SystemExit(main())
