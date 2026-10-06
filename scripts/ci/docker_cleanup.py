"""Retire reviewed Trackvance test resources while preserving exact installations."""
from __future__ import annotations

import argparse
import copy
import hashlib
import json
import re
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

try:
    from .common import DIGEST, EvidenceError, load_json, require, sha256
    from .docker_retention import docker
except ImportError:
    from common import DIGEST, EvidenceError, load_json, require, sha256
    from docker_retention import docker

INSTALLATION = "trackvance-certification"
BUILDER = "desktop-linux"


def fingerprint(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def safe_labels(value: dict | None) -> dict:
    return {k: v for k, v in (value or {}).items()
            if k.startswith(("com.docker.compose.", "org.opencontainers.image.", "io.trackvance.", "maintainer"))}


def cache_inventory() -> list[dict]:
    records = []
    for line in docker("buildx", "du", "--builder", BUILDER, "--format", "json").splitlines():
        record = json.loads(line)
        description = record.pop("Description", "")
        record["description_sha256"] = hashlib.sha256(description.encode()).hexdigest()
        record["description_markers"] = [term for term in ("trackvance", "backend/", "/app/backend", "frontend/",
            "uv sync", "pnpm", "polars", "pyspark", "mssql", "node:", "python:") if term in description.lower()]
        record["Parents"] = sorted(record.get("Parents") or [])
        records.append(record)
    return records


def inventory(*, with_cache: bool = True) -> dict:
    """Match the sanitized audit shape; hash environment/config rather than save it."""
    def inspected(arguments, ids):
        return json.loads(docker(*arguments, *ids)) if ids else []
    containers = inspected(("inspect",), docker("ps", "-aq", "--no-trunc").split())
    images = inspected(("image", "inspect"), sorted(set(docker("image", "ls", "-aq", "--no-trunc").split())))
    networks = inspected(("network", "inspect"), docker("network", "ls", "-q", "--no-trunc").split())
    volumes = inspected(("volume", "inspect"), docker("volume", "ls", "-q").split())
    return {"kind": "SANITIZED_CLI_DOCKER_INVENTORY", "checked_at": datetime.now(UTC).isoformat(),
        "containers": [{"id": c["Id"], "name": c["Name"].lstrip("/"), "image_id": c["Image"],
            "image_reference": c["Config"]["Image"], "status": c["State"]["Status"],
            "started_at": c["State"].get("StartedAt"), "finished_at": c["State"].get("FinishedAt"),
            "exit_code": c["State"].get("ExitCode"), "oom": c["State"].get("OOMKilled"),
            "labels": safe_labels(c["Config"].get("Labels")), "config_sha256": fingerprint(c["Config"]),
            "host_config_sha256": fingerprint(c["HostConfig"]),
            "mounts": sorted([{k: m.get(k) for k in ("Type", "Name", "Source", "Destination", "RW")}
                for m in c.get("Mounts", [])], key=lambda m: (m["Type"], m["Source"], m["Destination"])),
            "networks": sorted(c["NetworkSettings"]["Networks"])} for c in containers],
        "images": [{"id": i["Id"], "tags": i.get("RepoTags") or [], "digests": i.get("RepoDigests") or [],
            "created": i["Created"], "size_bytes": i["Size"], "labels": safe_labels(i.get("Config", {}).get("Labels")),
            "rootfs_layers": i.get("RootFS", {}).get("Layers", [])} for i in images],
        "networks": [{"id": n["Id"], "name": n["Name"], "labels": safe_labels(n.get("Labels")),
            "driver": n["Driver"], "endpoints": sorted(n.get("Containers", {}))} for n in networks],
        "volumes": [{"name": v["Name"], "labels": safe_labels(v.get("Labels")), "driver": v["Driver"],
            "mountpoint": v["Mountpoint"], "created": v.get("CreatedAt")} for v in volumes],
        "cache": cache_inventory() if with_cache else [], "builder_list": docker("buildx", "ls") if with_cache else ""}


def project(resource: dict) -> str:
    return resource.get("labels", {}).get("com.docker.compose.project", "")


def normalized(state: dict) -> dict:
    state = copy.deepcopy(state)
    for image in state["images"]:
        image["tags"], image["digests"] = sorted(image["tags"]), sorted(image["digests"])
    for network in state["networks"]:
        network["endpoints"] = sorted(network["endpoints"])
    for container in state["containers"]:
        container["networks"] = sorted(container["networks"])
        container["mounts"] = sorted(container["mounts"], key=lambda m: (m["Type"], m["Source"], m["Destination"]))
    for record in state.get("cache", []):
        record["Parents"] = sorted(record.get("Parents") or [])
    return state


def own_reference(reference: str, labelled: bool = False) -> bool:
    return reference.startswith("trackvance-") or (labelled and bool(re.fullmatch(r"sha256:[0-9a-f]{64}", reference)))


def image_owned(image: dict) -> bool:
    labels = image.get("labels", {})
    return (project(image).startswith("trackvance-") or any(own_reference(t) for t in image.get("tags", []))
            or labels.get("org.opencontainers.image.source", "").rstrip("/").lower() == "https://github.com/eddiesan422/trackvancecore"
            or labels.get("org.opencontainers.image.title", "").lower() in {"trackvance", "trackvancecore"}
            or any(k.startswith("io.trackvance.") for k in labels))


def removable_image(image: dict) -> bool:
    return (image_owned(image) and all(own_reference(t, True) for t in image.get("tags", []))
            and all(own_reference(t) or (project(image).startswith("trackvance-") and t == "sha256@" + image["id"])
                    for t in image.get("digests", [])))


def protected_snapshot(state: dict, retired: set[str]) -> dict:
    containers = [c for c in state["containers"] if project(c) not in retired]
    require(any(project(c) == INSTALLATION for c in containers), "PROTECTED_INSTALLATION_NOT_FOUND")
    refs = {c["image_reference"] for c in containers} | {c.get("labels", {}).get("com.docker.compose.image") for c in containers}
    images = {c["image_id"] for c in containers}
    network_names = {n for c in containers for n in c["networks"]}
    volume_names = {m["Name"] for c in containers for m in c["mounts"] if m["Type"] == "volume"}
    return {"containers": sorted(containers, key=lambda c: c["id"]),
        "images": sorted([i for i in state["images"] if i["id"] in images or refs.intersection(i["tags"] + i["digests"])
                          or not removable_image(i)], key=lambda i: i["id"]),
        "networks": sorted([n for n in state["networks"] if n["name"] in network_names or project(n) not in retired], key=lambda n: n["id"]),
        "volumes": sorted([v for v in state["volumes"] if v["name"] in volume_names or project(v) not in retired], key=lambda v: v["name"])}


def retirement_projects(proof: dict) -> set[str]:
    require(proof.get("schema_version") == 1 and isinstance(proof.get("projects"), dict), "INVALID_RETIREMENT_PROOF")
    retired = set()
    for name, entry in proof["projects"].items():
        require(re.fullmatch(r"trackvance-(?:v0[78]0-test-|recovery-|e2e-|identity-e2e-|connections-e2e-|delivery-e2e-|bench-|clean-demo-)[a-z0-9-]+", name),
                "UNRECOGNIZED_OR_PROTECTED_RETIREMENT_PROJECT")
        require(all(entry.get(k) is True for k in ("approved", "not_sole_copy", "unshared_storage")), "UNPROVEN_PROJECT_RETIREMENT")
        if "origininspection" in name:
            require(entry.get("read_only_clone_pass") is True and entry.get("promotion_legacy_pass") is True,
                    "ORIGIN_INSPECTION_PRESERVATION_NOT_PROVEN")
        require(isinstance(entry.get("evidence_sha256"), list) and entry["evidence_sha256"]
                and all(isinstance(h, str) and DIGEST.fullmatch(h) for h in entry["evidence_sha256"]), "RETIREMENT_EVIDENCE_HASH_MISSING")
        retired.add(name)
    return retired


def cache_candidates(state: dict, proof: dict, inventory_hash: str) -> list[dict]:
    require(proof.get("schema_version") == 1 and proof.get("builder") == BUILDER
            and proof.get("inventory_sha256") == inventory_hash and isinstance(proof.get("records"), list), "CACHE_PROOF_INVENTORY_MISMATCH")
    indexed = {c["ID"]: c for c in state["cache"]}
    def ancestors(identity: str, visited: set[str]) -> set[str]:
        require(identity in indexed and identity not in visited, "CACHE_PROOF_MISSING_OR_CYCLIC_PARENT")
        visited = visited | {identity}
        # GC can have removed an unselected parent already; absent records are
        # never deletion targets or invented proof of ownership.
        return {identity}.union(*(ancestors(p, visited) for p in indexed[identity].get("Parents", []) if p in indexed))
    selected = []
    seen = set()
    for entry in proof["records"]:
        identity, seed = entry.get("id"), entry.get("owned_root_id")
        require(isinstance(identity, str) and re.fullmatch(r"[a-z0-9]{20,64}", identity) and identity not in seen,
                "INVALID_OR_DUPLICATE_CACHE_ID")
        seen.add(identity)
        require(identity in indexed and seed in indexed and "trackvance" in indexed[seed].get("description_markers", [])
                and identity in ancestors(seed, set()), "CACHE_NOT_REACHABLE_FROM_TRACKVANCE_SEED")
        record = indexed[identity]
        require(entry.get("description_sha256") == record.get("description_sha256")
                and sorted(entry.get("parents", [])) == sorted(record.get("Parents", [])), "CACHE_IDENTITY_CHANGED")
        if record.get("Reclaimable") is True:
            selected.append(record)
    # Descendants must be attempted before their reviewed parents.
    selected.sort(key=lambda c: len(ancestors(c["ID"], set())), reverse=True)
    return selected


def plan(state: dict, retirement_proof: dict, cache_proof: dict, *, inventory_hash: str | None = None) -> dict:
    require(state.get("kind") == "SANITIZED_CLI_DOCKER_INVENTORY", "INVALID_SANITIZED_INVENTORY")
    inventory_hash = inventory_hash or fingerprint(state)
    state = normalized(state)
    retired = retirement_projects(retirement_proof)
    protected = protected_snapshot(state, retired)
    protected_ids = {i["id"] for i in protected["images"]}
    containers = [c for c in state["containers"] if project(c) in retired]
    retired_ids = {c["id"] for c in containers}
    all_consumers = {c["image_id"] for c in state["containers"] if c["id"] not in retired_ids}
    images = [i for i in state["images"] if i["id"] not in protected_ids | all_consumers and removable_image(i)]
    networks = [n for n in state["networks"] if project(n) in retired and n not in protected["networks"]
                and set(n["endpoints"]) <= retired_ids and n["name"].startswith(project(n) + "_")]
    volumes = [v for v in state["volumes"] if project(v) in retired and v not in protected["volumes"]
               and v["name"].startswith(project(v) + "_") and all(c["id"] in retired_ids for c in state["containers"]
                   if any(m.get("Name") == v["name"] for m in c["mounts"]))]
    caches = cache_candidates(state, cache_proof, inventory_hash)
    selected = {"containers": {c["id"] for c in containers}, "images": {i["id"] for i in images},
                "networks": {n["id"] for n in networks}, "volumes": {v["name"] for v in volumes}, "cache": {c["ID"] for c in caches}}
    exceptions = []
    for kind, key in (("containers", "id"), ("images", "id"), ("networks", "id"), ("volumes", "name"), ("cache", "ID")):
        for resource in state[kind]:
            if resource[key] not in selected[kind]:
                reason = ("PROTECTED_INSTALLATION_OR_FOREIGN_REFERENCE" if kind != "cache" else
                          "CACHE_ACTIVE_OR_WITHOUT_REVIEWED_TRACKVANCE_ATTRIBUTION")
                exceptions.append({"kind": kind, "id": resource[key], "name": resource.get("name", resource.get("tags", [])),
                                   "reason": reason, "size_bytes": resource.get("size_bytes", resource.get("Size"))})
    return {"schema_version": 1, "kind": "REVIEWED_TRACKVANCE_DOCKER_CLEANUP", "inventory": state,
        "inventory_sha256": inventory_hash, "retirement_proof": retirement_proof, "cache_proof": cache_proof,
        "retirement_proof_sha256": fingerprint(retirement_proof), "cache_proof_sha256": fingerprint(cache_proof),
        "protected": protected, "containers": containers, "images": images, "networks": networks,
        "volumes": volumes, "cache": caches, "retained": exceptions,
        "builders": [], "builder_policy": "Built-in builders and unreviewed builders are retained; owned test runners remove their temporary builders."}


def apply(document: dict, checkpoint: Path) -> dict:
    require(document.get("kind") == "REVIEWED_TRACKVANCE_DOCKER_CLEANUP" and document.get("schema_version") == 1, "INVALID_CLEANUP_PLAN")
    regenerated = plan(document["inventory"], document["retirement_proof"], document["cache_proof"], inventory_hash=document["inventory_sha256"])
    require(regenerated == document, "ALTERED_CLEANUP_PLAN_OR_PROOF")
    retired = retirement_projects(document["retirement_proof"])
    removed = {k: [] for k in ("containers", "stopped_containers", "images", "image_tags", "networks", "volumes", "cache")}
    result = {"status": "RUNNING", "removed": removed, "already_absent_cache": [], "protected_before_sha256": fingerprint(document["protected"])}
    def save():
        checkpoint.parent.mkdir(parents=True, exist_ok=True)
        checkpoint.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    def fresh():
        state = normalized(inventory(with_cache=False))
        require(protected_snapshot(state, retired) == document["protected"], "PROTECTED_IDENTITY_CONFIG_STATE_OR_REFERENCE_CHANGED")
        require({c["id"] for c in state["containers"]} == {c["id"] for c in document["inventory"]["containers"]} - set(removed["containers"]),
                "CONCURRENT_CONTAINER_OR_CONSUMER_CHANGE")
        return state
    save()
    try:
        for expected in document["containers"]:
            current = next(c for c in fresh()["containers"] if c["id"] == expected["id"])
            require(current == expected, "RETIRING_CONTAINER_CHANGED")
            if current["status"] == "running":
                docker("stop", "--time", "30", current["id"])
                removed["stopped_containers"].append(current["id"]); save()
                current = next(c for c in fresh()["containers"] if c["id"] == expected["id"])
                require(current["status"] in {"exited", "created"}
                        and all(current[k] == expected[k] for k in ("image_id", "image_reference", "config_sha256", "host_config_sha256", "mounts", "networks", "labels")),
                        "RETIRING_CONTAINER_CHANGED_AFTER_STOP")
            docker("rm", current["id"])
            removed["containers"].append(current["id"]); save()
        for kind, command, key in (("networks", "network", "id"), ("volumes", "volume", "name")):
            for expected in document[kind]:
                state = fresh()
                current = next(v for v in state[kind] if v[key] == expected[key])
                require(all(current[k] == expected[k] for k in expected if k != "endpoints"), "RETIRING_STORAGE_IDENTITY_CHANGED")
                require(not current.get("endpoints") and not any(any(m.get("Name") == current[key] for m in c["mounts"])
                        for c in state["containers"]), "RETIRING_STORAGE_ACQUIRED_CONSUMER")
                docker(command, "rm", current[key]); removed[kind].append(current[key]); save()
        for expected in document["images"]:
            remaining = list(expected["tags"])
            while True:
                state = fresh()
                current = next((i for i in state["images"] if i["id"] == expected["id"]), None)
                if current is None:
                    require(not remaining, "RETIRING_IMAGE_DISAPPEARED_BEFORE_OPERATION")
                    break
                require(current == expected | {"tags": remaining} and removable_image(current)
                        and not any(c["image_id"] == current["id"] for c in state["containers"]), "RETIRING_IMAGE_IDENTITY_OR_CONSUMER_CHANGED")
                target = remaining[0] if remaining and not remaining[0].startswith("sha256:") else current["id"]
                require(not target.startswith("sha256:") or len(remaining) <= 1, "AMBIGUOUS_IMAGE_ALIAS")
                docker("image", "rm", target)
                if remaining:
                    removed["image_tags"].append(remaining.pop(0))
                save()
                if not remaining:
                    after = fresh()
                    require(not any(i["id"] == expected["id"] for i in after["images"]), "IMAGE_STILL_PRESENT_AFTER_RETIREMENT")
                    break
            removed["images"].append(expected["id"]); save()
        for expected in document["cache"]:
            fresh()
            current = next((c for c in cache_inventory() if c["ID"] == expected["ID"]), None)
            if current is None:
                result["already_absent_cache"].append(expected["ID"]); save()
                continue
            require(current.get("Reclaimable") is True and all(current.get(k) == expected.get(k)
                    for k in ("ID", "Parents", "description_sha256", "Type", "Mutable", "UsageCount")), "CACHE_IDENTITY_OR_USE_CHANGED")
            docker("buildx", "prune", "--builder", BUILDER, "--filter", "id=" + current["ID"], "--force")
            require(not any(c["ID"] == current["ID"] for c in cache_inventory()), "CACHE_RECORD_STILL_PRESENT")
            removed["cache"].append(current["ID"]); save()
        after = fresh()
        require(not ({c["ID"] for c in document["cache"]} & {c["ID"] for c in cache_inventory()}), "REVIEWED_CACHE_REMAINS_AFTER_CLEANUP")
        result.update(status="PASS", after=after, protected_after_sha256=fingerprint(protected_snapshot(after, retired)),
            docker_space=docker("system", "df"), note="Docker image/cache accounting shares layers; physical Windows disk reclamation is measured separately.")
    except (EvidenceError, ValueError, OSError, StopIteration, KeyError, TypeError, subprocess.SubprocessError) as error:
        result.update(status="PARTIAL" if any(removed.values()) else "FAIL",
                      error_code=str(error) if isinstance(error, EvidenceError) else type(error).__name__)
        save()
        raise
    save()
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--inventory", type=Path)
    parser.add_argument("--retirement-proof", type=Path)
    parser.add_argument("--cache-proof", type=Path)
    parser.add_argument("--apply-plan", type=Path)
    parser.add_argument("--plan-sha256")
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    previous_checkpoint = args.output.read_bytes() if args.output.is_file() else None
    try:
        if args.apply_plan:
            require(sha256(args.apply_plan) == args.plan_sha256, "REVIEWED_PLAN_SHA256_MISMATCH")
            result = apply(load_json(args.apply_plan), args.output)
        else:
            require(all((args.inventory, args.retirement_proof, args.cache_proof)), "MISSING_REVIEWED_CLEANUP_INPUT")
            result = plan(load_json(args.inventory), load_json(args.retirement_proof), load_json(args.cache_proof), inventory_hash=sha256(args.inventory))
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
        print(json.dumps({"status": result.get("status", "PLAN"), "output": str(args.output),
            "counts": {k: len(result.get(k, result.get("removed", {}).get(k, []))) for k in ("containers", "images", "networks", "volumes", "cache")}}))
        return 0
    except (EvidenceError, ValueError, OSError, StopIteration, KeyError, TypeError, subprocess.SubprocessError) as error:
        if (not args.output.exists() or args.output.read_bytes() == previous_checkpoint
                or load_json(args.output).get("status") not in {"PARTIAL", "FAIL"}):
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_text(json.dumps({"status": "FAIL", "error_code": str(error) if isinstance(error, EvidenceError) else type(error).__name__}) + "\n")
        print(json.dumps({"status": "FAIL", "output": str(args.output)}))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
