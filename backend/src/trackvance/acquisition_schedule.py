"""Acquisition cadence on the existing scheduler and AcquisitionRun/Job worker lane."""
from datetime import UTC, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlalchemy import select

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
from .models import Dataset, DatasetSourceBinding, DatasetVersion, User
from .operations_common import OperationError
from .permissions import effective_permissions
from .services import audit


def configuration(db, dataset_id, *, lock=False):
    query = select(AcquisitionSourceConfiguration).where(AcquisitionSourceConfiguration.dataset_id == dataset_id)
    return db.scalar(query.with_for_update() if lock else query)


def revision(db, config):
    return db.scalar(select(AcquisitionSourceRevision).where(AcquisitionSourceRevision.configuration_id == config.id,
        AcquisitionSourceRevision.version == config.version))


def source_settings_dto(db, dataset):
    config = configuration(db, dataset.id)
    binding = db.scalar(select(DatasetSourceBinding).where(DatasetSourceBinding.dataset_id == dataset.id))
    attempts = db.scalar(select(AcquisitionRun).where(AcquisitionRun.dataset_id == dataset.id).order_by(AcquisitionRun.created_at.desc()))
    succeeded = db.scalar(select(AcquisitionRun).where(AcquisitionRun.dataset_id == dataset.id, AcquisitionRun.status == "SUCCESS").order_by(AcquisitionRun.finished_at.desc()))
    published = db.scalar(select(DatasetVersion).where(DatasetVersion.dataset_id == dataset.id).order_by(DatasetVersion.version.desc()))
    return {"dataset_id": dataset.id, "version": config.version if config else None, "enabled": config.enabled if config else False,
        "settings": revision(db, config).settings if config else None,
        "binding": {"connection_id": binding.connection_id, "schema_name": binding.schema_name, "object_name": binding.object_name,
                    "column_overrides": binding.column_overrides} if binding else None,
        "next_run_at": iso(config.next_run_at) if config else None,
        "last_attempt_at": iso(attempts.started_at or attempts.created_at) if attempts else None,
        "last_query_success_at": iso(succeeded.finished_at) if succeeded else None,
        "last_publication_at": iso(published.created_at) if published else None,
        "snapshot_policy": "FULL_SNAPSHOT", "misfire_policy": "COALESCE_LATEST", "overlap_policy": "SKIP_WHILE_ACTIVE"}


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
    if expected_version != (config.version if config else None):
        raise AcquisitionOperationError(409, "SOURCE_SETTINGS_CONFLICT", "Origen y actualización cambiaron; vuelve a cargar.")
    try:
        ZoneInfo(settings["timezone"])
    except (ZoneInfoNotFoundError, ValueError):
        raise AcquisitionOperationError(422, "INVALID_TIMEZONE", "Selecciona una zona horaria IANA válida.") from None
    responsible, authorize = _responsible(db, settings["responsible_user_id"], user.organization_id)
    authorize(db, responsible, dataset.id, "CONTENT")
    connection = owned_connection(db, settings["connection_id"], user)
    source, _ = saved_source(db, connection, schema_name=settings["schema_name"], object_name=settings["object_name"])
    selected = next((item for item in source.objects(settings["schema_name"]) if item["name"] == settings["object_name"]), None)
    if selected is None:
        raise AcquisitionOperationError(422, "SOURCE_OBJECT_UNAVAILABLE", "La tabla o colección no está disponible.")
    binding = db.scalar(select(DatasetSourceBinding).where(DatasetSourceBinding.dataset_id == dataset.id))
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
    config.next_run_at = utcnow() + timedelta(seconds=settings["interval_seconds"]) if config.enabled else None
    db.add(AcquisitionSourceRevision(organization_id=user.organization_id, configuration_id=config.id,
        version=config.version, actor_id=user.id, settings=dict(settings)))
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
    connection = owned_connection(db, binding.connection_id, user, lock=True)
    run = register_source(db, user, connection, binding, dataset, idempotency_key=idempotency_key,
        request_identity=request_identity, update_policy={"publication": settings["publication_policy"], "empty": settings["empty_policy"],
        "schema": "BLOCK_INCOMPATIBLE"} if settings else None)
    if config:
        run.source_snapshot = {**run.source_snapshot, "source_configuration_id": config.id,
            "source_configuration_version": config.version, "source_revision_id": revision(db, config).id}
    return run


def dispatch_due(db, now=None, *, limit=100):
    instant, count = now or utcnow(), 0
    due = db.scalars(select(AcquisitionSourceConfiguration).where(AcquisitionSourceConfiguration.enabled.is_(True),
        AcquisitionSourceConfiguration.next_run_at <= instant).order_by(AcquisitionSourceConfiguration.next_run_at).limit(limit)
        .with_for_update(skip_locked=True)).all()
    for config in due:
        frozen = revision(db, config)
        settings = frozen.settings
        planned = config.next_run_at.replace(tzinfo=UTC)
        missed = max(0, int((instant - planned).total_seconds())) // settings["interval_seconds"]
        planned += timedelta(seconds=missed * settings["interval_seconds"])
        config.next_run_at = planned + timedelta(seconds=settings["interval_seconds"])
        if db.scalar(select(AcquisitionOccurrence.id).where(AcquisitionOccurrence.configuration_id == config.id, AcquisitionOccurrence.planned_at == planned)):
            continue
        occurrence = AcquisitionOccurrence(organization_id=config.organization_id, configuration_id=config.id, revision_id=frozen.id,
            planned_at=planned, status="ENQUEUED")
        db.add(occurrence)
        busy = db.scalar(select(AcquisitionRun.id).where(AcquisitionRun.dataset_id == config.dataset_id,
            AcquisitionRun.status.in_(["QUEUED", "RUNNING"])))
        try:
            if busy:
                raise AcquisitionOperationError(409, "ACQUISITION_DATASET_BUSY", "El dataset tiene una adquisición activa.")
            responsible, authorize = _responsible(db, settings["responsible_user_id"], config.organization_id)
            dataset = db.get(Dataset, config.dataset_id)
            authorize(db, responsible, dataset.id, "CONTENT")
            with db.begin_nested():
                run = enqueue_update(db, dataset, responsible, config=config,
                    idempotency_key=f"schedule:{config.id}:{iso(planned)}", request_identity={"source_revision_id": frozen.id, "planned_at": iso(planned)})
                run.source_snapshot = {**run.source_snapshot, "occurrence_id": occurrence.id, "planned_at": iso(planned), "timezone": settings["timezone"]}
                occurrence.acquisition_id = run.id
        except (ConnectionOperationError, SourceError, OperationError) as error:
            occurrence.status, occurrence.reason_code = "SKIPPED" if busy else "BLOCKED", error.code
        count += 1
        db.flush()
    return count
