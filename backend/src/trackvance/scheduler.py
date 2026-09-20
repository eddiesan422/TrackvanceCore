"""Transactional local scheduler. It evaluates registered snapshots, never external credentials."""

from datetime import UTC, datetime, timedelta
from typing import cast

from sqlalchemy import select, update
from sqlalchemy.engine import CursorResult
from sqlalchemy.orm import Session

from .audit_context import Actor, actor_context
from .db import SessionLocal, iso, utcnow
from .models import (
    Configuration,
    DatasetVersion,
    MonitorOccurrence,
    MonitorSchedule,
    MonitorScheduleVersion,
    Run,
    User,
)
from .processing import ProcessingError
from .services import audit, enqueue

SCHEDULER_ACTOR = Actor("SYSTEM", "trackvance:local-scheduler", "Programador Sentinel")


class ScheduleError(Exception):
    def __init__(self, status: int, code: str, message: str):
        self.status, self.code, self.message = status, code, message


def aware(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def current_revision(db: Session, schedule: MonitorSchedule) -> MonitorScheduleVersion:
    revision = db.scalar(select(MonitorScheduleVersion).where(
        MonitorScheduleVersion.schedule_id == schedule.id,
        MonitorScheduleVersion.organization_id == schedule.organization_id,
        MonitorScheduleVersion.version == schedule.version,
    ))
    if revision is None:
        raise ScheduleError(409, "SCHEDULE_REVISION_MISSING", "La programación no tiene una revisión válida.")
    return revision


def schedule_dto(db: Session, schedule: MonitorSchedule) -> dict:
    revision = current_revision(db, schedule)
    return {
        "id": schedule.id, "monitor_id": schedule.monitor_id,
        "version": schedule.version, "schedule_version_id": revision.id,
        "interval_seconds": revision.interval_seconds, "enabled": schedule.enabled,
        "next_run_at": iso(schedule.next_run_at), "starts_at": iso(revision.starts_at),
        "updated_at": iso(schedule.updated_at), "source_policy": "LATEST_REGISTERED_SNAPSHOT",
        "misfire_policy": "COALESCE_LATEST", "overlap_policy": "SKIP_WHILE_ACTIVE",
    }


def save_schedule(
    db: Session, monitor: Configuration, user: User, *, interval_seconds: int,
    enabled: bool, starts_at: datetime | None, expected_version: int | None,
) -> MonitorSchedule:
    if monitor.module != "sentinel" or monitor.organization_id != user.organization_id:
        raise ScheduleError(404, "MONITOR_NOT_FOUND", "No se encontró el monitor.")
    if not 60 <= interval_seconds <= 2678400:
        raise ScheduleError(422, "INVALID_SCHEDULE_INTERVAL", "La periodicidad debe estar entre 1 minuto y 31 días.")
    now = utcnow()
    start = aware(starts_at) if starts_at else now
    # Creation and edits explicitly start a new cadence. Existing occurrences stay immutable.
    if start < now - timedelta(seconds=60):
        raise ScheduleError(422, "SCHEDULE_IN_PAST", "La primera ejecución debe ser actual o futura.")
    schedule = db.scalar(select(MonitorSchedule).where(
        MonitorSchedule.monitor_id == monitor.id, MonitorSchedule.organization_id == user.organization_id,
    ))
    if schedule is None:
        if expected_version is not None:
            raise ScheduleError(409, "SCHEDULE_CONFLICT", "La programación cambió. Actualiza e intenta nuevamente.")
        schedule = MonitorSchedule(
            organization_id=user.organization_id, monitor_id=monitor.id,
            version=1, enabled=enabled, next_run_at=start,
        )
        db.add(schedule)
        db.flush()
    else:
        if expected_version != schedule.version:
            raise ScheduleError(409, "SCHEDULE_CONFLICT", "La programación cambió. Actualiza e intenta nuevamente.")
        result = db.execute(update(MonitorSchedule).where(
            MonitorSchedule.id == schedule.id, MonitorSchedule.version == expected_version,
        ).values(version=expected_version + 1, enabled=enabled, next_run_at=start, updated_at=now)
            .execution_options(synchronize_session=False))
        if cast(CursorResult, result).rowcount != 1:
            raise ScheduleError(409, "SCHEDULE_CONFLICT", "Otra operación modificó la programación.")
        db.refresh(schedule)
    revision = MonitorScheduleVersion(
        organization_id=user.organization_id, schedule_id=schedule.id, version=schedule.version,
        interval_seconds=interval_seconds, enabled=enabled, starts_at=start, actor_id=user.id,
    )
    db.add(revision)
    db.flush()
    audit(db, "MONITOR_SCHEDULE_UPDATED", "configuration", monitor.id,
          "Programación de Sentinel actualizada", user.name, user.organization_id,
          {"schedule_id": schedule.id, "schedule_version_id": revision.id,
           "version": schedule.version, "enabled": enabled, "interval_seconds": interval_seconds})
    return schedule


def dispatch_due(db: Session, now: datetime | None = None, *, limit: int = 100) -> int:
    """CAS cursor, revision and occurrence/job commit together; callers own the transaction.

    Late intervals coalesce to the latest elapsed slot. One busy monitor is skipped,
    not queued repeatedly. Cursor advancement is rolled back on database failures.
    """
    instant = aware(now or utcnow())
    due = db.scalars(select(MonitorSchedule).where(
        MonitorSchedule.enabled.is_(True), MonitorSchedule.next_run_at <= instant,
    ).order_by(MonitorSchedule.next_run_at, MonitorSchedule.id).limit(limit)).all()
    dispatched = 0
    token = actor_context.set(SCHEDULER_ACTOR)
    try:
        for schedule in due:
            revision = current_revision(db, schedule)
            missed = max(0, int((instant - aware(schedule.next_run_at)).total_seconds()) // revision.interval_seconds)
            planned = aware(schedule.next_run_at) + timedelta(seconds=missed * revision.interval_seconds)
            next_at = planned + timedelta(seconds=revision.interval_seconds)
            claimed = db.execute(update(MonitorSchedule).where(
                MonitorSchedule.id == schedule.id, MonitorSchedule.version == schedule.version,
                MonitorSchedule.enabled.is_(True), MonitorSchedule.next_run_at == schedule.next_run_at,
            ).values(next_run_at=next_at).execution_options(synchronize_session=False))
            if cast(CursorResult, claimed).rowcount != 1:
                continue
            existing = db.scalar(select(MonitorOccurrence.id).where(
                MonitorOccurrence.schedule_id == schedule.id, MonitorOccurrence.planned_at == planned,
            ))
            if existing:
                # An edit can intentionally reuse a recent start time. Preserve the recorded
                # occurrence and advance the new cadence instead of replaying historical work.
                audit(db, "MONITOR_SCHEDULE_SLOT_REUSED", "configuration", schedule.monitor_id,
                      "El intervalo ya tenía una ocurrencia registrada", SCHEDULER_ACTOR, schedule.organization_id,
                      {"schedule_id": schedule.id, "planned_at": iso(planned), "reason_code": "ALREADY_RECORDED"})
                db.expire(schedule)
                continue
            monitor = db.get(Configuration, schedule.monitor_id)
            if monitor is None or monitor.module != "sentinel" or monitor.organization_id != schedule.organization_id:
                raise ScheduleError(409, "SCHEDULE_MONITOR_INVALID", "Referencia de monitor inconsistente.")
            source = db.scalar(select(DatasetVersion).where(
                DatasetVersion.organization_id == schedule.organization_id,
                DatasetVersion.dataset_id == monitor.dataset_id,
            ).order_by(DatasetVersion.version.desc()).limit(1))
            active = db.scalar(select(Run.id).where(
                Run.organization_id == schedule.organization_id, Run.config_id == monitor.id,
                Run.status.in_(["QUEUED", "RUNNING"]),
            ).limit(1))
            occurrence = MonitorOccurrence(
                organization_id=schedule.organization_id, schedule_id=schedule.id,
                schedule_version_id=revision.id, monitor_id=monitor.id,
                dataset_version_id=source.id if source else None, planned_at=planned,
                dispatched_at=instant, status="SKIPPED", coalesced_intervals=missed,
                reason_code="MONITOR_BUSY" if active else "NO_DATASET_VERSION" if source is None else None,
            )
            db.add(occurrence)
            db.flush()
            if source is not None and active is None:
                try:
                    with db.begin_nested():
                        run = enqueue(db, monitor, source, None, SCHEDULER_ACTOR.display_name)
                        occurrence.run_id = run.id
                        occurrence.status = "FAILED_PRECONDITION" if run.status == "FAILED_PRECONDITION" else "ENQUEUED"
                        occurrence.reason_code = run.error if run.status == "FAILED_PRECONDITION" else None
                        run.execution_plan = {**run.execution_plan, "schedule": {
                            "schedule_id": schedule.id, "schedule_version_id": revision.id,
                            "schedule_version": revision.version, "occurrence_id": occurrence.id,
                            "planned_at": iso(planned), "dispatched_at": iso(instant),
                            "interval_seconds": revision.interval_seconds, "coalesced_intervals": missed,
                            "source_policy": "LATEST_REGISTERED_SNAPSHOT",
                            "misfire_policy": "COALESCE_LATEST", "overlap_policy": "SKIP_WHILE_ACTIVE",
                        }}
                except (ProcessingError, ValueError):
                    occurrence.status, occurrence.reason_code = "FAILED_PRECONDITION", "INVALID_MONITOR_CONFIGURATION"
            audit(db, "MONITOR_SCHEDULE_DISPATCHED", "configuration", monitor.id,
                  "Intervalo Sentinel registrado", SCHEDULER_ACTOR, schedule.organization_id,
                  {"schedule_id": schedule.id, "schedule_version_id": revision.id,
                   "planned_at": iso(planned), "dispatched_at": iso(instant),
                   "coalesced_intervals": missed, "status": occurrence.status,
                   "reason_code": occurrence.reason_code}, run_id=occurrence.run_id)
            db.flush()
            # Avoid reusing the stale cursor if the same session performs another tick.
            db.expire(schedule)
            dispatched += 1
    finally:
        actor_context.reset(token)
    return dispatched


def tick() -> int:
    with SessionLocal() as db:
        count = dispatch_due(db)
        db.commit()
        return count


def occurrence_dto(db: Session, occurrence: MonitorOccurrence) -> dict:
    run = db.get(Run, occurrence.run_id) if occurrence.run_id else None
    return {
        "id": occurrence.id, "schedule_id": occurrence.schedule_id,
        "schedule_version_id": occurrence.schedule_version_id, "monitor_id": occurrence.monitor_id,
        "dataset_version_id": occurrence.dataset_version_id, "run_id": occurrence.run_id,
        "planned_at": iso(occurrence.planned_at), "dispatched_at": iso(occurrence.dispatched_at),
        "started_at": iso(run.started_at) if run else None,
        "finished_at": iso(run.finished_at) if run else None,
        "status": run.status if run else occurrence.status, "decision": run.decision if run else None,
        "reason_code": occurrence.reason_code, "coalesced_intervals": occurrence.coalesced_intervals,
        "metrics": run.metrics if run else {},
    }
