"""Delivery scheduling dispatches immutable snapshots; it never calls a SQL sink."""

from datetime import UTC, datetime, time, timedelta
from typing import Literal, cast
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from sqlalchemy import select, text, update
from sqlalchemy.engine import CursorResult
from sqlalchemy.orm import Session

from .audit_context import Actor, actor_context
from .automation_models import (
    DeliveryAutomation,
    DeliveryAutomationVersion,
    DeliveryInputClaim,
    DeliveryOccurrence,
    DeliveryTargetDecision,
    DeliveryTargetGuard,
)
from .db import iso, utcnow
from .delivery_audit import target_identity, target_policy
from .delivery_schemas import DeliveryDraft
from .models import (
    Artifact,
    Configuration,
    DatasetVersion,
    DeliveryDestination,
    DeliveryDestinationVersion,
    DeliveryOperationalReview,
    Run,
    User,
    uid,
)
from .permissions import effective_permissions
from .scheduler import aware

AUTOMATION_ACTOR = Actor("SYSTEM", "trackvance:delivery-dispatcher", "Automatización de Delivery")


class AutomationError(Exception):
    def __init__(self, status: int, code: str, message: str):
        self.status, self.code, self.message = status, code, message


class AutomationSettings(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    mode: Literal["ONCE", "INTERVAL", "DAILY", "WEEKLY", "CHAINED"] = "INTERVAL"
    timezone: str = Field(default="America/Bogota", max_length=80)
    starts_at: datetime
    interval_seconds: int = Field(default=3600, ge=60, le=2678400)
    local_time: str = "09:00"
    weekdays: list[int] = Field(default_factory=lambda: [0], max_length=7)
    source_policy: Literal["FIXED_VERSION", "LATEST_REGISTERED", "INTAKE_OUTPUT"] = "FIXED_VERSION"
    intake_configuration_id: str | None = None
    allow_warnings: bool = False
    allow_empty: bool = False
    repeat_versions: bool = False

    @field_validator("timezone")
    @classmethod
    def valid_zone(cls, value):
        try:
            ZoneInfo(value)
        except (ValueError, ZoneInfoNotFoundError):
            raise ValueError("Indica una zona IANA disponible.") from None
        return value

    @field_validator("starts_at", mode="before")
    @classmethod
    def aware_start(cls, value):
        parsed = datetime.fromisoformat(value) if isinstance(value, str) else value
        if not isinstance(parsed, datetime) or parsed.tzinfo is None:
            raise ValueError("La fecha de inicio requiere zona horaria explícita.")
        return parsed.astimezone(UTC)

    @field_validator("local_time")
    @classmethod
    def valid_time(cls, value):
        if len(value) != 5 or value[2] != ":":
            raise ValueError("La hora debe ser HH:MM.")
        try:
            time.fromisoformat(value)
        except ValueError:
            raise ValueError("La hora debe ser HH:MM.") from None
        return value

    @field_validator("weekdays")
    @classmethod
    def valid_days(cls, value):
        if not value or any(day < 0 or day > 6 for day in value) or len(set(value)) != len(value):
            raise ValueError("Selecciona días distintos de 0 (lunes) a 6 (domingo).")
        return sorted(value)

    @model_validator(mode="after")
    def valid_chain(self):
        if (self.mode == "CHAINED") != (self.source_policy == "INTAKE_OUTPUT"):
            raise ValueError("La salida Intake requiere modo encadenado y viceversa.")
        if self.mode == "CHAINED" and not self.intake_configuration_id:
            raise ValueError("Selecciona el contrato Intake que dispara la entrega.")
        return self


def _calendar_at(settings: AutomationSettings, day) -> datetime | None:
    zone = ZoneInfo(settings.timezone)
    local = datetime.combine(day, time.fromisoformat(settings.local_time)).replace(tzinfo=zone, fold=0)
    utc = local.astimezone(UTC)
    # Nonexistent wall-clock slots are skipped. Ambiguous slots use the first
    # occurrence (fold=0), never both. No elapsed-seconds arithmetic across DST.
    if utc.astimezone(zone).replace(tzinfo=None) != local.replace(tzinfo=None):
        return None
    return utc


def next_slot(settings: AutomationSettings, after: datetime, *, inclusive=False) -> datetime | None:
    instant = aware(after)
    start = aware(settings.starts_at)
    if settings.mode == "CHAINED":
        return None
    if settings.mode == "ONCE":
        return start if start > instant or (inclusive and start == instant) else None
    if settings.mode == "INTERVAL":
        if instant < start or (inclusive and instant == start):
            return start
        delta = (instant - start).total_seconds()
        step = int(delta // settings.interval_seconds)
        if not inclusive or delta % settings.interval_seconds:
            step += 1
        return start + timedelta(seconds=step * settings.interval_seconds)
    day = max(instant, start).astimezone(ZoneInfo(settings.timezone)).date()
    for offset in range(15):
        candidate_day = day + timedelta(days=offset)
        if settings.mode == "WEEKLY" and candidate_day.weekday() not in settings.weekdays:
            continue
        candidate = _calendar_at(settings, candidate_day)
        if candidate and candidate >= start and (candidate > instant or inclusive and candidate == instant):
            return candidate
    raise AutomationError(422, "SCHEDULE_INVALID", "No se pudo resolver el próximo horario.")


def elapsed_slot(settings: AutomationSettings, cursor: datetime, now: datetime):
    planned, missed = aware(cursor), 0
    next_at = next_slot(settings, planned)
    if settings.mode == "INTERVAL":
        missed = max(0, int((aware(now) - planned).total_seconds()) // settings.interval_seconds)
        planned += timedelta(seconds=missed * settings.interval_seconds)
        return planned, next_slot(settings, planned), missed
    # Calendar schedules are capped at the most recent due day; no queue avalanche.
    while next_at is not None and next_at <= aware(now):
        planned, missed = next_at, missed + 1
        next_at = next_slot(settings, planned)
    return planned, next_at, missed


def revision_for(db: Session, automation: DeliveryAutomation):
    revision = db.scalar(select(DeliveryAutomationVersion).where(
        DeliveryAutomationVersion.automation_id == automation.id,
        DeliveryAutomationVersion.organization_id == automation.organization_id,
        DeliveryAutomationVersion.version == automation.version))
    if revision is None:
        raise AutomationError(409, "AUTOMATION_REVISION_MISSING", "La revisión de automatización no está disponible.")
    return revision


def _authorized(db: Session, user: User | None, organization_id: str, draft: DeliveryDraft):
    if user is None or user.organization_id != organization_id or not user.active or user.deleted:
        raise AutomationError(412, "AUTOMATION_USER_DISABLED", "El usuario responsable no está habilitado.")
    required = {"delivery:execute", "delivery:read", "datasets:read", "destinations:use", "destinations:read"}
    if draft.write_strategy == "OVERWRITE":
        required.add("delivery:overwrite")
    if draft.target.mode == "CREATE_TABLE":
        required.add("delivery:alter_target")
    if not required.issubset(effective_permissions(db, user)):
        raise AutomationError(412, "AUTOMATION_PERMISSION_REVOKED", "El responsable no conserva los permisos necesarios.")
    destination = db.get(DeliveryDestination, draft.destination_id)
    version = db.get(DeliveryDestinationVersion, draft.destination_version_id)
    if (destination is None or version is None or destination.organization_id != organization_id
            or version.organization_id != organization_id or version.destination_id != destination.id
            or not destination.enabled or destination.deleted):
        raise AutomationError(412, "AUTOMATION_DESTINATION_UNAVAILABLE", "El destino de la automatización no está habilitado.")
    policy, _, _ = target_policy(db, destination, version, draft.target.model_dump())
    if (draft.audit_columns_enabled and (policy is None or policy.materialized_at is None)
            and "delivery:alter_target" not in effective_permissions(db, user)):
        raise AutomationError(412, "AUTOMATION_PERMISSION_REVOKED", "El responsable no puede materializar auditoría en el destino.")
    return user, destination, version


def save_automation(db: Session, actor: User, configuration_id: str, name: str,
                    settings: AutomationSettings, *, enabled=True, responsible_user_id=None,
                    automation: DeliveryAutomation | None = None, expected_version=None):
    from .services import audit

    config = db.get(Configuration, configuration_id)
    if config is None or config.organization_id != actor.organization_id or config.module != "DELIVERY" or config.status != "PUBLISHED":
        raise AutomationError(404, "NOT_FOUND", "No se encontró la configuración de Delivery.")
    responsible = db.get(User, responsible_user_id or actor.id)
    if responsible is None or responsible.organization_id != actor.organization_id:
        raise AutomationError(404, "NOT_FOUND", "No se encontró el responsable.")
    if enabled:
        _authorized(db, responsible, actor.organization_id, DeliveryDraft.model_validate(config.config))
    if settings.mode == "CHAINED":
        intake = db.get(Configuration, settings.intake_configuration_id)
        if intake is None or intake.organization_id != actor.organization_id or intake.module != "intake":
            raise AutomationError(404, "NOT_FOUND", "No se encontró el contrato Intake.")
        if responsible and "intake:read" not in effective_permissions(db, responsible):
            raise AutomationError(403, "FORBIDDEN", "El responsable no puede consultar el Intake que dispara la entrega.")
    now = utcnow()
    if not name.strip():
        raise AutomationError(422, "NAME_REQUIRED", "Indica el nombre de la automatización.")
    previous = None
    if automation is not None:
        if automation.organization_id != actor.organization_id or automation.version != expected_version:
            raise AutomationError(409, "VERSION_CONFLICT", "La automatización cambió; actualiza el formulario.")
        previous = settings_for_revision(revision_for(db, automation))
    same_anchor = previous is not None and aware(settings.starts_at) == aware(previous.starts_at)
    if aware(settings.starts_at) < now - timedelta(seconds=60) and not same_anchor:
        raise AutomationError(422, "SCHEDULE_IN_PAST", "La primera ejecución debe ser actual o futura.")
    calendar_fields = ("mode", "timezone", "starts_at", "interval_seconds", "local_time", "weekdays")
    same_calendar = previous is not None and all(getattr(settings, key) == getattr(previous, key) for key in calendar_fields)
    if automation is None:
        automation = DeliveryAutomation(id=uid(), organization_id=actor.organization_id,
            name=name.strip(), version=1, enabled=enabled, responsible_user_id=responsible.id)
        db.add(automation)
        db.flush()
    else:
        changed = db.execute(update(DeliveryAutomation).where(
            DeliveryAutomation.id == automation.id, DeliveryAutomation.version == expected_version
        ).values(version=expected_version + 1, name=name.strip(), enabled=enabled,
                 responsible_user_id=responsible.id, updated_at=now).execution_options(synchronize_session=False))
        if cast(CursorResult, changed).rowcount != 1:
            raise AutomationError(409, "VERSION_CONFLICT", "La automatización cambió; actualiza el formulario.")
        db.refresh(automation)
    if not same_calendar:
        # Editing business fields preserves the live cursor, including a
        # consumed ONCE's null cursor. A changed calendar uses future slots;
        # its retained historical anchor must not be dispatched again.
        if previous is not None and aware(settings.starts_at) < now:
            automation.next_run_at = next_slot(settings, now)
        else:
            automation.next_run_at = next_slot(settings, settings.starts_at, inclusive=True)
    revision = DeliveryAutomationVersion(id=uid(), organization_id=actor.organization_id,
        automation_id=automation.id, version=automation.version, configuration_id=config.id,
        responsible_user_id=responsible.id, enabled=enabled,
        settings=settings.model_dump(mode="json") | {"template": config.config, "activated_at": iso(now)},
        actor_id=actor.id)
    db.add(revision)
    audit(db, "DELIVERY_AUTOMATION_UPDATED", "delivery_automation", automation.id,
          "Automatización de Delivery versionada", actor.name, actor.organization_id,
          {"version": automation.version, "enabled": enabled, "user_id": responsible.id})
    db.flush()
    return automation


def settings_for_revision(revision):
    return AutomationSettings.model_validate({key: value for key, value in revision.settings.items()
                                              if key not in {"template", "activated_at"}})


def automation_dto(db: Session, item: DeliveryAutomation):
    revision = revision_for(db, item)
    user = db.get(User, item.responsible_user_id)
    return {"id": item.id, "name": item.name, "version": item.version, "enabled": item.enabled,
        "responsible_user_id": item.responsible_user_id, "responsible_name": user.name if user else None,
        "configuration_id": revision.configuration_id, "automation_version_id": revision.id,
        "settings": settings_for_revision(revision).model_dump(mode="json"),
        "next_run_at": iso(item.next_run_at), "created_at": iso(item.created_at),
        "updated_at": iso(item.updated_at), "misfire_policy": "COALESCE_LATEST",
        "overlap_policy": "SKIP_WHILE_ACTIVE"}


def occurrence_dto(db: Session, item: DeliveryOccurrence):
    run = db.get(Run, item.run_id) if item.run_id else None
    return {"id": item.id, "automation_id": item.automation_id,
        "automation_version_id": item.automation_version_id, "origin": item.origin,
        "planned_at": iso(item.planned_at), "dispatched_at": iso(item.dispatched_at),
        "source_run_id": item.source_run_id, "dataset_version_id": item.dataset_version_id,
        "run_id": item.run_id, "status": run.status if run else item.status,
        "decision": run.decision if run else None, "reason_code": item.reason_code,
        "started_at": iso(run.started_at) if run else None,
        "finished_at": iso(run.finished_at) if run else None,
        "coalesced_intervals": item.coalesced_intervals}


def _guard(db: Session, organization_id: str, fingerprint: str):
    if db.get_bind().dialect.name == "postgresql":
        key = int.from_bytes(bytes.fromhex(fingerprint[:16]), "big", signed=True)
        db.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": key})
    guard = db.scalar(select(DeliveryTargetGuard).where(
        DeliveryTargetGuard.organization_id == organization_id,
        DeliveryTargetGuard.target_fingerprint == fingerprint).with_for_update())
    if guard is None:
        guard = DeliveryTargetGuard(id=uid(), organization_id=organization_id,
                                    target_fingerprint=fingerprint)
        db.add(guard)
        db.flush()
    if not guard.unknown_run_id:
        # Pre-upgrade UNKNOWN attempts must also fence later deliveries. Only
        # immutable configuration/destination revisions establish target identity.
        unresolved = db.execute(select(Run, Configuration).join(Configuration, Configuration.id == Run.config_id)
            .where(Run.organization_id == organization_id, Run.module == "DELIVERY", Run.status == "UNKNOWN",
                   ~select(DeliveryTargetDecision.id).where(DeliveryTargetDecision.run_id == Run.id).exists())
            .order_by(Run.created_at.desc()))
        for old_run, old_config in unresolved:
            draft = DeliveryDraft.model_validate(old_config.config)
            destination = db.get(DeliveryDestination, draft.destination_id)
            version = db.get(DeliveryDestinationVersion, draft.destination_version_id)
            if destination and version and version.organization_id == organization_id:
                old_fingerprint, _ = target_identity(organization_id, destination.sink_type,
                                                      version.config, draft.target.model_dump())
                if old_fingerprint == fingerprint:
                    guard.unknown_run_id = old_run.id
                    break
    return guard


def claim_delivery_target(db: Session, run: Run):
    """Call immediately before STARTED, within its durable metadata transaction."""
    config = db.get(Configuration, run.config_id)
    if config is None:
        raise AutomationError(412, "CONFIGURATION_UNAVAILABLE", "La configuración no está disponible.")
    draft = DeliveryDraft.model_validate(config.config)
    destination = db.get(DeliveryDestination, draft.destination_id)
    version = db.get(DeliveryDestinationVersion, draft.destination_version_id)
    if destination is None or version is None:
        raise AutomationError(412, "DESTINATION_UNAVAILABLE", "El destino no está disponible.")
    fingerprint, _ = target_identity(run.organization_id, destination.sink_type,
                                     version.config, draft.target.model_dump())
    guard = _guard(db, run.organization_id, fingerprint)
    if guard.unknown_run_id:
        raise AutomationError(412, "TARGET_UNKNOWN_BLOCKED", "El target requiere una decisión operativa sobre una confirmación desconocida.")
    if guard.active_run_id and guard.active_run_id != run.id:
        raise AutomationError(412, "TARGET_BUSY", "Otra entrega mantiene una operación activa sobre este target.")
    guard.active_run_id = run.id
    run.execution_plan = {**(run.execution_plan or {}), "target_guard_id": guard.id,
                          "target_fingerprint": fingerprint}
    return guard


def authorize_automated_run(db: Session, run: Run):
    """Revalidate the real executor's current RBAC immediately before STARTED."""
    metadata = (run.execution_plan or {}).get("automation")
    if metadata:
        config = db.get(Configuration, run.config_id)
        if config is None:
            raise AutomationError(412, "CONFIGURATION_UNAVAILABLE", "La configuración no está disponible.")
        _authorized(db, db.get(User, metadata.get("responsible_user_id")), run.organization_id,
                    DeliveryDraft.model_validate(config.config))


def settle_delivery_target(db: Session, run: Run):
    guard_id = (run.execution_plan or {}).get("target_guard_id")
    guard = db.get(DeliveryTargetGuard, guard_id) if guard_id else None
    if guard and guard.organization_id == run.organization_id and guard.active_run_id == run.id:
        if run.status == "UNKNOWN":
            guard.unknown_run_id, guard.active_run_id = run.id, None
        elif run.status in {"SUCCESS", "FAILED", "FAILED_PRECONDITION", "CANCELLED"}:
            guard.active_run_id = None


def release_unknown_target(db: Session, user: User, run_id: str, review_id: str, note: str):
    from .services import audit

    review, run = db.get(DeliveryOperationalReview, review_id), db.get(Run, run_id)
    if (run is None or review is None or run.organization_id != user.organization_id
            or review.organization_id != user.organization_id or review.run_id != run.id):
        raise AutomationError(404, "NOT_FOUND", "No se encontró la revisión de esa entrega.")
    if review.outcome == "INCONCLUSIVE":
        raise AutomationError(412, "REVIEW_INCONCLUSIVE", "La revisión inconclusa no permite reanudar el target.")
    guard = db.scalar(select(DeliveryTargetGuard).where(
        DeliveryTargetGuard.organization_id == user.organization_id,
        DeliveryTargetGuard.unknown_run_id == run.id).with_for_update())
    if guard is None and run.status == "UNKNOWN":
        config = db.get(Configuration, run.config_id)
        if config:
            draft = DeliveryDraft.model_validate(config.config)
            destination = db.get(DeliveryDestination, draft.destination_id)
            version = db.get(DeliveryDestinationVersion, draft.destination_version_id)
            if destination and version and version.organization_id == user.organization_id:
                fingerprint, _ = target_identity(user.organization_id, destination.sink_type,
                                                  version.config, draft.target.model_dump())
                candidate = _guard(db, user.organization_id, fingerprint)
                if candidate.unknown_run_id == run.id:
                    guard = candidate
    if guard is None:
        raise AutomationError(409, "TARGET_NOT_BLOCKED", "El target no está bloqueado por esa entrega.")
    db.add(DeliveryTargetDecision(id=uid(), organization_id=user.organization_id,
        guard_id=guard.id, run_id=run.id, review_id=review.id, actor_id=user.id, note=note.strip()))
    guard.unknown_run_id = None
    audit(db, "DELIVERY_TARGET_RESUMED", "run", run.id,
          "Target reanudado tras decisión operativa explícita; intento histórico conservado",
          user.name, user.organization_id, {"review_id": review.id, "status": "RESUMED"}, run_id=run.id)


def _resolve_source(db, config, revision, settings, source_run):
    if settings.source_policy == "INTAKE_OUTPUT":
        if (source_run is None or source_run.module != "intake" or source_run.status != "SUCCESS"
                or source_run.organization_id != config.organization_id
                or source_run.config_id != settings.intake_configuration_id):
            return None, "INTAKE_NOT_ELIGIBLE"
        decisions = {"APPROVED", "APPROVED_WITH_WARNINGS"} if settings.allow_warnings else {"APPROVED"}
        if source_run.decision not in decisions:
            return None, "INTAKE_DECISION_NOT_ACCEPTED"
        source = db.get(DatasetVersion, source_run.output_version_id) if source_run.output_version_id else None
        if source is None or source.source_run_id != source_run.id or not source_run.evidence_path:
            return None, "INTAKE_OUTPUT_UNAVAILABLE"
    elif settings.source_policy == "FIXED_VERSION":
        source = db.get(DatasetVersion, revision.settings["template"]["dataset_version_id"])
    else:
        source = db.scalar(select(DatasetVersion).where(
            DatasetVersion.organization_id == config.organization_id,
            DatasetVersion.dataset_id == config.dataset_id,
            DatasetVersion.profile_status == "READY"
        ).order_by(DatasetVersion.version.desc()).limit(1))
    if (source is None or source.organization_id != config.organization_id
            or source.profile_status != "READY"
            or (settings.source_policy != "INTAKE_OUTPUT" and source.dataset_id != config.dataset_id)):
        return None, "NO_ELIGIBLE_VERSION"
    if not settings.allow_empty and source.row_count == 0:
        return source, "EMPTY_INPUT_BLOCKED"
    artifact = db.get(Artifact, source.canonical_artifact_id) if source.canonical_artifact_id else None
    if (artifact is None or artifact.organization_id != config.organization_id
            or artifact.kind not in {"CANONICAL_PARQUET", "INTAKE_ACCEPTED"}
            or not artifact.sha256 or not source.sha256 or not source.schema_hash):
        return source, "OUTPUT_NOT_PUBLISHED"
    # Eligibility is a metadata decision. Descriptor/part bytes remain an
    # execution precondition, checked by the Delivery worker before STARTED.
    return source, None


def dispatch_occurrence(db: Session, automation: DeliveryAutomation, revision: DeliveryAutomationVersion,
                        trigger_key: str, planned_at: datetime, *, origin="SCHEDULED",
                        source_run: Run | None = None, coalesced=0, repeat=False, request_hash=None):
    from .delivery_service import DeliveryOperationError, enqueue_delivery
    from .events import append_event
    from .services import audit

    if db.scalar(select(DeliveryOccurrence.id).where(
            DeliveryOccurrence.automation_id == automation.id, DeliveryOccurrence.trigger_key == trigger_key)):
        return None
    # Serialize chain consumers and ticks on the same automation; a UNIQUE
    # occurrence and input claim remain the persistent last line of defence.
    db.execute(select(DeliveryAutomation.id).where(DeliveryAutomation.id == automation.id).with_for_update())
    if db.scalar(select(DeliveryOccurrence.id).where(
            DeliveryOccurrence.automation_id == automation.id, DeliveryOccurrence.trigger_key == trigger_key)):
        return None
    settings = settings_for_revision(revision)
    config = db.get(Configuration, revision.configuration_id)
    if config is None or config.organization_id != automation.organization_id:
        raise AutomationError(409, "CONFIGURATION_UNAVAILABLE", "La configuración no está disponible.")
    source, reason = _resolve_source(db, config, revision, settings, source_run)
    occurrence = DeliveryOccurrence(id=uid(), organization_id=automation.organization_id,
        automation_id=automation.id, automation_version_id=revision.id, trigger_key=trigger_key,
        request_hash=request_hash,
        origin=origin, planned_at=planned_at, dispatched_at=utcnow(),
        source_run_id=source_run.id if source_run else None, dataset_version_id=source.id if source else None,
        status="SKIPPED", reason_code=reason, coalesced_intervals=coalesced)
    db.add(occurrence)
    db.flush()
    try:
        user, destination, version = _authorized(db, db.get(User, revision.responsible_user_id),
            automation.organization_id, DeliveryDraft.model_validate(revision.settings["template"]))
        fingerprint, _ = target_identity(automation.organization_id, destination.sink_type,
                                         version.config, revision.settings["template"]["target"])
        guard = _guard(db, automation.organization_id, fingerprint)
        if guard.unknown_run_id:
            reason = "TARGET_UNKNOWN_BLOCKED"
        elif guard.active_run_id or db.scalar(select(Run.id).join(DeliveryOccurrence, DeliveryOccurrence.run_id == Run.id).where(
                DeliveryOccurrence.automation_id == automation.id, Run.status.in_(["QUEUED", "RUNNING"])).limit(1)):
            reason = "AUTOMATION_BUSY"
        elif source and not (settings.repeat_versions or repeat) and db.scalar(select(DeliveryInputClaim.id).where(
                DeliveryInputClaim.automation_id == automation.id, DeliveryInputClaim.dataset_version_id == source.id)):
            reason = "VERSION_ALREADY_PROCESSED"
        if reason is None and source is not None:
            with db.begin_nested():
                draft = DeliveryDraft.model_validate({**revision.settings["template"], "dataset_version_id": source.id})
                effective = Configuration(id=uid(), organization_id=automation.organization_id,
                    name=automation.name, module="DELIVERY", version=revision.version,
                    dataset_id=source.dataset_id, owner=config.owner, status="AUTOMATION_EFFECTIVE", description="Configuración efectiva de una ocurrencia",
                    config=draft.snapshot())
                db.add(effective)
                db.flush()
                token = actor_context.set(AUTOMATION_ACTOR)
                try:
                    run = enqueue_delivery(db, effective, source, user)
                finally:
                    actor_context.reset(token)
                run.initiated_by_type, run.initiated_by_id = "SYSTEM", AUTOMATION_ACTOR.id
                run.initiated_by = AUTOMATION_ACTOR.display_name
                run.execution_plan = {**run.execution_plan, "automation": {
                    "automation_id": automation.id, "automation_version_id": revision.id,
                    "occurrence_id": occurrence.id, "responsible_user_id": user.id,
                    "origin": origin, "source_run_id": source_run.id if source_run else None,
                    "repeat_deliberate": repeat or settings.repeat_versions,
                    "target_fingerprint": fingerprint}}
                occurrence.status, occurrence.run_id = "ENQUEUED", run.id
                if not (settings.repeat_versions or repeat):
                    db.add(DeliveryInputClaim(id=uid(), organization_id=automation.organization_id,
                        automation_id=automation.id, dataset_version_id=source.id, occurrence_id=occurrence.id))
        occurrence.reason_code = reason
    except (AutomationError, DeliveryOperationError) as error:
        occurrence.status, occurrence.reason_code = "BLOCKED", error.code
    if occurrence.run_id is None:
        append_event(db, organization_id=automation.organization_id,
            dedupe_key=f"occurrence:{occurrence.id}", event_type="DELIVERY_AUTOMATION_BLOCKED",
            aggregate_type="DELIVERY_OCCURRENCE", aggregate_id=occurrence.id, module="DELIVERY",
            payload={"recipient_user_id": revision.responsible_user_id, "origin": origin,
                     "status": occurrence.status, "reason_code": occurrence.reason_code,
                     "automation_id": automation.id})
    audit(db, "DELIVERY_AUTOMATION_DISPATCHED", "delivery_automation", automation.id,
          "Ocurrencia de Delivery registrada", AUTOMATION_ACTOR, automation.organization_id,
          {"status": occurrence.status, "reason_code": occurrence.reason_code,
           "planned_at": iso(planned_at), "dataset_version_id": occurrence.dataset_version_id}, run_id=occurrence.run_id)
    return occurrence


def dispatch_due(db: Session, now: datetime | None = None, *, limit=100):
    instant = aware(now or utcnow())
    due = db.scalars(select(DeliveryAutomation).where(DeliveryAutomation.enabled.is_(True),
        DeliveryAutomation.next_run_at <= instant).order_by(DeliveryAutomation.next_run_at).limit(limit)).all()
    count = 0
    for automation in due:
        if automation.next_run_at is None:
            continue
        revision = revision_for(db, automation)
        settings = settings_for_revision(revision)
        planned, next_at, missed = elapsed_slot(settings, automation.next_run_at, instant)
        claimed = db.execute(update(DeliveryAutomation).where(
            DeliveryAutomation.id == automation.id, DeliveryAutomation.version == automation.version,
            DeliveryAutomation.enabled.is_(True), DeliveryAutomation.next_run_at == automation.next_run_at
        ).values(next_run_at=next_at).execution_options(synchronize_session=False))
        if cast(CursorResult, claimed).rowcount != 1:
            continue
        dispatch_occurrence(db, automation, revision, f"schedule:{revision.id}:{iso(planned)}",
                            planned, coalesced=missed)
        db.expire(automation)
        count += 1
    return count


def consume_intake_event(db: Session, event):
    if event.module != "intake" or event.event_type != "RUN_TERMINAL":
        return
    run = db.get(Run, event.aggregate_id)
    if run is None or run.organization_id != event.organization_id:
        return
    automations = db.scalars(select(DeliveryAutomation).where(
        DeliveryAutomation.organization_id == event.organization_id, DeliveryAutomation.enabled.is_(True))).all()
    for automation in automations:
        revision = revision_for(db, automation)
        settings = settings_for_revision(revision)
        if settings.mode != "CHAINED" or settings.intake_configuration_id != run.config_id:
            continue
        # Activation is a durable temporal fence, never replay historic events.
        activated = datetime.fromisoformat(revision.settings["activated_at"])
        if aware(event.created_at) < activated or aware(run.created_at) < aware(settings.starts_at):
            continue
        dispatch_occurrence(db, automation, revision, f"intake:{run.id}", event.created_at,
                            origin="CHAINED", source_run=run)
