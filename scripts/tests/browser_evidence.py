"""Retain only sanitized browser counters in publishable certification evidence."""
from __future__ import annotations

import json
import re
import subprocess
import urllib.request
from pathlib import Path


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
    # Mail bodies remain in memory. Neither raw reports nor credentials are published.
    sensitive = []
    mailbox_url = environment.get("TV_MAILPIT_URL")
    if mailbox_url:
        with urllib.request.urlopen(mailbox_url + "/api/v1/messages", timeout=10) as response:
            messages = json.load(response).get("messages", [])
        for message in messages:
            with urllib.request.urlopen(mailbox_url + "/api/v1/message/" + message["ID"], timeout=10) as response:
                body = json.load(response).get("Text", "")
            sensitive.extend(re.findall(r"Contraseña temporal:\s*(\S+)", body))
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
    print("Playwright: " + json.dumps(summary, ensure_ascii=False), flush=True)
    if completed.returncode:
        raise RuntimeError("Playwright falló; consultar únicamente las ubicaciones saneadas del resumen.")
    return summary
