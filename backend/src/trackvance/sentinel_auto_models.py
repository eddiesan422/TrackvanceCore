"""Additive identities for dataset monitoring, replay and lightweight reviews."""

from datetime import datetime

from sqlalchemy import JSON, Boolean, DateTime, ForeignKey, Integer, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from .db import Base, utcnow
from .models import Record


class AutomaticMonitor(Record, Base):
    __tablename__ = "sentinel_automatic_monitors"
    dataset_id: Mapped[str] = mapped_column(ForeignKey("datasets.id"), unique=True)
    monitor_root_id: Mapped[str] = mapped_column(ForeignKey("configurations.id"), unique=True)
    responsible_user_id: Mapped[str | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    version: Mapped[int] = mapped_column(Integer, default=1)
    review_interval_seconds: Mapped[int] = mapped_column(Integer, default=86400)
    next_review_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class SentinelVersionEvaluation(Record, Base):
    __tablename__ = "sentinel_version_evaluations"
    __table_args__ = (UniqueConstraint("automatic_monitor_id", "dataset_version_id", name="uq_sentinel_auto_version"),)
    automatic_monitor_id: Mapped[str] = mapped_column(ForeignKey("sentinel_automatic_monitors.id"), index=True)
    dataset_version_id: Mapped[str] = mapped_column(ForeignKey("dataset_versions.id"), index=True)
    monitor_id: Mapped[str] = mapped_column(ForeignKey("configurations.id"))
    event_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    run_id: Mapped[str | None] = mapped_column(ForeignKey("runs.id"), nullable=True, unique=True)
    status: Mapped[str] = mapped_column(String(30), default="PENDING")
    reason_code: Mapped[str | None] = mapped_column(String(80), nullable=True)


class AutomaticMonitorRevision(Record, Base):
    __tablename__ = "sentinel_automatic_revisions"
    __table_args__ = (UniqueConstraint("automatic_monitor_id", "version", name="uq_sentinel_auto_revision"),)
    automatic_monitor_id: Mapped[str] = mapped_column(ForeignKey("sentinel_automatic_monitors.id"), index=True)
    version: Mapped[int] = mapped_column(Integer)
    actor_id: Mapped[str | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    settings: Mapped[dict] = mapped_column(JSON)


class SentinelLightReview(Record, Base):
    __tablename__ = "sentinel_light_reviews"
    __table_args__ = (UniqueConstraint("automatic_monitor_id", "planned_at", name="uq_sentinel_light_slot"),)
    automatic_monitor_id: Mapped[str] = mapped_column(ForeignKey("sentinel_automatic_monitors.id"), index=True)
    dataset_version_id: Mapped[str | None] = mapped_column(ForeignKey("dataset_versions.id"), nullable=True)
    planned_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    status: Mapped[str] = mapped_column(String(30))
    reason_code: Mapped[str | None] = mapped_column(String(80), nullable=True)
    metrics: Mapped[dict] = mapped_column(JSON, default=dict)
