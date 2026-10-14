"""Durable remote batch identities; payloads remain immutable private artifacts."""

from datetime import datetime

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Integer,
    String,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from .db import Base
from .models import Record


class RestDeliveryBatch(Record, Base):
    __tablename__ = "rest_delivery_batches"
    __table_args__ = (
        UniqueConstraint("run_id", "ordinal"),
        CheckConstraint("status IN ('PREPARED','STARTED','COMMITTED','FAILED','UNKNOWN')", name="ck_rest_batch_status"),
    )
    run_id: Mapped[str] = mapped_column(ForeignKey("runs.id"), index=True)
    delivery_attempt_id: Mapped[str] = mapped_column(ForeignKey("delivery_attempts.id"), index=True)
    ordinal: Mapped[int] = mapped_column(Integer)
    payload_sha256: Mapped[str] = mapped_column(String(64))
    payload_artifact_id: Mapped[str] = mapped_column(ForeignKey("artifacts.id"))
    row_count: Mapped[int] = mapped_column(BigInteger)
    bytes_sent: Mapped[int] = mapped_column(BigInteger)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    status: Mapped[str] = mapped_column(String(20), default="PREPARED", index=True)
    response_status: Mapped[int | None] = mapped_column(Integer, nullable=True)
    response_sha256: Mapped[str | None] = mapped_column(String(64), nullable=True)
    error_code: Mapped[str | None] = mapped_column(String(80), nullable=True)
    confirmed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
