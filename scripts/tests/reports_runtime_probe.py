"""Safe CI diagnostics for a real confined native runtime, using synthetic rows.

Print only counts, status, exit/signal, public runtime facts and hashes. Child
stderr and strace are captured privately and discarded with the owned fixture.
This diagnostic returns zero so the complete pytest gates still run and report
every failure; it never changes production confinement or resource limits.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import re
import shutil
import subprocess
import sys
import tempfile
from collections import Counter
from pathlib import Path


def diagnose(force_default_cpu_count: int | None = None, public_wrapper_baseline: bool = False) -> dict:
    root = Path(__file__).resolve().parents[2]
    sys.path.insert(0, str(root / "backend/src"))
    with tempfile.TemporaryDirectory(prefix="tv-runtime-diagnostic-") as owned:
        directory = Path(owned)
        os.environ["DATABASE_URL"] = "sqlite:///:memory:"
        os.environ["TRACKVANCE_STORAGE_DIR"] = str(directory / "storage")
        import polars as pl

        from trackvance.report_config import ReportLimits
        from trackvance.report_query import compile_draft

        source = directory / "synthetic.parquet"
        pl.DataFrame({"key": ["001", "é", "東京"]}).write_parquet(source)
        schema = [{"name": "key", "logical_type": "STRING"}]
        plan = compile_draft({"mode": "GUIDED", "sources": [{"alias": "a"}],
                              "columns": [{"source_alias": "a", "column": "key", "alias": "key"}]}, {"a": schema})
        limits = ReportLimits.configured("PREVIEW").dto()
        payload = {"profile": "PREVIEW", "sources": [{"alias": "a", "schema": schema, "paths": [str(source)]}],
                   "plan": plan, "limits": limits, "staging": None}
        environment = {"PATH": "/usr/local/bin:/usr/bin:/bin", "LANG": "C.UTF-8", "HOME": "/nonexistent",
                       "OPENBLAS_NUM_THREADS": "1", "OMP_NUM_THREADS": "1", "TZ": "UTC", "PYTHONDONTWRITEBYTECODE": "1",
                       "LD_LIBRARY_PATH": str(Path(sys.base_prefix) / "lib")}
        if force_default_cpu_count is not None:
            # Official DuckDB GetSystemMaxThreads override, only in this
            # synthetic diagnostic process. Kernel CPU/memory/pid caps remain.
            environment["SLURM_CPUS_ON_NODE"] = str(force_default_cpu_count)
        script = root / "backend/src/trackvance/report_sandbox.py"
        if public_wrapper_baseline:
            # Reproduce the old import in this owned synthetic fixture only.
            # Preserve every OS policy and budget in the production child.
            baseline = script.read_text(encoding="utf-8")
            if baseline.count("import _duckdb as engine") != 1:
                raise ValueError("The native import baseline must be unambiguous")
            script = directory / "public-wrapper-baseline.py"
            script.write_text(baseline.replace("import _duckdb as engine", "import duckdb as engine"), encoding="utf-8")
        command = [sys.executable, "-I", "-B", str(script)]
        result = subprocess.run(command, input=json.dumps(payload).encode(), env=environment,
                                capture_output=True, timeout=65, check=False)
        messages = [json.loads(line) for line in result.stdout.splitlines()]
        report = {"check": "REPORT_NATIVE_RUNTIME_DIAGNOSTIC", "status": "PASS" if result.returncode == 0 else "FAIL",
                  "python": platform.python_version(), "libc": list(platform.libc_ver()),
                  "host_cpu_count": os.cpu_count(), "affinity_cpu_count": len(os.sched_getaffinity(0)),
                  "runtime_exit_code": result.returncode, "profile_threads": limits["threads"],
                  "profile_memory_bytes": limits["memory_bytes"],
                  "synthetic_default_cpu_count": force_default_cpu_count,
                  "runtime_module": "duckdb" if public_wrapper_baseline else "_duckdb",
                  "process_memory_bytes": limits["process_memory_bytes"], "message_kinds": [item["kind"] for item in messages],
                  "stderr_sha256": hashlib.sha256(result.stderr).hexdigest(), "stderr_bytes": len(result.stderr),
                  "stderr_categories": [name for name, pattern in {
                      "MEMORY": b"(?i)memory|ENOMEM|bad_alloc", "THREAD": b"(?i)thread|system_error|resource temporarily",
                      "PERMISSION": b"(?i)permission|operation not permitted|EACCES|EPERM",
                      "LOADER": b"(?i)shared librar|importerror|modulenotfound", "FILESYSTEM": b"(?i)filesystem|directory|file not found",
                      "CGROUP": b"(?i)cgroup", "CPP_TERMINATE": b"(?i)terminate called|terminating with",
                      "CPP_IO_EXCEPTION": b"(?i)ioexception|ios_base|filesystem_error",
                      "CPP_ASSERTION": b"(?i)assertion|assert failed", "NUMERIC_CONVERSION": b"(?i)stoi|stoll|out_of_range|invalid_argument",
                  }.items() if re.search(pattern, result.stderr)], "raw_stderr_published": False,
                  "raw_trace_published": False}
        if result.returncode and shutil.which("strace"):
            observed = subprocess.run(["strace", "-ff", "-s", "0", "-e", "trace=%file,mmap,clone,clone3,exit_group",
                                       "-o", str(directory / "native-trace"), *command],
                                      input=json.dumps(payload).encode(), env=environment, capture_output=True,
                                      timeout=65, check=False)
            traces = "\n".join(path.read_text(encoding="utf-8") for path in sorted(directory.glob("native-trace.*")))
            denials, signals = Counter(), Counter()
            recent = []
            for line in traces.splitlines():
                failure = re.search(r"\b([a-z][a-z0-9_]*)\(.*= -1 (E[A-Z0-9]+)", line)
                if failure:
                    name, error = failure.groups()
                    denials[name + ":" + error] += 1
                    counter_name = next((value for value in ("memory.max", "cpu.max", "memory.current", "memory.peak",
                                        "memory.limit_in_bytes", "cpu.cfs_quota_us", "cpu.cfs_period_us")
                                         if "/" + value + '"' in line), None)
                    recent.append({"syscall": name, "errno": error, "public_counter": counter_name,
                                   "path_category": "CGROUP" if "/sys/fs/cgroup" in line else "RUNTIME_LIBRARY" if "/lib" in line else
                                   "PROC_CPU" if "/proc" in line else "OTHER"})
                for signal in re.findall(r"\bSIG(?:ABRT|SEGV|ILL|SYS|KILL|XCPU|BUS)\b", line):
                    signals[signal] += 1
            report.update(traced_exit_code=observed.returncode, denied_syscalls=dict(sorted(denials.items())),
                          recent_denials=recent[-20:], signals=dict(sorted(signals.items())),
                          trace_sha256=hashlib.sha256(traces.encode()).hexdigest())
        return report


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--force-default-cpu-count", type=int, choices=(192,))
    parser.add_argument("--public-wrapper-baseline", action="store_true",
                        help="Reproduce the former import in an owned synthetic fixture; never modify production code")
    arguments = parser.parse_args()
    try:
        output = diagnose(arguments.force_default_cpu_count, arguments.public_wrapper_baseline)
    except Exception as error:  # noqa: BLE001 - diagnostics never suppress or replace the complete pytest gates.
        output = {"check": "REPORT_NATIVE_RUNTIME_DIAGNOSTIC", "status": "DIAGNOSTIC_FAILED", "error_type": type(error).__name__}
    print(json.dumps(output, sort_keys=True))
