"""Owned synthetic Linux channel diagnosis; never backend certification.

The protected 0.8.0 image supplies only CPython/libseccomp. Frozen wheels and
read-only 806 source supply the executor. No usual data, volumes, environment,
network or image writes are used. Rejected lines retain metadata, never cells.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import threading
import time
import tomllib
import urllib.request
import uuid
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SITE = "/app/backend/.venv/lib/python3.12/site-packages"
HOOK = "    started = time.monotonic()\n"
SETTINGS = (
    '    sys.stderr.write(json.dumps({"kind":"NATIVE_SETTINGS","engine":duckdb.__version__, '
    '"file_mode":hasattr(sys.modules["__main__"],"__file__"),"settings":dict(connection.execute('
    '"SELECT name,value FROM duckdb_settings() WHERE name IN '
    "('enable_progress_bar','enable_progress_bar_print','progress_bar_time')"
    '").fetchall())})+"\\n")\n'
    "    sys.stderr.flush()\n"
)


def private_child(source):
    if source.count(HOOK) != 1:
        raise ValueError("Settings diagnostic hook drifted.")
    result = source.replace(HOOK, SETTINGS + HOOK)
    assert result.replace(SETTINGS, "") == source
    return result


def line_failure(line, error):
    """No cell contents, paths, environment or exception message are retained."""
    return {"class": type(error).__name__, "bytes": len(line),
            "newline": line.endswith(b"\n"), "sha256": hashlib.sha256(line).hexdigest(),
            "carriage_returns": line.count(b"\r"),
            "progress_marker": bool(re.search(rb"\r\s*[0-9]{1,3}%", line)),
            "json_position": getattr(error, "pos", None), "errno": getattr(error, "errno", None)}


def encoded_row(alias, index):
    return [f"{index:08d}ñ", f'{alias}:á,{index}:"texto"']


def native():
    import _duckdb

    if sys.platform != "linux" or sys.version_info[:2] != (3, 12):
        raise ValueError("Exact Linux CPython 3.12 required.")
    scratch = Path("/tmp/channel-probe")
    scratch.mkdir()
    context = json.loads(Path("/probe/context.json").read_text())
    rows = context["rows_per_source"]
    connection = _duckdb.connect(":memory:", config={"threads": 2, "memory_limit": "512MB",
                                                    "temp_directory": "", "max_temp_directory_size": "0B"})
    connection.execute("SET enable_progress_bar=false")
    sources = []
    for alias, first in (("a", 0), ("b", rows // 2)):
        path = scratch / (alias + ".parquet")
        connection.execute(f"""COPY (SELECT lpad(CAST(i AS VARCHAR),8,'0')||'ñ' AS key,
            'z'||CAST(i%7 AS VARCHAR) AS zone,
            '{alias}:á,'||CAST(i AS VARCHAR)||':"texto"' AS value,
            CAST(i+1 AS VARCHAR)||'.12345678' AS amount
            FROM range({first},{first + rows}) t(i)) TO '{path}' (FORMAT PARQUET)""")
        sources.append({"alias": alias, "paths": [str(path)], "schema": context["schema"]})
    connection.close()
    payload = {"sources": sources, "plan": context["plan"], "limits": context["limits"],
               "profile": "DOWNLOAD", "staging": None, "probe": None}
    content = json.dumps(payload, ensure_ascii=False).encode() + b"\n"
    expected_hash = hashlib.sha256()
    for index in range(rows // 2, rows // 2 + 100000):
        expected_hash.update(json.dumps(encoded_row("a", index) + encoded_row("b", index),
                                       ensure_ascii=False, separators=(",", ":")).encode() + b"\n")
    result = {"kind": "SYNTHETIC_LINUX_CHANNEL_COMPONENT", "engine": _duckdb.__version__,
              "runtime_python": sys.version.split()[0], "source_rows": rows,
              "expected_rows": 100000, "expected_sha256": expected_hash.hexdigest(),
              "parquet": [{"sha256": hashlib.file_digest(path.open("rb"), "sha256").hexdigest(),
                            "bytes": path.stat().st_size} for path in scratch.glob("*.parquet")], "cases": []}
    environment = {"PATH": "/usr/local/bin:/usr/bin:/bin", "LANG": "C.UTF-8", "PYTHONDONTWRITEBYTECODE": "1",
                   "HOME": "/nonexistent", "OPENBLAS_NUM_THREADS": "1", "OMP_NUM_THREADS": "1", "TZ": "UTC"}
    environment["LD_LIBRARY_PATH"] = str(Path(sys.base_prefix) / "lib")
    for name, command in (
        ("PRODUCTION_FILE", [sys.executable, "-I", "-B", SITE + "/trackvance/report_sandbox.py"]),
        ("SETTINGS_FILE", [sys.executable, "-I", "-B", SITE + "/trackvance/report_channel_private.py"]),
        ("INTERACTIVE_CONTROL", [sys.executable, "-I", "-B", "-c",
                                  Path(SITE + "/trackvance/report_channel_private.py").read_text()]),
    ):
        started = time.monotonic()
        child = subprocess.Popen(command, env=environment, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                 stderr=subprocess.PIPE, close_fds=True, bufsize=65536)
        diagnostic = bytearray()

        def stderr_reader(process=child, retained=diagnostic):
            while chunk := process.stderr.read(1024):
                if len(retained) + len(chunk) > 8192:
                    process.kill()
                    return
                retained.extend(chunk)

        reader = threading.Thread(target=stderr_reader, daemon=True)
        reader.start()
        child.stdin.write(content)
        child.stdin.close()
        case = {"name": name, "rows": 0, "batches": 0, "complete": False, "rejected": None}
        hashed = hashlib.sha256()
        try:
            while True:
                line = child.stdout.readline(payload["limits"]["batch_bytes"] + 1024)
                if not line:
                    break
                try:
                    message = json.loads(line)
                except (ValueError, OSError) as error:
                    case["rejected"] = line_failure(line, error)
                    break
                if message["kind"] == "batch":
                    for row in message["rows"]:
                        hashed.update(json.dumps(row, ensure_ascii=False, separators=(",", ":")).encode() + b"\n")
                    case["rows"] += len(message["rows"])
                    case["batches"] += 1
                    time.sleep(0.025)  # Real pipe backpressure; no engine/query/output substitution.
                elif message["kind"] == "complete":
                    case["complete"] = True
                    case["metrics"] = message
                elif message["kind"] == "error":
                    case["error_code"] = message["code"]
            if case["rejected"] and child.poll() is None:
                child.kill()
            case["exit_code"] = child.wait(timeout=10)
        finally:
            if child.poll() is None:
                child.kill()
                child.wait(timeout=5)
            child.stdout.close()
            reader.join(timeout=5)
        case["settings"] = [json.loads(line) for line in diagnostic.splitlines()]
        case["sha256"] = hashed.hexdigest()
        case["full_oracle"] = (case["rows"] == 100000 and case["sha256"] == result["expected_sha256"]
                               and case["complete"] and case["exit_code"] == 0)
        case["duration_seconds"] = round(time.monotonic() - started, 3)
        result["cases"].append(case)
    result["cgroups"] = {name: Path("/sys/fs/cgroup", name).read_text().strip()
                         for name in ("memory.max", "memory.peak", "memory.events", "cpu.max")}
    return result


def host(args):
    sys.path.insert(0, str(ROOT / "scripts"))
    from ci import owned_cleanup
    from delivery_native_query_probe import bounded_process

    os.environ["DOCKER_CONFIG"], os.environ["DOCKER_HOST"] = str(args.docker_config.resolve()), args.docker_host
    os.environ.pop("DOCKER_AUTH_CONFIG", None)
    os.environ.pop("DOCKER_CONTEXT", None)
    config = json.loads((args.docker_config / "config.json").read_text())
    if config.get("auths") != {"https://index.docker.io/v1/": {}} or config.get("credsStore") or config.get("credHelpers"):
        raise ValueError("Explicit anonymous context required.")
    image = json.loads(owned_cleanup.docker("image", "inspect", "--format",
        '{"id":{{json .Id}},"labels":{{json .Config.Labels}},"os":{{json .Os}},"arch":{{json .Architecture}}}', args.image_id))
    if (image["id"] != args.image_id or image["labels"].get("org.opencontainers.image.revision") != args.image_revision
            or image["labels"].get("org.opencontainers.image.version") != "0.8.0"
            or image["os"] != "linux" or image["arch"] != "amd64"):
        raise ValueError("Protected runtime image drifted.")
    if subprocess.check_output(["git", "status", "--porcelain", "--untracked-files=no"], cwd=ROOT).strip():
        raise ValueError("Commit the diagnostic harness before execution.")
    project = "trackvance-v070-test-channel-probe-" + uuid.uuid4().hex[:12]
    directory = args.output.parent / project
    directory.mkdir(parents=True)
    package = directory / "trackvance"
    package.mkdir()
    files = {}
    for name in ("report_sandbox.py", "portable_temporal.py"):
        content = subprocess.check_output(["git", "show", args.source_sha + ":backend/src/trackvance/" + name], cwd=ROOT)
        (package / name).write_bytes(content)
        files[name] = hashlib.sha256(content).hexdigest()
    (package / "report_channel_private.py").write_text(private_child((package / "report_sandbox.py").read_text()), encoding="utf-8")
    lock = tomllib.loads(subprocess.check_output(["git", "show", args.source_sha + ":backend/uv.lock"], cwd=ROOT).decode())
    wheels = []
    mount = []
    for name in ("duckdb", "sqlglot"):
        entry = next(item for item in lock["package"] if item["name"] == name)
        wheel = next(item for item in entry["wheels"] if ("cp312-cp312-manylinux_2_26_x86_64" in item["url"]
                                                        if name == "duckdb" else "py3-none-any" in item["url"]))
        destination = directory / (name + ".whl")
        with urllib.request.urlopen(wheel["url"], timeout=60) as response, destination.open("wb") as output:
            while chunk := response.read(1024 * 1024):
                output.write(chunk)
        with destination.open("rb") as stream:
            actual = hashlib.file_digest(stream, "sha256").hexdigest()
        if actual != wheel["hash"].removeprefix("sha256:") or destination.stat().st_size != wheel["size"]:
            raise ValueError("Frozen wheel hash/size mismatch.")
        extracted = directory / name
        extracted.mkdir()
        with zipfile.ZipFile(destination) as archive:
            for entry in archive.infolist():
                target = extracted / entry.filename
                if not target.resolve().is_relative_to(extracted.resolve()):
                    raise ValueError("Unsafe wheel path.")
                if not entry.is_dir():
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_bytes(archive.read(entry))
        target = next(extracted.glob("_duckdb*.so")) if name == "duckdb" else extracted / "sqlglot"
        mount.extend(["--mount", f"type=bind,src={target.resolve()},dst={SITE}/{target.name},readonly"])
        wheels.append({"name": name, "sha256": actual, "bytes": wheel["size"]})
    sys.path.insert(0, str(ROOT / "backend/src"))
    from trackvance.report_config import ReportLimits
    from trackvance.report_query import compile_draft

    schema = [{"name": name, "logical_type": "DECIMAL" if name == "amount" else "STRING", "nullable": False}
              for name in ("key", "zone", "value", "amount")]
    draft = {"mode": "SQL", "sources": [{"alias": alias, "input_dataset_id": str(uuid.uuid4())} for alias in ("a", "b")],
             "sql": "SELECT a.key AS a_key,a.value AS a_value,b.key AS b_key,b.value AS b_value FROM a INNER JOIN b ON a.key=b.key AND a.zone=b.zone ORDER BY a.key,b.key LIMIT 100000"}
    context = {"rows_per_source": 1000000, "schema": schema, "plan": compile_draft(draft, {"a": schema, "b": schema}),
               "limits": ReportLimits.configured("DOWNLOAD").dto()}
    if (context["limits"]["memory_bytes"] != 512 * 1024**2
            or context["limits"]["process_memory_bytes"] != 2048 * 1024**2
            or context["limits"]["threads"] != 2 or context["limits"]["batch_rows"] != 512):
        raise ValueError("Exact diagnostic engine/process/thread/batch limits required.")
    (directory / "context.json").write_text(json.dumps(context), encoding="utf-8")
    before = owned_cleanup.snapshot()
    receipt = {"schema_version": 1, "kind": "OWNED_SYNTHETIC_CHANNEL_COMPONENT", "project": project,
               "runtime_image": image, "product_source_sha": args.source_sha, "source_files": files,
               "harness_sha": subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip(),
               "wheels": wheels, "limits": context["limits"], "cpu_limit": 2, "memory_limit_bytes": 4 * 1024**3,
               "before": {key: sorted(values) for key, values in before.items()}, "status": "REGISTERED"}

    def save():
        temporary = args.output.with_suffix(".partial")
        with temporary.open("w", encoding="utf-8") as stream:
            json.dump(receipt, stream, ensure_ascii=False, indent=2)
            stream.flush()
            os.fsync(stream.fileno())
        temporary.replace(args.output)

    save()
    try:
        identifier = owned_cleanup.docker("create", "--name", project,
            "--label", "com.docker.compose.project=" + project, "--network", "none", "--read-only",
            "--cpus", "2", "--memory", "4g", "--memory-swap", "4g", "--pids-limit", "128",
            "--tmpfs", "/tmp:rw,nosuid,nodev,size=256m,mode=1777",
            "--mount", f"type=bind,src={directory.resolve()},dst=/probe,readonly",
            "--mount", f"type=bind,src={Path(__file__).resolve()},dst=/probe/channel_probe.py,readonly",
            *mount, *[argument for name in (*files, "report_channel_private.py")
                      for argument in ("--mount", f"type=bind,src={package / name},dst={SITE}/trackvance/{name},readonly")],
            "--entrypoint", "python", args.image_id, "/probe/channel_probe.py", "--native").strip()
        receipt["container_id"] = identifier
        save()
        status, stdout, stderr = bounded_process(["docker", "start", "--attach", identifier], timeout=300)
        receipt.update(exit_code=status, native=json.loads(stdout) if status == 0 else None,
                       stderr=stderr.decode(errors="replace")[:8192], status="DIAGNOSED" if status == 0 else "FAILED")
    finally:
        receipt["cleanup"] = owned_cleanup.cleanup(before, projects={project})
        receipt["after"] = {key: sorted(values) for key, values in owned_cleanup.snapshot().items()}
        save()
    print(json.dumps({key: receipt[key] for key in ("project", "status", "cleanup")}))
    return 0 if receipt["status"] == "DIAGNOSED" else 1


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--native", action="store_true")
    parser.add_argument("--image-id")
    parser.add_argument("--image-revision")
    parser.add_argument("--source-sha")
    parser.add_argument("--docker-config", type=Path)
    parser.add_argument("--docker-host")
    parser.add_argument("--output", type=Path)
    arguments = parser.parse_args()
    if arguments.native:
        print(json.dumps(native(), ensure_ascii=False))
    else:
        raise SystemExit(host(arguments))
