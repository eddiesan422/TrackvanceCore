"""Retain closed channel metadata before removing an owned API container.

Only validated REPORT_CHANNEL_DIAGNOSTIC JSON is written. Docker log noise,
exception messages, rows, SQL and environment are neither retained nor printed.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import threading
from pathlib import Path

MARKER = b"REPORT_CHANNEL_DIAGNOSTIC "
LINE_LIMIT = 4096
SCAN_LIMIT = 1024**2
RECORD_LIMIT = 64
EXCEPTIONS = frozenset({"JSONDecodeError", "UnicodeDecodeError", "ValueError", "OSError", "BlockingIOError",
    "BrokenPipeError", "ChildProcessError", "ConnectionAbortedError", "ConnectionRefusedError", "ConnectionResetError",
    "FileExistsError", "FileNotFoundError", "InterruptedError", "IsADirectoryError", "NotADirectoryError",
    "PermissionError", "ProcessLookupError", "TimeoutError"})
KEYS = frozenset({"event", "profile", "exception_class", "json_position", "errno", "line_length",
                  "line_newline", "line_cr_count", "line_sha256", "child_returncode", "coordinator_killed",
                  "stop_requested"})


def optional_integer(value, minimum, maximum):
    return value is None or type(value) is int and minimum <= value <= maximum


def parse_record(line: bytes) -> dict | None:
    """Untrusted logs cannot extend this fixed private metadata schema."""
    if len(line) > LINE_LIMIT or MARKER not in line:
        return None
    try:
        value = json.loads(line.split(MARKER, 1)[1])
    except (ValueError, UnicodeError):
        return None
    if (not isinstance(value, dict) or set(value) != KEYS
            or not all(isinstance(value[key], str) for key in ("event", "profile", "exception_class"))
            or value["event"] not in {"reader_failure", "child_reaped"}
            or value["profile"] not in {"PREVIEW", "DOWNLOAD", "DATASET", "XLSX"}
            or value["exception_class"] not in EXCEPTIONS
            or not optional_integer(value["line_length"], 0, 8 * 1024**2 + 1024)
            or not optional_integer(value["json_position"], 0, 8 * 1024**2 + 1024)
            or not optional_integer(value["line_cr_count"], 0, 8 * 1024**2 + 1024)
            or not optional_integer(value["errno"], 0, 65535)
            or not optional_integer(value["child_returncode"], -256, 255)
            or type(value["coordinator_killed"]) is not bool or type(value["stop_requested"]) is not bool
            or value["line_newline"] is not None and type(value["line_newline"]) is not bool
            or value["line_sha256"] is not None and (not isinstance(value["line_sha256"], str)
                    or not re.fullmatch(r"[0-9a-f]{64}", value["line_sha256"]))):
        return None
    if value["line_length"] is None:
        if any(value[key] is not None for key in ("line_newline", "line_cr_count", "line_sha256", "json_position")):
            return None
    elif (value["line_newline"] is None or value["line_cr_count"] is None or value["line_sha256"] is None
          or value["line_cr_count"] > value["line_length"]
          or value["json_position"] is not None and value["json_position"] > value["line_length"]):
        return None
    return value


def capture_api_diagnostics(compose_arguments: list[str], project: str, target: Path) -> dict:
    """Read only the verified owned API; bounded metadata capture cannot skip cleanup."""
    from ci.owned_cleanup import PROJECT

    receipt = {"schema_version": 1, "kind": "PRIVATE_REPORT_CHANNEL_DIAGNOSTICS",
               "status": "UNAVAILABLE", "project": project, "records": [], "bounded": True,
               "scan_limit_bytes": SCAN_LIMIT, "record_limit": RECORD_LIMIT}
    try:
        if not PROJECT.fullmatch(project):
            raise ValueError("Owned project identity required.")
        command = subprocess.run([*compose_arguments, "ps", "--all", "-q", "api"], capture_output=True,
                                 check=False, timeout=15)
        identity = command.stdout.strip().decode("ascii") if len(command.stdout) < 128 else ""
        if command.returncode or not re.fullmatch(r"[0-9a-f]{64}", identity):
            raise ValueError("Exactly one owned API identity required.")
        command = subprocess.run(["docker", "inspect", "--format", (
            '{"project":{{json (index .Config.Labels "com.docker.compose.project")}},'
            '"service":{{json (index .Config.Labels "com.docker.compose.service")}}}'), identity],
            capture_output=True, check=False, timeout=15)
        if command.returncode or len(command.stdout) > 4096 or json.loads(command.stdout) != {"project": project, "service": "api"}:
            raise ValueError("API ownership drifted.")
        process = subprocess.Popen(["docker", "logs", "--tail", "2000", identity],
                                   stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        timer = threading.Timer(15, process.kill)
        timer.daemon = True
        timer.start()
        scanned = 0
        try:
            assert process.stdout is not None
            while line := process.stdout.readline(LINE_LIMIT + 1):
                scanned += len(line)
                if scanned > SCAN_LIMIT:
                    receipt["scan_truncated"] = True
                    process.kill()
                    break
                value = parse_record(line)
                if value is not None:
                    if len(receipt["records"]) < RECORD_LIMIT:
                        receipt["records"].append(value)
                    else:
                        receipt["records_truncated"] = True
            returncode = process.wait(timeout=5)
            receipt["status"] = "CAPTURED" if returncode == 0 else "PARTIAL"
        finally:
            timer.cancel()
            if process.poll() is None:
                process.kill()
                process.wait(timeout=5)
            if process.stdout:
                process.stdout.close()
    except (OSError, ValueError, subprocess.SubprocessError) as error:
        receipt["error_type"] = type(error).__name__
    temporary = target.with_suffix(target.suffix + ".partial")
    with temporary.open("w", encoding="utf-8") as stream:
        json.dump(receipt, stream, separators=(",", ":"))
        stream.flush()
        os.fsync(stream.fileno())
    temporary.replace(target)
    with target.open("rb") as stream:
        digest = hashlib.file_digest(stream, "sha256").hexdigest()
    return {"status": receipt["status"], "records": len(receipt["records"]), "private_file": target.name,
            "sha256": digest, "bytes": target.stat().st_size, "bounded": True}
