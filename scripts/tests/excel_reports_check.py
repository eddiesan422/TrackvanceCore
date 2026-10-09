"""Native Excel open check on private client XLSX files, with an owned-PID watchdog.

This evidence covers normal opening, dimensions and typed boundary samples.
Complete population hashes belong to the independent XML/openpyxl HTTP oracles.
Run only on Windows: --file <private.xlsx> --rows 1000000 --customers 100000
--source-sha <commit>. Opening a massive file requires its separately planned run.
"""
from __future__ import annotations

import argparse
import ctypes
import json
import os
import re
import shutil
import subprocess
import sys
import time
from ctypes import wintypes
from pathlib import Path
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts/tests"))
from reports_download_085 import HEADERS, customer, file_hash, transaction, write_json


class NativeFailure(ValueError):
    """Stable diagnostics without cell contents or application exception text."""


def require(condition, code):
    if not condition:
        raise NativeFailure(code)


def private_file(path: Path) -> Path:
    resolved = path.resolve(strict=True)
    require(not path.is_symlink() and resolved.is_relative_to((ROOT / ".codex-local").resolve()),
            "EXCEL_PRIVATE_CLIENT_FILE_REQUIRED")
    require(resolved.is_file() and resolved.suffix.lower() == ".xlsx", "EXCEL_XLSX_REQUIRED")
    return resolved


def expectation(path: Path, rows: int, customers: int, source_sha: str) -> dict:
    require(0 < rows <= 1000000 and 0 < customers <= 100000, "EXCEL_FIXTURE_DIMENSIONS")
    require(bool(re.fullmatch(r"[a-f0-9]{40}", source_sha)), "EXCEL_SOURCE_SHA_REQUIRED")
    samples = []
    # First five cover null, empty, backslash, apostrophe and whitespace; last
    # covers the final real row. No million-row in-memory population is built.
    for index in sorted({*range(min(5, rows)), rows - 1}):
        values = transaction(index, customers, canonical=True)
        samples.append({"worksheet_row": index + 2,
            "values": [*values[:2], customer(index % customers)[1], *values[2:]]})
    return {"file": str(path), "rows": rows, "headers": HEADERS, "samples": samples,
            "source_sha": source_sha, "file_sha256": file_hash(path)}


class Memory(ctypes.Structure):
    _fields_ = [("size", wintypes.DWORD), ("faults", wintypes.DWORD),
        ("peak_rss", ctypes.c_size_t), ("rss", ctypes.c_size_t),
        ("peak_paged", ctypes.c_size_t), ("paged", ctypes.c_size_t),
        ("peak_nonpaged", ctypes.c_size_t), ("nonpaged", ctypes.c_size_t),
        ("pagefile", ctypes.c_size_t), ("peak_pagefile", ctypes.c_size_t)]


def filetime(value):
    return (value.dwHighDateTime << 32) | value.dwLowDateTime


class OwnedExcel:
    """A pinned kernel handle prevents terminating a subsequently reused PID."""
    def __init__(self, owner: dict):
        require(owner.get("status") == "OWNED" and isinstance(owner.get("pid"), int)
                and owner["pid"] > 0 and owner["pid"] not in owner["preexisting_excel_pids"],
                "EXCEL_OWNERSHIP_INVALID")
        self.owner, self.handle = owner, None
        self.kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        self.psapi = ctypes.WinDLL("psapi", use_last_error=True)
        kernel = self.kernel
        kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel.OpenProcess.restype = wintypes.HANDLE
        kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel.GetProcessTimes.argtypes = [wintypes.HANDLE, *[ctypes.POINTER(wintypes.FILETIME)] * 4]
        kernel.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
        kernel.GetProcessAffinityMask.argtypes = [wintypes.HANDLE, ctypes.POINTER(ctypes.c_size_t), ctypes.POINTER(ctypes.c_size_t)]
        kernel.QueryFullProcessImageNameW.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD)]
        kernel.TerminateProcess.argtypes = [wintypes.HANDLE, wintypes.UINT]
        self.psapi.GetProcessMemoryInfo.argtypes = [wintypes.HANDLE, ctypes.POINTER(Memory), wintypes.DWORD]
        handle = kernel.OpenProcess(0x1000 | 0x10 | 0x1, False, owner["pid"])
        require(bool(handle), "EXCEL_OWNED_PROCESS_UNAVAILABLE")
        try:
            times = self.times(handle)
            size, executable = wintypes.DWORD(32768), ctypes.create_unicode_buffer(32768)
            require(bool(kernel.QueryFullProcessImageNameW(handle, 0, executable, ctypes.byref(size))), "EXCEL_EXECUTABLE_UNAVAILABLE")
            require(filetime(times[0]) == owner["process_creation_filetime"]
                    and Path(executable.value).name.upper() == "EXCEL.EXE"
                    and os.path.normcase(executable.value) == os.path.normcase(owner["executable"]),
                    "EXCEL_OWNED_IDENTITY_CHANGED")
            self.handle = handle
        except BaseException:
            kernel.CloseHandle(handle)
            raise

    def times(self, handle=None):
        times = [wintypes.FILETIME() for _ in range(4)]
        require(bool(self.kernel.GetProcessTimes(handle or self.handle,
            *[ctypes.byref(value) for value in times])), "EXCEL_PROCESS_TIMES_UNAVAILABLE")
        return times

    def alive(self):
        code = wintypes.DWORD()
        require(bool(self.kernel.GetExitCodeProcess(self.handle, ctypes.byref(code))), "EXCEL_PROCESS_STATE_UNAVAILABLE")
        return code.value == 259

    def sample(self):
        if not self.alive():
            return None
        memory = Memory()
        memory.size = ctypes.sizeof(memory)
        require(bool(self.psapi.GetProcessMemoryInfo(self.handle, ctypes.byref(memory), memory.size)), "EXCEL_MEMORY_UNAVAILABLE")
        mask, system = ctypes.c_size_t(), ctypes.c_size_t()
        require(bool(self.kernel.GetProcessAffinityMask(self.handle, ctypes.byref(mask), ctypes.byref(system))), "EXCEL_AFFINITY_UNAVAILABLE")
        return {"rss_bytes": memory.rss, "peak_rss_bytes": memory.peak_rss,
            "cpu_seconds": sum(filetime(value) for value in self.times()[2:]) / 10000000,
            "logical_processor_count": mask.value.bit_count()}

    def terminate(self):
        if self.alive():
            require(bool(self.kernel.TerminateProcess(self.handle, 1)), "EXCEL_OWNED_TERMINATION_FAILED")

    def close(self):
        if self.handle:
            self.kernel.CloseHandle(self.handle)
            self.handle = None


def enforce_budget(sample: dict, max_bytes: int, *, affinity_ready: bool):
    require(max(sample["rss_bytes"], sample["peak_rss_bytes"]) <= max_bytes, "EXCEL_MEMORY_BUDGET")
    if affinity_ready:
        require(sample["logical_processor_count"] == 1, "EXCEL_CPU_BUDGET")


def check(path: Path, rows: int, customers: int, source_sha: str,
          *, timeout: int = 900, memory_mib: int = 4096) -> dict:
    require(os.name == "nt", "EXCEL_WINDOWS_REQUIRED")
    require(1 <= timeout <= 900 and 128 <= memory_mib <= 4096, "EXCEL_FINITE_BUDGET_REQUIRED")
    path = private_file(path)
    expected = expectation(path, rows, customers, source_sha)
    evidence = path.parent / ("excel-check-085-" + uuid4().hex[:12])
    evidence.mkdir(exist_ok=False)
    write_json(evidence / "expected.json", expected)
    powershell = shutil.which("powershell")
    require(bool(powershell), "EXCEL_POWERSHELL_UNAVAILABLE")
    receipt = {"status": "FAIL", "source_sha": source_sha, "client_file": str(path),
        "input_file_sha256": expected["file_sha256"], "rows": rows, "columns": 11,
        "scope": "EXCEL_NATIVE_OPEN_DIMENSIONS_AND_BOUNDARY_SAMPLES",
        "full_population_hash_in_excel": False, "cpu_ceiling": 1,
        "rss_ceiling_bytes": memory_mib * 1024**2, "deadline_seconds": timeout,
        "evidence": str(evidence), "owned_process_verified": False}
    began, process, excel = time.monotonic(), None, None
    samples, peak, cpu, ownership_began = 0, 0, 0.0, None
    failure = None
    try:
        with (evidence / "native.private.log").open("wb") as output:
            process = subprocess.Popen([powershell, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
                "-File", str(Path(__file__).with_suffix(".ps1")), "-ExpectedFile", str(evidence / "expected.json"),
                "-EvidenceDirectory", str(evidence)], cwd=ROOT, stdout=output, stderr=subprocess.STDOUT,
                creationflags=subprocess.CREATE_NO_WINDOW)
            while process.poll() is None:
                require(time.monotonic() - began < timeout, "EXCEL_TOTAL_DEADLINE")
                ownership = evidence / "ownership.json"
                if excel is None and ownership.is_file():
                    owner = json.loads(ownership.read_text(encoding="utf-8"))
                    require(owner.get("source_sha") == source_sha, "EXCEL_OWNERSHIP_SOURCE_SHA")
                    excel = OwnedExcel(owner)
                    ownership_began = time.monotonic()
                    receipt.update(owned_process_verified=True, excel_pid=excel.owner["pid"],
                        preexisting_excel_pids=excel.owner["preexisting_excel_pids"])
                if excel is not None:
                    sample = excel.sample()
                    if sample:
                        samples += 1
                        peak = max(peak, sample["peak_rss_bytes"], sample["rss_bytes"])
                        cpu = max(cpu, sample["cpu_seconds"])
                        # The ownership handshake precedes applying affinity.
                        # Fail if that narrow setup interval lasts over 2 sec.
                        enforce_budget(sample, memory_mib * 1024**2,
                            affinity_ready=sample["logical_processor_count"] == 1 or time.monotonic() - ownership_began > 2)
                time.sleep(0.05)
            native_path = evidence / "excel-result.json"
            require(native_path.is_file(), "EXCEL_NATIVE_RECEIPT_MISSING")
            native = json.loads(native_path.read_text(encoding="utf-8-sig"))
            receipt["native"] = native
            require(process.returncode == 0 and native.get("status") == "PASS", "EXCEL_NATIVE_OPEN_CHECK_FAILED")
            require(excel is not None and samples > 0 and native.get("owned_instance_verified") is True, "EXCEL_RESOURCE_PROOF_MISSING")
            require(native.get("excel_cpu_affinity_mask", 0).bit_count() == 1, "EXCEL_CPU_BUDGET")
            require(file_hash(path) == expected["file_sha256"], "EXCEL_READ_ONLY_FILE_CHANGED")
            receipt["status"] = "PASS"
    except BaseException as error:  # noqa: BLE001 - preserve FAIL and clean the proved owned PID on interruption.
        failure = error
        receipt.update(error_type=type(error).__name__,
            error_code=str(error) if isinstance(error, NativeFailure) else "EXCEL_SUPERVISOR_FAILED")
    finally:
        # Never terminate an Excel process lacking both COM ownership and a
        # matching pinned kernel identity. The helper Popen itself is ours.
        if process is not None and process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
        if excel is not None:
            try:
                deadline = time.monotonic() + 10
                while excel.alive() and time.monotonic() < deadline and failure is None:
                    time.sleep(0.1)
                if excel.alive():
                    excel.terminate()
                    receipt.update(status="FAIL", cleanup="OWNED_PID_TERMINATED_AFTER_FAILURE_OR_LINGER")
                    deadline = time.monotonic() + 5
                    while excel.alive() and time.monotonic() < deadline:
                        time.sleep(0.05)
                else:
                    receipt["cleanup"] = "OWNED_COM_QUIT_AND_RELEASE"
                require(not excel.alive(), "EXCEL_OWNED_PROCESS_SURVIVED")
            except (NativeFailure, OSError) as error:
                receipt.update(status="FAIL", cleanup="OWNED_CLEANUP_FAILED", cleanup_error_type=type(error).__name__)
            finally:
                excel.close()
        else:
            receipt["cleanup"] = "NO_VERIFIED_EXCEL_PID_NO_EXCEL_TERMINATION"
        receipt.update(duration_seconds=round(time.monotonic() - began, 3),
            sampled_peak_rss_bytes=peak, excel_cpu_seconds=cpu, resource_samples=samples,
            sampling_interval_seconds=0.05, preexisting_options_touched=False)
        write_json(evidence / "receipt.json", receipt)
    return receipt


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--file", required=True, type=Path)
    parser.add_argument("--rows", required=True, type=int)
    parser.add_argument("--customers", required=True, type=int)
    parser.add_argument("--source-sha", required=True)
    parser.add_argument("--timeout", type=int, default=900)
    parser.add_argument("--memory-mib", type=int, default=4096)
    args = parser.parse_args()
    result = check(args.file, args.rows, args.customers, args.source_sha,
                   timeout=args.timeout, memory_mib=args.memory_mib)
    print(json.dumps({"status": result["status"], "evidence": result["evidence"], "scope": result["scope"]}), flush=True)
    require(result["status"] == "PASS", "EXCEL_CHECK_FAILED")


if __name__ == "__main__":
    main()
