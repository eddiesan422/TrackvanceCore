"""Local health and recovery diagnostics without printing configuration or secrets."""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def command_check(arguments: list[str], timeout: float = 15) -> tuple[bool, dict[str, object]]:
    started = time.monotonic()
    exit_code: int | None = None
    try:
        result = subprocess.run(
            arguments, capture_output=True, text=True, timeout=timeout, check=False, cwd=ROOT
        )
        exit_code = result.returncode
        reason = "OK" if exit_code == 0 else "EXIT_CODE"
    except FileNotFoundError:
        reason = "EXECUTABLE_NOT_FOUND"
    except subprocess.TimeoutExpired:
        reason = "TIMEOUT"
    # Arguments, output, exception messages and environment may contain secrets.
    # Retain only the measured duration and fixed diagnostic codes.
    return reason == "OK", {
        "reason": reason,
        "duration_seconds": round(time.monotonic() - started, 3),
        "timeout_seconds": timeout,
        "exit_code": exit_code,
    }


def inspect_health(base_url: str) -> dict[str, bool]:
    results: dict[str, bool] = {}
    try:
        endpoint = base_url.rstrip("/") + "/api/v1/health/ready"
        with urllib.request.urlopen(endpoint, timeout=10) as response:
            payload = json.load(response)
        results["API"] = payload.get("status") == "ready"
        results["Base de datos"] = payload.get("database") == "ready"
        results["Almacenamiento"] = payload.get("storage") == "ready"
        results["Migraciones Alembic"] = payload.get("migrations") == "head"
    except (OSError, ValueError, urllib.error.URLError):
        results["API / base de datos / almacenamiento / migraciones"] = False
    return results


def _compose_prefix(project: str | None, context: Path | None = None) -> list[str]:
    prefix = ["docker", "compose"]
    if context:
        if context.resolve().is_relative_to((ROOT / ".codex-local" / "v080").resolve()):
            import certification_v080

            directory, resolved = certification_v080.load_context(context)
            certification_v080.preflight(directory, resolved)
        else:
            import certification_v070

            directory, resolved = certification_v070.load_context(context)
        if project != resolved["project"]:
            raise ValueError("El diagnóstico debe usar el proyecto exacto del contexto desechable.")
        prefix.extend(["--env-file", str(directory / "test.env")])
    if project:
        prefix.extend(["-p", project])
    prefix.extend(["-f", str(ROOT / "compose.yml")])
    if context:
        prefix.extend(["-f", str(directory / "compose.json")])
    return prefix


def _inspect_mounts(container_id: str) -> dict[str, str]:
    try:
        result = subprocess.run(
            ["docker", "container", "inspect", container_id],
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
        if result.returncode:
            return {}
        inspected = json.loads(result.stdout)[0]
        return {
            item["Destination"]: item.get("Name", "")
            for item in inspected.get("Mounts", [])
            if item.get("Type") == "volume"
        }
    except (OSError, ValueError, KeyError, subprocess.TimeoutExpired):
        return {}


def recovery_checks(project: str, min_free_mib: int) -> dict[str, bool]:
    """Check recoverability without emitting secret references, paths or values."""

    try:
        import docker_state

        state = docker_state.inventory(project)
        services = {item["service"]: item for item in state["containers"]}
        volumes = {item["logical_name"]: item["name"] for item in state["volumes"]}
        api = services["api"]
        worker = services["worker"]
        delivery_worker = services["delivery-worker"]
        acquisition_worker = services["acquisition-worker"]
        report_worker = services["report-worker"]
        metadata_components = [services[name] for name in ("scheduler", "events-notifications", "events-chaining")]
        source_mounts = {
            "/var/lib/trackvance": volumes["trackvance_data"],
            "/var/lib/trackvance-credentials": volumes["connection_credentials"],
            "/var/lib/trackvance-keys": volumes["connection_keys"],
        }
        delivery_mounts = {
            "/var/lib/trackvance-delivery-credentials": volumes["delivery_credentials"],
            "/var/lib/trackvance-delivery-keys": volumes["delivery_keys"],
        }
        api_mounts = _inspect_mounts(api["id"])
        worker_mounts = _inspect_mounts(worker["id"])
        delivery_worker_mounts = _inspect_mounts(delivery_worker["id"])
        acquisition_worker_mounts = _inspect_mounts(acquisition_worker["id"])
        report_worker_mounts = _inspect_mounts(report_worker["id"])
        mounts_ok = all(
            api_mounts.get(path) == volume
            for path, volume in {**source_mounts, **delivery_mounts}.items()
        )
        worker_ok = (
            worker_mounts.get("/var/lib/trackvance") == volumes["trackvance_data"]
            and all(
                path not in worker_mounts
                for path in source_mounts | delivery_mounts
                if path != "/var/lib/trackvance"
            )
        )
        delivery_worker_ok = (
            delivery_worker_mounts.get("/var/lib/trackvance") == volumes["trackvance_data"]
            and all(
                delivery_worker_mounts.get(path) == volume
                for path, volume in delivery_mounts.items()
            )
            and all(
                path not in delivery_worker_mounts
                for path in source_mounts
                if path != "/var/lib/trackvance"
            )
        )
        acquisition_worker_ok = (all(acquisition_worker_mounts.get(path) == volume for path, volume in source_mounts.items())
            and set(acquisition_worker_mounts.values()) <= set(source_mounts.values()))
        metadata_ok = all(_inspect_mounts(item["id"]) == {"/var/lib/trackvance": volumes["trackvance_data"]}
                          for item in metadata_components)
        worker_ok = worker_ok and set(worker_mounts.values()) <= {volumes["trackvance_data"]}
        report_worker_ok = report_worker_mounts == {"/var/lib/trackvance": volumes["trackvance_data"]}
        delivery_worker_ok = delivery_worker_ok and set(delivery_worker_mounts.values()) <= {volumes["trackvance_data"], *delivery_mounts.values()}
    except (ImportError, KeyError, ValueError, RuntimeError):
        return {
            "Montajes de recuperación": False,
            "Huella/linaje/SecretStore": False,
            "Espacio de recuperación": False,
        }

    checks = {
        "Montajes de recuperación": mounts_ok and worker_ok and delivery_worker_ok and acquisition_worker_ok and report_worker_ok and metadata_ok
    }
    copied = True
    for filename in ("verify_storage.py", "physical_schema_guard.py"):
        copied = command_check(
            ["docker", "cp", str(ROOT / "scripts" / filename), f"{api['id']}:/tmp/{filename}"], timeout=30
        )[0] and copied
    snapshot_ok = copied and command_check(
        ["docker", "exec", api["id"], "python", "-c", docker_state.snapshot_bootstrap(), "snapshot"],
        timeout=300,
    )[0]
    checks["Huella/linaje/SecretStore"] = snapshot_ok
    probe = (
        "import shutil,sys;"
        f"sys.exit(0 if shutil.disk_usage('/var/lib/trackvance').free >= {min_free_mib * 1024 * 1024} else 1)"
    )
    checks["Espacio de recuperación"] = command_check(
        ["docker", "exec", api["id"], "python", "-c", probe]
    )[0]
    return checks


def local_storage_checks(root: Path) -> dict[str, bool]:
    checks: dict[str, bool] = {}
    try:
        resolved = root.resolve(strict=True)
        with tempfile.TemporaryFile(dir=resolved) as probe:
            probe.write(b"trackvance-doctor")
        checks["Directorio local escribible"] = True
        checks["Disco local (al menos 64 MiB libres)"] = (
            shutil.disk_usage(resolved).free >= 64 * 1024 * 1024
        )
        for lane in ("default", "delivery", "acquisition", "report"):
            heartbeat = json.loads(
                (resolved / f"worker-heartbeat-{lane}.json").read_text(encoding="utf-8")
            )
            age = (
                datetime.now(UTC) - datetime.fromisoformat(heartbeat["last_seen"])
            ).total_seconds()
            checks[f"Worker local {lane.upper()} (heartbeat reciente)"] = (
                heartbeat.get("lane") == lane.upper() and 0 <= age < 30
            )
        for component in ("scheduler", "events-notifications", "events-chaining"):
            heartbeat = json.loads((resolved / f"{component}-heartbeat.json").read_text(encoding="utf-8"))
            age = (datetime.now(UTC) - datetime.fromisoformat(heartbeat["updated_at"])).total_seconds()
            checks[f"Componente local {component} (heartbeat reciente)"] = 0 <= age < 30
    except (OSError, ValueError, KeyError):
        checks["Directorio/worker local"] = False
    return checks


def parse_arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default="http://127.0.0.1:3000")
    parser.add_argument("--docker", action="store_true", help="Comprueba Docker y Compose.")
    parser.add_argument("--project", help="Proyecto Compose explícito para diagnóstico.")
    parser.add_argument("--certification-context", type=Path, help="Contexto desechable explícito con env-file y override privados.")
    parser.add_argument("--storage-dir", type=Path, help="Artifacts/heartbeat del modo local.")
    parser.add_argument(
        "--recovery-ready",
        action="store_true",
        help="Valida huella, secretos, linaje, espacio y aislamiento de montajes.",
    )
    parser.add_argument("--min-free-mib", type=int, default=64)
    parser.add_argument("--json", action="store_true", dest="as_json")
    return parser.parse_args()


def main() -> int:
    options = parse_arguments()
    if options.recovery_ready and (not options.docker or not options.project):
        print("ERROR: --recovery-ready requiere --docker y --project.", file=sys.stderr)
        return 2
    if options.min_free_mib < 1:
        print("ERROR: --min-free-mib debe ser positivo.", file=sys.stderr)
        return 2
    checks = inspect_health(options.base_url)
    command_diagnostics: dict[str, dict[str, object]] = {}

    def checked(label: str, arguments: list[str]) -> None:
        passed, diagnostic = command_check(arguments)
        checks[label] = passed
        command_diagnostics[label] = diagnostic

    if options.docker:
        try:
            prefix = _compose_prefix(options.project, options.certification_context)
        except (OSError, ValueError, RuntimeError) as error:
            print(f"ERROR: contexto de diagnóstico inválido ({type(error).__name__}).", file=sys.stderr)
            return 2
        checked("Docker Linux disponible",
            ["docker", "info", "--format", "{{.OSType}}"]
        )
        checked("Configuración Compose", [*prefix, "config", "--quiet"])
        checked("Worker Compose DEFAULT",
            [
                *prefix,
                "exec",
                "-T",
                "worker",
                "python",
                "-c",
                (
                    "from trackvance.worker import worker_status; "
                    "raise SystemExit(0 if worker_status('DEFAULT')['status'] == 'RUNNING' else 1)"
                ),
            ]
        )
        checked("Worker Compose DELIVERY",
            [
                *prefix,
                "exec",
                "-T",
                "delivery-worker",
                "python",
                "-c",
                (
                    "from trackvance.worker import worker_status; "
                    "raise SystemExit(0 if worker_status('DELIVERY')['status'] == 'RUNNING' else 1)"
                ),
            ]
        )
        checked("Worker Compose ACQUISITION", [*prefix, "exec", "-T", "acquisition-worker", "python", "-c",
            "from trackvance.worker import worker_status; raise SystemExit(0 if worker_status('ACQUISITION')['status'] == 'RUNNING' else 1)"])
        checked("Worker Compose REPORT", [*prefix, "exec", "-T", "report-worker", "python", "-c",
            "from trackvance.worker import worker_status; raise SystemExit(0 if worker_status('REPORT')['status'] == 'RUNNING' else 1)"])
        for component in ("scheduler", "events-notifications", "events-chaining"):
            checked(f"Componente Compose {component}", [*prefix, "exec", "-T", component, "python", "-c",
                f"from trackvance.component_health import component_status; raise SystemExit(0 if component_status('{component}') == 'RUNNING' else 1)"])
    if options.storage_dir:
        checks.update(local_storage_checks(options.storage_dir))
    if options.recovery_ready:
        checks.update(recovery_checks(options.project, options.min_free_mib))
    if options.as_json:
        print(json.dumps({"checks": checks, "ok": all(checks.values()),
                          "command_diagnostics": command_diagnostics}, sort_keys=True))
    else:
        for label, passed in checks.items():
            detail = command_diagnostics.get(label)
            suffix = ""
            if not passed and detail:
                suffix = (f" ({detail['reason']}; duration_seconds={detail['duration_seconds']}; "
                          f"timeout_seconds={detail['timeout_seconds']}; exit_code={detail['exit_code']})")
            print(f"{'OK' if passed else 'FAIL'}  {label}{suffix}")
    return 0 if all(checks.values()) else 1


if __name__ == "__main__":
    sys.exit(main())
