#!/usr/bin/env python3
"""Certify authentic 0.4.1/0.5.0 backups against current disposable restores.

The existing installation is inspected only for immutable image IDs. Its
containers, volumes, networks and original backup files are never mutated.
Private backups/keys stay below the ignored evidence directory; only result.json
is suitable for publishing. The 0.5.0 fixture uses archived baseline tooling.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import secrets
import subprocess
import sys
import zipfile
from pathlib import Path
from unittest.mock import patch
from uuid import uuid4

sys.path.insert(0, str(Path(__file__).resolve().parent))
import docker_backup_cycle as recovery

docker_state = recovery.docker_state
ROOT = Path(__file__).resolve().parents[2]
BASELINE = "e7838c3c86b2117605a889f23242e75d9b09577d"
IMAGE_SERVICES = frozenset({"api", "worker", "delivery-worker", "web"})


def isolated_project(project: str) -> str:
    if not re.fullmatch(r"trackvance-recovery-(?:src|dst|db)-legacy-[a-z0-9-]+", project):
        raise ValueError("La certificación requiere un proyecto recovery legacy aislado.")
    return docker_state.validate_project(project)


def baseline_images(project: str) -> tuple[dict, dict[str, str]]:
    """Inspection only: do not execute anything in an existing container."""
    state = docker_state.inventory(project)
    images = {item["service"]: item["image_id"] for item in state["containers"]
              if item["service"] in IMAGE_SERVICES}
    if set(images) != IMAGE_SERVICES or any(
        not re.fullmatch(r"sha256:[a-f0-9]{64}", value) for value in images.values()
    ):
        raise ValueError("No se encontraron las cuatro imágenes inmutables de la baseline.")
    return state, images


def image_overrides(images: dict[str, str]) -> dict:
    """Minimal JSON Compose overlay; the CLI also forbids builds and pulls."""
    return {"services": {name: {"image": image_id, "pull_policy": "never"}
                         for name, image_id in images.items()}}


def extract_baseline(evidence: Path, environment: dict, credentials: tuple[str, ...]) -> Path:
    archive = evidence / "baseline-tooling.zip"
    recovery.execute(["git", "archive", "--format=zip", "--output", str(archive),
                      BASELINE, "compose.yml", "scripts"], environment,
                     credentials=credentials)
    destination = evidence / "baseline-tooling"
    destination.mkdir(mode=0o700)
    with zipfile.ZipFile(archive) as bundle:
        for member in bundle.infolist():
            path = (destination / member.filename).resolve()
            if not path.is_relative_to(destination.resolve()) or member.is_dir() and path == destination:
                raise ValueError("La baseline contiene una ruta fuera del directorio privado.")
        bundle.extractall(destination)
    return destination


def restore_and_capture(source: Path, target: str, port: int) -> dict:
    """Capture the exact normalized comparison BEFORE smoke creates new rows."""
    isolated_project(target)
    original_validator = docker_state.validate_restored_state
    report = {}

    def validate(manifest, expected, restored, normalized=None):
        original_validator(manifest, expected, restored, normalized)
        recovery.ensure(normalized == expected, "La proyección histórica no es exacta.")
        report.update({
            "source_manifest_schema": manifest["schema_version"],
            "source_state_schema": expected["schema_version"],
            "source_migration": expected["migration"],
            "target_state_schema": restored["schema_version"],
            "target_migration": restored["migration"],
            "normalized_state_sha256": docker_state.canonical_hash(normalized),
            "source_state_sha256": docker_state.canonical_hash(expected),
            "exact_historical_state": "PASS",
            "delivery_reviews_empty": restored["tables"]["delivery_reviews"] == {},
            "historical_table_counts": {name: len(rows) for name, rows in expected["tables"].items()},
            "verified_artifacts": restored["verified_artifacts"],
            "verified_source_secrets": restored["verified_source_secrets"],
            "verified_delivery_secrets": restored["verified_delivery_secrets"],
        })

    with patch.object(docker_state, "validate_restored_state", validate):
        receipt = docker_state.restore(source, target, start=True, smoke=True, web_port=port)
    recovery.ensure(bool(report) and report["delivery_reviews_empty"],
                    "Falta la comparación persistente anterior al smoke.")
    return {**report, "status": "PASS", "api_smoke": "PASS", "doctor": "PASS",
            "restore_status": receipt["status"]}


def capture_delivery(api: recovery.RecoveryApi, original: dict) -> dict:
    draft = recovery.delivery_draft(original, "records")
    config = api.json("POST", "/delivery/configurations", {
        **draft, "name": "Entrega auténtica 0.5.0 antes del backup",
    }, expected=201)
    queued = api.json("POST", "/delivery/runs", {
        "configuration_id": config["id"], "dataset_version_id": original["version"]["id"],
    }, expected=202)
    run = recovery.wait_existing_run(api, queued["id"])
    recovery.ensure(run["status"] == "SUCCESS" and run["decision"] == "COMMITTED",
                    "La baseline 0.5.0 no confirmó la entrega real.")
    receipt = api.request("GET", f"/delivery/runs/{run['id']}/receipt")
    manifest = api.request("GET", f"/runs/{run['id']}/evidence")
    recovery.ensure("metric_semantics" not in json.loads(receipt),
                    "El receipt debe proceder de 0.5.0, no de 0.5.1.")
    details = api.json("GET", f"/datasets/{original['dataset_id']}")
    version = next(item for item in details["versions"] if item["id"] == original["version"]["id"])
    return {"dataset_id": original["dataset_id"],
            "run": api.json("GET", f"/runs/{run['id']}"),
            "configuration": api.json("GET", f"/delivery/configurations/{config['id']}"),
            "attempts": api.json("GET", f"/delivery/runs/{run['id']}/attempts"),
            "dataset_version": version,
            "canonical_sha256": recovery.artifact_hash(api, version),
            "receipt_sha256": hashlib.sha256(receipt).hexdigest(),
            "manifest_sha256": hashlib.sha256(manifest).hexdigest()}


def verify_delivery_history(api: recovery.RecoveryApi, historical: dict) -> dict:
    run = historical["run"]
    comparisons = {
        "run": api.json("GET", f"/runs/{run['id']}"),
        "configuration": api.json("GET", f"/delivery/configurations/{historical['configuration']['id']}"),
        "attempts": api.json("GET", f"/delivery/runs/{run['id']}/attempts"),
    }
    for name, actual in comparisons.items():
        recovery.ensure(actual == historical[name], f"Cambió el contrato histórico {name}.")
    details = api.json("GET", f"/datasets/{historical['dataset_id']}")
    version = next(item for item in details["versions"] if item["id"] == historical["dataset_version"]["id"])
    recovery.ensure(version == historical["dataset_version"], "Cambió el DatasetVersion histórico.")
    recovery.ensure(recovery.artifact_hash(api, version) == historical["canonical_sha256"],
                    "Cambió el Parquet histórico.")
    for label, path in (
        ("receipt", f"/delivery/runs/{run['id']}/receipt"),
        ("manifest", f"/runs/{run['id']}/evidence"),
    ):
        recovery.ensure(hashlib.sha256(api.request("GET", path)).hexdigest()
                        == historical[f"{label}_sha256"], f"Cambió el {label} histórico.")
    reviews = api.json("GET", f"/delivery/runs/{run['id']}/reviews")
    recovery.ensure(reviews == {"items": [], "total": 0}, "Se inventaron revisiones históricas.")
    return {"status": "PASS", "run_id": run["id"],
            "configuration_id": historical["configuration"]["id"],
            "receipt_sha256": historical["receipt_sha256"],
            "manifest_sha256": historical["manifest_sha256"],
            "canonical_sha256": historical["canonical_sha256"],
            "immutable_api_contracts": "PASS", "immutable_artifact_bytes": "PASS",
            "historical_metric_semantics_added": False, "reviews": 0}


def main() -> int:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--legacy041-backup", type=Path, required=True)
    parser.add_argument("--baseline-image-project", default="trackvance-certification")
    parser.add_argument("--evidence-dir", type=Path, required=True)
    options = parser.parse_args()
    evidence = options.evidence_dir.resolve()
    evidence.mkdir(mode=0o700, parents=True, exist_ok=False)
    suffix = uuid4().hex[:10]
    source, target041, target050, database = [isolated_project(f"trackvance-recovery-{kind}-legacy-{label}-{suffix}")
        for kind, label in (("src", "050"), ("dst", "041"), ("dst", "050"), ("db", "050"))]
    internal, external, reader, writer = (secrets.token_hex(24) for _ in range(4))
    credentials = (internal, external, reader, writer)
    environment = {**os.environ, "POSTGRES_PASSWORD": internal,
                   "RECOVERY_SOURCE_PASSWORD": external, "DEMO_ACCESS_ENABLED": "true",
                   "DEMO_SEED_ENABLED": "false", "PYTHONIOENCODING": "utf-8",
                   "PYTHONUTF8": "1"}
    claimed = []
    result = {"status": "FAIL", "baseline_revision": BASELINE,
              "main_project": options.baseline_image_project}
    stage = "validate_inputs"
    before_main = None
    try:
        before_main, images = baseline_images(options.baseline_image_project)
        result["baseline_images"] = images
        for project in (source, target041, target050, database):
            recovery.assert_fresh(project)
        original_manifest_hash = docker_state.digest(options.legacy041_backup / "backup-manifest.json")
        private041 = evidence / "backup-041"
        docker_state.stage_verified_backup(options.legacy041_backup, private041)
        stage = "restore_041"
        recovery.assert_fresh(target041)
        claimed.append(target041)
        with patch.dict(os.environ, environment):
            result["legacy_041"] = restore_and_capture(private041, target041, recovery.available_port())
        recovery.cleanup(target041, evidence)
        claimed.remove(target041)
        recovery.ensure(docker_state.digest(options.legacy041_backup / "backup-manifest.json")
                        == original_manifest_hash, "El backup original cambió.")
        docker_state.verify_backup(options.legacy041_backup)
        result["legacy_041"]["original_backup_unmodified"] = True
        result["legacy_041"]["source_manifest_sha256"] = original_manifest_hash

        stage = "prepare_050"
        archive = extract_baseline(evidence, environment, credentials)
        frozen = evidence / "legacy-source.compose.json"
        frozen.write_text(json.dumps(image_overrides(images), indent=2), encoding="utf-8")
        fixture_file = evidence / "external-postgres.compose.json"
        fixture_file.write_text(json.dumps(recovery.fixture_compose(), indent=2), encoding="utf-8")
        fixture = ["docker", "compose", "-p", database, "-f", str(fixture_file)]
        source_port = recovery.available_port()
        environment.update({"WEB_PORT": str(source_port), "TRACKVANCE_WEB_ORIGIN": f"http://localhost:{source_port}"})
        recovery.assert_fresh(database)
        claimed.append(database)
        recovery.execute([*fixture, "up", "-d", "--wait"], environment, credentials=credentials)
        recovery.initialize_source(fixture, environment, credentials, reader, writer)
        compose = ["docker", "compose", "-p", source,
                   "-f", str(archive / "compose.yml"), "-f", str(frozen)]
        recovery.assert_fresh(source)
        claimed.append(source)
        recovery.execute([*compose, "up", "-d", "--wait", "--no-build", "--pull", "never"],
                         environment, credentials=credentials)
        actual_images = {item["service"]: item["image_id"]
                         for item in docker_state.inventory(source)["containers"]
                         if item["service"] in IMAGE_SERVICES}
        recovery.ensure(actual_images == images, "Compose no reutilizó las imágenes inmutables seleccionadas.")
        version = recovery.execute([*compose, "exec", "-T", "api", "python", "-c",
            "import trackvance; print(trackvance.__version__)"], environment, credentials=credentials).strip()
        recovery.ensure(version == "0.5.0", "La imagen aislada no es Trackvance Core 0.5.0.")
        recovery.attach_external_network(source, database, environment)
        api = recovery.RecoveryApi(source_port, credentials)
        original = recovery.capture_original(api, reader, writer)
        historical = capture_delivery(api, original)
        stage = "backup_050"
        backup050 = evidence / "backup-050"
        # The archive is not a checkout. Prevent the old tool from attributing
        # its backup to the parent workspace's newer HEAD; the actual archive
        # revision and immutable runtime images are recorded in result.json.
        backup_environment = {**environment, "GIT_CEILING_DIRECTORIES": str(evidence)}
        recovery.execute([sys.executable, str(archive / "scripts/docker_state.py"), "backup",
                          "--project", source, "--destination", str(backup050)],
                         backup_environment, credentials=credentials)
        manifest = docker_state.verify_backup(backup050)
        state = json.loads((backup050 / "state.json").read_text(encoding="utf-8"))
        recovery.ensure(manifest["migration"] == "0008_data_delivery" and state["schema_version"] == 3
                        and bool(state["tables"]["delivery_attempts"]), "El backup 0.5.0 no es auténtico/completo.")
        recovery.cleanup(source, evidence)
        claimed.remove(source)
        stage = "restore_050"
        recovery.assert_fresh(target050)
        claimed.append(target050)
        target_port = recovery.available_port()
        with patch.dict(os.environ, environment):
            result["legacy_050"] = restore_and_capture(backup050, target050, target_port)
        recovery.attach_external_network(target050, database, environment)
        restored_api = recovery.RecoveryApi(target_port, credentials)
        for path in (f"/connections/{original['connection_id']}/test",
                     f"/delivery/destinations/{original['destination']['id']}/test"):
            recovery.ensure(restored_api.json("POST", path, {})["status"] == "SUCCESS",
                            "La credencial histórica no permite reconectar.")
        result["legacy_050"]["source_runtime_version"] = version
        result["legacy_050"]["source_images_match_baseline"] = True
        result["legacy_050"]["source_destroyed_before_restore"] = True
        result["legacy_050"]["restored_source_and_destination_credentials"] = "PASS"
        result["legacy_050"]["historical_delivery"] = verify_delivery_history(
            restored_api, historical)
        result["status"] = "PASS"
    except (OSError, RuntimeError, ValueError, KeyError, StopIteration,
            subprocess.SubprocessError, zipfile.BadZipFile) as error:
        result.update({"failed_stage": stage, "error_type": type(error).__name__})
        print(f"ERROR: legacy restore en {stage} ({type(error).__name__}); detalle suprimido.", file=sys.stderr)
    finally:
        for project in reversed(claimed):
            try:
                recovery.cleanup(project, evidence)
            except (OSError, RuntimeError, ValueError, subprocess.SubprocessError) as error:
                result["status"] = "FAIL"
                result.setdefault("cleanup_failures", []).append({"project": project, "type": type(error).__name__})
        if before_main is not None:
            try:
                result["main_inventory_unchanged"] = docker_state.inventory(options.baseline_image_project) == before_main
            except (OSError, RuntimeError, ValueError, subprocess.SubprocessError) as error:
                result["main_inventory_unchanged"] = False
                result["main_inspection_error"] = type(error).__name__
            if not result["main_inventory_unchanged"]:
                result["status"] = "FAIL"
        serialized = json.dumps(result, indent=2, sort_keys=True)
        recovery.assert_no_secrets(serialized, credentials)
        (evidence / "result.json").write_text(serialized, encoding="utf-8")
        print(serialized)
    return 0 if result["status"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
