"""Real C01–C06 volume certification; every mutation belongs to a UUID stack.

Default limits are deliberately retained, including legacy 100k/10 MiB. A
successful fixture certifies all rows and values, never only a sample or count.
"""
from __future__ import annotations

import argparse
import contextlib
import io
import json
import os
import shutil
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import quote
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(Path(__file__).parent))
import certification_v070 as certification
import volume_cycle as volume
from browser_evidence import run_browser
from corrections_runtime import (
    GROUP_ALIASES,
    GROUP_DEADLINES,
    GROUP_SCENARIOS,
    ScenarioEvidence,
    atomic_json,
    deadline,
    immutable_image_pair,
    private_command,
    source_identity,
)
from v070_cycle import run
from xlsx_fixtures import COLUMNS, HEADER_ROW, generate

volume.COLUMNS = COLUMNS


def checkpoint(phase, rows, state):
    print(json.dumps({"corrections_checkpoint": phase, "rows": rows, "state": state,
                      "at": datetime.now(UTC).isoformat()}), flush=True)


def record_integrity(directory, context, version_id, fixture):
    observed = volume.materialized_hash(directory, context, version_id)
    if observed["rows"] != fixture["rows"] or observed["canonical_rows_sha256"] != fixture["canonical_rows_sha256"]:
        raise AssertionError("La población XLSX cambió o contiene valores diferentes al oráculo completo.")
    script = '''import json
from trackvance.db import SessionLocal
from trackvance.models import DatasetVersion
from trackvance.dataset_scans import version_paths,bounded_scan
with SessionLocal() as db:
 version=db.get(DatasetVersion,__VERSION__)
 with bounded_scan(version_paths(db,version)) as connection:
  result=connection.execute('SELECT count(*),min("__tv_record_number"),max("__tv_record_number"),sum(CASE WHEN "__tv_record_number" != CAST("record_id" AS BIGINT)+__HEADER__ THEN 1 ELSE 0 END) FROM population').fetchone()
 print(json.dumps({'rows':result[0],'first':result[1],'last':result[2],'numbering_mismatches':result[3]}))
'''.replace("__VERSION__", repr(version_id)).replace("__HEADER__", str(HEADER_ROW))
    numbering = json.loads(certification.compose(directory, context, ["exec", "-T", "api", "python", "-c", script]))
    if numbering != {"rows": fixture["rows"], "first": HEADER_ROW + 1,
                     "last": HEADER_ROW + fixture["rows"], "numbering_mismatches": 0}:
        raise AssertionError("La adquisición no conservó los números físicos de Excel.")
    return {**observed, "physical_numbering": numbering}


def register(api, fixture, label, *, dataset=None):
    started = time.monotonic()
    received = api.binary(Path(fixture["path"]))
    transfer_seconds = time.monotonic() - started
    options = {"sheet_name": fixture["sheet"]}
    started = time.monotonic()
    inspection = api.get('/api/v1/datasets/uploads/' + received["upload"]["id"] + '/inspect?reader_options=' + quote(json.dumps(options)))
    inspection_seconds = time.monotonic() - started
    if inspection.get("sampled_rows", 0) > 100:
        raise AssertionError("La petición HTTP leyó más que la muestra autorizada.")
    if dataset is None:
        dataset = api.post("/api/v1/datasets", {"name": label + " " + uuid4().hex[:8], "domain": "Área XLSX certificación"})
    body = {"upload_id": received["upload"]["id"], "reader_options": options,
            "column_overrides": {"record_id": {"logical_type": "STRING", "semantic_tag": "IDENTIFIER"}}}
    key = "xlsx-corrections-" + uuid4().hex
    started = time.monotonic()
    acquisition = api.keyed("/datasets/" + dataset["id"] + "/acquisitions", body, key)
    registration_seconds = time.monotonic() - started
    repeated = api.keyed("/datasets/" + dataset["id"] + "/acquisitions", body, key)
    if acquisition["id"] != repeated["id"] or acquisition["output_version_id"] is not None:
        raise AssertionError("Registro202 no fue idempotente y sólo metadata.")
    return dataset, acquisition, {"transfer_seconds": round(transfer_seconds, 3),
        "inspection_seconds": round(inspection_seconds, 3), "metadata_registration_seconds": round(registration_seconds, 3),
        "transferred_bytes": received["upload"]["size_bytes"], "inspection_sampled_rows": inspection.get("sampled_rows"),
        "inspection_total_rows": inspection.get("row_count"), "inspection_limited": inspection.get("inspection_limited", False)}


def wait_acquisition(api, acquisition_id, timeline, timeout=1800):
    """Observed stage boundaries are polling measurements, never exact worker clocks."""
    started = time.monotonic()
    last = None
    while time.monotonic() - started < timeout:
        if volume.ACTIVE_MEASUREMENTS is not None:
            volume.ACTIVE_MEASUREMENTS.check()
        current = api.get("/api/v1/acquisitions/" + acquisition_id)
        label = current["status"] + ":" + (current.get("stage") or "NONE")
        now = time.time()
        if label != last:
            if timeline:
                timeline[-1]["until_epoch_seconds"] = now
            timeline.append({"stage": label, "from_epoch_seconds": now,
                             "processed_rows_at_first_observation": current["processed_rows"]})
            print(json.dumps({"acquisition": acquisition_id, "stage": label,
                              "processed_rows": current["processed_rows"]}), flush=True)
            last = label
        if current["status"] in volume.TERMINAL:
            timeline[-1]["until_epoch_seconds"] = now
            return current
        time.sleep(1)
    raise TimeoutError("La adquisición XLSX excedió el límite de certificación.")


def stage_resources(timeline, samples):
    result = []
    for interval in timeline:
        start, end = interval["from_epoch_seconds"], interval["until_epoch_seconds"]
        selected = [sample for sample in samples if start <= sample["at"] < end]
        values = [sample["services"].get("acquisition-worker") for sample in selected]
        values = [value for value in values if value]
        same_container = len({value["container_id"] for value in values}) == 1
        cpu = None
        if len(values) >= 2 and same_container:
            cpu = max(0, int(values[-1].get("cpu", {}).get("usage_usec", 0)) - int(values[0].get("cpu", {}).get("usage_usec", 0))) / 1000000
        result.append({**interval, "observed_seconds": round(end - start, 3), "resource_samples": len(values),
            "rss_sample_peak_bytes": max((value["process_sum_rss_bytes"] for value in values), default=None),
            "cgroup_sample_peak_bytes": max((value.get("memory.current", 0) for value in values), default=None),
            "temporary_sample_peak_bytes": max((value["temporary_bytes"] for value in values), default=None),
            "cpu_between_first_and_last_resource_samples_seconds": cpu})
    return {"poll_interval_seconds": 1, "resource_interval_seconds": 2,
        "scope": "Observed API stages; reading includes XML/SST processing. Boundaries and resource samples are approximate; missing stages/samples are not inferred.",
        "stages": result}


def certify_dispatch(directory, context, source_version, additional_source_version, configuration, report):
    certification.assert_main_unchanged(context)
    with volume.phase(report, "dispatch_with_real_busy_worker", directory, context):
        certification.compose(directory, context, ["stop", "scheduler"])
        try:
            certification.compose(directory, context, ["exec", "-T", "api", "python",
                "/app/scripts/tests/corrections_dispatch.py", "--api-url", "http://api:8000",
                "--source-version", source_version, "--configuration", configuration,
                "--additional-source-version", additional_source_version,
                "--evidence", "/tmp/corrections-dispatch", "--scheduler-paused"])
        finally:
            try:
                encoded = certification.compose(directory, context, ["exec", "-T", "api", "python", "-c",
                    "from pathlib import Path; p=Path('/tmp/corrections-dispatch/dispatch-results.json'); print(p.read_text() if p.exists() else '{}')"])
                evidence = json.loads(encoded)
                report["dispatch"] = evidence
                (directory / "dispatch-results.json").write_text(json.dumps(evidence, indent=2), encoding="utf-8")
            finally:
                certification.compose(directory, context, ["up", "--no-build", "--detach", "--no-deps", "scheduler"])
        if evidence.get("status") != "PASS":
            raise AssertionError("La prueba PostgreSQL de dispatch C05 no terminó PASS.")
    certification.assert_main_unchanged(context)


def certify_unread_restart(api, directory, context, notification_id, report):
    path = "/api/v1/notifications/inbox/" + notification_id
    api.post(path + "/read", {}, expected=(200,))
    api.post(path + "/unread", {}, expected=(200,))
    before = api.get("/api/v1/notifications/unread-count")["unread_count"]
    certification.assert_main_unchanged(context)
    with volume.phase(report, "personal_unread_api_restart", directory, context):
        certification.compose(directory, context, ["restart", "api"])
        certification.compose(directory, context, ["up", "--no-build", "--detach", "--wait",
            "--wait-timeout", "120", "--no-deps", "api"])
        if api.get("/api/v1/health/ready")["status"] != "ready":
            raise AssertionError("La API aislada no volvió a ready tras reiniciar.")
        items = api.get("/api/v1/notifications/inbox?read_state=UNREAD&limit=100")["items"]
        notification = next(item for item in items if item["id"] == notification_id)
        if notification["read_at"] is not None:
            raise AssertionError("La transición no leída no persistió tras reiniciar API.")
        count = api.get("/api/v1/notifications/unread-count")["unread_count"]
        if count != before:
            raise AssertionError("El contador cambió al reiniciar API en el proyecto aislado.")
    report["unread_restart"] = {"status": "PASS", "notification_id": notification_id,
                                "read_at": None, "unread_count": count, "kept_unread_for_native_backup": True}
    certification.assert_main_unchanged(context)


def certify_inbox(api, source, report):
    inbox = volume.wait(api, "/notifications/inbox?limit=100", timeout=60,
        predicate=lambda value: any(item["resource_id"] == source["id"] for item in value["items"]))
    item = next(item for item in inbox["items"] if item["resource_id"] == source["id"])
    path = "/api/v1/notifications/inbox/" + item["id"]
    api.post(path + "/read", {}, expected=(200,))
    before = api.get("/api/v1/notifications/unread-count")
    api.post(path + "/unread", {}, expected=(200,))
    api.post(path + "/unread", {}, expected=(200,))
    after = api.get("/api/v1/notifications/inbox?limit=100")
    after_count = api.get("/api/v1/notifications/unread-count")
    unread = api.get("/api/v1/notifications/inbox?read_state=UNREAD&limit=100")
    current = next(record for record in after["items"] if record["id"] == item["id"])
    if current["read_at"] is not None or not any(record["id"] == item["id"] for record in unread["items"]):
        raise AssertionError("Marcar no leída no persistió o el filtro no refleja el setter.")
    if after_count["unread_count"] != before["unread_count"] + 1:
        raise AssertionError("El contador de no leídas no es idempotente.")
    api.post(path + "/read", {}, expected=(200,))
    final = api.get("/api/v1/notifications/unread-count")
    if final["unread_count"] != before["unread_count"]:
        raise AssertionError("Volver a marcar leída no restauró el contador.")
    report["unread"] = {"notification_id": item["id"], "read_unread_unread_read": "PASS", "counter_delta": 1}


def cancel_during_reading(api, directory, context, fixture, report):
    with volume.phase(report, "cancel_during_reading", directory, context):
        controlled = context.get("profile") == "functional"
        if controlled:
            arm_reader(directory, context)
        dataset, acquisition, _ = register(api, fixture, "XLSX cancel")
        active = volume.wait(api, "/acquisitions/" + acquisition["id"],
            predicate=lambda value: value["status"] == "RUNNING" and value["processed_rows"] >= (20 if controlled else 5000))
        api.post("/api/v1/acquisitions/" + acquisition["id"] + "/cancel", {}, expected=(200,))
        cancelled = volume.wait(api, "/acquisitions/" + acquisition["id"])
        if cancelled["status"] != "CANCELLED" or cancelled["output_version_id"] is not None or api.get("/api/v1/datasets/" + dataset["id"])["versions"]:
            raise AssertionError("La cancelación dejó una DatasetVersion parcial.")
        report["cancel"] = {"status": cancelled["status"], "observed_records_before_cancel": active["processed_rows"], "versions": 0}


def recover_crashed_acquisition(api, directory, context, fixture, report):
    with volume.phase(report, "xlsx_crash_lease_recovery", directory, context):
        controlled = context.get("profile") == "functional"
        if controlled:
            arm_reader(directory, context)
        dataset, acquisition, _ = register(api, fixture, "XLSX crash")
        active = volume.wait(api, "/acquisitions/" + acquisition["id"],
            predicate=lambda value: value["status"] == "RUNNING" and value["processed_rows"] >= (20 if controlled else 10000))
        certification.assert_main_unchanged(context)
        certification.compose(directory, context, ["kill", "--signal", "SIGKILL", "acquisition-worker"])
        try:
            certification.compose(directory, context, ["up", "--no-build", "--detach", "--no-deps", "acquisition-worker"])
            recovered = volume.wait(api, "/acquisitions/" + acquisition["id"])
        finally:
            certification.compose(directory, context, ["up", "--no-build", "--detach", "--no-deps", "acquisition-worker"])
        if recovered["status"] != "SUCCESS" or recovered["attempts"] < 2 or recovered["attempt_id"] == active["attempt_id"] or len(api.get("/api/v1/datasets/" + dataset["id"])["versions"]) != 1:
            raise AssertionError("La recuperación no cercó el intento anterior y publicó una única versión completa.")
        report["recovery"] = {"attempts": recovered["attempts"], "versions": 1,
            "full_integrity": record_integrity(directory, context, recovered["output_version_id"], fixture)}


def cancel_and_recover(api, directory, context, fixture, report):
    cancel_during_reading(api, directory, context, fixture, report)
    recover_crashed_acquisition(api, directory, context, fixture, report)


def certify_row_limit_failure(api, directory, context, baseline, report):
    maximum = report["effective_limits"]["xlsx_max_rows"]
    fixture = generate(directory / "xlsx-fixtures", maximum + 1, strings="inline",
        late_type_after=baseline["fixture"].get("late_type_after", 100000))
    dataset = api.get("/api/v1/datasets/" + baseline["acquisition"]["dataset_id"])
    original_ids = [version["id"] for version in dataset["versions"]]
    with volume.phase(report, "xlsx_row_limit_failure_preserves_previous_version", directory, context):
        _, acquisition, transfer = register(api, fixture, "XLSX limit failure", dataset=dataset)
        failed = volume.wait(api, "/acquisitions/" + acquisition["id"])
        if (failed["status"] != "FAILED" or failed["error_code"] != "ACQUISITION_ROW_LIMIT"
                or failed["output_version_id"] is not None or not failed["error"]["details"]
                or not failed["error"]["reference"]):
            raise AssertionError("El exceso XLSX no conservó la causa y detalle seguros sin publicar.")
        expected = {"limit": "data_rows", "maximum": maximum, "observed": maximum + 1}
        if (any(failed["error"]["details"].get(key) != value for key, value in expected.items())
                or failed["error"]["reference"] != failed["id"]):
            raise AssertionError("El diagnóstico real no declara la cota, observación y referencia exactas.")
        history = api.get("/api/v1/acquisitions?dataset_id=" + dataset["id"] + "&limit=100")
        historical = next(item for item in history["items"] if item["id"] == failed["id"])
        if historical["error"] != failed["error"]:
            raise AssertionError("El historial presenta una causa distinta del detalle de adquisición.")
        inbox = volume.wait(api, "/notifications/inbox?limit=100", timeout=60,
            predicate=lambda value: any(item["resource_id"] == failed["id"] for item in value["items"]))
        notice = next(item for item in inbox["items"] if item["resource_id"] == failed["id"])
        if notice["error"] != failed["error"]:
            raise AssertionError("La notificación perdió el diagnóstico público de la adquisición.")
        current = api.get("/api/v1/datasets/" + dataset["id"])
        if [version["id"] for version in current["versions"]] != original_ids:
            raise AssertionError("El fallo agregó o alteró las versiones anteriormente publicadas.")
        retained = record_integrity(directory, context, baseline["acquisition"]["output_version_id"], baseline["fixture"])
        report["row_limit_failure"] = {"status": "PASS", "acquisition_id": failed["id"],
            "attempt_status": failed["status"], "rows_in_fixture": fixture["rows"], "configured_maximum": maximum,
            "diagnostic": failed["error"], "notification_id": notice["id"], "transfer": transfer,
            "no_partial_version": True, "previous_version_ids": original_ids, "retained_full_integrity": retained,
            "new_diagnostic_kept_for_native_backup": True}


def certify(directory, context, args):
    certification.assert_main_unchanged(context)
    docker = json.loads(certification.command(["docker", "info", "--format", "{{json .}}"] ))
    if docker["MemTotal"] < 6 * volume.GIB or shutil.disk_usage(ROOT).free < 20 * volume.GIB:
        raise RuntimeError("Recursos insuficientes para la certificación real obligatoria.")
    volume.RESOURCE_MEMORY_BUDGET = min(6 * volume.GIB, docker["MemTotal"] - 2 * volume.GIB)
    api = volume.VolumeApi(f'http://127.0.0.1:{context["port"]}', 1800)
    session = api.post("/api/v1/auth/demo", {}, expected=(200,))
    api.csrf = session["csrf_token"]
    report = {"schema_version": 1, "version": "0.8.0", "cycle": "C01-C06", "project": context["project"], "status": "FAIL", "tiers": [],
        "source_sha": certification.command(["git", "rev-parse", "HEAD"]).strip(),
        "source_tree_dirty": bool(certification.command(["git", "status", "--porcelain"]).strip()),
        "effective_limits": api.get("/api/v1/system/engines")["limits"]["acquisition"],
        "resource_profile": {"docker_memory_bytes": docker["MemTotal"], "bounded_backend_budget_bytes": volume.RESOURCE_MEMORY_BUDGET,
            "xlsx_test_limit_overrides": False, "host_disk_reserve_bytes": 20 * volume.GIB}}
    destination, _, schema, _ = volume.destination_fixture(api, directory, context, uuid4().hex[:8])
    report["playwright"] = {"destination_id": destination["id"], "schema": schema}
    try:
        for rows in args.rows:
            for strings in ("inline", "shared"):
                fixture = generate(directory / "xlsx-fixtures", rows, strings=strings)
                tier = {"rows": rows, "strings": strings, "fixture": fixture, "status": "FAIL"}
                report["tiers"].append(tier)
                tier_started = time.monotonic()
                timeline = []
                with volume.phase(tier, "xlsx_acquisition_whole", directory, context) as metrics:
                    dataset, acquisition, transfer = register(api, fixture, f"XLSX {rows} {strings}")
                    complete = wait_acquisition(api, acquisition["id"], timeline)
                    if complete["status"] != "SUCCESS" or complete["processed_rows"] != rows or complete["processed_bytes"] != fixture["observed_utf8_bytes"]:
                        raise AssertionError("La adquisición completa no coincide con el oráculo XLSX.")
                    tier["acquisition"] = {"id": complete["id"], "dataset_id": complete["dataset_id"], "output_version_id": complete["output_version_id"], "duration_seconds": complete["duration_seconds"], **transfer}
                    tier["integrity"] = record_integrity(directory, context, complete["output_version_id"], fixture)
                    profile = api.get('/api/v1/dataset-versions/' + complete["output_version_id"] + '/profile')
                    tier["profile"] = volume.validate_profile(profile, fixture)
                    late = next(column for column in profile["profile"]["columns"] if column["name"] == "late_type")
                    if rows > 100000 and late["logical_type"] != "STRING":
                        raise AssertionError("La inferencia no consideró los cambios posteriores a100k.")
                tier["observed_worker_stages"] = stage_resources(timeline, metrics.samples)
                tier["acquisition_and_full_verification_elapsed_seconds"] = round(time.monotonic() - tier_started, 3)
                if rows == max(args.rows) and strings == "inline":
                    volume.chain(api, directory, context, fixture, dataset, complete, destination, schema, tier,
                        column_types={column['name']: column['logical_type'] for column in profile['profile']['columns']})
                    certify_inbox(api, complete, tier)
                    tier["upload_to_confirmed_delivery_and_inbox_elapsed_seconds"] = round(time.monotonic() - tier_started, 3)
                tier["status"] = "PASS"
        chained = next(tier for tier in report["tiers"] if tier["rows"] == max(args.rows) and tier["strings"] == "inline")
        if args.with_dispatch:
            additional = next(tier for tier in report["tiers"] if tier["rows"] == max(args.rows) and tier["strings"] == "shared")
            certify_dispatch(directory, context, chained["acquisition"]["output_version_id"],
                additional["acquisition"]["output_version_id"], chained["delivery"]["configuration_id"], report)
        if args.with_recovery:
            fixture = generate(directory / "xlsx-fixtures", max(args.rows), strings="shared")
            cancel_and_recover(api, directory, context, fixture, report)
            if max(args.rows) >= 1000000:
                certify_row_limit_failure(api, directory, context, chained, report)
        if args.with_browser:
            for rows in (400000, 1000000):
                fixture = generate(directory / "xlsx-fixtures", rows, strings="shared")
                metadata = Path(fixture["path"]).with_suffix(".json")
                environment = {**os.environ, "TV_E2E_URL": f'http://localhost:{context["port"]}', "TV_E2E_PRIVATE_ARTIFACTS": "1",
                    "TV_CORRECTIONS_E2E": "true", "TV_CORRECTIONS_PROJECT": context["project"], "TV_CORRECTIONS_FIXTURE": fixture["path"],
                    "TV_CORRECTIONS_METADATA": str(metadata), "TV_CORRECTIONS_DESTINATION": destination["id"], "TV_CORRECTIONS_SCHEMA": schema}
                checkpoint("browser", rows, "START")
                browser = run_browser(shutil.which("pnpm"), ["tests-e2e/corrections-volume.spec.ts"], root=ROOT,
                    project=context["project"] + f"-xlsx-{rows}", environment=environment, evidence=directory)
                if browser.get("skipped", 0) or browser.get("expected") != 1:
                    raise AssertionError("No se ejecutó la prueba de navegador XLSX obligatoria completa.")
                checkpoint("browser", rows, "PASS")
                reports = list((ROOT / ".codex-local" / "browser-results" / (context["project"] + f"-xlsx-{rows}")).rglob("corrections-ui.json"))
                if len(reports) != 1:
                    raise AssertionError("Falta evidencia integral del navegador XLSX.")
                ui = json.loads(reports[0].read_text(encoding="utf-8"))
                for key in ("source_version_id", "output_version_id"):
                    verification = "browser_full_source_verification" if key == "source_version_id" else "browser_full_output_verification"
                    checkpoint(verification, rows, "START")
                    record_integrity(directory, context, ui[key], fixture)
                    checkpoint(verification, rows, "PASS")
                checkpoint("browser_full_sql_verification", rows, "START")
                target = volume.target_hash(directory, context, schema, ui["table"])
                if target["rows"] != rows or target["canonical_rows_sha256"] != fixture["canonical_rows_sha256"]:
                    raise AssertionError("El navegador no conservó todas las filas/valores en SQL.")
                checkpoint("browser_full_sql_verification", rows, "PASS")
                report.setdefault("browser", []).append({"rows": rows, "status": "PASS", "target": target, "ui": ui})
        checkpoint("personal_unread_api_restart", max(args.rows), "START")
        certify_unread_restart(api, directory, context, chained["unread"]["notification_id"], report)
        checkpoint("personal_unread_api_restart", max(args.rows), "PASS")
        if args.with_native_recovery:
            native_directory = directory / ("corrections-native-recovery-" + uuid4().hex[:12])
            checkpoint("native_backup_restore", max(args.rows), "START")
            run([sys.executable, "scripts/tests/docker_backup_cycle.py", "--v070-context", str(directory),
                 "--evidence-dir", str(native_directory)], directory, "corrections-native-recovery")
            report["native_recovery"] = "PASS"
            report["native_recovery_report"] = json.loads((native_directory / "result.json").read_text(encoding="utf-8"))
            notification = next(item for item in api.get("/api/v1/notifications/inbox?read_state=UNREAD&limit=100")["items"]
                                if item["id"] == chained["unread"]["notification_id"])
            if notification["read_at"] is not None:
                raise AssertionError("El backup/restore no conservó la transición personal no leída.")
            report["unread_restart"]["native_state_fingerprint_comparison"] = "PASS"
            checkpoint("native_backup_restore", max(args.rows), "PASS")
        certification.assert_main_unchanged(context)
        report["status"] = "PASS"
        return report
    finally:
        encoded = json.dumps(report, ensure_ascii=False, indent=2)
        stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S.%fZ")
        (directory / ("corrections-evidence-" + stamp + ".json")).write_text(encoded, encoding="utf-8")
        (directory / "corrections-evidence.json").write_text(encoded, encoding="utf-8")


def acquire_group_fixture(api, directory, context, rows, variant, *, late_type_after=100000):
    report, timeline = {}, []
    began = time.monotonic()
    fixture = generate(directory / "xlsx-fixtures", rows, strings=variant, late_type_after=late_type_after)
    report.update(fixture=fixture, fixture_prepare_seconds=round(time.monotonic() - began, 6))
    with volume.phase(report, "xlsx_acquisition_whole", directory, context) as metrics:
        dataset, acquisition, transfer = register(api, fixture, f"XLSX {rows} {variant}")
        completed = wait_acquisition(api, acquisition["id"], timeline)
        if (completed["status"] != "SUCCESS" or completed["processed_rows"] != rows
                or completed["processed_bytes"] != fixture["observed_utf8_bytes"]):
            raise AssertionError("La adquisición completa no coincide con el oráculo XLSX.")
        report.update(dataset=dataset, acquisition=completed, transfer=transfer)
        began = time.monotonic()
        report["integrity"] = record_integrity(directory, context, completed["output_version_id"], fixture)
        report["full_integrity_verification_seconds"] = round(time.monotonic() - began, 6)
        profile = api.get('/api/v1/dataset-versions/' + completed["output_version_id"] + '/profile')
        report["profile"] = volume.validate_profile(profile, fixture)
        report["column_types"] = {column["name"]: column["logical_type"] for column in profile["profile"]["columns"]}
        if rows > late_type_after and report["column_types"]["late_type"] != "STRING":
            raise AssertionError("La inferencia no consideró el cambio tardío de tipo.")
    report["observed_worker_stages"] = stage_resources(timeline, metrics.samples)
    report.update(status="PASS", rows=rows, strings=variant)
    return report


def chain_group_fixture(api, directory, context, baseline, destination, schema):
    report = {"fixture": baseline["fixture"], "acquisition": baseline["acquisition"], "dataset": baseline["dataset"]}
    volume.chain(api, directory, context, baseline["fixture"], baseline["dataset"], baseline["acquisition"],
        destination, schema, report, column_types=baseline["column_types"])
    certify_inbox(api, baseline["acquisition"], report)
    return report


def browser_group_fixture(api, directory, context, rows, destination, schema):
    report = {}
    began = time.monotonic()
    fixture = generate(directory / "xlsx-fixtures", rows, strings="shared")
    report.update(fixture=fixture, fixture_prepare_seconds=round(time.monotonic() - began, 6))
    environment = {**os.environ, "TV_E2E_URL": f'http://localhost:{context["port"]}', "TV_E2E_PRIVATE_ARTIFACTS": "1",
        "TV_CORRECTIONS_E2E": "true", "TV_CORRECTIONS_PROJECT": context["project"], "TV_CORRECTIONS_FIXTURE": fixture["path"],
        "TV_CORRECTIONS_METADATA": str(Path(fixture["path"]).with_suffix(".json")),
        "TV_CORRECTIONS_DESTINATION": destination["id"], "TV_CORRECTIONS_SCHEMA": schema}
    with volume.phase(report, "real_browser_and_whole_population_verification", directory, context):
        checkpoint("browser", rows, "START")
        browser = run_browser(shutil.which("pnpm"), ["tests-e2e/corrections-volume.spec.ts"], root=ROOT,
            project=context["project"] + f"-xlsx-{rows}", environment=environment, evidence=directory,
            timeout_seconds=2400)
        if browser.get("skipped", 0) or browser.get("expected") != 1:
            raise AssertionError("No se ejecutó la prueba de navegador XLSX obligatoria completa.")
        reports = list((ROOT / ".codex-local" / "browser-results" / (context["project"] + f"-xlsx-{rows}")).rglob("corrections-ui.json"))
        if len(reports) != 1:
            raise AssertionError("Falta evidencia integral del navegador XLSX.")
        report.update(browser=browser, ui=json.loads(reports[0].read_text(encoding="utf-8")))
        for key, result_key in (("source_version_id", "source_integrity"), ("output_version_id", "output_integrity")):
            began = time.monotonic()
            report[result_key] = record_integrity(directory, context, report["ui"][key], fixture)
            report[result_key + "_seconds"] = round(time.monotonic() - began, 6)
        began = time.monotonic()
        report["target"] = volume.target_hash(directory, context, schema, report["ui"]["table"])
        report["target_verification_seconds"] = round(time.monotonic() - began, 6)
        if report["target"]["rows"] != rows or report["target"]["canonical_rows_sha256"] != fixture["canonical_rows_sha256"]:
            raise AssertionError("El navegador no conservó todas las filas/valores en SQL.")
    return report


def native_recovery_with_diagnostic(directory):
    """Keep closed child failure facts before the owning fixture is cleaned."""
    from ci.failure_diagnostics import MAX_BYTES, sanitize_recovery_summary
    native = directory / ("corrections-native-recovery-" + uuid4().hex[:12])
    path = native / "result.json"
    failure = None

    def read_result():
        if (native.is_symlink() or path.is_symlink() or not path.is_file()
                or path.stat().st_size > MAX_BYTES):
            raise ValueError("XLSX_NATIVE_RESULT_UNAVAILABLE")
        value = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(value, dict):
            raise TypeError("XLSX_NATIVE_RESULT_INVALID")
        return value

    try:
        private_command([sys.executable, "scripts/tests/docker_backup_cycle.py", "--v070-context", str(directory),
            "--evidence-dir", str(native)], directory, "corrections-native-recovery", seconds=1800)
        result = read_result()
        if result.get("status") != "PASS":
            raise ValueError("XLSX_NATIVE_RESULT_NOT_PASS")
        return result
    except BaseException as error:
        failure = error
        raise
    finally:
        try:
            try:
                summary, present = sanitize_recovery_summary(read_result()), True
            except (OSError, ValueError, TypeError, RecursionError):
                summary, present = {"status": "FAIL"}, False
            atomic_json(directory / "native-recovery-diagnostic.json", {"schema_version": 1,
                "kind": "RECOVERY_PARTIAL_SUMMARY", "profile": "native", "summary_present": present,
                "result": summary})
        except (OSError, ValueError):
            # The child exception remains authoritative even if its independent
            # diagnostic cannot be persisted during cleanup or disk failure.
            if failure is None:
                raise


def certify_group(directory, context, args, images, evidence):
    certification.assert_main_unchanged(context)
    docker = json.loads(certification.command(["docker", "info", "--format", "{{json .}}"] ))
    if docker["MemTotal"] < 6 * volume.GIB or shutil.disk_usage(ROOT).free < 20 * volume.GIB:
        evidence.write_group("FAIL", error={"code": "XLSX_RESOURCE_PREFLIGHT", "type": "RuntimeError"})
        raise RuntimeError("Recursos insuficientes para la certificación real obligatoria.")
    volume.RESOURCE_MEMORY_BUDGET = min(6 * volume.GIB, docker["MemTotal"] - 2 * volume.GIB)
    api = volume.VolumeApi(f'http://127.0.0.1:{context["port"]}', 1800)
    api.csrf = api.post("/api/v1/auth/demo", {}, expected=(200,))["csrf_token"]
    limits = api.get("/api/v1/system/engines")["limits"]["acquisition"]
    if limits["xlsx_max_rows"] != 1000000:
        raise ValueError("Mandatory XLSX certification requires the unchanged1M row limit")
    baseline = None
    if args.group == "corrections-acquisition":
        for rows in (100000, 100001, 400000, 1000000):
            for variant in ("inline", "shared"):
                result = evidence.scenario(f"acquisition-{rows}-{variant}", rows, variant,
                    lambda rows=rows, variant=variant: acquire_group_fixture(api, directory, context, rows, variant), seconds=2400)
                if rows == 1000000 and variant == "inline":
                    baseline = result
        def limit_case():
            result = {"effective_limits": limits}
            certify_row_limit_failure(api, directory, context, baseline, result)
            return result["row_limit_failure"] | {"phases": result.get("phases", {}), "effective_limits": limits}
        evidence.scenario("row-limit-preserves-version-1000001", 1000001, "inline", limit_case, seconds=1800)
    elif args.group == "corrections-browser":
        destination, _, schema, _ = volume.destination_fixture(api, directory, context, uuid4().hex[:8])
        for rows in (400000, 1000000):
            evidence.scenario(f"browser-{rows}-shared", rows, "shared",
                lambda rows=rows: browser_group_fixture(api, directory, context, rows, destination, schema), seconds=3000)
    else:
        destination, _, schema, _ = volume.destination_fixture(api, directory, context, uuid4().hex[:8])
        baseline = evidence.scenario("prerequisite-acquisition-1000000-inline", 1000000, "inline",
            lambda: acquire_group_fixture(api, directory, context, 1000000, "inline"), seconds=2400, prerequisite=True)
        if args.group == "corrections-dispatch":
            chained = evidence.scenario("chain-1000000-inline", 1000000, "inline",
                lambda: chain_group_fixture(api, directory, context, baseline, destination, schema), seconds=2400)
            additional = evidence.scenario("prerequisite-acquisition-1000000-shared", 1000000, "shared",
                lambda: acquire_group_fixture(api, directory, context, 1000000, "shared"), seconds=2400, prerequisite=True)
            def dispatch_case():
                result = {}
                certify_dispatch(directory, context, baseline["acquisition"]["output_version_id"],
                    additional["acquisition"]["output_version_id"], chained["delivery"]["configuration_id"], result)
                return result["dispatch"] | {"phases": result.get("phases", {})}
            evidence.scenario("dispatch-1000000-two-datasets", 1000000, "inline+shared", dispatch_case, seconds=2400)
            def restart_case():
                result = {}
                certify_unread_restart(api, directory, context, chained["unread"]["notification_id"], result)
                return result["unread_restart"] | {"phases": result.get("phases", {})}
            evidence.scenario("unread-api-restart", 1000000, "inline", restart_case, seconds=600)
        else:
            chained = evidence.scenario("prerequisite-chain-1000000-inline", 1000000, "inline",
                lambda: chain_group_fixture(api, directory, context, baseline, destination, schema), seconds=2400, prerequisite=True)
            def cancel_case():
                fixture = generate(directory / "xlsx-fixtures", 1000000, strings="shared")
                result = {"fixture": fixture}
                cancel_during_reading(api, directory, context, fixture, result)
                return result["cancel"] | {"fixture": fixture, "phases": result.get("phases", {})}
            cancelled = evidence.scenario("cancel-1000000-shared", 1000000, "shared", cancel_case, seconds=1200)
            fixture = cancelled["fixture"]
            def crash_case():
                result = {"fixture": fixture}
                recover_crashed_acquisition(api, directory, context, fixture, result)
                return result["recovery"] | {"fixture": fixture, "phases": result.get("phases", {})}
            evidence.scenario("crash-lease-1000000-shared", 1000000, "shared", crash_case, seconds=2400)
            def native_case():
                result = {"effective_limits": limits}
                certify_row_limit_failure(api, directory, context, baseline, result)
                certify_unread_restart(api, directory, context, chained["unread"]["notification_id"], result)
                # The immutable backup contract requires all nine original consumers;
                # unused web remains created/stopped, never an extra active workload.
                result["native_recovery_report"] = native_recovery_with_diagnostic(directory)
                notice = next(item for item in api.get("/api/v1/notifications/inbox?read_state=UNREAD&limit=100")["items"]
                    if item["id"] == chained["unread"]["notification_id"])
                if notice["read_at"] is not None:
                    raise AssertionError("El backup/restore no conservó la transición personal no leída.")
                result["unread_restart"]["native_state_fingerprint_comparison"] = "PASS"
                return result
            evidence.scenario("native-backup-restore", 1000000, "inline+shared", native_case, seconds=3000)
    certification.assert_main_unchanged(context)
    evidence.finish(runtime_images=images, main_inventory="UNCHANGED", xlsx_test_limit_overrides=False)
    return evidence.directory


def arm_reader(directory, context):
    certification.compose(directory, context, ["exec", "-T", "acquisition-worker", "python", "-c",
        ("from pathlib import Path; Path('/tmp/trackvance-reader-reached').unlink(missing_ok=True); "
         "Path('/tmp/trackvance-reader-arm').write_text('ONE_SHOT')")])


def prepare_functional(directory, context):
    context["profile"] = "functional"
    override = json.loads((directory / "compose.json").read_text(encoding="utf-8"))
    for name in certification.SERVICES:
        if name not in {"postgres", "web"}:
            override["services"][name].setdefault("environment", {}).update(
                TRACKVANCE_ACQUISITION_BATCH_ROWS="20", TRACKVANCE_ACQUISITION_XLSX_MAX_ROWS="120")
    override["services"]["acquisition-worker"]["command"] = ["python", "/app/scripts/tests/controlled_acquisition_worker.py"]
    atomic_json(directory / "compose.json", override)
    atomic_json(directory / "isolation.json", context)


def certify_functional(directory, context, images, evidence):
    api = volume.VolumeApi(f'http://127.0.0.1:{context["port"]}', 180)
    api.csrf = api.post("/api/v1/auth/demo", {}, expected=(200,))["csrf_token"]
    limits = api.get("/api/v1/system/engines")["limits"]["acquisition"]
    if limits["xlsx_max_rows"] != 120 or limits["batch_rows"] != 20:
        raise ValueError("Functional fixture bounds were not applied to the private test context")
    acquired = {}
    # Preserve the deep fixture's numeric-to-text transition across real batches.
    # Pure numeric short inputs otherwise change INT64 to DECIMAL after Intake.
    for variant in ("inline", "shared"):
        acquired[variant] = evidence.scenario(f"acquisition-120-{variant}", 120, variant,
            lambda variant=variant: acquire_group_fixture(api, directory, context, 120, variant, late_type_after=60), seconds=180)
    baseline = acquired["inline"]
    def limit_case():
        result = {"effective_limits": limits}
        certify_row_limit_failure(api, directory, context, baseline, result)
        return result["row_limit_failure"]
    evidence.scenario("row-limit-preserves-version-121", 121, "inline", limit_case, seconds=180)
    destination, _, schema, _ = volume.destination_fixture(api, directory, context, uuid4().hex[:8])
    chained = evidence.scenario("chain-120-inline", 120, "inline",
        lambda: chain_group_fixture(api, directory, context, baseline, destination, schema), seconds=240)
    fixture = acquired["shared"]["fixture"]
    def cancel_case():
        result = {}
        cancel_during_reading(api, directory, context, fixture, result)
        return result["cancel"] | {"fixture": fixture, "controlled_checkpoint": True}
    evidence.scenario("cancel-120-shared", 120, "shared", cancel_case, seconds=180)
    def crash_case():
        result = {}
        recover_crashed_acquisition(api, directory, context, fixture, result)
        return result["recovery"] | {"fixture": fixture, "controlled_checkpoint": True}
    evidence.scenario("crash-lease-120-shared", 120, "shared", crash_case, seconds=240)
    def restart_case():
        result = {}
        certify_unread_restart(api, directory, context, chained["unread"]["notification_id"], result)
        return result["unread_restart"]
    evidence.scenario("unread-api-restart-functional", 120, "inline", restart_case, seconds=180)
    def automation_case():
        certification.compose(directory, context, ["exec", "-T", "api", "python",
            "/app/scripts/tests/automation_cycle.py", "--evidence", "/tmp/functional-automation"])
        certification.compose(directory, context, ["cp", "api:/tmp/functional-automation/automation-results.json",
            str(directory / "automation-results.json")])
        return json.loads((directory / "automation-results.json").read_text(encoding="utf-8"))
    evidence.scenario("automation-integrated-functional", None, None, automation_case, seconds=240)
    certification.assert_main_unchanged(context)
    evidence.finish(runtime_images=images, main_inventory="UNCHANGED", xlsx_test_limit_overrides=True,
        profile="functional", effective_limits=limits, product_default_xlsx_max_rows=1000000)


def parse_arguments(arguments=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--context", type=Path)
    parser.add_argument("--port", type=int, default=32076)
    parser.add_argument("--rows", type=int, nargs="+", default=[100000, 100001, 400000, 1000000])
    parser.add_argument("--with-browser", action="store_true")
    parser.add_argument("--with-recovery", action="store_true")
    parser.add_argument("--with-native-recovery", action="store_true")
    parser.add_argument("--with-dispatch", action="store_true")
    parser.add_argument("--group", choices=["all", *GROUP_ALIASES, *GROUP_SCENARIOS], default="all")
    parser.add_argument("--reuse-images", action="store_true", help="Require the verified same-SHA CI image manifest; never rebuild")
    parser.add_argument("--backend-image", help="Reusable same-SHA backend image inspected to its immutable ID")
    parser.add_argument("--web-image", help="Reusable same-SHA web image inspected to its immutable ID")
    args = parser.parse_args(arguments)
    args.group = GROUP_ALIASES.get(args.group, args.group)
    if bool(args.backend_image) != bool(args.web_image):
        parser.error("Reusable backend/web images must be supplied together")
    if args.with_dispatch and max(args.rows) < 1000000:
        parser.error("C05 exige una población publicada de al menos 1M.")
    if args.group != "all" and args.rows != [100000, 100001, 400000, 1000000]:
        parser.error("Independent mandatory groups retain every configured population")
    if args.reuse_images and args.backend_image:
        parser.error("CI manifest reuse cannot be mixed with explicit image arguments")
    return args


def services_for(group):
    if group == "corrections-acquisition":
        return ("postgres", "api", "web", "acquisition-worker", "events-notifications")
    return certification.SERVICES


def prepare_images(directory, context, args, sha):
    if args.reuse_images:
        from ci_images import verified_images
        references = verified_images()
        if not references:
            raise ValueError("Image reuse requires the verified current-SHA manifest")
        backend, web = references["backend"], references["web"]
    elif args.backend_image:
        backend, web = args.backend_image, args.web_image
    else:
        backend, web = context["project"] + ":backend", context["project"] + ":web"
        private_command(["docker", "build", "--label", "org.opencontainers.image.revision=" + sha,
            "-t", backend, "-f", "backend/Dockerfile", "."], directory, "backend-build")
        private_command(["docker", "build", "--label", "org.opencontainers.image.revision=" + sha,
            "-t", web, "-f", "deploy/docker/frontend.Dockerfile", "."], directory, "web-build")
    images = immutable_image_pair(backend, web, sha, certification.command)
    override = json.loads((directory / "compose.json").read_text(encoding="utf-8"))
    for service in certification.SERVICES:
        if service != "postgres":
            override["services"][service]["image"] = images["web" if service == "web" else "backend"]["id"]
    context["image"] = images["backend"]["id"]
    atomic_json(directory / "compose.json", override)
    atomic_json(directory / "isolation.json", context)
    return images


def main():
    args = parse_arguments()
    managed = not args.context
    if managed:
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            certification.init("corrections" if args.group == "all" else args.group, args.port)
        args.context = Path(json.loads(output.getvalue())["context"])
    directory, context = certification.load_context(args.context)
    started = False
    images = {}
    sha = source_identity(certification.command)
    evidence = ScenarioEvidence(directory, args.group, sha) if args.group != "all" else None
    try:
        with deadline(GROUP_DEADLINES[args.group]) if evidence else contextlib.nullcontext():
            if managed:
                images = prepare_images(directory, context, args, sha)
                if args.group == "corrections-functional":
                    prepare_functional(directory, context)
                started = True
                certification.compose(directory, context, ["up", "--no-build", "--detach", "--wait", "--wait-timeout", "240",
                                                            *services_for(args.group)])
            elif args.reuse_images:
                # Existing private contexts cannot bypass image provenance.
                images = prepare_images(directory, context, args, sha)
            if args.group == "all":
                certify(directory, context, args)
            elif args.group == "corrections-functional":
                certify_functional(directory, context, images, evidence)
            else:
                certify_group(directory, context, args, images, evidence)
    except BaseException as error:
        if evidence:
            evidence.failed = True
            evidence.write_group("FAIL", error={"code": "XLSX_GROUP_DEADLINE" if isinstance(error, TimeoutError)
                else "XLSX_GROUP_FAILED", "type": type(error).__name__}, runtime_images=images)
        raise
    finally:
        try:
            if started:
                with deadline(180):
                    certification.compose(directory, context, ["down", "--volumes", "--remove-orphans"])
        finally:
            certification.assert_main_unchanged(context)


if __name__ == "__main__":
    main()
