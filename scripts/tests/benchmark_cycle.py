#!/usr/bin/env python3
"""Run measured workloads in one disposable Docker Compose project.

The default run is the 100 MiB certification baseline: file ingestion, all
three modules, PostgreSQL snapshot and SQL Server snapshot. ``--file-only`` and
smaller sizes are smoke checks and are explicitly reported as non-certifying.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import http.client
import importlib.util
import json
import math
import os
import platform
import re
import secrets
import shutil
import socket
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS))
import docker_state

_smoke_spec = importlib.util.spec_from_file_location("benchmark_smoke", SCRIPTS / "smoke_test.py")
assert _smoke_spec and _smoke_spec.loader
smoke = importlib.util.module_from_spec(_smoke_spec)
sys.modules[_smoke_spec.name] = smoke
_smoke_spec.loader.exec_module(smoke)

MIB = 1024 * 1024
GIB = 1024 * MIB
PROJECT_PATTERN = re.compile(r"trackvance-bench-[a-z0-9-]+")
PLANNER_SOFT_BYTES = 6 * GIB
DOCKER_RESERVE_BYTES = 2 * GIB
DISK_STOP_BYTES = 10 * GIB
FIXTURE_SEED = b"trackvance-benchmark-varied-v1"


def validated_project_name(value: str) -> str:
    if not PROJECT_PATTERN.fullmatch(value):
        raise ValueError("El benchmark requiere un proyecto aislado trackvance-bench-...")
    return value


def available_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


def redact(value: str, credentials: list[str]) -> str:
    for credential in credentials:
        value = value.replace(credential, "[REDACTED]")
    return value


def execute(
    arguments: list[str],
    environment: dict[str, str],
    credentials: list[str],
    *,
    input_text: str | None = None,
    timeout: int = 1800,
    label: str,
) -> str:
    print(f"+ {label}", flush=True)
    result = subprocess.run(
        arguments,
        cwd=ROOT,
        env=environment,
        input=input_text,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
        check=False,
    )
    if result.returncode:
        detail = redact(result.stdout + result.stderr, credentials)[-3000:]
        raise RuntimeError(f"{label} falló (exit {result.returncode}): {detail}")
    return result.stdout


def parse_size(value: str) -> int:
    match = re.fullmatch(r"\s*([0-9.]+)\s*([kKmMgGtT]i?[bB]|[bB])\s*", value)
    if not match:
        raise ValueError(f"Unidad Docker no reconocida: {value}")
    number = float(match.group(1))
    unit = match.group(2).lower()
    factors = {
        "b": 1,
        "kb": 1000,
        "kib": 1024,
        "mb": 1000**2,
        "mib": 1024**2,
        "gb": 1000**3,
        "gib": 1024**3,
        "tb": 1000**4,
        "tib": 1024**4,
    }
    return int(number * factors[unit])


def fixture_plan(target_bytes: int, rows: int) -> dict[str, int]:
    if target_bytes <= 0 or not 1 <= rows <= 100_000:
        raise ValueError("Tamaño y filas del fixture deben ser positivos y acotados.")
    payload_bytes = max(32, math.ceil(target_bytes / rows))
    if payload_bytes > 60 * 1024:
        raise ValueError("El tamaño solicitado supera el límite seguro por celda.")
    return {"target_bytes": target_bytes, "rows": rows, "payload_bytes": payload_bytes}


def payload_for_row(row: int, width: int, mode: str) -> str:
    if mode == "compressible":
        return "x" * width
    if mode != "varied":
        raise ValueError("Modo de fixture no reconocido.")
    required = math.ceil(width * 3 / 4) + 3
    raw = hashlib.shake_256(FIXTURE_SEED + row.to_bytes(8, "big")).digest(required)
    return base64.urlsafe_b64encode(raw).decode("ascii")[:width]


def generate_fixture(
    path: Path, target_bytes: int, rows: int, mode: str = "varied"
) -> dict[str, Any]:
    plan = fixture_plan(target_bytes, rows)
    hasher = hashlib.sha256()
    with path.open("xb") as output:
        header = b"record_key,amount,event_date,payload\n"
        output.write(header)
        hasher.update(header)
        for number in range(1, rows + 1):
            payload = payload_for_row(number, plan["payload_bytes"], mode)
            line = (
                f"B{number:08d},{number % 997 + 1},2026-01-{number % 28 + 1:02d},"
                f"{payload}\n"
            ).encode()
            output.write(line)
            hasher.update(line)
    size = path.stat().st_size
    if size < target_bytes:
        raise RuntimeError("El fixture generado no alcanzó el tamaño solicitado.")
    return {
        **plan,
        "actual_bytes": size,
        "sha256": hasher.hexdigest(),
        "fixture_mode": mode,
        "payload_method": "SHAKE256_URLSAFE_V1" if mode == "varied" else "REPEATED_X_V1",
        "seed_sha256": hashlib.sha256(FIXTURE_SEED).hexdigest(),
        "column_count": 4,
    }


def tier_decision(size_mib: int, docker_memory: int, disk_free: int) -> dict[str, Any]:
    source_bytes = size_mib * MIB * 2
    # Same formula used by ExecutionPlanner for a two-input Recon workload.
    estimated_working_set = source_bytes * 12 * 2
    allowed = (
        estimated_working_set <= PLANNER_SOFT_BYTES
        and estimated_working_set <= docker_memory - DOCKER_RESERVE_BYTES
        and disk_free >= 20 * GIB
    )
    return {
        "size_mib": size_mib,
        "status": "PLANNED" if allowed else "NOT_RUN_RESOURCE_LIMIT",
        "estimated_recon_working_set_bytes": estimated_working_set,
        "planner_soft_bytes": PLANNER_SOFT_BYTES,
        "docker_available_after_reserve_bytes": max(
            0, docker_memory - DOCKER_RESERVE_BYTES
        ),
        "disk_free_bytes": disk_free,
        "reason": None
        if allowed
        else "El Recon completo excede planner/Docker o la reserva de disco.",
    }


def tier_policy(docker_memory: int, disk_free: int) -> list[dict[str, Any]]:
    return [
        tier_decision(size_mib, docker_memory, disk_free)
        for size_mib in (100, 500, 1024, 2048, 5120)
    ]


def docker_capacity() -> dict[str, Any]:
    result = subprocess.run(
        ["docker", "info", "--format", "{{json .}}"],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    if result.returncode:
        raise RuntimeError("Docker no está disponible para el benchmark.")
    info = json.loads(result.stdout)
    disk = shutil.disk_usage(ROOT)
    return {
        "host": {
            "platform": platform.platform(),
            "logical_cpus": os.cpu_count(),
            "disk_free_bytes": disk.free,
            "disk_total_bytes": disk.total,
        },
        "docker": {
            "server_version": info.get("ServerVersion"),
            "operating_system": info.get("OperatingSystem"),
            "architecture": info.get("Architecture"),
            "cpus": info.get("NCPU"),
            "memory_bytes": int(info.get("MemTotal", 0)),
            "storage_driver": info.get("Driver"),
        },
    }


def final_container_metrics(project: str) -> dict[str, Any]:
    metrics: dict[str, Any] = {}
    for container in docker_state.inventory(project)["containers"]:
        service = container["service"]
        command = (
            "cat /sys/fs/cgroup/cpu.stat 2>/dev/null || true; "
            "cat /sys/fs/cgroup/memory.peak 2>/dev/null || true; "
            "grep -E '^(VmHWM|VmRSS):' /proc/1/status 2>/dev/null || true"
        )
        result = subprocess.run(
            ["docker", "exec", container["id"], "sh", "-c", command],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        values: dict[str, int | bool] = {}
        for line in result.stdout.splitlines():
            parts = line.replace(":", "").split()
            if len(parts) == 1 and parts[0].isdigit():
                values["cgroup_peak_memory_bytes"] = int(parts[0])
            elif len(parts) >= 2 and parts[0] == "usage_usec":
                values["cpu_usage_usec"] = int(parts[1])
            elif len(parts) >= 2 and parts[0] in {"VmHWM", "VmRSS"}:
                values[parts[0].lower() + "_bytes"] = int(parts[1]) * 1024
        inspected = docker_state.docker_json(["container", "inspect", container["id"]])[0]
        values["oom_killed"] = bool(inspected.get("State", {}).get("OOMKilled"))
        values["exit_code"] = int(inspected.get("State", {}).get("ExitCode", 0))
        metrics[service] = values
    return dict(sorted(metrics.items()))


@dataclass
class Watchdog:
    project: str
    memory_stop_bytes: int
    deadline: float
    evidence_root: Path
    stop_event: threading.Event = field(default_factory=threading.Event)
    breached: str | None = None
    peak_total_memory_bytes: int = 0
    peak_memory_by_service: dict[str, int] = field(default_factory=dict)
    peak_cpu_percent: float = 0
    peak_block_io_bytes: int = 0
    samples: int = 0
    _thread: threading.Thread | None = None

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, name="benchmark-watchdog", daemon=True)
        self._thread.start()

    def finish(self) -> None:
        self.stop_event.set()
        if self._thread:
            self._thread.join(timeout=20)

    def _stop_project(self, reason: str) -> None:
        self.breached = reason
        try:
            identifiers = [item["id"] for item in docker_state.inventory(self.project)["containers"]]
            if identifiers:
                subprocess.run(
                    ["docker", "stop", *identifiers],
                    capture_output=True,
                    timeout=120,
                    check=False,
                )
        finally:
            self.stop_event.set()

    def _run(self) -> None:
        while not self.stop_event.wait(2):
            if time.monotonic() >= self.deadline:
                self._stop_project("WALL_TIME_LIMIT")
                return
            if shutil.disk_usage(self.evidence_root).free < DISK_STOP_BYTES:
                self._stop_project("HOST_DISK_RESERVE")
                return
            try:
                state = docker_state.inventory(self.project)
                running = [item for item in state["containers"] if item["running"]]
                if not running:
                    continue
                result = subprocess.run(
                    [
                        "docker",
                        "stats",
                        "--no-stream",
                        "--format",
                        "{{json .}}",
                        *(item["id"] for item in running),
                    ],
                    capture_output=True,
                    text=True,
                    timeout=30,
                    check=False,
                )
                if result.returncode:
                    continue
                service_by_name = {item["name"]: item["service"] for item in running}
                total_memory = 0
                total_cpu = 0.0
                total_io = 0
                for line in result.stdout.splitlines():
                    sample = json.loads(line)
                    used = parse_size(sample["MemUsage"].split("/", 1)[0])
                    service = service_by_name.get(sample.get("Name", ""), sample.get("Name", ""))
                    total_memory += used
                    self.peak_memory_by_service[service] = max(
                        used, self.peak_memory_by_service.get(service, 0)
                    )
                    total_cpu += float(sample.get("CPUPerc", "0").rstrip("%") or 0)
                    for value in sample.get("BlockIO", "0B / 0B").split("/"):
                        total_io += parse_size(value.strip())
                self.samples += 1
                self.peak_total_memory_bytes = max(self.peak_total_memory_bytes, total_memory)
                self.peak_cpu_percent = max(self.peak_cpu_percent, total_cpu)
                self.peak_block_io_bytes = max(self.peak_block_io_bytes, total_io)
                if total_memory > self.memory_stop_bytes:
                    self._stop_project("DOCKER_MEMORY_RESERVE")
                    return
            except (OSError, ValueError, subprocess.SubprocessError):
                continue

    def report(self) -> dict[str, Any]:
        return {
            "samples": self.samples,
            "peak_total_memory_bytes": self.peak_total_memory_bytes,
            "peak_memory_by_service": dict(sorted(self.peak_memory_by_service.items())),
            "peak_cpu_percent": self.peak_cpu_percent,
            "peak_block_io_bytes": self.peak_block_io_bytes,
            "memory_stop_bytes": self.memory_stop_bytes,
            "disk_stop_bytes": DISK_STOP_BYTES,
            "breached": self.breached,
        }


@dataclass
class StorageSampler:
    container_id: str
    stop_event: threading.Event = field(default_factory=threading.Event)
    samples: int = 0
    baseline_storage_bytes: int | None = None
    peak_storage_bytes: int = 0
    peak_temp_bytes: int = 0
    _thread: threading.Thread | None = None

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, name="storage-sampler", daemon=True)
        self._thread.start()

    def finish(self) -> None:
        self.stop_event.set()
        if self._thread:
            self._thread.join(timeout=10)

    def _run(self) -> None:
        command = (
            "du -sb /var/lib/trackvance/tmp 2>/dev/null || echo '0 /tmp'; "
            "du -sb /var/lib/trackvance 2>/dev/null || echo '0 /storage'"
        )
        while not self.stop_event.is_set():
            try:
                result = subprocess.run(
                    ["docker", "exec", self.container_id, "sh", "-c", command],
                    capture_output=True,
                    text=True,
                    timeout=10,
                    check=False,
                )
                sizes = [
                    int(line.split()[0])
                    for line in result.stdout.splitlines()
                    if line.split() and line.split()[0].isdigit()
                ]
                if len(sizes) >= 2:
                    temporary, storage = sizes[0], sizes[1]
                    if self.baseline_storage_bytes is None:
                        self.baseline_storage_bytes = storage
                    self.peak_temp_bytes = max(self.peak_temp_bytes, temporary)
                    self.peak_storage_bytes = max(self.peak_storage_bytes, storage)
                    self.samples += 1
            except (OSError, ValueError, subprocess.SubprocessError):
                pass
            self.stop_event.wait(0.2)

    def report(self) -> dict[str, Any]:
        baseline = self.baseline_storage_bytes or 0
        return {
            "samples": self.samples,
            "sample_interval_seconds": 0.2,
            "baseline_storage_bytes": baseline,
            "peak_storage_bytes": self.peak_storage_bytes,
            "peak_storage_growth_bytes": max(0, self.peak_storage_bytes - baseline),
            "peak_temp_bytes": self.peak_temp_bytes,
        }


def stream_upload(api: Any, path: str, fixture: Path, timeout: int) -> dict[str, Any]:
    boundary = "trackvance-benchmark-" + uuid4().hex
    prefix = (
        f"--{boundary}\r\nContent-Disposition: form-data; name=\"file\"; "
        f"filename=\"{fixture.name}\"\r\nContent-Type: text/csv\r\n\r\n"
    ).encode()
    suffix = f"\r\n--{boundary}--\r\n".encode()
    parsed = urlsplit(api.base_url)
    connection = http.client.HTTPConnection(parsed.hostname, parsed.port, timeout=timeout)
    endpoint = (parsed.path.rstrip("/") + path) or "/"
    connection.putrequest("POST", endpoint)
    connection.putheader("Content-Type", f"multipart/form-data; boundary={boundary}")
    connection.putheader("Content-Length", str(len(prefix) + fixture.stat().st_size + len(suffix)))
    connection.putheader("Accept", "application/json")
    connection.putheader("X-CSRF-Token", api.csrf)
    cookies = "; ".join(f"{cookie.name}={cookie.value}" for cookie in api.cookies)
    connection.putheader("Cookie", cookies)
    connection.endheaders()
    connection.send(prefix)
    with fixture.open("rb") as source:
        while chunk := source.read(MIB):
            connection.send(chunk)
    connection.send(suffix)
    response = connection.getresponse()
    content = response.read()
    connection.close()
    if response.status != 201:
        raise RuntimeError(f"Upload benchmark HTTP {response.status}: {content[:500]!r}")
    return json.loads(content)


def timed(records: list[dict[str, Any]], name: str, operation):
    started = time.monotonic()
    value = operation()
    records.append({"operation": name, "wall_seconds": time.monotonic() - started})
    return value


def wait_run(api: Any, run_id: str, timeout: int = 1200) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        current = api.get(f"/api/v1/runs/{run_id}")
        if current["status"] in {"SUCCESS", "FAILED", "CANCELLED"}:
            if current["status"] != "SUCCESS":
                raise RuntimeError(f"Run {run_id} terminó en {current['status']}.")
            return current
        time.sleep(1)
    raise RuntimeError(f"Run {run_id} excedió {timeout} segundos.")


def run_intake(api: Any, dataset: dict, version: dict, name: str) -> dict[str, Any]:
    contract = api.post(
        "/api/v1/intake/contracts",
        {
            "name": name,
            "dataset_id": dataset["id"],
            "config": {
                "required_columns": ["record_key", "payload"],
                "unique_columns": ["record_key"],
                "numeric_columns": ["amount"],
                "positive_columns": ["amount"],
                "max_error_rate": 0,
            },
        },
    )
    queued = api.post(
        "/api/v1/intake/runs",
        {"contract_id": contract["id"], "dataset_version_id": version["id"]},
        expected=(202,),
    )
    return wait_run(api, queued["id"])


def run_file_workloads(
    api: Any, fixture: Path, rows: int, timings: list[dict[str, Any]], timeout: int
) -> dict[str, Any]:
    datasets = []
    versions = []
    for side in ("source", "target"):
        dataset = api.post(
            "/api/v1/datasets",
            {"name": f"Benchmark file {side} {uuid4().hex[:8]}", "domain": "Benchmark"},
        )
        version = timed(
            timings,
            f"file_upload_{side}",
            lambda dataset_id=dataset["id"]: stream_upload(
                api,
                f"/api/v1/datasets/{dataset_id}/versions/upload",
                fixture,
                timeout,
            ),
        )
        if version["row_count"] != rows:
            raise RuntimeError("La carga de archivo cambió la población esperada.")
        datasets.append(dataset)
        versions.append(version)

    intake = timed(
        timings,
        "intake_file",
        lambda: run_intake(api, datasets[0], versions[0], "Benchmark Intake file"),
    )
    control = api.post(
        "/api/v1/recon/controls",
        {
            "name": "Benchmark Recon file",
            "dataset_id": datasets[0]["id"],
            "target_dataset_id": datasets[1]["id"],
            "config": {
                "key_columns": ["record_key"],
                "comparison_rules": [
                    {
                        "type": "numeric_tolerance",
                        "source_column": "amount",
                        "target_column": "amount",
                        "parameters": {"abs": "0"},
                    }
                ],
            },
        },
    )
    recon = timed(
        timings,
        "recon_file",
        lambda: wait_run(
            api,
            api.post(
                "/api/v1/recon/runs",
                {
                    "control_id": control["id"],
                    "source_version_id": versions[0]["id"],
                    "target_version_id": versions[1]["id"],
                },
                expected=(202,),
            )["id"],
        ),
    )
    monitor = api.post(
        "/api/v1/monitors",
        {
            "name": "Benchmark Sentinel file",
            "dataset_id": datasets[0]["id"],
            "config": {
                "required_columns": ["record_key", "payload"],
                "null_columns": ["payload"],
                "max_null_rate": 0.01,
                "max_volume_change_pct": 15,
                "max_age_hours": 87600,
            },
        },
    )
    sentinel = timed(
        timings,
        "sentinel_file",
        lambda: wait_run(
            api,
            api.post(
                f"/api/v1/monitors/{monitor['id']}/runs",
                {"dataset_version_id": versions[0]["id"]},
                expected=(202,),
            )["id"],
        ),
    )
    if intake["metrics"]["total_rows"] != rows:
        raise RuntimeError("Intake no procesó la población completa.")
    if recon["metrics"]["total_rows"] != rows:
        raise RuntimeError("Recon no procesó la población completa.")
    if sentinel["metrics"]["row_count"] != rows:
        raise RuntimeError("Sentinel no procesó la población completa.")
    return {
        "datasets": [item["id"] for item in datasets],
        "versions": [item["id"] for item in versions],
        "version_metrics": [
            {
                "version_id": version["id"],
                "row_count": version["row_count"],
                "column_count": version["column_count"],
                "input_size_bytes": version["size_bytes"],
                "artifacts": [
                    {
                        "kind": artifact["kind"],
                        "size_bytes": artifact["size_bytes"],
                        "sha256": artifact["sha256"],
                    }
                    for artifact in version["artifacts"]
                ],
            }
            for version in versions
        ],
        "runs": {
            "intake": intake["id"],
            "recon": recon["id"],
            "sentinel": sentinel["id"],
        },
        "execution_plans": {
            name: api.get(f"/api/v1/runs/{run['id']}/execution-plan")
            for name, run in (("intake", intake), ("recon", recon), ("sentinel", sentinel))
        },
    }


def seed_sources(
    compose: list[str],
    environment: dict[str, str],
    credentials: list[str],
    rows: int,
    payload_bytes: int,
    fixture_mode: str,
    pg_reader_password: str,
    mssql_reader_password: str,
) -> dict[str, int]:
    block_count = math.ceil(payload_bytes / 32)
    if fixture_mode == "varied":
        postgres_payload = "LEFT(" + " || ".join(
            f"md5('trackvance-benchmark-varied-v1:' || value::text || ':{block}')"
            for block in range(block_count)
        ) + f", {payload_bytes})"
        mssql_payload = "LEFT(CONCAT(" + ",".join(
            "CONVERT(varchar(64), HASHBYTES('SHA2_256', "
            f"CONCAT('trackvance-benchmark-varied-v1:', value, ':{block}')), 2)"
            for block in range(block_count)
        ) + f"), {payload_bytes})"
    else:
        postgres_payload = f"repeat('x', {payload_bytes})"
        mssql_payload = f"REPLICATE('x', {payload_bytes})"
    postgres_sql = f"""
CREATE SCHEMA source_data;
CREATE TABLE source_data.benchmark_records (
  record_key varchar(20) PRIMARY KEY, amount numeric(18,2), event_date date, payload text
);
INSERT INTO source_data.benchmark_records
SELECT 'B' || lpad(value::text, 8, '0'), (value % 997 + 1)::numeric(18,2),
       DATE '2026-01-01' + ((value % 28)::int), {postgres_payload}
FROM generate_series(1, {rows}) AS value;
CREATE ROLE tv_bench_reader LOGIN PASSWORD '{pg_reader_password}';
GRANT CONNECT ON DATABASE trackvance_source TO tv_bench_reader;
GRANT USAGE ON SCHEMA source_data TO tv_bench_reader;
GRANT SELECT ON ALL TABLES IN SCHEMA source_data TO tv_bench_reader;
"""
    execute(
        [*compose, "exec", "-T", "source-postgres", "psql", "-v", "ON_ERROR_STOP=1",
         "-U", "source_admin", "-d", "trackvance_source"],
        environment,
        credentials,
        input_text=postgres_sql,
        label="seed PostgreSQL 100 MiB source",
    )
    postgres_bytes = int(
        execute(
            [*compose, "exec", "-T", "source-postgres", "psql", "-At", "-U",
             "source_admin", "-d", "trackvance_source", "-c",
             (
                 "SELECT SUM(octet_length(record_key)+octet_length(amount::text)+"
                 "octet_length(event_date::text)+octet_length(payload)) "
                 "FROM source_data.benchmark_records;"
             )],
            environment,
            credentials,
            label="measure PostgreSQL logical bytes",
        ).strip()
    )

    mssql_sql = f"""
IF DB_ID('trackvance_source') IS NULL CREATE DATABASE trackvance_source;
GO
USE trackvance_source;
GO
CREATE SCHEMA source_data;
GO
CREATE TABLE source_data.benchmark_records (
  record_key varchar(20) PRIMARY KEY, amount decimal(18,2), event_date date,
  payload varchar(max)
);
WITH numbers AS (
  SELECT TOP ({rows}) ROW_NUMBER() OVER (ORDER BY (SELECT NULL)) AS value
  FROM sys.all_objects a CROSS JOIN sys.all_objects b
)
INSERT INTO source_data.benchmark_records
SELECT 'B' + RIGHT(REPLICATE('0', 8) + CONVERT(varchar(8), value), 8),
       value % 997 + 1, DATEADD(day, value % 28, CONVERT(date, '2026-01-01')),
       {mssql_payload}
FROM numbers;
GO
USE master;
CREATE LOGIN tv_bench_reader WITH PASSWORD = '{mssql_reader_password}', CHECK_POLICY = OFF;
GO
USE trackvance_source;
CREATE USER tv_bench_reader FOR LOGIN tv_bench_reader;
GRANT SELECT ON SCHEMA::source_data TO tv_bench_reader;
GO
"""
    execute(
        [*compose, "exec", "-T", "source-sqlserver", "sh", "-c",
         (
             'SQLCMDPASSWORD="$MSSQL_SA_PASSWORD" /opt/mssql-tools18/bin/sqlcmd '
             "-S localhost -U sa -C -b"
         )],
        environment,
        credentials,
        input_text=mssql_sql,
        label="seed SQL Server 100 MiB source",
    )
    mssql_output = execute(
        [*compose, "exec", "-T", "source-sqlserver", "sh", "-c",
         (
             'SQLCMDPASSWORD="$MSSQL_SA_PASSWORD" /opt/mssql-tools18/bin/sqlcmd '
             '-S localhost -U sa -C -b -h -1 -W -Q "SET NOCOUNT ON; '
             "USE trackvance_source; SELECT SUM(DATALENGTH(record_key)+DATALENGTH(amount)+"
             "DATALENGTH(event_date)+DATALENGTH(payload)) "
             'FROM source_data.benchmark_records;"'
         )],
        environment,
        credentials,
        label="measure SQL Server logical bytes",
    )
    numbers = re.findall(r"\b\d{6,}\b", mssql_output)
    if not numbers:
        raise RuntimeError("SQL Server no informó el tamaño lógico sembrado.")
    return {"POSTGRESQL": postgres_bytes, "SQLSERVER": int(numbers[-1])}


def register_source(
    api: Any,
    source_type: str,
    password: str,
    rows: int,
    timings: list[dict[str, Any]],
) -> dict[str, Any]:
    service = "source-postgres" if source_type == "POSTGRESQL" else "source-sqlserver"
    config = {
        "name": f"Benchmark {source_type}",
        "source_type": source_type,
        "host": service,
        "port": 5432 if source_type == "POSTGRESQL" else 1433,
        "database": "trackvance_source",
        "username": "tv_bench_reader",
        "password": password,
        "options": {
            "connect_timeout": 10,
            "query_timeout": 60,
            **({"sslmode": "disable"} if source_type == "POSTGRESQL" else {"encryption": "off"}),
        },
    }
    connection = api.post("/api/v1/connections", config)
    registered = timed(
        timings,
        f"snapshot_{source_type.lower()}",
        lambda: api.post(
            f"/api/v1/connections/{connection['id']}/datasets",
            {
                "name": f"Benchmark snapshot {source_type}",
                "domain": "Benchmark",
                "schema_name": "source_data",
                "object_name": "benchmark_records",
            },
        ),
    )
    if registered["version"]["row_count"] != rows:
        raise RuntimeError(f"El snapshot {source_type} no conservó todas las filas.")
    intake = timed(
        timings,
        f"intake_{source_type.lower()}",
        lambda: run_intake(
            api,
            registered["dataset"],
            registered["version"],
            f"Benchmark Intake {source_type}",
        ),
    )
    if intake["metrics"]["total_rows"] != rows:
        raise RuntimeError(f"Intake {source_type} no procesó la población completa.")
    return {
        "connection_id": connection["id"],
        "dataset_id": registered["dataset"]["id"],
        "version_id": registered["version"]["id"],
        "run_id": intake["id"],
        "canonical_size_bytes": registered["version"]["size_bytes"],
        "artifacts": [
            {
                "kind": artifact["kind"],
                "size_bytes": artifact["size_bytes"],
                "sha256": artifact["sha256"],
            }
            for artifact in registered["version"]["artifacts"]
        ],
        "source_identity": registered["version"]["ingestion_metadata"]["source"],
    }


def cleanup(project: str, evidence: Path) -> None:
    state = docker_state.inventory(project)
    if not state["containers"] and not state["volumes"] and not state["networks"]:
        return
    plan_path = evidence / "reset-plan.json"
    plan = docker_state.create_reset_plan(project, plan_path, ttl_minutes=60)
    confirmation = f"RESET:{project}:{plan['plan_sha256'][:12]}"
    docker_state.reset(plan_path, confirmation)


def parse_arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project", default=f"trackvance-bench-{os.getpid()}-{uuid4().hex[:6]}")
    parser.add_argument("--port", type=int, default=0)
    parser.add_argument("--target-mib", type=int, default=100)
    parser.add_argument("--rows", type=int, default=50_000)
    parser.add_argument(
        "--fixture-mode", choices=("varied", "compressible"), default="varied"
    )
    parser.add_argument("--file-only", action="store_true")
    parser.add_argument("--skip-build", action="store_true")
    parser.add_argument("--keep", action="store_true")
    parser.add_argument("--max-wall-seconds", type=int, default=2700)
    parser.add_argument("--evidence-dir", type=Path)
    return parser.parse_args()


def main() -> int:
    options = parse_arguments()
    try:
        project = validated_project_name(options.project)
        plan = fixture_plan(options.target_mib * MIB, options.rows)
    except ValueError as error:
        print(f"ERROR: {error}", file=sys.stderr)
        return 2
    port = options.port or available_port()
    if not 1 <= port <= 65535 or options.max_wall_seconds < 60:
        print("ERROR: puerto o watchdog inválido.", file=sys.stderr)
        return 2
    evidence = (
        options.evidence_dir
        or ROOT / ".codex-local" / "benchmarks" / project
    ).resolve()
    try:
        evidence.mkdir(parents=True, exist_ok=False)
    except OSError as error:
        print(f"ERROR: no fue posible crear evidencia nueva: {error}", file=sys.stderr)
        return 2
    result_path = evidence / "result.json"
    result: dict[str, Any] = {
        "schema_version": 1,
        "status": "FAIL",
        "certification": False,
        "project": project,
        "requested": {
            "target_mib": options.target_mib,
            "rows": options.rows,
            "fixture_mode": options.fixture_mode,
            "file_only": options.file_only,
            "max_wall_seconds": options.max_wall_seconds,
        },
    }
    watchdog: Watchdog | None = None
    storage_sampler: StorageSampler | None = None
    owned_project = False
    fixture = evidence / "benchmark.csv"
    credentials = [secrets.token_urlsafe(32) for _ in range(4)]
    environment = os.environ.copy()
    environment.update(
        {
            "POSTGRES_PASSWORD": credentials[0],
            "SOURCE_POSTGRES_PASSWORD": credentials[1],
            "SOURCE_MSSQL_SA_PASSWORD": credentials[2],
            "DEMO_ACCESS_ENABLED": "true",
            "DEMO_SEED_ENABLED": "true",
            "WEB_PORT": str(port),
            "TRACKVANCE_WEB_ORIGIN": f"http://localhost:{port}",
        }
    )
    if options.file_only:
        environment.update(
            {
                "BENCHMARK_POSTGRES_MEMORY_LIMIT": "768m",
                "BENCHMARK_API_MEMORY_LIMIT": "1g",
                "BENCHMARK_WORKER_MEMORY_LIMIT": "3g",
                "BENCHMARK_WEB_MEMORY_LIMIT": "128m",
            }
        )
    started_at = time.monotonic()
    try:
        capacity = docker_capacity()
        result["machine"] = capacity
        policies = tier_policy(
            capacity["docker"]["memory_bytes"], capacity["host"]["disk_free_bytes"]
        )
        result["tiers"] = policies
        full_certification = options.target_mib == 100 and not options.file_only
        requested_policy = tier_decision(
            options.target_mib,
            capacity["docker"]["memory_bytes"],
            capacity["host"]["disk_free_bytes"],
        )
        if options.target_mib >= 100 and requested_policy["status"] != "PLANNED":
            result.update(
                {
                    "status": "NOT_RUN_RESOURCE_LIMIT",
                    "reason": requested_policy["reason"],
                    "requested_tier": requested_policy,
                    "wall_seconds": time.monotonic() - started_at,
                }
            )
            result_path.write_text(json.dumps(result, indent=2, sort_keys=True), encoding="utf-8")
            return 0
        existing = docker_state.inventory(project)
        if existing["containers"] or existing["volumes"] or existing["networks"]:
            raise RuntimeError("El proyecto benchmark ya tiene recursos Docker.")
        # From this point onward only this process may create resources under the
        # high-entropy project name. Never clean a project that failed this check.
        owned_project = True
        fixture_metadata = generate_fixture(
            fixture, plan["target_bytes"], options.rows, options.fixture_mode
        )
        result["fixture"] = fixture_metadata
        compose = [
            "docker",
            "compose",
            "-p",
            project,
            "-f",
            str(ROOT / "compose.yml"),
            "-f",
            str(ROOT / "deploy" / "docker" / "compose.connections-test.yml"),
            "-f",
            str(ROOT / "deploy" / "docker" / "compose.benchmark.yml"),
        ]
        up = [*compose, "up", "-d", "--wait"]
        if not options.skip_build:
            up.append("--build")
        if options.file_only:
            available = execute(
                [*compose, "config", "--services"],
                environment,
                credentials,
                label="resolve benchmark services",
            ).splitlines()
            up.extend(
                service
                for service in available
                if service in {"postgres", "api", "worker", "scheduler", "web"}
            )
        execute(up, environment, credentials, timeout=1800, label="start isolated benchmark")
        memory_stop = max(
            2 * GIB, capacity["docker"]["memory_bytes"] - DOCKER_RESERVE_BYTES
        )
        watchdog = Watchdog(
            project,
            memory_stop,
            time.monotonic() + options.max_wall_seconds,
            evidence,
        )
        watchdog.start()
        api_container = next(
            item
            for item in docker_state.inventory(project)["containers"]
            if item["service"] == "api"
        )
        storage_sampler = StorageSampler(api_container["id"])
        storage_sampler.start()
        api = smoke.Api(f"http://127.0.0.1:{port}", timeout=1200)
        authentication = api.post("/api/v1/auth/demo", {}, expected=(200,))
        api.csrf = authentication["csrf_token"]
        timings: list[dict[str, Any]] = []
        result["file_workloads"] = run_file_workloads(
            api, fixture, options.rows, timings, timeout=1200
        )
        if not options.file_only:
            logical_bytes = seed_sources(
                compose,
                environment,
                credentials,
                options.rows,
                plan["payload_bytes"],
                options.fixture_mode,
                credentials[3],
                credentials[3],
            )
            if any(value < options.target_mib * MIB for value in logical_bytes.values()):
                raise RuntimeError("Una fuente externa no alcanzó el tamaño lógico solicitado.")
            external = {}
            external["POSTGRESQL"] = register_source(
                api, "POSTGRESQL", credentials[3], options.rows, timings
            )
            external["SQLSERVER"] = register_source(
                api, "SQLSERVER", credentials[3], options.rows, timings
            )
            result["external_sources"] = {
                "logical_bytes": logical_bytes,
                "snapshots": external,
            }
        if watchdog.breached:
            raise RuntimeError(f"Watchdog detuvo el benchmark: {watchdog.breached}")
        result["container_metrics"] = final_container_metrics(project)
        result["timings"] = timings
        result["throughput"] = [
            {
                "operation": item["operation"],
                "input_bytes_per_second": fixture_metadata["actual_bytes"]
                / item["wall_seconds"],
                "rows_per_second": options.rows / item["wall_seconds"],
            }
            for item in timings
            if item["wall_seconds"] > 0
        ]
        result["status"] = "PASS"
        result["certification"] = full_certification
        result["wall_seconds"] = time.monotonic() - started_at
        if options.target_mib == 100:
            result["tiers"][0]["status"] = "PASS"
            result["tiers"][0]["actual_input_bytes"] = fixture_metadata["actual_bytes"]
        return 0
    except (OSError, RuntimeError, ValueError, subprocess.SubprocessError) as error:
        result["status"] = "FAIL"
        result["error_type"] = type(error).__name__
        result["error"] = redact(str(error), credentials)[-1000:]
        result["wall_seconds"] = time.monotonic() - started_at
        print(f"ERROR: {result['error']}", file=sys.stderr)
        return 1
    finally:
        if watchdog:
            watchdog.finish()
            result["resources"] = watchdog.report()
        if storage_sampler:
            storage_sampler.finish()
            result["storage"] = storage_sampler.report()
        result_path.write_text(json.dumps(result, indent=2, sort_keys=True), encoding="utf-8")
        fixture.unlink(missing_ok=True)
        if owned_project and not options.keep:
            try:
                cleanup(project, evidence)
            except (OSError, RuntimeError, ValueError) as cleanup_error:
                print(f"ERROR de limpieza benchmark: {cleanup_error}", file=sys.stderr)


if __name__ == "__main__":
    raise SystemExit(main())
