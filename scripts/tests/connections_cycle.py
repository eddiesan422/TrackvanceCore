"""Certify external PostgreSQL/SQL Server readers against disposable real engines.

Run with Python from the repository root. Only the new, isolated project is ever
removed; an existing project/volume is rejected before any mutation. Credentials
are generated per run, passed through environment/stdin, and never written to the
evidence. SQL fixtures are administrative test setup, not a product write feature.
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
import uuid
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlencode

ROOT = Path(__file__).resolve().parents[2]
PREFIX = "trackvance-connections-e2e-"
FIXTURES = Path(__file__).with_name("fixtures")

_spec = importlib.util.spec_from_file_location("connections_smoke", ROOT / "scripts/smoke_test.py")
assert _spec and _spec.loader
smoke = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = smoke
_spec.loader.exec_module(smoke)


def validated_project_name(value: str) -> str:
    if not re.fullmatch(r"trackvance-connections-e2e-[a-z0-9-]+", value):
        raise ValueError(f"El proyecto aislado debe comenzar por {PREFIX!r}.")
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
    *,
    environment: dict[str, str],
    credentials: list[str],
    cwd: Path = ROOT,
    input_text: str | None = None,
    capture: bool = False,
) -> str:
    print("+ " + redact(" ".join(arguments), credentials), flush=True)
    result = subprocess.run(
        arguments, cwd=cwd, env=environment, input=input_text,
        text=True, encoding="utf-8", errors="replace", capture_output=True, check=False,
    )
    if result.returncode:
        raise RuntimeError(redact(result.stdout + result.stderr, credentials)[-8000:])
    if not capture:
        print(redact(result.stdout + result.stderr, credentials), end="", flush=True)
    return result.stdout


def assert_no_credentials(value: object, credentials: list[str], message: str) -> None:
    rendered = value if isinstance(value, str) else json.dumps(value, default=str)
    if any(credential in rendered for credential in credentials):
        raise smoke.SmokeFailure(message)


def certify_read_only_accounts(run, checks):
    """Fixture administration verifies grants; the product has no write endpoint."""
    postgres = run(["exec", "-T", "source-postgres", "psql", "-U", "source_admin", "-d",
                    "trackvance_source", "-v", "ON_ERROR_STOP=1"], input_text="""
DO $test$
BEGIN
    SET LOCAL ROLE tv_reader;
    BEGIN
        INSERT INTO source_data.transactions (record_id) VALUES ('forbidden-write');
        RAISE EXCEPTION 'The source reader unexpectedly has write permission';
    EXCEPTION WHEN insufficient_privilege THEN
        RAISE NOTICE 'SELECT_ONLY_CONFIRMED';
    END;
END $test$;
SELECT COUNT(*) FROM source_data.transactions;
""", capture=True)
    checks.verify("6" in postgres, "POSTGRESQL: cuenta externa sin permiso de escritura")
    sqlserver = run(["exec", "-T", "source-sqlserver", "sh", "-c",
                     ('SQLCMDPASSWORD="$MSSQL_SA_PASSWORD" /opt/mssql-tools18/bin/sqlcmd '
                      '-S localhost -U sa -C -b')], input_text="""
USE trackvance_source;
BEGIN TRANSACTION;
EXECUTE AS USER = 'tv_reader';
BEGIN TRY
    INSERT INTO source_data.transactions (record_id) VALUES ('forbidden-write');
    REVERT;
    ROLLBACK;
    THROW 51000, 'The source reader unexpectedly has write permission', 1;
END TRY
BEGIN CATCH
    DECLARE @error_number int = ERROR_NUMBER();
    REVERT;
    IF @@TRANCOUNT > 0 ROLLBACK;
    IF @error_number <> 229 THROW;
    PRINT 'SELECT_ONLY_CONFIRMED';
END CATCH;
GO
""", capture=True)
    checks.verify("SELECT_ONLY_CONFIRMED" in sqlserver,
                  "SQLSERVER: cuenta externa sin permiso de escritura")


def temporal_fixture(source_type):
    """Explicit precision fixtures, avoiding any cast that rounds input values."""
    columns, expected, logical = [], {}, {}
    for precision in range(7 if source_type == "POSTGRESQL" else 8):
        fraction = "." + "1234567"[:precision] if precision else ""
        naive = f"2026-09-23T10:00:00{fraction}"
        for aware in (False, True):
            name = f"{'aware' if aware else 'naive'}_{precision}"
            native = (
                f"timestamp({precision}) {'with' if aware else 'without'} time zone"
                if source_type == "POSTGRESQL" else
                f"{'datetimeoffset' if aware else 'datetime2'}({precision})"
            )
            value = naive + ("-05:00" if aware else "")
            columns.append((name, native, value))
            expected[name] = (
                (datetime.fromisoformat(value).astimezone(UTC) if aware
                 else datetime.fromisoformat(value)).isoformat()
                if source_type == "POSTGRESQL" else
                # DatasetSource already asks SQL Server for style 127. It
                # normalizes datetimeoffset to UTC without losing digit 7;
                # exact STRING preservation applies to this canonical text.
                f"2026-09-23T15:00:00{fraction}Z" if aware else value
            )
            logical[name] = "TIMESTAMP" if aware and precision <= 6 else "STRING"
    if source_type == "SQLSERVER":
        for name, native, value in (
            ("legacy_datetime", "datetime", "2026-09-23T10:00:00.123"),
            ("legacy_small", "smalldatetime", "2026-09-23T10:00:00"),
        ):
            columns.append((name, native, value))
            expected[name], logical[name] = value, "STRING"
    return columns, expected, logical


def certify_temporal_snapshots(api, checks, source_type, connection_id, admin_password, run):
    """Exercise actual SQL readers, refresh, all snapshot consumers and Delivery."""
    columns, expected, logical = temporal_fixture(source_type)
    postgres = source_type == "POSTGRESQL"
    service = "source-postgres" if postgres else "source-sqlserver"

    def sql(statement):
        if postgres:
            return run(["exec", "-T", service, "psql", "-U", "source_admin", "-d",
                        "trackvance_source", "-v", "ON_ERROR_STOP=1"], input_text=statement)
        return run(["exec", "-T", service, "sh", "-c",
            ('SQLCMDPASSWORD="$MSSQL_SA_PASSWORD" /opt/mssql-tools18/bin/sqlcmd '
             '-S localhost -U sa -C -b')], input_text="USE trackvance_source;\n" + statement + "\nGO\n")

    key_type = "varchar(20)" if postgres else "nvarchar(20)"
    sql(f"CREATE TABLE source_data.temporal_regression (record_key {key_type} NOT NULL, " +
        ", ".join(f"{name} {native} NULL" for name, native, _ in columns) + ");\n" +
        "INSERT INTO source_data.temporal_regression VALUES ('baseline'," +
        ",".join(f"'{value}'" for _, _, value in columns) + "),('nulls'," +
        ",".join("NULL" for _ in columns) + ");\n" +
        "GRANT SELECT ON source_data.temporal_regression TO tv_reader;")
    path = f"/api/v1/connections/{connection_id}"
    registration = api.post(path + "/datasets", {
        "name": f"Temporal regression {source_type}", "schema_name": "source_data",
        "object_name": "temporal_regression",
    })
    dataset, original = registration["dataset"], registration["version"]
    original_profile = api.get(f"/api/v1/dataset-versions/{original['id']}/profile")
    observed = next(row for row in original_profile["sample"] if row["record_key"] == "baseline")
    observed_nulls = next(row for row in original_profile["sample"] if row["record_key"] == "nulls")
    mismatches = {name: {"expected": value, "actual": observed[name]}
                  for name, value in expected.items() if observed[name] != value}
    checks.verify(all(observed[name] == value for name, value in expected.items())
                  and all(observed_nulls[name] is None for name in expected),
                  f"{source_type}: todas las precisiones temporales conservan valores exactos"
                  + (f" ({json.dumps(mismatches)})" if mismatches else ""))
    schema = {item["name"]: item["logical_type"] for item in original["schema"]}
    checks.verify(all(schema[name] == value for name, value in logical.items()),
                  f"{source_type}: tipos temporales con/sin offset y precisión 7 explícitos")
    artifact_url = f"/api/v1/artifacts/{original['canonical_artifact_id']}/download"
    original_bytes = api.request("GET", artifact_url).body
    refreshed = api.post(f"/api/v1/datasets/{dataset['id']}/refresh-source", {})
    checks.verify(refreshed["version"] == 2 and refreshed["schema"] == original["schema"]
                  and api.request("GET", artifact_url).body == original_bytes
                  and api.get(f"/api/v1/dataset-versions/{original['id']}/profile")["sample"]
                  == original_profile["sample"],
                  f"{source_type}: refresh coherente y snapshot temporal histórico íntegro")

    def wait_run(queued):
        return smoke.wait_until("Temporal regression", lambda: api.get(
            f"/api/v1/runs/{queued['id']}"), lambda current: current["status"] in {
                "SUCCESS", "FAILED", "FAILED_PRECONDITION", "UNKNOWN"}, timeout=90, interval=0.3)

    contract = api.post("/api/v1/intake/contracts", {
        "name": f"Temporal Intake {source_type}", "dataset_id": dataset["id"],
        "config": {"required_columns": ["record_key"], "max_error_rate": 0},
    })
    intake = wait_run(api.post("/api/v1/intake/runs", {
        "contract_id": contract["id"], "dataset_version_id": original["id"],
    }, expected=(202,)))
    accepted = api.get(f"/api/v1/dataset-versions/{intake['output_version_id']}/profile")
    checks.verify(intake["status"] == "SUCCESS" and intake["decision"] == "APPROVED"
                  and accepted["sample"] == original_profile["sample"],
                  f"{source_type}: Intake conserva texto temporal sin reinterpretación implícita")
    monitor = api.post("/api/v1/monitors", {
        "name": f"Temporal Sentinel {source_type}", "dataset_id": dataset["id"],
        "config": {"required_columns": list(logical), "null_columns": [],
                   "rules": [{"type": "schema_type", "column": name,
                              "parameters": {"expected_type": value}}
                             for name, value in logical.items()]},
    })
    sentinel = wait_run(api.post(f"/api/v1/monitors/{monitor['id']}/runs", {
        "dataset_version_id": original["id"],
    }, expected=(202,)))
    checks.verify(sentinel["status"] == "SUCCESS" and sentinel["decision"] == "HEALTHY",
                  f"{source_type}: Sentinel monitoriza todas las columnas temporales")
    control = api.post("/api/v1/recon/controls", {
        "name": f"Temporal Recon {source_type}", "dataset_id": dataset["id"],
        "target_dataset_id": dataset["id"], "config": {"key_columns": ["record_key"],
            "comparison_rules": [{"type": "EXACT_COMPARE", "source_column": name,
                                  "target_column": name,
                                  "parameters": {"null_policy": "MATCH_NULLS"}}
                                 for name in logical]},
    })
    recon = wait_run(api.post("/api/v1/recon/runs", {
        "control_id": control["id"], "source_version_id": original["id"],
        "target_version_id": refreshed["id"],
    }, expected=(202,)))
    checks.verify(recon["status"] == "SUCCESS" and recon["metrics"]["matched"] == 2,
                  f"{source_type}: Recon exacto conserva temporales y nulls entre snapshots")
    destination = api.post("/api/v1/delivery/destinations", {
        "name": f"Temporal roundtrip {source_type}", "sink_type": source_type,
        "host": service, "port": 5432 if postgres else 1433,
        "database": "trackvance_source", "username": "source_admin" if postgres else "sa",
        "password": admin_password,
        "options": {"sslmode": "disable"} if postgres else {"encryption": "off"},
    })
    mapping = [{"source_name": name, "target_name": name, "target_type": value,
                "ordinal": index, "nullable": True}
               for index, (name, value) in enumerate(schema.items())]
    draft = {"dataset_version_id": original["id"], "destination_id": destination["id"],
             "destination_version_id": destination["destination_version_id"],
             "target": {"mode": "CREATE_TABLE", "schema_name": "source_data",
                        "table_name": "temporal_delivered", "create_schema": False},
             "columns": mapping, "write_strategy": "CREATE_AND_LOAD", "upsert_keys": []}
    for name, logical_type in logical.items():
        if logical_type == "STRING":
            invalid = {**draft, "columns": [
                {**item, "target_type": "TIMESTAMP"} if item["source_name"] == name else item
                for item in mapping]}
            rejected = api.request("POST", "/api/v1/delivery/preflight", invalid,
                                   expected=(412,)).json()
            checks.verify("SOURCE_TYPE_PRESERVATION" in json.dumps(rejected),
                          f"{source_type}: Delivery rechaza STRING temporal {name} → TIMESTAMP")
    configuration = api.post("/api/v1/delivery/configurations", {
        **draft, "name": f"Temporal delivery {source_type}",
    })
    delivered = wait_run(api.post("/api/v1/delivery/runs", {
        "configuration_id": configuration["id"], "dataset_version_id": original["id"],
    }, expected=(202,)))
    checks.verify(delivered["status"] == "SUCCESS" and delivered["decision"] == "COMMITTED",
                  f"{source_type}: Delivery confirma snapshot temporal sin convertir familias")
    sql("GRANT SELECT ON source_data.temporal_delivered TO tv_reader;")
    roundtrip = api.post(path + "/datasets", {"name": f"Temporal delivered {source_type}",
        "schema_name": "source_data", "object_name": "temporal_delivered"})["version"]
    output = api.get(f"/api/v1/dataset-versions/{roundtrip['id']}/profile")
    row = next(item for item in output["sample"] if item["record_key"] == "baseline")
    null_row = next(item for item in output["sample"] if item["record_key"] == "nulls")
    checks.verify(all(row[name] == expected[name] if logical[name] == "STRING" else
        datetime.fromisoformat(row[name]) == datetime.fromisoformat(expected[name])
        for name in logical) and all(null_row[name] is None for name in logical),
        f"{source_type}: roundtrip real preserva STRING exacto e instante/microsegundos TIMESTAMP")
    return {"engine": source_type, "status": "PASS", "temporal_columns": len(logical),
            "source_version_id": original["id"], "refreshed_version_id": refreshed["id"],
            "delivery_run_id": delivered["id"]}


def certify_source(api, checks, source_type: str, password: str, credentials: list[str], run):
    service = "source-postgres" if source_type == "POSTGRESQL" else "source-sqlserver"
    config = {
        "name": f"Certificación {source_type}", "source_type": source_type,
        "host": service, "port": 5432 if source_type == "POSTGRESQL" else 1433,
        "database": "trackvance_source", "username": "tv_reader", "password": password,
        "options": {"connect_timeout": 3, "query_timeout": 10,
                    **({"sslmode": "disable"} if source_type == "POSTGRESQL"
                       else {"encryption": "off"})},
    }
    tested = api.post("/api/v1/connections/test", config, expected=(200,))
    checks.verify(tested["status"] == "SUCCESS", f"{source_type}: conexión real correcta")
    assert_no_credentials(tested, credentials, "Secreto filtrado en la prueba de conexión")
    for change, label in [
        ({"password": "Incorrect-" + password}, "credenciales incorrectas"),
        ({"port": 1}, "puerto incorrecto"),
        ({"host": "nonexistent-source.invalid"}, "host incorrecto"),
    ]:
        failure = api.request("POST", "/api/v1/connections/test", {**config, **change},
                              expected=(422,)).json()
        checks.verify(bool(failure.get("error", {}).get("code")),
                      f"{source_type}: {label} produce error controlado")
        assert_no_credentials(failure, credentials, "Secreto filtrado en error de conexión")

    connection = api.post("/api/v1/connections", config)
    connection_id = connection["id"]
    path = f"/api/v1/connections/{connection_id}"
    checks.verify(connection["version"] == 1 and connection["enabled"],
                  f"{source_type}: configuración de conexión versionada")
    assert_no_credentials(connection, credentials, "Secreto filtrado en conexión serializada")
    schemas = api.get(path + "/schemas")["items"]
    checks.verify("source_data" in schemas, f"{source_type}: descubrimiento de schemas")
    objects = api.get(path + "/objects?schema_name=source_data")["items"]
    found = {obj["name"]: obj["kind"] for obj in objects}
    checks.verify(found.get("transactions") == "TABLE"
                  and found.get("transactions_view") == "VIEW",
                  f"{source_type}: tablas y vistas accesibles")
    query = urlencode({"schema_name": "source_data", "object_name": "transactions", "limit": 3})
    preview = api.get(path + "/preview?" + query)
    columns = {column["name"]: column for column in preview["columns"]}
    checks.verify(set(columns) == {"record_id", "customer_name", "amount", "quantity",
                                  "happened_on", "updated_at", "is_active", "optional_note"},
                  f"{source_type}: metadata de columnas completa")
    checks.verify(columns["happened_on"]["logical_type"] == "DATE"
                  and columns["updated_at"]["logical_type"] == "STRING"
                  and columns["quantity"]["numeric"] and columns["amount"]["numeric"],
                  f"{source_type}: temporales sin zona preservados como texto exacto")
    checks.verify(len(preview["rows"]) == 3 and preview["sampled_rows"] == 3,
                  f"{source_type}: preview limitado a la muestra solicitada")
    for object_name in ["transactions_view", "private_records"]:
        query = urlencode({"schema_name": "source_data", "object_name": object_name, "limit": 10})
        if object_name == "private_records":
            error = api.request("GET", path + "/preview?" + query, expected=(422,)).json()
            checks.verify(bool(error.get("error", {}).get("code")),
                          f"{source_type}: permisos insuficientes controlados")
            assert_no_credentials(error, credentials, "Secreto filtrado en error de permisos")
        else:
            view = api.get(path + "/preview?" + query)
            checks.verify(view["sampled_rows"] == 6, f"{source_type}: lectura real de vista")
            checks.verify(any(row["record_id"] == "001234567" for row in view["rows"])
                          and any(row["customer_name"] == "José Álvarez" for row in view["rows"])
                          and any(row["optional_note"] == "Unicode 東京" for row in view["rows"])
                          and any(row["amount"] is None for row in view["rows"]),
                          f"{source_type}: preserva identificadores, Unicode y nulos")

    registered = api.post(path + "/datasets", {
        "name": f"Certificación dataset {source_type}", "domain": "Pruebas",
        "description": "Datos efímeros del ciclo aislado de conectores",
        "schema_name": "source_data", "object_name": "transactions",
    })
    dataset, version = registered["dataset"], registered["version"]
    dataset_id = dataset["id"]
    source = version["ingestion_metadata"]["source"]
    checks.verify(version["source_type"] == source_type and version["row_count"] == 6
                  and not version["original_artifact_id"] and bool(version["canonical_artifact_id"]),
                  f"{source_type}: snapshot canónico de seis registros en ArtifactStore")
    checks.verify(source["connection_id"] == connection_id
                  and source["connection_version_id"] == connection["connection_version_id"]
                  and source["schema_name"] == "source_data"
                  and source["object_name"] == "transactions"
                  and source["config_hash"] == connection["config_hash"],
                  f"{source_type}: linaje hacia conexión/configuración/objeto")
    artifact_path = f"/api/v1/artifacts/{version['canonical_artifact_id']}/download"
    original_bytes = api.request("GET", artifact_path).body
    checks.verify(original_bytes[:4] == b"PAR1", f"{source_type}: artifact materializado es Parquet")
    contract = api.post("/api/v1/intake/contracts", {
        "name": f"Contrato {source_type}", "dataset_id": dataset_id,
        "config": {"required_columns": ["record_id"], "unique_columns": ["record_id"],
                   "positive_columns": ["amount"], "max_error_rate": 0},
    })
    queued = api.post("/api/v1/intake/runs", {
        "contract_id": contract["id"], "dataset_version_id": version["id"],
    }, expected=(202,))
    completed = smoke.wait_until(
        f"Intake {source_type}", lambda: api.get(f"/api/v1/runs/{queued['id']}"),
        lambda current: current["status"] in {"SUCCESS", "FAILED"}, timeout=60, interval=0.5,
    )
    checks.verify(completed["status"] == "SUCCESS" and completed["decision"] == "REJECTED"
                  and completed["metrics"]["total_rows"] == 6,
                  f"{source_type}: Intake sobre snapshot detecta fallos de negocio")
    evidence = api.get(f"/api/v1/runs/{queued['id']}/evidence")
    checks.verify(evidence["inputs"][0]["ingestion_metadata"]["source"] == source,
                  f"{source_type}: evidencia del run conserva el linaje externo")
    assert_no_credentials(evidence, credentials, "Secreto filtrado en manifest")
    smoke.check_export(api, checks, completed)
    refreshed = api.post(f"/api/v1/datasets/{dataset_id}/refresh-source", {})
    checks.verify(refreshed["version"] == 2 and refreshed["id"] != version["id"],
                  f"{source_type}: reconexión materializa nueva DatasetVersion")
    checks.verify(api.request("GET", artifact_path).body == original_bytes,
                  f"{source_type}: snapshot original permanece inmutable")
    edited = api.request("PATCH", path, {"version": connection["version"],
                                        "name": config["name"] + " revisada"}).json()
    checks.verify(edited["version"] == 2, f"{source_type}: edición crea configuración nueva")
    next_version = api.post(f"/api/v1/datasets/{dataset_id}/refresh-source", {})
    checks.verify(next_version["ingestion_metadata"]["source"]["connection_version"] == 2,
                  f"{source_type}: nuevo snapshot usa nueva versión de conexión")

    run(["stop", service])
    try:
        outage = api.request("POST", path + "/test", {}, expected=(422,)).json()
        assert_no_credentials(outage, credentials, "Secreto filtrado durante caída temporal")
        api.request("POST", f"/api/v1/datasets/{dataset_id}/refresh-source", {}, expected=(422,))
        unchanged = api.get(f"/api/v1/datasets/{dataset_id}")
        checks.verify(len(unchanged["versions"]) == 3
                      and api.request("GET", artifact_path).body == original_bytes,
                      f"{source_type}: pérdida temporal conserva versiones y evidencia")
    finally:
        run(["up", "-d", "--wait", "--wait-timeout", "180", service])
    reconnected = api.post(path + "/test", {}, expected=(200,))
    checks.verify(reconnected["status"] == "SUCCESS", f"{source_type}: recupera conexión tras caída")
    disabled = api.request("PATCH", path, {"version": edited["version"], "enabled": False}).json()
    denied = api.request("POST", f"/api/v1/datasets/{dataset_id}/refresh-source", {},
                         expected=(409,)).json()
    checks.verify(denied["error"]["code"] == "CONNECTION_DISABLED",
                  f"{source_type}: conexión deshabilitada bloquea nueva lectura")
    api.request("PATCH", path, {"version": disabled["version"], "enabled": True})
    return {"source_type": source_type, "dataset_id": dataset_id,
            "connection_id": connection_id, "run_id": completed["id"],
            "row_count": 6, "status": "PASS"}


def main() -> int:
    # Docker/browser output includes Unicode even when Windows redirects stdout
    # through a legacy console encoding. Keep the evidence portable and intact.
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project", default=f"{PREFIX}{os.getpid()}-{uuid.uuid4().hex[:6]}")
    parser.add_argument("--port", type=int, default=0)
    parser.add_argument("--skip-build", action="store_true")
    parser.add_argument("--skip-playwright", action="store_true")
    parser.add_argument("--full-playwright", action="store_true",
                        help="Ejecutar también todos los flujos de regresión del navegador.")
    parser.add_argument("--skip-regression", action="store_true",
                        help="Omitir smoke general; no omite certificación de conectores.")
    parser.add_argument("--evidence-dir", type=Path)
    args = parser.parse_args()
    try:
        project = validated_project_name(args.project)
    except ValueError as error:
        parser.error(str(error))
    port = args.port or available_port()
    if not 1 <= port <= 65535:
        parser.error("--port debe estar entre 1 y 65535")
    password = "TvReader-" + secrets.token_hex(18)
    admin_password = "TvAdmin-" + secrets.token_hex(18)
    internal_password = "TvInternal-" + secrets.token_hex(18)
    credentials = [password, admin_password, internal_password]
    base_url = f"http://127.0.0.1:{port}"
    environment = {**os.environ,
                   "PYTHONIOENCODING": "utf-8",
                   "COMPOSE_PROJECT_NAME": project, "WEB_PORT": str(port),
                   "TRACKVANCE_WEB_ORIGIN": base_url, "TV_E2E_URL": base_url,
                   "POSTGRES_USER": "trackvance", "POSTGRES_DB": "trackvance",
                   "POSTGRES_PASSWORD": internal_password,
                   "SOURCE_POSTGRES_PASSWORD": admin_password,
                   "SOURCE_MSSQL_SA_PASSWORD": admin_password,
                   "DEMO_ACCESS_ENABLED": "true", "DEMO_SEED_ENABLED": "false",
                   "TV_CONNECTIONS_E2E": "true", "TV_CONNECTIONS_PASSWORD": password}
    compose = ["docker", "compose", "-p", project, "-f", "compose.yml",
               "-f", "deploy/docker/compose.connections-test.yml"]
    evidence = args.evidence_dir or ROOT / ".codex-local" / "connections-e2e" / project
    evidence.mkdir(parents=True, exist_ok=True)

    def command(arguments, **kwargs):
        return execute(arguments, environment=environment, credentials=credentials, **kwargs)

    def run(arguments, **kwargs):
        return command([*compose, *arguments], **kwargs)

    started = False
    checks = smoke.Checks()
    try:
        command(["docker", "info", "--format", "{{.OSType}}"])
        existing = command(["docker", "ps", "-aq", "--filter",
                            f"label=com.docker.compose.project={project}"], capture=True).strip()
        volumes = command(["docker", "volume", "ls", "-q", "--filter",
                           f"label=com.docker.compose.project={project}"], capture=True).strip()
        if existing or volumes:
            raise RuntimeError(f"El proyecto {project} ya tiene recursos; utiliza otro nombre.")
        started = True
        run(["up", "-d", "--wait", "--wait-timeout", "300", "source-postgres", "source-sqlserver"])
        postgres_sql = (FIXTURES / "connections-postgresql.sql").read_text(encoding="utf-8")
        sqlserver_sql = (FIXTURES / "connections-sqlserver.sql").read_text(encoding="utf-8")
        run(["exec", "-T", "source-postgres", "psql", "-U", "source_admin", "-d",
             "trackvance_source", "-v", "ON_ERROR_STOP=1"],
            input_text=postgres_sql.replace("__READER_PASSWORD__", password))
        run(["exec", "-T", "source-sqlserver", "sh", "-c",
             ('SQLCMDPASSWORD="$MSSQL_SA_PASSWORD" /opt/mssql-tools18/bin/sqlcmd '
              '-S localhost -U sa -C -b -f 65001')],
            input_text=sqlserver_sql.replace("__READER_PASSWORD__", password))
        certify_read_only_accounts(run, checks)
        up = ["up", "-d", "--wait", "--wait-timeout", "300"]
        if not args.skip_build:
            up.append("--build")
        run(up)
        run(["exec", "-T", "api", "alembic", "check"])
        migration_result = run(["exec", "-T", "api", "python", "-"],
                               input_text=(ROOT / "scripts/check_postgres_migrations.py").read_text(encoding="utf-8"),
                               capture=True)
        checks.verify(json.loads(migration_result)["roundtrip"] == "PASS",
                      "Migraciones PostgreSQL: ida/vuelta, historial y paridad de modelos")
        api = smoke.Api(base_url, timeout=60)
        api.request("GET", "/api/v1/connections", expected=(401,))
        auth = api.post("/api/v1/auth/demo", {}, expected=(200,))
        api.csrf = auth["csrf_token"]
        results = [certify_source(api, checks, source, password, credentials, run)
                   for source in ["POSTGRESQL", "SQLSERVER"]]
        temporal_results = [certify_temporal_snapshots(api, checks, item["source_type"],
            item["connection_id"], admin_password, run) for item in results]
        if not args.skip_regression:
            command([sys.executable, "scripts/smoke_test.py", "--base-url", base_url])
        if not args.skip_playwright:
            pnpm = shutil.which("pnpm")
            if not pnpm:
                raise RuntimeError("pnpm no está disponible para ejecutar Playwright.")
            browser_args = [] if args.full_playwright else ["tests-e2e/connections.spec.ts"]
            command([pnpm, "exec", "playwright", "test", *browser_args],
                    cwd=ROOT / "frontend")
            for directory in ("test-results", "playwright-report"):
                source = ROOT / "frontend" / directory
                if source.exists():
                    shutil.copytree(source, evidence / directory, dirs_exist_ok=True)
        run(["restart", "postgres", "api", "worker", "delivery-worker", "web"])
        run(["up", "-d", "--wait", "--wait-timeout", "180"])
        for result in results:
            tested = api.post(f"/api/v1/connections/{result['connection_id']}/test", {}, expected=(200,))
            preserved = api.get(f"/api/v1/runs/{result['run_id']}")
            checks.verify(tested["status"] == "SUCCESS" and preserved["status"] == "SUCCESS",
                          f"{result['source_type']}: conserva credenciales cifradas y runs tras reinicio")
        logs = run(["logs", "--no-color", "api", "worker", "delivery-worker"], capture=True)
        assert_no_credentials(logs, credentials, "Secreto en logs de la aplicación")
        database_dump = run(["exec", "-T", "postgres", "pg_dump", "-U", "trackvance",
                             "-d", "trackvance", "--data-only"], capture=True)
        assert_no_credentials(database_dump, credentials, "Credencial en texto plano en metadata")
        audits = api.get("/api/v1/audit-events")
        assert_no_credentials(audits, credentials, "Secreto filtrado en auditoría")
        checks.verify(True, "Contraseñas ausentes de logs, auditoría y metadata interna")
        result = {"status": "PASS", "project": project, "sources": results,
                  "temporal_regressions": temporal_results,
                  "checks": checks.completed, "playwright": "SKIPPED" if args.skip_playwright else "PASS",
                  "regression_smoke": "SKIPPED" if args.skip_regression else "PASS"}
        (evidence / "result.json").write_text(json.dumps(result, indent=2), encoding="utf-8")
        print(f"OK: {len(checks.completed)} comprobaciones de conectores reales.", flush=True)
        return 0
    except (OSError, RuntimeError, KeyError, smoke.SmokeFailure) as error:
        print("ERROR: " + redact(str(error), credentials), file=sys.stderr, flush=True)
        (evidence / "result.json").write_text(json.dumps({"status": "FAIL", "project": project,
                                                        "checks": checks.completed,
                                                        "error": redact(str(error), credentials)},
                                                       indent=2), encoding="utf-8")
        if started:
            try:
                logs = run(
                    ["logs", "--no-color", "--tail", "150", "api", "worker",
                     "delivery-worker"],
                    capture=True,
                )
                (evidence / "application.log").write_text(redact(logs, credentials), encoding="utf-8")
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
