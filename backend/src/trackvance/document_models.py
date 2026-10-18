"""Operational document evidence; governance remains attached to published datasets."""
from datetime import datetime

from sqlalchemy import JSON, Boolean, DateTime, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from .db import Base
from .models import Record


class DocumentFamily(Record, Base):
    __tablename__ = "document_families"
    __table_args__ = (UniqueConstraint("organization_id", "name"),)
    name: Mapped[str] = mapped_column(String(140))
    description: Mapped[str] = mapped_column(Text, default="")
    responsible_user_id: Mapped[str] = mapped_column(ForeignKey("users.id"))
    active_template_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    output_datasets: Mapped[dict] = mapped_column(JSON, default=dict)
    next_cut_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    lock_version: Mapped[int] = mapped_column(Integer, default=1)


class DocumentTemplate(Record, Base):
    __tablename__ = "document_templates"
    __table_args__ = (UniqueConstraint("family_id", "revision"),)
    family_id: Mapped[str] = mapped_column(ForeignKey("document_families.id"), index=True)
    revision: Mapped[int] = mapped_column(Integer)
    definition: Mapped[dict] = mapped_column(JSON)
    definition_hash: Mapped[str] = mapped_column(String(64))
    created_by_id: Mapped[str] = mapped_column(ForeignKey("users.id"))
    tested_extraction_id: Mapped[str | None] = mapped_column(String(64), nullable=True)


class DocumentOriginal(Record, Base):
    __tablename__ = "document_originals"
    family_id: Mapped[str] = mapped_column(ForeignKey("document_families.id"), index=True)
    template_id: Mapped[str] = mapped_column(ForeignKey("document_templates.id"))
    artifact_id: Mapped[str] = mapped_column(ForeignKey("artifacts.id"))
    filename: Mapped[str] = mapped_column(String(240))
    sha256: Mapped[str] = mapped_column(String(64), index=True)
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    uploaded_by_id: Mapped[str] = mapped_column(ForeignKey("users.id"))
    status: Mapped[str] = mapped_column(String(30), default="QUEUED", index=True)
    progress: Mapped[int] = mapped_column(Integer, default=0)
    cancel_requested: Mapped[bool] = mapped_column(Boolean, default=False)
    extraction_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    duplicate_of_id: Mapped[str | None] = mapped_column(ForeignKey("document_originals.id"), nullable=True)
    correction_of_id: Mapped[str | None] = mapped_column(ForeignKey("document_originals.id"), nullable=True)
    is_example: Mapped[bool] = mapped_column(Boolean, default=False)
    error_code: Mapped[str | None] = mapped_column(String(80), nullable=True)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class DocumentExtraction(Record, Base):
    __tablename__ = "document_extractions"
    __table_args__ = (UniqueConstraint("document_id", "revision"),)
    document_id: Mapped[str] = mapped_column(ForeignKey("document_originals.id"), index=True)
    template_id: Mapped[str] = mapped_column(ForeignKey("document_templates.id"))
    artifact_id: Mapped[str | None] = mapped_column(ForeignKey("artifacts.id"), nullable=True)
    revision: Mapped[int] = mapped_column(Integer)
    previous_id: Mapped[str | None] = mapped_column(ForeignKey("document_extractions.id"), nullable=True)
    payload: Mapped[dict] = mapped_column(JSON)
    payload_hash: Mapped[str] = mapped_column(String(64))
    engine: Mapped[dict] = mapped_column(JSON)
    reviewed: Mapped[bool] = mapped_column(Boolean, default=False)
    actor_id: Mapped[str] = mapped_column(ForeignKey("users.id"))
    reason: Mapped[str] = mapped_column(String(2000), default="Extracción automática")


class DocumentBatch(Record, Base):
    __tablename__ = "document_batches"
    __table_args__ = (UniqueConstraint("family_id", "idempotency_key"),)
    family_id: Mapped[str] = mapped_column(ForeignKey("document_families.id"), index=True)
    template_id: Mapped[str] = mapped_column(ForeignKey("document_templates.id"))
    idempotency_key: Mapped[str] = mapped_column(String(160))
    status: Mapped[str] = mapped_column(String(30), default="OPEN", index=True)
    window_start: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    window_end: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    settings: Mapped[dict] = mapped_column(JSON)
    sealed_manifest: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    manifest_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    published_versions: Mapped[dict] = mapped_column(JSON, default=dict)
    actor_id: Mapped[str] = mapped_column(ForeignKey("users.id"))
    blocked_reason: Mapped[str | None] = mapped_column(String(120), nullable=True)
    wait_until: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class DocumentBatchMember(Record, Base):
    __tablename__ = "document_batch_members"
    __table_args__ = (UniqueConstraint("batch_id", "document_id"),)
    batch_id: Mapped[str] = mapped_column(ForeignKey("document_batches.id"), index=True)
    document_id: Mapped[str] = mapped_column(ForeignKey("document_originals.id"))
    extraction_id: Mapped[str | None] = mapped_column(ForeignKey("document_extractions.id"), nullable=True)
    status: Mapped[str] = mapped_column(String(30), default="PENDING")
    reason: Mapped[str | None] = mapped_column(String(120), nullable=True)


class DocumentPublicationClaim(Record, Base):
    __tablename__ = "document_publication_claims"
    extraction_id: Mapped[str] = mapped_column(ForeignKey("document_extractions.id"), unique=True)
    document_id: Mapped[str] = mapped_column(ForeignKey("document_originals.id"), index=True)
    batch_id: Mapped[str] = mapped_column(ForeignKey("document_batches.id"), index=True)


class DocumentReference(Record, Base):
    """Configuration corpus, deliberately outside operational originals/publication."""
    __tablename__ = "document_references"
    family_id: Mapped[str] = mapped_column(ForeignKey("document_families.id"), index=True)
    artifact_id: Mapped[str] = mapped_column(ForeignKey("artifacts.id"))
    filename: Mapped[str] = mapped_column(String(240))
    sha256: Mapped[str] = mapped_column(String(64))
    actor_id: Mapped[str] = mapped_column(ForeignKey("users.id"))
    role: Mapped[str] = mapped_column(String(20), default="CONFIGURATION")
    association: Mapped[str | None] = mapped_column(String(120), nullable=True)
    configuration_used: Mapped[bool] = mapped_column(Boolean, default=False)
    status: Mapped[str] = mapped_column(String(20), default="QUEUED")
    payload: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    payload_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    engine: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    error_code: Mapped[str | None] = mapped_column(String(80), nullable=True)


class DocumentDraft(Record, Base):
    __tablename__ = "document_drafts"
    family_id: Mapped[str] = mapped_column(ForeignKey("document_families.id"), index=True)
    name: Mapped[str] = mapped_column(String(140), default="Borrador")
    version: Mapped[int] = mapped_column(Integer, default=1)
    definition: Mapped[dict] = mapped_column(JSON)
    definition_hash: Mapped[str] = mapped_column(String(64))
    reference_ids: Mapped[list] = mapped_column(JSON, default=list)
    discovered_from: Mapped[list] = mapped_column(JSON, default=list)
    source_template_id: Mapped[str | None] = mapped_column(ForeignKey("document_templates.id"), nullable=True)
    actor_id: Mapped[str] = mapped_column(ForeignKey("users.id"))


class DocumentBuilderTest(Record, Base):
    __tablename__ = "document_builder_tests"
    __table_args__ = (UniqueConstraint("draft_id", "snapshot_hash"),)
    draft_id: Mapped[str] = mapped_column(ForeignKey("document_drafts.id"), index=True)
    draft_version: Mapped[int] = mapped_column(Integer)
    snapshot: Mapped[dict] = mapped_column(JSON)
    snapshot_hash: Mapped[str] = mapped_column(String(64))
    automatic_results: Mapped[dict] = mapped_column(JSON)
    automatic_results_hash: Mapped[str] = mapped_column(String(64))
    reviewed_results: Mapped[dict] = mapped_column(JSON, default=dict)
    review_version: Mapped[int] = mapped_column(Integer, default=1)
    review_history: Mapped[list] = mapped_column(JSON, default=list)
    activated_template_id: Mapped[str | None] = mapped_column(ForeignKey("document_templates.id"), nullable=True)
    actor_id: Mapped[str] = mapped_column(ForeignKey("users.id"))
