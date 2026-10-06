"""Build and certify 0.8.0 using resolved, bounded UUID-owned Docker resources."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))
import certification_v080 as guard
from browser_evidence import run_browser


def run(arguments, directory, name):
    with (directory / (name + ".private.log")).open("w", encoding="utf-8") as output:
        result = subprocess.run(arguments, cwd=ROOT, stdout=output, stderr=subprocess.STDOUT, check=False)
    if result.returncode:
        raise RuntimeError(f"La etapa {name} falló (exit={result.returncode}); diagnóstico privado disponible.")


def report_tier(directory, context, rows, summary):
    """Copy the last safe checkpoint before the failed fixture is destroyed."""
    stage = f"reports-{rows}"
    evidence = f"/tmp/v080-reports-{rows}"
    target = directory / (stage + ".json")
    failure = None
    try:
        run([*guard.compose_args(directory, context), "exec", "-T", "api", "python",
             "/app/scripts/tests/catalog_reports_api_cycle.py", "--rows", str(rows), "--evidence", evidence], directory, stage)
    except RuntimeError as error:
        failure = error
    try:
        run([*guard.compose_args(directory, context), "cp", f"api:{evidence}/result.json", str(target)], directory, f"copy-{rows}")
        result = json.loads(target.read_text(encoding="utf-8"))
    except (RuntimeError, OSError, ValueError):
        summary.update(failed_stage=stage, failed_tier={"rows_per_source": rows, "status": "FAIL", "diagnostics_copied": False},
                       error={"type": type(failure).__name__ if failure else "EvidenceCopyError", "code": "CATALOG_FIXTURE_EVIDENCE_UNAVAILABLE"})
        if failure is not None:
            raise failure
        raise
    if failure is not None or result.get("status") != "PASS":
        detail = {key: result[key] for key in ("active_phase", "phase_started_at", "last_terminal", "error", "duration_seconds") if key in result}
        summary.update(failed_stage=stage, failed_tier={"rows_per_source": rows, "status": "FAIL", "diagnostics_copied": True, **detail},
                       error=result.get("error", {"type": "RuntimeError", "code": "CATALOG_FIXTURE_FAILED"}))
        if failure is not None:
            raise failure
        raise RuntimeError("La certificación completa no terminó PASS.")
    return result


def browser_gate(directory, context):
    """Run only the real catalog/report journey and publish its synthetic captures."""
    pnpm = shutil.which("pnpm")
    if not pnpm:
        raise RuntimeError("pnpm no está disponible para el gate obligatorio de navegador.")
    environment = {**os.environ, "TV_E2E_URL": f"http://localhost:{context['port']}",
                   "TV_E2E_PRIVATE_ARTIFACTS": "1"}
    result = run_browser(pnpm, ["tests-e2e/catalog-reports.spec.ts"], root=ROOT,
                         project=context["project"], environment=environment, evidence=directory)
    if result.get("skipped", 0) or result.get("unexpected", 0) or result.get("flaky", 0) or result.get("expected") != 1:
        raise RuntimeError("El recorrido obligatorio de navegador no terminó completo sin reintentos.")
    raw = ROOT / ".codex-local" / "browser-results" / context["project"]
    captures = directory / "browser-screenshots"
    captures.mkdir(exist_ok=True)
    result["screenshots"] = []
    for name in ("catalog-quality.png", "report-preview.png", "report-lineage.png"):
        paths = list(raw.rglob(name))
        if len(paths) != 1:
            raise RuntimeError("Falta una captura real del recorrido de Catálogo/Reportes.")
        content = paths[0].read_bytes()
        if not content.startswith(b"\x89PNG\r\n\x1a\n") or len(content) > 8 * 1024**2:
            raise RuntimeError("Captura de navegador fuera del formato o límite admitido.")
        (captures / name).write_bytes(content)
        result["screenshots"].append({"name": name, "bytes": len(content), "sha256": hashlib.sha256(content).hexdigest(),
                                      "provenance": "SYNTHETIC_REAL_BROWSER"})
    result["raw_artifacts_published"] = False
    (directory / "browser-summary.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--rows", nargs="+", type=int, choices=(120, 400000, 1000000), default=[120])
    parser.add_argument("--port", type=int, default=32082)
    parser.add_argument("--main-project")
    parser.add_argument("--reuse-images", action="store_true")
    parser.add_argument("--keep", action="store_true")
    parser.add_argument("--with-browser", action="store_true")
    parser.add_argument("--with-recovery", action="store_true")
    parser.add_argument("--with-ephemeral-observation", action="store_true")
    args = parser.parse_args()
    directory = guard.init("catalog-reports", args.port, args.main_project)
    directory, context = guard.load_context(directory)
    summary = {"version": "0.8.0", "status": "FAIL", "project": context["project"], "tiers": [],
               "source_sha": guard.command(["git", "rev-parse", "HEAD"]).strip(),
               "source_tree_dirty": bool(guard.command(["git", "status", "--porcelain"]).strip()),
               "main_inventory_before": context["main_before"], "isolation": "RESOLVED_PREFLIGHT"}
    started, began = False, time.monotonic()
    try:
        if not args.reuse_images:
            run(["docker", "build", "-t", context["image"], "-f", "backend/Dockerfile", "."], directory, "backend-build")
            run(["docker", "build", "-t", "trackvance-v080-isolated:web", "-f", "deploy/docker/frontend.Dockerfile", "."], directory, "web-build")
        guard.preflight(directory, context)
        started = True
        run([*guard.compose_args(directory, context), "up", "--no-build", "--detach", "--wait", "--wait-timeout", "240",
             "postgres", "api", "worker", "acquisition-worker", "report-worker", "web"], directory, "startup")
        run([*guard.compose_args(directory, context), "exec", "-T", "api", "python",
             "/app/scripts/tests/reports_postgres_snapshot.py"], directory, "postgres-snapshot")
        summary["postgres_joint_snapshot"] = json.loads((directory / "postgres-snapshot.private.log").read_text(encoding="utf-8"))
        if summary["postgres_joint_snapshot"].get("status") != "PASS":
            raise RuntimeError("La resolución conjunta concurrente PostgreSQL no pasó.")
        browser_done = False
        if args.with_browser and 120 not in args.rows:
            summary["browser"] = browser_gate(directory, context)
            browser_done = True
        for rows in args.rows:
            guard.preflight(directory, context)
            result = report_tier(directory, context, rows, summary)
            summary["tiers"].append(result)
            if args.with_browser and rows == 120 and not browser_done:
                summary["browser"] = browser_gate(directory, context)
                browser_done = True
        if args.with_recovery or args.with_ephemeral_observation:
            # The population fixture has finished. Keep its owned volumes for
            # recovery, but release all running services before another bounded
            # certification project starts on the same host.
            guard.preflight(directory, context)
            run([*guard.compose_args(directory, context), "stop"], directory, "stop-population-services")
        if args.with_ephemeral_observation:
            observation_error = None
            try:
                run([sys.executable, str(ROOT / "scripts/tests/reports_ephemeral_http.py"),
                     "--main-project", context["main_project"]], directory, "ephemeral-observation")
            except RuntimeError as exc:
                observation_error = exc
            output = (directory / "ephemeral-observation.private.log").read_text(encoding="utf-8").splitlines()
            record = json.loads(output[-1])
            evidence_path = Path(record["evidence"]).resolve()
            if not evidence_path.is_relative_to((ROOT / ".codex-local/v080").resolve()):
                raise RuntimeError("La observación HTTP publicó una ruta fuera del ensayo aislado.")
            observation = json.loads(evidence_path.read_text(encoding="utf-8"))
            summary["ephemeral_http_observation"] = observation
            (directory / "reports-ephemeral-http.json").write_text(
                json.dumps(observation, ensure_ascii=False, indent=2), encoding="utf-8")
            if observation_error or observation.get("status") != "PASS" or not observation.get("main_unchanged"):
                raise RuntimeError("La observación real de API, proxy y ejecutores no pasó.") from observation_error
        if args.with_recovery:
            run([sys.executable, str(ROOT / "scripts/tests/catalog_reports_recovery.py"),
                 "--context", str(directory), "--mode", "both"], directory, "recovery")
            recoveries = list(directory.glob("catalog-recovery-*/result.json"))
            if len(recoveries) != 1:
                raise RuntimeError("La recuperación no produjo una evidencia exclusiva.")
            summary["recovery"] = json.loads(recoveries[0].read_text(encoding="utf-8"))
            if summary["recovery"].get("status") != "PASS":
                raise RuntimeError("La recuperación nativa y auténtica 0.7.0 no pasó.")
        summary["status"] = "PASS"
    finally:
        summary["duration_seconds"] = round(time.monotonic() - began, 3)
        summary["main_inventory_after"] = guard.inventory(context["main_project"])
        summary["main_unchanged"] = summary["main_inventory_after"] == context["main_before"]
        if not summary["main_unchanged"]:
            summary["status"] = "FAIL"
        (directory / "result.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
        if started and not args.keep:
            guard.preflight(directory, context)
            guard.command([*guard.compose_args(directory, context), "down", "--volumes", "--remove-orphans"])
        print(json.dumps({"status": summary["status"], "evidence": str(directory / "result.json"), "main_unchanged": summary["main_unchanged"]}))
    if summary["status"] != "PASS":
        raise RuntimeError("La certificación o el inventario protegido no pasó.")


if __name__ == "__main__":
    main()
