"""Personal inbox; current resource permissions also constrain unread counts."""

from typing import Literal

from fastapi import APIRouter, Depends, Query, Request
from pydantic import BaseModel
from sqlalchemy import and_, exists, func, or_, select, update
from sqlalchemy.orm import Session

from .automation import AutomationError
from .automation_models import DeliveryOccurrence, InternalNotification, OutboxEvent
from .db import get_db, iso, utcnow
from .models import MonitorOccurrence, Run, User
from .permissions import effective_permissions

router = APIRouter(prefix="/api/v1/notifications", tags=["Internal notifications"])


class NotificationDiagnostic(BaseModel):
    code: str
    message: str
    details: dict | None
    reference: str | None


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
    error: NotificationDiagnostic | None = None


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


def notification_dto(item, event: OutboxEvent | None = None):
    error = None
    if (item.resource_type == "ACQUISITION" and event is not None
            and event.organization_id == item.organization_id and event.module == "acquisition"
            and event.aggregate_type == "ACQUISITION" and event.aggregate_id == item.resource_id):
        payload = event.payload or {}
        # Only new events carry a public diagnostic. Historical descriptions
        # cannot establish a code or cause and therefore remain error=null.
        if (all(key in payload for key in ("error_code", "error_message", "error_details", "error_reference"))
                and isinstance(payload["error_code"], str) and isinstance(payload["error_message"], str)):
            error = {"code": payload["error_code"], "message": payload["error_message"],
                     "details": payload["error_details"], "reference": payload["error_reference"]}
    return {"id": item.id, "module": item.module, "origin": item.origin,
        "status": item.status, "decision": item.decision, "description": item.description,
        "resource_type": item.resource_type, "resource_id": item.resource_id,
        "detail_url": item.detail_url, "created_at": iso(item.created_at), "read_at": iso(item.read_at), "error": error}


def notification_event(db: Session, item: InternalNotification) -> OutboxEvent | None:
    if item.resource_type != "ACQUISITION":
        return None
    return db.scalar(select(OutboxEvent).where(OutboxEvent.id == item.event_id,
                                             OutboxEvent.organization_id == item.organization_id))


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
    event_ids = {item.event_id for item in items if item.resource_type == "ACQUISITION"}
    event_map = {event.id: event for event in db.scalars(select(OutboxEvent).where(
        OutboxEvent.id.in_(event_ids), OutboxEvent.organization_id == user.organization_id)).all()} if event_ids else {}
    return {"items": [notification_dto(item, event_map.get(item.event_id)) for item in items], "total": total}


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
    identifiers = visible_query(db, user).where(
        InternalNotification.id == notification_id).with_only_columns(InternalNotification.id)
    item = db.scalar(update(InternalNotification).where(
        InternalNotification.id.in_(identifiers))
        .values(read_at=func.coalesce(InternalNotification.read_at, utcnow()))
        .returning(InternalNotification).execution_options(populate_existing=True))
    if item is None:
        raise AutomationError(404, "NOT_FOUND", "No se encontró la notificación.")
    db.commit()
    return notification_dto(item, notification_event(db, item))


@router.post("/inbox/{notification_id}/unread", response_model=NotificationResponse)
def unread_notification(notification_id: str, db: Session = Depends(get_db), user: User = Depends(current_user)):
    identifiers = visible_query(db, user).where(
        InternalNotification.id == notification_id).with_only_columns(InternalNotification.id)
    # An explicit setter must issue SQL even when a cached ORM entity is already
    # null: a concurrent /read may have changed the persisted value meanwhile.
    item = db.scalar(update(InternalNotification).where(
        InternalNotification.id.in_(identifiers)).values(read_at=None)
        .returning(InternalNotification).execution_options(populate_existing=True))
    if item is None:
        raise AutomationError(404, "NOT_FOUND", "No se encontró la notificación.")
    db.commit()
    return notification_dto(item, notification_event(db, item))
