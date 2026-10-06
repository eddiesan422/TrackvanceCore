"""Sample only this runner and its descendant processes; never inspect user data."""
from __future__ import annotations

import os


def sample_process_tree(root_pid: int) -> dict:
    if os.name == "nt":
        return _windows(root_pid)
    try:
        import psutil
    except ImportError:
        return {"status": "UNAVAILABLE", "reason": "Optional psutil is absent on this platform"}
    root = psutil.Process(root_pid)
    processes = [root, *root.children(recursive=True)]
    rows = []
    for process in processes:
        try:
            times = process.cpu_times()
            rows.append({"pid": process.pid, "rss_bytes": process.memory_info().rss,
                         "cpu_seconds": times.user + times.system})
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
    return {"status": "PASS", "processes": rows}


def _windows(root_pid: int) -> dict:
    import ctypes
    from ctypes import wintypes

    class ProcessEntry(ctypes.Structure):
        _fields_ = [("size", wintypes.DWORD), ("usage", wintypes.DWORD), ("pid", wintypes.DWORD),
            ("heap", ctypes.c_size_t), ("module", wintypes.DWORD), ("threads", wintypes.DWORD),
            ("parent", wintypes.DWORD), ("priority", wintypes.LONG), ("flags", wintypes.DWORD),
            ("exe", wintypes.WCHAR * 260)]

    class Memory(ctypes.Structure):
        _fields_ = [("size", wintypes.DWORD), ("faults", wintypes.DWORD),
            ("peak_rss", ctypes.c_size_t), ("rss", ctypes.c_size_t), ("peak_paged", ctypes.c_size_t),
            ("paged", ctypes.c_size_t), ("peak_nonpaged", ctypes.c_size_t), ("nonpaged", ctypes.c_size_t),
            ("pagefile", ctypes.c_size_t), ("peak_pagefile", ctypes.c_size_t)]

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    psapi = ctypes.WinDLL("psapi", use_last_error=True)
    kernel.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
    kernel.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    kernel.Process32FirstW.argtypes = [wintypes.HANDLE, ctypes.POINTER(ProcessEntry)]
    kernel.Process32NextW.argtypes = [wintypes.HANDLE, ctypes.POINTER(ProcessEntry)]
    kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel.OpenProcess.restype = wintypes.HANDLE
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel.GetProcessTimes.argtypes = [wintypes.HANDLE, *[ctypes.POINTER(wintypes.FILETIME)] * 4]
    psapi.GetProcessMemoryInfo.argtypes = [wintypes.HANDLE, ctypes.POINTER(Memory), wintypes.DWORD]
    snapshot = kernel.CreateToolhelp32Snapshot(2, 0)
    if snapshot == ctypes.c_void_p(-1).value:
        return {"status": "UNAVAILABLE", "reason": "Windows process ancestry snapshot unavailable"}
    parents = {}
    try:
        entry = ProcessEntry()
        entry.size = ctypes.sizeof(entry)
        more = kernel.Process32FirstW(snapshot, ctypes.byref(entry))
        while more:
            parents[entry.pid] = entry.parent
            more = kernel.Process32NextW(snapshot, ctypes.byref(entry))
    finally:
        kernel.CloseHandle(snapshot)
    owned = {root_pid}
    while descendants := {pid for pid, parent in parents.items() if parent in owned} - owned:
        owned |= descendants
    rows = []
    for pid in owned:
        handle = kernel.OpenProcess(0x1000 | 0x10, False, pid)
        if not handle:
            continue
        try:
            memory = Memory()
            memory.size = ctypes.sizeof(memory)
            times = [wintypes.FILETIME() for _ in range(4)]
            if psapi.GetProcessMemoryInfo(handle, ctypes.byref(memory), memory.size) and kernel.GetProcessTimes(
                    handle, *[ctypes.byref(value) for value in times]):
                cpu = sum((value.dwHighDateTime << 32) | value.dwLowDateTime for value in times[2:]) / 10_000_000
                rows.append({"pid": pid, "rss_bytes": memory.rss, "cpu_seconds": cpu})
        finally:
            kernel.CloseHandle(handle)
    return {"status": "PASS" if rows else "UNAVAILABLE", "processes": rows}
