"""Scheduled monitoring and compatible metric series over the existing local runtime."""

from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation
from math import isfinite
from typing import cast

from fastapi import APIRouter, Depends, Query, Request
from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import or_, select, update
from sqlalchemy.engine import CursorResult
from sqlalchemy.orm import Session

from .db import get_db, iso, utcnow
from .governance import authorize_dataset, authorize_dataset_content, authorize_run_content
from .models import (
    Configuration,
    Dataset,
    DatasetVersion,
    Finding,
    MonitorOccurrence,
    MonitorSchedule,
    Run,
    SentinelMetricHistory,
    User,
)
from .permissions import effective_permissions
from .scheduler import (
    ScheduleError,
    authorize_responsible,
    occurrence_dto,
    save_schedule,
    schedule_dto,
)
from .sentinel_auto import automatic_dto, ensure_automatic_monitor, record_revision
from .sentinel_auto_models import AutomaticMonitor
from .services import finding_dto

router = APIRouter(prefix="/api/v1", tags=["Sentinel"])


def current_user(request: Request) -> User:
    return request.state.user


def owned_monitor(db: Session, monitor_id: str, user: User) -> Configuration:
    monitor = db.scalar(select(Configuration).where(
        Configuration.id == monitor_id, Configuration.organization_id == user.organization_id,
        Configuration.module == "sentinel",
    ))
    if monitor is None:
        raise ScheduleError(404, "MONITOR_NOT_FOUND", "No se encontró el monitor.")
    authorize_dataset(db, user, monitor.dataset_id, "MODULE")
    return monitor


class ScheduleBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    interval_seconds: int = Field(ge=60, le=2678400, strict=True)
    enabled: bool = True
    starts_at: datetime | None = None
    expected_version: int | None = Field(default=None, ge=1, strict=True)
    responsible_user_id: str | None = Field(default=None, max_length=64)

    @field_validator("starts_at")
    @classmethod
    def explicit_timezone(cls, value: datetime | None) -> datetime | None:
        if value is not None and value.tzinfo is None:
            raise ValueError("Indica la zona horaria de la primera ejecución.")
        return value


class ScheduleResponse(BaseModel):
    id: str
    monitor_id: str
    version: int
    schedule_version_id: str
    interval_seconds: int
    enabled: bool
    next_run_at: str
    starts_at: str
    updated_at: str
    source_policy: str
    misfire_policy: str
    overlap_policy: str
    responsible_user_id: str | None
    requires_executor_assignment: bool


class OccurrenceResponse(BaseModel):
    id: str
    schedule_id: str
    schedule_version_id: str
    monitor_id: str
    dataset_version_id: str | None
    run_id: str | None
    planned_at: str
    dispatched_at: str
    started_at: str | None
    finished_at: str | None
    status: str
    decision: str | None
    reason_code: str | None
    coalesced_intervals: int
    metrics: dict


class OccurrenceCollection(BaseModel):
    items: list[OccurrenceResponse]
    total: int


class MetricPointResponse(BaseModel):
    run_id: str
    configuration_id: str
    dataset_version_id: str
    observed_at: str
    value: float | None
    exact_value: str | None
    decision: str | None


class MetricSeriesResponse(BaseModel):
    metric_key: str
    dimensions: dict
    method: str
    metric_definition_version: int
    points: list[MetricPointResponse]


class MetricSeriesCollection(BaseModel):
    items: list[MetricSeriesResponse]
    total: int
    sample_count: int
    limit: int
    includes_configuration_versions: bool


@router.get("/monitors/{monitor_id}/schedule", response_model=ScheduleResponse | None)
def get_schedule(monitor_id: str, db: Session = Depends(get_db), user: User = Depends(current_user)):
    owned_monitor(db, monitor_id, user)
    schedule = db.scalar(select(MonitorSchedule).where(
        MonitorSchedule.monitor_id == monitor_id, MonitorSchedule.organization_id == user.organization_id,
    ))
    return schedule_dto(db, schedule) if schedule else None


@router.post("/monitors/{monitor_id}/schedule", response_model=ScheduleResponse)
def configure_schedule(monitor_id: str, body: ScheduleBody, db: Session = Depends(get_db), user: User = Depends(current_user)):
    monitor = owned_monitor(db, monitor_id, user)
    if body.enabled and "sentinel:execute" not in effective_permissions(db, user):
        raise ScheduleError(403, "FORBIDDEN", "No tienes permiso para programar ejecuciones.")
    schedule = save_schedule(db, monitor, user, **body.model_dump())
    db.commit()
    return schedule_dto(db, schedule)


@router.get("/monitors/{monitor_id}/occurrences", response_model=OccurrenceCollection)
def occurrences(monitor_id: str, limit: int = Query(default=50, ge=1, le=200), db: Session = Depends(get_db), user: User = Depends(current_user)):
    owned_monitor(db, monitor_id, user)
    rows = db.scalars(select(MonitorOccurrence).where(
        MonitorOccurrence.monitor_id == monitor_id, MonitorOccurrence.organization_id == user.organization_id,
    ).order_by(MonitorOccurrence.planned_at.desc(), MonitorOccurrence.id).limit(limit)).all()
    return {"items": [occurrence_dto(db, row) for row in rows], "total": len(rows)}


def monitor_family(db: Session, monitor: Configuration) -> set[str]:
    """Resolve only the explicitly versioned configuration chain in this organization."""
    configs = db.scalars(select(Configuration).where(
        Configuration.organization_id == monitor.organization_id, Configuration.module == "sentinel",
    )).all()
    family = {monitor.id}
    for _ in range(len(configs)):
        prior = len(family)
        for config in configs:
            if config.id in family and config.previous_version_id:
                family.add(config.previous_version_id)
            if config.previous_version_id in family:
                family.add(config.id)
        if len(family) == prior:
            break
    return family


@router.get("/monitors/{monitor_id}/series", response_model=MetricSeriesCollection)
def metric_series(
    monitor_id: str, include_versions: bool = True, limit: int = Query(default=500, ge=1, le=2000),
    db: Session = Depends(get_db), user: User = Depends(current_user),
):
    monitor = owned_monitor(db, monitor_id, user)
    family = monitor_family(db, monitor) if include_versions else {monitor.id}
    rows = db.execute(select(SentinelMetricHistory, Run).join(Run, Run.id == SentinelMetricHistory.run_id).where(
        SentinelMetricHistory.organization_id == user.organization_id,
        SentinelMetricHistory.monitor_id.in_(family), Run.organization_id == user.organization_id,
        Run.status == "SUCCESS",
    ).order_by(SentinelMetricHistory.observed_at.desc(), SentinelMetricHistory.id).limit(limit)).all()
    groups: dict[tuple, dict] = {}
    for metric, run in reversed(rows):
        # Method/version/dimensions are part of series identity; incompatible samples never merge.
        key = (metric.metric_key, metric.dimension_hash, metric.method, metric.metric_definition_version)
        group = groups.setdefault(key, {
            "metric_key": metric.metric_key, "dimensions": metric.dimensions,
            "method": metric.method, "metric_definition_version": metric.metric_definition_version,
            "points": [],
        })
        exact = str(metric.numeric_value) if metric.numeric_value is not None else None
        chart_value = None
        try:
            candidate = float(Decimal(exact)) if exact is not None else None
            if candidate is not None and isfinite(candidate):
                chart_value = candidate
        except (InvalidOperation, ValueError, OverflowError):
            pass
        group["points"].append({
            "run_id": run.id, "configuration_id": run.config_id,
            "dataset_version_id": run.dataset_version_id, "observed_at": iso(metric.observed_at),
            "value": chart_value, "exact_value": exact, "decision": run.decision,
        })
    return {"items": list(groups.values()), "total": len(groups), "sample_count": len(rows),
            "limit": limit, "includes_configuration_versions": include_versions}


@router.get("/monitors/{monitor_id}/alerts")
def monitor_alerts(monitor_id: str, limit: int = Query(default=50, ge=1, le=200), db: Session = Depends(get_db), user: User = Depends(current_user)):
    monitor = owned_monitor(db, monitor_id, user)
    family = monitor_family(db, monitor)
    rows = db.scalars(select(Finding).join(Run, Finding.run_id == Run.id).where(
        Finding.organization_id == user.organization_id, Run.organization_id == user.organization_id,
        Run.config_id.in_(family), Run.status == "SUCCESS",
    ).order_by(Finding.created_at.desc(), Finding.id).limit(limit)).all()
    return {"items": [finding_dto(db, row) for row in rows], "total": len(rows)}


class AutomaticBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    enabled: bool
    expected_version: int = Field(ge=1, strict=True)
    review_interval_seconds: int = Field(default=86400, ge=3600, le=2678400, strict=True)
    responsible_user_id: str | None = Field(default=None, max_length=64)


@router.get("/monitors/{monitor_id}/automatic")
def get_automatic(monitor_id: str, db: Session = Depends(get_db), user: User = Depends(current_user)):
    monitor = owned_monitor(db, monitor_id, user)
    automatic = db.scalar(select(AutomaticMonitor).where(AutomaticMonitor.dataset_id == monitor.dataset_id))
    # A dataset has one automatic family; additional personalized monitors keep their own schedules.
    if automatic is None:
        return None
    root = db.get(Configuration, automatic.monitor_root_id)
    if root is None or monitor.id not in monitor_family(db, root):
        return None
    return automatic_dto(db, automatic)


@router.post("/monitors/{monitor_id}/automatic")
def configure_automatic(monitor_id: str, body: AutomaticBody, db: Session = Depends(get_db), user: User = Depends(current_user)):
    from .services import audit

    monitor = owned_monitor(db, monitor_id, user)
    dataset = db.get(Dataset, monitor.dataset_id)
    if dataset is None:
        raise ScheduleError(409, "MONITOR_DATASET_MISSING", "El dataset del monitor ya no está disponible.")
    automatic = ensure_automatic_monitor(db, dataset)
    root = db.get(Configuration, automatic.monitor_root_id)
    if root is None or monitor.id not in monitor_family(db, root):
        raise ScheduleError(409, "NOT_AUTOMATIC_MONITOR", "Este monitor tiene su propia programación personalizada.")
    responsible_id = body.responsible_user_id or automatic.responsible_user_id or user.id
    if body.enabled:
        responsible = authorize_responsible(db, responsible_id, user.organization_id)
        authorize_dataset(db, responsible, dataset.id)
    instant = utcnow()
    changed = db.execute(update(AutomaticMonitor).where(AutomaticMonitor.id == automatic.id,
        AutomaticMonitor.version == body.expected_version).values(enabled=body.enabled,
        responsible_user_id=responsible_id, review_interval_seconds=body.review_interval_seconds,
        version=body.expected_version + 1, next_review_at=instant + timedelta(seconds=body.review_interval_seconds),
        updated_at=instant).execution_options(synchronize_session=False))
    if cast(CursorResult, changed).rowcount != 1:
        raise ScheduleError(409, "AUTOMATIC_MONITOR_CONFLICT", "La programación cambió. Actualiza e intenta nuevamente.")
    db.refresh(automatic)
    record_revision(db, automatic, user.id)
    audit(db, "SENTINEL_AUTOMATIC_UPDATED", "configuration", monitor.id,
        "Responsable, pausa y frecuencia de revisión actualizados", user.name, user.organization_id,
        {"automatic_monitor_id": automatic.id, "version": automatic.version, "enabled": automatic.enabled,
         "review_interval_seconds": automatic.review_interval_seconds, "responsible_user_id": responsible_id})
    db.commit()
    return automatic_dto(db, automatic)


def chart_number(value) -> float | None:
    try:
        candidate = float(Decimal(str(value)))
        return candidate if isfinite(candidate) else None
    except (InvalidOperation, ValueError, OverflowError, TypeError):
        return None


@router.get("/runs/{run_id}/visual-summary")
def visual_summary(run_id: str, db: Session = Depends(get_db), user: User = Depends(current_user)):
    """Read immutable execution evidence and compatible metadata only; never scan."""
    if "sentinel:read" not in effective_permissions(db, user):
        raise ScheduleError(403, "FORBIDDEN", "No tienes permiso para consultar Sentinel.")
    run = db.scalar(select(Run).where(Run.id == run_id, Run.organization_id == user.organization_id,
                                     Run.module == "sentinel"))
    if run is None:
        raise ScheduleError(404, "RUN_NOT_FOUND", "No se encontró la ejecución Sentinel.")
    authorize_run_content(db, user, run)
    source = db.get(DatasetVersion, run.dataset_version_id)
    if source is None:
        raise ScheduleError(409, "MONITOR_VERSION_MISSING", "La versión de esta ejecución no está disponible.")
    authorize_dataset_content(db, user, source, "MODULE")
    metrics = run.metrics or {}
    profile = metrics.get("profile_snapshot") or source.profile or {}
    monitor = db.get(Configuration, run.config_id)
    if monitor is None:
        raise ScheduleError(409, "MONITOR_CONFIGURATION_MISSING", "La revisión de esta ejecución no está disponible.")
    columns = [{"name": item["name"], "null_count": item.get("null_count"),
        "null_rate": item.get("null_rate"), "distinct_count": item.get("distinct_count"),
        "distinct_rate": item.get("distinct_rate"), "uniqueness_ratio": item.get("uniqueness_ratio"),
        "logical_type": item.get("logical_type"), "null_denominator": profile.get("row_count"),
        "distinct_denominator": profile.get("row_count", 0) - item["null_count"]
            if isinstance(item.get("null_count"), int) and isinstance(profile.get("row_count"), int) else None}
        for item in profile.get("columns", [])]
    records = db.scalars(select(SentinelMetricHistory).where(SentinelMetricHistory.run_id == run.id,
        SentinelMetricHistory.organization_id == user.organization_id)).all()
    exact = [{"metric_key": item.metric_key, "dimensions": item.dimensions,
        "exact_value": str(item.numeric_value) if item.numeric_value is not None else None,
        "value": chart_number(item.numeric_value), "method": item.method,
        "metric_definition_version": item.metric_definition_version} for item in records]
    history_rows = db.execute(select(SentinelMetricHistory, Run, DatasetVersion)
        .join(Run, Run.id == SentinelMetricHistory.run_id).join(DatasetVersion, DatasetVersion.id == Run.dataset_version_id)
        .where(SentinelMetricHistory.organization_id == user.organization_id,
            SentinelMetricHistory.monitor_id.in_(monitor_family(db, monitor)), Run.status == "SUCCESS",
            Run.organization_id == user.organization_id, DatasetVersion.schema_hash == source.schema_hash,
            DatasetVersion.version <= source.version,
            or_(Run.id == run.id, SentinelMetricHistory.observed_at <= (run.started_at or run.created_at)))
        .order_by(SentinelMetricHistory.observed_at.desc(), SentinelMetricHistory.id.desc()).limit(501)).all()
    evolution: dict[tuple, dict] = {}
    selected = {(item.metric_key, item.dimension_hash, item.method, item.metric_definition_version) for item in records}
    for item, sample_run, _version in reversed(history_rows[:500]):
        key = (item.metric_key, item.dimension_hash, item.method, item.metric_definition_version)
        if key not in selected:
            continue
        authorize_run_content(db, user, sample_run)
        group = evolution.setdefault(key, {"metric_key": item.metric_key, "dimensions": item.dimensions,
            "method": item.method, "metric_definition_version": item.metric_definition_version, "points": []})
        group["points"].append({"run_id": sample_run.id, "dataset_version_id": sample_run.dataset_version_id,
            "observed_at": iso(item.observed_at), "exact_value": str(item.numeric_value) if item.numeric_value is not None else None,
            "value": chart_number(item.numeric_value)})
    return {"run_id": run.id, "configuration_id": run.config_id, "dataset_version_id": source.id,
        "dataset_version": source.version, "status": run.status, "decision": run.decision,
        "observed_at": iso(run.started_at), "profile_method": profile.get("metric_method"),
        "metric_definition_version": profile.get("metric_definition_version"), "scans": 0,
        "indicators": {"row_count": profile.get("row_count"), "column_count": profile.get("column_count"),
            "duplicate_row_count": profile.get("duplicate_row_count"), "distinct_row_count": profile.get("distinct_row_count"),
            "duplicate_definition": profile.get("row_duplicate_definition"), "total_checks": metrics.get("total_checks"),
            "failed_checks": metrics.get("failed_checks"), "conforming_checks": metrics.get("conforming_checks")
                if "conforming_checks" in metrics else metrics["total_checks"] - metrics["failed_checks"]
                if "total_checks" in metrics and "failed_checks" in metrics else None,
            "valid_rows": None}, "columns": columns,
        "checks": metrics.get("checks", []), "exact_metrics": exact, "evolution": list(evolution.values()),
        "evolution_truncated": len(history_rows) > 500, "evolution_limit": 500,
        "notes": ["Los controles conformes no equivalen a filas válidas.",
                  "Las reglas pueden superponerse; no se suman sus fallos como filas distintas.",
                  "Los nulos y categorías solo incumplen los límites declarados."]}
