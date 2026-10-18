"""Native acquisition scheduler configuration and immutable occurrence evidence."""
from datetime import datetime

from sqlalchemy import JSON, Boolean, DateTime, ForeignKey, Integer, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from .db import Base, utcnow
from .models import Record


class AcquisitionSourceConfiguration(Record, Base):
    __tablename__ = "acquisition_source_configurations"
    dataset_id: Mapped[str] = mapped_column(ForeignKey("datasets.id"), unique=True)
    version: Mapped[int] = mapped_column(Integer, default=1)
    enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    next_run_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    last_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_query_success_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_publication_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    dispatch_diagnostic: Mapped[dict | None] = mapped_column(JSON, nullable=True)


class AcquisitionSourceRevision(Record, Base):
    __tablename__ = "acquisition_source_revisions"
    __table_args__ = (UniqueConstraint("configuration_id", "version"),)
    configuration_id: Mapped[str] = mapped_column(ForeignKey("acquisition_source_configurations.id"), index=True)
    version: Mapped[int] = mapped_column(Integer)
    actor_id: Mapped[str] = mapped_column(ForeignKey("users.id"))
    settings: Mapped[dict] = mapped_column(JSON)


class AcquisitionOccurrence(Record, Base):
    __tablename__ = "acquisition_occurrences"
    __table_args__ = (UniqueConstraint("configuration_id", "planned_at"),)
    configuration_id: Mapped[str] = mapped_column(ForeignKey("acquisition_source_configurations.id"), index=True)
    revision_id: Mapped[str] = mapped_column(ForeignKey("acquisition_source_revisions.id"))
    planned_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    acquisition_id: Mapped[str | None] = mapped_column(ForeignKey("acquisition_runs.id"), nullable=True)
    status: Mapped[str] = mapped_column(String(30))
    reason_code: Mapped[str | None] = mapped_column(String(80), nullable=True)
    dispatched_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    coalesced_intervals: Mapped[int] = mapped_column(Integer, default=0)
    reason_message: Mapped[str | None] = mapped_column(String(500), nullable=True)
    error_details: Mapped[dict | None] = mapped_column(JSON, nullable=True)
