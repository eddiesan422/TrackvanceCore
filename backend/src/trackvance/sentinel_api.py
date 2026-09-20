"""Scheduled monitoring and compatible metric series over the existing local runtime."""

from datetime import datetime
from decimal import Decimal, InvalidOperation
from math import isfinite

from fastapi import APIRouter, Depends, Query, Request
from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import select
from sqlalchemy.orm import Session

from .db import get_db, iso
from .models import (
    Configuration,
    Finding,
    MonitorOccurrence,
    MonitorSchedule,
    Run,
    SentinelMetricHistory,
    User,
)
from .permissions import permissions_for
from .scheduler import ScheduleError, occurrence_dto, save_schedule, schedule_dto
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
    return monitor


class ScheduleBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    interval_seconds: int = Field(ge=60, le=2678400, strict=True)
    enabled: bool = True
    starts_at: datetime | None = None
    expected_version: int | None = Field(default=None, ge=1, strict=True)

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
    if body.enabled and "runs:execute" not in permissions_for(user.role):
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
