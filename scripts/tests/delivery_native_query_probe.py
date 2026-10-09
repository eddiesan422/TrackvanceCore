"""Owned, tiny Linux diagnostic: real Intake canonical input and confined report child.

No API stack, external SQL, new image, shared storage, or sandbox changes. The
private second child changes only the handled DuckDB exception's stderr receipt.
Its output is synthetic and bounded; this is diagnosis, not Delivery certification.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import io
import json
import os
import re
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
BOUND = 64 * 1024
HOOK = '        elif duckdb is not None and isinstance(exc, duckdb.Error):\n'
DIAGNOSTIC = (
    '            sys.stderr.write(json.dumps({"kind":"SYNTHETIC_DUCKDB_EXCEPTION",'
    '"class":type(exc).__name__,"message":str(exc)[:8192]},ensure_ascii=False)+"\\n")\n'
    '            sys.stderr.flush()\n'
)


def private_child(source: str) -> str:
    if source.count(HOOK) != 1:
        raise ValueError("Native exception handler changed; diagnostic hook refused.")
    changed = source.replace(HOOK, HOOK + DIAGNOSTIC)
    assert changed.replace(HOOK + DIAGNOSTIC, HOOK) == source
    return changed


def digest(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def bounded_process(command, *, environment=None, content=None, timeout=120):
    """Drain two pipes while retaining at most BOUND bytes per channel."""
    child = subprocess.Popen(command, env=environment, stdin=subprocess.PIPE,
                             stdout=subprocess.PIPE, stderr=subprocess.PIPE, close_fds=True)
    captured = {"stdout": bytearray(), "stderr": bytearray()}
    overflow = threading.Event()

    def read(channel):
        pipe = getattr(child, channel)
        assert pipe is not None
        with pipe:
            while chunk := pipe.read(4096):
                retained = captured[channel]
                remaining = BOUND - len(retained)
                retained.extend(chunk[:remaining])
                if len(chunk) > remaining:
                    overflow.set()
                    child.kill()
                    return

    readers = [threading.Thread(target=read, args=(channel,), daemon=True) for channel in captured]
    for reader in readers:
        reader.start()
    try:
        assert child.stdin is not None
        if content:
            child.stdin.write(content)
        child.stdin.close()
        status = child.wait(timeout=timeout)
    finally:
        if child.poll() is None:
            child.kill()
        child.wait(timeout=5)
        for reader in readers:
            reader.join(timeout=5)
    if overflow.is_set() or any(reader.is_alive() for reader in readers):
        raise ValueError("Synthetic diagnostic channel exceeded its bound.")
    return status, bytes(captured["stdout"]), bytes(captured["stderr"])


def native() -> dict:
    """Run only inside the bounded disposable, network-free Linux container."""
    if sys.platform != "linux":
        raise RuntimeError("Linux confinement required.")
    scratch = Path("/tmp/native-query-probe")
    scratch.mkdir()
    os.environ.update({"DATABASE_URL": f"sqlite:///{scratch / 'metadata.db'}",
                       "TRACKVANCE_STORAGE_DIR": str(scratch / "storage"),
                       "TRACKVANCE_STORAGE_ROOT": str(scratch / "storage"),
                       "DEMO_ACCESS_ENABLED": "false", "DEMO_SEED_ENABLED": "false",
                       "REPORT_MEMORY_MB": "512", "REPORT_PROCESS_MEMORY_MB": "2048",
                       "REPORT_THREADS": "2", "REPORT_CONCURRENCY": "1"})
    import delivery_typed_chain as chain
    import polars as pl
    from trackvance import identity_bootstrap, services  # noqa: F401
    from trackvance.artifactstore import storage_provider
    from trackvance.db import Base, SessionLocal, engine
    from trackvance.governance import strict_approval
    from trackvance.models import Artifact, Configuration, Dataset, DatasetVersion
    from trackvance.operations_common import OperationError
    from trackvance.report_config import ReportLimits
    from trackvance.report_executor import execute_messages
    from trackvance.report_query import compile_draft

    names = ["record_id", "customer_name", "amount", "quantity", "happened_on",
             "updated_at", "is_active", "optional_note", "all_null_int"]
    rows = [
        ["A-001", "José Álvarez", "10.50", 1, "2026-09-20", "2026-09-20T10:00:00+00:00", True, "000001", None],
        ["B-002", "Miyuki 東京", "20.25", 2, "2026-09-21", "2026-09-21T11:30:00.123456-05:00", False, None, None],
        ["C-003", "Zoë", "30.00", 3, "2026-09-22", "2026-09-22T12:45:00+00:00", True, "000003", None],
    ]
    content = io.StringIO(newline="")
    writer = csv.writer(content)
    writer.writerow(names)
    writer.writerows(rows)
    source_path = scratch / "synthetic.csv"
    source_path.write_text(content.getvalue(), encoding="utf-8", newline="")
    Base.metadata.create_all(engine)
    with SessionLocal() as db:
        dataset = Dataset(name="Synthetic native query diagnosis")
        db.add(dataset)
        db.flush()
        original = services.create_version(db, dataset, source_path, source_path.name,
            column_overrides={name: {"logical_type": kind,
                "semantic_tag": "IDENTIFIER" if index == 0 else None}
                for index, (name, kind) in enumerate(zip(names, chain.TYPES, strict=True))})
        configuration = Configuration(name="Synthetic strict POLARS", module="intake", dataset_id=dataset.id,
            config={"required_columns": ["record_id"], "unique_columns": ["record_id"], "max_error_rate": 0})
        db.add(configuration)
        db.flush()
        run = services.enqueue(db, configuration, original, None, "Synthetic diagnostic", requested_engine="POLARS")
        db.commit()
        services.execute_run(db, run)
        db.commit()
        assert run.status == "SUCCESS" and strict_approval(db, run)["approved"], run.error
        output = db.get(DatasetVersion, run.output_version_id)
        artifact = db.get(Artifact, output.canonical_artifact_id)
        paths = storage_provider.dataset_paths(artifact)
        frames = [pl.read_parquet(path) for path in paths]
        frame = pl.concat(frames)
        # rows() returns tuples, whereas the independent oracle requires arrays.
        actual = chain.normalize_canonical_rows([list(row) for row in frame.rows()])
        assert actual == chain.normalize_rows(rows)
        sources = [{"alias": "a", "schema": output.schema_json, "paths": [str(path) for path in paths]}]
        draft = chain.report_draft(dataset.id, configuration.id, names)
        plan = compile_draft(draft, {"a": output.schema_json})
        staging = scratch / "report-staging"
        staging.mkdir()
        limits = ReportLimits.configured("DATASET").dto()
        result = {"schema_version": 1, "kind": "SYNTHETIC_NATIVE_QUERY_DIAGNOSTIC",
                  "intake": {"status": run.status, "decision": run.decision, "execution_plan": run.execution_plan,
                             "metrics": run.metrics, "input_version_id": original.id, "output_version_id": output.id},
                  "source_sha256": digest(source_path), "logical_values_sha256": chain.content_hash(actual),
                  "source_schema": output.schema_json,
                  "physical_schema": [{"name": name, "type": str(kind)} for name, kind in frame.schema.items()],
                  "canonical_sha256": artifact.sha256,
                  "parquet": [{"sha256": digest(path), "bytes": path.stat().st_size} for path in paths],
                  "sql": plan["sql"], "plan": plan, "limits": limits, "rows": 3, "columns": 9}
        try:
            result["production_messages"] = list(execute_messages(sources, plan, "DATASET", staging=staging))
            result["production_status"] = "COMPLETE"
        except OperationError as exc:
            result["production_status"] = "FAILED"
            result["production_error"] = {"code": exc.code, "message": exc.message}
        import trackvance.report_executor as executor
        script = Path(executor.__file__).with_name("report_sandbox.py")
        source = script.read_text(encoding="utf-8")
        result["sandbox_sha256"] = hashlib.sha256(source.encode()).hexdigest()
        code = private_child(source)
        environment = {"PATH": "/usr/local/bin:/usr/bin:/bin", "LANG": "C.UTF-8",
                       "PYTHONDONTWRITEBYTECODE": "1", "HOME": "/nonexistent",
                       "OPENBLAS_NUM_THREADS": "1", "OMP_NUM_THREADS": "1", "TZ": "UTC"}
        runtime_library = Path(sys.base_prefix) / "lib"
        if runtime_library.is_dir():
            environment["LD_LIBRARY_PATH"] = str(runtime_library.resolve())
        payload = {"sources": sources, "plan": plan, "limits": limits, "profile": "DATASET",
                   "staging": str(staging), "probe": None}
        status, stdout, stderr = bounded_process([sys.executable, "-I", "-B", "-c", code],
            environment=environment, content=json.dumps(payload, ensure_ascii=False).encode() + b"\n")
        # Exact fixture is only 27 cells. The exception hook itself caps stderr to 8 KiB.
        if len(stdout) > BOUND or len(stderr) > 16384:
            raise ValueError("Synthetic diagnostic channel exceeded its bound.")
        result["private_exit_code"] = status
        result["private_messages"] = [json.loads(line) for line in stdout.splitlines()]
        result["private_exception"] = [json.loads(line) for line in stderr.splitlines()]
        result["cgroups"] = {name: Path("/sys/fs/cgroup", name).read_text().strip()
            for name in ("memory.max", "memory.current", "memory.peak", "memory.events", "cpu.max")
            if Path("/sys/fs/cgroup", name).is_file()}
        return result


def host(args) -> int:
    sys.path.insert(0, str(ROOT / "scripts"))
    from ci import owned_cleanup

    os.environ["DOCKER_CONFIG"] = str(args.docker_config.resolve())
    os.environ["DOCKER_HOST"] = args.docker_host
    os.environ.pop("DOCKER_AUTH_CONFIG", None)
    os.environ.pop("DOCKER_CONTEXT", None)
    config = json.loads((args.docker_config / "config.json").read_text())
    if (config.get("auths") != {"https://index.docker.io/v1/": {}}
            or config.get("credsStore") or config.get("credHelpers")):
        raise ValueError("Private anonymous Docker configuration required.")
    registry = json.loads(args.image_registry.read_text())
    builds = [row for row in registry["builds"] if row["role"] == "backend" and row["status"] == "OWNED"
              and row["source_sha"] == args.runtime_sha]
    if len(builds) != 1:
        raise ValueError("Exactly one registered backend image required.")
    build = builds[0]
    image_id = build["image"]["image_id"]
    if not re.fullmatch(r"sha256:[0-9a-f]{64}", image_id):
        raise ValueError("Immutable Engine image identity required.")
    inspected = json.loads(owned_cleanup.docker("image", "inspect", "--format",
        '{"id":{{json .Id}},"labels":{{json .Config.Labels}},"os":{{json .Os}},"arch":{{json .Architecture}}}', image_id))
    if (inspected["id"] != image_id or inspected["labels"] != build["expected_labels"]
            or inspected["os"] != "linux" or inspected["arch"] != "amd64"):
        raise ValueError("Registered immutable Linux backend image drifted.")
    git = lambda *values: subprocess.check_output(["git", *values], cwd=ROOT, text=True).strip()
    if git("rev-parse", args.runtime_sha + ":backend") != git("rev-parse", "HEAD:backend"):
        raise ValueError("Diagnostic harness backend differs from immutable runtime source.")
    if git("status", "--porcelain", "--untracked-files=no"):
        raise ValueError("Commit the diagnostic harness before execution.")
    project = "trackvance-v070-test-native-probe-" + uuid.uuid4().hex[:12]
    before = owned_cleanup.snapshot()
    receipt = {"schema_version": 1, "kind": "OWNED_SYNTHETIC_QUERY_PROBE", "project": project,
               "runtime_source_sha": args.runtime_sha, "harness_source_sha": git("rev-parse", "HEAD"),
               "backend_tree": git("rev-parse", "HEAD:backend"), "image_id": image_id,
               "runtime_image_labels": inspected["labels"], "cpu_limit": 2, "memory_limit_bytes": 4 * 1024**3,
               "before": {key: sorted(values) for key, values in before.items()}, "status": "REGISTERED"}
    args.output.parent.mkdir(parents=True, exist_ok=True)

    def save():
        partial = args.output.with_suffix(".partial")
        with partial.open("w", encoding="utf-8") as stream:
            json.dump(receipt, stream, ensure_ascii=False, indent=2)
            stream.flush()
            os.fsync(stream.fileno())
        partial.replace(args.output)

    save()  # UUID and baseline are durable before any Docker creation.
    started = time.monotonic()
    try:
        identifier = owned_cleanup.docker("create", "--name", project,
            "--label", "com.docker.compose.project=" + project, "--network", "none",
            "--read-only", "--cpus", "2", "--memory", "4g", "--memory-swap", "4g", "--pids-limit", "128",
            "--mount", f"type=bind,src={ROOT / 'scripts' / 'tests'},dst=/probe/scripts/tests,readonly",
            "--tmpfs", "/tmp:rw,nosuid,nodev,size=256m,mode=1777",
            "--entrypoint", "python", image_id, "/probe/scripts/tests/delivery_native_query_probe.py", "--native").strip()
        receipt["container_id"] = identifier
        save()
        status, stdout, stderr = bounded_process(["docker", "start", "--attach", identifier], timeout=300)
        receipt["container_exit_code"] = status
        receipt["native"] = json.loads(stdout) if status == 0 else None
        receipt["diagnostic_stderr"] = stderr.decode("utf-8", errors="replace")[:8192]
        receipt["status"] = "DIAGNOSED" if status == 0 else "FAILED"
    finally:
        receipt["elapsed_seconds"] = round(time.monotonic() - started, 3)
        receipt["cleanup"] = owned_cleanup.cleanup(before, projects={project})
        receipt["after"] = {key: sorted(values) for key, values in owned_cleanup.snapshot().items()}
        save()
    print(json.dumps({"project": project, "status": receipt["status"], "cleanup": receipt["cleanup"]["status"],
                      "output": str(args.output), "elapsed_seconds": receipt["elapsed_seconds"]}))
    return 0 if receipt["status"] == "DIAGNOSED" else 1


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--native", action="store_true")
    parser.add_argument("--image-registry", type=Path)
    parser.add_argument("--docker-config", type=Path)
    parser.add_argument("--docker-host")
    parser.add_argument("--runtime-sha")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    if args.native:
        print(json.dumps(native(), ensure_ascii=False, separators=(",", ":")))
        return 0
    if not all((args.image_registry, args.docker_config, args.docker_host, args.runtime_sha, args.output)):
        parser.error("Explicit immutable registry, anonymous context, runtime SHA and own receipt required.")
    return host(args)


if __name__ == "__main__":
    raise SystemExit(main())
