"""Real C05 PostgreSQL scheduling while a Delivery worker handles 1M rows.

Run inside the UUID certification API container. The caller pauses only that
project's scheduler and restarts it in finally. This helper never invokes Docker.
Five complete API preflights precede the measured interval. Four schedules alternate
two published populations from distinct datasets and use distinct physical targets;
the fifth uses the first population to keep the worker busy.
Only this helper's pre-STARTED queued deliveries are cancelled, through the API.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import time
from collections import Counter
from contextlib import ExitStack, contextmanager
from copy import deepcopy
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.parse import urlsplit
from uuid import UUID, uuid4

from automation_cycle import Client, require_isolated_environment


def validate_arguments(args):
    project, database = require_isolated_environment()
    if not args.scheduler_paused:
        raise ValueError("El caller debe pausar exclusivamente scheduler del UUID aislado y reiniciarlo en finally.")
    url = urlsplit(args.api_url)
    if (url.scheme != "http" or url.username or url.password or url.query or url.fragment
            or not ((url.hostname == "api" and url.port == 8000)
                    or (url.hostname in {"127.0.0.1", "localhost"} and 32000 <= (url.port or 0) <= 32999))):
        raise ValueError("La API debe pertenecer al contexto privado de certificación.")
    if not 2 <= args.count <= 8 or args.expected_rows < 1000000:
        raise ValueError("El ensayo requiere 2–8 programaciones y una población de al menos 1M.")
    if not 30 <= args.timeout <= 7200:
        raise ValueError("El timeout privado debe estar entre 30 y 7200 segundos.")
    for identifier in [args.source_version, args.additional_source_version, args.configuration,
                       *([args.busy_run] if args.busy_run else [])]:
        UUID(identifier)
    if UUID(args.source_version) == UUID(args.additional_source_version):
        raise ValueError("El ensayo requiere dos DatasetVersions de datasets distintos.")
    if not args.evidence.is_absolute() or args.evidence.is_symlink():
        raise ValueError("La evidencia exige un directorio privado absoluto sin enlace.")
    return project, database


def wait_value(callback, predicate, timeout):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = callback()
        if predicate(value):
            return value
        time.sleep(0.1)
    raise TimeoutError("La condición de certificación no se observó dentro del límite.")


@contextmanager
def forbid_population_io():
    """Instrument the real product call; neither SQL nor dispatch is mocked."""
    from unittest.mock import patch

    import polars as pl

    from trackvance import artifactstore, dataset_scans, delivery_service, delivery_streams

    calls = Counter()

    def forbidden(name):
        def reject(*_args, **_kwargs):
            calls[name] += 1
            raise AssertionError("C05 dispatch intentó I/O de población: " + name)
        return reject

    targets = [(artifactstore.storage_provider, name, "storage." + name) for name in (
        "dataset_paths", "dataset_descriptor", "open_read", "materialize", "materialize_reference")]
    targets += [(artifactstore, "file_hash", "artifact.hash"),
        (delivery_streams, "file_hash", "preparation.hash"),
        (dataset_scans, "iter_version_batches", "dataset.scan"),
        (delivery_streams.DatasetRecords, "__init__", "dataset.footer"),
        (delivery_streams.DatasetRecords, "__iter__", "dataset.rows"),
        (delivery_streams.PreparedRows, "write", "preparation.spool"),
        (delivery_service.sink_registry, "create", "sink.open")]
    targets += [(pl, name, "polars." + name) for name in ("scan_parquet", "read_parquet", "read_parquet_schema")]
    with ExitStack() as stack:
        for target, attribute, name in targets:
            calls[name] = 0
            stack.enter_context(patch.object(target, attribute, forbidden(name)))
        yield calls


def busy_snapshot(run_id, source_id, organization_id, expected_rows):
    from sqlalchemy import select

    from trackvance.db import SessionLocal, iso, utcnow
    from trackvance.models import DatasetVersion, DeliveryAttempt, Job, Run

    with SessionLocal() as db:
        run = db.get(Run, run_id)
        job = db.scalar(select(Job).where(Job.run_id == run_id))
        source = db.get(DatasetVersion, source_id)
        if (run is None or job is None or source is None or run.organization_id != organization_id
                or source.organization_id != organization_id or run.module != "DELIVERY"
                or run.dataset_version_id != source_id or source.row_count != expected_rows):
            raise ValueError("El busy Run no pertenece a la población y organización declaradas.")
        attempt = db.scalar(select(DeliveryAttempt).where(DeliveryAttempt.run_id == run.id))
        lease_until = job.lease_until
        if lease_until is not None:
            lease_until = lease_until.replace(tzinfo=UTC) if lease_until.tzinfo is None else lease_until.astimezone(UTC)
        live = (job.status == "RUNNING" and run.status in {"QUEUED", "RUNNING"}
                and job.lease_owner is not None and job.lease_until is not None
                and lease_until > utcnow())
        return {"run_id": run.id, "job_id": job.id, "run_status": run.status,
                "job_status": job.status, "live_worker_lease": live,
                "source_version_id": source.id, "rows": source.row_count,
                "progress_stage": run.progress_stage, "started_at": iso(run.started_at),
                "attempt_status": attempt.status if attempt else None, "observed_at": iso(utcnow())}


def load_template(args, organization_id, database, *, source_version_id=None):
    from trackvance.db import SessionLocal
    from trackvance.delivery_schemas import DeliveryDraft
    from trackvance.models import (
        Artifact,
        Configuration,
        DatasetVersion,
        DeliveryDestination,
        DeliveryDestinationVersion,
    )

    with SessionLocal() as db:
        config = db.get(Configuration, args.configuration)
        source = db.get(DatasetVersion, source_version_id or args.source_version)
        if (config is None or source is None or config.organization_id != organization_id
                or source.organization_id != organization_id or config.module != "DELIVERY"
                or config.status != "PUBLISHED" or source.profile_status != "READY"
                or source.row_count != args.expected_rows):
            raise ValueError("Se requiere Configuration real PUBLISHED y DatasetVersion READY de 1M en la organización.")
        draft = DeliveryDraft.model_validate(config.config).snapshot()
        original = db.get(DatasetVersion, draft["dataset_version_id"])
        canonical = db.get(Artifact, source.canonical_artifact_id)
        destination = db.get(DeliveryDestination, draft["destination_id"])
        revision = db.get(DeliveryDestinationVersion, draft["destination_version_id"])
        if (original is None or original.organization_id != organization_id
                or original.schema_hash != source.schema_hash or canonical is None
                or canonical.organization_id != organization_id or destination is None or revision is None
                or destination.organization_id != organization_id or revision.organization_id != organization_id
                or revision.destination_id != destination.id or destination.sink_type != "POSTGRESQL"
                or revision.config["database"] != "tv_v070_test"
                or revision.config["host"].casefold() != (database.host or "").casefold()
                or int(revision.config["port"]) != (database.port or 5432)):
            raise ValueError("El template y su destino PostgreSQL deben pertenecer íntegramente al contexto aislado.")
        draft["dataset_version_id"] = source.id
        identity = {"dataset_version_id": source.id, "dataset_id": source.dataset_id, "row_count": source.row_count,
                    "column_count": source.column_count, "schema_hash": source.schema_hash,
                    "canonical_artifact_id": canonical.id, "canonical_sha256": canonical.sha256,
                    "source_sha256": source.sha256, "canonical_size_bytes": canonical.size_bytes,
                    "canonical_media_type": canonical.media_type, "source_run_id": source.source_run_id}
        return draft, identity


def publish_targets(client, templates, nonce, timeout, owned_validations, report):
    from trackvance.manifests import configuration_hash

    configs, setup = [], []
    identities = {item["dataset_version_id"]: item for item in report["sources"]}
    report["configuration_inputs"] = {}
    for number, template in enumerate(templates):
        draft = deepcopy(template)
        draft["target"] = {"mode": "CREATE_TABLE", "schema_name": template["target"]["schema_name"],
            "table_name": f"corrections_dispatch_{nonce}_{number}", "create_schema": False}
        draft["write_strategy"], draft["upsert_keys"] = "CREATE_AND_LOAD", []
        started = time.perf_counter()
        validation = client.call("/delivery/validations", draft, expected=202)
        owned_validations.append(validation["id"])
        result = wait_value(lambda identifier=validation["id"]: client.call("/delivery/validations/" + identifier),
                            lambda item: item["status"] in {"SUCCESS", "FAILED", "FAILED_PRECONDITION", "CANCELLED"}, timeout)
        if result["status"] != "SUCCESS" or result.get("result", {}).get("status") != "PASS":
            raise AssertionError("Un preflight completo real no aprobó el target sintético.")
        published = client.call("/delivery/configurations?validation_run_id=" + validation["id"],
            {"name": f"C05 full preflight {nonce} {number}", **draft}, expected=201)
        configs.append(published["id"])
        expected_source = identities[draft["dataset_version_id"]]
        report["configuration_inputs"][published["id"]] = {"source": expected_source,
            "config_hash": configuration_hash(draft), "destination_version_id": draft["destination_version_id"]}
        setup.append({"configuration_id": published["id"], "validation_run_id": validation["id"],
            "status": result["status"], "population_rows": expected_source["row_count"],
            "dataset_version_id": expected_source["dataset_version_id"], "dataset_id": expected_source["dataset_id"],
            "seconds": round(time.perf_counter() - started, 6)})
    report["setup"] = {"complete_api_preflights": setup, "measured_in_dispatch": False,
                       "synthetic_targets": len(templates), "distinct_datasets": len(identities),
                       "version_reuse_declared": True}
    return configs


def pause_owned(client, automations, report):
    failures = []
    for identifier in automations:
        try:
            current = client.call("/delivery/automations/" + identifier)
            if not current["enabled"]:
                continue
            settings = {**current["settings"], "starts_at": (datetime.now(UTC) + timedelta(hours=1)).isoformat()}
            client.call("/delivery/automations/" + identifier + "/versions", {
                "name": current["name"], "configuration_id": current["configuration_id"],
                "settings": settings, "enabled": False, "expected_version": current["version"]}, expected=201)
        except Exception:  # noqa: BLE001 -- retain every scoped cleanup failure without credential-bearing output.
            failures.append(identifier)
    report.setdefault("cleanup", {})["paused_automations"] = len(automations) - len(failures)
    report["cleanup"]["pause_failures"] = failures


def cancel_validations(client, identifiers, report, timeout):
    failures, terminal = [], []
    for identifier in identifiers:
        try:
            current = client.call("/delivery/validations/" + identifier)
            if current["status"] in {"QUEUED", "RUNNING"}:
                client.call("/delivery/validations/" + identifier + "/cancel", {})
                current = wait_value(lambda run_id=identifier: client.call("/delivery/validations/" + run_id),
                    lambda item: item["status"] not in {"QUEUED", "RUNNING"}, min(30, timeout))
            terminal.append({"run_id": identifier, "status": current["status"]})
        except Exception:  # noqa: BLE001 -- report only scoped identifiers and never HTTP or database bodies.
            failures.append(identifier)
    report.setdefault("cleanup", {}).update(validation_terminal=terminal, validation_cancel_failures=failures)


def discover_owned_runs(automations, organization_id):
    """Recover committed run IDs even if an assertion failed after dispatch."""
    from sqlalchemy import select

    from trackvance.automation_models import DeliveryOccurrence
    from trackvance.db import SessionLocal

    with SessionLocal() as db:
        return list(db.scalars(select(DeliveryOccurrence.run_id).where(
            DeliveryOccurrence.organization_id == organization_id,
            DeliveryOccurrence.automation_id.in_(automations), DeliveryOccurrence.run_id.is_not(None))).all())


def cancel_owned(client, identifiers, organization_id, report):
    from sqlalchemy import select

    from trackvance.db import SessionLocal
    from trackvance.models import DeliveryAttempt, Run

    cancelled, failures = [], []
    for identifier in dict.fromkeys(identifiers):
        try:
            with SessionLocal() as db:
                run = db.get(Run, identifier)
                attempt = db.scalar(select(DeliveryAttempt).where(DeliveryAttempt.run_id == identifier))
                if run is None or run.organization_id != organization_id or run.started_at or attempt:
                    raise ValueError("Una entrega propia ya no está antes de STARTED.")
                status = run.status
            if status == "QUEUED":
                client.call("/runs/" + identifier + "/cancel", {})
            elif status != "CANCELLED":
                raise ValueError("Una entrega propia dejó de estar en cola.")
            with SessionLocal() as db:
                run = db.get(Run, identifier)
                attempt = db.scalar(select(DeliveryAttempt).where(DeliveryAttempt.run_id == identifier))
                if run.status != "CANCELLED" or run.started_at or attempt:
                    raise ValueError("La cancelación normal no quedó antes de STARTED.")
            cancelled.append(identifier)
        except Exception:  # noqa: BLE001 -- scoped failures are recorded; no fabricated completion or forced DB status.
            failures.append(identifier)
    report.setdefault("cleanup", {}).update(cancelled_run_ids=cancelled, cancel_failures=failures,
        cancelled_via_normal_api=True, own_remote_attempts=0 if not failures else None)
    return not failures


def verify_occurrence_contract(db, occurrence, report, organization_id):
    """Resolve the published parent through the immutable automation revision.

    Product creates a distinct AUTOMATION_EFFECTIVE Configuration for every
    occurrence; its previous_version_id is not a link to the published template.
    This check uses real persisted rows and neither requires nor invents that FK.
    """
    from sqlalchemy import select

    from trackvance.automation import AUTOMATION_ACTOR
    from trackvance.automation_models import DeliveryAutomation, DeliveryAutomationVersion
    from trackvance.delivery_schemas import DeliveryDraft
    from trackvance.manifests import configuration_hash
    from trackvance.models import Configuration, DeliveryDestinationVersion, Job, Run

    published_id = report["automation_configurations"].get(occurrence.automation_id)
    expected = report["configuration_inputs"].get(published_id)
    if expected is None:
        raise AssertionError("Una ocurrencia utilizó una automatización ajena al ensayo.")
    automation = db.get(DeliveryAutomation, occurrence.automation_id)
    revision = db.get(DeliveryAutomationVersion, occurrence.automation_version_id)
    published = db.get(Configuration, published_id)
    run = db.get(Run, occurrence.run_id)
    job = db.scalar(select(Job).where(Job.run_id == occurrence.run_id))
    effective = db.get(Configuration, run.config_id) if run else None
    if (occurrence.status != "ENQUEUED" or any(item is None for item in (
            automation, revision, published, run, job, effective))
            or any(item.organization_id != organization_id for item in (
                occurrence, automation, revision, published, run, job, effective))):
        raise AssertionError("Una programación elegible no conservó sus entidades y organización.")
    source = expected["source"]
    frozen = {key: value for key, value in source.items() if key != "dataset_id"}
    draft = DeliveryDraft.model_validate({**published.config, "dataset_version_id": source["dataset_version_id"]})
    destination = db.get(DeliveryDestinationVersion, draft.destination_version_id)
    if (revision.automation_id != automation.id or revision.configuration_id != published_id
            or revision.settings.get("template") != published.config or not revision.enabled
            or published.status != "PUBLISHED" or published.module != "DELIVERY"
            or effective.id == published_id or effective.status != "AUTOMATION_EFFECTIVE"
            or effective.module != "DELIVERY" or effective.version != revision.version
            or effective.dataset_id != source["dataset_id"] or effective.config != draft.snapshot()
            or effective.owner != published.owner or effective.name != automation.name
            or configuration_hash(effective.config) != expected["config_hash"]
            or run.module != "DELIVERY" or job.lane != "DELIVERY"
            or run.dataset_version_id != source["dataset_version_id"]
            or occurrence.dataset_version_id != source["dataset_version_id"]
            or run.execution_plan.get("source_identity") != frozen
            or run.execution_plan.get("config_hash") != expected["config_hash"]
            or run.execution_plan.get("destination_id") != draft.destination_id
            or run.execution_plan.get("destination_version_id") != expected["destination_version_id"]
            or destination is None or destination.organization_id != organization_id
            or destination.destination_id != draft.destination_id
            or run.execution_plan.get("destination_config_hash") != destination.config_hash
            or run.initiated_by_type != "SYSTEM" or run.initiated_by_id != AUTOMATION_ACTOR.id
            or run.execution_plan.get("automation", {}).get("automation_id") != automation.id
            or run.execution_plan["automation"].get("automation_version_id") != revision.id
            or run.execution_plan["automation"].get("responsible_user_id") != revision.responsible_user_id
            or run.execution_plan["automation"].get("occurrence_id") != occurrence.id):
        raise AssertionError("La configuración efectiva no conserva el parent y las identidades congeladas.")
    return run, job, source, published_id


def measured_dispatch(args, client, automations, organization_id, report, owned_runs):
    from sqlalchemy import select

    from trackvance.automation import dispatch_due
    from trackvance.automation_models import DeliveryAutomation, DeliveryOccurrence
    from trackvance.db import SessionLocal, iso, utcnow

    starts = datetime.now(UTC) + timedelta(seconds=2)
    for identifier in automations:
        current = client.call("/delivery/automations/" + identifier)
        client.call("/delivery/automations/" + identifier + "/versions", {
            "name": current["name"], "configuration_id": current["configuration_id"],
            "settings": {**current["settings"], "starts_at": starts.isoformat()},
            "enabled": True, "expected_version": current["version"]}, expected=201)
    while datetime.now(UTC) < starts:
        time.sleep(0.05)
    before = busy_snapshot(report["busy"]["run_id"], args.source_version, organization_id, args.expected_rows)
    if not before["live_worker_lease"]:
        raise AssertionError("El worker dejó de estar ocupado antes del intervalo medido.")
    with SessionLocal() as db:
        due = set(db.scalars(select(DeliveryAutomation.id).where(
            DeliveryAutomation.enabled.is_(True), DeliveryAutomation.next_run_at <= utcnow())).all())
        if due != set(automations):
            raise ValueError("El tick debe contener exclusivamente las programaciones de este ensayo.")
        db.rollback()
        started = time.perf_counter()
        with forbid_population_io() as calls:
            count = dispatch_due(db, utcnow(), limit=len(automations))
            db.commit()
        seconds = time.perf_counter() - started
        occurrences = db.scalars(select(DeliveryOccurrence).where(
            DeliveryOccurrence.automation_id.in_(automations))).all()
        report["dispatch"] = {"seconds": round(seconds, 6), "count": count, "metadata_io_calls": dict(calls),
                              "includes_commit": True, "includes_setup": False, "includes_queue_wait": False}
        for occurrence in occurrences:
            if occurrence.run_id:
                owned_runs.append(occurrence.run_id)
        if count != len(automations) or len(occurrences) != len(automations) or any(calls.values()):
            raise AssertionError("El tick no registró exactamente todas las ocurrencias sin I/O.")
        rows = []
        target_fingerprints = set()
        source_ids, dataset_ids = set(), set()
        for occurrence in occurrences:
            run, job, expected_source, published_id = verify_occurrence_contract(db, occurrence, report, organization_id)
            frozen = run.execution_plan["source_identity"]
            source_ids.add(expected_source["dataset_version_id"])
            dataset_ids.add(expected_source["dataset_id"])
            target_fingerprints.add(run.execution_plan["automation"]["target_fingerprint"])
            rows.append({"occurrence_id": occurrence.id, "automation_id": occurrence.automation_id,
                "run_id": run.id, "job_id": job.id, "status": occurrence.status,
                "dataset_version_id": run.dataset_version_id, "dataset_id": expected_source["dataset_id"],
                "row_count": frozen["row_count"],
                "effective_configuration_id": run.config_id, "published_configuration_id": published_id,
                "automation_version_id": occurrence.automation_version_id,
                "destination_version_id": run.execution_plan["destination_version_id"],
                "config_hash": run.execution_plan["config_hash"], "canonical_sha256": frozen["canonical_sha256"],
                "planned_at": iso(occurrence.planned_at), "dispatched_at": iso(occurrence.dispatched_at),
                "queued_at": iso(job.created_at), "started_at": iso(run.started_at),
                "observed_queue_age_seconds": max(0.0, (utcnow() - job.created_at.replace(tzinfo=UTC)).total_seconds()),
                "wait_censored": run.started_at is None,
                "queue_to_started_seconds": None if run.started_at is None else
                    max(0.0, (run.started_at - job.created_at).total_seconds())})
        if len(target_fingerprints) != len(automations):
            raise AssertionError("Las programaciones deben usar targets físicos distintos.")
        if len(source_ids) != 2 or len(dataset_ids) != 2:
            raise AssertionError("El tick debe despachar dos poblaciones grandes de datasets distintos.")
        report["occurrences"] = rows
        report["dispatch"].update(distinct_source_versions=len(source_ids), distinct_datasets=len(dataset_ids))
    after = busy_snapshot(report["busy"]["run_id"], args.source_version, organization_id, args.expected_rows)
    report["busy"].update(before_dispatch=before, after_dispatch=after)
    if not after["live_worker_lease"]:
        raise AssertionError("No hubo ocupación real del worker durante todo el tick medido.")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--api-url", default="http://api:8000")
    parser.add_argument("--source-version", required=True)
    parser.add_argument("--additional-source-version", required=True)
    parser.add_argument("--configuration", required=True)
    parser.add_argument("--busy-run")
    parser.add_argument("--evidence", type=Path, required=True)
    parser.add_argument("--scheduler-paused", action="store_true")
    parser.add_argument("--count", type=int, default=4)
    parser.add_argument("--expected-rows", type=int, default=1000000)
    parser.add_argument("--timeout", type=int, default=1800)
    args = parser.parse_args()
    project, database = validate_arguments(args)
    args.evidence.mkdir(parents=True, exist_ok=True, mode=0o700)
    report = {"status": "FAIL", "project": project, "schema_version": 1,
              "scheduler_scope": "caller-paused-only-this-UUID-scheduler; caller-restarts-in-finally",
              "database_backend": "POSTGRESQL", "data_values_in_evidence": False,
              "requested_schedules": args.count, "automation_configurations": {},
              "population_scope": "Schedules alternate two independently published populations from distinct datasets, each of at least 1M rows; each version is reused explicitly.",
              "script_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest()}
    client = Client(args.api_url)
    automations, runs, validations = [], [], []
    organization_id = None
    started = time.perf_counter()
    phase = "authenticate_private_api"
    try:
        session = client.call("/auth/demo", {})
        organization_id = session["organization"]["id"]
        phase = "validate_published_template"
        template, report["source"] = load_template(args, organization_id, database)
        additional_template, additional_source = load_template(args, organization_id, database,
                                                               source_version_id=args.additional_source_version)
        if additional_source["dataset_id"] == report["source"]["dataset_id"]:
            raise ValueError("Las dos poblaciones deben pertenecer a datasets distintos.")
        report["sources"] = [report["source"], additional_source]
        nonce = uuid4().hex[:12]
        phase = "complete_real_preflights_and_publish"
        templates = [template if number % 2 == 0 else additional_template for number in range(args.count)]
        if args.busy_run is None:
            templates.append(template)
        configurations = publish_targets(client, templates, nonce, args.timeout, validations, report)
        phase = "register_paused_schedules"
        for identifier in configurations[:args.count]:
            created = client.call("/delivery/automations", {
                "configuration_id": identifier, "name": "C05 dispatcher " + nonce,
                "enabled": False, "settings": {"mode": "INTERVAL", "source_policy": "FIXED_VERSION",
                    "starts_at": (datetime.now(UTC) + timedelta(hours=1)).isoformat(),
                    "interval_seconds": 3600, "timezone": "America/Bogota"}}, expected=201)
            automations.append(created["id"])
            report["automation_configurations"][created["id"]] = identifier
        busy_id = args.busy_run
        phase = "observe_real_busy_worker"
        if busy_id is None:
            busy_id = client.call("/delivery/runs", {"configuration_id": configurations[-1],
                "dataset_version_id": args.source_version}, expected=202)["id"]
        report["busy"] = {"run_id": busy_id, "owned": args.busy_run is None}
        wait_value(lambda: busy_snapshot(busy_id, args.source_version, organization_id, args.expected_rows),
                   lambda item: item["live_worker_lease"], min(60, args.timeout))
        phase = "metadata_only_dispatch_under_real_worker_load"
        measured_dispatch(args, client, automations, organization_id, report, runs)
        phase = "cancel_owned_queued_runs"
        if not cancel_owned(client, runs, organization_id, report):
            raise AssertionError("No todas las entregas medidas se cancelaron normalmente antes de STARTED.")
        pause_owned(client, automations, report)
        if report["cleanup"]["pause_failures"]:
            raise AssertionError("No se pausaron todas las automatizaciones propias.")
        if args.busy_run is None:
            phase = "observe_busy_remote_commit"
            complete = wait_value(lambda: client.call("/runs/" + busy_id),
                lambda item: item["status"] in {"SUCCESS", "FAILED", "FAILED_PRECONDITION", "UNKNOWN", "CANCELLED"}, args.timeout)
            report["busy"]["terminal"] = {"status": complete["status"], "decision": complete["decision"],
                "rows_written": complete.get("metrics", {}).get("rows_written")}
            if complete["status"] != "SUCCESS" or complete["decision"] != "COMMITTED" or complete["metrics"].get("rows_written") != args.expected_rows:
                raise AssertionError("La entrega busy real no confirmó toda la población.")
        report["status"] = "PASS"
    except Exception as error:  # noqa: BLE001 -- never emit database URLs, values, raw HTTP bodies or credentials.
        report["failure"] = {"code": "C05_DISPATCH_CERTIFICATION_FAILED", "phase": phase,
                             "exception_type": type(error).__name__}
    finally:
        if organization_id:
            if automations:
                try:
                    runs.extend(discover_owned_runs(automations, organization_id))
                except Exception:  # noqa: BLE001 -- no forced cleanup or cross-scope query on an unavailable database.
                    report.setdefault("cleanup", {})["run_discovery_failed"] = True
            if runs:
                cancel_owned(client, runs, organization_id, report)
            if automations:
                pause_owned(client, automations, report)
            if validations:
                cancel_validations(client, validations, report, args.timeout)
            if (report.get("cleanup", {}).get("run_discovery_failed")
                    or any(report.get("cleanup", {}).get(key) for key in (
                        "cancel_failures", "pause_failures", "validation_cancel_failures"))):
                report["status"] = "FAIL"
                report.setdefault("failure", {"code": "C05_SCOPED_CLEANUP_FAILED", "phase": "cleanup"})
        report["total_seconds"] = round(time.perf_counter() - started, 6)
        report["owned_validation_run_ids"] = validations
        (args.evidence / "dispatch-results.json").write_text(json.dumps(report, indent=2, sort_keys=True), encoding="utf-8")
    print(json.dumps({"status": report["status"], "schedules": len(report.get("occurrences", [])),
                      "dispatch_seconds": report.get("dispatch", {}).get("seconds")}))
    return 0 if report["status"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
