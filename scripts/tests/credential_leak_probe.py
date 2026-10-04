"""Check ephemeral test credentials using stdin only and sanitized boolean results.

Never target the user's installation. Call from Playwright while issued secrets
are still in test-process memory; no credential file or command argument is used.
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
ARTIFACT_SCAN = """
import json, sys
from pathlib import Path
secrets = [value.encode() for value in json.load(sys.stdin)['secrets']]
files = [path for path in Path('/var/lib/trackvance').rglob('*') if path.is_file()]
leaked = any(any(value in path.read_bytes() for value in secrets) for path in files)
print(json.dumps({'status': 'FAIL' if leaked else 'PASS', 'files_scanned': len(files)}))
"""


def validated_project(value: str) -> str:
    if not re.fullmatch(r"trackvance-(?:v070-test-[a-z0-9-]+-[a-f0-9]{12}|(?:identity-e2e|e2e|connections-e2e|delivery-e2e|ci)(?:-[a-z0-9-]+)?)", value):
        raise ValueError("Se requiere un proyecto desechable de certificación.")
    return value


def execute(arguments: list[str], *, input_text: str | None = None) -> str:
    result = subprocess.run(arguments, cwd=ROOT, input=input_text, capture_output=True,
                            text=True, encoding="utf-8", errors="replace", check=False)
    if result.returncode:
        raise RuntimeError("No se pudo completar una comprobación de privacidad.")
    return result.stdout + result.stderr


def scan(project: str, secrets: list[str]) -> dict:
    project = validated_project(project)
    if not secrets or any(not isinstance(value, str) or len(value) < 12 for value in secrets):
        raise ValueError("Se requieren credenciales de prueba no vacías por stdin.")
    containers: dict[str, str] = {}
    names = execute(["docker", "ps", "--filter", f"label=com.docker.compose.project={project}",
                     "--format", '{{.ID}} {{.Label "com.docker.compose.service"}}'])
    for line in names.splitlines():
        identifier, service = line.split(maxsplit=1)
        containers[service] = identifier
    if not {"api", "postgres"}.issubset(containers):
        raise RuntimeError("El proyecto de prueba no tiene API y PostgreSQL activos.")
    dump = execute(["docker", "exec", containers["postgres"], "sh", "-c",
                    'pg_dump --data-only --username="$POSTGRES_USER" --dbname="$POSTGRES_DB"'])
    if any(value in dump for value in secrets):
        raise RuntimeError("Se detectó persistencia de una credencial temporal.")
    del dump
    for identifier in containers.values():
        logs = execute(["docker", "logs", identifier])
        if any(value in logs for value in secrets):
            raise RuntimeError("Se detectó una credencial en logs de contenedor.")
    artifact_result = json.loads(execute(
        ["docker", "exec", "-i", containers["api"], "python", "-c", ARTIFACT_SCAN],
        input_text=json.dumps({"secrets": secrets}),
    ))
    if artifact_result.get("status") != "PASS":
        raise RuntimeError("Se detectó una credencial en artifacts persistidos.")
    browser_files = 0
    for folder in (ROOT / ".codex-local/browser-results" / project, ROOT / "frontend/test-results"):
        for path in folder.rglob("*"):
            if path.is_file():
                browser_files += 1
                contents = path.read_bytes()
                if any(value.encode() in contents for value in secrets):
                    raise RuntimeError("Se detectó una credencial en evidencia de navegador.")
    return {"status": "PASS", "database": "PASS", "container_logs": "PASS",
            "artifacts": "PASS", "artifact_files_scanned": artifact_result["files_scanned"],
            "browser_files_scanned": browser_files, "secrets_scanned": len(secrets)}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project", required=True)
    args = parser.parse_args()
    try:
        payload = json.load(sys.stdin)
        result = scan(args.project, payload["secrets"])
    except (OSError, ValueError, KeyError, RuntimeError, subprocess.SubprocessError):
        # Raw subprocess/errors/input can contain credentials; do not interpolate them.
        print(json.dumps({"status": "FAIL", "error": "CREDENTIAL_PRIVACY_PROBE_FAILED"}))
        return 1
    print(json.dumps(result))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
