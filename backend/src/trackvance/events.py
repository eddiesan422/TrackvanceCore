"""Transactional outbox with independently leased, idempotent consumers."""

from __future__ import annotations

import logging
import os
import signal
import threading
from datetime import timedelta
from typing import TYPE_CHECKING, cast

from sqlalchemy import and_, or_, select, update
from sqlalchemy import event as sqlalchemy_event
from sqlalchemy.engine import CursorResult
from sqlalchemy.orm import Session

from .db import SessionLocal, utcnow

if TYPE_CHECKING:
    from .automation_models import OutboxEvent
    from .models import Run

TERMINAL = frozenset({"SUCCESS", "FAILED", "FAILED_PRECONDITION", "CANCELLED", "UNKNOWN"})
CONSUMERS = frozenset({"CHAINING", "NOTIFICATIONS"})
_installed = False
logger = logging.getLogger(__name__)


def append_event(db: Session, *, organization_id: str, dedupe_key: str, event_type: str,
                 aggregate_type: str, aggregate_id: str, module: str, payload: dict):
    from .automation_models import EventConsumption, OutboxEvent
    from .models import uid

    created = db.info.get("outbox_created_in_transaction", set())
    for pending in [*db.new, *db.identity_map.values()]:
        same_terminal_transaction = (pending.id in created and event_type in {"RUN_TERMINAL", "ACQUISITION_TERMINAL"}
            and pending.event_type == event_type and pending.aggregate_type == aggregate_type
            and pending.aggregate_id == aggregate_id) if isinstance(pending, OutboxEvent) else False
        if isinstance(pending, OutboxEvent) and pending.organization_id == organization_id and (
                pending.dedupe_key == dedupe_key and pending.id in created or same_terminal_transaction):
            pending.dedupe_key = dedupe_key
            pending.payload = payload
            return pending
    existing = db.scalar(select(OutboxEvent).where(OutboxEvent.organization_id == organization_id,
                                                   OutboxEvent.dedupe_key == dedupe_key))
    if existing:
        if existing.id in db.info.get("outbox_created_in_transaction", set()):
            existing.payload = payload
        return existing
    record = OutboxEvent(id=uid(), organization_id=organization_id, created_at=utcnow(),
        dedupe_key=dedupe_key, event_type=event_type, aggregate_type=aggregate_type,
        aggregate_id=aggregate_id, module=module, payload=payload)
    db.add(record)
    db.info.setdefault("outbox_created_in_transaction", set()).add(record.id)
    for consumer in sorted(CONSUMERS):
        db.add(EventConsumption(id=uid(), organization_id=organization_id,
                                event_id=record.id, event=record, consumer=consumer))
    return record


def record_run_event(db: Session, run: Run):
    if run.status not in TERMINAL or not run.id:
        return
    from .automation import settle_delivery_target

    plan = run.execution_plan or {}
    automation = plan.get("automation") or {}
    schedule = plan.get("schedule") or {}
    recipient = automation.get("responsible_user_id") or schedule.get("responsible_user_id")
    if not recipient and run.initiated_by_type == "USER":
        recipient = run.initiated_by_id
    origin = automation.get("origin") or ("SCHEDULED" if schedule else "MANUAL")
    evidence = (run.metrics or {}).get("evidence_status")
    append_event(db, organization_id=run.organization_id,
        dedupe_key=f"run:{run.id}:{run.status}:{run.decision or '-'}:{evidence or '-'}",
        event_type="RUN_TERMINAL", aggregate_type="RUN", aggregate_id=run.id,
        module="DELIVERY" if run.module == "DELIVERY_PREFLIGHT" else run.module,
        payload={"recipient_user_id": recipient, "status": run.status, "decision": run.decision,
                 "origin": origin, "evidence_status": evidence, "output_version_id": run.output_version_id,
                 "operation": "PREFLIGHT" if run.module == "DELIVERY_PREFLIGHT" else "EXECUTION"})
    if run.module == "DELIVERY":
        settle_delivery_target(db, run)


def _before_flush(db: Session, _context, _instances):
    from .models import Run

    # No history scan: only objects changed in this transaction produce events.
    for record in list(db.dirty):
        if isinstance(record, Run):
            record_run_event(db, record)
        elif type(record).__name__ == "AcquisitionRun" and record.status in TERMINAL and record.id:
            append_event(db, organization_id=record.organization_id,
                dedupe_key=f"acquisition:{record.id}:{record.status}", event_type="ACQUISITION_TERMINAL",
                aggregate_type="ACQUISITION", aggregate_id=record.id, module="acquisition",
                payload={"recipient_user_id": record.initiated_by_id, "status": record.status,
                         "origin": "MANUAL", "dataset_id": record.dataset_id,
                         "output_version_id": record.output_version_id})


def _clear_transaction_events(db: Session, transaction):
    if transaction.parent is None:
        db.info.pop("outbox_created_in_transaction", None)


def install_event_hooks():
    global _installed
    if not _installed:
        sqlalchemy_event.listen(Session, "before_flush", _before_flush)
        sqlalchemy_event.listen(Session, "after_transaction_end", _clear_transaction_events)
        _installed = True


def functional_description(event: OutboxEvent):
    payload = event.payload
    status, decision = payload.get("status"), payload.get("decision")
    if payload.get("operation") == "PREFLIGHT":
        if status == "SUCCESS" and decision == "PASS":
            return "La validación de la entrega terminó y aprobó sus precondiciones."
        if status == "CANCELLED":
            return "La validación de la entrega fue cancelada."
        return "La validación de la entrega no aprobó sus precondiciones; revisa el diagnóstico."
    if event.event_type == "MONITOR_SCHEDULE_BLOCKED":
        return {"SCHEDULE_EXECUTOR_DISABLED": "La programación Sentinel se bloqueó porque el responsable no está habilitado.",
                "SCHEDULE_PERMISSION_REVOKED": "La programación Sentinel se bloqueó por permisos revocados.",
                "MONITOR_BUSY": "El intervalo Sentinel se omitió porque existe una ejecución activa.",
                "NO_DATASET_VERSION": "El intervalo Sentinel se omitió porque no existe una versión registrada."}.get(
                    str(payload.get("reason_code")), "La programación Sentinel no pudo ejecutar este intervalo; revisa sus ocurrencias.")
    if event.event_type == "DELIVERY_AUTOMATION_BLOCKED":
        reasons = {
            "TARGET_UNKNOWN_BLOCKED": "La automatización está bloqueada por una confirmación remota desconocida.",
            "AUTOMATION_BUSY": "La ocurrencia se omitió porque hay una entrega activa.",
            "VERSION_ALREADY_PROCESSED": "La versión ya fue procesada por esta automatización.",
            "EMPTY_INPUT_BLOCKED": "La entrega automática se bloqueó porque la entrada está vacía.",
            "INTAKE_DECISION_NOT_ACCEPTED": "El Intake terminó con una decisión que no habilita la entrega.",
            "AUTOMATION_USER_DISABLED": "La entrega automática se bloqueó porque el responsable está desactivado.",
            "AUTOMATION_PERMISSION_REVOKED": "La entrega automática se bloqueó por permisos revocados.",
        }
        return reasons.get(str(payload.get("reason_code")), "La automatización no pudo despachar esta ocurrencia; revisa el detalle.")
    if event.module == "DELIVERY":
        if status == "UNKNOWN":
            return "La confirmación remota de la entrega es desconocida; requiere revisión."
        if decision == "COMMITTED":
            return ("Entrega confirmada con evidencia local pendiente de reparación."
                    if payload.get("evidence_status") == "PENDING_REPAIR" else "Entrega confirmada en el destino.")
    if event.module == "intake" and status == "SUCCESS":
        return {"REJECTED": "La validación terminó y el dataset fue rechazado.",
                "APPROVED_WITH_WARNINGS": "La validación terminó y el dataset fue aprobado con advertencias.",
                "APPROVED": "La validación terminó y el dataset fue aprobado."}.get(str(decision), "La validación Intake terminó.")
    if event.module == "sentinel" and status == "SUCCESS":
        return "El monitor terminó y detectó alertas." if decision == "ALERT" else "El monitor terminó sin alertas."
    if event.module == "recon" and status == "SUCCESS":
        return "La conciliación terminó con hallazgos." if decision == "WITH_FINDINGS" else "La conciliación terminó conforme."
    label = {"acquisition": "La adquisición", "DELIVERY": "La entrega", "intake": "La validación",
             "recon": "La conciliación", "sentinel": "El monitor"}.get(event.module, "El proceso")
    return label + {"FAILED": " falló; revisa la referencia de diagnóstico.",
                    "FAILED_PRECONDITION": " se bloqueó antes de ejecutar; revisa las precondiciones.",
                    "CANCELLED": " fue cancelada.", "SUCCESS": " terminó y publicó una versión completa."}.get(str(status), " cambió de estado.")


def consume_notification(db: Session, record: OutboxEvent):
    from .automation_models import InternalNotification
    from .models import User, uid

    recipient_id = record.payload.get("recipient_user_id")
    user = db.get(User, recipient_id) if recipient_id else None
    if user is None or user.organization_id != record.organization_id:
        return  # Never synthesize recipients for SYSTEM or unresolved legacy actors.
    if db.scalar(select(InternalNotification.id).where(
            InternalNotification.event_id == record.id, InternalNotification.recipient_user_id == user.id)):
        return
    if record.aggregate_type == "RUN":
        url = (f"/delivery/validation/{record.aggregate_id}" if record.payload.get("operation") == "PREFLIGHT"
               else f"/runs/{record.aggregate_id}")
    elif record.aggregate_type == "ACQUISITION":
        url = f"/datasets/{record.payload.get('dataset_id', '')}?acquisition={record.aggregate_id}"
    elif record.aggregate_type == "MONITOR_OCCURRENCE":
        url = f"/sentinel?monitor={record.payload.get('monitor_id', '')}"
    else:
        url = f"/delivery/automation/{record.payload.get('automation_id', '')}"
    db.add(InternalNotification(id=uid(), organization_id=record.organization_id,
        event_id=record.id, recipient_user_id=user.id, module=record.module,
        origin=record.payload.get("origin", "MANUAL"), status=record.payload["status"],
        decision=record.payload.get("decision"), description=functional_description(record),
        resource_type=record.aggregate_type, resource_id=record.aggregate_id, detail_url=url,
        created_at=record.created_at))


def consume_once(consumer: str, *, owner: str | None = None, now=None) -> bool:
    from .automation_models import EventConsumption, OutboxEvent
    from .dispatcher import bounded_parameter
    from .models import uid

    if consumer not in CONSUMERS:
        raise ValueError("Consumidor desconocido.")
    owner, instant = owner or uid(), now or utcnow()
    lease_seconds = bounded_parameter("TRACKVANCE_EVENT_LEASE_SECONDS", 300, 60, 3600)
    eligible = or_(and_(EventConsumption.status == "PENDING", EventConsumption.available_at <= instant),
                   and_(EventConsumption.status == "RUNNING", EventConsumption.lease_until < instant))
    with SessionLocal() as db:
        delivery = db.scalar(select(EventConsumption).where(
            EventConsumption.consumer == consumer, eligible).order_by(EventConsumption.created_at).limit(1))
        if delivery is None:
            return False
        claim = db.execute(update(EventConsumption).where(EventConsumption.id == delivery.id, eligible).values(
            status="RUNNING", lease_owner=owner, lease_until=instant + timedelta(seconds=lease_seconds)
        ).execution_options(synchronize_session=False))
        if cast(CursorResult, claim).rowcount != 1:
            db.rollback()
            return False
        db.refresh(delivery)
        if delivery.attempts >= 5:
            delivery.status, delivery.error_code, delivery.lease_until = "DEAD", "EVENT_RETRY_EXHAUSTED", None
            db.commit()
            return True
        delivery.attempts += 1
        delivery_id = delivery.id
        db.commit()
    try:
        with SessionLocal() as db:
            delivery = db.scalar(select(EventConsumption).where(
                EventConsumption.id == delivery_id, EventConsumption.lease_owner == owner,
                EventConsumption.status == "RUNNING", EventConsumption.lease_until > utcnow()).with_for_update())
            if delivery is None:
                return True
            record = db.get(OutboxEvent, delivery.event_id)
            if record is None:
                raise RuntimeError("EVENT_MISSING")
            if consumer == "NOTIFICATIONS":
                consume_notification(db, record)
            else:
                from .automation import consume_intake_event
                consume_intake_event(db, record)
            from .scheduler import aware

            if delivery.lease_until is None or aware(delivery.lease_until) <= utcnow():
                raise RuntimeError("EVENT_LEASE_EXPIRED")
            delivery.status, delivery.lease_until, delivery.completed_at = "DONE", None, utcnow()
            delivery.error_code = None
            db.commit()
    except Exception:  # noqa: BLE001 -- bounded consumer boundary records retry state without leaking diagnostics
        # Bounded recovery owns only consumer metadata; the original process and
        # its committed state are untouched. Never persist raw exception text.
        with SessionLocal() as db:
            delivery = db.scalar(select(EventConsumption).where(
                EventConsumption.id == delivery_id, EventConsumption.lease_owner == owner,
                EventConsumption.status == "RUNNING").with_for_update())
            if delivery:
                delivery.status = "DEAD" if delivery.attempts >= 5 else "PENDING"
                delivery.error_code, delivery.lease_until = "EVENT_CONSUMER_FAILED", None
                delivery.available_at = utcnow() + timedelta(seconds=min(300, 2 ** delivery.attempts))
                db.commit()
    return True


def main():
    from .dispatcher import bounded_parameter, heartbeat_loop
    from .models import uid

    consumer = os.environ.get("TRACKVANCE_EVENT_CONSUMER", "NOTIFICATIONS").upper()
    if consumer not in CONSUMERS:
        raise RuntimeError("TRACKVANCE_EVENT_CONSUMER debe ser CHAINING o NOTIFICATIONS.")
    interval = bounded_parameter("TRACKVANCE_EVENT_POLL_SECONDS", 1, 1, 60)
    stop = threading.Event()
    signal.signal(signal.SIGINT, lambda *_: stop.set())
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    owner = uid()
    threading.Thread(target=heartbeat_loop, args=(f"events-{consumer.lower()}", owner, stop), daemon=True).start()
    while not stop.is_set():
        try:
            if not consume_once(consumer, owner=owner):
                stop.wait(interval)
        except Exception:  # noqa: BLE001 -- supervised process boundary preserves the independent consumer
            logger.warning("Consumidor %s espera metadata disponible.", consumer)
            stop.wait(interval)


if __name__ == "__main__":
    main()
