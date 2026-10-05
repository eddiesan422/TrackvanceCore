"""Observe real API/nginx/isolated-child file syscalls during report HTTP flows.

All Docker resources belong to one guarded UUID project. Startup/acquisition is
outside the frozen observation window. Only the independent observer owns trace
files; raw traces and diagnostics remain in the private certification context.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import http.client
import io
import json
import re
import socket
import struct
import subprocess
import sys
import time
import urllib.parse
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))
import catalog_reports_api_cycle as cycle
import certification_v080 as guard

TRACE_CALLS = "%file,write,writev,pwrite64,pwritev,pwritev2,ftruncate,mmap,clone,fork,vfork,exit_group"
MUTATIONS = {"creat", "mkdir", "mkdirat", "unlink", "unlinkat", "rename", "renameat", "renameat2",
             "link", "linkat", "symlink", "symlinkat", "truncate", "ftruncate", "chmod", "fchmod",
             "fchmodat", "chown", "fchown", "fchownat", "utime", "utimes", "utimensat"}
WRITES = {"write", "writev", "pwrite64", "pwritev", "pwritev2"}


def analyze_trace(content: str) -> dict:
    """Fail closed on attempted regular-file mutation, including removed files.

    strace -yy annotates descriptors. IPC, sockets and the null character device
    are allowed; unclassified writes are rejected instead of assuming they are
    harmless. Result opens are rejected even when the kernel denied the open.
    No trace line or filesystem pathname is included in the public summary.
    """
    calls, violations = Counter(), Counter()
    for line in content.splitlines():
        found = re.search(r"\b([a-z][a-z0-9_]*)\(", line)
        if not found:
            continue
        call = found.group(1)
        calls[call] += 1
        if (call in {"open", "openat", "openat2"}
                and re.search(r"\bO_(?:WRONLY|RDWR|CREAT|TRUNC|APPEND|TMPFILE)\b", line)
                and not re.search(r'"/dev/null", O_RDWR(?:\||\))', line)):
            violations["writable_open"] += 1
        if call in MUTATIONS:
            violations["filesystem_mutation"] += 1
        if call == "mmap" and "PROT_WRITE" in line and "MAP_SHARED" in line and "MAP_ANONYMOUS" not in line:
            violations["shared_file_mapping"] += 1
        if call in WRITES:
            descriptor = re.search(r"\(\d+<([^>]+)>", line)
            destination = descriptor.group(1) if descriptor else ""
            if not (destination.startswith(("pipe:[", "socket:[", "TCP:", "TCPv6:", "UDP:", "UNIX-STREAM:", "UNIX:[", "anon_inode:"))
                    or destination in {"/dev/null", "/dev/stdout", "/dev/stderr"}):
                violations["regular_or_unclassified_write"] += 1
    return {"status": "PASS" if calls and not violations else "FAIL", "syscall_counts": dict(sorted(calls.items())),
            "violations": dict(sorted(violations.items())), "observed_syscalls": sum(calls.values()),
            "sha256": hashlib.sha256(content.encode()).hexdigest(), "raw_trace_published": False}


def private_run(arguments: list[str], directory: Path, name: str, timeout: int = 300) -> None:
    with (directory / (name + ".private.log")).open("w", encoding="utf-8") as output:
        result = subprocess.run(arguments, cwd=ROOT, stdout=output, stderr=subprocess.STDOUT,
                                timeout=timeout, check=False)
    if result.returncode:
        raise RuntimeError(f"La etapa aislada {name} falló (exit={result.returncode}); diagnóstico privado.")


def prepare(port: int, main_project: str | None) -> tuple[Path, dict]:
    directory = guard.init("reports-http", port, main_project)
    directory, context = guard.load_context(directory)
    observer = directory / "observer"
    for service in ("api", "proxy"):
        (observer / service).mkdir(parents=True)
        # The enclosing context is 0700; the mounted leaf must be writable by
        # the image's unprivileged uid on Linux runners, too.
        (observer / service).chmod(0o777)
    # Diagnostic images add only strace, never extra Linux capabilities.
    (directory / "backend.Dockerfile").write_text(
        "FROM trackvance-v080-isolated:backend\nUSER root\n"
        "RUN apt-get update && apt-get install -y --no-install-recommends strace && rm -rf /var/lib/apt/lists/*\n"
        "USER trackvance\n", encoding="utf-8")
    (directory / "web.Dockerfile").write_text(
        "FROM trackvance-v080-isolated:web\nUSER root\nRUN apk add --no-cache strace "
        "&& mkdir -p /var/cache/nginx && chown -R nginx:nginx /var/cache/nginx\nUSER nginx\n", encoding="utf-8")
    (observer / "proxy" / "reports.conf").write_bytes((ROOT / "deploy/nginx/default.conf").read_bytes())
    (observer / "proxy" / "nginx.conf").write_text(
        "worker_processes 1;\npid /tmp/nginx.pid;\nerror_log /dev/stderr warn;\n"
        "events { worker_connections 256; }\nhttp { include /etc/nginx/mime.types; "
        "access_log /dev/stdout; include /observe/reports.conf; }\n", encoding="utf-8")
    configuration = json.loads((directory / "compose.json").read_text(encoding="utf-8"))
    images = {"api": "trackvance-v080-http-debug:backend", "web": "trackvance-v080-http-debug:web"}
    for service, trace_service in (("api", "api"), ("web", "proxy")):
        entry = configuration["services"][service]
        entry.update(image=images[service], cap_drop=["ALL"], security_opt=["no-new-privileges:true"])
        entry.setdefault("volumes", []).append({"type": "bind", "source": str(observer / trace_service), "target": "/observe"})
        trace = ["strace", "-ff", "-yy", "-ttt", "-s", "0", "-e", "trace=" + TRACE_CALLS, "-o", "/observe/trace"]
        entry["command"] = trace + (["python", "-B", "-m", "uvicorn", "trackvance.api:app", "--host", "0.0.0.0", "--port", "8000", "--no-access-log"]
                                    if service == "api" else ["nginx", "-c", "/observe/nginx.conf", "-g", "daemon off;"])
        if service == "web":
            entry["entrypoint"] = []  # Avoid root entrypoint writes; nginx runs as nginx with cap-drop ALL.
        else:
            entry["environment"].update(PYTHONDONTWRITEBYTECODE="1", REPORT_BATCH_ROWS="16", REPORT_PREVIEW_MAX_BYTES="65536",
                REPORT_DOWNLOAD_MAX_ROWS="300", REPORT_XLSX_MAX_ROWS="300", REPORT_MAX_JOIN_ROWS="3000")
            # The production ready probe intentionally writes a .ready file.
            # Verify it explicitly before the observation baseline below; use
            # the real read-only liveness endpoint for concurrent Docker checks.
            entry["healthcheck"] = {"test": ["CMD", "python", "-c",
                "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=3)"],
                "interval": "5s", "timeout": "5s", "retries": 20}
    (directory / "compose.json").write_text(json.dumps(configuration, indent=2), encoding="utf-8")
    guard.preflight(directory, context)
    return directory, context


class ProxyClient(cycle.Client):
    def __init__(self, port: int):
        super().__init__()
        self.port, self.base = port, f"http://127.0.0.1:{port}/api/v1"

    def headers(self) -> dict:
        return {"Content-Type": "application/json", "X-CSRF-Token": self.csrf,
                "Cookie": "; ".join(f"{cookie.name}={cookie.value}" for cookie in self.cookies)}

    def upload(self, path: Path):
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=180)
        try:
            headers = {**self.headers(), "Content-Type": "application/octet-stream", "Content-Length": str(path.stat().st_size)}
            connection.putrequest("POST", "/api/v1/datasets/uploads/stage?filename=" + urllib.parse.quote(path.name))
            for name, value in headers.items():
                connection.putheader(name, value)
            connection.endheaders()
            with path.open("rb") as source:
                while chunk := source.read(65536):
                    connection.send(chunk)
            response = connection.getresponse()
            value = json.loads(response.read())
            assert response.status == 201, "El upload sintético no fue admitido."
            return value["upload"]["id"]
        finally:
            connection.close()

    def disconnect(self, context: str, format_name: str) -> str:
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=60)
        connection.connect()
        assert connection.sock is not None
        connection.sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 1024)
        connection.request("POST", "/api/v1/reports/download", json.dumps({"context_id": context, "format": format_name}), self.headers())
        response = connection.getresponse()
        assert response.status == 200
        identity = response.headers["X-Report-Execution-Id"]
        assert response.read(1)
        # RST while the bounded response is in flight, never after consuming it.
        connection.sock.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0))
        connection.close()
        response.close()
        return identity

    def disconnect_preview(self, context: str) -> str:
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=60)
        try:
            connection.connect()
            assert connection.sock is not None
            connection.request("POST", "/api/v1/reports/preview", json.dumps({"context_id": context}), self.headers())
            deadline = time.monotonic() + 15
            while time.monotonic() < deadline:
                running = [item for item in self.call("/reports/executions?limit=10")["items"]
                           if item["context_id"] == context and item["status"] == "RUNNING"]
                if running:
                    connection.sock.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0))
                    return running[0]["id"]
                time.sleep(0.02)
            raise AssertionError("No se observó PREVIEW activo antes de desconectar al consumidor.")
        finally:
            connection.close()


def source_value(alias: str, index: int) -> str:
    # Deterministic broad text: CSV/XLSX both exceed proxy/socket buffers. Each
    # cell remains below XLSX 32767 characters and the API's 64 KiB cell budget.
    value = "".join(hashlib.sha256(f"{alias}:{index}:{part}".encode()).hexdigest() for part in range(440))
    return f"{alias}:á,\"{index}\":東京:" + value


def trace_positions(directory: Path) -> dict[str, int]:
    return {str(path): path.stat().st_size for path in (directory / "observer").glob("*/trace.*")}


def observe(directory: Path, baseline: dict[str, int]) -> dict:
    result = {}
    for service in ("api", "proxy"):
        segments = []
        for path in sorted((directory / "observer" / service).glob("trace.*")):
            with path.open("rb") as trace:
                trace.seek(baseline.get(str(path), 0))
                segments.append(trace.read().decode("utf-8", errors="strict"))
        result[service] = analyze_trace("\n".join(segments))
    return result


def storage_snapshot(directory: Path, context: dict) -> dict:
    script = '''import hashlib,json
from pathlib import Path
from sqlalchemy import select,func
from trackvance.db import SessionLocal
from trackvance.models import Artifact,DatasetVersion,Job
digest,files,total=hashlib.sha256(),0,0
for path in sorted(Path('/var/lib/trackvance').rglob('*')):
    if path.is_file():
        files+=1; total+=path.stat().st_size
        digest.update(str(path.relative_to('/var/lib/trackvance')).encode()+b'\\0')
        with path.open('rb') as source:
            while chunk:=source.read(1048576):digest.update(chunk)
with SessionLocal() as db:
    counts={name:db.scalar(select(func.count()).select_from(model)) for name,model in [('artifacts',Artifact),('versions',DatasetVersion)]}
    counts['jobs']=dict(db.execute(select(Job.status,func.count()).group_by(Job.status)).all())
active=0
for path in Path('/proc').glob('[0-9]*/cmdline'):
    try:
        if any(arg.endswith(b'/report_sandbox.py') for arg in path.read_bytes().split(b'\\0')):active+=1
    except OSError:pass
print(json.dumps(dict(counts,files=files,total_bytes=total,storage_sha256=digest.hexdigest(),active_report_children=active)))
'''
    output = guard.command([*guard.compose_args(directory, context), "exec", "-T", "api", "python", "-B", "-c", script])
    return json.loads(output)


def verify_trace_privacy(directory: Path) -> None:
    sentinel = "TV_HTTP_PRIVATE_BUFFER_SENTINEL_079431"
    for service, image in (("api", "backend"), ("proxy", "web")):
        private_run(["docker", "run", "--rm", "--network", "none", "--cap-drop", "ALL",
                     "--security-opt", "no-new-privileges", "--cpus", "0.25", "--memory", "128m", "--pids-limit", "32",
                     "--mount", f"type=bind,source={directory / 'observer' / service},target=/observe",
                     "--entrypoint", "strace", "trackvance-v080-http-debug:" + image,
                     "-yy", "-s", "0", "-e", "trace=write,writev", "-o", "/observe/privacy-check",
                     "/bin/sh", "-c", "printf " + sentinel], directory, service + "-privacy-check")
        trace = (directory / "observer" / service / "privacy-check").read_text(encoding="utf-8")
        if sentinel in trace or re.search(r"\b(?:write|writev)\(", trace) is None:
            raise RuntimeError("strace no ocultó el buffer o no produjo evidencia de escritura.")


def terminal(client: ProxyClient, identity: str) -> dict:
    return cycle.wait(client, "/reports/executions/" + identity, timeout=60)


def phase(progress: dict, stage: str, case: str | None = None) -> None:
    progress["stage"] = stage
    progress["current_case"] = case


def certify(directory: Path, context: dict, progress: dict) -> dict:
    progress.update(version="0.8.0", project=context["project"], sources=2, rows_per_source=120, cases=[])
    client = ProxyClient(context["port"])
    phase(progress, "SYNTHETIC_AUTHORIZATION")
    client.call("/auth/demo", {})
    phase(progress, "SYNTHETIC_GOVERNANCE")
    macro = client.call("/catalog/macrodomains", {"name": "HTTP ephemeral " + context["project"][-12:]}, expected=201)
    domain = client.call("/catalog/domains", {"name": "Syscall observation", "macro_domain_id": macro["id"]}, expected=201)
    cycle.source_value = source_value
    sources = []
    for alias in ("a", "b"):
        phase(progress, "REAL_ACQUISITION_AND_STRICT_INTAKE", alias)
        sources.append(cycle.acquire(client, directory, alias, 120, macro["id"], domain["id"])[0])
    broad = cycle.draft(sources, "FULL")
    narrow = {**broad, "columns": [column for column in broad["columns"] if column["column"] == "key"]}
    excessive = cycle.draft(sources)
    excessive["joins"][0].update(keys=[{"left_column": "zone", "right_column": "zone"}], expected_cardinality="N:M", allow_many_to_many=True)
    excessive["columns"] = narrow["columns"]
    frozen = {}
    for name, query in (("broad", broad), ("narrow", narrow), ("excessive", excessive), ("preview_disconnect", narrow)):
        phase(progress, "FREEZE_JOINT_CONTEXT", name)
        frozen[name] = client.call("/reports/resolve", {"draft": query})["context_id"]
    phase(progress, "VERIFY_FIXTURE_JOBS_TERMINAL")
    progress["fixture_job_counts_before_stop"] = storage_snapshot(directory, context)["jobs"]
    assert progress["fixture_job_counts_before_stop"] == {"SUCCESS": 4}, "Los cuatro trabajos de adquisición/Intake deben terminar antes del baseline."
    phase(progress, "STOP_COMPLETED_FIXTURE_WORKERS")
    private_run([*guard.compose_args(directory, context), "stop", "worker", "acquisition-worker"],
                directory, "freeze-fixture-workers")
    phase(progress, "STORAGE_AND_METADATA_BASELINE")
    assert client.call("/health/ready")["status"] == "ready"
    before = storage_snapshot(directory, context)
    progress["metadata_storage_baseline"] = before
    assert before["active_report_children"] == 0
    expected = cycle.rows_hash(cycle.oracle_rows("FULL", 120))
    cases = progress["cases"]

    def record(name: str, baseline: dict, execution: dict, **evidence):
        phase(progress, "VERIFY_STORAGE_METADATA_AND_SYSCALLS", name)
        after = storage_snapshot(directory, context)
        progress["last_storage_snapshot"] = after
        progress["last_execution"] = {key: execution.get(key) for key in ("status", "generation_status", "transmission_status", "error_code")}
        observed = observe(directory, baseline)
        progress["last_observation"] = observed
        assert after == before, "Un flujo HTTP alteró artefactos/versiones/almacén o dejó un ejecutor activo."
        if any(item["status"] != "PASS" for item in observed.values()):
            raise RuntimeError("Escritura o evidencia incompleta; ver contadores públicos y traza privada.")
        cases.append({"name": name, "status": "PASS", "execution_status": execution["status"],
                      "generation": execution["generation_status"], "transmission": execution["transmission_status"],
                      "error_code": execution.get("error_code"), "during_execution": observed,
                      "storage_and_artifact_metadata_unchanged": True, "active_report_children_after": 0, **evidence})

    baseline = trace_positions(directory)
    phase(progress, "EXECUTE_HTTP_CASE", "PREVIEW_SUCCESS")
    preview = client.call("/reports/preview", {"context_id": frozen["narrow"]})
    assert len(preview["rows"]) == 10 and preview["sample"] and preview["total_rows"] is None
    assert preview["rows"] == [{"a_key": row[0], "b_key": row[2]} for row in list(cycle.oracle_rows("FULL", 120))[:10]]
    record("PREVIEW_SUCCESS", baseline, terminal(client, preview["execution_id"]), sampled_rows=10)
    # A valid typed parameter makes the real resolve request exceed nginx's
    # default client-body buffer. Observe it too, so request spooling cannot
    # escape a gate that only transmits tiny context-id request bodies.
    baseline = trace_positions(directory)
    phase(progress, "EXECUTE_HTTP_CASE", "PREVIEW_LARGE_CONTEXT_REQUEST")
    wide_request = {**narrow, "mode": "SQL", "sql": 'SELECT a.key AS a_key,b.key AS b_key FROM a FULL OUTER JOIN b ON a.key=b.key AND a.zone=b.zone WHERE a.key<>$excluded ORDER BY a.key ASC,b.key ASC',
                    "parameters": [{"name": "excluded", "type": "TEXT", "value": "synthetic-unused-" + "x" * 32768}]}
    assert len(json.dumps({"draft": wide_request}).encode()) > 32768
    extra_context = client.call("/reports/resolve", {"draft": wide_request})
    extra_preview = client.call("/reports/preview", {"context_id": extra_context["context_id"]})
    assert extra_preview["rows"] == preview["rows"]
    record("PREVIEW_LARGE_CONTEXT_REQUEST", baseline, terminal(client, extra_preview["execution_id"]), request_bytes_over=32768)
    baseline = trace_positions(directory)
    phase(progress, "EXECUTE_HTTP_CASE", "PREVIEW_RESOURCE_FAILURE")
    rejected = client.call("/reports/preview", {"context_id": frozen["broad"]}, expected=422)
    error = rejected["error"]
    assert error["code"] == "REPORT_RESULT_LIMIT" and error["details"]["allow_generate"] is True
    record("PREVIEW_RESOURCE_FAILURE", baseline, terminal(client, error["details"]["execution_id"]))
    baseline = trace_positions(directory)
    phase(progress, "EXECUTE_HTTP_CASE", "PREVIEW_CONSUMER_DISCONNECT")
    abandoned_preview = terminal(client, client.disconnect_preview(frozen["preview_disconnect"]))
    # PREVIEW is bounded synchronous generation. Its metadata deliberately has
    # no browser-transmission claim; it must finish/close the child even when
    # the consumer leaves before JSON serialization is transmitted.
    assert abandoned_preview["status"] in {"SUCCESS", "INTERRUPTED"} and abandoned_preview["transmission_status"] == "NOT_APPLICABLE"
    record("PREVIEW_CONSUMER_DISCONNECT", baseline, abandoned_preview, consumer_disconnected_while_running=True)
    for format_name in ("CSV", "XLSX"):
        baseline = trace_positions(directory)
        phase(progress, "EXECUTE_HTTP_CASE", format_name + "_SUCCESS")
        with client.stream(frozen["broad"], format_name) as response:
            identity = response.headers["X-Report-Execution-Id"]
            assert response.headers.get("Cache-Control") == "no-store"
            content = bytearray()
            while chunk := response.read(16384):
                content.extend(chunk)
                time.sleep(0.002)  # Keep both proxy and API transmitting under consumer backpressure.
        if format_name == "CSV":
            reader = csv.reader(io.StringIO(content.decode("utf-8")))
            assert next(reader) == ["a_key", "a_value", "b_key", "b_value"]
            actual = cycle.rows_hash([cycle.decode_csv(value) for value in row] for row in reader)
        else:
            rows = iter(cycle.xlsx_rows(content))
            assert next(rows) == ["a_key", "a_value", "b_key", "b_value"]
            actual = cycle.rows_hash(rows)
        assert actual == expected and len(content) > 1024 * 1024, "El oráculo completo o el tamaño real no coincide."
        complete = terminal(client, identity)
        assert complete["status"] == "SUCCESS" and complete["transmission_status"] == "COMPLETE"
        record(format_name + "_SUCCESS", baseline, complete, bytes=len(content), rows=actual["rows"], population_sha256=actual["sha256"])
        baseline = trace_positions(directory)
        phase(progress, "EXECUTE_HTTP_CASE", format_name + "_RESOURCE_FAILURE")
        identity = None
        try:
            with client.stream(frozen["excessive"], format_name) as response:
                identity = response.headers["X-Report-Execution-Id"]
                while response.read(65536):
                    pass
        except (http.client.IncompleteRead, OSError):
            pass  # Midstream failure: committed execution state is authoritative.
        assert identity is not None
        failed = terminal(client, identity)
        assert failed["status"] == "FAILED" and failed["error_code"] == "REPORT_RESULT_LIMIT"
        assert failed["transmission_status"] == "INTERRUPTED" and failed["output_version_id"] is None
        record(format_name + "_RESOURCE_FAILURE", baseline, failed)
        baseline = trace_positions(directory)
        phase(progress, "EXECUTE_HTTP_CASE", format_name + "_CONSUMER_DISCONNECT")
        interrupted = terminal(client, client.disconnect(frozen["broad"], format_name))
        assert interrupted["status"] == "INTERRUPTED" and interrupted["transmission_status"] == "INTERRUPTED"
        record(format_name + "_CONSUMER_DISCONNECT", baseline, interrupted)
    assert len(cases) == 10
    phase(progress, "COMPLETE")
    return {**progress, "status": "PASS", "trace_scope": "REAL_NGINX_API_AND_CONFINED_CHILDREN",
            "baseline": "AFTER_STARTUP_REAL_ACQUISITION_STRICT_APPROVAL_AND_FROZEN_CONTEXTS",
            "privileges_added": False, "raw_traces_published": False, "metadata_storage_baseline": before}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=32086)
    parser.add_argument("--main-project")
    parser.add_argument("--context", type=Path)
    parser.add_argument("--prepare-only", action="store_true")
    parser.add_argument("--reuse-debug-images", action="store_true")
    parser.add_argument("--keep", action="store_true")
    args = parser.parse_args()
    directory, context = guard.load_context(args.context) if args.context else prepare(args.port, args.main_project)
    if not re.fullmatch(r"trackvance-v080-test-reports-http-[a-f0-9]{12}", context["project"]):
        raise guard.IsolationError("La observación HTTP requiere su propio proyecto UUID; no admite contextos de otros ensayos.")
    proxy_configuration = directory / "observer/proxy/reports.conf"
    if proxy_configuration.read_bytes() != (ROOT / "deploy/nginx/default.conf").read_bytes():
        raise guard.IsolationError("La configuración Nginx cambió desde la preparación; crea un contexto de observación nuevo.")
    if args.prepare_only:
        print(json.dumps({"status": "PREPARED", "context": str(directory), "project": context["project"]}))
        return
    summary, started, began = {"status": "FAIL", "check": "REPORT_EPHEMERAL_HTTP", "raw_traces_published": False}, False, time.monotonic()
    try:
        phase(summary, "RESOLVED_ISOLATION_PREFLIGHT")
        guard.preflight(directory, context)
        if not args.reuse_debug_images:
            for service, image in (("backend", "backend"), ("web", "web")):
                phase(summary, "BUILD_DIAGNOSTIC_IMAGE", service)
                private_run(["docker", "build", "-t", "trackvance-v080-http-debug:" + image,
                             "-f", str(directory / (service + ".Dockerfile")), str(directory)], directory, service + "-debug-build")
        phase(summary, "VERIFY_TRACE_BUFFER_REDACTION")
        verify_trace_privacy(directory)
        started = True
        phase(summary, "START_PRIVATE_BOUNDED_PROJECT")
        private_run([*guard.compose_args(directory, context), "up", "--no-build", "--detach", "--wait", "--wait-timeout", "240",
                     "postgres", "api", "worker", "acquisition-worker", "web"], directory, "startup")
        summary = certify(directory, context, summary)
        summary["nginx_configuration_sha256"] = hashlib.sha256(proxy_configuration.read_bytes()).hexdigest()
    except Exception as exc:  # noqa: BLE001 - publish a sanitized failed gate and keep diagnostics private.
        summary["error_type"] = type(exc).__name__
        code = re.search(r"code=([A-Z][A-Z0-9_]{0,79})\b", str(exc))
        if code:
            summary["api_error_code"] = code.group(1)
        # Sanitized public report; traceback may contain source/SQL/URLs and stays private.
        import traceback
        (directory / "failure.private.log").write_text(traceback.format_exc(), encoding="utf-8")
        for service in ("api", "web", "worker", "acquisition-worker"):
            try:
                private_run([*guard.compose_args(directory, context), "logs", "--no-color", service], directory, service + "-failure")
            except (RuntimeError, subprocess.TimeoutExpired, OSError) as diagnostic_error:
                summary.setdefault("unavailable_diagnostics", []).append(type(diagnostic_error).__name__)
    finally:
        summary.update(duration_seconds=round(time.monotonic() - began, 3), main_unchanged=guard.inventory(context["main_project"]) == context["main_before"])
        if not summary["main_unchanged"]:
            summary["status"] = "FAIL"
        (directory / "reports-ephemeral-http.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
        if started and not args.keep:
            guard.preflight(directory, context)
            guard.command([*guard.compose_args(directory, context), "down", "--volumes", "--remove-orphans"])
        print(json.dumps({"status": summary["status"], "evidence": str(directory / "reports-ephemeral-http.json"), "main_unchanged": summary["main_unchanged"]}))
    if summary["status"] != "PASS":
        raise SystemExit(1)


if __name__ == "__main__":
    main()
