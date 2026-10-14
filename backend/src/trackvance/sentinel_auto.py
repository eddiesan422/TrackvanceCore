"""Automatic monitoring reuses immutable versions and the existing durable queue.

New-version evaluations and lightweight freshness reviews are separate identities.
No executor is inferred from a system actor or an arbitrary administrator.
"""

from datetime import timedelta
from typing import cast

from sqlalchemy import select, update
from sqlalchemy.engine import CursorResult
from sqlalchemy.orm import Session

from .audit_context import Actor
from .db import iso, utcnow
from .governance import authorize_dataset, authorize_dataset_content
from .models import (
    Configuration,
    Dataset,
    DatasetVersion,
    MonitorSchedule,
    MonitorScheduleVersion,
    Run,
)
from .operations_common import OperationError
from .scheduler import ScheduleError, authorize_responsible, aware
from .sentinel_auto_models import (
    AutomaticMonitor,
    AutomaticMonitorRevision,
    SentinelLightReview,
    SentinelVersionEvaluation,
)


def monitor_family(db: Session, monitor: Configuration) -> set[str]:
    configs = db.scalars(select(Configuration).where(Configuration.organization_id == monitor.organization_id,
        Configuration.module == "sentinel", Configuration.dataset_id == monitor.dataset_id)).all()
    family = {monitor.id}
    for _ in configs:
        before = len(family)
        for item in configs:
            if item.id in family and item.previous_version_id:
                family.add(item.previous_version_id)
            if item.previous_version_id in family:
                family.add(item.id)
        if len(family) == before:
            break
    return family


def current_monitor(db: Session, automatic: AutomaticMonitor) -> Configuration:
    root = db.get(Configuration, automatic.monitor_root_id)
    if root is None or root.organization_id != automatic.organization_id:
        raise ScheduleError(409, "AUTOMATIC_MONITOR_INVALID", "La identidad del monitor está incompleta.")
    return db.scalars(select(Configuration).where(Configuration.id.in_(monitor_family(db, root)))
        .order_by(Configuration.version.desc(), Configuration.created_at.desc())).first() or root


def _authorized_candidate(db: Session, dataset: Dataset, candidate: str | None) -> str | None:
    try:
        user = authorize_responsible(db, candidate, dataset.organization_id)
        authorize_dataset(db, user, dataset.id)
        return user.id
    except (ScheduleError, OperationError):
        return None


def ensure_automatic_monitor(db: Session, dataset: Dataset, responsible_user_id: str | None = None) -> AutomaticMonitor:
    """Add only missing identities; existing configuration JSON/cadences stay intact."""
    from .services import audit

    db.scalar(select(Dataset).where(Dataset.id == dataset.id).with_for_update())
    existing = db.scalar(select(AutomaticMonitor).where(AutomaticMonitor.dataset_id == dataset.id))
    if existing:
        return existing
    monitors = db.scalars(select(Configuration).where(Configuration.organization_id == dataset.organization_id,
        Configuration.dataset_id == dataset.id, Configuration.module == "sentinel")
        .order_by(Configuration.created_at, Configuration.id)).all()
    monitor = next((item for item in monitors if not item.previous_version_id), None)
    enabled = True
    candidate = None
    if monitor:
        family = monitor_family(db, monitor)
        schedules = db.scalars(select(MonitorSchedule).where(MonitorSchedule.monitor_id.in_(family))
            .order_by(MonitorSchedule.updated_at.desc(), MonitorSchedule.id)).all()
        if schedules:
            # Any explicitly paused family schedule wins over automatic enablement.
            enabled = all(schedule.enabled for schedule in schedules)
            revision = db.scalar(select(MonitorScheduleVersion).where(
                MonitorScheduleVersion.schedule_id == schedules[0].id,
                MonitorScheduleVersion.version == schedules[0].version))
            candidate = revision.responsible_user_id if revision else None
    if monitor is None:
        monitor = Configuration(organization_id=dataset.organization_id, name=(dataset.name[:130] + " · Monitor"),
            module="sentinel", dataset_id=dataset.id, owner=dataset.owner,
            description="Monitoreo automático del perfil completo; los nulos y categorías son observaciones.",
            config={"schema_version": 2, "automatic_base_version": 1, "rules": [],
                    "required_columns": [], "null_columns": [], "max_age_hours": 48,
                    "max_volume_change_pct": 50})
        db.add(monitor)
        db.flush()
    candidates = [responsible_user_id, dataset.technical_custodian_id,
                  dataset.steward_id, dataset.business_owner_id]
    # An explicitly assigned historical executor remains the executor even when
    # revoked; dispatch records BLOCKED instead of upgrading to a publisher/admin.
    responsible = candidate or next((valid for identity in candidates
                                    if (valid := _authorized_candidate(db, dataset, identity))), None)
    if responsible is None and candidate is None:
        # Historical executions prove ownership; a role/name alone does not.
        users = db.scalars(select(Run.initiated_by_id).join(DatasetVersion, Run.dataset_version_id == DatasetVersion.id)
            .where(DatasetVersion.dataset_id == dataset.id, Run.organization_id == dataset.organization_id,
                Run.initiated_by_type == "USER").order_by(Run.created_at.desc()).limit(100)).all()
        responsible = next((valid for identity in users
                            if (valid := _authorized_candidate(db, dataset, identity))), None)
    automatic = AutomaticMonitor(organization_id=dataset.organization_id, dataset_id=dataset.id,
        monitor_root_id=monitor.id, responsible_user_id=responsible, enabled=enabled, next_review_at=utcnow())
    db.add(automatic)
    db.flush()
    record_revision(db, automatic, responsible)
    audit(db, "SENTINEL_AUTOMATIC_REGISTERED", "configuration", monitor.id,
        "Monitor por dataset registrado sin modificar revisiones existentes", "Sistema", dataset.organization_id,
        {"dataset_id": dataset.id, "automatic_monitor_id": automatic.id,
         "responsible_user_id": responsible, "enabled": enabled})
    return automatic


def record_revision(db: Session, automatic: AutomaticMonitor, actor_id: str | None) -> None:
    db.add(AutomaticMonitorRevision(organization_id=automatic.organization_id, automatic_monitor_id=automatic.id,
        version=automatic.version, actor_id=actor_id, settings={"enabled": automatic.enabled,
            "responsible_user_id": automatic.responsible_user_id,
            "review_interval_seconds": automatic.review_interval_seconds, "next_review_at": iso(automatic.next_review_at)}))


def backfill_automatic_monitors(db: Session) -> int:
    datasets = db.scalars(select(Dataset).where(~Dataset.id.in_(select(AutomaticMonitor.dataset_id)))).all()
    for dataset in datasets:
        ensure_automatic_monitor(db, dataset)
    return len(datasets)


def authorize_automatic_run(db: Session, run: Run) -> None:
    if run.module != "sentinel":
        return
    metadata = (run.execution_plan or {}).get("sentinel_automatic") or (run.execution_plan or {}).get("schedule")
    if not metadata:
        return
    responsible = authorize_responsible(db, metadata.get("responsible_user_id"), run.organization_id)
    source = db.get(DatasetVersion, run.dataset_version_id)
    if source is None or source.profile_status != "READY":
        raise ScheduleError(412, "DATASET_VERSION_NOT_COMPLETE", "La publicación no tiene un perfil completo.")
    try:
        authorize_dataset_content(db, responsible, source)
    except OperationError as error:
        raise ScheduleError(error.status, error.code, error.message) from None


def consume_dataset_event(db: Session, event) -> None:
    if event.event_type != "DATASET_VERSION_PUBLISHED" or event.payload.get("publication_status") != "COMPLETE":
        return
    source = db.get(DatasetVersion, event.payload.get("dataset_version_id"))
    if source is None or source.organization_id != event.organization_id or source.profile_status != "READY":
        return
    dataset = db.get(Dataset, source.dataset_id)
    if dataset is None:
        return
    automatic = ensure_automatic_monitor(db, dataset, event.payload.get("responsible_user_id"))
    # Assign only an unowned identity from a provable publication executor.
    if automatic.responsible_user_id is None:
        responsible = _authorized_candidate(db, dataset, event.payload.get("responsible_user_id"))
        if responsible:
            automatic.responsible_user_id = responsible
            automatic.version += 1
            automatic.updated_at = utcnow()
            record_revision(db, automatic, responsible)
    dispatch_dataset_version(db, source, event_id=event.id)


def dispatch_dataset_version(db: Session, source: DatasetVersion, event_id: str | None = None) -> SentinelVersionEvaluation | None:
    from .services import enqueue

    if source.profile_status != "READY" or (source.profile or {}).get("row_count") != source.row_count:
        return None
    dataset = db.get(Dataset, source.dataset_id)
    if dataset is None:
        return None
    automatic = ensure_automatic_monitor(db, dataset)
    db.scalar(select(AutomaticMonitor).where(AutomaticMonitor.id == automatic.id).with_for_update())
    monitor = current_monitor(db, automatic)
    existing = db.scalar(select(SentinelVersionEvaluation).where(
        SentinelVersionEvaluation.automatic_monitor_id == automatic.id,
        SentinelVersionEvaluation.dataset_version_id == source.id))
    if existing:
        return existing
    evaluation = SentinelVersionEvaluation(organization_id=source.organization_id,
        automatic_monitor_id=automatic.id, dataset_version_id=source.id, monitor_id=monitor.id, event_id=event_id)
    db.add(evaluation)
    db.flush()
    if not automatic.enabled:
        evaluation.status, evaluation.reason_code = "PAUSED", "AUTOMATIC_MONITOR_PAUSED"
        return evaluation
    schedules = db.scalars(select(MonitorSchedule).where(MonitorSchedule.monitor_id.in_(monitor_family(db, monitor)))).all()
    if any(not schedule.enabled for schedule in schedules):
        evaluation.status, evaluation.reason_code = "PAUSED", "EXPLICIT_SCHEDULE_PAUSE"
        return evaluation
    try:
        user = authorize_responsible(db, automatic.responsible_user_id, automatic.organization_id)
        authorize_dataset_content(db, user, source)
        run = enqueue(db, monitor, source, None, Actor("USER", user.id, user.name))
        run.execution_plan = {**run.execution_plan, "sentinel_automatic": {
            "automatic_monitor_id": automatic.id, "automatic_revision": automatic.version,
            "evaluation_id": evaluation.id, "event_id": event_id,
            "responsible_user_id": user.id, "origin": "DATASET_VERSION_PUBLISHED",
            "dataset_version_id": source.id, "configuration_id": monitor.id}}
        evaluation.run_id = run.id
        evaluation.status = "FAILED_PRECONDITION" if run.status == "FAILED_PRECONDITION" else "ENQUEUED"
        evaluation.reason_code = run.error if run.status == "FAILED_PRECONDITION" else None
    except (ScheduleError, OperationError) as error:
        evaluation.status, evaluation.reason_code = "BLOCKED", error.code
    return evaluation


def dispatch_daily(db: Session, now=None, *, limit: int = 100) -> int:
    """Review freshness/state from metadata; never enqueue or scan canonical data."""
    instant = aware(now or utcnow())
    due = db.scalars(select(AutomaticMonitor).where(AutomaticMonitor.enabled.is_(True),
        AutomaticMonitor.next_review_at <= instant).order_by(AutomaticMonitor.next_review_at, AutomaticMonitor.id)
        .limit(limit)).all()
    count = 0
    for automatic in due:
        cursor = automatic.next_review_at
        missed = max(0, int((instant - aware(cursor)).total_seconds()) // automatic.review_interval_seconds)
        planned = aware(cursor) + timedelta(seconds=missed * automatic.review_interval_seconds)
        claimed = db.execute(update(AutomaticMonitor).where(AutomaticMonitor.id == automatic.id,
            AutomaticMonitor.version == automatic.version, AutomaticMonitor.next_review_at == cursor,
            AutomaticMonitor.enabled.is_(True)).values(next_review_at=planned + timedelta(seconds=automatic.review_interval_seconds))
            .execution_options(synchronize_session=False))
        if cast(CursorResult, claimed).rowcount != 1:
            continue
        if db.scalar(select(SentinelLightReview.id).where(SentinelLightReview.automatic_monitor_id == automatic.id,
            SentinelLightReview.planned_at == planned)):
            db.expire(automatic)
            continue
        monitor = current_monitor(db, automatic)
        source = db.scalar(select(DatasetVersion).where(DatasetVersion.dataset_id == automatic.dataset_id,
            DatasetVersion.profile_status == "READY").order_by(DatasetVersion.version.desc()).limit(1))
        review = SentinelLightReview(organization_id=automatic.organization_id, automatic_monitor_id=automatic.id,
            dataset_version_id=source.id if source else None, planned_at=planned, observed_at=instant,
            status="WAITING_FIRST_VERSION", metrics={"mode": "METADATA_ONLY", "scans": 0})
        paused = db.scalar(select(MonitorSchedule.id).where(MonitorSchedule.monitor_id.in_(monitor_family(db, monitor)),
                                                           MonitorSchedule.enabled.is_(False)).limit(1))
        if paused:
            review.status, review.reason_code = "PAUSED", "EXPLICIT_SCHEDULE_PAUSE"
            db.add(review)
            db.expire(automatic)
            count += 1
            continue
        try:
            user = authorize_responsible(db, automatic.responsible_user_id, automatic.organization_id)
            if source:
                authorize_dataset_content(db, user, source)
                age = max(0, (instant - aware(source.created_at)).total_seconds() / 3600)
                maximum = monitor.config.get("max_age_hours", 48)
                latest_run = db.scalar(select(Run).where(Run.config_id.in_(monitor_family(db, monitor)),
                    Run.dataset_version_id == source.id).order_by(Run.created_at.desc()).limit(1))
                review.status = "FRESH" if age <= maximum else "STALE"
                review.metrics = {"mode": "METADATA_ONLY", "scans": 0, "age_hours": age,
                    "max_age_hours": maximum, "dataset_version_id": source.id,
                    "last_run_id": latest_run.id if latest_run else None,
                    "last_run_status": latest_run.status if latest_run else None,
                    "last_run_decision": latest_run.decision if latest_run else None}
        except (ScheduleError, OperationError) as error:
            review.status, review.reason_code = "BLOCKED", error.code
        db.add(review)
        db.expire(automatic)
        count += 1
    db.flush()
    return count


def automatic_dto(db: Session, automatic: AutomaticMonitor) -> dict:
    source = db.scalar(select(DatasetVersion).where(DatasetVersion.dataset_id == automatic.dataset_id,
        DatasetVersion.profile_status == "READY").order_by(DatasetVersion.version.desc()).limit(1))
    reviews = db.scalars(select(SentinelLightReview).where(SentinelLightReview.automatic_monitor_id == automatic.id)
        .order_by(SentinelLightReview.observed_at.desc()).limit(20)).all()
    return {"id": automatic.id, "dataset_id": automatic.dataset_id,
        "monitor_id": current_monitor(db, automatic).id, "monitor_root_id": automatic.monitor_root_id,
        "version": automatic.version, "enabled": automatic.enabled,
        "responsible_user_id": automatic.responsible_user_id,
        "requires_executor_assignment": automatic.responsible_user_id is None,
        "review_interval_seconds": automatic.review_interval_seconds,
        "next_review_at": iso(automatic.next_review_at), "new_version_policy": "EACH_COMPLETE_VERSION",
        "state": "PAUSED" if not automatic.enabled else "WAITING_FIRST_VERSION" if not source else
                 "EXECUTOR_ASSIGNMENT_REQUIRED" if not automatic.responsible_user_id else "ACTIVE",
        "latest_dataset_version_id": source.id if source else None,
        "reviews": [{"id": review.id, "dataset_version_id": review.dataset_version_id,
            "planned_at": iso(review.planned_at), "observed_at": iso(review.observed_at),
            "status": review.status, "reason_code": review.reason_code, "metrics": review.metrics} for review in reviews]}
