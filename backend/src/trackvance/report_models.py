"""Report identities, immutable definitions and executions; never fictitious Intake runs."""
from datetime import datetime

from sqlalchemy import JSON, Boolean, DateTime, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from .db import Base, utcnow
from .models import Record


class ReportDefinition(Record, Base):
    __tablename__ = "report_definitions"
    __table_args__ = (UniqueConstraint("organization_id", "name"),)
    name: Mapped[str] = mapped_column(String(160))
    description: Mapped[str] = mapped_column(Text, default="")
    owner_user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    version: Mapped[int] = mapped_column(Integer, default=1)
    active: Mapped[bool] = mapped_column(Boolean, default=True)


class ReportRevision(Record, Base):
    __tablename__ = "report_revisions"
    __table_args__ = (UniqueConstraint("definition_id", "version"),)
    definition_id: Mapped[str] = mapped_column(ForeignKey("report_definitions.id"), index=True)
    version: Mapped[int] = mapped_column(Integer)
    draft: Mapped[dict] = mapped_column(JSON)
    query_hash: Mapped[str] = mapped_column(String(64))
    created_by_user_id: Mapped[str] = mapped_column(ForeignKey("users.id"))


class ReportContext(Record, Base):
    __tablename__ = "report_contexts"
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    revision_id: Mapped[str | None] = mapped_column(ForeignKey("report_revisions.id"), nullable=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    snapshot: Mapped[dict] = mapped_column(JSON)
    integrity_hash: Mapped[str] = mapped_column(String(64))


class ReportExecution(Record, Base):
    __tablename__ = "report_executions"
    __table_args__ = (UniqueConstraint("organization_id", "user_id", "idempotency_key"),)
    context_id: Mapped[str] = mapped_column(ForeignKey("report_contexts.id"), index=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    profile: Mapped[str] = mapped_column(String(20))
    status: Mapped[str] = mapped_column(String(30), default="QUEUED", index=True)
    generation_status: Mapped[str] = mapped_column(String(30), default="PENDING")
    transmission_status: Mapped[str] = mapped_column(String(30), default="NOT_APPLICABLE")
    idempotency_key: Mapped[str | None] = mapped_column(String(100), nullable=True)
    request_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    publication: Mapped[dict] = mapped_column(JSON, default=dict)
    metrics: Mapped[dict] = mapped_column(JSON, default=dict)
    progress_stage: Mapped[str] = mapped_column(String(80), default="En cola")
    progress_percent: Mapped[int] = mapped_column(Integer, default=0)
    cancel_requested: Mapped[bool] = mapped_column(Boolean, default=False)
    output_version_id: Mapped[str | None] = mapped_column(ForeignKey("dataset_versions.id"), nullable=True)
    error_code: Mapped[str | None] = mapped_column(String(80), nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


def now() -> datetime:
    return utcnow()
