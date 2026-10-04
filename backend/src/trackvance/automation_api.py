"""Scoped API for versioned Delivery automation and deliberate target resumption."""

import hashlib
import json

from fastapi import APIRouter, Depends, Query, Request
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .automation import (
    AutomationError,
    AutomationSettings,
    automation_dto,
    dispatch_occurrence,
    occurrence_dto,
    release_unknown_target,
    revision_for,
    save_automation,
)
from .automation_models import DeliveryAutomation, DeliveryOccurrence
from .db import get_db, utcnow
from .models import Run, User

router = APIRouter(prefix="/api/v1/delivery", tags=["Delivery automation"])


class AutomationBody(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    configuration_id: str = Field(min_length=1, max_length=64)
    name: str = Field(min_length=1, max_length=160)
    enabled: bool = True
    responsible_user_id: str | None = None
    settings: AutomationSettings
    expected_version: int | None = Field(default=None, ge=1)


class DispatchBody(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    request_key: str = Field(min_length=1, max_length=100)
    repeat: bool = False
    source_run_id: str | None = None


class TargetResumeBody(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    review_id: str = Field(min_length=1, max_length=64)
    note: str = Field(min_length=3, max_length=2000)


class AutomationResponse(BaseModel):
    id: str
    name: str
    version: int
    enabled: bool
    responsible_user_id: str
    responsible_name: str | None
    configuration_id: str
    automation_version_id: str
    settings: AutomationSettings
    next_run_at: str | None
    created_at: str
    updated_at: str
    misfire_policy: str
    overlap_policy: str


class AutomationCollection(BaseModel):
    items: list[AutomationResponse]
    total: int


class OccurrenceResponse(BaseModel):
    id: str
    automation_id: str
    automation_version_id: str
    origin: str
    planned_at: str
    dispatched_at: str
    source_run_id: str | None
    dataset_version_id: str | None
    run_id: str | None
    status: str
    decision: str | None
    reason_code: str | None
    started_at: str | None
    finished_at: str | None
    coalesced_intervals: int


class OccurrenceCollection(BaseModel):
    items: list[OccurrenceResponse]
    total: int


def current_user(request: Request) -> User:
    return request.state.user


def owned_automation(db, user, identifier, *, lock=False):
    query = select(DeliveryAutomation).where(DeliveryAutomation.id == identifier,
                                             DeliveryAutomation.organization_id == user.organization_id)
    result = db.scalar(query.with_for_update() if lock else query)
    if result is None:
        raise AutomationError(404, "NOT_FOUND", "No se encontró la automatización.")
    return result


@router.get("/automations", response_model=AutomationCollection)
def list_automations(offset: int = Query(default=0, ge=0), limit: int = Query(default=50, ge=1, le=100),
                     db: Session = Depends(get_db), user: User = Depends(current_user)):
    query = select(DeliveryAutomation).where(DeliveryAutomation.organization_id == user.organization_id)
    items = db.scalars(query.order_by(DeliveryAutomation.created_at.desc()).offset(offset).limit(limit)).all()
    total = db.scalar(select(func.count()).select_from(query.subquery()))
    return {"items": [automation_dto(db, item) for item in items], "total": total}


@router.post("/automations", status_code=201, response_model=AutomationResponse)
def create_automation(body: AutomationBody, db: Session = Depends(get_db), user: User = Depends(current_user)):
    item = save_automation(db, user, body.configuration_id, body.name, body.settings,
        enabled=body.enabled, responsible_user_id=body.responsible_user_id)
    db.commit()
    return automation_dto(db, item)


@router.get("/automations/{automation_id}", response_model=AutomationResponse)
def automation_detail(automation_id: str, db: Session = Depends(get_db), user: User = Depends(current_user)):
    return automation_dto(db, owned_automation(db, user, automation_id))


@router.post("/automations/{automation_id}/versions", status_code=201, response_model=AutomationResponse)
def automation_version(automation_id: str, body: AutomationBody,
                       db: Session = Depends(get_db), user: User = Depends(current_user)):
    item = save_automation(db, user, body.configuration_id, body.name, body.settings,
        enabled=body.enabled, responsible_user_id=body.responsible_user_id,
        automation=owned_automation(db, user, automation_id, lock=True), expected_version=body.expected_version)
    db.commit()
    return automation_dto(db, item)


@router.get("/automations/{automation_id}/occurrences", response_model=OccurrenceCollection)
def automation_occurrences(automation_id: str, offset: int = Query(default=0, ge=0),
                           limit: int = Query(default=50, ge=1, le=100),
                           db: Session = Depends(get_db), user: User = Depends(current_user)):
    owned_automation(db, user, automation_id)
    query = select(DeliveryOccurrence).where(DeliveryOccurrence.automation_id == automation_id,
                                             DeliveryOccurrence.organization_id == user.organization_id)
    items = db.scalars(query.order_by(DeliveryOccurrence.planned_at.desc()).offset(offset).limit(limit)).all()
    total = db.scalar(select(func.count()).select_from(query.subquery()))
    return {"items": [occurrence_dto(db, item) for item in items], "total": total}


@router.post("/automations/{automation_id}/dispatch", status_code=202, response_model=OccurrenceResponse)
def manual_dispatch(automation_id: str, body: DispatchBody,
                    db: Session = Depends(get_db), user: User = Depends(current_user)):
    item = owned_automation(db, user, automation_id, lock=True)
    source_run = db.get(Run, body.source_run_id) if body.source_run_id else None
    if source_run and source_run.organization_id != user.organization_id:
        raise AutomationError(404, "NOT_FOUND", "No se encontró la ejecución de origen.")
    trigger_key = f"manual:{body.request_key}"
    request_hash = hashlib.sha256(json.dumps(body.model_dump(exclude={"request_key"}), sort_keys=True,
                                             separators=(",", ":")).encode()).hexdigest()
    existing = db.scalar(select(DeliveryOccurrence).where(DeliveryOccurrence.automation_id == item.id,
                                                          DeliveryOccurrence.trigger_key == trigger_key))
    if existing:
        if existing.request_hash != request_hash:
            raise AutomationError(409, "IDEMPOTENCY_CONFLICT", "La solicitud ya fue registrada con otros parámetros.")
        return occurrence_dto(db, existing)
    occurrence = dispatch_occurrence(db, item, revision_for(db, item), trigger_key,
                                     utcnow(), origin="MANUAL", source_run=source_run, repeat=body.repeat,
                                     request_hash=request_hash)
    db.commit()
    return occurrence_dto(db, occurrence)


@router.post("/runs/{run_id}/resume-target")
def resume_target(run_id: str, body: TargetResumeBody,
                  db: Session = Depends(get_db), user: User = Depends(current_user)):
    if not body.note.strip():
        raise AutomationError(422, "REASON_REQUIRED", "Describe la decisión operativa.")
    release_unknown_target(db, user, run_id, body.review_id, body.note)
    db.commit()
    return {"status": "RESUMED", "run_id": run_id, "historical_status": "UNKNOWN"}
