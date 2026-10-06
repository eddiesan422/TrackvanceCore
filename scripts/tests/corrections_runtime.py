"""Incremental XLSX evidence and bounded phases; no cached PASS state."""
from __future__ import annotations

import hashlib
import json
import os
import re
import signal
import subprocess
import threading
import time
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from ci.common import EvidenceError
from ci.evidence import ci_context, write_receipt

GROUP_SCENARIOS = {
    "corrections-functional": ["acquisition-120-inline", "acquisition-120-shared",
        "row-limit-preserves-version-121", "chain-120-inline", "cancel-120-shared",
        "crash-lease-120-shared", "unread-api-restart-functional", "automation-integrated-functional"],
    "corrections-acquisition": [f"acquisition-{rows}-{variant}" for rows in (100000, 100001, 400000, 1000000)
        for variant in ("inline", "shared")] + ["row-limit-preserves-version-1000001"],
    "corrections-browser": ["browser-400000-shared", "browser-1000000-shared"],
    "corrections-dispatch": ["chain-1000000-inline", "dispatch-1000000-two-datasets", "unread-api-restart"],
    "corrections-recovery": ["cancel-1000000-shared", "crash-lease-1000000-shared", "native-backup-restore"],
}
GROUP_DEADLINES = {"corrections-functional": 1800, "corrections-acquisition": 3600, "corrections-browser": 3600,
                   "corrections-dispatch": 2400, "corrections-recovery": 3300}
GROUP_ALIASES = {group.removeprefix("corrections-"): group for group in GROUP_SCENARIOS}


def file_digest(path):
    with Path(path).open("rb") as source:
        return hashlib.file_digest(source, "sha256").hexdigest()


def source_identity(command, environment=None):
    environment = os.environ if environment is None else environment
    actual = command(["git", "rev-parse", "HEAD"]).strip()
    expected = environment.get("CI_SOURCE_SHA") or environment.get("GITHUB_SHA") or actual
    if not re.fullmatch("[0-9a-f]{40}", actual) or expected != actual:
        raise ValueError("XLSX checkout must match the requested source commit")
    return actual


def atomic_json(path, value):
    path = Path(path)
    temporary = path.with_name(path.name + "." + uuid4().hex + ".pending")
    try:
        with temporary.open("x", encoding="utf-8") as target:
            json.dump(value, target, ensure_ascii=False, indent=2, allow_nan=False)
            target.write("\n")
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def artifact_reference(path, base, kind):
    path, base = Path(path).resolve(strict=True), Path(base).resolve(strict=True)
    if not path.is_file() or not path.is_relative_to(base):
        raise ValueError("Scenario attachments must be owned regular files")
    return {"path": path.relative_to(base).as_posix(), "sha256": file_digest(path), "kind": kind}


class ScenarioDeadline(TimeoutError):
    pass


@contextmanager
def deadline(seconds):
    """Linux alarms interrupt blocked browser/HTTP/CLI calls before job expiry.

    Windows uses the already bounded transport calls and checks elapsed time
    on return; this fallback is explicitly identified in the receipt.
    """
    started = time.monotonic()
    if not 0 < seconds <= 3600:
        raise ValueError("Scenario deadline must be bounded at one hour")
    alarms = hasattr(signal, "SIGALRM") and threading.current_thread() is threading.main_thread()
    previous = None
    parent_timer = (0, 0)
    if alarms:
        parent_timer = signal.getitimer(signal.ITIMER_REAL)
        previous = signal.signal(signal.SIGALRM, lambda *_args: (_ for _ in ()).throw(ScenarioDeadline("XLSX_PHASE_DEADLINE")))
        signal.setitimer(signal.ITIMER_REAL, min(seconds, parent_timer[0]) if parent_timer[0] else seconds)
    try:
        yield
        elapsed = time.monotonic() - started
        if elapsed >= seconds or (parent_timer[0] and elapsed >= parent_timer[0]):
            raise ScenarioDeadline("XLSX_PHASE_DEADLINE")
    finally:
        if alarms:
            signal.setitimer(signal.ITIMER_REAL, 0)
            signal.signal(signal.SIGALRM, previous)
            if parent_timer[0]:
                remaining = parent_timer[0] - (time.monotonic() - started)
                if remaining > 0:
                    signal.setitimer(signal.ITIMER_REAL, remaining, parent_timer[1])


class ScenarioEvidence:
    def __init__(self, directory, group, source_sha, *, ci=None):
        if group not in GROUP_SCENARIOS or not re.fullmatch("[0-9a-f]{40}", source_sha):
            raise ValueError("Unknown group or invalid source SHA")
        self.base = Path(directory)
        self.directory = self.base / ("xlsx-scenarios-" + group + "-" + uuid4().hex[:12])
        self.directory.mkdir(parents=True, exist_ok=False)
        self.group, self.source_sha = group, source_sha
        if ci:
            self.ci = ci
        else:
            try:
                self.ci = ci_context()
            except EvidenceError:
                if os.environ.get("GITHUB_RUN_ID"):
                    raise
                self.ci = {"kind": "LOCAL", "execution_id": "local-" + uuid4().hex, "group": group}
        self.required, self.completed, self.failed = GROUP_SCENARIOS[group], {}, False
        self.seen, self.prerequisites = set(), []
        self.started = datetime.now(UTC).isoformat()
        self.began = time.monotonic()
        self.write_group("RUNNING")

    def write_group(self, status, **extra):
        value = {"schema_version": 1, "kind": "XLSX_GROUP",
            "group": self.group, "source_sha": self.source_sha,
            "ci": None if self.ci.get("kind") == "LOCAL" else self.ci,
            **({"execution": self.ci} if self.ci.get("kind") == "LOCAL" else {}), "status": status,
            "started_at": self.started, "completed_at": datetime.now(UTC).isoformat() if status != "RUNNING" else None,
            "duration_seconds": round(time.monotonic() - self.began, 6),
            "required_scenarios": self.required, "scenario_results": list(self.completed.values()),
            "prerequisite_results": self.prerequisites, **extra}
        atomic_json(self.base / "corrections-evidence.json", value)

    def scenario(self, scenario_id, rows, variant, callback, *, seconds=1800, prerequisite=False):
        if scenario_id in self.seen or (not prerequisite and scenario_id not in self.required):
            raise ValueError("Unexpected or duplicate scenario")
        if self.failed:
            raise ValueError("A failed group cannot resume until green")
        if prerequisite and not scenario_id.startswith("prerequisite-"):
            raise ValueError("Prerequisites must be explicit and cannot replace mandatory scenarios")
        self.seen.add(scenario_id)
        stamp = uuid4().hex[:12]
        path = self.directory / ("progress-" + scenario_id + "-" + stamp + ".json")
        kind = "XLSX_PREREQUISITE" if prerequisite else "XLSX_SCENARIO"
        started, began = datetime.now(UTC).isoformat(), time.monotonic()
        value = {"schema_version": 1, "kind": "XLSX_PROGRESS",
            "group": self.group, "scenario_id": scenario_id, "source_sha": self.source_sha,
            "ci": None if self.ci.get("kind") == "LOCAL" else self.ci,
            **({"execution": self.ci} if self.ci.get("kind") == "LOCAL" else {}),
            "status": "RUNNING", "rows": rows, "variant": variant, "started_at": started,
            "completed_at": None, "duration_seconds": 0, "result": {}, "resources": {}, "evidence": [],
            "deadline_seconds": seconds, "deadline_method": "POSIX_ALARM" if hasattr(signal, "SIGALRM") else "TRANSPORT_AND_ELAPSED"}
        atomic_json(path, value)
        print(json.dumps({"group": self.group, "scenario": scenario_id, "rows": rows, "variant": variant, "status": "RUNNING"}), flush=True)
        try:
            with deadline(seconds):
                result = callback()
            if not isinstance(result, dict):
                raise TypeError("Scenario result must be an independent aggregate")
            value.update(status="PASS", result=result, resources={"phases": result.get("phases", {})})
            return result
        except BaseException as error:
            self.failed = True
            value.update(status="FAIL", error={"code": "XLSX_PHASE_DEADLINE" if isinstance(error, TimeoutError)
                else "XLSX_CERTIFICATION_FAILED", "type": type(error).__name__})
            value["result"] = {"status": "FAIL", "error": value["error"]}
            raise
        finally:
            value.update(completed_at=datetime.now(UTC).isoformat(), duration_seconds=round(time.monotonic() - began, 6))
            final = write_receipt(self.directory, group=self.group, scenario_id=scenario_id,
                result=value["result"], rows=rows, variant=variant, status=value["status"],
                started_at=started, completed_at=value["completed_at"], duration_seconds=value["duration_seconds"],
                resources=value["resources"] | {"deadline_seconds": seconds, "deadline_method": value["deadline_method"]},
                source_sha=self.source_sha, ci=self.ci, kind=kind, error=value.get("error"))
            # RUNNING progress remains distinct from immutable final receipts.
            atomic_json(path, value | {"final_receipt": final.name})
            reference = artifact_reference(final, self.base, kind) | {"scenario_id": scenario_id}
            if prerequisite:
                self.prerequisites.append(reference)
            else:
                self.completed[scenario_id] = reference
            self.write_group("FAIL" if self.failed else "RUNNING")
            print(json.dumps({"group": self.group, "scenario": scenario_id, "rows": rows, "status": value["status"],
                "duration_seconds": value["duration_seconds"]}), flush=True)

    def finish(self, **extra):
        if self.failed or set(self.completed) != set(self.required):
            self.write_group("FAIL", **extra)
            raise ValueError("Every mandatory scenario must complete before group PASS")
        self.write_group("PASS", **extra)


def immutable_image_pair(backend, web, source_sha, command):
    if not backend or not web:
        raise ValueError("Both same-SHA immutable images are required")
    result = {}
    for name, reference in (("backend", backend), ("web", web)):
        row = json.loads(command(["docker", "image", "inspect", reference]))[0]
        identity = row.get("Id", "")
        labels = (row.get("Config") or {}).get("Labels") or {}
        if not re.fullmatch("sha256:[0-9a-f]{64}", identity) or labels.get("org.opencontainers.image.revision") != source_sha:
            raise ValueError("Certification images must have the exact checkout OCI revision")
        result[name] = {"id": identity, "source_sha": source_sha}
    return result


def private_command(arguments, directory, name, *, seconds=1800):
    with (Path(directory) / (name + ".private.log")).open("w", encoding="utf-8") as log:
        process = subprocess.Popen(arguments, cwd=Path(__file__).resolve().parents[2],
            stdout=log, stderr=subprocess.STDOUT, start_new_session=os.name == "posix")
        term_handler = os.name == "posix" and threading.current_thread() is threading.main_thread()
        previous_term = None
        if term_handler:
            def interrupted(*_args):
                raise InterruptedError("XLSX_WRAPPER_INTERRUPTED")
            previous_term = signal.signal(signal.SIGTERM, interrupted)
        try:
            code = process.wait(timeout=seconds)
        except BaseException:
            # Python's SIGINT handler enters the native recovery finally block;
            # the parent scenario remains failed even if cleanup finishes well.
            try:
                if os.name == "posix":
                    os.killpg(process.pid, signal.SIGINT)
                elif process.poll() is None:
                    process.terminate()
                process.wait(timeout=30)
            except BaseException:  # noqa: BLE001 -- a second deadline/signal must still reap this owned child.
                try:
                    if os.name == "posix":
                        os.killpg(process.pid, signal.SIGKILL)
                    elif process.poll() is None:
                        process.kill()
                    process.wait(timeout=10)
                except BaseException as cleanup_error:  # noqa: BLE001 -- preserve the original failure; UUID fallback remains mandatory.
                    # The central UUID/label fallback cleans Docker resources.
                    # Never replace the original interruption with child output.
                    print(json.dumps({"private_command_cleanup": "FAIL", "error_type": type(cleanup_error).__name__}), file=log)
            raise
        finally:
            if term_handler:
                signal.signal(signal.SIGTERM, previous_term)
    if code:
        raise RuntimeError("XLSX_CONTROLLED_COMMAND_FAILED")
