"""Additive lifecycle metadata; published contracts and historical runs stay intact."""

from datetime import datetime

from sqlalchemy import JSON, Boolean, DateTime, ForeignKey, Integer, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from .db import Base, utcnow
from .models import Record


class IntakeContractState(Record, Base):
    __tablename__ = "intake_contract_states"
    root_configuration_id: Mapped[str] = mapped_column(ForeignKey("configurations.id"), unique=True)
    active_run_id: Mapped[str | None] = mapped_column(ForeignKey("runs.id"), nullable=True)
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    deleted_by_id: Mapped[str | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    deletion_note: Mapped[str | None] = mapped_column(String(2000), nullable=True)
    deletion_impact: Mapped[dict | None] = mapped_column(JSON, nullable=True)


class IntakeSchedule(Record, Base):
    __tablename__ = "intake_schedules"
    root_configuration_id: Mapped[str] = mapped_column(ForeignKey("configurations.id"), unique=True)
    version: Mapped[int] = mapped_column(Integer, default=1)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    next_run_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True, index=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class IntakeScheduleVersion(Record, Base):
    __tablename__ = "intake_schedule_versions"
    __table_args__ = (UniqueConstraint("schedule_id", "version"),)
    schedule_id: Mapped[str] = mapped_column(ForeignKey("intake_schedules.id"), index=True)
    version: Mapped[int] = mapped_column(Integer)
    configuration_id: Mapped[str] = mapped_column(ForeignKey("configurations.id"))
    responsible_user_id: Mapped[str] = mapped_column(ForeignKey("users.id"))
    actor_id: Mapped[str] = mapped_column(ForeignKey("users.id"))
    enabled: Mapped[bool] = mapped_column(Boolean)
    settings: Mapped[dict] = mapped_column(JSON)


class IntakeOccurrence(Record, Base):
    __tablename__ = "intake_occurrences"
    __table_args__ = (UniqueConstraint("schedule_id", "trigger_key"),)
    schedule_id: Mapped[str] = mapped_column(ForeignKey("intake_schedules.id"), index=True)
    schedule_version_id: Mapped[str] = mapped_column(ForeignKey("intake_schedule_versions.id"))
    configuration_id: Mapped[str] = mapped_column(ForeignKey("configurations.id"))
    trigger_key: Mapped[str] = mapped_column(String(180))
    dataset_version_id: Mapped[str | None] = mapped_column(ForeignKey("dataset_versions.id"), nullable=True)
    run_id: Mapped[str | None] = mapped_column(ForeignKey("runs.id"), nullable=True, unique=True)
    origin: Mapped[str] = mapped_column(String(20))
    planned_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    status: Mapped[str] = mapped_column(String(30))
    reason_code: Mapped[str | None] = mapped_column(String(80), nullable=True)
    coalesced_intervals: Mapped[int] = mapped_column(Integer, default=0)
