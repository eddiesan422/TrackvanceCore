"""Durable automation and internal events. Legacy email records stay separate."""

from datetime import datetime

from sqlalchemy import (
    JSON,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .db import Base, utcnow
from .models import Record


class DeliveryAutomation(Record, Base):
    __tablename__ = "delivery_automations"
    name: Mapped[str] = mapped_column(String(160))
    version: Mapped[int] = mapped_column(Integer, default=1)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    responsible_user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    next_run_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True, index=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class DeliveryAutomationVersion(Record, Base):
    __tablename__ = "delivery_automation_versions"
    __table_args__ = (UniqueConstraint("automation_id", "version"),)
    automation_id: Mapped[str] = mapped_column(ForeignKey("delivery_automations.id"), index=True)
    version: Mapped[int] = mapped_column(Integer)
    configuration_id: Mapped[str] = mapped_column(ForeignKey("configurations.id"))
    responsible_user_id: Mapped[str] = mapped_column(ForeignKey("users.id"))
    enabled: Mapped[bool] = mapped_column(Boolean)
    settings: Mapped[dict] = mapped_column(JSON)
    actor_id: Mapped[str] = mapped_column(ForeignKey("users.id"))


class DeliveryOccurrence(Record, Base):
    __tablename__ = "delivery_occurrences"
    __table_args__ = (UniqueConstraint("automation_id", "trigger_key"),)
    automation_id: Mapped[str] = mapped_column(ForeignKey("delivery_automations.id"), index=True)
    automation_version_id: Mapped[str] = mapped_column(ForeignKey("delivery_automation_versions.id"))
    trigger_key: Mapped[str] = mapped_column(String(180))
    request_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    origin: Mapped[str] = mapped_column(String(20))
    planned_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    dispatched_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    source_run_id: Mapped[str | None] = mapped_column(ForeignKey("runs.id"), nullable=True)
    dataset_version_id: Mapped[str | None] = mapped_column(ForeignKey("dataset_versions.id"), nullable=True)
    run_id: Mapped[str | None] = mapped_column(ForeignKey("runs.id"), nullable=True, unique=True)
    status: Mapped[str] = mapped_column(String(30))
    reason_code: Mapped[str | None] = mapped_column(String(80), nullable=True)
    coalesced_intervals: Mapped[int] = mapped_column(Integer, default=0)


class DeliveryInputClaim(Record, Base):
    __tablename__ = "delivery_input_claims"
    __table_args__ = (UniqueConstraint("automation_id", "dataset_version_id"),)
    automation_id: Mapped[str] = mapped_column(ForeignKey("delivery_automations.id"))
    dataset_version_id: Mapped[str] = mapped_column(ForeignKey("dataset_versions.id"))
    occurrence_id: Mapped[str] = mapped_column(ForeignKey("delivery_occurrences.id"))


class DeliveryTargetGuard(Record, Base):
    __tablename__ = "delivery_target_guards"
    __table_args__ = (UniqueConstraint("organization_id", "target_fingerprint"),)
    target_fingerprint: Mapped[str] = mapped_column(String(64))
    active_run_id: Mapped[str | None] = mapped_column(ForeignKey("runs.id"), nullable=True)
    unknown_run_id: Mapped[str | None] = mapped_column(ForeignKey("runs.id"), nullable=True)


class DeliveryTargetDecision(Record, Base):
    __tablename__ = "delivery_target_decisions"
    __table_args__ = (UniqueConstraint("run_id"),)
    guard_id: Mapped[str] = mapped_column(ForeignKey("delivery_target_guards.id"))
    run_id: Mapped[str] = mapped_column(ForeignKey("runs.id"))
    review_id: Mapped[str] = mapped_column(ForeignKey("delivery_reviews.id"))
    actor_id: Mapped[str] = mapped_column(ForeignKey("users.id"))
    note: Mapped[str] = mapped_column(String(2000))


class OutboxEvent(Record, Base):
    __tablename__ = "outbox_events"
    __table_args__ = (UniqueConstraint("organization_id", "dedupe_key"),)
    dedupe_key: Mapped[str] = mapped_column(String(220))
    event_type: Mapped[str] = mapped_column(String(60))
    aggregate_type: Mapped[str] = mapped_column(String(30))
    aggregate_id: Mapped[str] = mapped_column(String(64))
    module: Mapped[str] = mapped_column(String(20))
    payload: Mapped[dict] = mapped_column(JSON)


class EventConsumption(Record, Base):
    __tablename__ = "event_consumptions"
    __table_args__ = (
        UniqueConstraint("event_id", "consumer"),
        CheckConstraint("attempts >= 0 AND attempts <= 5", name="ck_event_attempts"),
        Index("ix_event_consumer_pending", "consumer", "status", "available_at"),
    )
    event_id: Mapped[str] = mapped_column(ForeignKey("outbox_events.id"))
    event: Mapped[OutboxEvent] = relationship()
    consumer: Mapped[str] = mapped_column(String(20))
    status: Mapped[str] = mapped_column(String(20), default="PENDING")
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    available_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    lease_owner: Mapped[str | None] = mapped_column(String(64), nullable=True)
    lease_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    error_code: Mapped[str | None] = mapped_column(String(80), nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class InternalNotification(Record, Base):
    __tablename__ = "internal_notifications"
    __table_args__ = (
        UniqueConstraint("event_id", "recipient_user_id"),
        Index("ix_notification_inbox", "organization_id", "recipient_user_id", "read_at", "created_at"),
    )
    event_id: Mapped[str] = mapped_column(ForeignKey("outbox_events.id"))
    recipient_user_id: Mapped[str] = mapped_column(ForeignKey("users.id"))
    module: Mapped[str] = mapped_column(String(20))
    origin: Mapped[str] = mapped_column(String(20))
    status: Mapped[str] = mapped_column(String(30))
    decision: Mapped[str | None] = mapped_column(String(40), nullable=True)
    description: Mapped[str] = mapped_column(String(500))
    resource_type: Mapped[str] = mapped_column(String(30))
    resource_id: Mapped[str] = mapped_column(String(64))
    detail_url: Mapped[str] = mapped_column(String(200))
    read_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


# Register after all outbox types exist, including when this module is the
# initial import. This avoids a circular import through models metadata setup.
from .events import install_event_hooks

install_event_hooks()
