"""Retain only sanitized browser counters in publishable certification evidence."""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import signal
import subprocess
import threading
from pathlib import Path

CI_OPT_IN_SPECS = {
    "tests-e2e/automation.spec.ts": ("TV_AUTOMATION_E2E", "async-volume-100"),
    "tests-e2e/volume.spec.ts": ("TV_VOLUME_E2E", "async-volume-100"),
    "tests-e2e/corrections-volume.spec.ts": ("TV_CORRECTIONS_E2E", "corrections-browser"),
    "tests-e2e/connections.spec.ts": ("TV_CONNECTIONS_E2E", "connections"),
    "tests-e2e/roadmap-source-cycle.spec.ts": ("TV_CONNECTIONS_E2E", "connections"),
    "tests-e2e/delivery.spec.ts": ("TV_DELIVERY_E2E", "delivery"),
    "tests-e2e/identity-sso.spec.ts": ("TV_IDENTITY_SSO_E2E", "identity-sso"),
    "tests-e2e/demo-access-clean.spec.ts": ("TV_EXPECT_CLEAN_DEMO", "compose-critical"),
}


def ci_spec_selection(arguments: list[str], root: Path, environment: dict[str, str]) -> tuple[list[str], dict | None]:
    """Select all actual specs except named opt-ins lacking their exact flag."""
    if not environment.get("TRACKVANCE_CI_IMAGE_MANIFEST"):
        return arguments, None
    if arguments:
        if all(re.fullmatch(r"tests-e2e/[a-z0-9-]+\.spec\.ts", name) for name in arguments):
            if len(set(arguments)) != len(arguments) or any(
                not (root / "frontend" / name).is_file() or (root / "frontend" / name).is_symlink()
                for name in arguments
            ):
                raise RuntimeError("CI_BROWSER_EXPLICIT_SELECTION_INVALID")
            return arguments, {"selected_spec_files": arguments, "browser_selection_mode": "explicit"}
        return arguments, None
    selected, excluded = [], []
    frontend = root / "frontend"
    for path in sorted((frontend / "tests-e2e").rglob("*.spec.ts")):
        file = path.relative_to(frontend).as_posix()
        opt_in = CI_OPT_IN_SPECS.get(file)
        if opt_in and environment.get(opt_in[0]) != "true":
            excluded.append({"file": file, "required_flag": opt_in[0], "covered_by_group": opt_in[1]})
        else:
            selected.append(file)
    if not selected:
        raise RuntimeError("CI_BROWSER_SELECTION_EMPTY")
    return selected, {"selected_spec_files": selected, "excluded_opt_in_specs": excluded}


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


def bounded_browser_command(arguments: list[str], *, cwd: Path, environment: dict[str, str],
                            timeout_seconds: int) -> subprocess.CompletedProcess:
    """Own one browser process group and reap it on timeout or interruption."""
    if not 0 < timeout_seconds <= 3600:
        raise ValueError("Browser deadline must be positive and at most one hour")
    process = subprocess.Popen(arguments, cwd=cwd, env=environment, stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding="utf-8", errors="replace",
        start_new_session=os.name == "posix")
    previous_term = None
    term_handler = os.name == "posix" and threading.current_thread() is threading.main_thread()
    if term_handler:
        def interrupted(*_args):
            raise InterruptedError("BROWSER_WRAPPER_INTERRUPTED")
        previous_term = signal.signal(signal.SIGTERM, interrupted)
    try:
        stdout, stderr = process.communicate(timeout=timeout_seconds)
        return subprocess.CompletedProcess(arguments, process.returncode, stdout, stderr)
    finally:
        if term_handler:
            signal.signal(signal.SIGTERM, previous_term)
        # The group ID is solely this child launched with start_new_session.
        # A parent wrapper signal or scenario alarm also enters this cleanup.
        if os.name == "posix":
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        elif process.poll() is None:
            subprocess.run(["taskkill", "/PID", str(process.pid), "/T", "/F"],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=15, check=False)
        if process.poll() is None:
            process.kill()
        process.communicate(timeout=15)


def run_browser(pnpm: str, arguments: list[str], *, root: Path, project: str,
                environment: dict[str, str], evidence: Path, timeout_seconds: int | None = None) -> dict:
    raw = root / ".codex-local" / "browser-results" / project
    raw.mkdir(parents=True, exist_ok=True)
    selected, selection = ci_spec_selection(arguments, root, environment)
    command = [pnpm, "exec", "playwright", "test", *selected, "--reporter=json", "--output", str(raw)]
    child_environment = {**environment, "TV_E2E_PRIVATE_ARTIFACTS": "1"}
    if timeout_seconds is None:
        completed = subprocess.run(command, cwd=root / "frontend", env=child_environment,
            text=True, encoding="utf-8", errors="replace", capture_output=True, check=False)
    else:
        try:
            completed = bounded_browser_command(command, cwd=root / "frontend", environment=child_environment,
                                                timeout_seconds=timeout_seconds)
        except BaseException as error:
            summary = {"status": "FAIL", "error_type": type(error).__name__, "raw_artifacts_published": False,
                       "deadline_seconds": timeout_seconds, **(selection or {})}
            (evidence / "browser-summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
            raise
    try:
        report = json.loads(completed.stdout[completed.stdout.index("{"):])
    except (ValueError, TypeError) as error:
        raise RuntimeError("Playwright no devolvió un informe JSON válido; salida omitida por privacidad.") from error
    summary = {"status": "PASS" if not completed.returncode else "FAIL", **report.get("stats", {})}
    summary.update(selection or {})
    unexpected_skips = selection is not None and summary.get("skipped", 0) != 0
    if unexpected_skips:
        summary["status"] = "FAIL"
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
    if unexpected_skips:
        raise RuntimeError("CI_BROWSER_UNEXPECTED_SKIP")
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
