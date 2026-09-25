#!/usr/bin/env python3
"""Measure four Delivery strategies against isolated real PostgreSQL and SQL Server.

Defaults are a representative 8 MiB / 20,000-row varied fixture, within unchanged
product limits. --smoke selects 1 MiB / 1,000 rows, never volume certification.
Only resources created under this runner's fresh high-entropy project are removed.
No passwords, fixture payloads or connection strings are written into evidence.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import math
import os
import re
import secrets
import subprocess
import sys
import time
from pathlib import Path
from typing import Any
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[2]


def load_runner(name: str):
    spec = importlib.util.spec_from_file_location(name, Path(__file__).with_name(name + ".py"))
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


bench = load_runner("benchmark_cycle")
delivery = load_runner("delivery_cycle")
SERVICES = ("postgres", "api", "worker", "delivery-worker", "web",
            "destination-postgres", "destination-sqlserver")
STRATEGIES = ("CREATE_AND_LOAD", "APPEND", "OVERWRITE", "UPSERT")
ENGINES = ("POSTGRESQL", "SQLSERVER")
METRIC_FIELDS = ("rows_attempted", "rows_written", "rows_inserted", "rows_updated", "bytes_sent")


class BenchmarkNotRun(Exception):
    """A policy skip still passes through cleanup and its exit-code checks."""


def validated_project_name(value: str) -> str:
    if not re.fullmatch(r"trackvance-delivery-bench-[a-z0-9-]+", value):
        raise ValueError("Use un proyecto aislado trackvance-delivery-bench-...")
    return value


def resource_policy(target_bytes: int, rows: int, memory: int, disk: int) -> dict[str, Any]:
    # Defaults are never increased to make the measurement pass. The CSV has
    # framing overhead in addition to payload bytes, so leave 1 MiB headroom.
    reasons = []
    if target_bytes > 9 * bench.MIB or rows > 100_000:
        reasons.append("UNCHANGED_PRODUCT_UPLOAD_OR_ROW_LIMIT")
    if memory < 6 * bench.GIB:
        reasons.append("DOCKER_MEMORY_RESERVE")
    if disk < 12 * bench.GIB:
        reasons.append("HOST_DISK_RESERVE")
    return {"status": "NOT_RUN_RESOURCE_LIMIT" if reasons else "PLANNED",
            "reasons": reasons, "max_upload_bytes": 10 * bench.MIB,
            "max_rows": 100_000, "minimum_docker_memory_bytes": 6 * bench.GIB,
            "minimum_host_disk_bytes": 12 * bench.GIB}


def mappings(payload_width: int) -> list[dict[str, Any]]:
    definitions = (("record_key", "STRING", 40), ("amount", "DECIMAL", None),
                   ("event_date", "DATE", None), ("payload", "STRING", payload_width))
    return [{"source_name": name, "target_name": name, "target_type": logical,
             "ordinal": index, "nullable": False,
             **({"length": width} if width else {}),
             **({"precision": 18, "scale": 0} if logical == "DECIMAL" else {})}
            for index, (name, logical, width) in enumerate(definitions)]


def validate_source(version: dict, fixture: dict) -> dict[str, str]:
    schema = {column["name"]: column["logical_type"] for column in version["schema"]}
    expected = {item["source_name"]: item["target_type"] for item in mappings(fixture["payload_bytes"])}
    if schema != expected or version["row_count"] != fixture["rows"]:
        raise RuntimeError("El fixture ingerido no conserva filas y tipos lógicos esperados.")
    if version["sha256"] != fixture["sha256"]:
        raise RuntimeError("El hash de la fuente ingerida no coincide con el fixture.")
    return schema


def validate_evidence(completed: dict, attempt: dict, receipt: dict, manifest: dict,
                      version: dict, destination: dict, strategy: str, rows: int) -> None:
    metrics = completed.get("metrics", {})
    if (receipt.get("kind") != "DELIVERY_RECEIPT" or receipt.get("result") != "COMMITTED"
            or receipt.get("run_id") != completed["id"]
            or receipt.get("delivery_attempt_id") != attempt["id"]
            or receipt.get("dataset_version_id") != version["id"]
            or receipt.get("source_sha256") != version["sha256"]
            or receipt.get("destination_version_id") != destination["destination_version_id"]
            or receipt.get("write_strategy") != strategy
            or manifest.get("schema_version") != 2 or manifest.get("module") != "DELIVERY"
            or manifest.get("run_id") != completed["id"]
            or manifest.get("delivery", {}).get("attempt", {}).get("id") != attempt["id"]):
        raise RuntimeError("La identidad de receipt/manifest no coincide con la entrega medida.")
    for key in METRIC_FIELDS:
        if key not in attempt or any(key not in item or item[key] != attempt[key]
                                    for item in (metrics, receipt, manifest.get("metrics", {}))):
            raise RuntimeError("Las métricas difieren entre intento, Run, receipt y manifest.")
    if attempt["rows_attempted"] != rows or attempt["rows_written"] != rows:
        raise RuntimeError("Las métricas no coinciden con la población fuente preparada.")
    # This benchmark's baseline PostgreSQL destination is version 16. Mixed
    # update-capable UPSERT deliberately reports null action counts on PG16/17.
    expected_actions = ((None, None) if destination["sink_type"] == "POSTGRESQL"
                        and strategy == "UPSERT" else
                        (rows - rows // 2, rows // 2) if strategy == "UPSERT" else (rows, 0))
    if (attempt["rows_inserted"], attempt["rows_updated"]) != expected_actions:
        raise RuntimeError("Los conteos de acciones no respetan la política del motor/estrategia.")
    semantics = metrics.get("metric_semantics")
    if (not isinstance(semantics, dict) or semantics.get("version") != 1
            or any(item.get("metric_semantics") != semantics
                   for item in (receipt, manifest.get("metrics", {})))):
        raise RuntimeError("Falta semántica aditiva consistente en la evidencia.")
    for key in ("preflight_seconds", "write_seconds"):
        value = metrics.get(key)
        if (not isinstance(value, (int, float)) or isinstance(value, bool)
                or not math.isfinite(value) or value <= 0
                or receipt.get(key) != value or manifest["metrics"].get(key) != value):
            raise RuntimeError("Falta duración real consistente de preflight/escritura.")


def verify_destination(run, engine: str, strategy: str, fixture: dict) -> dict:
    locator = ('"existing_delivery"."benchmark_records"' if engine == "POSTGRESQL"
               else "[existing_delivery].[benchmark_records]")
    length = "LENGTH" if engine == "POSTGRESQL" else "LEN"
    query = (f"SELECT CONCAT(COUNT(*), '|', COUNT(DISTINCT record_key), '|', SUM(amount), "
             f"'|', MIN({length}(payload)), '|', MAX({length}(payload))) FROM {locator};")
    observed = [int(item) for item in delivery.target_scalar(run, engine, query).split("|")]
    rows = fixture["rows"]
    multiplier = 2 if strategy == "APPEND" else 1
    expected = [rows * multiplier, rows,
                sum(number % 997 + 1 for number in range(1, rows + 1)) * multiplier,
                fixture["payload_bytes"], fixture["payload_bytes"]]
    if observed != expected:
        raise RuntimeError("La comprobación independiente de filas/claves/importes/longitudes no coincide.")
    return dict(zip(("rows", "distinct_keys", "amount_sum", "min_payload_characters",
                     "max_payload_characters"), observed, strict=True))


def remaining_cases(cases: list[dict], active: dict | None, resource_stop: bool) -> list[dict]:
    reached = {(item["engine"], item["strategy"]) for item in cases}
    if active:
        reached.add((active["engine"], active["strategy"]))
    return [{"engine": engine, "strategy": strategy,
             "status": "NOT_RUN_RESOURCE_LIMIT" if resource_stop else "NOT_RUN_AFTER_FAILURE"}
            for engine in ENGINES for strategy in STRATEGIES if (engine, strategy) not in reached]


def resource_snapshot(project: str) -> dict[str, Any]:
    """Cgroup counters are lifetime values, not attributed solely to one run."""
    metrics = bench.final_container_metrics(project)
    for container in bench.docker_state.inventory(project)["containers"]:
        response = subprocess.run(
            ["docker", "exec", container["id"], "sh", "-c",
             "test ! -f /sys/fs/cgroup/io.stat || cat /sys/fs/cgroup/io.stat"],
            capture_output=True, text=True, timeout=15, check=False,
        )
        totals: dict[str, int] = {}
        if response.returncode == 0:
            for line in response.stdout.splitlines():
                for token in line.split()[1:]:
                    key, _, value = token.partition("=")
                    if value.isdigit():
                        totals[key] = totals.get(key, 0) + int(value)
        metrics.setdefault(container["service"], {})["cgroup_io"] = totals or None
    return metrics


def resource_delta(before: dict, after: dict) -> dict:
    result = {}
    for service, values in after.items():
        old = before.get(service, {})
        cpu = values.get("cpu_usage_usec")
        previous = old.get("cpu_usage_usec")
        io = values.get("cgroup_io")
        old_io = old.get("cgroup_io")
        result[service] = {
            "cpu_seconds": (cpu - previous) / 1_000_000
            if isinstance(cpu, int) and isinstance(previous, int) else None,
            "memory_peak_since_container_start_bytes": values.get("cgroup_peak_memory_bytes"),
            "io_delta": {key: value - old_io.get(key, 0) for key, value in io.items()}
            if io is not None and old_io is not None else None,
            "oom_killed": values.get("oom_killed"),
        }
    return result


def prepare_strategy(run, engine: str, strategy: str, rows: int) -> None:
    locator = ('"existing_delivery"."benchmark_records"' if engine == "POSTGRESQL"
               else "[existing_delivery].[benchmark_records]")
    if strategy == "UPSERT":
        # OVERWRITE has restored one row per key. Prepare a deterministic mixed
        # UPSERT outside the timed operation: half updated, half inserted.
        middle = f"B{rows // 2:08d}"
        delivery.target_sql(run, engine,
            f"ALTER TABLE {locator} ADD CONSTRAINT benchmark_pk PRIMARY KEY (record_key); "
            f"DELETE FROM {locator} WHERE record_key > '{middle}'; "
            f"UPDATE {locator} SET amount=0;")


def measure_case(api, run, project: str, destination: dict, version: dict,
                 fixture: dict, strategy: str, credentials: list[str], timeout: int) -> dict:
    engine = destination["sink_type"]
    rows = fixture["rows"]
    prepare_strategy(run, engine, strategy, rows)
    draft = {
        "schema_version": 1, "dataset_version_id": version["id"],
        "destination_id": destination["id"],
        "destination_version_id": destination["destination_version_id"],
        "target": {"mode": "CREATE_TABLE" if strategy == "CREATE_AND_LOAD" else "EXISTING_TABLE",
                   "schema_name": "existing_delivery", "table_name": "benchmark_records",
                   "create_schema": False},
        "columns": mappings(fixture["payload_bytes"]), "write_strategy": strategy,
        "upsert_keys": ["record_key"] if strategy == "UPSERT" else [],
    }
    cpu_before = resource_snapshot(project)
    api_id = next(item["id"] for item in bench.docker_state.inventory(project)["containers"]
                  if item["service"] == "api")
    sampler = bench.StorageSampler(api_id)
    sampler.start()
    started = time.monotonic()
    try:
        preflight_start = time.monotonic()
        preflight = api.post("/api/v1/delivery/preflight", draft, expected=(200,))
        preflight_seconds = time.monotonic() - preflight_start
        if preflight["status"] != "PASS":
            raise RuntimeError(f"{engine}/{strategy}: preflight falló.")
        configuration = api.post("/api/v1/delivery/configurations", {
            **draft, "name": f"Benchmark {engine} {strategy}", "owner": "Benchmark 0.5.1",
            "description": "Medición desechable, no modifica límites del producto",
        })
        queued_start = time.monotonic()
        queued = delivery.post_idempotent(api, "/api/v1/delivery/runs", {
            "configuration_id": configuration["id"], "dataset_version_id": version["id"],
        }, "delivery-benchmark-" + uuid4().hex)
        completed = delivery.wait_for_run(api, queued["id"], timeout=timeout)
        run_seconds = time.monotonic() - queued_start
        if completed["status"] != "SUCCESS" or completed["decision"] != "COMMITTED":
            raise RuntimeError(f"{engine}/{strategy}: {completed['status']} no certifica commit.")
        # Keep enqueue-to-terminal timing separate. Total includes the explicit
        # wait for asynchronous local evidence, without a repair/replay request.
        completed = delivery.wait_for_evidence(api, queued["id"], timeout=timeout)
        attempts = api.get(f"/api/v1/delivery/runs/{queued['id']}/attempts")["items"]
        receipt = api.get(f"/api/v1/delivery/runs/{queued['id']}/receipt")
        manifest = api.get(f"/api/v1/runs/{queued['id']}/evidence")
        total_seconds = time.monotonic() - started
        if len(attempts) != 1 or attempts[0]["status"] != "COMMITTED":
            raise RuntimeError("El benchmark requiere un único intento confirmado.")
        attempt = attempts[0]
        validate_evidence(completed, attempt, receipt, manifest, version, destination, strategy, rows)
        destination_check = verify_destination(run, engine, strategy, fixture)
        metrics = completed.get("metrics", {})
        write_seconds = metrics["write_seconds"]
        result = {
            "engine": engine, "strategy": strategy, "status": "PASS", "stop_reason": None,
            "source": {key: fixture[key] for key in ("actual_bytes", "rows", "column_count", "sha256")},
            "run_id": queued["id"], "attempt_id": attempt["id"],
            "remote_commit": "COMMITTED", "destination_check": destination_check,
            "timings": {"total_seconds": total_seconds, "explicit_preflight_seconds": preflight_seconds,
                        "worker_preflight_seconds": metrics.get("preflight_seconds"),
                        "enqueue_to_terminal_seconds": run_seconds, "write_seconds": write_seconds},
            "throughput": {"rows_per_second_write": rows / write_seconds,
                           "source_mb_per_second_write": fixture["actual_bytes"] / 1_000_000 / write_seconds,
                           "rows_per_second_total": rows / total_seconds},
            "metrics": {key: attempt[key] for key in METRIC_FIELDS},
            "evidence": {"receipt_kind": receipt.get("kind"), "manifest_schema_version": manifest.get("schema_version")},
        }
        delivery.assert_no_credentials([result, completed, attempts, receipt, manifest], credentials,
                                       "Credencial en resultado/evidencia benchmark.")
        return result
    finally:
        sampler.finish()
        if "result" in locals():
            result["storage"] = sampler.report()
            result["resources"] = resource_delta(cpu_before, resource_snapshot(project))


def main() -> int:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project", default=f"trackvance-delivery-bench-{os.getpid()}-{uuid4().hex[:8]}")
    parser.add_argument("--smoke", action="store_true")
    parser.add_argument("--target-mib", type=int)
    parser.add_argument("--rows", type=int)
    parser.add_argument("--port", type=int, default=0)
    parser.add_argument("--max-wall-seconds", type=int, default=1800)
    parser.add_argument("--skip-build", action="store_true")
    parser.add_argument("--evidence-dir", type=Path)
    args = parser.parse_args()
    project = validated_project_name(args.project)
    target_mib = args.target_mib if args.target_mib is not None else (1 if args.smoke else 8)
    rows = args.rows if args.rows is not None else (1000 if args.smoke else 20_000)
    bench.fixture_plan(target_mib * bench.MIB, rows)
    if args.max_wall_seconds < 60 or not 0 <= args.port <= 65535:
        parser.error("Puerto/watchdog inválido.")
    evidence = (args.evidence_dir or ROOT / ".codex-local/delivery-benchmarks" / project).resolve()
    evidence.mkdir(parents=True, exist_ok=False)
    credentials = ["TvBenchmark-" + secrets.token_hex(18) + "!Aa1" for _ in range(3)]
    port = args.port or bench.available_port()
    base_url = f"http://127.0.0.1:{port}"
    environment = {**os.environ, "POSTGRES_PASSWORD": credentials[0],
        "POSTGRES_USER": "trackvance", "POSTGRES_DB": "trackvance",
        "DELIVERY_POSTGRES_ADMIN_PASSWORD": credentials[1],
        "DELIVERY_MSSQL_SA_PASSWORD": credentials[1], "DEMO_ACCESS_ENABLED": "true",
        "DEMO_SEED_ENABLED": "false", "WEB_PORT": str(port), "TRACKVANCE_WEB_ORIGIN": base_url,
        # Explicit standard defaults stop a shell's benchmark overrides leaking in.
        "MAX_UPLOAD_BYTES": str(10 * bench.MIB), "TRACKVANCE_MAX_ROWS": "100000"}
    compose = ["docker", "compose", "-p", project, "-f", "compose.yml",
               "-f", "deploy/docker/compose.delivery-test.yml",
               "-f", "deploy/docker/compose.delivery-benchmark.yml"]

    def run(arguments, *, input_text=None, capture=False):
        return bench.execute([*compose, *arguments], environment, credentials,
                             input_text=input_text, label="Delivery benchmark " + arguments[0])

    result: dict[str, Any] = {"schema_version": 1, "version": "0.5.1", "status": "FAIL",
        "project": project, "measurement": "SMOKE" if args.smoke else "BOUNDED_REPRESENTATIVE",
        "production_capacity_certification": False, "cases": [],
        "requested": {"target_mib": target_mib, "rows": rows, "max_wall_seconds": args.max_wall_seconds},
        "resource_measurement": "Cgroup deltas include incidental same-container activity; memory peak is cumulative. Sampling may miss short peaks.",
        "timing_semantics": {
            "total_seconds": "Explicit preflight + configuration publication + enqueue/poll + evidence GETs; excludes ingestion/startup, strategy preparation and independent SQL verification.",
            "write_seconds": "Worker perf_counter around deliver_prepared: connection, transactional checks/locks, DML and commit; excludes worker preflight and local preparation.",
            "worker_preflight_seconds": "Worker perf_counter around preflight_delivery, separate from explicit HTTP preflight.",
            "source_mb_per_second_write": "Original CSV bytes / 1,000,000 / write_seconds, not database wire throughput.",
        }}
    started = time.monotonic()
    owned = False
    watchdog = None
    fixture_path = evidence / "benchmark.csv"
    exit_code = 1
    try:
        capacity = bench.docker_capacity()
        result["machine"] = capacity
        policy = resource_policy(target_mib * bench.MIB, rows, capacity["docker"]["memory_bytes"],
                                 capacity["host"]["disk_free_bytes"])
        result["resource_policy"] = policy
        result["larger_tiers"] = [
            {"target_mib": size, "rows": 100_000,
             **resource_policy(size * bench.MIB, 100_000, capacity["docker"]["memory_bytes"],
                               capacity["host"]["disk_free_bytes"])}
            for size in (100, 500, 1024, 2048, 5120)
        ]
        if policy["status"] != "PLANNED":
            result.update(status="NOT_RUN_RESOURCE_LIMIT", stop_reason=policy["reasons"])
            raise BenchmarkNotRun
        existing = bench.docker_state.inventory(project)
        if any(existing[key] for key in ("containers", "volumes", "networks")):
            raise RuntimeError("El proyecto ya tiene recursos; no se iniciará ni limpiará.")
        owned = True
        fixture = bench.generate_fixture(fixture_path, target_mib * bench.MIB, rows, "varied")
        result["fixture"] = fixture
        if fixture["actual_bytes"] > 10 * bench.MIB:
            result.update(status="NOT_RUN_RESOURCE_LIMIT", stop_reason="ACTUAL_CSV_EXCEEDS_DEFAULT_UPLOAD")
            raise BenchmarkNotRun
        watchdog = bench.Watchdog(project, capacity["docker"]["memory_bytes"] - bench.GIB,
                                 started + args.max_wall_seconds, evidence)
        watchdog.start()
        up = ["up", "-d", "--wait", "--wait-timeout", "300"]
        if not args.skip_build:
            up.append("--build")
        run([*up, *SERVICES])
        for engine, filename in (("POSTGRESQL", "delivery-postgresql.sql"),
                                  ("SQLSERVER", "delivery-sqlserver.sql")):
            sql = (delivery.FIXTURES / filename).read_text(encoding="utf-8")
            delivery.target_sql(run, engine, sql.replace("__WRITER_PASSWORD__", credentials[2]),
                                use_delivery_database=engine != "SQLSERVER")
        api = bench.smoke.Api(base_url, timeout=600)
        api.csrf = api.post("/api/v1/auth/demo", {}, expected=(200,))["csrf_token"]
        dataset = api.post("/api/v1/datasets", {"name": "Delivery benchmark varied", "domain": "Benchmark"})
        version = bench.stream_upload(api, f"/api/v1/datasets/{dataset['id']}/versions/upload", fixture_path, 600)
        result["source_schema"] = {column["name"]: column["logical_type"] for column in version["schema"]}
        validate_source(version, fixture)
        for engine in ENGINES:
            destination = api.post("/api/v1/delivery/destinations", delivery.destination_body(engine, credentials[2]))
            for strategy in STRATEGIES:
                result["active_case"] = {"engine": engine, "strategy": strategy}
                result["cases"].append(measure_case(api, run, project, destination, version, fixture,
                                                   strategy, credentials, args.max_wall_seconds))
                if watchdog.breached:
                    raise RuntimeError("Resource watchdog: " + watchdog.breached)
        result.pop("active_case", None)
        result.update(status="PASS", stop_reason=None)
        exit_code = 0
    except BenchmarkNotRun:
        exit_code = 0
    except (OSError, RuntimeError, ValueError, KeyError, subprocess.SubprocessError,
            bench.smoke.SmokeFailure, delivery.smoke.SmokeFailure) as error:
        result.update(status="STOPPED_RESOURCE_LIMIT" if watchdog and watchdog.breached else "FAIL",
                      stop_reason=watchdog.breached if watchdog else None,
                      error=bench.redact(str(error), credentials)[-2000:])
        if result.get("active_case"):
            result["active_case"].update(status=result["status"], stop_reason=result["stop_reason"])
        print("ERROR: " + result["error"], file=sys.stderr)
    finally:
        if watchdog:
            watchdog.finish()
            result["resources"] = watchdog.report()
        result["not_run_cases"] = remaining_cases(
            result["cases"], result.get("active_case"),
            result["status"] in {"NOT_RUN_RESOURCE_LIMIT", "STOPPED_RESOURCE_LIMIT"})
        result["wall_seconds"] = time.monotonic() - started
        fixture_path.unlink(missing_ok=True)
        if owned:
            try:
                bench.cleanup(project, evidence)
                result["cleanup"] = "PASS"
            except (OSError, RuntimeError, ValueError) as error:
                result.update(cleanup="FAIL", cleanup_error=bench.redact(str(error), credentials), status="FAIL")
                exit_code = 1
        delivery.assert_no_credentials(result, credentials, "Credencial detectada en evidencia.")
        (evidence / "result.json").write_text(json.dumps(result, indent=2, sort_keys=True), encoding="utf-8")
        print(f"Delivery benchmark: {result['status']} ({len(result['cases'])}/8 casos).")
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
