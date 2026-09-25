"""Certify Data Delivery against disposable real PostgreSQL and SQL Server targets.

Run from the repository root. The runner creates a uniquely named Compose project,
rejects pre-existing resources, and removes only that project's containers, volumes
and network.
Generated credentials are passed through environment/stdin and never written to evidence.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
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
from urllib.parse import urlencode

ROOT = Path(__file__).resolve().parents[2]
PREFIX = "trackvance-delivery-e2e-"
FIXTURES = Path(__file__).with_name("fixtures")

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
) -> dict[str, Any]:
    return {
        "schema_version": 1,
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
        f"{label}: preflight real de solo lectura supera todas las comprobaciones",
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
    return {
        "sink_type": engine,
        "destination_id": destination["id"],
        "destination_version_id": destination["destination_version_id"],
        "runs": runs,
        "failed_attempt": failure,
        "collation_guard": collation_guard,
        "status": "PASS",
    }


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
    credentials = [writer_password, admin_password, internal_password]
    base_url = f"http://127.0.0.1:{port}"
    environment = {
        **os.environ,
        "PYTHONIOENCODING": "utf-8",
        "COMPOSE_PROJECT_NAME": project,
        "WEB_PORT": str(port),
        "TRACKVANCE_WEB_ORIGIN": base_url,
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
    ]
    evidence = args.evidence_dir or ROOT / ".codex-local" / "delivery-e2e" / project
    evidence.mkdir(parents=True, exist_ok=True)

    def command(arguments, **kwargs):
        return execute(arguments, environment=environment, credentials=credentials, **kwargs)

    def run(arguments, **kwargs):
        return command([*compose, *arguments], **kwargs)

    started = False
    checks = smoke.Checks()
    results: list[dict[str, Any]] = []
    try:
        command(["docker", "info", "--format", "{{.OSType}}"])
        existing = preexisting_project_resources(command, project)
        if any(existing.values()):
            raise RuntimeError(f"El proyecto {project} ya tiene recursos; utiliza otro nombre.")
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

        audits = api.get("/api/v1/audit-events")
        event_types = {item["event_type"] for item in audits["items"]}
        checks.verify(
            {"DESTINATION_CREATED", "DESTINATION_TESTED", "DELIVERY_RUN_QUEUED",
             "DELIVERY_STARTED", "DELIVERY_COMMITTED", "DELIVERY_FAILED"}.issubset(event_types),
            "Auditoría cubre destino, cola, inicio, commit y fallo de Delivery",
        )
        logs = run(["logs", "--no-color", "api", "worker", "delivery-worker"], capture=True)
        database_dump = run(
            ["exec", "-T", "postgres", "pg_dump", "-U", "trackvance", "-d", "trackvance",
             "--data-only"],
            capture=True,
        )
        assert_no_credentials(logs, credentials, "Una credencial apareció en logs.")
        assert_no_credentials(database_dump, credentials, "Una credencial apareció en metadata SQL.")
        assert_no_credentials(audits, credentials, "Una credencial apareció en auditoría.")
        checks.verify(True, "Secretos ausentes de logs, metadata, auditoría y evidencia")

        playwright = "SKIPPED"
        if not args.skip_playwright:
            pnpm = shutil.which("pnpm")
            if not pnpm:
                raise RuntimeError("pnpm no está disponible para ejecutar Playwright.")
            browser_args = [] if args.full_playwright else ["tests-e2e/delivery.spec.ts"]
            command([pnpm, "exec", "playwright", "test", *browser_args], cwd=ROOT / "frontend")
            playwright = "PASS"
            for directory in ("test-results", "playwright-report"):
                source = ROOT / "frontend" / directory
                if source.exists():
                    shutil.copytree(source, evidence / directory, dirs_exist_ok=True)

        result = {
            "status": "PASS",
            "project": project,
            "dataset_version_id": version["id"],
            "destinations": results,
            "checks": checks.completed,
            "playwright": playwright,
            "unknown_reproduction": "NOT_RUN_NONDETERMINISTIC",
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
                    ["logs", "--no-color", "--tail", "180", "api", "worker", "delivery-worker"],
                    capture=True,
                )
                (evidence / "application.log").write_text(
                    redact(logs, credentials), encoding="utf-8"
                )
                for directory in ("test-results", "playwright-report"):
                    source = ROOT / "frontend" / directory
                    if source.exists():
                        shutil.copytree(source, evidence / directory, dirs_exist_ok=True)
            except (OSError, RuntimeError):
                pass
        return 1
    finally:
        if started:
            run(["down", "-v", "--remove-orphans"])


if __name__ == "__main__":
    raise SystemExit(main())
