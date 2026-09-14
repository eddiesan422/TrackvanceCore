"""Local health diagnostics. Never prints database URLs, credentials or environment values."""

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


def command_check(arguments: list[str], timeout: int = 15) -> tuple[bool, str]:
    try:
        result = subprocess.run(arguments, capture_output=True, text=True, timeout=timeout,
                                check=False)
    except (FileNotFoundError, subprocess.TimeoutExpired) as error:
        return False, type(error).__name__
    return result.returncode == 0, "OK" if result.returncode == 0 else "No disponible"


def inspect_health(base_url: str) -> dict:
    results = {}
    try:
        with urllib.request.urlopen(base_url.rstrip("/") + "/api/v1/health/ready", timeout=10) as response:
            payload = json.load(response)
        results["API"] = payload.get("status") == "ready"
        results["Base de datos"] = payload.get("database") == "ready"
        results["Almacenamiento"] = payload.get("storage") == "ready"
        results["Migraciones Alembic"] = payload.get("migrations") == "head"
    except (OSError, ValueError, urllib.error.URLError):
        results["API / base de datos / almacenamiento / migraciones"] = False
    return results


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default="http://127.0.0.1:3000")
    parser.add_argument("--docker", action="store_true", help="Comprueba Docker y Compose.")
    parser.add_argument("--storage-dir", type=Path, help="Directorio local de artifacts/heartbeat.")
    options = parser.parse_args()
    checks = inspect_health(options.base_url)
    if options.docker:
        checks["Docker Linux disponible"] = command_check(["docker", "info", "--format", "{{.OSType}}"])[0]
        checks["Configuración Compose"] = command_check(["docker", "compose", "config", "--quiet"])[0]
        checks["Worker Compose"] = command_check([
            "docker", "compose", "exec", "-T", "worker", "python", "-c",
            ("from trackvance.worker import worker_status; "
             "raise SystemExit(0 if worker_status()['status'] == 'RUNNING' else 1)"),
        ])[0]
    if options.storage_dir:
        root = options.storage_dir.resolve(strict=True)
        try:
            with tempfile.TemporaryFile(dir=root) as probe:
                probe.write(b"trackvance-doctor")
            checks["Directorio local escribible"] = True
            free = shutil.disk_usage(root).free
            checks["Disco local (al menos 64 MiB libres)"] = free >= 64 * 1024 * 1024
            heartbeat = json.loads((root / "worker-heartbeat.json").read_text(encoding="utf-8"))
            age = (datetime.now(UTC) - datetime.fromisoformat(heartbeat["last_seen"])).total_seconds()
            checks["Worker local (heartbeat reciente)"] = 0 <= age < 30
        except (OSError, ValueError, KeyError):
            checks["Directorio/worker local"] = False
    for label, passed in checks.items():
        print(f"{'OK' if passed else 'FAIL'}  {label}")
    return 0 if all(checks.values()) else 1


if __name__ == "__main__":
    sys.exit(main())
