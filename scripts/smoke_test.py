#!/usr/bin/env python3
"""Exercise the local Trackvance API with new, isolated verification data.

Uses only the Python standard library. It never resets or deletes application data.
Run: python scripts/smoke_test.py --base-url http://127.0.0.1:8000
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import http.cookiejar
import io
import json
import sys
import time
import urllib.error
import urllib.request
import uuid
import zipfile
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any
from xml.etree import ElementTree


class SmokeFailure(RuntimeError):
    """An externally observable product invariant did not hold."""


@dataclass
class Response:
    status: int
    headers: Any
    body: bytes

    def json(self) -> Any:
        try:
            return json.loads(self.body)
        except (ValueError, UnicodeDecodeError) as exc:
            raise SmokeFailure("La API no devolvio el JSON esperado.") from exc


class Api:
    def __init__(self, base_url: str, timeout: float) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.cookies = http.cookiejar.CookieJar()
        self.opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(self.cookies)
        )
        self.csrf = ""

    def request(
        self,
        method: str,
        path: str,
        payload: Any = None,
        *,
        expected: tuple[int, ...] = (200,),
        raw: bytes | None = None,
        content_type: str | None = None,
    ) -> Response:
        headers = {"Accept": "application/json"}
        if self.csrf and method not in ("GET", "HEAD", "OPTIONS"):
            headers["X-CSRF-Token"] = self.csrf
        body = raw
        if payload is not None:
            body = json.dumps(payload).encode("utf-8")
            headers["Content-Type"] = "application/json"
        elif content_type:
            headers["Content-Type"] = content_type
        request = urllib.request.Request(
            self.base_url + path, data=body, headers=headers, method=method
        )
        try:
            with self.opener.open(request, timeout=self.timeout) as result:
                response = Response(result.status, result.headers, result.read())
        except urllib.error.HTTPError as exc:
            response = Response(exc.code, exc.headers, exc.read())
        except (urllib.error.URLError, TimeoutError) as exc:
            raise SmokeFailure(
                f"No fue posible conectar con {self.base_url}: {exc}"
            ) from exc
        if response.status not in expected:
            detail = response.body.decode("utf-8", errors="replace")[:800]
            raise SmokeFailure(
                f"{method} {path}: HTTP {response.status}, esperado {expected}. {detail}"
            )
        return response

    def get(self, path: str) -> Any:
        return self.request("GET", path).json()

    def post(self, path: str, payload: Any, *, expected=(201,)) -> Any:
        return self.request("POST", path, payload, expected=expected).json()

    def upload(
        self,
        path: str,
        filename: str,
        contents: bytes,
        *,
        fields: dict[str, str] | None = None,
        file_field: str = "file",
    ) -> Any:
        boundary = "trackvance-smoke-" + uuid.uuid4().hex
        parts = []
        for name, value in (fields or {}).items():
            parts.append(
                f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"'
                f"\r\n\r\n{value}\r\n".encode()
            )
        parts.extend(
            [
                (
                    f'--{boundary}\r\nContent-Disposition: form-data; name="{file_field}"; '
                    f'filename="{filename}"\r\nContent-Type: text/csv\r\n\r\n'
                ).encode(),
                contents,
                f"\r\n--{boundary}--\r\n".encode("ascii"),
            ]
        )
        return self.request(
            "POST",
            path,
            raw=b"".join(parts),
            content_type="multipart/form-data; boundary=" + boundary,
            expected=(201,),
        ).json()


class Checks:
    def __init__(self) -> None:
        self.completed: list[str] = []

    def verify(self, condition: bool, message: str) -> None:
        if not condition:
            raise SmokeFailure(message)
        self.completed.append(message)
        print("[OK] " + message, flush=True)


def csv_bytes(columns: list[str], rows: list[list[Any]]) -> bytes:
    output = io.StringIO(newline="")
    writer = csv.writer(output, lineterminator="\n")
    writer.writerow(columns)
    writer.writerows(rows)
    return output.getvalue().encode("utf-8")


def wait_until(description: str, fetch, ready, *, timeout: float, interval: float):
    deadline = time.monotonic() + timeout
    last = None
    while time.monotonic() < deadline:
        last = fetch()
        if ready(last):
            return last
        time.sleep(interval)
    raise SmokeFailure(f"Tiempo agotado esperando {description}. Ultimo estado: {last!r}")


def items(payload: Any) -> list[dict[str, Any]]:
    if (
        not isinstance(payload, dict)
        or not isinstance(payload.get("items"), list)
        or not isinstance(payload.get("total"), int)
        or payload["total"] < len(payload["items"])
    ):
        raise SmokeFailure("La lista de la API debe incluir items y total.")
    return payload["items"]


def check_error(checks: Checks, response: Response, message: str) -> None:
    error = response.json().get("error", {})
    checks.verify(
        bool(error.get("code"))
        and bool(error.get("message"))
        and "details" in error
        and bool(error.get("request_id")),
        message,
    )


def run_to_completion(api: Api, checks: Checks, created: dict, args) -> dict:
    run_id = created["id"]
    run = wait_until(
        "la ejecucion " + run_id,
        lambda: api.get("/api/v1/runs/" + run_id),
        lambda current: current["status"] in ("SUCCESS", "FAILED", "CANCELLED"),
        timeout=args.job_timeout,
        interval=args.poll_interval,
    )
    checks.verify(
        run["status"] == "SUCCESS",
        f"{run['module']}: worker termina la ejecucion correctamente ({run_id})"
        + (f"; error: {run.get('error')}" if run["status"] != "SUCCESS" else ""),
    )
    checks.verify(
        run.get("finished_at") is not None and run.get("progress_percent") == 100,
        f"{run['module']}: la ejecucion final tiene fecha y progreso completo",
    )
    return run


def check_export(api: Api, checks: Checks, run: dict) -> None:
    response = api.request("GET", f"/api/v1/runs/{run['id']}/export.xlsx")
    expected_sheets = {
        "intake": ["Resumen", "Errores", "Reglas", "Trazabilidad"],
        "recon": ["Resumen", "Resultados", "Hallazgos", "Trazabilidad"],
        "sentinel": ["Resumen", "Controles", "Hallazgos", "Trazabilidad"],
    }
    namespace = {"x": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}
    with zipfile.ZipFile(io.BytesIO(response.body)) as archive:
        workbook = ElementTree.fromstring(archive.read("xl/workbook.xml"))
        sheets = [sheet.attrib["name"] for sheet in workbook.findall("x:sheets/x:sheet", namespace)]
        worksheet_xml = [ElementTree.fromstring(archive.read(name)) for name in archive.namelist()
                         if name.startswith("xl/worksheets/sheet") and name.endswith(".xml")]
    checks.verify(
        sheets == expected_sheets[run["module"].lower()]
        and response.headers.get("Content-Type", "").split(";")[0]
        == "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        f"{run['module']}: informe Excel tiene las cuatro hojas y el Content-Type correcto",
    )
    checks.verify(
        f"trackvance_{run['module'].lower()}_{run['id']}.xlsx"
        in response.headers.get("Content-Disposition", "")
        and "attachment" in response.headers.get("Content-Disposition", "").lower(),
        f"{run['module']}: Excel se descarga con nombre seguro y run identificable",
    )
    checks.verify(
        all(not sheet.findall(".//x:c/x:f", namespace) for sheet in worksheet_xml),
        f"{run['module']}: las celdas de negocio no contienen formulas ejecutables",
    )


def exercise(args, checks: Checks) -> dict[str, str]:
    api = Api(args.base_url, args.request_timeout)
    suffix = time.strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:6]
    name = "Verificación " + suffix
    created_ids: dict[str, str] = {}
    health = api.get("/api/v1/health")
    checks.verify(
        health.get("status") == "ok" and health.get("mode") == "local-prototype",
        "API local disponible y claramente identificada como prototipo",
    )
    api.request("GET", "/api/v1/dashboard", expected=(401,))
    checks.verify(True, "Dashboard exige una sesion autenticada")
    if args.email:
        auth = api.post(
            "/api/v1/auth/login", {"email": args.email, "password": args.password}, expected=(200,)
        )
    else:
        auth = api.post("/api/v1/auth/demo", {}, expected=(200,))
    api.csrf = auth.get("csrf_token", "")
    checks.verify(
        bool(api.csrf)
        and any(cookie.name == "trackvance_session" for cookie in api.cookies),
        "Inicio de sesion devuelve cookie y token CSRF",
    )
    me = api.get("/api/v1/me")
    checks.verify(me["user"]["id"] == auth["user"]["id"], "La cookie conserva la identidad")
    token = api.csrf
    api.csrf = ""
    try:
        csrf_error = api.request(
            "POST", "/api/v1/datasets", {"name": name + " CSRF"}, expected=(403,)
        )
    finally:
        api.csrf = token
    check_error(checks, csrf_error, "Una mutacion sin CSRF devuelve un error 403 identificable")

    dashboard = api.get("/api/v1/dashboard")
    checks.verify(
        dashboard["stats"]["datasets"] >= 3
        and bool(dashboard.get("recent_runs"))
        and bool(dashboard.get("module_status")),
        "Dashboard contiene datasets, actividad y modulos demo",
    )
    demo_controls = items(api.get("/api/v1/recon/controls"))
    demo_control = next(
        (item for item in demo_controls if item["id"] == "demo-control-payments"),
        None,
    )
    checks.verify(demo_control is not None, "Hay un control Recon demo reutilizable")
    assert demo_control is not None

    dataset = api.post(
        "/api/v1/datasets",
        {
            "name": name + " - Ventas",
            "description": "Datos ficticios de verificacion automatica; conservar evidencia.",
            "domain": "Verificación",
            "owner": "Verificación automatica",
        },
    )
    dataset_id = dataset["id"]
    created_ids["dataset"] = dataset_id
    columns = ["pedido_id", "cliente_id", "producto", "fecha", "cantidad", "valor", "estado"]
    rows = [
        ["V-001", "C-001", "Producto A", "2026-01-10", 2, "100.00", "ACTIVO"],
        ["V-002", "C-002", "Producto B", "2026-01-10", 1, "-20.00", "ACTIVO"],
        ["V-003", "C-003", "Producto C", "2026-01-10", 1, "45.00", "ACTIVO"],
        ["V-004", "C-004", "Producto D", "2026-01-10", 3, "80.00", "ACTIVO"],
    ]
    contents = csv_bytes(columns, rows)
    version = api.upload(
        f"/api/v1/datasets/{dataset_id}/versions/upload",
        "Verificacion-ventas.csv",
        contents,
    )
    created_ids["input_version"] = version["id"]
    profile = api.get(f"/api/v1/dataset-versions/{version['id']}/profile")
    checks.verify(
        profile["row_count"] == 4 and profile["column_count"] == 7,
        "CSV cargado conserva las cuatro filas y siete columnas",
    )
    checks.verify(
        profile["sha256"] == hashlib.sha256(contents).hexdigest(),
        "La huella de la version coincide con el archivo original",
    )

    contract = api.post(
        "/api/v1/intake/contracts",
        {
            "name": name + " - Intake",
            "dataset_id": dataset_id,
            "config": {
                "required_columns": ["pedido_id", "cliente_id", "producto", "valor"],
                "unique_columns": ["pedido_id"],
                "numeric_columns": ["cantidad", "valor"],
                "positive_columns": ["cantidad", "valor"],
                "max_error_rate": 0,
            },
        },
    )
    intake = run_to_completion(
        api,
        checks,
        api.post(
            "/api/v1/intake/runs",
            {"contract_id": contract["id"], "dataset_version_id": version["id"]},
            expected=(202,),
        ),
        args,
    )
    created_ids["intake_run"] = intake["id"]
    metrics = intake["metrics"]
    checks.verify(
        metrics["total_rows"] == 4
        and metrics["valid_rows"] == 3
        and metrics["error_rows"] == 1
        and metrics["valid_rows"] + metrics["error_rows"] == metrics["total_rows"],
        "Intake conserva la poblacion: tres filas validas y una invalida",
    )
    checks.verify(
        intake["decision"] == "REJECTED" and metrics["decision"] == "REJECTED",
        "Intake rechaza por calidad aunque el procesamiento termina en SUCCESS",
    )
    errors = items(api.get(f"/api/v1/intake/runs/{intake['id']}/errors"))
    checks.verify(
        any(
            error.get("column") == "valor"
            and float(error.get("received_value", 0)) == -20
            and error.get("original_row_number", 0) > 0
            and error.get("rule_code")
            and error.get("message")
            for error in errors
        ),
        "La fila rechazada conserva valor, numero original y regla explicable",
    )
    checks.verify(bool(intake.get("output_version_id")), "Intake publica la version de filas aceptadas")
    accepted = api.get(f"/api/v1/dataset-versions/{intake['output_version_id']}/profile")
    checks.verify(
        accepted["row_count"] == 3
        and {row["pedido_id"] for row in accepted["sample"]} == {"V-001", "V-003", "V-004"},
        "La salida reutilizable de Intake contiene exactamente las tres filas aceptadas",
    )
    check_export(api, checks, intake)

    source = api.get("/api/v1/datasets/" + demo_control["dataset_id"])
    target = api.get("/api/v1/datasets/" + demo_control["target_dataset_id"])
    control = api.post(
        "/api/v1/recon/controls",
        {
            "name": name + " - Recon",
            "dataset_id": source["id"],
            "target_dataset_id": target["id"],
            "config": demo_control["config"],
        },
    )
    recon = run_to_completion(
        api,
        checks,
        api.post(
            "/api/v1/recon/runs",
            {
                "control_id": control["id"],
                "source_version_id": source["latest_version_id"],
                "target_version_id": target["latest_version_id"],
            },
            expected=(202,),
        ),
        args,
    )
    created_ids["recon_run"] = recon["id"]
    counts = recon["metrics"]["counts"]
    categories = (
        "MATCH", "VALUE_MISMATCH", "SOURCE_ONLY", "TARGET_ONLY", "DUPLICATE_SOURCE", "DUPLICATE_TARGET"
    )
    checks.verify(
        all(counts.get(category, 0) > 0 for category in categories),
        "Recon distingue matches, discrepancias, faltantes y duplicados en ambos lados",
    )
    checks.verify(
        sum(counts.values()) == recon["metrics"]["total_rows"],
        "Las clasificaciones Recon cubren todos sus resultados sin solaparse",
    )
    for category in categories:
        page = api.get(
            f"/api/v1/recon/runs/{recon['id']}/results?classification={category}&limit=2&offset=0"
        )
        filtered = items(page)
        checks.verify(
            0 < len(filtered) <= 2
            and all(row["classification"] == category for row in filtered)
            and page["total"] == counts[category],
            f"Recon filtra y pagina {category} con recuento coherente",
        )
        if category == "VALUE_MISMATCH":
            row = filtered[0]
            try:
                difference = abs(Decimal(str(row["source_value"])) - Decimal(str(row["target_value"])))
                tolerance = Decimal(str(row["tolerance"]))
                recorded_difference = abs(Decimal(str(row["difference"])))
            except (InvalidOperation, TypeError, KeyError) as exc:
                raise SmokeFailure("Discrepancia sin evidencia numerica interpretable.") from exc
            checks.verify(
                difference > tolerance
                and recorded_difference == difference
                and bool(row.get("source_row"))
                and bool(row.get("target_row")),
                "Detalle comparado Recon prueba la diferencia y tolerancia con ambas filas",
            )
    check_export(api, checks, recon)

    findings = recon.get("findings", [])
    checks.verify(bool(findings), "Recon produce hallazgos vinculados a la ejecucion")
    checks.verify(
        all(not finding.get("exception_id") for finding in findings),
        "Los hallazgos nuevos no crean excepciones automaticamente",
    )
    exception = api.post(f"/api/v1/findings/{findings[0]['id']}/exceptions", {})
    created_ids["exception"] = exception["id"]
    checks.verify(
        exception["run_id"] == recon["id"] and exception["state"] == "OPEN",
        "La conversion explicita conserva el enlace de excepcion a su ejecucion",
    )
    repeated_exception = api.post(f"/api/v1/findings/{findings[0]['id']}/exceptions", {})
    checks.verify(
        repeated_exception["id"] == exception["id"]
        and repeated_exception["version"] == exception["version"],
        "Repetir la conversion de un hallazgo reutiliza la misma excepcion",
    )
    old_version = exception["version"]
    updated = api.request(
        "PATCH",
        f"/api/v1/exceptions/{exception['id']}",
        {
            "version": old_version,
            "state": "INVESTIGATING",
            "owner": "Verificación automatica",
            "comment": "Verificación: revisar diferencia con evidencia del run.",
        },
    ).json()
    checks.verify(
        updated["state"] == "INVESTIGATING"
        and updated["version"] > old_version
        and any(
            event.get("from_state") == "OPEN"
            and event.get("to_state") == "INVESTIGATING"
            and event.get("actor")
            and event.get("timestamp")
            for event in updated["events"]
        ),
        "La transicion de excepcion registra actor, momento y nueva version",
    )
    stale_error = api.request(
        "PATCH",
        f"/api/v1/exceptions/{exception['id']}",
        {"version": old_version, "state": "WAITING_EXTERNAL", "owner": "Verificación obsoleta"},
        expected=(409,),
    )
    check_error(checks, stale_error, "El conflicto 409 incluye codigo y referencia de solicitud")
    after_conflict = api.get(f"/api/v1/exceptions/{exception['id']}")
    checks.verify(
        after_conflict["state"] == "INVESTIGATING"
        and after_conflict["version"] == updated["version"]
        and after_conflict["owner"] == "Verificación automatica",
        "Una version obsoleta devuelve 409 y conserva el cambio vigente",
    )
    api.request(
        "PATCH",
        f"/api/v1/exceptions/{exception['id']}",
        {"version": updated["version"], "state": "RESOLVED"},
        expected=(422,),
    )
    after_validation = api.get(f"/api/v1/exceptions/{exception['id']}")
    checks.verify(
        after_validation["state"] == "INVESTIGATING"
        and after_validation["version"] == updated["version"],
        "La excepcion no puede resolverse sin causa raiz y resolucion",
    )
    resolved = api.request(
        "PATCH",
        f"/api/v1/exceptions/{exception['id']}",
        {
            "version": updated["version"],
            "state": "RESOLVED",
            "root_cause": "Diferencia ficticia introducida por el conjunto de demostracion.",
            "resolution": "Evidencia contrastada durante la verificacion automatica.",
            "comment": "Cierre de la excepcion de prueba con explicacion conservada.",
        },
    ).json()
    checks.verify(
        resolved["state"] == "RESOLVED"
        and resolved["root_cause"]
        and resolved["resolution"]
        and len(resolved["events"]) == len(updated["events"]) + 1,
        "La excepcion se resuelve con causa, resolucion e historia completa",
    )

    monitor = api.post(
        "/api/v1/monitors",
        {
            "name": name + " - Sentinel",
            "dataset_id": dataset_id,
            "config": {
                "required_columns": ["pedido_id", "cliente_id"],
                "null_columns": ["pedido_id"],
                "max_null_rate": 0.05,
                "max_volume_change_pct": 15,
                "max_age_hours": 48,
            },
        },
    )
    created_ids["monitor"] = monitor["id"]
    first = run_to_completion(
        api,
        checks,
        api.post(
            f"/api/v1/monitors/{monitor['id']}/runs",
            {"dataset_version_id": version["id"]},
            expected=(202,),
        ),
        args,
    )
    checks.verify(
        first["metrics"]["row_count"] == 4
        and first["metrics"]["failed_checks"] == 0
        and first["metrics"]["health_score"] == 100,
        "La primera observacion Sentinel establece una referencia saludable",
    )
    history_before = items(api.get(f"/api/v1/monitors/{monitor['id']}/metrics"))
    anomalous = api.upload(
        f"/api/v1/datasets/{dataset_id}/versions/upload",
        "Verificacion-anomalia.csv",
        csv_bytes(
            ["pedido_id", "producto", "fecha", "cantidad", "valor", "estado"],
            [["", "Producto E", "2026-01-10", 1, "10.00", "ACTIVO"]],
        ),
    )
    sentinel = run_to_completion(
        api,
        checks,
        api.post(
            f"/api/v1/monitors/{monitor['id']}/runs",
            {"dataset_version_id": anomalous["id"]},
            expected=(202,),
        ),
        args,
    )
    created_ids["sentinel_run"] = sentinel["id"]
    history_after = items(api.get(f"/api/v1/monitors/{monitor['id']}/metrics"))
    history_by_run = {point["run_id"]: point for point in history_after}
    checks.verify(
        len(history_after) == len(history_before) + 1
        and history_by_run[first["id"]]["row_count"] == 4
        and history_by_run[sentinel["id"]]["row_count"] == 1,
        "Sentinel agrega historia y conserva las observaciones previas",
    )
    sentinel_metrics = sentinel["metrics"]
    failing = [check for check in sentinel_metrics["checks"] if check["status"] == "FAIL"]
    checks.verify(
        sentinel_metrics["row_count"] == 1
        and sentinel_metrics["failed_checks"] == 3
        and len(failing) == sentinel_metrics["failed_checks"],
        "Sentinel detecta problemas del dataset con caida de volumen, nulos y schema",
    )
    checks.verify(
        all("actual" in check and "expected" in check and check.get("message") for check in failing),
        "Cada alerta Sentinel explica valor observado y condicion esperada",
    )
    volume_check = next(
        (check for check in failing if check["code"] == "VOLUME_CHANGE"), {}
    )
    checks.verify(
        volume_check.get("actual") == 75
        and volume_check.get("expected", {}).get("baseline_rows") == 4
        and sentinel_metrics["null_rate"] == 1,
        "Sentinel compara contra las cuatro filas previas y mide caida del 75% y nulos del 100%",
    )
    checks.verify(
        bool(sentinel.get("findings"))
        and all(not finding.get("exception_id") for finding in sentinel["findings"]),
        "Sentinel conserva hallazgos para conversion voluntaria en excepcion",
    )
    original_again = api.get(f"/api/v1/dataset-versions/{version['id']}/profile")
    checks.verify(
        original_again["sha256"] == profile["sha256"] and original_again["row_count"] == 4,
        "Una carga posterior no sobrescribe la version original",
    )
    intake_again = api.get(f"/api/v1/runs/{intake['id']}")
    checks.verify(intake_again["metrics"] == intake["metrics"], "Los resultados historicos Intake permanecen inmutables")

    for run in (intake, recon, sentinel):
        manifest = api.request("GET", f"/api/v1/runs/{run['id']}/evidence").json()
        checks.verify(
            manifest.get("run_id") == run["id"],
            f"{run['module']}: manifiesto descargable referencia la ejecucion correcta",
        )
        expected_inputs = {run["dataset_version_id"]}
        if run.get("target_version_id"):
            expected_inputs.add(run["target_version_id"])
        manifest_inputs = manifest.get("inputs", [])
        checks.verify(
            {entry.get("dataset_version_id") for entry in manifest_inputs} == expected_inputs
            and all(
                entry.get("artifact_sha256")
                == api.get(f"/api/v1/dataset-versions/{entry['dataset_version_id']}/profile")["sha256"]
                for entry in manifest_inputs
            )
            and manifest.get("metrics") == run["metrics"],
            f"{run['module']}: evidencia conserva hashes de entradas y metricas verificables",
        )
    check_export(api, checks, sentinel)
    audit = items(api.get("/api/v1/audit-events"))
    checks.verify(
        {intake["id"], recon["id"], sentinel["id"], exception["id"]}
        <= {event.get("subject_id") for event in audit}
        and all(
            event.get("actor") and event.get("created_at") and event.get("event_type")
            for event in audit if event.get("subject_id") in set(created_ids.values())
        ),
        "La auditoria conserva ejecuciones y excepcion con actor, momento y tipo de accion",
    )
    api.post("/api/v1/auth/logout", {}, expected=(200,))
    api.request("GET", "/api/v1/me", expected=(401,))
    checks.verify(True, "Cerrar sesion revoca su acceso")
    return created_ids


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default="http://127.0.0.1:8000", help="URL base, sin /api/v1")
    parser.add_argument("--request-timeout", type=float, default=30.0, help="Timeout de una peticion, segundos")
    parser.add_argument("--job-timeout", type=float, default=90.0, help="Espera maxima por ejecucion, segundos")
    parser.add_argument("--poll-interval", type=float, default=0.5, help="Intervalo de consulta al worker, segundos")
    parser.add_argument("--email", help="Usar login local en lugar de sesion demo")
    parser.add_argument("--password", help="Contrasena del usuario local; omitir para demo")
    args = parser.parse_args()
    if bool(args.email) != bool(args.password):
        parser.error("--email y --password deben usarse juntos")
    if min(args.request_timeout, args.job_timeout, args.poll_interval) <= 0:
        parser.error("Los tiempos deben ser mayores que cero")
    checks = Checks()
    started = time.monotonic()
    try:
        created_ids = exercise(args, checks)
    except (SmokeFailure, KeyError, ValueError, TypeError) as exc:
        print(f"[FALLO] {exc}", file=sys.stderr, flush=True)
        print(f"Verificaciones completadas: {len(checks.completed)}. Datos creados conservados.", file=sys.stderr)
        return 1
    print(f"\nVerificacion completa: {len(checks.completed)} comprobaciones en {time.monotonic() - started:.1f}s.")
    print("Datos y evidencia conservados (no se elimino ni reinicio informacion):")
    print(json.dumps(created_ids, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
