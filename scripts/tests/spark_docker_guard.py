"""Ownership and evidence checks shared by disposable Spark proof runners."""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import xml.etree.ElementTree as ET
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
IMAGE = "trackvance-v070-isolated:backend"
OWNER_LABEL = "io.trackvance.spark-proof-owner"


def docker(*arguments, check=True):
    result = subprocess.run(["docker", *arguments], capture_output=True, text=True,
                            encoding="utf-8", errors="replace", timeout=120, check=False)
    if check and result.returncode:
        raise RuntimeError(result.stderr.strip() or f"Docker exited {result.returncode}")
    return result.stdout + result.stderr if arguments and arguments[0] == "logs" else result.stdout


def prepare_evidence(path: Path) -> Path:
    result = path.resolve()
    base = (ROOT / ".codex-local" / "v070" / "spark").resolve()
    if not base.is_relative_to(ROOT.resolve()) or not result.is_relative_to(base) or result == base:
        raise ValueError("Evidence must be a new directory inside .codex-local/v070/spark")
    result.mkdir(parents=True, exist_ok=False)
    return result


def protected_inventory():
    projects = {"trackvance-certification", "trackvance-core"}
    if configured := os.getenv("TRACKVANCE_PROTECTED_DOCKER_PROJECT"):
        projects.add(configured)
    identities = set()
    for project in sorted(projects):
        identities.update(docker("ps", "--all", "--quiet", "--filter",
                                 "label=com.docker.compose.project=" + project).split())
    if not identities:
        return []
    records = json.loads(docker("inspect", *sorted(identities)))
    return sorted([{"id": item["Id"], "image": item["Image"],
                    "state": {key: item["State"][key] for key in ("Running", "Paused", "Restarting", "Status", "ExitCode")},
                    "restart": item["HostConfig"]["RestartPolicy"],
                    "mounts": sorted(item["Mounts"], key=lambda mount: (mount.get("Destination", ""), mount.get("Source", "")))}
                   for item in records], key=lambda item: item["id"])


def verify_container_owner(name: str, owner: str):
    if not re.fullmatch(r"trackvance-v070-test-spark-[a-f0-9]{12}", owner) or not name.startswith(owner + "-"):
        raise RuntimeError("Cleanup identity escaped its generated proof owner")
    records = json.loads(docker("inspect", name, check=False) or "[]")
    if not records:
        return False
    if (records[0]["Config"].get("Labels") or {}).get(OWNER_LABEL) != owner:
        raise RuntimeError("Container owner label differs; cleanup refused")
    return True


def remove_owned(containers: list[str], network: str | None, volume: str | None, owner: str):
    for name in reversed(containers):
        if verify_container_owner(name, owner):
            docker("rm", "--force", name)
    for kind, name in (("volume", volume), ("network", network)):
        if name is None:
            continue
        if name != owner + ("-data" if kind == "volume" else "-network"):
            raise RuntimeError("Resource identity escaped its generated proof owner")
        records = json.loads(docker(kind, "inspect", name, check=False) or "[]")
        if records and (records[0].get("Labels") or {}).get(OWNER_LABEL) != owner:
            raise RuntimeError("Resource owner label differs; cleanup refused")
        if records:
            docker(kind, "rm", name)


def sanitize_log(value: str) -> str:
    return re.sub(r"(?i)((?:password|authorization|csrf_token|access_token|cookie|client_secret)\s*[:=]\s*)[^\s,;]+",
                  r"\1[REDACTED]", value)


def assert_tests(junit: Path) -> dict:
    cases = list(ET.parse(junit).getroot().iter("testcase"))
    counts = {key: sum(case.find(key) is not None for case in cases) for key in ("skipped", "failure", "error")}
    if len(cases) < 35 or any(counts.values()):
        raise RuntimeError("Dedicated Spark suite requires at least 35 passing tests and zero skips/failures")
    return {"passed": len(cases), "skipped": 0, "failed": 0}


def assert_population(evidence: Path, rows: int, deployment_mode: str) -> dict:
    measured = json.loads(evidence.read_text(encoding="utf-8"))
    if measured.get("status") != "PASS" or measured.get("input_population_rows") != rows:
        raise RuntimeError("Full-population proof does not match the requested rows")
    outcomes = measured.get("outcomes", {})
    if set(outcomes) != {"intake", "recon", "sentinel"}:
        raise RuntimeError("Full-population proof must include all three modules")
    expected = measured.get("expected_logical_fingerprints", {})
    if measured.get("complete_value_verification") != "PASS" or set(expected) != {"intake", "recon", "sentinel", "intake_accepted"}:
        raise RuntimeError("Full-population proof must verify every value and original position")
    for module, outcome in outcomes.items():
        if outcome.get("status") != "PASS" or outcome.get("runtime", {}).get("deployment_mode") != deployment_mode:
            raise RuntimeError("Module proof did not pass in the requested deployment mode")
        if deployment_mode == "STANDALONE_CLIENT" and outcome["runtime"].get("executor_memory_status_entries", 0) < 3:
            raise RuntimeError("Standalone proof must include two real executors and the driver")
        if outcome.get("results", {}).get("logical_fingerprint") != expected[module]:
            raise RuntimeError("Complete result values differ from the fixture oracle")
    if outcomes["intake"].get("accepted", {}).get("logical_fingerprint") != expected["intake_accepted"]:
        raise RuntimeError("Complete accepted values and physical positions differ from the input snapshot")
    return measured


def source_fingerprint() -> str:
    digest = hashlib.sha256()
    for path in sorted((ROOT / "backend" / "src" / "trackvance").glob("*.py")):
        digest.update(path.name.encode())
        digest.update(path.read_bytes())
    return digest.hexdigest()
