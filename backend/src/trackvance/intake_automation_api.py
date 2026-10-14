"""Scoped Intake schedule, occurrence and reviewed logical deletion endpoints."""

from fastapi import APIRouter, Depends, Query, Request
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .automation import AutomationError
from .db import get_db
from .intake_automation import (
    IntakeScheduleSettings,
    contract_root,
    delete_contract,
    deletion_impact,
    occurrence_dto,
    save_schedule,
    schedule_dto,
    schedule_for,
)
from .intake_automation_models import IntakeOccurrence
from .models import Configuration, User

router = APIRouter(prefix="/api/v1/intake", tags=["Intake automation and lifecycle"])


class ScheduleBody(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    settings: IntakeScheduleSettings
    enabled: bool = True
    responsible_user_id: str | None = None
    expected_version: int | None = Field(default=None, ge=1)


class DeleteBody(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    impact_token: str = Field(min_length=64, max_length=64)
    note: str = Field(min_length=3, max_length=2000)


def current_user(request: Request) -> User:
    return request.state.user


def owned_contract(db, identifier, user):
    config = db.scalar(select(Configuration).where(Configuration.id == identifier,
        Configuration.organization_id == user.organization_id, Configuration.module == "intake"))
    if config is None:
        raise AutomationError(404, "NOT_FOUND", "No se encontró el contrato Intake.")
    return config


@router.get("/contracts/{configuration_id}")
def historical_contract(configuration_id: str, db: Session = Depends(get_db), user: User = Depends(current_user)):
    from .services import config_dto
    return config_dto(db, owned_contract(db, configuration_id, user))


@router.get("/contracts/{configuration_id}/schedule")
def get_schedule(configuration_id: str, db: Session = Depends(get_db), user: User = Depends(current_user)):
    schedule = schedule_for(db, owned_contract(db, configuration_id, user))
    return schedule_dto(db, schedule) if schedule else None


@router.post("/contracts/{configuration_id}/schedule")
def update_schedule(configuration_id: str, body: ScheduleBody, db: Session = Depends(get_db), user: User = Depends(current_user)):
    config = owned_contract(db, configuration_id, user)
    schedule = save_schedule(db, config, user, body.settings, enabled=body.enabled,
        responsible_user_id=body.responsible_user_id, expected_version=body.expected_version)
    db.commit()
    return schedule_dto(db, schedule)


@router.get("/contracts/{configuration_id}/occurrences")
def get_occurrences(configuration_id: str, offset: int = Query(default=0, ge=0),
                    limit: int = Query(default=25, ge=1, le=100),
                    db: Session = Depends(get_db), user: User = Depends(current_user)):
    config = owned_contract(db, configuration_id, user)
    schedule = schedule_for(db, contract_root(db, config))
    if not schedule:
        return {"items": [], "total": 0}
    query = select(IntakeOccurrence).where(IntakeOccurrence.schedule_id == schedule.id,
        IntakeOccurrence.organization_id == user.organization_id)
    items = db.scalars(query.order_by(IntakeOccurrence.planned_at.desc()).offset(offset).limit(limit)).all()
    total = db.scalar(select(func.count()).select_from(query.subquery()))
    return {"items": [occurrence_dto(db, item) for item in items], "total": total}


@router.get("/contracts/{configuration_id}/deletion-impact")
def get_impact(configuration_id: str, db: Session = Depends(get_db), user: User = Depends(current_user)):
    return deletion_impact(db, owned_contract(db, configuration_id, user))


@router.delete("/contracts/{configuration_id}")
def logical_delete(configuration_id: str, body: DeleteBody, db: Session = Depends(get_db), user: User = Depends(current_user)):
    if not body.note.strip():
        raise AutomationError(422, "REASON_REQUIRED", "Describe el motivo de baja lógica.")
    result = delete_contract(db, owned_contract(db, configuration_id, user), user,
                            impact_token=body.impact_token, note=body.note)
    db.commit()
    return result
