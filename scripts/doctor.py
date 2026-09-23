"""Local health and recovery diagnostics without printing configuration or secrets."""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def command_check(arguments: list[str], timeout: int = 15) -> tuple[bool, str]:
    try:
        result = subprocess.run(
            arguments, capture_output=True, text=True, timeout=timeout, check=False, cwd=ROOT
        )
    except (FileNotFoundError, subprocess.TimeoutExpired) as error:
        return False, type(error).__name__
    return result.returncode == 0, "OK" if result.returncode == 0 else "No disponible"


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


def _compose_prefix(project: str | None) -> list[str]:
    prefix = ["docker", "compose"]
    if project:
        prefix.extend(["-p", project])
    prefix.extend(["-f", str(ROOT / "compose.yml")])
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
    except (ImportError, KeyError, ValueError):
        return {
            "Montajes de recuperación": False,
            "Huella/linaje/SecretStore": False,
            "Espacio de recuperación": False,
        }

    checks = {
        "Montajes de recuperación": mounts_ok and worker_ok and delivery_worker_ok
    }
    verify = ROOT / "scripts" / "verify_storage.py"
    copied = command_check(
        ["docker", "cp", str(verify), f"{api['id']}:/tmp/verify_storage.py"], timeout=30
    )[0]
    snapshot_ok = copied and command_check(
        ["docker", "exec", api["id"], "python", "/tmp/verify_storage.py", "snapshot"],
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
        for lane in ("default", "delivery"):
            heartbeat = json.loads(
                (resolved / f"worker-heartbeat-{lane}.json").read_text(encoding="utf-8")
            )
            age = (
                datetime.now(UTC) - datetime.fromisoformat(heartbeat["last_seen"])
            ).total_seconds()
            checks[f"Worker local {lane.upper()} (heartbeat reciente)"] = (
                heartbeat.get("lane") == lane.upper() and 0 <= age < 30
            )
    except (OSError, ValueError, KeyError):
        checks["Directorio/worker local"] = False
    return checks


def parse_arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default="http://127.0.0.1:3000")
    parser.add_argument("--docker", action="store_true", help="Comprueba Docker y Compose.")
    parser.add_argument("--project", help="Proyecto Compose explícito para diagnóstico.")
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
    if options.docker:
        prefix = _compose_prefix(options.project)
        checks["Docker Linux disponible"] = command_check(
            ["docker", "info", "--format", "{{.OSType}}"]
        )[0]
        checks["Configuración Compose"] = command_check([*prefix, "config", "--quiet"])[0]
        checks["Worker Compose DEFAULT"] = command_check(
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
        )[0]
        checks["Worker Compose DELIVERY"] = command_check(
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
        )[0]
    if options.storage_dir:
        checks.update(local_storage_checks(options.storage_dir))
    if options.recovery_ready:
        checks.update(recovery_checks(options.project, options.min_free_mib))
    if options.as_json:
        print(json.dumps({"checks": checks, "ok": all(checks.values())}, sort_keys=True))
    else:
        for label, passed in checks.items():
            print(f"{'OK' if passed else 'FAIL'}  {label}")
    return 0 if all(checks.values()) else 1


if __name__ == "__main__":
    sys.exit(main())
