"""Personal inbox; current resource permissions also constrain unread counts."""

from typing import Literal

from fastapi import APIRouter, Depends, Query, Request
from pydantic import BaseModel
from sqlalchemy import and_, exists, func, or_, select, update
from sqlalchemy.orm import Session

from .automation import AutomationError
from .automation_models import DeliveryOccurrence, InternalNotification
from .db import get_db, iso, utcnow
from .models import MonitorOccurrence, Run, User
from .permissions import effective_permissions

router = APIRouter(prefix="/api/v1/notifications", tags=["Internal notifications"])


class NotificationResponse(BaseModel):
    id: str
    module: str
    origin: str
    status: str
    decision: str | None
    description: str
    resource_type: str
    resource_id: str
    detail_url: str
    created_at: str
    read_at: str | None


class InboxResponse(BaseModel):
    items: list[NotificationResponse]
    total: int


class UnreadResponse(BaseModel):
    unread_count: int


class ReadResponse(BaseModel):
    status: str


def current_user(request: Request) -> User:
    return request.state.user


def visible_query(db: Session, user: User):
    grants = set(effective_permissions(db, user))
    modules = [module for module in ("intake", "recon", "sentinel", "DELIVERY")
               if f"{module.lower()}:read" in grants]
    resources = [and_(InternalNotification.resource_type == "RUN",
        InternalNotification.module.in_(modules), exists(select(Run.id).where(
            Run.id == InternalNotification.resource_id,
            Run.organization_id == user.organization_id,
            or_(Run.module == InternalNotification.module,
                and_(Run.module == "DELIVERY_PREFLIGHT", InternalNotification.module == "DELIVERY")))))]
    if "delivery:read" in grants:
        resources.append(and_(InternalNotification.resource_type == "DELIVERY_OCCURRENCE",
            exists(select(DeliveryOccurrence.id).where(
                DeliveryOccurrence.id == InternalNotification.resource_id,
                DeliveryOccurrence.organization_id == user.organization_id))))
    if "sentinel:read" in grants:
        resources.append(and_(InternalNotification.resource_type == "MONITOR_OCCURRENCE",
            exists(select(MonitorOccurrence.id).where(
                MonitorOccurrence.id == InternalNotification.resource_id,
                MonitorOccurrence.organization_id == user.organization_id))))
    if "datasets:read" in grants:
        from .acquisition_models import AcquisitionRun

        resources.append(and_(InternalNotification.resource_type == "ACQUISITION",
            exists(select(AcquisitionRun.id).where(AcquisitionRun.id == InternalNotification.resource_id,
                                                   AcquisitionRun.organization_id == user.organization_id))))
    return select(InternalNotification).where(
        InternalNotification.organization_id == user.organization_id,
        InternalNotification.recipient_user_id == user.id,
        or_(*resources))


def notification_dto(item):
    return {"id": item.id, "module": item.module, "origin": item.origin,
        "status": item.status, "decision": item.decision, "description": item.description,
        "resource_type": item.resource_type, "resource_id": item.resource_id,
        "detail_url": item.detail_url, "created_at": iso(item.created_at), "read_at": iso(item.read_at)}


@router.get("/inbox", response_model=InboxResponse)
def inbox(offset: int = Query(default=0, ge=0), limit: int = Query(default=25, ge=1, le=100),
          unread: bool = False, module: str | None = None, origin: str | None = None,
          status: str | None = None, read_state: Literal["ALL", "READ", "UNREAD"] | None = None,
          db: Session = Depends(get_db), user: User = Depends(current_user)):
    query = visible_query(db, user)
    selected_state = read_state or ("UNREAD" if unread else "ALL")
    if selected_state == "UNREAD":
        query = query.where(InternalNotification.read_at.is_(None))
    elif selected_state == "READ":
        query = query.where(InternalNotification.read_at.is_not(None))
    for column, value in ((InternalNotification.module, module), (InternalNotification.origin, origin),
                          (InternalNotification.status, status)):
        if value:
            query = query.where(column == value)
    total = db.scalar(select(func.count()).select_from(query.subquery()))
    items = db.scalars(query.order_by(InternalNotification.created_at.desc(), InternalNotification.id.desc())
                       .offset(offset).limit(limit)).all()
    return {"items": [notification_dto(item) for item in items], "total": total}


@router.get("/unread-count", response_model=UnreadResponse)
def unread_count(db: Session = Depends(get_db), user: User = Depends(current_user)):
    query = visible_query(db, user).where(InternalNotification.read_at.is_(None))
    return {"unread_count": db.scalar(select(func.count()).select_from(query.subquery()))}


@router.post("/inbox/read-all", response_model=ReadResponse)
def read_all(db: Session = Depends(get_db), user: User = Depends(current_user)):
    identifiers = visible_query(db, user).with_only_columns(InternalNotification.id)
    db.execute(update(InternalNotification).where(InternalNotification.id.in_(identifiers),
        InternalNotification.read_at.is_(None)).values(read_at=utcnow()).execution_options(synchronize_session=False))
    db.commit()
    return {"status": "READ"}


@router.post("/inbox/{notification_id}/read", response_model=NotificationResponse)
def read_notification(notification_id: str, db: Session = Depends(get_db), user: User = Depends(current_user)):
    item = db.scalar(visible_query(db, user).where(InternalNotification.id == notification_id))
    if item is None:
        raise AutomationError(404, "NOT_FOUND", "No se encontró la notificación.")
    item.read_at = item.read_at or utcnow()
    db.commit()
    return notification_dto(item)
