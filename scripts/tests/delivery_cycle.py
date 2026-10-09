"""Certify Data Delivery against disposable real PostgreSQL and SQL Server targets.

Run from the repository root. The runner creates a uniquely named Compose project,
rejects pre-existing resources, and removes only that project's containers, volumes
and network.
Generated credentials are passed through environment/stdin and never written to evidence.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import math
import os
import re
import secrets
import shutil
import socket
import subprocess
import sys
import urllib.error
import urllib.request
import uuid
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlencode, urlsplit

from browser_evidence import run_browser
from isolation_profile import (
    assert_main_unchanged,
    isolate_compose,
    main_inventory,
    runtime_diagnostics,
)

ROOT = Path(__file__).resolve().parents[2]
PREFIX = "trackvance-delivery-e2e-"
FIXTURES = Path(__file__).with_name("fixtures")
PRIVATE_RESOURCES = {
    "postgres": (512, 0.10), "api": (768, 0.30), "worker": (512, 0.20),
    "acquisition-worker": (512, 0.10), "delivery-worker": (512, 0.30),
    "report-worker": (768, 0.20), "web": (128, 0.05), "mock-oidc": (128, 0.05),
    "destination-postgres": (256, 0.10), "destination-postgres18": (256, 0.10),
    "destination-sqlserver": (3072, 0.41), "scheduler": (256, 0.03),
    "events-notifications": (256, 0.03), "events-chaining": (256, 0.03),
}


def validate_private_resources(profile: dict) -> dict:
    """Every connector belongs to the same finite aggregate cgroup budget."""
    from ci.local_resources import memory_bytes

    services = profile["services"]
    if set(services) != set(PRIVATE_RESOURCES):
        raise ValueError("DELIVERY_PRIVATE_RESOURCE_TOPOLOGY")
    memory, cpus = 0, 0.0
    for name, (mib, cpu_ceiling) in PRIVATE_RESOURCES.items():
        row = services[name]
        allocated, cpu = memory_bytes(row["mem_limit"]), float(row["cpus"])
        if (allocated != mib * 1024**2 or not math.isfinite(cpu)
                or cpu <= 0 or cpu > cpu_ceiling + 0.000001):
            raise ValueError("PENDING_CAPACITY: DELIVERY_PRIVATE_PROFILE_REQUIRES_8192_MIB")
        if row.get("profiles"):
            raise ValueError("DELIVERY_ALL_FOURTEEN_SERVICES_REQUIRED")
        memory += allocated
        cpus += cpu
    if memory > 8 * 1024**3 or cpus > 2.000001:
        raise ValueError("DELIVERY_PRIVATE_AGGREGATE_BUDGET")
    sql = services["destination-sqlserver"]
    pool = int(sql.get("environment", {}).get("MSSQL_MEMORY_LIMIT_MB", "0"))
    if pool != 2048 or pool * 1024**2 >= memory_bytes(sql["mem_limit"]):
        raise ValueError("DELIVERY_SQLSERVER_ENGINE_CGROUP_MISMATCH")
    for name in ("api", "report-worker"):
        env = services[name].get("environment", {})
        engine = int(env.get("REPORT_MEMORY_MB", "512"))
        process = int(env.get("REPORT_PROCESS_MEMORY_MB", "2048"))
        if engine != 512 or process != 2048 or engine * 1024**2 >= memory_bytes(services[name]["mem_limit"]):
            raise ValueError("DELIVERY_REPORT_ENGINE_CGROUP_MISMATCH")
    return {"status": "PASS", "scope": "OWNED_SMALL_DELIVERY_085_ONLY",
            "memory_bytes": memory, "memory_ceiling_bytes": 8 * 1024**3,
            "cpus": round(cpus, 6), "cpu_ceiling": 2,
            "sqlserver_engine_memory_mib": pool, "sqlserver_cgroup_memory_mib": 3072,
            "report_engine_memory_mib": 512, "report_process_virtual_memory_mib": 2048,
            "active_services": sorted(PRIVATE_RESOURCES),
            "capacity_claim": "FINITE_CONFIG_ONLY_REAL_PEAK_MEASUREMENT_REQUIRED"}


def configure_private_resources(profile: dict, project: str) -> dict:
    """Restore explicit connector budgets after the generic private overlay."""
    from ci.local_resources import apply_limits

    validated_project_name(project)
    services = profile["services"]
    optional = {"report-worker", "destination-postgres", "destination-postgres18", "destination-sqlserver"}
    if (set(services) - set(PRIVATE_RESOURCES)
            or not set(PRIVATE_RESOURCES) - optional <= set(services)):
        raise ValueError("DELIVERY_PRIVATE_RESOURCE_TOPOLOGY")
    for name, (mib, cpus) in PRIVATE_RESOURCES.items():
        row = services.setdefault(name, {"pids_limit": 128 if name == "report-worker" else 256})
        row.update(mem_limit=mib * 1024**2, cpus=cpus)
        row.pop("profiles", None)
    services["destination-sqlserver"].setdefault("environment", {}).setdefault("MSSQL_MEMORY_LIMIT_MB", "2048")
    validate_private_resources(profile)
    apply_limits(profile, project)
    return validate_private_resources(profile)  # A reduced SQL cgroup never passes.

_spec = importlib.util.spec_from_file_location(
    "delivery_smoke", ROOT / "scripts" / "smoke_test.py"
)
assert _spec and _spec.loader
smoke = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = smoke
_spec.loader.exec_module(smoke)

DATASET_ROWS = [
    ["A-001", "José Álvarez", "10.50", 1, "2026-09-20", "2026-09-20T10:00:00+00:00", True, ""],
    ["B-002", "Miyuki 東京", "20.25", 2, "2026-09-21", "2026-09-21T11:30:00.123456-05:00", False, "nota"],
    ["C-003", "Zoë", "30.00", 3, "2026-09-22", "2026-09-22T12:45:00+00:00", True, "fin"],
]
DATASET_COLUMNS = [
    "record_id",
    "customer_name",
    "amount",
    "quantity",
    "happened_on",
    "updated_at",
    "is_active",
    "optional_note",
]
COLUMN_OVERRIDES = {
    "quantity": {"logical_type": "INT64"},
    "is_active": {"logical_type": "BOOLEAN"},
}
COLUMN_MAPPING = [
    {"source_name": "record_id", "target_name": "record_id", "target_type": "STRING",
     "ordinal": 0, "nullable": False, "length": 40},
    {"source_name": "customer_name", "target_name": "customer_name", "target_type": "STRING",
     "ordinal": 1, "nullable": False, "length": 160},
    {"source_name": "amount", "target_name": "amount", "target_type": "DECIMAL",
     "ordinal": 2, "nullable": True, "precision": 18, "scale": 2},
    {"source_name": "quantity", "target_name": "quantity", "target_type": "INT64",
     "ordinal": 3, "nullable": False},
    {"source_name": "happened_on", "target_name": "happened_on", "target_type": "DATE",
     "ordinal": 4, "nullable": False},
    {"source_name": "updated_at", "target_name": "updated_at", "target_type": "TIMESTAMP",
     "ordinal": 5, "nullable": False},
    {"source_name": "is_active", "target_name": "is_active", "target_type": "BOOLEAN",
     "ordinal": 6, "nullable": False},
    {"source_name": "optional_note", "target_name": "optional_note", "target_type": "STRING",
     "ordinal": 7, "nullable": True, "length": 240},
]


def validated_project_name(value: str) -> str:
    if not re.fullmatch(r"trackvance-delivery-e2e-[a-z0-9-]+", value):
        raise ValueError(f"El proyecto aislado debe comenzar por {PREFIX!r}.")
    return value


def preexisting_project_resources(command, project: str) -> dict[str, str]:
    """Inventory every Compose-owned resource that ``down -v`` could remove."""

    return {
        "containers": command(
            [
                "docker",
                "ps",
                "-aq",
                "--filter",
                f"label=com.docker.compose.project={project}",
            ],
            capture=True,
        ).strip(),
        "volumes": command(
            [
                "docker",
                "volume",
                "ls",
                "-q",
                "--filter",
                f"label=com.docker.compose.project={project}",
            ],
            capture=True,
        ).strip(),
        "networks": command(
            [
                "docker",
                "network",
                "ls",
                "-q",
                "--filter",
                f"label=com.docker.compose.project={project}",
            ],
            capture=True,
        ).strip(),
    }


def available_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


def redact(value: str, credentials: list[str]) -> str:
    for credential in credentials:
        value = value.replace(credential, "[REDACTED]")
    return value


def assert_no_credentials(value: object, credentials: list[str], message: str) -> None:
    rendered = value if isinstance(value, str) else json.dumps(value, default=str)
    if any(credential in rendered for credential in credentials):
        raise smoke.SmokeFailure(message)


def execute(
    arguments: list[str],
    *,
    environment: dict[str, str],
    credentials: list[str],
    cwd: Path = ROOT,
    input_text: str | None = None,
    capture: bool = False,
) -> str:
    print("+ " + redact(" ".join(arguments), credentials), flush=True)
    result = subprocess.run(
        arguments,
        cwd=cwd,
        env=environment,
        input=input_text,
        text=True,
        encoding="utf-8",
        errors="replace",
        capture_output=True,
        check=False,
    )
    if result.returncode:
        raise RuntimeError(redact(result.stdout + result.stderr, credentials)[-8000:])
    if not capture:
        print(redact(result.stdout + result.stderr, credentials), end="", flush=True)
    return result.stdout


def post_idempotent(api, path: str, payload: dict[str, Any], key: str) -> dict[str, Any]:
    body = json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(
        api.base_url + path,
        data=body,
        headers={
            "Accept": "application/json",
            "Content-Type": "application/json",
            "X-CSRF-Token": api.csrf,
            "Idempotency-Key": key,
        },
        method="POST",
    )
    try:
        with api.opener.open(request, timeout=api.timeout) as result:
            response = smoke.Response(result.status, result.headers, result.read())
    except urllib.error.HTTPError as error:
        response = smoke.Response(error.code, error.headers, error.read())
    if response.status != 202:
        detail = response.body.decode("utf-8", errors="replace")[:800]
        raise smoke.SmokeFailure(f"POST {path}: HTTP {response.status}, esperado 202. {detail}")
    return response.json()


def delivery_draft(
    dataset_version_id: str,
    destination: dict[str, Any],
    *,
    mode: str,
    schema_name: str,
    table_name: str,
    strategy: str,
    create_schema: bool = False,
    primary_key_columns: list[str] | None = None,
) -> dict[str, Any]:
    return {
        "schema_version": 1 if primary_key_columns is None else 2,
        "dataset_version_id": dataset_version_id,
        "destination_id": destination["id"],
        "destination_version_id": destination["destination_version_id"],
        "target": {
            "mode": mode,
            "schema_name": schema_name,
            "table_name": table_name,
            "create_schema": create_schema,
        },
        "columns": COLUMN_MAPPING,
        "write_strategy": strategy,
        "upsert_keys": ["record_id"] if strategy == "UPSERT" else [],
        **({"primary_key_mode": "DEFINE" if primary_key_columns else "NONE",
            "primary_key_columns": primary_key_columns} if primary_key_columns is not None else {}),
    }


def wait_for_run(api, run_id: str, *, timeout: float = 120) -> dict[str, Any]:
    terminal = {"SUCCESS", "FAILED", "UNKNOWN", "FAILED_PRECONDITION", "CANCELLED"}
    return smoke.wait_until(
        "la entrega " + run_id,
        lambda: api.get("/api/v1/runs/" + run_id),
        lambda current: current["status"] in terminal,
        timeout=timeout,
        interval=0.5,
    )


def wait_for_evidence(api, run_id: str, *, timeout: float = 120) -> dict[str, Any]:
    """COMMITTED is durable before evidence; wait without replay or repair."""
    def ready(current):
        if current["status"] != "SUCCESS" or current["decision"] != "COMMITTED":
            raise smoke.SmokeFailure("La evidencia requiere una entrega SUCCESS / COMMITTED.")
        metrics = current.get("metrics") or {}
        if metrics.get("evidence_status") == "PENDING_REPAIR":
            raise smoke.SmokeFailure("La entrega requiere reparación explícita de evidencia.")
        # The worker persists this reference in the same transaction as the
        # completed receipt AND manifest, after its separate COMMITTED commit.
        return bool(metrics.get("receipt_artifact_id"))

    return smoke.wait_until(
        "la evidencia local de la entrega " + run_id,
        lambda: api.get("/api/v1/runs/" + run_id),
        ready, timeout=timeout, interval=0.5,
    )


def publish_and_run(
    api,
    checks,
    draft: dict[str, Any],
    label: str,
    credentials: list[str],
) -> dict[str, Any]:
    preview = api.post("/api/v1/delivery/preview", draft, expected=(200,))
    checks.verify(
        preview["sampled_rows"] == len(DATASET_ROWS)
        and [item["target_name"] for item in preview["columns"]] == DATASET_COLUMNS,
        f"{label}: preview acotada respeta selección, orden y nombres",
    )
    preflight = api.post("/api/v1/delivery/preflight", draft, expected=(200,))
    checks.verify(
        preflight["status"] == "PASS"
        and all(item["status"] == "PASS" for item in preflight["checks"]),
        f"{label}: preflight real sin cambios persistentes supera todas las comprobaciones",
    )
    configuration = api.post(
        "/api/v1/delivery/configurations",
        {
            **draft,
            "name": f"Certificación {label}",
            "owner": "CI Data Delivery",
            "description": "Configuración efímera de certificación aislada",
        },
    )
    key = "delivery-e2e-" + uuid.uuid4().hex
    request = {
        "configuration_id": configuration["id"],
        "dataset_version_id": draft["dataset_version_id"],
    }
    queued = post_idempotent(api, "/api/v1/delivery/runs", request, key)
    duplicate = post_idempotent(api, "/api/v1/delivery/runs", request, key)
    checks.verify(
        queued["id"] == duplicate["id"],
        f"{label}: Idempotency-Key devuelve la misma Run sin duplicarla",
    )
    completed = wait_for_run(api, queued["id"])
    checks.verify(
        completed["status"] == "SUCCESS" and completed["decision"] == "COMMITTED",
        f"{label}: delivery-worker confirma la transacción remota",
    )
    completed = wait_for_evidence(api, completed["id"])
    attempts = api.get(f"/api/v1/delivery/runs/{completed['id']}/attempts")
    checks.verify(
        attempts["total"] == 1
        and attempts["items"][0]["status"] == "COMMITTED"
        and attempts["items"][0]["rows_written"] == len(DATASET_ROWS),
        f"{label}: DeliveryAttempt registra filas y estado COMMITTED",
    )
    receipt_response = api.request(
        "GET", f"/api/v1/delivery/runs/{completed['id']}/receipt"
    )
    receipt = receipt_response.json()
    evidence = api.get(f"/api/v1/runs/{completed['id']}/evidence")
    checks.verify(
        receipt["kind"] == "DELIVERY_RECEIPT"
        and receipt["run_id"] == completed["id"]
        and receipt["destination_version_id"] == draft["destination_version_id"]
        and receipt["write_strategy"] == draft["write_strategy"]
        and receipt["result"] == "COMMITTED",
        f"{label}: receipt inmutable identifica fuente, destino, estrategia e intento",
    )
    checks.verify(
        evidence["module"] == "DELIVERY"
        and evidence["delivery"]["attempt"]["id"] == attempts["items"][0]["id"],
        f"{label}: manifest schema 2 enlaza destino, intento y evidencia",
    )
    if draft.get("audit_columns_enabled"):
        snapshot = receipt.get("system_audit", {})
        checks.verify(snapshot.get("enabled") and bool(snapshot.get("username"))
                      and bool(snapshot.get("fecha_ingesta")) and bool(snapshot.get("policy_id"))
                      and snapshot == attempts["items"][0].get("system_audit")
                      and snapshot == evidence["delivery"].get("system_audit"),
                      f"{label}: receipt/manifest/intento preservan snapshot audit idéntico")
    assert_no_credentials(
        [preview, preflight, configuration, completed, attempts, receipt, evidence],
        credentials,
        "Una credencial apareció en respuesta, receipt o manifest de Delivery.",
    )
    return {
        "configuration_id": configuration["id"],
        "run_id": completed["id"],
        "attempt_id": attempts["items"][0]["id"],
        "status": "COMMITTED",
        "strategy": draft["write_strategy"],
    }


def failed_attempt(
    api,
    checks,
    draft: dict[str, Any],
    label: str,
    credentials: list[str],
) -> dict[str, Any]:
    preflight = api.post("/api/v1/delivery/preflight", draft, expected=(200,))
    checks.verify(preflight["status"] == "PASS", f"{label}: preflight previo es válido")
    configuration = api.post(
        "/api/v1/delivery/configurations",
        {
            **draft,
            "name": f"Fallo controlado {label}",
            "owner": "CI Data Delivery",
            "description": "Constraint remota deliberada para certificar DeliveryAttempt FAILED",
        },
    )
    queued = post_idempotent(
        api,
        "/api/v1/delivery/runs",
        {
            "configuration_id": configuration["id"],
            "dataset_version_id": draft["dataset_version_id"],
        },
        "delivery-e2e-failure-" + uuid.uuid4().hex,
    )
    completed = wait_for_run(api, queued["id"])
    attempts = api.get(f"/api/v1/delivery/runs/{completed['id']}/attempts")
    checks.verify(
        completed["status"] == "FAILED"
        and attempts["total"] == 1
        and attempts["items"][0]["status"] == "FAILED",
        f"{label}: constraint remota genera intento FAILED sin éxito simulado",
    )
    assert_no_credentials(
        [completed, attempts], credentials, "Una credencial apareció en el error de Delivery."
    )
    return {
        "run_id": completed["id"],
        "attempt_id": attempts["items"][0]["id"],
        "status": "FAILED",
    }


def certify_sqlserver_collation_guard(
    api,
    checks,
    destination: dict[str, Any],
    credentials: list[str],
    run,
) -> dict[str, Any]:
    dataset = api.post(
        "/api/v1/datasets",
        {"name": "Dataset collation SQL Server", "domain": "Certificación"},
    )
    rows = [
        ["COLLIDE", "First", "1.00", 1, "2026-09-20", "2026-09-20T10:00:00+00:00", True, ""],
        ["collide", "Second", "2.00", 2, "2026-09-21", "2026-09-21T10:00:00+00:00", True, ""],
    ]
    version = api.upload(
        f"/api/v1/datasets/{dataset['id']}/versions/upload",
        "delivery-collation.csv",
        smoke.csv_bytes(DATASET_COLUMNS, rows),
        fields={"column_overrides": json.dumps(COLUMN_OVERRIDES)},
    )
    draft = delivery_draft(
        version["id"],
        destination,
        mode="EXISTING_TABLE",
        schema_name="existing_delivery",
        table_name="records",
        strategy="UPSERT",
    )
    preflight = api.post("/api/v1/delivery/preflight", draft, expected=(200,))
    checks.verify(
        preflight["status"] == "PASS",
        "SQLSERVER: preflight conserva deduplicación exacta de la fuente",
    )
    before = existing_target_count(run, "SQLSERVER")
    configuration = api.post(
        "/api/v1/delivery/configurations",
        {
            **draft,
            "name": "Certificación collation SQL Server",
            "owner": "CI Data Delivery",
            "description": "La semántica nativa debe rechazar claves equivalentes",
        },
    )
    queued = post_idempotent(
        api,
        "/api/v1/delivery/runs",
        {
            "configuration_id": configuration["id"],
            "dataset_version_id": version["id"],
        },
        "delivery-e2e-collation-" + uuid.uuid4().hex,
    )
    completed = wait_for_run(api, queued["id"])
    attempts = api.get(f"/api/v1/delivery/runs/{completed['id']}/attempts")
    attempt = attempts["items"][0]
    checks.verify(
        completed["status"] == "FAILED"
        and attempt["status"] == "FAILED"
        and attempt["error_code"] == "DESTINATION_CONSTRAINT_VIOLATION",
        "SQLSERVER: collation nativa rechaza claves UPSERT equivalentes",
    )
    after = existing_target_count(run, "SQLSERVER")
    collision_rows = int(
        target_scalar(
            run,
            "SQLSERVER",
            "SELECT COUNT(*) FROM [existing_delivery].[records] "
            "WHERE [record_id] IN (N'COLLIDE', N'collide');",
        )
    )
    checks.verify(
        after == before and collision_rows == 0,
        "SQLSERVER: colisión UPSERT hace rollback antes de mutar el target",
    )
    assert_no_credentials(
        [completed, attempts],
        credentials,
        "Una credencial apareció en el fallo de collation SQL Server.",
    )
    return {"run_id": completed["id"], "attempt_id": attempt["id"], "status": "FAILED"}


def target_command(
    engine: str, sql: str, *, use_delivery_database: bool = True
) -> tuple[list[str], str]:
    if engine == "POSTGRESQL":
        return (
            [
                "exec",
                "-T",
                "destination-postgres",
                "psql",
                "-U",
                "delivery_admin",
                "-d",
                "trackvance_delivery",
                "-v",
                "ON_ERROR_STOP=1",
                "-At",
            ],
            sql,
        )
    database_prefix = "USE trackvance_delivery;\n" if use_delivery_database else ""
    return (
        [
            "exec",
            "-T",
            "destination-sqlserver",
            "sh",
            "-c",
            (
                'SQLCMDPASSWORD="$MSSQL_SA_PASSWORD" /opt/mssql-tools18/bin/sqlcmd '
                "-S localhost -U sa -C -b -h -1 -W -f 65001"
            ),
        ],
        "SET NOCOUNT ON;\n" + database_prefix + sql + "\nGO\n",
    )


def target_sql(
    run,
    engine: str,
    sql: str,
    *,
    capture: bool = False,
    use_delivery_database: bool = True,
) -> str:
    command, payload = target_command(
        engine, sql, use_delivery_database=use_delivery_database
    )
    return run(command, input_text=payload, capture=capture)


def target_scalar(run, engine: str, sql: str) -> str:
    output = target_sql(run, engine, sql, capture=True)
    lines = [line.strip() for line in output.splitlines() if line.strip()]
    if not lines:
        raise smoke.SmokeFailure(f"{engine}: la consulta de certificación no devolvió valor.")
    return lines[-1]


def existing_target_count(run, engine: str) -> int:
    locator = (
        '"existing_delivery"."records"'
        if engine == "POSTGRESQL"
        else "[existing_delivery].[records]"
    )
    return int(target_scalar(run, engine, f"SELECT COUNT(*) FROM {locator};"))


def prepare_overwrite_probe(run, engine: str) -> None:
    locator = (
        '"existing_delivery"."records"'
        if engine == "POSTGRESQL"
        else "[existing_delivery].[records]"
    )
    boolean = "TRUE" if engine == "POSTGRESQL" else "1"
    target_sql(
        run,
        engine,
        (
            f"INSERT INTO {locator} (record_id, customer_name, amount, quantity, happened_on, "
            "updated_at, is_active, optional_note) VALUES "
            f"('STALE', 'stale', 1.00, 1, '2026-01-01', '2026-01-01T00:00:00+00:00', "
            f"{boolean}, 'stale');"
        ),
    )


def prepare_upsert_probe(run, engine: str) -> None:
    locator = (
        '"existing_delivery"."records"'
        if engine == "POSTGRESQL"
        else "[existing_delivery].[records]"
    )
    target_sql(
        run,
        engine,
        (
            f"DELETE FROM {locator} WHERE record_id='C-003'; "
            f"UPDATE {locator} SET customer_name='stale' WHERE record_id='B-002';"
        ),
    )


def destination_body(engine: str, password: str) -> dict[str, Any]:
    return {
        "name": f"Destino real {engine}",
        "sink_type": engine,
        "host": "destination-postgres" if engine == "POSTGRESQL" else "destination-sqlserver",
        "port": 5432 if engine == "POSTGRESQL" else 1433,
        "database": "trackvance_delivery",
        "username": "tv_delivery_writer",
        "password": password,
        "options": {
            "connect_timeout": 5,
            "query_timeout": 30,
            **({"sslmode": "disable"} if engine == "POSTGRESQL" else {"encryption": "off"}),
        },
    }


def mock_sso_delivery_client(admin, checks, provider, credentials):
    """Run actual state/nonce/PKCE code flow against the disposable signed mock."""
    suffix = uuid.uuid4().hex[:10]
    username = f"delivery.{provider}.{suffix}"
    email = username + ("@outlook.com" if provider == "microsoft" else "@gmail.com")
    roles = admin.get("/api/v1/roles")["items"]
    role = next(item for item in roles if item["name"] == "Data Analyst")
    created = admin.post("/api/v1/users", {
        "first_name": "Delivery", "last_name": provider, "username": username,
        "email": email, "role_id": role["id"], "active": True,
    })
    temporary = created["temporary_credentials"]["temporary_password"]
    credentials.append(temporary)
    checks.verify(created["user"]["must_change_password"]
                  and created["temporary_credentials"]["username"] == username
                  and len(temporary) >= 20,
                  provider + ": usuario preprovisionado obtiene credencial efímera sin email")
    assert_no_credentials(admin.get(f"/api/v1/users/{created['user']['id']}"), [temporary],
                          "La credencial temporal apareció en GET User.")
    del temporary, created
    client = smoke.Api(admin.base_url, timeout=60)
    try:
        with client.opener.open(admin.base_url + f"/api/v1/auth/sso/{provider}/start", timeout=60) as response:
            authorization = response.geturl()
            response.read()
        parsed = urlsplit(authorization)
        if parsed.hostname != "127.0.0.1" or parsed.path != f"/{provider}/authorize":
            raise smoke.SmokeFailure("El proveedor OIDC de prueba no recibió la autorización.")
        form = {key: values[0] for key, values in parse_qs(parsed.query).items()}
        form.update(email=email, subject="delivery-" + suffix,
                    profile="Microsoft personal" if provider == "microsoft" else "Google Gmail", scenario="valid")
        request = urllib.request.Request(authorization.split("?", 1)[0],
            data=urlencode(form).encode(), headers={"Content-Type": "application/x-www-form-urlencoded"})
        with client.opener.open(request, timeout=60) as response:
            response.read()
    except (urllib.error.URLError, OSError, ValueError):
        raise smoke.SmokeFailure("Falló el flujo OIDC mock de Delivery; secretos excluidos del diagnóstico.") from None
    identity = client.get("/api/v1/me")
    client.csrf = identity["csrf_token"]
    checks.verify(identity["user"]["username"] == username and identity["user"]["must_change_password"],
                  provider + ": SSO conserva username interno y primer acceso restringido")
    client.request("GET", "/api/v1/delivery/configurations", expected=(403,))
    password = secrets.token_urlsafe(32)
    credentials.append(password)
    changed = client.post("/api/v1/auth/first-login/change-password", {"new_password": password}, expected=(200,))
    client.csrf = changed["csrf_token"]
    checks.verify(not changed["user"]["must_change_password"], provider + ": password local definido tras primer SSO")
    return client, username


def certify_audit_columns(api, checks, engine, version_id, destination, password, credentials, run):
    """Real transactional DDL/DML, permanent policy, compatible adoption and drift."""
    pg = engine == "POSTGRESQL"
    schema = "existing_delivery"
    quote = (lambda value: '"' + value + '"') if pg else (lambda value: "[" + value + "]")
    locator = lambda name: quote(schema) + "." + quote(name)
    stamp, username = quote("fechaIngesta"), quote("usuario")
    records = locator("records")
    audit_records = locator("audit_records")
    def clone(name, *, rows=False):
        target = locator(name)
        sql = (f"CREATE TABLE {target} AS SELECT * FROM {records}" + (";" if rows else " WHERE false;")
               if pg else f"SELECT * INTO {target} FROM {records}" + (";" if rows else " WHERE 1=0;"))
        if pg:
            sql += f" ALTER TABLE {target} OWNER TO tv_delivery_writer;"
        target_sql(run, engine, sql)
    def draft(name, strategy="APPEND", mode="EXISTING_TABLE"):
        return {**delivery_draft(version_id, destination, mode=mode,
                                schema_name=schema, table_name=name, strategy=strategy),
                "audit_columns_enabled": True}
    def policy(name):
        return api.get(f"/api/v1/delivery/destinations/{destination['id']}/target-policy?" + urlencode(
            {"schema_name": schema, "table_name": name}))
    def count(name, where="1=1"):
        return int(target_scalar(run, engine, f"SELECT COUNT(*) FROM {locator(name)} WHERE {where};"))
    created = publish_and_run(api, checks, draft("audit_created", "CREATE_AND_LOAD", "CREATE_TABLE"),
                              engine + " AUDIT CREATE", credentials)
    checks.verify(count("audit_created", f"{stamp} IS NOT NULL AND {username} IS NOT NULL") == 3,
                  engine + ": CREATE audit llena ambos campos en todas las filas")
    metadata = api.get(f"/api/v1/delivery/destinations/{destination['id']}/table-metadata?" + urlencode(
        {"schema_name": schema, "table_name": "audit_created"}))
    audit_types = {item["name"]: item for item in metadata["columns"]}
    checks.verify(not audit_types["fechaIngesta"]["nullable"] and not audit_types["usuario"]["nullable"],
                  engine + ": CREATE audit declara ambos NOT NULL")
    clone("audit_records", rows=True)
    target_sql(run, engine, f"UPDATE {audit_records} SET record_id=" +
               ("'OLD-' || record_id;" if pg else "N'OLD-' + record_id;"))
    target_sql(run, engine, f"ALTER TABLE {audit_records} ADD CONSTRAINT audit_records_pk PRIMARY KEY(record_id);")
    appended = publish_and_run(api, checks, draft("audit_records"), engine + " AUDIT APPEND", credentials)
    checks.verify(count("audit_records", f"{stamp} IS NULL AND {username} IS NULL") == 3
                  and count("audit_records", f"{stamp} IS NOT NULL AND {username} IS NOT NULL") == 3,
                  engine + ": ALTER sin DEFAULT conserva filas históricas NULL y audita nuevas")
    first_policy = policy("audit_records")
    checks.verify(first_policy["audit_columns_required"] and first_policy["materialized_at"],
                  engine + ": policy durable required/materialized tras commit confirmado")
    target_sql(run, engine, f"DELETE FROM {audit_records} WHERE record_id='C-003';")
    upserted = publish_and_run(api, checks, draft("audit_records", "UPSERT"), engine + " AUDIT UPSERT", credentials)
    upsert_receipt = api.get(f"/api/v1/delivery/runs/{upserted['run_id']}/receipt")
    latest_stamp = upsert_receipt["system_audit"]["fecha_ingesta"]
    instant = (f"'{latest_stamp}'::timestamptz" if pg else f"CONVERT(datetimeoffset(6), '{latest_stamp}', 127)")
    checks.verify(count("audit_records", f"{stamp} = {instant}") == 3,
                  engine + ": UPSERT INSERT y UPDATE reciben mismo timestamp nuevo")
    overwritten = publish_and_run(api, checks, draft("audit_records", "OVERWRITE"), engine + " AUDIT OVERWRITE", credentials)
    checks.verify(count("audit_records") == 3 and count("audit_records", f"{stamp} IS NULL") == 0,
                  engine + ": OVERWRITE audita todas las filas resultantes")
    disabled = {**draft("audit_records"), "audit_columns_enabled": False}
    rejection = api.request("POST", "/api/v1/delivery/configurations", {"name": "disable forbidden", **disabled}, expected=(412,)).json()
    checks.verify(rejection["error"]["code"] == "AUDIT_COLUMNS_REQUIRED", engine + ": API rechaza deshabilitar política")
    duplicate_destination = api.post("/api/v1/delivery/destinations", {
        **destination_body(engine, password), "name": "Physical target alias " + engine,
    })
    alias_policy = api.get(f"/api/v1/delivery/destinations/{duplicate_destination['id']}/target-policy?" + urlencode(
        {"schema_name": schema, "table_name": "audit_records"}))
    checks.verify(alias_policy["policy_id"] == first_policy["policy_id"],
                  engine + ": destino duplicado conserva política física")
    for name, existing in (("audit_adopted", "both"), ("audit_partial", "timestamp"), ("audit_bad", "bad")):
        clone(name)
        temporal = "TIMESTAMPTZ(6)" if pg else "DATETIMEOFFSET(6)"
        string = "VARCHAR(128)" if pg else "NVARCHAR(128)"
        target_sql(run, engine, f"ALTER TABLE {locator(name)} ADD {stamp} " +
                   ("INTEGER" if existing == "bad" else temporal) + " NULL;")
        if existing == "both":
            target_sql(run, engine, f"ALTER TABLE {locator(name)} ADD {username} {string} NULL;")
        if existing == "bad":
            invalid = api.request("POST", "/api/v1/delivery/preflight", draft(name), expected=(412,)).json()
            checks.verify(invalid["error"]["code"] == "AUDIT_COLUMNS_INCOMPATIBLE", engine + ": tipo audit externo incompatible falla cerrado")
        else:
            result = publish_and_run(api, checks, draft(name), engine + " AUDIT " + existing, credentials)
            receipt = api.get(f"/api/v1/delivery/runs/{result['run_id']}/receipt")
            checks.verify(receipt["system_audit"]["columns_created"] == (existing == "timestamp"),
                          engine + ": adopción audit compatible crea sólo faltante")
    collision = draft("audit_records")
    collision["columns"] = [dict(c) for c in COLUMN_MAPPING]
    collision["columns"][0]["target_name"] = "usuario"
    invalid = api.request("POST", "/api/v1/delivery/preflight", collision, expected=(412,)).json()
    checks.verify(invalid["error"]["code"] == "AUDIT_MAPPING_COLLISION", engine + ": mapping no puede escribir auditoría")
    # Execute against the real target, then deliberately lose only the adapter's
    # acknowledgement. This certifies OUR durable UNKNOWN semantics, not a real
    # network outage, and the fixture records that distinction in its evidence.
    clone("audit_unknown")
    unknown_draft = draft("audit_unknown")
    unknown_config = api.post("/api/v1/delivery/configurations", {"name": engine + " audit UNKNOWN", **unknown_draft})
    run(["stop", "delivery-worker"])
    try:
        queued = post_idempotent(api, "/api/v1/delivery/runs", {
            "configuration_id": unknown_config["id"], "dataset_version_id": version_id,
        }, "audit-unknown-" + uuid.uuid4().hex)
        probe = "\n".join([
            "from trackvance.db import SessionLocal",
            "from trackvance.models import Run",
            "from trackvance import delivery_service",
            "from trackvance.data_sinks import DeliveryError",
            "original = delivery_service.sink_registry.create",
            "def create(settings):",
            "    sink = original(settings)",
            "    deliver = sink.deliver_prepared",
            "    def lose_ack(payload):",
            "        deliver(payload)",
            "        raise DeliveryError('CERTIFICATION_ACK_LOSS', 'Controlled commit acknowledgement loss', ambiguous=True)",
            "    sink.deliver_prepared = lose_ack",
            "    return sink",
            "delivery_service.sink_registry.create = create",
            "with SessionLocal() as db:",
            "    delivery_service.execute_delivery_run(db, db.get(Run, " + repr(queued["id"]) + "))",
        ])
        run(["exec", "-T", "api", "python", "-"], input_text=probe)
    finally:
        run(["up", "-d", "--wait", "delivery-worker"])
    unknown = wait_for_run(api, queued["id"])
    unknown_policy = policy("audit_unknown")
    checks.verify(unknown["status"] == "UNKNOWN" and count("audit_unknown") == 3
                  and unknown_policy["audit_columns_required"] and unknown_policy["materialized_at"] is None,
                  engine + ": commit real + pérdida simulada de confirmación conserva UNKNOWN y required sin materialized")
    attempts = api.get(f"/api/v1/delivery/runs/{unknown['id']}/attempts")['items']
    review = api.post(f"/api/v1/delivery/runs/{unknown['id']}/reviews", {
        "delivery_attempt_id": attempts[0]["id"], "outcome": "REMOTE_COMMIT_OBSERVED",
        "note": "Fixture aislada: SELECT verificó tres filas tras el commit remoto y antes de una nueva operación deliberada.",
    })
    blocked_config = api.post("/api/v1/delivery/configurations", {
        "name": engine + " UNKNOWN fence", **draft("audit_unknown", "OVERWRITE"),
    })
    blocked = post_idempotent(api, "/api/v1/delivery/runs", {
        "configuration_id": blocked_config["id"], "dataset_version_id": version_id,
    }, "unknown-fence-" + uuid.uuid4().hex)
    blocked = wait_for_run(api, blocked["id"])
    diagnostic = [item for item in api.get("/api/v1/audit-events")["items"]
                  if item["run_id"] == blocked["id"] and item["event_type"] == "DELIVERY_FAILED"]
    checks.verify(blocked["status"] == "FAILED_PRECONDITION"
                  and any(item["metadata"].get("error_code") == "TARGET_UNKNOWN_BLOCKED" for item in diagnostic)
                  and not api.get(f"/api/v1/delivery/runs/{blocked['id']}/attempts")['items']
                  and count("audit_unknown") == 3,
                  engine + ": la revisión sola no permite escribir ni crea otro intento remoto")
    decision = api.post(f"/api/v1/delivery/runs/{unknown['id']}/resume-target", {
        "review_id": review["id"],
        "note": "Verificación SQL concluida; autorizar únicamente una nueva operación OVERWRITE explícita de la fixture.",
    }, expected=(200,))
    checks.verify(decision["status"] == "RESUMED" and decision["historical_status"] == "UNKNOWN",
                  engine + ": decisión explícita reanuda el target sin cambiar la incertidumbre histórica")
    resumed = publish_and_run(api, checks, draft("audit_unknown", "OVERWRITE"), engine + " AUDIT after UNKNOWN", credentials)
    checks.verify(api.get("/api/v1/runs/" + unknown["id"])["status"] == "UNKNOWN"
                  and policy("audit_unknown")["materialized_at"] is not None,
                  engine + ": nueva operación deliberada adopta columnas sin reescribir UNKNOWN")
    sso_runs = []
    for provider in ("microsoft", "google"):
        sso, internal_username = mock_sso_delivery_client(api, checks, provider, credentials)
        sso_result = publish_and_run(sso, checks, draft("audit_" + provider, "CREATE_AND_LOAD", "CREATE_TABLE"),
                                     engine + " AUDIT " + provider, credentials)
        remote_username = target_scalar(run, engine, f"SELECT MIN({username}) FROM {locator('audit_' + provider)};")
        receipt = sso.get(f"/api/v1/delivery/runs/{sso_result['run_id']}/receipt")
        checks.verify(remote_username == internal_username and receipt["system_audit"]["username"] == internal_username,
                      engine + ": " + provider + " publica username interno, no email/display name")
        sso_runs.append(sso_result)
    clone("audit_no_alter")
    if pg:
        target_sql(run, engine, f"ALTER TABLE {locator('audit_no_alter')} OWNER TO delivery_admin; GRANT SELECT, INSERT ON {locator('audit_no_alter')} TO tv_delivery_writer;")
    else:
        target_sql(run, engine, f"DENY ALTER ON OBJECT::{locator('audit_no_alter')} TO tv_delivery_writer;")
    denied = api.request("POST", "/api/v1/delivery/preflight", draft("audit_no_alter"), expected=(412,)).json()
    checks.verify(any(c["code"] == "AUDIT_ALTER_PERMISSION" and c["status"] == "FAIL"
                      for c in denied["error"]["details"]["checks"]), engine + ": permiso ALTER remoto insuficiente bloquea preflight")
    target_sql(run, engine, f"ALTER TABLE {audit_records} DROP COLUMN {username};")
    drift = api.request("POST", "/api/v1/delivery/preflight", draft("audit_records"), expected=(412,)).json()
    checks.verify(drift["error"]["code"] == "AUDIT_COLUMNS_DRIFT", engine + ": external DROP detecta drift sin recrear columna")
    return {"status": "PASS", "runs": [created, appended, upserted, overwritten], "policy_id": first_policy["policy_id"], "unknown_simulated_ack_loss": unknown["id"], "resumed": resumed, "sso_runs": sso_runs}


def certify_engine(
    api,
    checks,
    engine: str,
    dataset_version_id: str,
    password: str,
    credentials: list[str],
    run,
) -> dict[str, Any]:
    body = destination_body(engine, password)
    tested = api.post("/api/v1/delivery/destinations/test", body, expected=(200,))
    checks.verify(tested["status"] == "SUCCESS", f"{engine}: prueba credencial real de escritura")
    destination = api.post("/api/v1/delivery/destinations", body)
    assert_no_credentials(destination, credentials, "El destino serializado contiene una credencial.")
    saved_test = api.post(
        f"/api/v1/delivery/destinations/{destination['id']}/test", {}, expected=(200,)
    )
    checks.verify(
        saved_test["status"] == "SUCCESS" and destination["version"] == 1,
        f"{engine}: destino versionado recupera su secreto segregado",
    )
    path = f"/api/v1/delivery/destinations/{destination['id']}"
    schemas = api.get(path + "/schemas")
    tables = api.get(path + "/tables?" + urlencode({"schema_name": "existing_delivery"}))
    metadata = api.get(
        path
        + "/table-metadata?"
        + urlencode({"schema_name": "existing_delivery", "table_name": "records"})
    )
    checks.verify(
        "existing_delivery" in schemas["items"]
        and {"records", "failing_records"}.issubset(tables["items"])
        and [item["name"] for item in metadata["columns"]] == DATASET_COLUMNS,
        f"{engine}: descubre schemas, tablas, columnas, nullability y constraints reales",
    )

    created_schema = "created_pg" if engine == "POSTGRESQL" else "created_mssql"
    runs = [
        publish_and_run(
            api,
            checks,
            delivery_draft(
                dataset_version_id,
                destination,
                mode="CREATE_TABLE",
                schema_name=created_schema,
                table_name="created_records",
                strategy="CREATE_AND_LOAD",
                create_schema=True,
            ),
            f"{engine} CREATE_AND_LOAD",
            credentials,
        )
    ]
    created_locator = (
        f'"{created_schema}"."created_records"'
        if engine == "POSTGRESQL"
        else f"[{created_schema}].[created_records]"
    )
    checks.verify(
        int(target_scalar(run, engine, f"SELECT COUNT(*) FROM {created_locator};"))
        == len(DATASET_ROWS),
        f"{engine}: crea schema/tabla y carga exactamente la DatasetVersion",
    )

    for strategy in ("APPEND", "OVERWRITE", "UPSERT"):
        if strategy == "OVERWRITE":
            prepare_overwrite_probe(run, engine)
            checks.verify(
                existing_target_count(run, engine) == len(DATASET_ROWS) + 1,
                f"{engine}: fixture stale preparado antes de OVERWRITE",
            )
        elif strategy == "UPSERT":
            prepare_upsert_probe(run, engine)
            checks.verify(
                existing_target_count(run, engine) == len(DATASET_ROWS) - 1,
                f"{engine}: fixture update/insert preparado antes de UPSERT",
            )
        draft = delivery_draft(
            dataset_version_id,
            destination,
            mode="EXISTING_TABLE",
            schema_name="existing_delivery",
            table_name="records",
            strategy=strategy,
        )
        runs.append(
            publish_and_run(api, checks, draft, f"{engine} {strategy}", credentials)
        )
        checks.verify(
            existing_target_count(run, engine) == len(DATASET_ROWS),
            f"{engine}: {strategy} deja el conjunto remoto esperado",
        )
    locator = (
        '"existing_delivery"."records"'
        if engine == "POSTGRESQL"
        else "[existing_delivery].[records]"
    )
    restored_name = target_scalar(
        run, engine, f"SELECT customer_name FROM {locator} WHERE record_id='B-002';"
    )
    checks.verify(
        restored_name == "Miyuki 東京",
        f"{engine}: UPSERT actualiza coincidencias y recupera valores Unicode",
    )

    missing = delivery_draft(
        dataset_version_id,
        destination,
        mode="EXISTING_TABLE",
        schema_name="existing_delivery",
        table_name="missing_records",
        strategy="APPEND",
    )
    missing_error = api.request(
        "POST", "/api/v1/delivery/preflight", missing, expected=(412,)
    ).json()
    checks.verify(
        missing_error["error"]["code"] == "FAILED_PRECONDITION",
        f"{engine}: preflight rechaza target inexistente sin modificarlo",
    )
    forbidden = delivery_draft(
        dataset_version_id,
        destination,
        mode="CREATE_TABLE",
        schema_name="forbidden_delivery",
        table_name="blocked_records",
        strategy="CREATE_AND_LOAD",
    )
    permission_error = api.request(
        "POST", "/api/v1/delivery/preflight", forbidden, expected=(412,)
    ).json()
    checks.verify(
        permission_error["error"]["code"] == "FAILED_PRECONDITION",
        f"{engine}: preflight detecta permisos insuficientes sin escribir",
    )
    failure = failed_attempt(
        api,
        checks,
        delivery_draft(
            dataset_version_id,
            destination,
            mode="EXISTING_TABLE",
            schema_name="existing_delivery",
            table_name="failing_records",
            strategy="APPEND",
        ),
        engine,
        credentials,
    )
    collation_guard = (
        certify_sqlserver_collation_guard(api, checks, destination, credentials, run)
        if engine == "SQLSERVER"
        else None
    )
    primary_keys = certify_primary_keys(api, checks, engine, dataset_version_id, destination, credentials, run)
    audit_columns = certify_audit_columns(api, checks, engine, dataset_version_id, destination, password, credentials, run)
    return {
        "primary_keys": primary_keys,
        "audit_columns": audit_columns,
        "sink_type": engine,
        "destination_id": destination["id"],
        "destination_version_id": destination["destination_version_id"],
        "runs": runs,
        "failed_attempt": failure,
        "collation_guard": collation_guard,
        "status": "PASS",
    }


def certify_primary_keys(api, checks, engine, version_id, destination, credentials, run):
    """R085-02 oracle: real SQL constraints, one backing index and all source rows."""
    cases = []
    for table, keys in (("pk_simple_085", ["quantity"]), ("pk_composite_085", ["transaction_code", "quantity"])):
        draft = delivery_draft(version_id, destination, mode="CREATE_TABLE", schema_name="existing_delivery",
                               table_name=table, strategy="CREATE_AND_LOAD", primary_key_columns=keys)
        draft["columns"] = [dict(column) for column in draft["columns"]]
        if table == "pk_composite_085":
            draft["columns"][0]["target_name"] = "transaction_code"
        delivered = publish_and_run(api, checks, draft, f"R085-02 {engine} {table}", credentials)
        metadata = api.get(f"/api/v1/delivery/destinations/{destination['id']}/table-metadata?" + urlencode(
            {"schema_name": "existing_delivery", "table_name": table}))
        constraints = [item for item in metadata["constraints"] if item["type"] == "PRIMARY_KEY"]
        checks.verify(len(constraints) == 1 and constraints[0]["columns"] == keys,
                      f"R085-02 {engine}: PK real respeta orden y nombres finales {table}")
        checks.verify(all(not item["nullable"] for item in metadata["columns"] if item["name"] in keys),
                      f"R085-02 {engine}: todas las columnas PK son NOT NULL")
        locator = f'"existing_delivery"."{table}"' if engine == "POSTGRESQL" else f"[existing_delivery].[{table}]"
        count = int(target_scalar(run, engine, f"SELECT COUNT(*) FROM {locator};"))
        if engine == "POSTGRESQL":
            indexes = int(target_scalar(run, engine, f"SELECT COUNT(*) FROM pg_index WHERE indrelid='existing_delivery.{table}'::regclass;"))
        else:
            indexes = int(target_scalar(run, engine, f"SELECT COUNT(*) FROM sys.indexes WHERE object_id=OBJECT_ID(N'existing_delivery.{table}') AND index_id>0;"))
        checks.verify(count == len(DATASET_ROWS) and indexes == 1,
                      f"R085-02 {engine}: carga completa y único índice de respaldo, sin duplicación")
        existing = {**draft, "schema_version": 1, "target": {**draft["target"], "mode": "EXISTING_TABLE"}}
        existing.pop("primary_key_mode")
        existing.pop("primary_key_columns")
        existing["write_strategy"] = "APPEND"
        append_failure = failed_attempt(api, checks, existing, f"R085-02 {engine} APPEND PK", credentials)
        checks.verify(int(target_scalar(run, engine, f"SELECT COUNT(*) FROM {locator};")) == count,
                      f"R085-02 {engine}: APPEND duplicado revierte población, conserva PK")
        existing["write_strategy"] = "OVERWRITE"
        overwritten = publish_and_run(api, checks, existing, f"R085-02 {engine} OVERWRITE PK", credentials)
        existing.update(write_strategy="UPSERT", upsert_keys=keys)
        upserted = publish_and_run(api, checks, existing, f"R085-02 {engine} UPSERT PK", credentials)
        checks.verify(int(target_scalar(run, engine, f"SELECT COUNT(*) FROM {locator};")) == count,
                      f"R085-02 {engine}: OVERWRITE/UPSERT posteriores conservan estructura y conteo")
        after = api.get(f"/api/v1/delivery/destinations/{destination['id']}/table-metadata?" + urlencode(
            {"schema_name": "existing_delivery", "table_name": table}))
        checks.verify([item for item in after["constraints"] if item["type"] == "PRIMARY_KEY"] == constraints
                      and all(not item["nullable"] for item in after["columns"] if item["name"] in keys),
                      f"R085-02 {engine}: estrategias posteriores preservan la PK y NOT NULL")
        cases.append({"status": "PASS", "table": table, "primary_key_columns": keys, "rows": count,
                      "backing_indexes": indexes, "not_null": True, "existing_strategies_preserve_pk": True,
                      "run": delivered, "append_failure": append_failure, "overwrite": overwritten, "upsert": upserted})
    none = delivery_draft(version_id, destination, mode="CREATE_TABLE", schema_name="existing_delivery",
                          table_name="pk_none_085", strategy="CREATE_AND_LOAD", primary_key_columns=[])
    publish_and_run(api, checks, none, f"R085-02 {engine} sin PK explícita", credentials)
    metadata = api.get(f"/api/v1/delivery/destinations/{destination['id']}/table-metadata?" + urlencode(
        {"schema_name": "existing_delivery", "table_name": "pk_none_085"}))
    checks.verify(not any(item["type"] == "PRIMARY_KEY" for item in metadata["constraints"]),
                  f"R085-02 {engine}: NONE explícito crea sin inferir una PK")
    cases.append({"case": "EXPLICIT_NONE", "status": "PASS", "primary_key_mode": "NONE"})
    repeated = delivery_draft(version_id, destination, mode="CREATE_TABLE", schema_name="existing_delivery",
                              table_name="pk_repeated_rejected_085", strategy="CREATE_AND_LOAD", primary_key_columns=["is_active"])
    error = api.request("POST", "/api/v1/delivery/preflight", repeated, expected=(412,)).json()
    checks.verify(any(item["code"] == "PRIMARY_KEY_SOURCE_KEYS" and item["status"] == "FAIL"
                      for item in error["error"]["details"]["checks"]),
                  f"R085-02 {engine}: clave repetida rechazada sobre población completa")
    cases.append({"case": "REPEATED_POPULATION", "status": "REJECTED_PREFLIGHT"})
    for problem in ("NULL_BEYOND_PREVIEW", "COLLATION", "NATIVE_KEY_BYTES"):
        if problem == "COLLATION" and engine != "SQLSERVER":
            continue
        rows = [list(DATASET_ROWS[0]) for _ in range(13 if problem == "NULL_BEYOND_PREVIEW" else 2)]
        for index, row in enumerate(rows):
            row[0] = ("" if index == 12 else f"K-{index:03}") if problem == "NULL_BEYOND_PREVIEW" else ("COLLIDE" if index == 0 else "collide")
        if problem == "NATIVE_KEY_BYTES":
            rows = [list(DATASET_ROWS[0])]
            rows[0][0] = "X" * 500 if engine == "SQLSERVER" else "".join(hashlib.sha256(str(index).encode()).hexdigest() for index in range(256))
        dataset = api.post("/api/v1/datasets", {"name": f"R085-02 {engine} {problem}", "domain": "Certificación"})
        source = api.upload(f"/api/v1/datasets/{dataset['id']}/versions/upload", "pk-rejected.csv",
                            smoke.csv_bytes(DATASET_COLUMNS, rows), fields={"column_overrides": json.dumps(COLUMN_OVERRIDES)})
        draft = delivery_draft(source["id"], destination, mode="CREATE_TABLE", schema_name="existing_delivery",
                               table_name="pk_" + problem.lower() + "_085", strategy="CREATE_AND_LOAD", primary_key_columns=["record_id"])
        draft["columns"] = [dict(column) for column in draft["columns"]]
        if problem == "NATIVE_KEY_BYTES":
            draft["columns"][0]["length"] = 600 if engine == "SQLSERVER" else 20000
        error = api.request("POST", "/api/v1/delivery/preflight", draft, expected=(412,)).json()
        code = {"NULL_BEYOND_PREVIEW": "PRIMARY_KEY_SOURCE_KEYS", "COLLATION": "PRIMARY_KEY_DESTINATION_COLLISION", "NATIVE_KEY_BYTES": "PRIMARY_KEY_DESTINATION_LIMIT"}[problem]
        checks.verify(any(item["code"] == code and item["status"] == "FAIL" for item in error["error"]["details"]["checks"]),
                      f"R085-02 {engine}: {problem} rechazado sin crear target")
        if engine == "POSTGRESQL":
            created = target_scalar(run, engine, f"SELECT COUNT(*) FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace WHERE n.nspname='existing_delivery' AND c.relname='{draft['target']['table_name']}';")
        else:
            created = target_scalar(run, engine, f"SELECT COUNT(*) FROM sys.tables WHERE object_id=OBJECT_ID(N'existing_delivery.{draft['target']['table_name']}');")
        checks.verify(int(created) == 0, f"R085-02 {engine}: preflight inválido no deja tabla final")
        cases.append({"case": problem, "status": "REJECTED_PREFLIGHT", "source_rows": len(rows)})
    return {"status": "PASS", "requirement": "R085-02", "cases": cases}


def main() -> int:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project", default=f"{PREFIX}{os.getpid()}-{uuid.uuid4().hex[:6]}")
    parser.add_argument("--port", type=int, default=0)
    parser.add_argument("--skip-build", action="store_true")
    parser.add_argument("--skip-playwright", action="store_true")
    parser.add_argument("--full-playwright", action="store_true")
    parser.add_argument("--evidence-dir", type=Path)
    args = parser.parse_args()
    try:
        project = validated_project_name(args.project)
    except ValueError as error:
        parser.error(str(error))
    port = args.port or available_port()
    if not 1 <= port <= 65535:
        parser.error("--port debe estar entre 1 y 65535")

    writer_password = "TvDelivery-" + secrets.token_hex(18)
    admin_password = "TvAdmin-" + secrets.token_hex(18) + "!Aa1"
    internal_password = "TvInternal-" + secrets.token_hex(18)
    mock_secret = secrets.token_urlsafe(32)
    credentials = [writer_password, admin_password, internal_password, mock_secret]
    base_url = f"http://127.0.0.1:{port}"
    environment = {
        **os.environ,
        "PYTHONIOENCODING": "utf-8",
        "COMPOSE_PROJECT_NAME": project,
        "WEB_PORT": str(port),
        "TRACKVANCE_WEB_ORIGIN": base_url,
        "TRACKVANCE_PUBLIC_URL": base_url,
        "MOCK_OIDC_PORT": str(available_port()),
        "MOCK_OIDC_CLIENT_SECRET": mock_secret,
        "TV_E2E_URL": base_url,
        "POSTGRES_USER": "trackvance",
        "POSTGRES_DB": "trackvance",
        "POSTGRES_PASSWORD": internal_password,
        "DELIVERY_POSTGRES_ADMIN_PASSWORD": admin_password,
        "DELIVERY_MSSQL_SA_PASSWORD": admin_password,
        "DEMO_ACCESS_ENABLED": "true",
        "DEMO_SEED_ENABLED": "false",
        "TV_DELIVERY_E2E": "true",
        "TV_DELIVERY_PASSWORD": writer_password,
    }
    compose = [
        "docker",
        "compose",
        "-p",
        project,
        "-f",
        "compose.yml",
        "-f",
        "deploy/docker/compose.delivery-test.yml",
        "-f", "deploy/docker/compose.identity-test.yml",
    ]
    evidence = args.evidence_dir or ROOT / ".codex-local" / "delivery-e2e" / project
    evidence.mkdir(parents=True, exist_ok=True)
    compose = isolate_compose(compose, environment, evidence, project)
    # The integral browser adds a small Polars Intake and real Reportes worker.
    # No Spark-sized JVM is required for this functional fixture.
    private_profile = evidence / "private-compose.json"
    profile = json.loads(private_profile.read_text(encoding="utf-8"))
    resource_profile = configure_private_resources(profile, project)
    private_profile.write_text(json.dumps(profile, indent=2), encoding="utf-8")
    (evidence / "private-resource-profile.json").write_text(json.dumps(resource_profile, indent=2), encoding="utf-8")

    def command(arguments, **kwargs):
        return execute(arguments, environment=environment, credentials=credentials, **kwargs)

    def run(arguments, **kwargs):
        if command(["docker", "context", "show"], capture=True).strip() != docker_context:
            raise RuntimeError("El contexto Docker cambió; se rechaza operar o limpiar el proyecto.")
        return command([*compose, *arguments], **kwargs)

    inventory_reader = lambda arguments: command(arguments, capture=True)
    docker_context = command(["docker", "context", "show"], capture=True).strip()
    main_before = main_inventory(inventory_reader)

    started = False
    checks = smoke.Checks()
    results: list[dict[str, Any]] = []
    try:
        command(["docker", "info", "--format", "{{.OSType}}"])
        existing = preexisting_project_resources(command, project)
        if any(existing.values()):
            raise RuntimeError(f"El proyecto {project} ya tiene recursos; utiliza otro nombre.")
        resolved_profile = json.loads(command([*compose, "config", "--format", "json"], capture=True))
        resource_profile = validate_private_resources(resolved_profile)
        started = True
        run(
            ["up", "-d", "--wait", "--wait-timeout", "300",
             "destination-postgres", "destination-postgres18", "destination-sqlserver"]
        )
        postgres_sql = (FIXTURES / "delivery-postgresql.sql").read_text(encoding="utf-8")
        sqlserver_sql = (FIXTURES / "delivery-sqlserver.sql").read_text(encoding="utf-8")
        target_sql(run, "POSTGRESQL", postgres_sql.replace("__WRITER_PASSWORD__", writer_password))
        target_sql(
            run,
            "SQLSERVER",
            sqlserver_sql.replace("__WRITER_PASSWORD__", writer_password),
            use_delivery_database=False,
        )
        up = ["up", "-d", "--wait", "--wait-timeout", "300"]
        if not args.skip_build:
            up.append("--build")
        run(up)
        run(["exec", "-T", "api", "alembic", "check"])
        command(
            [sys.executable, "scripts/doctor.py", "--base-url", base_url,
             "--docker", "--project", project]
        )
        api = smoke.Api(base_url, timeout=60)
        application_version = api.get("/api/v1/health")["version"]
        api.request("GET", "/api/v1/delivery/destinations", expected=(401,))
        auth = api.post("/api/v1/auth/demo", {}, expected=(200,))
        api.csrf = auth["csrf_token"]
        dataset = api.post(
            "/api/v1/datasets",
            {"name": "Dataset Data Delivery E2E", "domain": "Certificación"},
        )
        contents = smoke.csv_bytes(DATASET_COLUMNS, DATASET_ROWS)
        version = api.upload(
            f"/api/v1/datasets/{dataset['id']}/versions/upload",
            "delivery-e2e.csv",
            contents,
            fields={"column_overrides": json.dumps(COLUMN_OVERRIDES)},
        )
        checks.verify(
            version["row_count"] == len(DATASET_ROWS),
            "DatasetVersion inmutable de entrada contiene las filas certificadas",
        )
        results = [
            certify_engine(
                api, checks, engine, version["id"], writer_password, credentials, run
            )
            for engine in ("POSTGRESQL", "SQLSERVER")
        ]
        metrics_probe = (ROOT / "scripts/tests/delivery_metrics_probe.py").read_text(encoding="utf-8")
        metrics_matrix = json.loads(run(["exec", "-T", "api", "python", "-"],
            input_text=metrics_probe + "\nimport json\nprint(json.dumps([" +
            ",".join(f"certify_postgres_metrics({host!r}, {admin_password!r})"
                     for host in ("destination-postgres", "destination-postgres18")) + "]))\n",
            capture=True))
        for engine_result in metrics_matrix:
            for case in engine_result["cases"]:
                checks.verify(case["status"] == "PASS",
                    f"PostgreSQL {engine_result['major_version']}: métricas {case['case']}")

        run(["restart", "postgres", "api", "worker", "delivery-worker", "web"])
        run(["up", "-d", "--wait", "--wait-timeout", "180"])
        for result in results:
            tested = api.post(
                f"/api/v1/delivery/destinations/{result['destination_id']}/test",
                {},
                expected=(200,),
            )
            checks.verify(
                tested["status"] == "SUCCESS",
                f"{result['sink_type']}: credencial destino persiste cifrada tras reinicio",
            )

        for result in results:
            policy_after_restart = api.get(f"/api/v1/delivery/destinations/{result['destination_id']}/target-policy?" + urlencode(
                {"schema_name": "existing_delivery", "table_name": "audit_records"}))
            checks.verify(policy_after_restart["policy_id"] == result["audit_columns"]["policy_id"]
                          and policy_after_restart["audit_columns_required"],
                          result["sink_type"] + ": policy irreversible sobrevive restart")

        audits = api.get("/api/v1/audit-events")
        event_types = {item["event_type"] for item in audits["items"]}
        checks.verify(
            {"DESTINATION_CREATED", "DESTINATION_TESTED", "DELIVERY_RUN_QUEUED",
             "DELIVERY_STARTED", "DELIVERY_COMMITTED", "DELIVERY_FAILED", "DELIVERY_TARGET_AUDIT_ENABLED",
             "DELIVERY_TARGET_AUDIT_COLUMNS_CREATED", "DELIVERY_TARGET_AUDIT_DRIFT"}.issubset(event_types),
            "Auditoría cubre destino, cola, inicio, commit y fallo de Delivery",
        )
        logs = run(["logs", "--no-color", "api", "worker", "delivery-worker"], capture=True)
        database_dump = run(
            ["exec", "-T", "postgres", "pg_dump", "-U", environment["POSTGRES_USER"], "-d", environment["POSTGRES_DB"],
             "--data-only"],
            capture=True,
        )
        assert_no_credentials(logs, credentials, "Una credencial apareció en logs.")
        assert_no_credentials(database_dump, credentials, "Una credencial apareció en metadata SQL.")
        assert_no_credentials(audits, credentials, "Una credencial apareció en auditoría.")
        checks.verify(True, "Secretos ausentes de logs, metadata, auditoría y evidencia")

        playwright = "SKIPPED"
        integral = None
        if not args.skip_playwright:
            pnpm = shutil.which("pnpm")
            if not pnpm:
                raise RuntimeError("pnpm no está disponible para ejecutar Playwright.")
            environment["TV_INTEGRAL_085"] = "true"
            environment["TV_E2E_DELIVERY_DESTINATION_ID"] = next(item["destination_id"] for item in results if item["sink_type"] == "POSTGRESQL")
            local_python = ROOT / "backend/.venv" / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
            environment["TV_E2E_PYTHON"] = str(local_python) if local_python.is_file() else sys.executable
            browser_args = [] if args.full_playwright else ["tests-e2e/delivery.spec.ts", "tests-e2e/catalog-reports.spec.ts"]
            browser_result = run_browser(pnpm, browser_args, root=ROOT, project=project,
                                         environment=environment, evidence=evidence)
            if browser_result.get("skipped", 0) or browser_result.get("unexpected", 0):
                raise RuntimeError("El recorrido integral obligatorio no admite pruebas omitidas o fallidas.")
            candidates = list((ROOT / ".codex-local/browser-results" / project).rglob("catalog-reports-cycle.json"))
            if len(candidates) != 1:
                raise RuntimeError("Falta evidencia única del recorrido integral obligatorio R085.")
            journey = json.loads(candidates[0].read_text(encoding="utf-8"))
            delivered = journey.get("delivery") or {}
            table = delivered.get("table", "")
            if (journey.get("scope") != "R085_INTEGRAL" or not re.fullmatch(r"ui_integral_085_[0-9]+", table)
                    or delivered.get("destination_id") != environment["TV_E2E_DELIVERY_DESTINATION_ID"]
                    or delivered.get("decision") != "COMMITTED" or journey.get("excel", {}).get("rows") != 3):
                raise RuntimeError("El gate integral no acreditó personas, Excel completo y Delivery confirmado.")
            remote_rows = int(target_scalar(run, "POSTGRESQL", f'SELECT COUNT(*) FROM "existing_delivery"."{table}";'))
            ids = target_scalar(run, "POSTGRESQL", f'SELECT string_agg(client_code,\',\' ORDER BY client_code) FROM "existing_delivery"."{table}";')
            indexes = int(target_scalar(run, "POSTGRESQL", f"SELECT COUNT(*) FROM pg_index WHERE indrelid='existing_delivery.{table}'::regclass;"))
            checks.verify(remote_rows == 3 and ids == "001,002,003" and indexes == 1,
                          "R085 integral: SQL independiente confirma todas las filas, ceros iniciales y único índice PK")
            integral = {"status": "PASS", "scope": "R085_INTEGRAL", "browser": journey,
                        "remote_sql": {"rows": remote_rows, "client_codes": ids, "backing_indexes": indexes}}
            (evidence / "integral-085.json").write_text(json.dumps(integral, ensure_ascii=False, indent=2), encoding="utf-8")
            playwright = "PASS"

        result = {
            "status": "PASS",
            "version": application_version,
            "project": project,
            "dataset_version_id": version["id"],
            "destinations": results,
            "checks": checks.completed,
            "playwright": playwright,
            "integral": integral,
            "unknown_reproduction": "NOT_RUN_NONDETERMINISTIC",
            "unknown_reproduction_scope": "PHYSICAL_NONDETERMINISTIC_NETWORK_FAILURE",
            "controlled_ack_loss": {
                "status": "PASS",
                "mechanism": "ADAPTER_ACK_LOSS_AFTER_REAL_SQL_COMMIT",
                "engines": [item["sink_type"] for item in results
                            if item["audit_columns"]["unknown_simulated_ack_loss"]],
                "automatic_replay": False,
            },
            "postgres_metrics_matrix": metrics_matrix,
        }
        (evidence / "result.json").write_text(json.dumps(result, indent=2), encoding="utf-8")
        print(f"OK: {len(checks.completed)} comprobaciones Data Delivery reales.", flush=True)
        return 0
    except (OSError, RuntimeError, KeyError, ValueError, smoke.SmokeFailure) as error:
        print("ERROR: " + redact(str(error), credentials), file=sys.stderr, flush=True)
        result = {
            "status": "FAIL",
            "project": project,
            "destinations": results,
            "checks": checks.completed,
            "error": redact(str(error), credentials),
        }
        (evidence / "result.json").write_text(json.dumps(result, indent=2), encoding="utf-8")
        if started:
            try:
                logs = run(
                    ["logs", "--no-color", "--tail", "180", "api", "worker", "delivery-worker",
                     "acquisition-worker", "scheduler", "events-chaining", "events-notifications"],
                    capture=True,
                )
                (evidence / "application.log").write_text(
                    redact(logs, credentials), encoding="utf-8"
                )
            except (OSError, RuntimeError):
                pass
        return 1
    finally:
        if started:
            try:
                diagnostics = runtime_diagnostics(project, inventory_reader)
                (evidence / "runtime-diagnostics.json").write_text(
                    json.dumps(diagnostics, indent=2), encoding="utf-8"
                )
            except (OSError, RuntimeError, ValueError):
                pass
            run(["down", "-v", "--remove-orphans"])
        assert_main_unchanged(main_before, inventory_reader)


if __name__ == "__main__":
    raise SystemExit(main())
