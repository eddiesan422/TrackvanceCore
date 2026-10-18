"""Personal inbox; current resource permissions also constrain unread counts."""

from datetime import datetime
from typing import Literal

from fastapi import APIRouter, Depends, Query, Request
from pydantic import BaseModel, Field
from sqlalchemy import and_, exists, func, or_, select, update
from sqlalchemy.orm import Session

from .acquisition_schedule_models import AcquisitionOccurrence
from .automation import AutomationError
from .automation_models import DeliveryOccurrence, InternalNotification, OutboxEvent
from .db import get_db, iso, utcnow
from .intake_automation_models import IntakeOccurrence
from .models import MonitorOccurrence, Run, User
from .operation_processes import notification_process
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
    groups: list[dict] | None = None
    watermark: str | None = None


class UnreadResponse(BaseModel):
    unread_count: int


class ReadResponse(BaseModel):
    status: str
    marked_count: int = 0


class ReadSnapshot(BaseModel):
    # Exact IDs preserve the rendered scope, including delayed events whose
    # event timestamp predates their arrival in the personal inbox.
    ids: list[str] | None = Field(default=None, max_length=1000)


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
    if "intake:read" in grants:
        resources.append(and_(InternalNotification.resource_type == "INTAKE_OCCURRENCE",
            exists(select(IntakeOccurrence.id).where(IntakeOccurrence.id == InternalNotification.resource_id,
                IntakeOccurrence.organization_id == user.organization_id))))
    if "datasets:read" in grants:
        from .acquisition_models import AcquisitionRun

        resources.append(and_(InternalNotification.resource_type == "ACQUISITION",
            exists(select(AcquisitionRun.id).where(AcquisitionRun.id == InternalNotification.resource_id,
                                                   AcquisitionRun.organization_id == user.organization_id))))
        resources.append(and_(InternalNotification.resource_type == "ACQUISITION_OCCURRENCE",
            exists(select(AcquisitionOccurrence.id).where(AcquisitionOccurrence.id == InternalNotification.resource_id,
                AcquisitionOccurrence.organization_id == user.organization_id))))
    return select(InternalNotification).where(
        InternalNotification.organization_id == user.organization_id,
        InternalNotification.recipient_user_id == user.id,
        or_(*resources))


def authorized_notification(db, user, item):
    """Resource policy is identical for inbox rows, reading and bell counts."""
    from .acquisition_models import AcquisitionRun
    from .acquisition_schedule_models import AcquisitionSourceConfiguration
    from .governance import authorize_dataset, authorize_run_content
    from .models import Configuration, DatasetVersion
    from .operations_common import OperationError
    try:
        if item.resource_type == "RUN":
            authorize_run_content(db, user, db.get(Run, item.resource_id))
        elif item.resource_type == "ACQUISITION":
            run = db.get(AcquisitionRun, item.resource_id)
            if run.dataset_id:
                authorize_dataset(db, user, run.dataset_id)
        elif item.resource_type == "ACQUISITION_OCCURRENCE":
            occurrence = db.get(AcquisitionOccurrence, item.resource_id)
            config = db.get(AcquisitionSourceConfiguration, occurrence.configuration_id)
            authorize_dataset(db, user, config.dataset_id)
        elif item.resource_type in {"INTAKE_OCCURRENCE", "MONITOR_OCCURRENCE", "DELIVERY_OCCURRENCE"}:
            model = {"INTAKE_OCCURRENCE": IntakeOccurrence, "MONITOR_OCCURRENCE": MonitorOccurrence,
                "DELIVERY_OCCURRENCE": DeliveryOccurrence}[item.resource_type]
            occurrence = db.get(model, item.resource_id)
            if occurrence.run_id:
                authorize_run_content(db, user, db.get(Run, occurrence.run_id))
            elif occurrence.dataset_version_id:
                version = db.get(DatasetVersion, occurrence.dataset_version_id)
                if version is None or version.organization_id != user.organization_id:
                    return False
                authorize_dataset(db, user, version.dataset_id)
            elif item.resource_type == "INTAKE_OCCURRENCE":
                config = db.get(Configuration, occurrence.configuration_id)
                authorize_dataset(db, user, config.dataset_id)
        return True
    except OperationError:
        return False


def accessible_items(db, user, query):
    # Iterate metadata in server batches, never load dataset populations. This
    # also keeps historical resource permissions authoritative for the counter.
    authorized = {}
    for item in db.scalars(query.execution_options(yield_per=100)):
        key = (item.resource_type, item.resource_id)
        if key not in authorized:
            authorized[key] = authorized_notification(db, user, item)
        if authorized[key]:
            yield item


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
          decision: str | None = None, from_date: datetime | None = None, until_date: datetime | None = None,
          grouped: bool = False, process: str | None = None,
          notification_offset: int = Query(default=0, ge=0),
          db: Session = Depends(get_db), user: User = Depends(current_user)):
    query = visible_query(db, user)
    selected_state = read_state or ("UNREAD" if unread else "ALL")
    if selected_state == "UNREAD":
        query = query.where(InternalNotification.read_at.is_(None))
    elif selected_state == "READ":
        query = query.where(InternalNotification.read_at.is_not(None))
    for column, value in ((InternalNotification.module, module), (InternalNotification.origin, origin),
                          (InternalNotification.status, status), (InternalNotification.decision, decision)):
        if value:
            query = query.where(column == value)
    if from_date:
        query = query.where(InternalNotification.created_at >= from_date)
    if until_date:
        query = query.where(InternalNotification.created_at <= until_date)
    ordered = query.order_by(InternalNotification.created_at.desc(), InternalNotification.id.desc())
    if grouped:
        from .operation_tracking import group_item, process_item
        grants = set(effective_permissions(db, user))
        groups: dict[str, dict] = {}
        seen_processes: set[str] = set()
        for item in accessible_items(db, user, ordered):
            process_id = notification_process(db, item)
            anchor = process_item(db, user, process_id, grants)
            if anchor is None:
                process_id = f"{item.resource_type.lower()}:{item.resource_id}"
            if process and process_id != process:
                continue
            if process_id not in seen_processes:
                index = len(seen_processes)
                seen_processes.add(process_id)
                if offset <= index < offset + limit:
                    groups[process_id] = {"process_id": process_id, "operation": group_item(db, user, anchor, grants) if anchor else None,
                        "items": [], "unread_count": 0, "notification_count": 0, "notification_offset": notification_offset}
            if process_id in groups:
                group = groups[process_id]
                index = group["notification_count"]
                group["notification_count"] += 1
                if notification_offset <= index < notification_offset + 25:
                    group["items"].append(notification_dto(item, notification_event(db, item)))
        # Read state describes the entire accessible personal process, even
        # when the visible filter only shows one already-read stage.
        unread_query = visible_query(db, user).where(InternalNotification.read_at.is_(None))
        for item in accessible_items(db, user, unread_query):
            process_id = notification_process(db, item)
            if process_id not in groups:
                process_id = f"{item.resource_type.lower()}:{item.resource_id}"
            if process_id in groups:
                groups[process_id]["unread_count"] += 1
        for group in groups.values():
            group["more_notifications"] = group["notification_count"] > notification_offset + 25
        return {"items": [], "groups": list(groups.values()), "total": len(seen_processes), "watermark": iso(utcnow())}
    total, items = 0, []
    for item in accessible_items(db, user, ordered):
        if process and notification_process(db, item) != process:
            continue
        if offset <= total < offset + limit:
            items.append(item)
        total += 1
    event_ids = {item.event_id for item in items if item.resource_type == "ACQUISITION"}
    event_map = {event.id: event for event in db.scalars(select(OutboxEvent).where(
        OutboxEvent.id.in_(event_ids), OutboxEvent.organization_id == user.organization_id)).all()} if event_ids else {}
    return {"items": [notification_dto(item, event_map.get(item.event_id)) for item in items], "total": total, "watermark": iso(utcnow())}


@router.get("/unread-count", response_model=UnreadResponse)
def unread_count(db: Session = Depends(get_db), user: User = Depends(current_user)):
    query = visible_query(db, user).where(InternalNotification.read_at.is_(None))
    return {"unread_count": sum(1 for _ in accessible_items(db, user, query))}


@router.post("/inbox/read-all", response_model=ReadResponse)
def read_all(snapshot: ReadSnapshot | None = None, db: Session = Depends(get_db), user: User = Depends(current_user)):
    query = visible_query(db, user).where(InternalNotification.read_at.is_(None))
    if snapshot is not None and snapshot.ids is not None:
        query = query.where(InternalNotification.id.in_(snapshot.ids))
    # Materialize the operation boundary before issuing the update. A new
    # personal event can never enter this set during the update or commit.
    identifiers = [item.id for item in accessible_items(db, user, query)]
    db.execute(update(InternalNotification).where(InternalNotification.id.in_(identifiers),
        InternalNotification.read_at.is_(None)).values(read_at=utcnow()).execution_options(synchronize_session=False))
    db.commit()
    return {"status": "READ", "marked_count": len(identifiers)}


@router.post("/inbox/{notification_id}/read", response_model=NotificationResponse)
def read_notification(notification_id: str, db: Session = Depends(get_db), user: User = Depends(current_user)):
    candidate = db.scalar(visible_query(db, user).where(InternalNotification.id == notification_id))
    if candidate is None or not authorized_notification(db, user, candidate):
        raise AutomationError(404, "NOT_FOUND", "No se encontró la notificación.")
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
    candidate = db.scalar(visible_query(db, user).where(InternalNotification.id == notification_id))
    if candidate is None or not authorized_notification(db, user, candidate):
        raise AutomationError(404, "NOT_FOUND", "No se encontró la notificación.")
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
