"""Retain only sanitized browser counters in publishable certification evidence."""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
from pathlib import Path


def credential_privacy_reports(report: dict) -> list[dict]:
    """Accept only a fixed aggregate schema; never retain arbitrary child text."""
    prefix = "CREDENTIAL_PRIVACY_PROBE "
    statuses = {"status", "database", "container_logs", "artifacts"}
    counts = {"secrets_scanned", "artifact_files_scanned", "browser_files_scanned"}
    records = []

    def inspect(suite):
        for spec in suite.get("specs", []):
            for test in spec.get("tests", []):
                for attempt in test.get("results", []):
                    for entry in attempt.get("stdout", []):
                        for line in entry.get("text", "").splitlines():
                            if not line.startswith(prefix):
                                continue
                            try:
                                value = json.loads(line[len(prefix):])
                            except (ValueError, TypeError) as error:
                                raise RuntimeError("Agregado de privacidad inválido; contenido omitido.") from error
                            if (not isinstance(value, dict) or set(value) != statuses | counts
                                    or any(type(value[key]) is not str or value[key] not in {"PASS", "FAIL"}
                                           for key in statuses)
                                    or any(type(value[key]) is not int or value[key] < 0 for key in counts)):
                                raise RuntimeError("Agregado de privacidad inválido; contenido omitido.")
                            records.append({key: value[key] for key in sorted(statuses | counts)})
        for nested in suite.get("suites", []):
            inspect(nested)

    for suite in report.get("suites", []):
        inspect(suite)
    return records


def run_browser(pnpm: str, arguments: list[str], *, root: Path, project: str,
                environment: dict[str, str], evidence: Path) -> dict:
    raw = root / ".codex-local" / "browser-results" / project
    raw.mkdir(parents=True, exist_ok=True)
    completed = subprocess.run(
        [pnpm, "exec", "playwright", "test", *arguments, "--reporter=json", "--output", str(raw)],
        cwd=root / "frontend", env={**environment, "TV_E2E_PRIVATE_ARTIFACTS": "1"},
        text=True, encoding="utf-8", errors="replace", capture_output=True, check=False,
    )
    try:
        report = json.loads(completed.stdout[completed.stdout.index("{"):])
    except (ValueError, TypeError) as error:
        raise RuntimeError("Playwright no devolvió un informe JSON válido; salida omitida por privacidad.") from error
    summary = {"status": "PASS" if not completed.returncode else "FAIL", **report.get("stats", {})}
    probes = credential_privacy_reports(report)
    privacy_failed = any(value != "PASS" for probe in probes for key, value in probe.items()
                         if key in {"status", "database", "container_logs", "artifacts"})
    if privacy_failed:
        summary["status"] = "FAIL"
    if probes:
        summary["credential_privacy_probes"] = probes
    failures = []
    def inspect(suite):
        for spec in suite.get("specs", []):
            for test in spec.get("tests", []):
                if test.get("status") == "unexpected":
                    for attempt in test.get("results", []):
                        failures.append({"test": spec.get("title"), "status": attempt.get("status"),
                                         "location": attempt.get("error", {}).get("location", {})})
        for nested in suite.get("suites", []):
            inspect(nested)
    for suite in report.get("suites", []):
        inspect(suite)
    if failures:
        summary["failures"] = failures
    # Issued user credentials are scanned inside Playwright using a stdin-only
    # probe while still in memory; they are never exported to this reporter.
    sensitive = []
    sensitive.extend(environment[key] for key in (
        "TV_CONNECTIONS_PASSWORD", "POSTGRES_PASSWORD", "SOURCE_POSTGRES_PASSWORD",
        "SOURCE_MSSQL_SA_PASSWORD", "TV_DELIVERY_PASSWORD", "DELIVERY_POSTGRES_ADMIN_PASSWORD",
        "DELIVERY_MSSQL_SA_PASSWORD", "MOCK_OIDC_CLIENT_SECRET")
                     if environment.get(key))
    content = json.dumps(summary, ensure_ascii=False)
    if any(secret in content for secret in sensitive):
        raise RuntimeError("Se detectó una credencial en el resumen; no se publicó.")
    for artifact in raw.rglob("*"):
        if artifact.is_file() and any(secret.encode() in artifact.read_bytes() for secret in sensitive):
            raise RuntimeError("Se detectó una credencial en un artefacto local del navegador.")
    summary["artifact_credentials_scan"] = "PASS"
    summary["raw_artifacts_published"] = False
    (evidence / "browser-summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
    print("Playwright: " + json.dumps(summary, ensure_ascii=True), flush=True)
    if completed.returncode:
        raise RuntimeError("Playwright falló; consultar únicamente las ubicaciones saneadas del resumen.")
    if privacy_failed:
        raise RuntimeError("La comprobación de privacidad de credenciales falló.")
    return summary


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project", required=True)
    parser.add_argument("--evidence-dir", type=Path, required=True)
    options = parser.parse_args()
    root = Path(__file__).resolve().parents[2]
    pnpm = shutil.which("pnpm")
    if not pnpm:
        raise RuntimeError("pnpm no está disponible.")
    options.evidence_dir.mkdir(parents=True, exist_ok=True)
    run_browser(pnpm, [], root=root, project=options.project,
                environment=dict(os.environ), evidence=options.evidence_dir)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
