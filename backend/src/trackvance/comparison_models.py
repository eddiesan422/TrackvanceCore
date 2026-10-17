"""Immutable input identities and durable evidence for complete version comparisons."""
from datetime import datetime

from sqlalchemy import JSON, Boolean, DateTime, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from .db import Base
from .models import Record


class VersionComparison(Record, Base):
    __tablename__ = "version_comparisons"
    before_version_id: Mapped[str] = mapped_column(ForeignKey("dataset_versions.id"), index=True)
    after_version_id: Mapped[str] = mapped_column(ForeignKey("dataset_versions.id"), index=True)
    actor_id: Mapped[str] = mapped_column(ForeignKey("users.id"))
    settings: Mapped[dict] = mapped_column(JSON)
    status: Mapped[str] = mapped_column(String(30), default="QUEUED", index=True)
    progress_percent: Mapped[int] = mapped_column(Integer, default=0)
    progress_stage: Mapped[str] = mapped_column(String(160), default="En cola")
    cancel_requested: Mapped[bool] = mapped_column(Boolean, default=False)
    summary: Mapped[dict] = mapped_column(JSON, default=dict)
    artifact_id: Mapped[str | None] = mapped_column(ForeignKey("artifacts.id"), nullable=True)
    error_code: Mapped[str | None] = mapped_column(String(80), nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
