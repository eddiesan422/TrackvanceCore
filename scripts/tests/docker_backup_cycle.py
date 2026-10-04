#!/usr/bin/env python3
"""Certify recovery with real PostgreSQL source/destination and disposable projects.

Trackvance source is destroyed before restoration. Its independent PostgreSQL
source remains alive; restored source and destination credentials must work again.
The backup also includes an assigned exception with an attachment and a completed
scheduled Sentinel occurrence; the paused schedule must remain paused on restore.
--keep preserves destination and external DB; source Trackvance is still destroyed.
"""

from __future__ import annotations

import argparse
import hashlib
import http.cookiejar
import json
import os
import re
import secrets
import socket
import subprocess
import sys
import tarfile
import tempfile
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier
from typing import Any, BinaryIO, ClassVar
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))
import docker_state

PROJECT_PATTERN = re.compile(r"(?:trackvance-recovery-(?:src|dst)-[a-z0-9-]+|trackvance-v070-test-[a-z0-9-]+-[a-f0-9]{12})")
DATABASE_PROJECT_PATTERN = re.compile(r"trackvance-recovery-db-[a-z0-9-]+")
DATABASE_SERVICE = "recovery-postgres"


def validated_project_name(value: str, *, database: bool = False) -> str:
    pattern = DATABASE_PROJECT_PATTERN if database else PROJECT_PATTERN
    if not pattern.fullmatch(value):
        raise ValueError("El proyecto debe tener el prefijo trackvance-recovery y ser aislado.")
    docker_state.validate_project(value)
    return value


def available_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


def ensure(condition: bool, message: str) -> None:
    if not condition:
        raise RuntimeError(message)


def assert_no_secrets(contents: str | bytes, credentials: tuple[str, ...]) -> None:
    text = contents.decode("utf-8", errors="replace") if isinstance(contents, bytes) else contents
    ensure(not any(value and value in text for value in credentials),
           "Se detectó una credencial en la salida; el contenido fue suprimido.")


def assert_stream_no_secrets(stream: BinaryIO, credentials: tuple[str, ...]) -> None:
    """Inspect large artifacts with bounded memory, including block boundaries."""
    needles = tuple(value.encode("utf-8") for value in credentials if value)
    overlap = max((len(value) for value in needles), default=1) - 1
    tail = b""
    while chunk := stream.read(1024 * 1024):
        contents = tail + chunk
        ensure(not any(value in contents for value in needles),
               "Se detectó una credencial en la salida; el contenido fue suprimido.")
        tail = contents[-overlap:] if overlap else b""


def scan_backup_plaintext(backup: Path, application: list[str], environment: dict[str, str],
                          credentials: tuple[str, ...]) -> dict[str, Any]:
    """Scan private streams with bounded memory, deleting transient decoded SQL."""
    with (backup / "postgres.dump").open('rb') as raw_dump, \
            tempfile.TemporaryFile(dir=backup.parent) as decoded, \
            tempfile.TemporaryFile(dir=backup.parent) as errors:
        result = subprocess.run(
            [*application, "exec", "-T", "postgres", "pg_restore", "--file=-"],
            cwd=ROOT, env=environment, stdin=raw_dump, stdout=decoded, stderr=errors,
            timeout=300, check=False,
        )
        decoded.seek(0)
        errors.seek(0)
        assert_stream_no_secrets(decoded, credentials)
        assert_stream_no_secrets(errors, credentials)
        ensure(result.returncode == 0, "No se pudo inspeccionar el dump privado del backup.")
    files = 0
    for path in backup.rglob("*"):
        if not path.is_file():
            continue
        with path.open("rb") as raw:
            assert_stream_no_secrets(raw, credentials)
        if path.name.endswith(".tar.gz"):
            with tarfile.open(path, "r:gz") as archive:
                for entry in archive:
                    if entry.isfile():
                        member = archive.extractfile(entry)
                        ensure(member is not None, "Entrada de backup no legible.")
                        with member:
                            assert_stream_no_secrets(member, credentials)
                        files += 1
    return {"status": "PASS", "postgres_dump_decompressed": "PASS",
            "archive_files_scanned": files, "known_credentials_scanned": len(credentials),
            "raw_contents_published": False}


class RecoveryCommandError(RuntimeError):
    """Retain only an exit code and an allowlisted diagnostic, never child output."""

    categories: ClassVar[frozenset[str]] = frozenset({"CONNECTION", "SQL", "UNKNOWN"})

    def __init__(self, exit_code: int, category: str) -> None:
        if type(exit_code) is not int or category not in self.categories:
            raise ValueError("Diagnóstico de subproceso inválido.")
        super().__init__(f"Falló el subproceso (exit {exit_code}; {category}); salida suprimida.")
        self.exit_code = exit_code
        self.category = category


def command_failure_category(arguments: list[str], stderr: str) -> str:
    """Classify psql output in memory; return no text originating from the child."""
    if "psql" not in arguments:
        return "UNKNOWN"
    if re.search(
        r"psql:.*(?:connection to server|could not connect to server)|"
        r"server closed the connection unexpectedly|"
        r"terminating connection due to administrator command|"
        r"database system is (?:starting up|shutting down)",
        stderr, re.IGNORECASE,
    ):
        return "CONNECTION"
    if re.search(r"^(?:psql:[^\n]*:\s*)?ERROR:\s", stderr, re.MULTILINE):
        return "SQL"
    return "UNKNOWN"


def execute(arguments: list[str], environment: dict[str, str], *, timeout: int = 1800,
            input_text: str | None = None, credentials: tuple[str, ...] = ()) -> str:
    # Credentials may enter the child only via environment or stdin, never argv.
    assert_no_secrets(" ".join(arguments), credentials)
    print("+ " + " ".join(arguments), flush=True)
    result = subprocess.run(arguments, cwd=ROOT, env=environment, capture_output=True,
                            text=True, encoding="utf-8", errors="replace",
                            timeout=timeout, check=False, input=input_text)
    if result.returncode:
        raise RecoveryCommandError(
            result.returncode, command_failure_category(arguments, result.stderr))
    assert_no_secrets(result.stdout + result.stderr, credentials)
    return result.stdout


class RecoveryApi:
    """Standard-library authenticated client; never renders response/error bodies."""

    def __init__(self, port: int, credentials: tuple[str, ...]) -> None:
        self.port = port
        self.base_url = f"http://127.0.0.1:{port}/api/v1"
        self.credentials = credentials
        self.csrf = ""
        self.opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))
        auth = self.json("POST", "/auth/demo", {})
        self.csrf = auth["csrf_token"]
        self.user_id = auth["user"]["id"]
        ensure(bool(self.csrf), "La sesión local no tiene protección CSRF.")

    def request(self, method: str, path: str, payload: Any = None, *, expected=200,
                raw: bytes | None = None, content_type: str | None = None) -> bytes:
        headers = {"Accept": "application/json"}
        body = raw
        if content_type:
            headers["Content-Type"] = content_type
        if payload is not None:
            body = json.dumps(payload).encode("utf-8")
            headers["Content-Type"] = "application/json"
        if self.csrf and method != "GET":
            headers["X-CSRF-Token"] = self.csrf
        request = urllib.request.Request(self.base_url + path, data=body,
                                         headers=headers, method=method)
        try:
            with self.opener.open(request, timeout=90) as response:
                status, content = response.status, response.read()
        except urllib.error.HTTPError as error:
            status, content = error.code, error.read()
        except (urllib.error.URLError, TimeoutError) as error:
            raise RuntimeError("La API del entorno aislado no respondió.") from error
        assert_no_secrets(content, self.credentials)
        ensure(status == expected, f"{method} {path}: HTTP {status}; respuesta suprimida.")
        return content

    def json(self, method: str, path: str, payload: Any = None, *, expected=200) -> Any:
        return json.loads(self.request(method, path, payload, expected=expected))

    def upload_attachment(self, case: dict[str, Any], contents: bytes) -> dict[str, Any]:
        boundary = "trackvance-recovery-" + uuid4().hex
        parts = []
        for name, value in {"version": str(case["version"]),
                            "description": "Comprobante controlado para recuperación"}.items():
            parts.append((f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"'
                          f"\r\n\r\n{value}\r\n").encode())
        parts.extend([
            (f'--{boundary}\r\nContent-Disposition: form-data; name="file"; '
             'filename="recovery-evidence.txt"\r\nContent-Type: text/plain\r\n\r\n').encode(),
            contents, f"\r\n--{boundary}--\r\n".encode(),
        ])
        return json.loads(self.request("POST", f"/exceptions/{case['id']}/attachments",
            raw=b"".join(parts), content_type=f"multipart/form-data; boundary={boundary}", expected=201))


def assert_fresh(project: str) -> None:
    state = docker_state.inventory(project)
    ensure(not any(state[key] for key in ("containers", "volumes", "networks")),
           f"El proyecto aislado {project} ya tiene recursos.")


def cleanup(project: str, evidence: Path) -> None:
    validated_project_name(project, database=bool(DATABASE_PROJECT_PATTERN.fullmatch(project)))
    state = docker_state.inventory(project)
    if not any(state[key] for key in ("containers", "volumes", "networks")):
        return
    plan_path = evidence / f"reset-{project}-{uuid4().hex[:8]}.json"
    plan = docker_state.create_reset_plan(project, plan_path, ttl_minutes=60)
    docker_state.reset(plan_path, f"RESET:{project}:{plan['plan_sha256'][:12]}")


def fixture_compose() -> dict[str, Any]:
    return {
        "services": {DATABASE_SERVICE: {
            "image": "postgres:16-alpine", "restart": "no",
            "environment": {
                "POSTGRES_USER": "recovery_admin", "POSTGRES_DB": "recovery_source",
                "POSTGRES_PASSWORD": "${RECOVERY_SOURCE_PASSWORD:?Required disposable credential}",
            },
            "volumes": ["source_data:/var/lib/postgresql/data"],
            "healthcheck": {
                # initdb's temporary server accepts sockets but never TCP.
                "test": ["CMD-SHELL", "pg_isready -h 127.0.0.1 -U recovery_admin -d recovery_source"],
                "interval": "2s", "timeout": "3s", "retries": 30,
            },
        }}, "volumes": {"source_data": {}},
    }


def initialize_source(compose: list[str], environment: dict[str, str],
                      credentials: tuple[str, ...], reader_password: str,
                      delivery_password: str | None = None) -> None:
    # Hexadecimal generated password cannot escape this SQL literal. SQL enters
    # psql via stdin; it is never written to evidence or shell command arguments.
    delivery_password = delivery_password or reader_password
    ensure(bool(re.fullmatch(r"[a-f0-9]{48}", reader_password)), "Credencial de fixture inválida.")
    ensure(bool(re.fullmatch(r"[a-f0-9]{48}", delivery_password)),
           "Credencial destino de fixture inválida.")
    sql = f"""
CREATE ROLE tv_recovery_reader LOGIN PASSWORD '{reader_password}';
CREATE ROLE tv_recovery_writer LOGIN PASSWORD '{delivery_password}';
CREATE SCHEMA recovery_data;
CREATE TABLE recovery_data.transactions (
    record_id varchar(20) PRIMARY KEY, customer_id varchar(20), amount numeric(16,2),
    booked_on date, recorded_at timestamptz, active boolean
);
INSERT INTO recovery_data.transactions VALUES
('001','A',10.50,'2026-01-01','2026-01-01T12:00:00Z',true),
('002','A',-2.00,'2026-01-02','2026-01-02T12:00:00Z',true),
('003',NULL,30.25,'2026-01-03','2026-01-03T12:00:00Z',false),
('004','B',40.00,'2026-01-04',NULL,true);
CREATE VIEW recovery_data.transaction_view AS SELECT * FROM recovery_data.transactions;
REVOKE ALL ON DATABASE recovery_source FROM PUBLIC;
GRANT CONNECT ON DATABASE recovery_source TO tv_recovery_reader;
GRANT USAGE ON SCHEMA recovery_data TO tv_recovery_reader;
GRANT SELECT ON ALL TABLES IN SCHEMA recovery_data TO tv_recovery_reader;
CREATE SCHEMA recovery_delivery AUTHORIZATION tv_recovery_writer;
CREATE TABLE recovery_delivery.records (
    record_id varchar(20) PRIMARY KEY, customer_id varchar(20), amount numeric(16,2) NOT NULL,
    booked_on date NOT NULL, recorded_at timestamptz, active boolean NOT NULL
);
ALTER TABLE recovery_delivery.records OWNER TO tv_recovery_writer;
CREATE TABLE recovery_delivery.operational_records
(LIKE recovery_delivery.records INCLUDING ALL);
ALTER TABLE recovery_delivery.operational_records OWNER TO tv_recovery_writer;
GRANT CONNECT ON DATABASE recovery_source TO tv_recovery_writer;
GRANT USAGE ON SCHEMA recovery_delivery TO tv_recovery_writer;
GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA recovery_delivery
TO tv_recovery_writer;
"""
    execute([*compose, "exec", "-T", DATABASE_SERVICE, "psql", "-U", "recovery_admin",
             "-d", "recovery_source", "-v", "ON_ERROR_STOP=1"], environment,
            input_text=sql, credentials=credentials)


def attach_external_network(project: str, database: str, environment: dict[str, str]) -> None:
    application, fixture = docker_state.inventory(project), docker_state.inventory(database)
    clients = [item for item in application["containers"]
               if item["service"] in {"api", "delivery-worker"}]
    ensure(len(clients) == 2 and all(item["running"] for item in clients),
           "API o delivery-worker aislado no disponible.")
    ensure(len(fixture["networks"]) == 1, "La fuente externa necesita una única red aislada.")
    ensure(len(fixture["containers"]) == 1 and fixture["containers"][0]["running"],
           "La fuente PostgreSQL externa no está disponible.")
    for client in clients:
        execute(["docker", "network", "connect", fixture["networks"][0]["id"],
                 client["id"]], environment)


def wait_run(api: RecoveryApi, contract_id: str, version_id: str) -> dict[str, Any]:
    queued = api.json("POST", "/intake/runs", {
        "contract_id": contract_id, "dataset_version_id": version_id,
    }, expected=202)
    return wait_existing_run(api, queued["run_id"])


def wait_existing_run(api: RecoveryApi, run_id: str) -> dict[str, Any]:
    deadline = time.monotonic() + 120
    while time.monotonic() < deadline:
        run = api.json("GET", f"/runs/{run_id}")
        if run["status"] in {"SUCCESS", "FAILED", "UNKNOWN", "FAILED_PRECONDITION", "CANCELLED"}:
            ensure(run["status"] == "SUCCESS", "La ejecución restaurada tuvo un error técnico.")
            return run
        time.sleep(0.5)
    raise RuntimeError("La ejecución aislada no terminó a tiempo.")


class DeliveryEvidenceError(RuntimeError):
    """Only fixed, non-sensitive readiness codes may enter recovery evidence."""

    messages: ClassVar[dict[str, str]] = {
        "DELIVERY_EVIDENCE_NOT_COMMITTED": "La evidencia requiere SUCCESS / COMMITTED.",
        "DELIVERY_EVIDENCE_PENDING_REPAIR": "La entrega requiere reparación explícita de evidencia.",
        "DELIVERY_EVIDENCE_TIMEOUT": "La evidencia local no se publicó a tiempo.",
    }

    def __init__(self, code: str) -> None:
        super().__init__(self.messages[code])
        self.code = code


def wait_for_delivery_evidence(
    api: RecoveryApi, run_id: str, *, timeout: float = 120,
) -> dict[str, Any]:
    """Wait for local publication after COMMITTED; never replay or repair."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        run = api.json("GET", f"/runs/{run_id}")
        if run["status"] != "SUCCESS" or run["decision"] != "COMMITTED":
            raise DeliveryEvidenceError("DELIVERY_EVIDENCE_NOT_COMMITTED")
        metrics = run.get("metrics") or {}
        if metrics.get("evidence_status") == "PENDING_REPAIR":
            raise DeliveryEvidenceError("DELIVERY_EVIDENCE_PENDING_REPAIR")
        # SUCCESS / COMMITTED has its own durable transaction. The worker adds
        # this reference atomically with BOTH completed receipt and manifest in
        # the later publication transaction; the Run DTO has no evidence_path.
        if metrics.get("receipt_artifact_id"):
            return run
        time.sleep(min(0.5, max(0, deadline - time.monotonic())))
    raise DeliveryEvidenceError("DELIVERY_EVIDENCE_TIMEOUT")


def artifact_hash(api: RecoveryApi, version: dict[str, Any]) -> str:
    artifact = next(item for item in version["artifacts"]
                    if item["artifact_id"] == version["canonical_artifact_id"])
    actual = hashlib.sha256(api.request("GET", f"/artifacts/{artifact['artifact_id']}/download")).hexdigest()
    ensure(actual == artifact["sha256"], "El hash del snapshot canónico no coincide.")
    return actual


def attachment_hash(api: RecoveryApi, case: dict[str, Any]) -> str:
    ensure(len(case["attachments"]) == 1, "La excepción debe conservar su evidencia adjunta.")
    attachment = case["attachments"][0]
    contents = api.request("GET", f"/exceptions/{case['id']}/attachments/{attachment['id']}/download")
    actual = hashlib.sha256(contents).hexdigest()
    ensure(actual == attachment["sha256"] and len(contents) == attachment["size_bytes"],
           "El adjunto no coincide con su hash y tamaño registrados.")
    return actual


def prepare_exception(api: RecoveryApi, run: dict[str, Any]) -> dict[str, Any]:
    finding = next(item for item in run["findings"] if item["code"] == "POSITIVE:amount")
    case = api.json("POST", f"/findings/{finding['id']}/exceptions", {}, expected=201)
    ensure(case["auto_resolve_enabled"] is False, "La resolución automática debe iniciar deshabilitada.")
    case = api.json("PATCH", f"/exceptions/{case['id']}", {
        "version": case["version"], "state": "ASSIGNED", "assigned_user_id": api.user_id,
        "priority": "CRITICAL", "sla_hours": 24, "comment": "Investigar importe negativo en la fuente.",
    })
    case = api.json("POST", f"/exceptions/{case['id']}/comments", {
        "version": case["version"], "comment": "Conservar evidencia antes de respaldar el entorno.",
    })
    contents = b"Trackvance recovery evidence: negative amount remains pending correction.\n"
    case = api.upload_attachment(case, contents)
    ensure(attachment_hash(api, case) == hashlib.sha256(contents).hexdigest(),
           "La evidencia recibida no corresponde al comprobante original.")
    for state in ("INVESTIGATING", "PENDING_VALIDATION"):
        case = api.json("PATCH", f"/exceptions/{case['id']}", {
            "version": case["version"], "state": state,
            "root_cause": "Importe negativo en la base externa.",
            "resolution": "Pendiente de corrección y nueva validación de la fuente.",
        })
    detail = api.json("GET", f"/exceptions/{case['id']}")
    ensure(detail["assigned_user_id"] == api.user_id and detail["sla_hours"] == 24
           and detail["due_at"] and detail["state"] == "PENDING_VALIDATION"
           and detail["technical_validation"]["validated"] is False,
           "La gestión del caso no quedó persistida en estado pendiente.")
    return detail


def prepare_scheduled_monitor(api: RecoveryApi, dataset_id: str, version_id: str) -> dict[str, Any]:
    monitor = api.json("POST", "/monitors", {
        "name": "Recuperación - monitor programado", "dataset_id": dataset_id,
        "config": {"required_columns": ["customer_id"], "null_columns": ["customer_id"],
                   "max_null_rate": 0},
    }, expected=201)
    path = f"/monitors/{monitor['id']}"
    schedule = api.json("POST", path + "/schedule", {"interval_seconds": 60, "enabled": True})
    deadline = time.monotonic() + 120
    occurrence = None
    while time.monotonic() < deadline:
        rows = api.json("GET", path + "/occurrences")["items"]
        if rows:
            occurrence = rows[0]
            break
        time.sleep(0.5)
    ensure(occurrence is not None and bool(occurrence["run_id"]),
           "El worker no creó la ocurrencia programada esperada.")
    # Pause as soon as dispatch exists, before waiting for the run to finish.
    # The queued occurrence completes, but no new intervals can mutate the backup.
    paused = api.json("POST", path + "/schedule", {
        "interval_seconds": 60, "enabled": False, "expected_version": schedule["version"],
    })
    run = wait_existing_run(api, occurrence["run_id"])
    ensure(run["dataset_version_id"] == version_id and run["decision"] == "ALERT"
           and run["initiated_by"]["type"] == "SYSTEM", "El monitor programado no evaluó el snapshot esperado.")
    occurrences = api.json("GET", path + "/occurrences")
    series = api.json("GET", path + "/series")
    alerts = api.json("GET", path + "/alerts")
    ensure(paused["enabled"] is False and paused["version"] == 2
           and len(occurrences["items"]) == 1 and series["sample_count"] > 0 and bool(alerts["items"]),
           "No se conservaron programación, métricas y alerta del monitor.")
    manifest = api.request("GET", f"/runs/{run['id']}/evidence")
    return {"monitor_id": monitor["id"], "schedule": paused, "occurrences": occurrences,
            "series": series, "alerts": alerts, "run": run,
            "evidence_sha256": hashlib.sha256(manifest).hexdigest()}


def validate_monitor_restoration(api: RecoveryApi, historical: dict[str, Any]) -> dict[str, Any]:
    path = f"/monitors/{historical['monitor_id']}"
    for endpoint in ("schedule", "occurrences", "series", "alerts"):
        ensure(api.json("GET", f"{path}/{endpoint}") == historical[endpoint],
               f"El estado restaurado de Sentinel cambió: {endpoint}.")
    run = historical["run"]
    ensure(api.json("GET", f"/runs/{run['id']}") == run, "La ejecución Sentinel histórica cambió.")
    manifest = api.request("GET", f"/runs/{run['id']}/evidence")
    ensure(hashlib.sha256(manifest).hexdigest() == historical["evidence_sha256"],
           "El manifest de la ocurrencia programada cambió.")
    return {"monitor_id": historical["monitor_id"], "schedule_id": historical["schedule"]["id"],
            "paused": True, "schedule_version": historical["schedule"]["version"],
            "occurrence_id": historical["occurrences"]["items"][0]["id"], "run_id": run["id"],
            "dataset_version_id": run["dataset_version_id"],
            "metric_samples": historical["series"]["sample_count"],
            "historical_occurrence_metrics_alerts_unchanged": True}


def validate_case_after_correction(api: RecoveryApi, historical: dict[str, Any],
                                  run_id: str) -> dict[str, Any]:
    current = api.json("GET", f"/exceptions/{historical['id']}")
    ensure(current["state"] == "PENDING_VALIDATION" and current["auto_resolve_enabled"] is False,
           "Una política automática deshabilitada no debe cerrar el caso.")
    ensure(current["technical_validation"]["validated"] is True and current["validation_run_id"] == run_id,
           "La excepción no conserva la validación técnica de la nueva ejecución.")
    for key in ("origin_run_id", "configuration_id", "finding_id", "assigned_user_id", "priority", "sla_hours", "due_at", "attachments"):
        ensure(current[key] == historical[key], f"La trazabilidad del caso cambió: {key}.")
    ensure(current["events"][:len(historical["events"])] == historical["events"]
           and not any(event.get("event_type") == "AUTO_RESOLVED" for event in current["events"]),
           "El timeline histórico o la política de cierre no se respetaron.")
    attachment = current["attachments"][0]
    return {"case_id": current["id"], "state": current["state"], "validation_run_id": run_id,
            "origin_run_id": current["origin_run_id"], "assigned_user_id": current["assigned_user_id"],
            "sla_hours": current["sla_hours"], "attachment_id": attachment["id"],
            "attachment_artifact_id": attachment["artifact_id"], "attachment_sha256": attachment_hash(api, current),
            "technical_validation": "VALIDATED", "auto_resolve_enabled": False,
            "historical_timeline_preserved": True}


def capture_original(api: RecoveryApi, password: str,
                     delivery_password: str | None = None) -> dict[str, Any]:
    delivery_password = delivery_password or password
    connection = api.json("POST", "/connections", {
        "name": "PostgreSQL externo de recuperación", "source_type": "POSTGRESQL",
        "host": DATABASE_SERVICE, "port": 5432, "database": "recovery_source",
        "username": "tv_recovery_reader", "password": password,
        "options": {"sslmode": "disable", "connect_timeout": 3, "query_timeout": 15},
    }, expected=201)
    connection_id = connection["id"]
    test = api.json("POST", f"/connections/{connection_id}/test", {})
    ensure(test["status"] == "SUCCESS", "La conexión real inicial no pasó la prueba.")
    schemas = api.json("GET", f"/connections/{connection_id}/schemas")["items"]
    ensure("recovery_data" in schemas, "El schema externo no es accesible.")
    objects = api.json("GET", f"/connections/{connection_id}/objects?schema_name=recovery_data")
    ensure({(item["name"], item["kind"]) for item in objects["items"]}
           >= {("transactions", "TABLE"), ("transaction_view", "VIEW")},
           "No se descubrieron la tabla y la vista PostgreSQL.")
    preview = api.json("GET", f"/connections/{connection_id}/preview"
                       "?schema_name=recovery_data&object_name=transactions&limit=2")
    ensure(preview["sampled_rows"] == 2 and len(preview["columns"]) == 6,
           "La vista previa PostgreSQL no coincide con la fixture.")
    registered = api.json("POST", f"/connections/{connection_id}/datasets", {
        "name": "Recuperación - fuente PostgreSQL", "domain": "Pruebas aisladas",
        "schema_name": "recovery_data", "object_name": "transactions",
    }, expected=201)
    dataset_id, version = registered["dataset"]["id"], registered["version"]
    ensure(version["source_type"] == "POSTGRESQL" and version["row_count"] == 4,
           "El snapshot de origen no corresponde a los datos externos.")
    source = version["ingestion_metadata"]["source"]
    ensure(source["connection_version_id"] == connection["connection_version_id"]
           and source["config_hash"] == connection["config_hash"]
           and any(item["relation"] == "SOURCE_SNAPSHOT" for item in version["lineage"]),
           "El snapshot no conserva la configuración y el linaje externos.")
    contract = api.json("POST", "/intake/contracts", {
        "name": "Recuperación - validación", "dataset_id": dataset_id,
        "config": {"required_columns": ["record_id"], "positive_columns": ["amount"],
                   "max_error_rate": 0},
    }, expected=201)
    run = wait_run(api, contract["id"], version["id"])
    ensure(run["decision"] == "REJECTED" and run["metrics"]["error_rows"] == 1,
           "La ejecución original debe conservar un incumplimiento de negocio.")
    case = prepare_exception(api, run)
    monitor = prepare_scheduled_monitor(api, dataset_id, version["id"])
    # Intake adds lineage to the input. Capture the fully completed historical graph.
    details = api.json("GET", f"/datasets/{dataset_id}")
    version = next(item for item in details["versions"] if item["id"] == version["id"])
    run = api.json("GET", f"/runs/{run['id']}")  # Finding DTO now links the associated case.
    manifest = api.request("GET", f"/runs/{run['id']}/evidence")
    destination = api.json("POST", "/delivery/destinations", {
        "name": "PostgreSQL destino de recuperación", "sink_type": "POSTGRESQL",
        "host": DATABASE_SERVICE, "port": 5432, "database": "recovery_source",
        "username": "tv_recovery_writer", "password": delivery_password,
        "options": {"sslmode": "disable", "connect_timeout": 3, "query_timeout": 15},
    }, expected=201)
    destination_test = api.json(
        "POST", f"/delivery/destinations/{destination['id']}/test", {})
    ensure(destination_test["status"] == "SUCCESS",
           "El destino real inicial no pasó la prueba.")
    destination_tables = api.json(
        "GET", f"/delivery/destinations/{destination['id']}/tables"
        "?schema_name=recovery_delivery")
    ensure("records" in destination_tables["items"],
           "No se descubrió la tabla del destino de recuperación.")
    return {
        "connection_id": connection_id, "connection_version_id": connection["connection_version_id"],
        "connection_config_hash": connection["config_hash"], "dataset_id": dataset_id,
        "version": version, "contract_id": contract["id"], "run": run,
        "exception": case, "monitor": monitor,
        "destination": {
            "id": destination["id"],
            "version": destination["version"],
            "destination_version_id": destination["destination_version_id"],
            "config_hash": destination["config_hash"],
        },
        "canonical_sha256": artifact_hash(api, version),
        "evidence_sha256": hashlib.sha256(manifest).hexdigest(),
    }


def destroy_before_restore(source: str, database: str, evidence: Path) -> None:
    cleanup(source, evidence)
    assert_fresh(source)
    fixture = docker_state.inventory(database)
    ensure(len(fixture["containers"]) == 1 and fixture["containers"][0]["running"],
           "La fuente PostgreSQL debe sobrevivir a la destrucción de Trackvance.")


def notification_count(application: list[str], environment: dict[str, str],
                       credentials: tuple[str, ...]) -> int:
    script = """
from sqlalchemy import func, select
from trackvance.db import SessionLocal
from trackvance.models import NotificationDeliveryRecord
with SessionLocal() as db:
    print(db.scalar(select(func.count()).select_from(NotificationDeliveryRecord)))
"""
    return int(execute([*application, "exec", "-T", "api", "python", "-"], environment,
                       input_text=script, credentials=credentials).strip())


def prepare_identity_recovery(api: RecoveryApi, original: dict[str, Any], application: list[str],
                              environment: dict[str, str], credentials: tuple[str, ...]) -> tuple[str, ...]:
    notifications_before = notification_count(application, environment, credentials)
    role = api.json("POST", "/roles", {
        "name": "Recovery custom role", "description": "Native restore fixture",
        "permissions": ["datasets:read"], "active": True,
    }, expected=201)
    issued = api.json("POST", "/users", {
        "first_name": "Recovery", "last_name": "Fixture", "username": "recovery.fixture",
        "email": "recovery.fixture@trackvance.test", "role_id": role["id"], "active": True,
    }, expected=201)
    user = issued["user"]
    temporary = issued["temporary_credentials"]["temporary_password"]
    ensure(len(temporary) >= 20 and user["must_change_password"], "Credencial temporal no válida.")
    credentials += (temporary,)
    api.credentials = credentials
    regenerated = api.json("POST", f"/users/{user['id']}/regenerate-credentials",
                           {"version": user["version"]})
    replacement = regenerated["temporary_credentials"]["temporary_password"]
    ensure(replacement != temporary and len(replacement) >= 20, "La regeneración no rota credenciales.")
    credentials += (replacement,)
    api.credentials = credentials
    del issued, regenerated, temporary, replacement
    ensure(notification_count(application, environment, credentials) == notifications_before,
           "Alta o regeneración 0.6.1 creó una notificación.")
    api.json("GET", "/users")
    api.json("GET", "/audit-events")
    # Durable synthetic metadata only. Actual signed OIDC/PKCE authentication is
    # certified by identity_sso_cycle; recovery must also cover nonempty tables.
    script = '''
import hashlib
import json
from datetime import timedelta
from trackvance.db import SessionLocal, utcnow
from trackvance.models import ExternalIdentity, NotificationDeliveryRecord, OIDCLoginAttempt, User
identity = json.loads(INPUT)
with SessionLocal() as db:
    user = db.get(User, identity["user_id"])
    now = utcnow()
    db.add(ExternalIdentity(organization_id=user.organization_id, user_id=user.id,
        provider="GOOGLE", issuer="https://accounts.google.com",
        subject="recovery-synthetic-subject", email_at_link=user.email,
        linked_at=now, last_login_at=now))
    db.add(OIDCLoginAttempt(
        state_hash=hashlib.sha256(b"recovery-consumed-state").hexdigest(),
        browser_hash=hashlib.sha256(b"recovery-browser-binding").hexdigest(),
        provider="GOOGLE", nonce="", code_verifier="",
        expires_at=now + timedelta(minutes=10), consumed_at=now))
    # Declared historical persistence fixture: no active SMTP delivery in 0.6.1.
    db.add(NotificationDeliveryRecord(organization_id=user.organization_id,
        event_type="USER_TEMPORARY_CREDENTIALS", template_key="USER_TEMPORARY_CREDENTIALS",
        channel="EMAIL", recipient_type="USER", recipient_user_id=user.id,
        recipient_email_snapshot=user.email, provider_key="SMTP", attempt_number=1,
        status="FAILED", error_code="NO_PROVIDER", failed_at=now))
    db.commit()
'''.replace("INPUT", repr(json.dumps({"user_id": user["id"]})))
    execute([*application, "exec", "-T", "api", "python", "-"], environment,
            input_text=script, credentials=credentials)
    original["identity"] = {
        "user": api.json("GET", f"/users/{user['id']}"),
        "role": api.json("GET", f"/roles/{role['id']}"),
        "fixture": "SYNTHETIC_HISTORICAL_NOTIFICATION_LINK_AND_CONSUMED_OIDC_NO_TOKENS_OR_BODY",
        "create_and_regenerate_notifications_unchanged": True,
    }
    return credentials


def validate_identity_restoration(api: RecoveryApi, original: dict[str, Any]) -> dict[str, Any]:
    saved = original["identity"]
    user = api.json("GET", f"/users/{saved['user']['id']}")
    role = api.json("GET", f"/roles/{saved['role']['id']}")
    ensure(user == saved["user"] and role == saved["role"],
           "El restore modificó identidad, permisos, credenciales o vínculo externo.")
    ensure(len(user["external_identities"]) == 1 and user["must_change_password"],
           "Falta el vínculo externo o la restricción del primer acceso restaurado.")
    return {"status": "PASS", "fixture": saved["fixture"],
            "create_and_regenerate_notifications_unchanged": saved["create_and_regenerate_notifications_unchanged"],
            "user_and_role_api_exactly_preserved": True,
            "external_identity_exactly_preserved": True,
            "notification_and_first_login_exactly_preserved": True,
            "oidc_consumed_attempt_preserved_by_state_hash": True}


def delivery_draft(original: dict[str, Any], table_name: str) -> dict[str, Any]:
    destination = original["destination"]
    return {
        "schema_version": 1,
        "dataset_version_id": original["version"]["id"],
        "destination_id": destination["id"],
        "destination_version_id": destination["destination_version_id"],
        "target": {"mode": "EXISTING_TABLE", "schema_name": "recovery_delivery",
                   "table_name": table_name, "create_schema": False},
        "columns": [
            {"source_name": "record_id", "target_name": "record_id",
             "target_type": "STRING", "ordinal": 0, "nullable": False, "length": 20},
            {"source_name": "customer_id", "target_name": "customer_id",
             "target_type": "STRING", "ordinal": 1, "nullable": True, "length": 20},
            {"source_name": "amount", "target_name": "amount", "target_type": "DECIMAL",
             "ordinal": 2, "nullable": False, "precision": 16, "scale": 2},
            {"source_name": "booked_on", "target_name": "booked_on", "target_type": "DATE",
             "ordinal": 3, "nullable": False},
            {"source_name": "recorded_at", "target_name": "recorded_at",
             "target_type": "TIMESTAMP", "ordinal": 4, "nullable": True},
            {"source_name": "active", "target_name": "active", "target_type": "BOOLEAN",
             "ordinal": 5, "nullable": False},
        ],
        "write_strategy": "APPEND",
        "upsert_keys": [],
    }


def operational_remote_count(compose: list[str], environment: dict[str, str],
                             credentials: tuple[str, ...]) -> int:
    return int(execute(
        [*compose, "exec", "-T", DATABASE_SERVICE, "psql", "-U", "recovery_admin",
         "-d", "recovery_source", "-At", "-c",
         "SELECT COUNT(*) FROM recovery_delivery.operational_records"],
        environment, credentials=credentials,
    ).strip())


def prepare_delivery_operations(
    api: RecoveryApi, original: dict[str, Any], application: list[str],
    fixture: list[str], environment: dict[str, str], credentials: tuple[str, ...],
) -> None:
    """Real COMMITTED write; local evidence loss; explicitly simulated UNKNOWN.

    The injection runs only inside the guarded disposable project. UNKNOWN is a
    durable test fixture with no remote I/O, not a claimed network/commit failure.
    """
    configuration = api.json("POST", "/delivery/configurations", {
        **delivery_draft(original, "operational_records"),
        "audit_columns_enabled": True,
        "name": "Entrega previa al respaldo y reparación local", "owner": "Recovery drill",
        "description": "Escritura real; pérdida controlada exclusivamente de evidencia local",
    }, expected=201)
    queued = api.json("POST", "/delivery/runs", {
        "configuration_id": configuration["id"],
        "dataset_version_id": original["version"]["id"],
    }, expected=202)
    committed = wait_existing_run(api, queued["id"])
    ensure(committed["decision"] == "COMMITTED", "La entrega real inicial no quedó confirmada.")
    committed = wait_for_delivery_evidence(api, committed["id"])
    receipt_before = api.request("GET", f"/delivery/runs/{committed['id']}/receipt")
    manifest_before = api.request("GET", f"/runs/{committed['id']}/evidence")
    ensure(operational_remote_count(fixture, environment, credentials) == 4,
           "La fixture operativa requiere exactamente cuatro filas reales.")
    # Input contains only generated IDs. No credential or business value enters argv.
    injection = '''
import json
from sqlalchemy import select
from trackvance.artifactstore import storage_provider
from trackvance.db import SessionLocal, utcnow
from trackvance.delivery_service import enqueue_delivery, _delivery_idempotency
from trackvance.models import Artifact, Configuration, DatasetVersion, DeliveryAttempt, Job, Run, User, uid

identity = json.loads(INPUT)
with SessionLocal() as db:
    committed = db.get(Run, identity["run_id"])
    assert committed.module == "DELIVERY" and committed.status == "SUCCESS"
    assert committed.decision == "COMMITTED"
    known = db.scalar(select(DeliveryAttempt).where(DeliveryAttempt.run_id == committed.id))
    assert known.status == "COMMITTED"
    evidence = [db.get(Artifact, committed.metrics["receipt_artifact_id"]),
                db.scalar(select(Artifact).where(Artifact.path == committed.evidence_path))]
    assert {item.kind for item in evidence} == {"DELIVERY_RECEIPT", "RUN_MANIFEST"}
    # materialize checks storage ownership, exact hash and size before these two
    # fixture-owned files alone are removed. Metadata and original hashes remain.
    for item in evidence:
        assert item.organization_id == committed.organization_id
        storage_provider.materialize(item).unlink()
    committed.metrics = {**committed.metrics, "evidence_status": "PENDING_REPAIR"}
    config = db.get(Configuration, committed.config_id)
    source = db.get(DatasetVersion, committed.dataset_version_id)
    actor = db.get(User, identity["user_id"])
    unknown = enqueue_delivery(db, config, source, actor)
    now = utcnow()
    unknown.status = unknown.decision = "UNKNOWN"
    unknown.started_at = unknown.finished_at = now
    unknown.progress_stage = "Fixture UNKNOWN simulada"
    unknown.error = "Fixture explícita: no se realizó I/O remoto para este intento."
    unknown.execution_plan = {**unknown.execution_plan,
                              "recovery_fixture": "SIMULATED_UNKNOWN_NO_REMOTE_IO"}
    job = db.scalar(select(Job).where(Job.run_id == unknown.id))
    job.status = "UNKNOWN"
    job.attempts = 1
    attempt = DeliveryAttempt(id=uid(), organization_id=unknown.organization_id,
        run_id=unknown.id, destination_version_id=known.destination_version_id,
        attempt_number=1, idempotency_key=_delivery_idempotency(unknown, config),
        status="UNKNOWN", target_locator=known.target_locator, rows_attempted=source.row_count,
        rows_written=None, rows_inserted=None, rows_updated=None, bytes_sent=None,
        error_code="TEST_SIMULATED_UNKNOWN", error_message=unknown.error,
        started_at=now, finished_at=now)
    db.add(attempt)
    db.commit()
    print(json.dumps({"unknown_run_id": unknown.id, "unknown_attempt_id": attempt.id}))
'''.replace("INPUT", repr(json.dumps({"run_id": committed["id"], "user_id": api.user_id})))
    injected = json.loads(execute(
        [*application, "exec", "-T", "api", "python", "-"], environment,
        input_text=injection, credentials=credentials,
    ))
    clients = [RecoveryApi(api.port, credentials) for _ in range(2)]
    barrier = Barrier(2)

    def concurrent_repair(client: RecoveryApi) -> dict[str, Any]:
        barrier.wait(timeout=30)
        return client.json("POST", f"/delivery/runs/{committed['id']}/repair-evidence", {})

    with ThreadPoolExecutor(max_workers=2) as executor:
        repaired = list(executor.map(concurrent_repair, clients))
    ensure(sorted(item["status"] for item in repaired) == ["ALREADY_VALID", "REPAIRED"],
           "La reparación concurrente PostgreSQL no fue idempotente.")
    ensure(repaired[0]["receipt_artifact_id"] == repaired[1]["receipt_artifact_id"]
           and repaired[0]["manifest_artifact_id"] == repaired[1]["manifest_artifact_id"],
           "La reparación concurrente generó identidades duplicadas.")
    ensure(api.request("GET", f"/delivery/runs/{committed['id']}/receipt") == receipt_before
           and api.request("GET", f"/runs/{committed['id']}/evidence") == manifest_before,
           "La reparación no reconstruyó exactamente los bytes registrados.")
    unknown_id = injected["unknown_run_id"]
    unknown_before = api.json("GET", f"/runs/{unknown_id}")
    attempts_before = api.json("GET", f"/delivery/runs/{unknown_id}/attempts")
    review = api.json("POST", f"/delivery/runs/{unknown_id}/reviews", {
        "delivery_attempt_id": injected["unknown_attempt_id"], "outcome": "INCONCLUSIVE",
        "note": "Fixture UNKNOWN simulada sin I/O remoto; prueba de recuperación, no fallo real de commit.",
    }, expected=201)
    ensure(api.json("GET", f"/runs/{unknown_id}") == unknown_before
           and api.json("GET", f"/delivery/runs/{unknown_id}/attempts") == attempts_before,
           "La revisión operativa alteró el UNKNOWN histórico.")
    ensure(operational_remote_count(fixture, environment, credentials) == 4,
           "La reparación/revisión duplicó o cambió filas remotas.")
    original["delivery_operations"] = {
        "committed_run": api.json("GET", f"/runs/{committed['id']}"),
        "committed_attempts": api.json("GET", f"/delivery/runs/{committed['id']}/attempts"),
        "repair": repaired[0], "concurrent_repair_statuses": sorted(item["status"] for item in repaired),
        "receipt_sha256": hashlib.sha256(receipt_before).hexdigest(),
        "manifest_sha256": hashlib.sha256(manifest_before).hexdigest(),
        "unknown_run": unknown_before, "unknown_attempts": attempts_before,
        "review": review, "reviews": api.json("GET", f"/delivery/runs/{unknown_id}/reviews"),
        "unknown_fixture": "SIMULATED_UNKNOWN_NO_REMOTE_IO", "remote_rows": 4,
    }
    # Delivery adds real canonical lineage edges after the earlier Intake capture.
    details = api.json("GET", f"/datasets/{original['dataset_id']}")
    original["version"] = next(item for item in details["versions"]
                               if item["id"] == original["version"]["id"])


def validate_operational_delivery_restoration(
    api: RecoveryApi, saved: dict[str, Any], compose: list[str],
    environment: dict[str, str], credentials: tuple[str, ...],
) -> dict[str, Any]:
    for prefix in ("committed", "unknown"):
        run_id = saved[f"{prefix}_run"]["id"]
        ensure(api.json("GET", f"/runs/{run_id}") == saved[f"{prefix}_run"],
               "El Run Delivery histórico cambió durante el restore.")
        ensure(api.json("GET", f"/delivery/runs/{run_id}/attempts") == saved[f"{prefix}_attempts"],
               "El intento Delivery histórico cambió durante el restore.")
    committed_id, unknown_id = saved["committed_run"]["id"], saved["unknown_run"]["id"]
    for path, digest in (
        (f"/delivery/runs/{committed_id}/receipt", saved["receipt_sha256"]),
        (f"/runs/{committed_id}/evidence", saved["manifest_sha256"]),
    ):
        ensure(hashlib.sha256(api.request("GET", path)).hexdigest() == digest,
               "La evidencia reparada cambió durante el restore.")
    ensure(api.json("GET", f"/delivery/runs/{unknown_id}/reviews") == saved["reviews"],
           "La revisión estructurada del UNKNOWN cambió durante el restore.")
    repeated = api.json("POST", f"/delivery/runs/{committed_id}/repair-evidence", {})
    ensure(repeated == {**saved["repair"], "status": "ALREADY_VALID"},
           "La reparación posterior al restore dejó de ser idempotente.")
    ensure(operational_remote_count(compose, environment, credentials) == saved["remote_rows"],
           "El restore o la reparación repitió escritura remota.")
    return {
        "committed_run_id": committed_id, "unknown_run_id": unknown_id,
        "review_id": saved["review"]["id"], "unknown_fixture": saved["unknown_fixture"],
        "receipt_sha256": saved["receipt_sha256"], "manifest_sha256": saved["manifest_sha256"],
        "concurrent_repair_statuses": saved["concurrent_repair_statuses"],
        "post_restore_repair_status": repeated["status"], "remote_rows_before_and_after": 4,
        "historical_unknown_unchanged": True, "review_exactly_preserved": True,
        "repaired_evidence_exactly_preserved": True, "remote_replay": False,
    }


def validate_delivery_restoration(
    api: RecoveryApi,
    original: dict[str, Any],
    compose: list[str],
    environment: dict[str, str],
    credentials: tuple[str, ...],
) -> dict[str, Any]:
    saved = original["destination"]
    tested = api.json("POST", f"/delivery/destinations/{saved['id']}/test", {})
    ensure(tested["status"] == "SUCCESS",
           "La credencial destino restaurada no permite reconectar.")
    destination = api.json("GET", f"/delivery/destinations/{saved['id']}")
    ensure(destination["version"] == saved["version"]
           and destination["destination_version_id"] == saved["destination_version_id"]
           and destination["config_hash"] == saved["config_hash"],
           "La configuración destino cambió durante la recuperación.")
    draft = delivery_draft(original, "records")
    preflight = api.json("POST", "/delivery/preflight", draft)
    ensure(preflight["status"] == "PASS",
           "El destino restaurado no superó el preflight de solo lectura.")
    configuration = api.json("POST", "/delivery/configurations", {
        **draft,
        "name": "Entrega posterior a recuperación",
        "owner": "Recovery drill",
        "description": "Demuestra secreto destino restaurado sin exponerlo",
    }, expected=201)
    queued = api.json("POST", "/delivery/runs", {
        "configuration_id": configuration["id"],
        "dataset_version_id": original["version"]["id"],
    }, expected=202)
    run = wait_existing_run(api, queued["id"])
    ensure(run["module"] == "DELIVERY" and run["decision"] == "COMMITTED",
           "La entrega posterior al restore no quedó confirmada.")
    run = wait_for_delivery_evidence(api, run["id"])
    attempts = api.json("GET", f"/delivery/runs/{run['id']}/attempts")
    ensure(attempts["total"] == 1 and attempts["items"][0]["status"] == "COMMITTED",
           "El intento Delivery restaurado no quedó confirmado.")
    receipt = json.loads(api.request("GET", f"/delivery/runs/{run['id']}/receipt"))
    ensure(receipt["result"] == "COMMITTED" and receipt["rows_written"] == 4,
           "El receipt posterior al restore no coincide con la escritura.")
    count = execute(
        [*compose, "exec", "-T", DATABASE_SERVICE, "psql", "-U", "recovery_admin",
         "-d", "recovery_source", "-At", "-c",
         "SELECT COUNT(*) FROM recovery_delivery.records"],
        environment,
        credentials=credentials,
    ).strip()
    ensure(count == "4", "La entrega restaurada no escribió las cuatro filas externas.")
    return {
        "destination_test": "SUCCESS",
        "destination_id": destination["id"],
        "destination_version_id": destination["destination_version_id"],
        "run_id": run["id"],
        "attempt_id": attempts["items"][0]["id"],
        "receipt_result": receipt["result"],
        "rows_written": receipt["rows_written"],
        "restored_destination_credential_used": True,
    }


def validate_restored(api: RecoveryApi, original: dict[str, Any], compose: list[str],
                      environment: dict[str, str], credentials: tuple[str, ...]) -> dict[str, Any]:
    identity_report = validate_identity_restoration(api, original)
    connection_id, dataset_id = original["connection_id"], original["dataset_id"]
    # No password after recovery: prove restored ciphertext AND master key work.
    test = api.json("POST", f"/connections/{connection_id}/test", {})
    ensure(test["status"] == "SUCCESS", "La credencial restaurada no permite reconectar.")
    connection = api.json("GET", f"/connections/{connection_id}")
    ensure(connection["connection_version_id"] == original["connection_version_id"]
           and connection["config_hash"] == original["connection_config_hash"],
           "La configuración de conexión cambió durante la recuperación.")
    details = api.json("GET", f"/datasets/{dataset_id}")
    restored_version = next(item for item in details["versions"]
                            if item["id"] == original["version"]["id"])
    ensure(restored_version == original["version"], "El DatasetVersion histórico cambió.")
    ensure(artifact_hash(api, restored_version) == original["canonical_sha256"],
           "El Parquet restaurado cambió.")
    original_run_id = original["run"]["id"]
    preserved = api.json("GET", f"/runs/{original_run_id}")
    ensure(preserved == original["run"], "La ejecución histórica cambió durante el restore.")
    manifest = api.request("GET", f"/runs/{original_run_id}/evidence")
    ensure(hashlib.sha256(manifest).hexdigest() == original["evidence_sha256"],
           "El manifest histórico cambió.")
    restored_case = api.json("GET", f"/exceptions/{original['exception']['id']}")
    ensure(restored_case == original["exception"], "La excepción y su historial cambiaron durante el restore.")
    attachment_hash(api, restored_case)
    monitor_report = validate_monitor_restoration(api, original["monitor"])
    operational_delivery_report = validate_operational_delivery_restoration(
        api, original["delivery_operations"], compose, environment, credentials)
    delivery_report = validate_delivery_restoration(
        api, original, compose, environment, credentials)
    execute([*compose, "exec", "-T", DATABASE_SERVICE, "psql", "-U", "recovery_admin",
             "-d", "recovery_source", "-v", "ON_ERROR_STOP=1"], environment,
            input_text="UPDATE recovery_data.transactions SET amount=2.00 WHERE record_id='002';",
            credentials=credentials)
    refreshed = api.json("POST", f"/datasets/{dataset_id}/refresh-source", {}, expected=201)
    ensure(refreshed["id"] != restored_version["id"] and refreshed["version"] == 2
           and refreshed["ingestion_metadata"]["source"]["connection_version_id"]
           == original["connection_version_id"], "El refresh no generó un nuevo snapshot trazable.")
    ensure(any(item["relation"] == "REFRESH_OF" and item["target_id"] == restored_version["id"]
               for item in refreshed["lineage"]), "Falta el linaje entre snapshots.")
    refreshed_hash = artifact_hash(api, refreshed)
    ensure(refreshed_hash != original["canonical_sha256"], "El refresh no leyó la corrección externa.")
    run = wait_run(api, original["contract_id"], refreshed["id"])
    ensure(run["decision"] == "APPROVED" and run["metrics"]["error_rows"] == 0
           and run["metrics"]["total_rows"] == 4,
           "La nueva validación no confirmó la corrección de los datos.")
    latest = api.json("GET", f"/datasets/{dataset_id}")
    ensure(len(latest["versions"]) == 2, "El refresh sobrescribió o duplicó versiones.")
    unchanged = next(item for item in latest["versions"] if item["id"] == restored_version["id"])
    # A new REFRESH_OF graph edge is expected, but immutable version fields stay equal.
    ensure({key: value for key, value in unchanged.items() if key != "lineage"}
           == {key: value for key, value in restored_version.items() if key != "lineage"},
           "La versión original fue modificada al refrescar la fuente.")
    ensure(api.json("GET", f"/runs/{original_run_id}") == original["run"],
           "La nueva ejecución modificó el resultado original.")
    new_manifest = api.request("GET", f"/runs/{run['id']}/evidence")
    ensure(isinstance(json.loads(new_manifest), dict), "La nueva ejecución no tiene un manifest válido.")
    case_report = validate_case_after_correction(api, original["exception"], run["id"])
    validate_monitor_restoration(api, original["monitor"])
    return {
        "connection_test": "SUCCESS", "connection_id": connection_id,
        "connection_version_id": original["connection_version_id"],
        "dataset_id": dataset_id, "original_version_id": restored_version["id"],
        "refreshed_version_id": refreshed["id"], "original_run_id": original_run_id,
        "original_decision": preserved["decision"], "new_run_id": run["id"],
        "new_decision": run["decision"], "canonical_sha256": original["canonical_sha256"],
        "refreshed_canonical_sha256": refreshed_hash, "evidence_sha256": original["evidence_sha256"],
        "new_evidence_sha256": hashlib.sha256(new_manifest).hexdigest(),
        "historical_records_unchanged": True, "refresh_lineage_verified": True,
        "restored_credential_used": True,
        "restored_destination_credential_used": True,
        "exception": case_report, "sentinel": monitor_report, "delivery": delivery_report,
        "delivery_operations": operational_delivery_report,
        "identity": identity_report,
    }


def main() -> int:
    if '--v070-context' in sys.argv:
        parser = argparse.ArgumentParser(description='Native isolated 0.7.0 recovery without starting automatic processes')
        parser.add_argument('--v070-context', type=Path, required=True)
        parser.add_argument('--evidence-dir', type=Path)
        options = parser.parse_args()
        from v070_recovery import native_cycle

        return native_cycle(options.v070_context, options.evidence_dir)
    parser = argparse.ArgumentParser(description=__doc__)
    suffix = f"{os.getpid()}-{uuid4().hex[:6]}"
    parser.add_argument("--source-project", default=f"trackvance-recovery-src-{suffix}")
    parser.add_argument("--target-project", default=f"trackvance-recovery-dst-{suffix}")
    parser.add_argument("--database-project", default=f"trackvance-recovery-db-{suffix}")
    parser.add_argument("--source-port", type=int, default=0)
    parser.add_argument("--target-port", type=int, default=0)
    parser.add_argument("--skip-build", action="store_true")
    parser.add_argument("--keep", action="store_true",
                        help="Conserva destino y fuente externa; Trackvance origen se destruye.")
    parser.add_argument("--evidence-dir", type=Path)
    options = parser.parse_args()
    try:
        source = validated_project_name(options.source_project)
        target = validated_project_name(options.target_project)
        database = validated_project_name(options.database_project, database=True)
    except ValueError as error:
        parser.error(str(error))
    if source == target:
        parser.error("Los proyectos fuente y destino deben ser diferentes.")
    source_port, target_port = options.source_port or available_port(), options.target_port or available_port()
    if source_port == target_port or not all(1 <= value <= 65535 for value in (source_port, target_port)):
        parser.error("Los puertos deben ser distintos y válidos.")
    evidence = (options.evidence_dir or ROOT / ".codex-local" / "recovery"
                / f"{source}-to-{target}").resolve()
    evidence.mkdir(parents=True, exist_ok=False)
    backup = evidence / "backup"
    internal_password, external_password, reader_password, delivery_password = (
        secrets.token_hex(24) for _ in range(4)
    )
    credentials = (internal_password, external_password, reader_password, delivery_password)
    environment = {
        **os.environ, "POSTGRES_PASSWORD": internal_password,
        "RECOVERY_SOURCE_PASSWORD": external_password,
        "DEMO_ACCESS_ENABLED": "true", "DEMO_SEED_ENABLED": "false",
        "WEB_PORT": str(source_port), "TRACKVANCE_WEB_ORIGIN": f"http://localhost:{source_port}",
    }
    # A failed freshness guard must never schedule cleanup of existing resources.
    # Claim only proven-empty projects immediately before attempting their creation.
    claimed: list[str] = []
    result: dict[str, Any] = {
        "status": "FAIL", "schema_version": 4, "source_project": source,
        "target_project": target, "external_database_project": database,
        "source_port": source_port, "target_port": target_port,
        "source_destroyed_before_restore": False,
        "ui_validation": {"html_http_status": None, "playwright": "NOT_RUN_IN_THIS_DRILL"},
    }
    stage = "freshness_guards"
    try:
        for project in (source, target, database):
            assert_fresh(project)
        fixture_file = evidence / "external-postgres.compose.json"
        fixture_file.write_text(json.dumps(fixture_compose(), indent=2), encoding="utf-8")
        fixture = ["docker", "compose", "-p", database, "-f", str(fixture_file)]
        stage = "external_postgresql"
        claimed.append(database)
        execute([*fixture, "up", "-d", "--wait"], environment, credentials=credentials)
        initialize_source(
            fixture, environment, credentials, reader_password, delivery_password)
        stage = "source_trackvance"
        claimed.append(source)
        compose = ["docker", "compose", "-p", source, "-f", str(ROOT / "compose.yml")]
        up = [*compose, "up", "-d", "--wait"]
        if not options.skip_build:
            up.append("--build")
        execute(up, environment, credentials=credentials)
        stage = "postgres_migration"
        result["postgres_migration"] = json.loads(execute(
            [*compose, "exec", "-T", "api", "python", "-"], environment,
            input_text=(ROOT / "scripts" / "check_postgres_migrations.py").read_text(encoding="utf-8"),
            credentials=credentials,
        ))
        ensure(result["postgres_migration"].get("status") == "PASS",
               "La migración aditiva no preservó los registros históricos.")
        stage = "source_trackvance_fixtures"
        attach_external_network(source, database, environment)
        source_api = RecoveryApi(source_port, credentials)
        result["version"] = source_api.json("GET", "/health")["version"]
        original = capture_original(source_api, reader_password, delivery_password)
        stage = "delivery_operational_fixtures"
        prepare_delivery_operations(source_api, original, compose, fixture, environment, credentials)
        stage = "identity_fixtures"
        credentials = prepare_identity_recovery(source_api, original, compose, environment, credentials)
        stage = "backup"
        execute([sys.executable, str(ROOT / "scripts" / "docker_state.py"), "backup",
                 "--project", source, "--destination", str(backup)], environment,
                credentials=credentials)
        state = json.loads((backup / "state.json").read_text(encoding="utf-8"))
        result["plaintext_backup_scan"] = scan_backup_plaintext(backup, compose, environment, credentials)
        result["state_schema_version"] = state["schema_version"]
        ensure(state.get("verified_secrets") == 2
               and state.get("verified_source_secrets") == 1
               and state.get("verified_delivery_secrets") == 1,
               "No se verificaron por separado las credenciales de fuente y destino.")
        counts = {name: len(state["tables"].get(name, {})) for name in (
            "exceptions", "exception_attachments", "monitor_schedules", "monitor_schedule_versions",
            "monitor_occurrences", "metric_history", "delivery_attempts", "delivery_reviews",
            "roles", "role_permissions", "notification_deliveries", "delivery_target_policies",
            "external_identities", "oidc_login_attempts",
        )}
        ensure(all(counts.values()) and counts["monitor_schedule_versions"] == 2,
               "El respaldo no contiene todos los registros operativos del ciclo.")
        result.update({
            "backup_manifest_sha256": docker_state.digest(backup / "backup-manifest.json"),
            "state_sha256": docker_state.digest(backup / "state.json"),
            "verified_artifacts": state.get("verified_artifacts"),
            "verified_secrets": state.get("verified_secrets"),
            "verified_source_secrets": state.get("verified_source_secrets"),
            "verified_delivery_secrets": state.get("verified_delivery_secrets"),
            "validated_relationships": state.get("validated_relationships"),
            "alembic_revision": state.get("migration"),
            "operational_table_counts": counts,
        })
        execute([*compose, "logs", "--no-color"], environment, credentials=credentials)
        stage = "destroy_source"
        destroy_before_restore(source, database, evidence)
        result["source_destroyed_before_restore"] = True
        stage = "restore"
        assert_fresh(target)
        claimed.append(target)
        execute([sys.executable, str(ROOT / "scripts" / "docker_state.py"), "restore",
                 "--source", str(backup), "--target-project", target, "--start", "--smoke",
                 "--web-port", str(target_port)], environment, credentials=credentials)
        attach_external_network(target, database, environment)
        stage = "functional_recovery"
        result["recovery"] = validate_restored(
            RecoveryApi(target_port, credentials), original, fixture, environment, credentials)
        stage = "doctor_and_logs"
        execute([sys.executable, str(ROOT / "scripts" / "doctor.py"), "--base-url",
                 f"http://127.0.0.1:{target_port}", "--docker", "--project", target,
                 "--recovery-ready", "--json"], environment, credentials=credentials)
        target_compose = ["docker", "compose", "-p", target, "-f", str(ROOT / "compose.yml")]
        for command in (target_compose, fixture):
            execute([*command, "logs", "--no-color"], environment, credentials=credentials)
        with urllib.request.urlopen(f"http://127.0.0.1:{target_port}/", timeout=15) as response:
            ensure(response.status == 200 and b"<" in response.read(4096),
                   "La interfaz restaurada no respondió con HTML.")
            result["ui_validation"]["html_http_status"] = response.status
        result.update({"status": "PASS", "api_smoke": "PASS", "doctor": "PASS",
                       "log_secret_scan": "PASS", "persistence_exact_comparison": "PASS"})
    except (OSError, RuntimeError, ValueError, KeyError, StopIteration,
            subprocess.SubprocessError) as error:
        # Exception text may contain driver details. Report phase and class only.
        result.update({"failed_stage": stage, "error_type": type(error).__name__})
        if isinstance(error, DeliveryEvidenceError):
            result["error_code"] = error.code
        if isinstance(error, RecoveryCommandError):
            result.update({"exit_code": error.exit_code, "error_category": error.category})
        print(f"ERROR: recuperación en {stage} ({type(error).__name__}); detalle suprimido.",
              file=sys.stderr)
    finally:
        if not options.keep:
            for project in reversed(claimed):
                try:
                    cleanup(project, evidence)
                except (OSError, RuntimeError, ValueError, subprocess.SubprocessError) as error:
                    result["status"] = "FAIL"
                    result.setdefault("cleanup_failures", []).append({
                        "project": project, "error_type": type(error).__name__})
        result["retained_projects"] = claimed if options.keep else [
            item["project"] for item in result.get("cleanup_failures", [])]
        if options.keep and result["source_destroyed_before_restore"]:
            result["retained_projects"] = [item for item in claimed if item != source]
        serialized = json.dumps(result, indent=2, sort_keys=True)
        assert_no_secrets(serialized, credentials)
        (evidence / "result.json").write_text(serialized, encoding="utf-8")
        print(serialized)
    return 0 if result["status"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
