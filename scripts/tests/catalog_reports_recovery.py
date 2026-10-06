"""Restore populated 0.8.0 and authentic 0.7.0 into guarded UUID-owned projects.

No source checkout changes or habitual resources are allowed. All response bodies,
archives and PostgreSQL credentials remain inside the private certification root.
"""

from __future__ import annotations

import argparse
import json
import os
import socket
import subprocess
import sys
import time
import traceback
import zipfile
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))
import certification_v080 as guard
import docker_state
from docker_backup_cycle import (
    RecoveryApi,
    assert_no_secrets,
    execute,
    scan_backup_plaintext,
)

AUTHENTIC_070 = "d9b6856e757a2a1fcab3913209146f3b7b79d70c"
AUTOMATIC = ("worker", "acquisition-worker", "delivery-worker", "report-worker", "scheduler",
             "events-notifications", "events-chaining")
NATIVE_FIXTURE_SERVICES = ("postgres", "api", "worker", "acquisition-worker", "report-worker", "web")
INACTIVE_NATIVE_SERVICES = tuple(name for name in AUTOMATIC if name not in NATIVE_FIXTURE_SERVICES)


def available_port():
    for port in range(32100, 33000):
        try:
            with socket.socket() as probe:
                probe.bind(("127.0.0.1", port))
            return port
        except OSError:
            continue
    raise ValueError("No hay un puerto libre dentro del rango de certificación.")


def private_environment(directory):
    result = dict(line.split("=", 1) for line in (directory / "test.env").read_text(encoding="utf-8").splitlines()
                  if line and not line.startswith("#"))
    if result.get("POSTGRES_USER") != "tv_v080_test" or result.get("POSTGRES_DB") != "tv_v080_test":
        raise ValueError("La recuperación requiere PostgreSQL sintético exclusivo.")
    return result


def assert_main(context):
    if guard.inventory(context["main_project"]) != context["main_before"]:
        raise ValueError("El inventario habitual cambió durante la recuperación.")


def run(arguments, environment, *, input_text=None, timeout=1800):
    return execute(arguments, {**os.environ, **environment}, input_text=input_text, timeout=timeout,
                   credentials=(environment["POSTGRES_PASSWORD"],))


def args_for(directory, context):
    base = context.get("compose_base", str(ROOT / "compose.yml"))
    return ["docker", "compose", "--project-name", context["project"], "--env-file", str(directory / "test.env"),
            "-f", base, "-f", str(directory / "compose.json")]


def preflight(directory, context, environment):
    assert_main(context)
    # Resolved Compose contains synthetic credentials. Read it privately instead
    # of passing it through a command helper intended for shareable outputs.
    resolved = subprocess.run([*args_for(directory, context), "config", "--format", "json"], cwd=ROOT,
        env={**os.environ, **environment}, capture_output=True, text=True, encoding="utf-8", check=False, timeout=60)
    if resolved.returncode:
        raise ValueError("No fue posible resolver el perfil privado de recuperación.")
    config = json.loads(resolved.stdout)
    guard.validate_resolved(config, context, directory)
    return config


def compose_adapter(directory, context, environment):
    """Pin project and validated private files even for an archived Compose base."""
    def compose(project, *arguments, environment=None):
        if project != context["project"]:
            raise ValueError("El adaptador recibió un proyecto ajeno.")
        child = {**private_environment(directory), **(environment or {})}
        config = preflight(directory, context, child)
        if arguments == ("config", "--format", "json"):
            # The validated resolved configuration contains synthetic secrets.
            # Keep it in memory instead of scanning it as shareable output.
            return json.dumps(config)
        return run([*args_for(directory, context), *arguments], child)
    return compose


def cleanup(directory, context, evidence):
    # The context proves names, labels and non-overlap before the plan enumerates
    # concrete Docker IDs. reset verifies those exact IDs again before removal.
    preflight(directory, context, private_environment(directory))
    state = docker_state.inventory(context["project"])
    if not any(state[key] for key in ("containers", "volumes", "networks")):
        return
    plan_path = evidence / ("reset-" + context["project"] + ".json")
    plan = docker_state.create_reset_plan(context["project"], plan_path, ttl_minutes=60)
    docker_state.reset(plan_path, "RESET:" + context["project"] + ":" + plan["plan_sha256"][:12])


def image_id(tag, role):
    if role not in {"backend", "web"} or tag != "trackvance-v080-isolated:" + role:
        raise ValueError("La recuperación sólo admite imágenes privadas 0.8.0.")
    from ci_images import verified_images
    images = verified_images()
    if images:
        return images[role]
    return json.loads(guard.command(["docker", "image", "inspect", tag]))[0]["Id"]


def target_context(parent, suite, environment):
    directory = guard.init(suite, available_port(), parent["main_project"])
    directory, context = guard.load_context(directory)
    profile = json.loads((directory / "compose.json").read_text(encoding="utf-8"))
    backend, web = image_id("trackvance-v080-isolated:backend", "backend"), image_id("trackvance-v080-isolated:web", "web")
    lines = ["services:"]
    for name, service in profile["services"].items():
        if name != "postgres":
            service["image"] = web if name == "web" else backend
            service.pop("volumes", None)  # Use the frozen image, without live source mounts.
        lines.append("  " + name + ":")
        if name != "postgres":
            lines.append("    build: !reset null")
        for key, value in service.items():
            lines.append("    " + key + ": " + json.dumps(value))
    (directory / "compose.json").write_text("\n".join(lines) + "\n", encoding="utf-8")
    restored_env = {**environment, "WEB_PORT": str(context["port"]),
                    "TRACKVANCE_WEB_ORIGIN": "http://localhost:" + str(context["port"]),
                    "DEMO_SEED_ENABLED": "false", "DEMO_ACCESS_ENABLED": "false"}
    (directory / "test.env").write_text("\n".join(f"{key}={value}" for key, value in restored_env.items()) + "\n", encoding="utf-8")
    preflight(directory, context, restored_env)
    docker_state.ensure_fresh_project(context["project"])
    return directory, context, restored_env


def wait(api, path):
    deadline = time.monotonic() + 300
    while time.monotonic() < deadline:
        result = api.json("GET", path)
        if result["status"] not in {"QUEUED", "RUNNING", "PENDING"}:
            if result["status"] != "SUCCESS":
                raise ValueError("La fixture real no terminó correctamente.")
            return result
        time.sleep(0.5)
    raise ValueError("La fixture agotó su espera finita.")


def acquire_fixture(api, label, *, native=False):
    dataset = api.json("POST", "/datasets", {"name": label}, expected=201)
    upload = json.loads(api.request("POST", "/datasets/uploads/stage?filename=recovery.csv",
        raw=b"id,value\n001,alpha\n002,beta\n003,gamma\n", content_type="application/octet-stream", expected=201))
    acquisition = api.json("POST", "/datasets/" + dataset["id"] + "/acquisitions", {"upload_id": upload["upload"]["id"]}, expected=202)
    acquired = wait(api, "/acquisitions/" + acquisition["id"])
    if native:
        macro = api.json("POST", "/governance/macrodomains", {"name": label + " Macro"}, expected=201)
        domain = api.json("POST", "/governance/domains", {"name": label + " Domain", "macro_domain_id": macro["id"]}, expected=201)
        api.json("PATCH", "/datasets/" + dataset["id"] + "/governance", {"expected_version": 1,
            "macro_domain_id": macro["id"], "domain_id": domain["id"], "information_classification": "INTERNAL"})
    contract = api.json("POST", "/intake/contracts", {"name": label + " Intake", "dataset_id": dataset["id"],
        "config": {"required_columns": ["id", "value"], "max_error_rate": 0}}, expected=201)
    queued = api.json("POST", "/intake/runs", {"contract_id": contract["id"], "dataset_version_id": acquired["output_version_id"],
        "requested_engine": "POLARS"}, expected=202)
    approved = wait(api, "/runs/" + queued["id"])
    if approved["decision"] != "APPROVED" or approved["metrics"]["total_rows"] != 3:
        raise ValueError("La fixture no contiene una validación Intake real de tres filas.")
    return dataset, acquired, contract, approved


def diagnostic_fixture_payload():
    # The authentic 0.7 staging endpoint inspects at most 100 data rows. A
    # malformed later record reaches the real worker's complete reader.
    return b"id,value\n" + b"001,valid\n" * 100 + b"101,invalid,extra\n"


def resolve_saved_definition(api, definition):
    revision = definition["revisions"][0]
    return api.json("POST", "/reports/resolve", {"draft": revision["draft"], "revision_id": revision["id"]})


def prepare_native(context, environment):
    api = RecoveryApi(context["port"], (environment["POSTGRES_PASSWORD"],))
    label = "Recovery080 " + uuid4().hex[:8]
    dataset, acquired, contract, approved = acquire_fixture(api, label, native=True)
    term = api.json("POST", "/governance/glossary", {"name": label + " Term", "definition": "Synthetic immutable recovery fixture"}, expected=201)
    api.json("PATCH", "/catalog/datasets/" + dataset["id"] + "/columns", {"version_id": acquired["output_version_id"],
        "column_name": "id", "description": "Stable recovery identifier", "term_ids": [term["id"]], "expected_version": 0})
    api.json("POST", "/catalog/datasets/" + dataset["id"] + "/terms", {"term_id": term["id"]}, expected=201)
    draft = {"mode": "GUIDED", "sources": [{"alias": "a", "input_dataset_id": dataset["id"], "contract_id": contract["id"],
        "contract_revision_ids": [contract["id"]], "policy": "LATEST_APPROVED"}],
        "columns": [{"source_alias": "a", "column": "id", "alias": "id"}, {"source_alias": "a", "column": "value", "alias": "value"}],
        "joins": [], "order_by": [{"source_alias": "a", "column": "id", "direction": "ASC"}]}
    definition = api.json("POST", "/reports/definitions", {"name": label + " Report", "draft": draft})
    definition = api.json("POST", "/reports/definitions/" + definition["id"] + "/revisions", {"expected_version": 1, "draft": draft})
    context_result = resolve_saved_definition(api, definition)
    generation = api.json("POST", "/reports/datasets", {"context_id": context_result["context_id"],
        "idempotency_key": "recover-" + uuid4().hex, "name": label + " Output"})
    completed = wait(api, "/reports/executions/" + generation["id"])
    if completed["metrics"]["rows"] != 3:
        raise ValueError("La publicación nativa no contiene la población real esperada.")
    block = api.json("POST", "/catalog/datasets/" + dataset["id"] + "/blocks", {"scope": "REPORT", "reason": "Synthetic current recovery restriction"}, expected=201)
    return {"dataset_id": dataset["id"], "definition_id": definition["id"], "context_id": context_result["context_id"],
            "context_expires_at": context_result["expires_at"],
            "execution_id": completed["id"], "output_dataset_id": completed["output_dataset_id"],
            "output_version_id": completed["output_version_id"], "approval_run_id": approved["id"], "block_id": block["id"]}


def assert_native_state(state):
    if state["schema_version"] != 8 or state["migration"] != docker_state.CURRENT_MIGRATION or len(state["tables"]) != 55:
        raise ValueError("La huella no corresponde a un runtime nativo 0.8.0 completo.")
    if any(not state["tables"].get(table) for table in docker_state.CATALOG_STATE_TABLES):
        raise ValueError("La recuperación nativa exige fixtures reales en las trece entidades nuevas.")


def verify_restored_restriction(api, definition, fixture):
    revision = definition["selected_revision"]
    refused = api.json("POST", "/reports/resolve", {"draft": revision["draft"], "revision_id": revision["id"]}, expected=422)
    error = refused.get("error", {})
    if error.get("code") != "REPORT_SOURCE_INELIGIBLE" or not any(
            reason.get("code") == "DATASET_BLOCKED" for reason in error.get("details", {}).get("reasons", [])):
        raise ValueError("La resolución nueva no aplicó el bloqueo vigente restaurado.")
    expires = datetime.fromisoformat(fixture["context_expires_at"])
    now = datetime.now(UTC)
    frozen = "NOT_RUN_EXPIRED" if expires <= now else "NOT_RUN_NEAR_EXPIRY"
    if expires > now + timedelta(seconds=5):
        refused = api.json("POST", "/reports/preview", {"context_id": fixture["context_id"]}, expected=403)
        if refused.get("error", {}).get("code") != "DATASET_BLOCKED":
            raise ValueError("El contexto congelado vigente no revalidó el bloqueo restaurado.")
        frozen = "PASS_CURRENT_BLOCK"
    return {"current_block_new_resolution": "PASS", "frozen_context_preview": frozen,
            "context_expires_at_preserved": fixture["context_expires_at"]}


def stop_quiescent_population(directory, context, environment):
    """Release the owned source budget without hiding queued or active work."""
    preflight(directory, context, environment)
    state = docker_state.inventory(context["project"])
    postgres = next((item for item in state["containers"] if item["service"] == "postgres"), None)
    if postgres is None or not postgres["running"]:
        if any(item["running"] for item in state["containers"]):
            raise ValueError("No se puede demostrar quiescencia sin PostgreSQL activo.")
        return "ALREADY_STOPPED"
    active = run(["docker", "exec", str(postgres["id"]), "psql", "--no-psqlrc", "--username", environment["POSTGRES_USER"],
        "--dbname", environment["POSTGRES_DB"], "--no-align", "--tuples-only", "--quiet", "--set", "ON_ERROR_STOP=1",
        "--command", "BEGIN READ ONLY; SELECT count(*) FROM jobs WHERE status IN ('QUEUED', 'RUNNING'); COMMIT;"], environment)
    if str(active).strip() != "0":
        raise ValueError("La población aislada conserva Jobs activos; no se oculta trabajo pendiente con stop.")
    docker_state.compose(context["project"], "stop", "--timeout", "30")
    if any(item["running"] for item in docker_state.inventory(context["project"])["containers"]):
        raise ValueError("El contexto propio no quedó detenido tras liberar el presupuesto.")
    assert_main(context)
    return "STOPPED_QUIESCENT"


def restore_compare(parent, environment, backup, evidence, *, legacy=False, fixture=None):
    directory, target, target_env = target_context(parent, "restore070" if legacy else "restore080", environment)
    before = json.loads((backup / "state.json").read_text(encoding="utf-8"))
    old_compose, claimed = docker_state.compose, False
    try:
        docker_state.compose = compose_adapter(directory, target, target_env)
        claimed = True
        receipt = docker_state.restore(backup, target["project"], start=False, web_port=target["port"])
        if any(item["running"] for item in docker_state.inventory(target["project"])["containers"]):
            raise ValueError("El restore activó consumidores antes de concluir la verificación.")
        docker_state.compose(target["project"], "up", "--no-build", "-d", "--wait", "api")
        api_id = next(item["id"] for item in docker_state.inventory(target["project"])["containers"] if item["service"] == "api")
        after = docker_state._copy_snapshot(api_id, evidence / "restored-state.json")
        normalized = docker_state._copy_snapshot(api_id, evidence / "restored-legacy-state.json", command="snapshot-legacy-v7") if legacy else after
        if normalized != before:
            raise ValueError("La huella restaurada no conserva exactamente la historia original.")
        if legacy and any(after["tables"][table] for table in docker_state.CATALOG_STATE_TABLES):
            raise ValueError("La actualización fabricó gobierno, aprobaciones o ejecuciones nuevas.")
        functional = None
        restriction = None
        if fixture:
            # Authenticate only after comparing snapshots: sessions and audits
            # are new intentional actions, never hidden historical differences.
            active_env = {**target_env, "DEMO_ACCESS_ENABLED": "true"}
            docker_state.compose(target["project"], "up", "--no-build", "-d", "--wait", "api", "web", environment=active_env)
            api = RecoveryApi(target["port"], (target_env["POSTGRES_PASSWORD"],))
            definition = api.json("GET", "/reports/definitions/" + fixture["definition_id"])
            execution = api.json("GET", "/reports/executions/" + fixture["execution_id"])
            profile = api.json("GET", "/dataset-versions/" + fixture["output_version_id"] + "/profile")
            lineage = api.json("GET", "/catalog/datasets/" + fixture["output_dataset_id"] + "?section=lineage")
            restriction = verify_restored_restriction(api, definition, fixture)
            if definition["version"] != 2 or len(definition["revisions"]) != 2 or execution["status"] != "SUCCESS" or profile["row_count"] != 3:
                raise ValueError("Las rutas restauradas no conservan la definición, publicación o perfil esperado.")
            if not {"REPORT_OUTPUT", "REPORT_REVISION", "DERIVED_FROM"}.issubset({row["relation"] for row in lineage["items"]}):
                raise ValueError("El linaje restaurado no expone las relaciones auténticas de publicación.")
            if any(item["running"] and item["service"] in AUTOMATIC for item in docker_state.inventory(target["project"])["containers"]):
                raise ValueError("Una comprobación funcional activó consumidores automáticos.")
            functional = "DEFINITION_REVISIONS_EXECUTION_PROFILE_LINEAGE_CURRENT_BLOCK_PASS"
        return {"restore": receipt["status"], "tables_before": len(before["tables"]), "tables_after": len(after["tables"]),
                "source_state_sha256": docker_state.canonical_hash(before), "restored_projection_sha256": docker_state.canonical_hash(normalized),
                "exact_state_comparison": "PASS", "verified_artifacts": after["verified_artifacts"],
                "verified_source_secrets": after["verified_source_secrets"], "verified_delivery_secrets": after["verified_delivery_secrets"],
                "automatic_processes_started": False, "new_tables_empty": True if legacy else None,
                "no_automatic_classification": True if legacy else None, "target_project": target["project"],
                "functional_restored_bindings": functional, "restored_restrictions": restriction}
    finally:
        docker_state.compose = old_compose
        if claimed:
            cleanup(directory, target, evidence)
        assert_main(parent)


def native_cycle(directory, context, evidence):
    environment = private_environment(directory)
    preflight(directory, context, environment)
    old_compose, started = docker_state.compose, False
    result = None
    try:
        docker_state.compose = compose_adapter(directory, context, environment)
        started = True
        docker_state.compose(context["project"], "up", "--no-build", "-d", "--wait", "--wait-timeout", "300", *NATIVE_FIXTURE_SERVICES)
        docker_state.compose(context["project"], "create", "--no-build", "--no-recreate", *INACTIVE_NATIVE_SERVICES)
        fixture = prepare_native(context, environment)
        backup = evidence / "backup"
        docker_state.backup(context["project"], backup)
        docker_state.verify_backup(backup)
        assert_native_state(json.loads((backup / "state.json").read_text(encoding="utf-8")))
        privacy = scan_backup_plaintext(backup, args_for(directory, context), {**os.environ, **environment}, (environment["POSTGRES_PASSWORD"],))
        restored = restore_compare(context, environment, backup, evidence, fixture=fixture)
        result = {"status": "PASS", "source_version": "0.8.0", "target_version": "0.8.0", "fixture": fixture,
                  "backup_privacy": privacy, "all_thirteen_new_entities_populated": True, **restored}
        return result
    finally:
        try:
            if started:
                source_state = stop_quiescent_population(directory, context, environment)
                if result is not None:
                    result["source_final_state"] = source_state
        finally:
            docker_state.compose = old_compose


def legacy_cycle(parent, evidence):
    source_dir = guard.init("authentic070", available_port(), parent["main_project"])
    source_dir, source = guard.load_context(source_dir)
    environment = private_environment(source_dir)
    environment.update(DEMO_ACCESS_ENABLED="true", DEMO_SEED_ENABLED="false", TRACKVANCE_SMTP_ENABLED="false")
    (source_dir / "test.env").write_text("\n".join(f"{key}={value}" for key, value in environment.items()) + "\n", encoding="utf-8")
    archive, baseline = source_dir / "authentic.zip", source_dir / "authentic"
    baseline.mkdir()
    run(["git", "archive", "--format=zip", "--output", str(archive), AUTHENTIC_070], environment)
    with zipfile.ZipFile(archive) as bundle:
        for entry in bundle.infolist():
            if not (baseline / entry.filename).resolve().is_relative_to(baseline.resolve()):
                raise ValueError("El archive contiene una ruta fuera del origen privado.")
        bundle.extractall(baseline)
    source["compose_base"] = str(baseline / "compose.yml")
    profile = json.loads((source_dir / "compose.json").read_text(encoding="utf-8"))
    profile["services"].pop("report-worker")
    for name, service in profile["services"].items():
        service.pop("volumes", None)
        service["restart"] = "no"
        if name != "postgres":
            service["image"] = source["project"] + (":web" if name == "web" else ":backend")
    (source_dir / "compose.json").write_text(json.dumps(profile, indent=2), encoding="utf-8")
    (source_dir / "isolation.json").write_text(json.dumps(source, indent=2), encoding="utf-8")
    preflight(source_dir, source, environment)
    old_compose, claimed = docker_state.compose, False
    try:
        docker_state.compose = compose_adapter(source_dir, source, environment)
        claimed = True
        run([*args_for(source_dir, source), "build", "api", "web"], environment)
        run([*args_for(source_dir, source), "up", "--no-build", "-d", "--wait", "--wait-timeout", "300", "postgres", "api", "worker", "acquisition-worker", "web"], environment)
        # The backup inventory includes every historical service, while only the
        # workers required for real fixture ingestion are allowed to run.
        run([*args_for(source_dir, source), "create", "--no-build", "delivery-worker", "scheduler", "events-notifications", "events-chaining"], environment)
        api = RecoveryApi(source["port"], (environment["POSTGRES_PASSWORD"],))
        if api.json("GET", "/health")["version"] != "0.7.0":
            raise ValueError("La fuente no ejecuta el commit auténtico 0.7.0.")
        dataset, _acquired, _contract, _approved = acquire_fixture(api, "Authentic070 " + uuid4().hex[:8])
        api.json("POST", "/roles", {"name": "Legacy recovery custom", "permissions": ["datasets:read"]}, expected=201)
        for path, kind in (("/connections", "source_type"), ("/delivery/destinations", "sink_type")):
            api.json("POST", path, {"name": "Encrypted authentic070 " + kind, kind: "POSTGRESQL", "host": "postgres", "port": 5432,
                "database": "tv_v080_test", "username": "tv_v080_test", "password": environment["POSTGRES_PASSWORD"],
                "options": {"sslmode": "disable", "connect_timeout": 5, "query_timeout": 30}}, expected=201)
        # The historical worker records real parse diagnostics, rather than this
        # harness manufacturing an error_details object directly in persistence.
        staged = json.loads(api.request("POST", "/datasets/uploads/stage?filename=invalid.csv", raw=diagnostic_fixture_payload(),
            content_type="application/octet-stream", expected=201))
        failed = api.json("POST", "/datasets/" + dataset["id"] + "/acquisitions", {"upload_id": staged["upload"]["id"]}, expected=202)
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            failure = api.json("GET", "/acquisitions/" + failed["id"])
            if failure["status"] not in {"QUEUED", "RUNNING"}:
                break
            time.sleep(0.5)
        if failure["status"] != "FAILED" or not failure.get("error"):
            raise ValueError("El worker histórico no produjo un fallo de adquisición real.")
        backup = evidence / "backup"
        docker_state.backup(source["project"], backup)
        before = json.loads((backup / "state.json").read_text(encoding="utf-8"))
        if before["schema_version"] != 7 or before["migration"] != "0016_acquisition_diagnostics" or len(before["tables"]) != 42:
            raise ValueError("El backup no procede del estado auténtico 0.7.0/0016.")
        privacy = scan_backup_plaintext(backup, args_for(source_dir, source), {**os.environ, **environment}, (environment["POSTGRES_PASSWORD"],))
        cleanup(source_dir, source, evidence)
        claimed = False
        docker_state.ensure_fresh_project(source["project"])
        result = restore_compare(parent, environment, backup, evidence, legacy=True)
        return {"status": "PASS", "source_version": "0.7.0", "target_version": "0.8.0", "baseline_commit": AUTHENTIC_070,
                "source_destroyed_before_restore": True, "authentic_worker_diagnostics": "PASS", "backup_privacy": privacy, **result}
    finally:
        docker_state.compose = old_compose
        if claimed:
            cleanup(source_dir, source, evidence)
        assert_main(parent)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--context", type=Path, required=True)
    parser.add_argument("--mode", choices=("native", "legacy", "both"), default="both")
    options = parser.parse_args()
    directory, context = guard.load_context(options.context)
    assert_main(context)
    evidence = directory / ("catalog-recovery-" + uuid4().hex[:12])
    evidence.mkdir()
    result, began = {"status": "FAIL", "mode": options.mode, "source_project": context["project"],
                    "main_inventory": "UNCHANGED"}, time.monotonic()
    stage = "native" if options.mode != "legacy" else "legacy"
    try:
        if options.mode in {"native", "both"}:
            native_evidence = evidence / "native"
            native_evidence.mkdir()
            result["native"] = native_cycle(directory, context, native_evidence)
        if options.mode in {"legacy", "both"}:
            stage = "legacy"
            legacy_evidence = evidence / "legacy"
            legacy_evidence.mkdir()
            result["legacy"] = legacy_cycle(context, legacy_evidence)
        result["status"] = "PASS"
    except (ValueError, RuntimeError, OSError, KeyError, subprocess.SubprocessError) as error:
        result.update(error_type=type(error).__name__, failed_stage=stage)
        with (evidence / "diagnostic.private.log").open("w", encoding="utf-8") as diagnostic:
            traceback.print_exc(file=diagnostic)
    finally:
        assert_main(context)
        result["duration_seconds"] = round(time.monotonic() - began, 3)
        serialized = json.dumps(result, ensure_ascii=False, indent=2)
        assert_no_secrets(serialized, (private_environment(directory)["POSTGRES_PASSWORD"],))
        (evidence / "result.json").write_text(serialized, encoding="utf-8")
        print(json.dumps({"status": result["status"], "evidence": str(evidence / "result.json")}))
    return 0 if result["status"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
