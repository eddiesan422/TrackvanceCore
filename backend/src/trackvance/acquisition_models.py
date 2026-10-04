"""Durable acquisition identity exists before its final DatasetVersion."""

from datetime import datetime

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from .db import Base
from .models import Record


class AcquisitionUpload(Record, Base):
    __tablename__ = "acquisition_uploads"
    __table_args__ = (CheckConstraint("status IN ('RECEIVED','REGISTERED','CONSUMED','EXPIRED')", name="ck_acquisition_upload_status"),)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    filename: Mapped[str] = mapped_column(String(240))
    path: Mapped[str] = mapped_column(Text)
    sha256: Mapped[str] = mapped_column(String(64))
    size_bytes: Mapped[int] = mapped_column(BigInteger)
    source_format: Mapped[str] = mapped_column(String(30))
    status: Mapped[str] = mapped_column(String(20), default="RECEIVED")
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)


class AcquisitionRun(Record, Base):
    __tablename__ = "acquisition_runs"
    __table_args__ = (
        UniqueConstraint("organization_id", "initiated_by_id", "idempotency_key"),
        CheckConstraint("status IN ('QUEUED','RUNNING','SUCCESS','FAILED','CANCELLED')", name="ck_acquisition_status"),
        CheckConstraint("(source_type = 'UPLOAD' AND upload_id IS NOT NULL AND connection_version_id IS NULL) OR (source_type IN ('POSTGRESQL','SQLSERVER') AND upload_id IS NULL AND connection_version_id IS NOT NULL)", name="ck_acquisition_source_identity"),
        Index("ix_acquisition_org_dataset_created", "organization_id", "dataset_id", "created_at"),
    )
    dataset_id: Mapped[str] = mapped_column(ForeignKey("datasets.id"), index=True)
    upload_id: Mapped[str | None] = mapped_column(ForeignKey("acquisition_uploads.id"), nullable=True, unique=True)
    connection_version_id: Mapped[str | None] = mapped_column(ForeignKey("external_connection_versions.id"), nullable=True)
    source_type: Mapped[str] = mapped_column(String(30))
    filename: Mapped[str] = mapped_column(String(240))
    source_snapshot: Mapped[dict] = mapped_column(JSON, default=dict)
    reader_options: Mapped[dict] = mapped_column(JSON, default=dict)
    column_overrides: Mapped[dict] = mapped_column(JSON, default=dict)
    effective_limits: Mapped[dict] = mapped_column(JSON, default=dict)
    request_hash: Mapped[str] = mapped_column(String(64))
    idempotency_key: Mapped[str | None] = mapped_column(String(128), nullable=True)
    initiated_by_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    initiated_by_name: Mapped[str] = mapped_column(String(200))
    status: Mapped[str] = mapped_column(String(20), default="QUEUED", index=True)
    stage: Mapped[str] = mapped_column(String(50), default="QUEUED")
    attempt_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    processed_rows: Mapped[int] = mapped_column(BigInteger, default=0)
    processed_bytes: Mapped[int] = mapped_column(BigInteger, default=0)
    total_rows: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    total_bytes: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    cancel_requested: Mapped[bool] = mapped_column(Boolean, default=False)
    output_version_id: Mapped[str | None] = mapped_column(ForeignKey("dataset_versions.id"), nullable=True, unique=True)
    error_code: Mapped[str | None] = mapped_column(String(80), nullable=True)
    error_message: Mapped[str | None] = mapped_column(String(500), nullable=True)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

