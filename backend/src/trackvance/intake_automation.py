"""Intake schedules freeze inputs and queue every event behind a logical contract lock."""

import hashlib
import json
from datetime import datetime, timedelta
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import select
from sqlalchemy.orm import Session

from .audit_context import Actor
from .automation import AutomationError, AutomationSettings, elapsed_slot, next_slot
from .config_semantics import effective_config
from .db import iso, utcnow
from .intake_automation_models import (
    IntakeContractState,
    IntakeOccurrence,
    IntakeSchedule,
    IntakeScheduleVersion,
)
from .manifests import configuration_hash
from .models import Configuration, DatasetVersion, ExceptionCase, Job, Run, User, uid
from .permissions import effective_permissions
from .scheduler import aware

INTAKE_ACTOR = Actor("SYSTEM", "trackvance:intake-scheduler", "Programador Intake")
PENDING = frozenset({"QUEUED", "RUNNING"})


class IntakeScheduleSettings(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    mode: Literal["INTERVAL", "DAILY", "WEEKLY", "EVENT"] = "EVENT"
    timezone: str = Field(default="America/Bogota", max_length=80)
    starts_at: datetime
    interval_seconds: int = Field(default=3600, ge=60, le=2678400)
    local_time: str = "09:00"
    weekdays: list[int] = Field(default_factory=lambda: [0], max_length=7)
    requested_engine: Literal["AUTO", "POLARS", "PYSPARK"] = "AUTO"

    @field_validator("starts_at", mode="before")
    @classmethod
    def valid_start(cls, value):
        return AutomationSettings.aware_start(value)

    @field_validator("timezone")
    @classmethod
    def valid_zone(cls, value):
        return AutomationSettings.valid_zone(value)

    @field_validator("local_time")
    @classmethod
    def valid_time(cls, value):
        return AutomationSettings.valid_time(value)

    @field_validator("weekdays")
    @classmethod
    def valid_days(cls, value):
        return AutomationSettings.valid_days(value)


def _calendar(settings):
    return AutomationSettings.model_validate({
        **settings.model_dump(exclude={"requested_engine"}),
        "mode": "INTERVAL" if settings.mode == "EVENT" else settings.mode,
    })


def contract_root(db: Session, config: Configuration, *, lock=False):
    current, visited = config, set()
    while current.previous_version_id:
        if current.id in visited:
            raise AutomationError(409, "CONTRACT_HISTORY_INVALID", "La historia del contrato es inconsistente.")
        visited.add(current.id)
        previous = db.get(Configuration, current.previous_version_id)
        if (previous is None or previous.organization_id != config.organization_id
                or previous.module != "intake" or previous.dataset_id != config.dataset_id):
            raise AutomationError(409, "CONTRACT_HISTORY_INVALID", "La historia del contrato es inconsistente.")
        current = previous
    if lock:
        locked = db.scalar(select(Configuration).where(Configuration.id == current.id).with_for_update())
        if locked is None:
            raise AutomationError(404, "NOT_FOUND", "No se encontró el contrato Intake.")
        current = locked
    return current


def contract_revisions(db, root):
    revisions, current, visited = [], root, set()
    while current is not None:
        if current.id in visited:
            raise AutomationError(409, "CONTRACT_HISTORY_INVALID", "La historia del contrato es inconsistente.")
        visited.add(current.id)
        revisions.append(current)
        current = db.scalar(select(Configuration).where(
            Configuration.organization_id == root.organization_id,
            Configuration.module == "intake", Configuration.previous_version_id == current.id))
    return revisions


def contract_state(db, root, *, create=False):
    state = db.scalar(select(IntakeContractState).where(
        IntakeContractState.root_configuration_id == root.id,
        IntakeContractState.organization_id == root.organization_id))
    if state is None and create:
        state = IntakeContractState(id=uid(), organization_id=root.organization_id,
                                    root_configuration_id=root.id)
        db.add(state)
        db.flush()
    return state


def assert_active_contract(db, config, *, lock=False):
    if config is None or config.module != "intake":
        raise AutomationError(404, "NOT_FOUND", "No se encontró el contrato Intake.")
    root = contract_root(db, config, lock=lock)
    # Scalar read deliberately bypasses a stale ORM identity map when a
    # concurrent delete committed between event selection and dispatch.
    deleted_at = db.scalar(select(IntakeContractState.deleted_at).where(
        IntakeContractState.root_configuration_id == root.id,
        IntakeContractState.organization_id == root.organization_id))
    if config.status == "DELETED" or root.status == "DELETED" or deleted_at:
        raise AutomationError(410, "INTAKE_CONTRACT_DELETED", "Contrato eliminado: no admite nuevas operaciones.")
    return root


def authorize_responsible(db, user_id, organization_id, source=None, parameters=None):
    from .governance import authorize_dataset_content
    from .services import rule_reference_versions

    user = db.get(User, user_id, populate_existing=True)
    if user is None or user.organization_id != organization_id or not user.active or user.deleted:
        raise AutomationError(412, "INTAKE_EXECUTOR_DISABLED", "El responsable Intake no está habilitado.")
    if not {"intake:read", "intake:execute", "datasets:read"}.issubset(effective_permissions(db, user)):
        raise AutomationError(412, "INTAKE_PERMISSION_REVOKED", "El responsable no conserva los permisos necesarios.")
    if source is not None:
        authorize_dataset_content(db, user, source)
    for reference in rule_reference_versions(db, parameters or {}, organization_id):
        authorize_dataset_content(db, user, reference)
    return user


def register_run(db, config, source, run, job, *, responsible_id=None):
    """Caller locks the existing root row before planning, including manual enqueue."""
    root = assert_active_contract(db, config, lock=True)
    state = contract_state(db, root, create=True)
    parameters = effective_config("intake", config.config)
    # Jobs retain actual values, never dereference a newer schedule or revision.
    run.execution_plan = {**run.execution_plan, "intake": {
        "root_configuration_id": root.id, "configuration_id": config.id,
        "configuration_version": config.version, "dataset_version_id": source.id,
        "effective_parameters": parameters, "config_hash": configuration_hash(parameters),
        "responsible_user_id": responsible_id,
    }}
    if run.status not in PENDING:
        return
    active = db.get(Run, state.active_run_id) if state.active_run_id else None
    if active is None or active.status not in PENDING:
        # Include jobs registered before upgrading; their references remain intact.
        ids = [item.id for item in contract_revisions(db, root)]
        active = db.scalar(select(Run).where(Run.config_id.in_(ids), Run.id != run.id,
            Run.status.in_(PENDING)).order_by(Run.created_at, Run.id).limit(1))
    if active is not None:
        state.active_run_id = active.id
        job.status = "WAITING"
        run.progress_stage = "Pendiente del contrato"
    else:
        state.active_run_id = run.id


def claim_contract_run(db, run, job):
    """Validate even legacy queue rows so two workers cannot run two revisions."""
    config = db.get(Configuration, run.config_id)
    root = assert_active_contract(db, config, lock=True)
    state = contract_state(db, root, create=True)
    active = db.get(Run, state.active_run_id) if state.active_run_id else None
    if active and active.id != run.id and active.status in PENDING:
        job.status, job.lease_until = "WAITING", None
        run.progress_stage = "Pendiente del contrato"
        return False
    state.active_run_id = run.id
    return True


def settle_contract_run(db, run):
    """Release after every terminal path, including cancellation and worker failure."""
    if run.module != "intake" or run.status in PENDING:
        return
    config = db.get(Configuration, run.config_id)
    if config is None:
        return
    root = contract_root(db, config, lock=True)
    state = contract_state(db, root)
    if state is None or state.active_run_id != run.id:
        return
    state.active_run_id = None
    ids = [item.id for item in contract_revisions(db, root)]
    waiting = db.execute(select(Run, Job).join(Job, Job.run_id == Run.id).where(
        Run.config_id.in_(ids), Run.status == "QUEUED", Job.status == "WAITING"
    ).order_by(Run.created_at, Run.id).limit(1)).first()
    if waiting and not state.deleted_at:
        pending, job = waiting
        state.active_run_id, job.status = pending.id, "QUEUED"
        pending.progress_stage = "En cola"


def authorize_run(db, run):
    config = db.get(Configuration, run.config_id)
    assert_active_contract(db, config)
    metadata = (run.execution_plan or {}).get("intake") or {}
    parameters = metadata.get("effective_parameters") or effective_config("intake", config.config)
    if metadata.get("config_hash") and configuration_hash(parameters) != metadata["config_hash"]:
        raise AutomationError(409, "INTAKE_SNAPSHOT_INVALID", "Los parámetros registrados no coinciden con su hash.")
    user_id = metadata.get("responsible_user_id")
    if user_id:
        authorize_responsible(db, user_id, run.organization_id,
                              db.get(DatasetVersion, run.dataset_version_id), parameters)


def schedule_revision(db, schedule):
    revision = db.scalar(select(IntakeScheduleVersion).where(
        IntakeScheduleVersion.schedule_id == schedule.id,
        IntakeScheduleVersion.version == schedule.version))
    if revision is None:
        raise AutomationError(409, "SCHEDULE_REVISION_MISSING", "La programación no tiene revisión publicada.")
    return revision


def schedule_for(db, config):
    root = contract_root(db, config)
    return db.scalar(select(IntakeSchedule).where(IntakeSchedule.root_configuration_id == root.id,
                                                 IntakeSchedule.organization_id == config.organization_id))


def save_schedule(db, config, user, settings, *, enabled, responsible_user_id=None, expected_version=None):
    from .services import audit

    if config.organization_id != user.organization_id:
        raise AutomationError(404, "NOT_FOUND", "No se encontró el contrato Intake.")
    root = assert_active_contract(db, config, lock=True)
    if config.status != "PUBLISHED":
        raise AutomationError(409, "CONTRACT_NOT_PUBLISHED", "Selecciona una revisión publicada del contrato.")
    schedule = schedule_for(db, config)
    previous = schedule_revision(db, schedule) if schedule else None
    responsible_id = responsible_user_id or (previous.responsible_user_id if previous else user.id)
    parameters = effective_config("intake", config.config)
    if enabled:
        authorize_responsible(db, responsible_id, user.organization_id, parameters=parameters)
    elif db.get(User, responsible_id) is None or db.get(User, responsible_id).organization_id != user.organization_id:
        raise AutomationError(404, "NOT_FOUND", "No se encontró el responsable.")
    prior_settings = IntakeScheduleSettings.model_validate(previous.settings["calendar"]) if previous else None
    same_anchor = prior_settings and aware(prior_settings.starts_at) == aware(settings.starts_at)
    if aware(settings.starts_at) < utcnow() - timedelta(seconds=60) and not same_anchor:
        raise AutomationError(422, "SCHEDULE_IN_PAST", "La primera ejecución debe ser actual o futura.")
    fields = ("mode", "timezone", "starts_at", "interval_seconds", "local_time", "weekdays")
    same_calendar = prior_settings and all(getattr(prior_settings, key) == getattr(settings, key) for key in fields)
    next_at = None if settings.mode == "EVENT" else next_slot(_calendar(settings), utcnow(), inclusive=True)
    if schedule is None:
        if expected_version is not None:
            raise AutomationError(409, "SCHEDULE_CONFLICT", "La programación cambió; actualiza el formulario.")
        schedule = IntakeSchedule(id=uid(), organization_id=user.organization_id,
            root_configuration_id=root.id, version=1, enabled=enabled, next_run_at=next_at)
        db.add(schedule)
        db.flush()
    else:
        if expected_version != schedule.version:
            raise AutomationError(409, "SCHEDULE_CONFLICT", "La programación cambió; actualiza el formulario.")
        if same_calendar and enabled and schedule.enabled:
            next_at = schedule.next_run_at
        schedule.version += 1
        schedule.enabled, schedule.next_run_at, schedule.updated_at = enabled, next_at, utcnow()
    revision = IntakeScheduleVersion(id=uid(), organization_id=user.organization_id,
        schedule_id=schedule.id, version=schedule.version, configuration_id=config.id,
        responsible_user_id=responsible_id, actor_id=user.id, enabled=enabled,
        settings={"calendar": settings.model_dump(mode="json"), "activated_at": iso(utcnow()),
                  "effective_parameters": parameters, "config_hash": configuration_hash(parameters)})
    db.add(revision)
    db.flush()
    audit(db, "INTAKE_SCHEDULE_UPDATED", "configuration", config.id, "Programación Intake actualizada",
          user.name, user.organization_id, {"schedule_id": schedule.id, "schedule_version_id": revision.id,
                                          "configuration_id": config.id, "enabled": enabled})
    return schedule


def schedule_dto(db, schedule):
    revision = schedule_revision(db, schedule)
    config = db.get(Configuration, revision.configuration_id)
    latest = contract_revisions(db, contract_root(db, config))[-1]
    user = db.get(User, revision.responsible_user_id)
    return {"id": schedule.id, "root_configuration_id": schedule.root_configuration_id,
        "version": schedule.version, "schedule_version_id": revision.id,
        "configuration_id": revision.configuration_id, "configuration_version": config.version,
        "latest_configuration_id": latest.id, "update_available": latest.id != config.id,
        "enabled": schedule.enabled, "responsible_user_id": revision.responsible_user_id,
        "responsible_name": user.name if user else None, "settings": revision.settings["calendar"],
        "next_run_at": iso(schedule.next_run_at), "updated_at": iso(schedule.updated_at),
        "overlap_policy": "QUEUE_BY_LOGICAL_CONTRACT", "event_policy": "EVERY_PUBLISHED_VERSION",
        "misfire_policy": "COALESCE_LATEST_CALENDAR_SLOT", "config_hash": revision.settings["config_hash"]}


def dispatch_occurrence(db, schedule, revision, trigger_key, planned_at, *, source=None, origin="SCHEDULED", coalesced=0):
    from .events import append_event
    from .services import OperationError, enqueue

    config = db.get(Configuration, revision.configuration_id)
    assert_active_contract(db, config, lock=True)
    if db.scalar(select(IntakeOccurrence.id).where(IntakeOccurrence.schedule_id == schedule.id,
                                                  IntakeOccurrence.trigger_key == trigger_key)):
        return None
    if source is None:
        source = db.scalar(select(DatasetVersion).where(DatasetVersion.dataset_id == config.dataset_id,
            DatasetVersion.organization_id == config.organization_id,
            DatasetVersion.profile_status == "READY").order_by(DatasetVersion.version.desc()).limit(1))
    occurrence = IntakeOccurrence(id=uid(), organization_id=schedule.organization_id,
        schedule_id=schedule.id, schedule_version_id=revision.id, configuration_id=config.id,
        trigger_key=trigger_key, planned_at=planned_at, dataset_version_id=source.id if source else None,
        origin=origin, status="SKIPPED", reason_code="NO_PUBLISHED_VERSION" if source is None else None,
        coalesced_intervals=coalesced)
    db.add(occurrence)
    db.flush()
    try:
        authorize_responsible(db, revision.responsible_user_id, config.organization_id,
                              source, revision.settings["effective_parameters"])
        if source is not None:
            if origin == "SCHEDULED" and db.scalar(select(Run.id).where(
                    Run.config_id == config.id, Run.dataset_version_id == source.id,
                    Run.status == "SUCCESS", Run.decision == "REJECTED").limit(1)):
                raise AutomationError(412, "QUALITY_REJECTION_REVIEW_REQUIRED",
                    "La entrada ya fue rechazada con esta revisión; requiere revisión o una entrada/revisión nueva.")
            if (source.dataset_id != config.dataset_id or source.organization_id != config.organization_id
                    or source.profile_status != "READY"):
                raise AutomationError(412, "INTAKE_SOURCE_UNPUBLISHED", "La entrada no es una versión completa.")
            settings = IntakeScheduleSettings.model_validate(revision.settings["calendar"])
            run = enqueue(db, config, source, None, INTAKE_ACTOR, requested_engine=settings.requested_engine,
                          intake_responsible_id=revision.responsible_user_id)
            run.execution_plan = {**run.execution_plan, "intake_schedule": {
                "schedule_id": schedule.id, "schedule_version_id": revision.id,
                "occurrence_id": occurrence.id, "responsible_user_id": revision.responsible_user_id,
                "origin": origin, "planned_at": iso(planned_at)}}
            occurrence.run_id, occurrence.status = run.id, "ENQUEUED"
            if run.status == "FAILED_PRECONDITION":
                occurrence.status, occurrence.reason_code = run.status, run.error
    except (AutomationError, OperationError) as error:
        occurrence.status, occurrence.reason_code = "BLOCKED", error.code
    if occurrence.run_id is None:
        append_event(db, organization_id=schedule.organization_id,
            dedupe_key=f"intake-occurrence:{occurrence.id}", event_type="INTAKE_SCHEDULE_BLOCKED",
            aggregate_type="INTAKE_OCCURRENCE", aggregate_id=occurrence.id, module="intake",
            payload={"recipient_user_id": revision.responsible_user_id, "status": occurrence.status,
                     "reason_code": occurrence.reason_code, "origin": origin, "configuration_id": config.id})
    return occurrence


def dispatch_due(db, now=None, *, limit=100):
    instant, count = aware(now or utcnow()), 0
    schedules = db.scalars(select(IntakeSchedule).where(IntakeSchedule.enabled.is_(True),
        IntakeSchedule.next_run_at <= instant).order_by(IntakeSchedule.next_run_at).limit(limit)).all()
    for schedule in schedules:
        root = db.get(Configuration, schedule.root_configuration_id)
        assert_active_contract(db, root, lock=True)
        schedule = db.scalar(select(IntakeSchedule).where(IntakeSchedule.id == schedule.id)
            .with_for_update().execution_options(populate_existing=True))
        if not schedule.enabled or schedule.next_run_at is None or aware(schedule.next_run_at) > instant:
            continue
        revision = schedule_revision(db, schedule)
        settings = IntakeScheduleSettings.model_validate(revision.settings["calendar"])
        if settings.mode == "EVENT" or schedule.next_run_at is None:
            continue
        planned, next_at, missed = elapsed_slot(_calendar(settings), schedule.next_run_at, instant)
        dispatch_occurrence(db, schedule, revision, f"schedule:{revision.id}:{iso(planned)}", planned, coalesced=missed)
        schedule.next_run_at = next_at
        count += 1
    return count


def consume_dataset_event(db, event):
    if event.event_type != "DATASET_VERSION_PUBLISHED" or event.payload.get("publication_status") != "COMPLETE":
        return
    source = db.get(DatasetVersion, event.aggregate_id)
    if source is None or source.organization_id != event.organization_id or source.profile_status != "READY":
        return
    schedules = db.scalars(select(IntakeSchedule).where(
        IntakeSchedule.organization_id == event.organization_id)).all()
    for schedule in schedules:
        # Choose the schedule revision in force when publication committed.
        # Consumer lag, a pause or publication of a later contract cannot erase
        # an earlier pending event or change its registered parameters.
        revision = db.scalar(select(IntakeScheduleVersion).where(
            IntakeScheduleVersion.schedule_id == schedule.id,
            IntakeScheduleVersion.created_at <= event.created_at
        ).order_by(IntakeScheduleVersion.version.desc()).limit(1))
        if revision is None or not revision.enabled:
            continue
        config = db.get(Configuration, revision.configuration_id)
        try:
            assert_active_contract(db, config, lock=True)
        except AutomationError:
            continue
        settings = IntakeScheduleSettings.model_validate(revision.settings["calendar"])
        if config.dataset_id != source.dataset_id or settings.mode != "EVENT":
            continue
        if (aware(event.created_at) < datetime.fromisoformat(revision.settings["activated_at"])
                or aware(source.created_at) < aware(settings.starts_at)):
            continue
        # Output of the same logical contract never becomes its own input.
        originating = db.get(Run, source.source_run_id) if source.source_run_id else None
        if originating and originating.module == "intake":
            origin_config = db.get(Configuration, originating.config_id)
            if contract_root(db, origin_config).id == schedule.root_configuration_id:
                continue
        dispatch_occurrence(db, schedule, revision, f"version:{source.id}", event.created_at,
                            source=source, origin="EVENT")


def occurrence_dto(db, occurrence):
    run = db.get(Run, occurrence.run_id) if occurrence.run_id else None
    job = db.scalar(select(Job).where(Job.run_id == run.id)) if run else None
    return {"id": occurrence.id, "schedule_id": occurrence.schedule_id,
        "schedule_version_id": occurrence.schedule_version_id, "configuration_id": occurrence.configuration_id,
        "dataset_version_id": occurrence.dataset_version_id, "run_id": occurrence.run_id,
        "origin": occurrence.origin, "planned_at": iso(occurrence.planned_at),
        "status": run.status if run else occurrence.status, "queue_status": job.status if job else None,
        "decision": run.decision if run else None, "reason_code": occurrence.reason_code,
        "coalesced_intervals": occurrence.coalesced_intervals, "attempts": job.attempts if job else 0}


def deletion_impact(db, config, *, lock=False):
    from .automation import revision_for, settings_for_revision
    from .automation_models import DeliveryAutomation

    root = contract_root(db, config, lock=lock)
    revisions = contract_revisions(db, root)
    ids = [item.id for item in revisions]
    state = contract_state(db, root)
    pending = list(db.scalars(select(Run.id).where(Run.config_id.in_(ids), Run.status.in_(PENDING)).order_by(Run.id)))
    # Match the exception module's technical and administrative closed states.
    exceptions = list(db.scalars(select(ExceptionCase.id).where(ExceptionCase.configuration_id.in_(ids),
        ExceptionCase.state.not_in(["CLOSED", "RESOLVED", "FALSE_POSITIVE", "DISCARDED", "ACCEPTED", "NOT_APPLICABLE"])).order_by(ExceptionCase.id)))
    schedule = schedule_for(db, root)
    delivery = []
    for item in db.scalars(select(DeliveryAutomation).where(
            DeliveryAutomation.organization_id == config.organization_id, DeliveryAutomation.enabled.is_(True))):
        settings = settings_for_revision(revision_for(db, item))
        if settings.mode == "CHAINED" and settings.intake_configuration_id in ids:
            delivery.append(item.id)
    result = {"root_configuration_id": root.id, "name": root.name,
        "deleted": bool(state and state.deleted_at or root.status == "DELETED"),
        "revision_ids": ids, "revision_count": len(ids), "pending_run_ids": pending,
        "open_exception_ids": exceptions, "intake_schedule_id": schedule.id if schedule else None,
        "intake_schedule_version": schedule.version if schedule else None,
        "intake_schedule_enabled": bool(schedule and schedule.enabled),
        "delivery_automation_ids": sorted(delivery), "can_delete": not pending and not exceptions,
        "preserves": ["revisions", "hashes", "runs", "evidence", "datasets", "historical_approvals", "registered_deliveries", "sentinel_monitors"]}
    result["impact_token"] = hashlib.sha256(json.dumps(result, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    return result


def delete_contract(db, config, user, *, impact_token, note):
    from .automation_models import DeliveryAutomation
    from .services import audit

    if "intake:delete" not in effective_permissions(db, user):
        raise AutomationError(403, "PERMISSION_DENIED", "Tu rol no permite eliminar contratos Intake.")
    if config.organization_id != user.organization_id:
        raise AutomationError(404, "NOT_FOUND", "No se encontró el contrato Intake.")
    root = assert_active_contract(db, config, lock=True)
    impact = deletion_impact(db, config)
    if impact["impact_token"] != impact_token:
        raise AutomationError(409, "INTAKE_DELETE_IMPACT_CHANGED", "Cambió el impacto; consulta nuevamente antes de eliminar.")
    if not impact["can_delete"]:
        raise AutomationError(409, "INTAKE_DELETE_BLOCKED", "Hay ejecuciones pendientes/activas o excepciones abiertas que requieren este contrato.")
    state = contract_state(db, root, create=True)
    state.deleted_at, state.deleted_by_id, state.deletion_note = utcnow(), user.id, note.strip()
    state.deletion_impact = impact
    for revision in contract_revisions(db, root):
        revision.status = "DELETED"
    schedule = schedule_for(db, root)
    if schedule:
        schedule.enabled, schedule.next_run_at, schedule.updated_at = False, None, utcnow()
    for identifier in impact["delivery_automation_ids"]:
        automation = db.get(DeliveryAutomation, identifier)
        automation.enabled, automation.next_run_at, automation.updated_at = False, None, utcnow()
    audit(db, "INTAKE_CONTRACT_DELETED", "configuration", root.id, "Contrato Intake eliminado lógicamente",
          user.name, user.organization_id, {"impact": impact, "note": note.strip()})
    return {"status": "DELETED", "label": "Contrato eliminado", "impact": impact}
