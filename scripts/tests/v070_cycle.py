#!/usr/bin/env python3
"""Certify real volume, automation and recovery using UUID-owned resources only."""

from __future__ import annotations

import argparse
import contextlib
import io
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))
import certification_v070 as certification
from browser_evidence import run_browser


def run(arguments, directory, name, *, environment=None):
    """Keep potentially sensitive process output private; publish only counters."""
    with (directory / (name + ".private.log")).open("w", encoding="utf-8") as log:
        result = subprocess.run(arguments, cwd=ROOT, env=environment, stdout=log,
                                stderr=subprocess.STDOUT, check=False)
    if result.returncode:
        raise RuntimeError(f"La etapa {name} falló (exit={result.returncode}).")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tier-mib", type=int, choices=(100, 500, 1024), required=True)
    parser.add_argument("--port", type=int, default=32071)
    parser.add_argument("--with-browser", action="store_true")
    parser.add_argument("--with-recovery", action="store_true")
    parser.add_argument("--reuse-images", action="store_true")
    args = parser.parse_args()
    captured = io.StringIO()
    with contextlib.redirect_stdout(captured):
        certification.init(f"volume-{args.tier_mib}", args.port)
    directory, context = certification.load_context(Path(json.loads(captured.getvalue())["context"]))
    # Explicit certification profile accounts for encoding/header overhead of a
    # nominal 1 GiB fixture. The user's .env and legacy limits are never read.
    with (directory / "test.env").open("a", encoding="utf-8") as stream:
        stream.write("TRACKVANCE_ACQUISITION_MAX_UPLOAD_BYTES=2147483648\n"
                     "TRACKVANCE_ACQUISITION_MAX_OBSERVED_BYTES=4294967296\n")
    summary = {"status": "FAIL", "version": "0.7.0", "project": context["project"],
               "tier_mib": args.tier_mib, "rows": 1_000_000}
    started = False
    try:
        if not args.reuse_images:
            run(["docker", "build", "-t", context["image"], "-f", "backend/Dockerfile", "."], directory, "backend-build")
            run(["docker", "build", "-t", "trackvance-v070-isolated:web", "-f",
                 "deploy/docker/frontend.Dockerfile", "."], directory, "web-build")
        started = True
        certification.compose(directory, context, ["up", "--no-build", "--detach", "--wait", "--wait-timeout", "240"])
        run([sys.executable, "scripts/tests/volume_cycle.py", "--context", str(directory),
             "--sizes", str(args.tier_mib)], directory, "volume")
        volume = json.loads((directory / "volume-evidence.json").read_text(encoding="utf-8"))
        if len(volume["tiers"]) != 1 or volume["tiers"][0]["status"] != "PASS":
            raise RuntimeError("El tier obligatorio no quedó completamente aprobado.")
        summary["volume"] = "PASS"
        # The real API/PG workers, independently leased consumers and SQL target
        # certify dedupe, permissions and exact Intake output without mocks.
        certification.compose(directory, context, ["exec", "-T", "api", "python",
            "/app/scripts/tests/automation_cycle.py", "--evidence", "/tmp/v070-automation"])
        certification.compose(directory, context, ["cp", "api:/tmp/v070-automation/automation-results.json",
                                                   str(directory / "automation-results.json")])
        automation = json.loads((directory / "automation-results.json").read_text(encoding="utf-8"))
        if automation["status"] != "PASS":
            raise RuntimeError("La automatización real no quedó aprobada.")
        summary["automation"] = "PASS"
        if args.with_browser:
            pnpm = shutil.which("pnpm")
            if not pnpm:
                raise RuntimeError("pnpm no está disponible para la prueba obligatoria de navegador.")
            fixture = volume["tiers"][0]["fixture"]
            basename = f"volume-{args.tier_mib}mib-1000000-varied.json"
            environment = {**os.environ, "TV_E2E_URL": f"http://localhost:{context['port']}",
                "TV_E2E_PRIVATE_ARTIFACTS": "1", "TV_VOLUME_E2E": "true",
                "TV_VOLUME_PROJECT": context["project"], "TV_VOLUME_FIXTURE": fixture["formats"]["CSV"]["path"],
                "TV_VOLUME_METADATA": str(directory / "volume-fixtures" / basename),
                "TV_VOLUME_DESTINATION": volume["playwright"]["destination_id"],
                "TV_VOLUME_SCHEMA": volume["playwright"]["schema"],
                "TV_AUTOMATION_E2E": "true", "TV_AUTOMATION_ID": automation["automation_id"]}
            browser = run_browser(pnpm, ["tests-e2e/volume.spec.ts", "tests-e2e/automation.spec.ts"],
                                  root=ROOT, project=context["project"], environment=environment, evidence=directory)
            if browser.get("skipped", 0) or browser.get("expected") != 2:
                raise RuntimeError("Las dos pruebas obligatorias de navegador no se ejecutaron completas.")
            browser_reports = list((ROOT / ".codex-local" / "browser-results" / context["project"]).rglob("volume-ui.json"))
            if len(browser_reports) != 1:
                raise RuntimeError("El navegador no produjo exactamente una evidencia integral de volumen.")
            run([sys.executable, "scripts/tests/volume_cycle.py", "--context", str(directory),
                 "--verify-browser-report", str(browser_reports[0])], directory, "browser-volume-integrity")
            summary["browser"] = "PASS"
        if args.with_recovery:
            run([sys.executable, "scripts/tests/docker_backup_cycle.py", "--v070-context", str(directory),
                 "--evidence-dir", str(directory / "native-recovery")], directory, "native-recovery")
            summary["native_recovery"] = "PASS"
        certification.assert_main_unchanged(context)
        summary["main_unchanged"] = "PASS"
        summary["status"] = "PASS"
    except Exception as error:
        summary["error_type"] = type(error).__name__
        raise
    finally:
        if started:
            certification.compose(directory, context, ["down", "--volumes", "--remove-orphans"])
        certification.assert_main_unchanged(context)
        summary["cleanup"] = "PASS"
        (directory / "cycle-summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
        print(json.dumps({"summary": str(directory / "cycle-summary.json"), "status": summary["status"]}))


if __name__ == "__main__":
    main()
