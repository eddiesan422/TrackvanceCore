"""Acquisition cadence on the existing scheduler and AcquisitionRun/Job worker lane."""
import logging
from datetime import UTC, timedelta
from typing import cast
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlalchemy import select, update
from sqlalchemy.engine import CursorResult

from .acquisition import AcquisitionOperationError, register_source
from .acquisition_models import AcquisitionRun
from .acquisition_schedule_models import (
    AcquisitionOccurrence,
    AcquisitionSourceConfiguration,
    AcquisitionSourceRevision,
)
from .connections_service import ConnectionOperationError, owned_connection, saved_source
from .dataset_sources import SourceError
from .db import iso, utcnow
from .models import Dataset, DatasetSourceBinding, DatasetVersion, Job, User
from .operations_common import OperationError
from .permissions import effective_permissions
from .services import audit

logger = logging.getLogger(__name__)


def configuration(db, dataset_id, *, lock=False):
    query = select(AcquisitionSourceConfiguration).where(AcquisitionSourceConfiguration.dataset_id == dataset_id)
    return db.scalar(query.with_for_update() if lock else query)


def revision(db, config):
    return db.scalar(select(AcquisitionSourceRevision).where(AcquisitionSourceRevision.configuration_id == config.id,
        AcquisitionSourceRevision.version == config.version))


def source_settings_dto(db, dataset):
    config = configuration(db, dataset.id)
    binding = db.scalar(select(DatasetSourceBinding).where(DatasetSourceBinding.dataset_id == dataset.id))
    attempts = db.scalar(select(AcquisitionRun).where(AcquisitionRun.dataset_id == dataset.id).order_by(AcquisitionRun.created_at.desc()).limit(1))
    succeeded = db.scalar(select(AcquisitionRun).where(AcquisitionRun.dataset_id == dataset.id, AcquisitionRun.status == "SUCCESS").order_by(AcquisitionRun.finished_at.desc()).limit(1))
    published = db.scalar(select(DatasetVersion).where(DatasetVersion.dataset_id == dataset.id).order_by(DatasetVersion.version.desc()).limit(1))
    latest = db.scalar(select(AcquisitionOccurrence).where(AcquisitionOccurrence.configuration_id == config.id)
        .order_by(AcquisitionOccurrence.planned_at.desc()).limit(1)) if config else None
    frozen = revision(db, config) if config else None
    settings = frozen.settings if frozen and isinstance(frozen.settings, dict) else None
    from .component_health import component_details, worker_status
    scheduler, worker = component_details("scheduler"), worker_status("ACQUISITION")
    overdue = max(0, (utcnow() - _aware(config.next_run_at)).total_seconds()) if config and config.enabled and config.next_run_at else 0
    health = _health(config, latest, attempts, scheduler, worker, overdue)
    return {"dataset_id": dataset.id, "version": config.version if config else None, "enabled": config.enabled if config else False,
        "settings": {key: value for key, value in settings.items() if not key.startswith("_")} if settings else None,
        "binding": {"connection_id": binding.connection_id, "schema_name": binding.schema_name, "object_name": binding.object_name,
                    "column_overrides": binding.column_overrides} if binding else None,
        "next_run_at": iso(config.next_run_at) if config else None,
        "last_attempt_at": iso(attempts.started_at or attempts.created_at) if attempts else None,
        "last_query_success_at": iso(succeeded.finished_at) if succeeded else None,
        "last_publication_at": iso(published.created_at) if published else None,
        "scheduler": scheduler, "worker": worker, "health": health,
        "dispatch_diagnostic": config.dispatch_diagnostic if config else None,
        "last_occurrence": occurrence_dto(db, latest) if latest else None,
        "interval_seconds": settings.get("interval_seconds") if settings else None,
        "timezone": settings.get("timezone") if settings else None,
        "snapshot_policy": "FULL_SNAPSHOT", "misfire_policy": "COALESCE_LATEST", "overlap_policy": "SKIP_WHILE_ACTIVE"}


def _aware(value):
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def _health(config, occurrence, run, scheduler, worker, overdue):
    action: str | None
    state, message, action = "PAUSED", "Las actualizaciones programadas están en pausa.", "Habilita la programación para registrar nuevos cortes."
    if config and config.enabled:
        state, message, action = "READY", "Programación habilitada; el próximo corte todavía no vence.", None
        if config.dispatch_diagnostic:
            state, message, action = "BLOCKED", config.dispatch_diagnostic["message"], "Guarda nuevamente el origen y actualización o revisa el diagnóstico de metadata; no se consultó la fuente."
        elif scheduler["status"] == "OFFLINE":
            state, message, action = "SCHEDULER_OFFLINE", "El ejecutor de calendarios no está disponible.", "Inicia o revisa el servicio scheduler; la fecha guardada no acredita una consulta."
        elif scheduler["execution_status"] in {"DEGRADED", "UNKNOWN", "STALE"}:
            state, message, action = "SCHEDULER_DEGRADED", "El proceso está activo sin un ciclo de despacho sano comprobado.", "Revisa el diagnóstico del scheduler y su último ciclo."
        elif occurrence and occurrence.status == "BLOCKED":
            state, message, action = "BLOCKED", occurrence.reason_message or "El corte no pudo registrar una adquisición.", "Corrige la causa y espera el próximo corte; la versión anterior permanece disponible."
        elif run and run.status == "QUEUED":
            state, message = "WAITING_WORKER", "La adquisición está en cola, esperando al worker."
            action = "Inicia o revisa el worker de adquisición." if worker["status"] == "OFFLINE" else "El worker tomará el trabajo según su capacidad."
        elif run and run.status == "RUNNING":
            state, message, action = "RUNNING", "El worker está consultando una fotografía completa.", "Los cortes que coincidan con esta consulta se omiten para evitar una cola de fotografías vencidas."
        elif run and run.status == "FAILED":
            state, message, action = "QUERY_FAILED", run.error_message or "La última adquisición falló y no publicó una versión nueva.", "Revisa el diagnóstico de la adquisición; la versión anterior permanece disponible."
        elif overdue:
            state, message, action = "DUE", "El próximo corte está vencido y todavía no tiene un despacho registrado.", "Revisa el scheduler y el retraso; no se afirma que esta consulta haya ocurrido."
    return {"state": state, "message": message, "action": action, "delay_seconds": round(overdue, 3),
            "last_activity_at": iso(run.finished_at or run.started_at or run.created_at) if run else iso(occurrence.dispatched_at) if occurrence else None}


def occurrence_dto(db, occurrence):
    run = db.get(AcquisitionRun, occurrence.acquisition_id) if occurrence.acquisition_id else None
    job = db.scalar(select(Job).where(Job.acquisition_id == run.id)) if run else None
    config = db.get(AcquisitionSourceConfiguration, occurrence.configuration_id)
    return {"id": occurrence.id, "configuration_id": occurrence.configuration_id, "revision_id": occurrence.revision_id,
        "dataset_id": config.dataset_id, "planned_at": iso(occurrence.planned_at), "dispatched_at": iso(occurrence.dispatched_at),
        "status": occurrence.status, "acquisition_id": occurrence.acquisition_id,
        "job_status": job.status if job else None, "acquisition_status": run.status if run else None, "stage": run.stage if run else None,
        "started_at": iso(run.started_at) if run else None, "finished_at": iso(run.finished_at) if run else None,
        "reason_code": occurrence.reason_code or (run.error_code if run else None),
        "reason_message": occurrence.reason_message or (run.error_message if run else None),
        "error_details": occurrence.error_details or (run.error_details if run else None),
        "coalesced_intervals": occurrence.coalesced_intervals, "output_version_id": run.output_version_id if run else None}


def _responsible(db, user_id, organization_id):
    user = db.get(User, user_id)
    if user is None or user.organization_id != organization_id or not user.active or user.deleted:
        raise AcquisitionOperationError(412, "ACQUISITION_RESPONSIBLE_UNAVAILABLE", "El responsable no está habilitado.")
    if not {"datasets:write", "connections:use"} <= set(effective_permissions(db, user)):
        raise AcquisitionOperationError(412, "ACQUISITION_PERMISSION_REVOKED", "El responsable requiere permisos para actualizar datasets y usar conexiones.")
    from .governance import authorize_dataset
    return user, authorize_dataset


def save_settings(db, dataset, user, *, settings, expected_version):
    config = configuration(db, dataset.id, lock=True)
    db.scalar(select(Dataset).where(Dataset.id == dataset.id).with_for_update())
    if config is None:
        # Concurrent first saves serialize on the dataset. Re-read after taking
        # that lock so the loser observes the committed optimistic version.
        config = configuration(db, dataset.id, lock=True)
    if expected_version != (config.version if config else None):
        raise AcquisitionOperationError(409, "SOURCE_SETTINGS_CONFLICT", "Origen y actualización cambiaron; vuelve a cargar.")
    try:
        ZoneInfo(settings["timezone"])
    except (ZoneInfoNotFoundError, ValueError):
        raise AcquisitionOperationError(422, "INVALID_TIMEZONE", "Selecciona una zona horaria IANA válida.") from None
    if not isinstance(settings["interval_seconds"], int) or isinstance(settings["interval_seconds"], bool) or not 30 <= settings["interval_seconds"] <= 2678400:
        raise AcquisitionOperationError(422, "INVALID_SCHEDULE_INTERVAL", "La frecuencia debe estar entre 30 segundos y 31 días.")
    binding = db.scalar(select(DatasetSourceBinding).where(DatasetSourceBinding.dataset_id == dataset.id))
    prior_revision = revision(db, config) if config else None
    prior = prior_revision.settings if prior_revision else None
    pause_only = bool(prior and not settings["enabled"] and
        {key: value for key, value in prior.items() if not key.startswith("_") and key != "enabled"} ==
        {key: value for key, value in settings.items() if key != "enabled"})
    selected: dict | None
    if pause_only and prior is not None:
        # Stopping future dispatches never needs a remote read or a working
        # credential, and remains possible after the responsible loses access.
        frozen_settings = {**settings, **({"_source": prior["_source"]} if "_source" in prior else {})}
        selected = {"kind": binding.object_kind if binding else "TABLE"}
    else:
        responsible, authorize = _responsible(db, settings["responsible_user_id"], user.organization_id)
        authorize(db, responsible, dataset.id, "CONTENT")
        connection = owned_connection(db, settings["connection_id"], user)
        source, source_version = saved_source(db, connection, schema_name=settings["schema_name"], object_name=settings["object_name"])
        selected = next((item for item in source.objects(settings["schema_name"]) if item["name"] == settings["object_name"]), None)
        if selected is None:
            raise AcquisitionOperationError(422, "SOURCE_OBJECT_UNAVAILABLE", "La tabla o colección no está disponible.")
        from .processing import validate_column_overrides
        columns = source.columns(settings["schema_name"], settings["object_name"])
        validate_column_overrides(settings["column_overrides"], [column["name"] for column in columns])
        frozen_settings = {**settings, "_source": {"connection_version_id": source_version.id,
            "source_native_schema": columns, "object_kind": selected["kind"]}}
    if binding is None:
        binding = DatasetSourceBinding(organization_id=user.organization_id, dataset_id=dataset.id)
        db.add(binding)
    for key in ("connection_id", "schema_name", "object_name", "column_overrides"):
        setattr(binding, key, settings[key])
    binding.object_kind = selected["kind"]
    if config is None:
        config = AcquisitionSourceConfiguration(organization_id=user.organization_id, dataset_id=dataset.id, version=1)
        db.add(config)
        db.flush()
    else:
        config.version += 1
    config.enabled, config.updated_at = settings["enabled"], utcnow()
    config.dispatch_diagnostic = None
    config.next_run_at = utcnow() + timedelta(seconds=settings["interval_seconds"]) if config.enabled else None
    db.add(AcquisitionSourceRevision(organization_id=user.organization_id, configuration_id=config.id,
        version=config.version, actor_id=user.id, settings=frozen_settings))
    audit(db, "ACQUISITION_SOURCE_UPDATED", "dataset", dataset.id, "Origen y actualización configurados", user.name,
        user.organization_id, {"configuration_id": config.id, "version": config.version, "enabled": config.enabled})
    db.flush()
    return config


def enqueue_update(db, dataset, user, *, idempotency_key=None, request_identity=None, config=None):
    config = config or configuration(db, dataset.id)
    settings = revision(db, config).settings if config else None
    binding = db.scalar(select(DatasetSourceBinding).where(DatasetSourceBinding.dataset_id == dataset.id,
        DatasetSourceBinding.organization_id == user.organization_id))
    if binding is None:
        raise AcquisitionOperationError(409, "DATASET_HAS_NO_SOURCE", "El dataset no tiene una fuente externa registrada.")
    if settings:
        # Immutable settings are authoritative even if the mutable binding or
        # current connection revision changes after this calendar was saved.
        binding = DatasetSourceBinding(organization_id=user.organization_id, dataset_id=dataset.id,
            connection_id=settings["connection_id"], schema_name=settings["schema_name"], object_name=settings["object_name"],
            column_overrides=settings["column_overrides"], object_kind=settings.get("_source", {}).get("object_kind", binding.object_kind))
    connection = owned_connection(db, binding.connection_id, user, lock=True)
    run = register_source(db, user, connection, binding, dataset, idempotency_key=idempotency_key,
        request_identity=request_identity, update_policy={"publication": settings["publication_policy"], "empty": settings["empty_policy"],
        "schema": "BLOCK_INCOMPATIBLE"} if settings else None,
        defer_source_validation=True, frozen_source=settings.get("_source") if settings else None)
    if config:
        run.source_snapshot = {**run.source_snapshot, "source_configuration_id": config.id,
            "source_configuration_version": config.version, "source_revision_id": revision(db, config).id}
    return run


class DispatchCount(int):
    """Historical count of recorded occurrences plus distinct successful jobs."""
    successful_count: int
    def __new__(cls, count, successful_count):
        value = super().__new__(cls, count)
        value.successful_count = successful_count
        return value


def dispatch_due(db, now=None, *, limit=100):
    instant, count, successful = _aware(now or utcnow()), 0, 0
    due = db.scalars(select(AcquisitionSourceConfiguration).where(AcquisitionSourceConfiguration.enabled.is_(True),
        AcquisitionSourceConfiguration.next_run_at <= instant).order_by(AcquisitionSourceConfiguration.next_run_at).limit(limit)
        .with_for_update(skip_locked=True)).all()
    for config in due:
        try:
            # This boundary includes the cursor claim, occurrence INSERT, audit,
            # event publication and every flush, not only acquisition creation.
            with db.begin_nested():
                registered, jobs = _dispatch_configuration(db, config, instant)
                config.dispatch_diagnostic = None
                db.flush()
            count += registered
            successful += jobs
        except Exception as error:  # noqa: BLE001 -- isolate the complete source transaction
            code = error.code if isinstance(error, AcquisitionOperationError) else "ACQUISITION_DISPATCH_TRANSACTION_FAILED"
            message = error.message if isinstance(error, AcquisitionOperationError) else "No se pudo registrar el corte de esta fuente; revisa su configuración y la referencia del diagnóstico."
            db.info.setdefault("scheduler_errors", []).append({"module": "acquisition", "code": code})
            logger.warning("Despacho de fuente bloqueado reference=%s code=%s exception_type=%s", config.id, code, type(error).__name__)
            # If even an occurrence cannot be persisted (for example its revision
            # is missing), record the configuration incident without inventing a
            # query or choosing a different immutable revision. Repeated cycles
            # retain one incident and its first observation instead of notices.
            try:
                with db.begin_nested():
                    changed = not config.dispatch_diagnostic or config.dispatch_diagnostic.get("code") != code
                    if changed:
                        config.dispatch_diagnostic = {"code": code, "message": message, "reference": config.id,
                            "recorded_at": iso(instant), "planned_at": iso(config.next_run_at)}
                    db.flush()
                    if changed:
                        try:
                            with db.begin_nested():
                                audit(db, "ACQUISITION_SCHEDULE_METADATA_BLOCKED", "dataset", config.dataset_id,
                                    message, "Programador de adquisición", config.organization_id,
                                    {"configuration_id": config.id, "reason_code": code, "planned_at": iso(config.next_run_at)})
                                db.flush()
                        except Exception:  # noqa: BLE001 -- retain the configuration diagnostic even if audit storage fails
                            logger.warning("La fuente conserva el diagnóstico de metadata con auditoría pendiente reference=%s.", config.id)
            except Exception:  # noqa: BLE001 -- unavailable metadata is reflected by degraded cycle health
                logger.warning("No se pudo persistir el diagnóstico de una fuente; el ciclo conserva su estado degradado.")
    return DispatchCount(count, successful)


def _dispatch_configuration(db, config, instant):
    frozen = revision(db, config)
    if frozen is None or not isinstance(frozen.settings, dict):
        raise AcquisitionOperationError(409, "SOURCE_REVISION_MISSING", "La programación no tiene una revisión válida; guarda nuevamente su origen y actualización.")
    settings = frozen.settings
    interval = settings.get("interval_seconds")
    # Historical malformed values get a bounded blocked occurrence,
    # preserving the cursor and preventing a hot loop.
    valid_interval = isinstance(interval, int) and not isinstance(interval, bool) and 30 <= interval <= 2678400
    cadence = cast(int, interval) if valid_interval else 30
    missed = max(0, int((instant - _aware(config.next_run_at)).total_seconds())) // cadence
    planned = _aware(config.next_run_at) + timedelta(seconds=missed * cadence)
    claimed = db.execute(update(AcquisitionSourceConfiguration).where(
        AcquisitionSourceConfiguration.id == config.id, AcquisitionSourceConfiguration.version == config.version,
        AcquisitionSourceConfiguration.enabled.is_(True), AcquisitionSourceConfiguration.next_run_at == config.next_run_at,
    ).values(next_run_at=planned + timedelta(seconds=cadence)).execution_options(synchronize_session=False))
    if cast(CursorResult, claimed).rowcount != 1:
        return 0, 0
    if db.scalar(select(AcquisitionOccurrence.id).where(AcquisitionOccurrence.configuration_id == config.id, AcquisitionOccurrence.planned_at == planned)):
        db.expire(config)
        return 0, 0
    occurrence = AcquisitionOccurrence(organization_id=config.organization_id, configuration_id=config.id, revision_id=frozen.id,
        planned_at=planned, dispatched_at=instant, coalesced_intervals=missed, status="ENQUEUED")
    db.add(occurrence)
    db.flush()  # Identity exists before enqueue and before a blocked event.
    busy = False
    try:
        with db.begin_nested():
            if not valid_interval:
                raise AcquisitionOperationError(422, "INVALID_SCHEDULE_INTERVAL", "La frecuencia guardada debe estar entre 30 segundos y 31 días.")
            dataset = db.scalar(select(Dataset).where(Dataset.id == config.dataset_id,
                Dataset.organization_id == config.organization_id))
            if dataset is None:
                raise AcquisitionOperationError(404, "DATASET_UNAVAILABLE", "El dataset de la programación ya no está disponible.")
            busy = bool(db.scalar(select(AcquisitionRun.id).where(AcquisitionRun.dataset_id == config.dataset_id,
                AcquisitionRun.status.in_(["QUEUED", "RUNNING"]))))
            if busy:
                raise AcquisitionOperationError(409, "ACQUISITION_DATASET_BUSY", "El corte se omitió porque el dataset tiene una adquisición activa.")
            responsible, authorize = _responsible(db, settings["responsible_user_id"], config.organization_id)
            authorize(db, responsible, dataset.id, "CONTENT")
            run = enqueue_update(db, dataset, responsible, config=config,
                idempotency_key=f"schedule:{config.id}:{iso(planned)}", request_identity={"source_revision_id": frozen.id, "planned_at": iso(planned)})
            run.source_snapshot = {**run.source_snapshot, "occurrence_id": occurrence.id, "planned_at": iso(planned),
                "timezone": settings["timezone"], "responsible_user_id": responsible.id, "origin": "SCHEDULED"}
            occurrence.acquisition_id = run.id
            db.flush()
    except (ConnectionOperationError, SourceError, OperationError) as error:
        occurrence.status, occurrence.reason_code = "SKIPPED" if busy else "BLOCKED", error.code
        # These domain messages are controlled functional text, never
        # driver exception strings, URLs, passwords or SQL fragments.
        occurrence.reason_message = error.message[:500]
        occurrence.error_details = {"cause": error.code, "reference": occurrence.id, "retry_policy": "NEXT_CUT"}
    except Exception:  # noqa: BLE001 -- isolate one source, retain a safe diagnostic
        occurrence.status, occurrence.reason_code = "BLOCKED", "ACQUISITION_DISPATCH_FAILED"
        occurrence.reason_message = "No se pudo registrar este corte; revisa la configuración y la referencia del diagnóstico."
        occurrence.error_details = {"cause": "ACQUISITION_DISPATCH_FAILED", "reference": occurrence.id, "retry_policy": "NEXT_CUT"}
    if occurrence.acquisition_id is None:
        if occurrence.status == "BLOCKED":
            db.info.setdefault("scheduler_errors", []).append({"module": "acquisition", "code": occurrence.reason_code})
        from .events import append_event
        previous = db.scalar(select(AcquisitionOccurrence).where(AcquisitionOccurrence.configuration_id == config.id,
            AcquisitionOccurrence.planned_at < planned).order_by(AcquisitionOccurrence.planned_at.desc()).limit(1))
        repeated = bool(previous and previous.status == occurrence.status and previous.reason_code == occurrence.reason_code)
        append_event(db, organization_id=config.organization_id,
            dedupe_key=f"acquisition-occurrence:{occurrence.id}", event_type="ACQUISITION_SCHEDULE_BLOCKED",
            aggregate_type="ACQUISITION_OCCURRENCE", aggregate_id=occurrence.id, module="acquisition",
            payload={"recipient_user_id": settings.get("responsible_user_id"), "origin": "SCHEDULED",
                "dataset_id": config.dataset_id, "configuration_id": config.id, "source_revision_id": frozen.id,
                "planned_at": iso(planned), "status": occurrence.status, "reason_code": occurrence.reason_code,
                "reason_message": occurrence.reason_message, "coalesced_intervals": missed,
                "suppress_notification": repeated, "related_occurrence_id": previous.id if repeated else None})
    audit(db, "ACQUISITION_SCHEDULE_DISPATCHED", "dataset", config.dataset_id,
        "Corte de adquisición registrado", "Programador de adquisición", config.organization_id,
        {"configuration_id": config.id, "occurrence_id": occurrence.id, "planned_at": iso(planned),
            "status": occurrence.status, "reason_code": occurrence.reason_code, "coalesced_intervals": missed})
    db.flush()
    db.expire(config)
    return 1, int(occurrence.acquisition_id is not None)
