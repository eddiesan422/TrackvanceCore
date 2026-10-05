"""Organization-scoped catalog identities and append-only governance evidence."""

from datetime import datetime

from sqlalchemy import (
    JSON,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from .db import Base
from .models import Record


class MacroDomain(Record, Base):
    __tablename__ = "macro_domains"
    __table_args__ = (UniqueConstraint("organization_id", "normalized_name"),)
    name: Mapped[str] = mapped_column(String(160))
    normalized_name: Mapped[str] = mapped_column(String(160))
    description: Mapped[str] = mapped_column(Text, default="")
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    version: Mapped[int] = mapped_column(Integer, default=1)


class DataDomain(Record, Base):
    __tablename__ = "data_domains"
    __table_args__ = (UniqueConstraint("organization_id", "macro_domain_id", "normalized_name"),)
    macro_domain_id: Mapped[str] = mapped_column(ForeignKey("macro_domains.id"), index=True)
    name: Mapped[str] = mapped_column(String(160))
    normalized_name: Mapped[str] = mapped_column(String(160))
    description: Mapped[str] = mapped_column(Text, default="")
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    version: Mapped[int] = mapped_column(Integer, default=1)


class GovernanceHistory(Record, Base):
    __tablename__ = "governance_history"
    __table_args__ = (UniqueConstraint("dataset_id", "version"),)
    dataset_id: Mapped[str] = mapped_column(ForeignKey("datasets.id"), index=True)
    version: Mapped[int] = mapped_column(Integer)
    actor_id: Mapped[str | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    snapshot: Mapped[dict] = mapped_column(JSON)
    reason: Mapped[str] = mapped_column(String(500), default="")


class GlossaryTerm(Record, Base):
    __tablename__ = "glossary_terms"
    __table_args__ = (UniqueConstraint("organization_id", "normalized_name"),)
    name: Mapped[str] = mapped_column(String(160))
    normalized_name: Mapped[str] = mapped_column(String(160))
    definition: Mapped[str] = mapped_column(Text)
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    version: Mapped[int] = mapped_column(Integer, default=1)


class ColumnDocumentation(Record, Base):
    __tablename__ = "column_documentation"
    __table_args__ = (UniqueConstraint("dataset_version_id", "column_name"),)
    dataset_version_id: Mapped[str] = mapped_column(ForeignKey("dataset_versions.id"), index=True)
    schema_hash: Mapped[str] = mapped_column(String(64))
    column_name: Mapped[str] = mapped_column(String(240))
    description: Mapped[str] = mapped_column(Text, default="")
    version: Mapped[int] = mapped_column(Integer, default=1)


class GlossaryAssociation(Record, Base):
    __tablename__ = "glossary_associations"
    __table_args__ = (UniqueConstraint("term_id", "dataset_id", "dataset_version_id", "column_name"),)
    term_id: Mapped[str] = mapped_column(ForeignKey("glossary_terms.id"), index=True)
    dataset_id: Mapped[str] = mapped_column(ForeignKey("datasets.id"), index=True)
    dataset_version_id: Mapped[str | None] = mapped_column(ForeignKey("dataset_versions.id"), nullable=True)
    column_name: Mapped[str | None] = mapped_column(String(240), nullable=True)


class DatasetBlock(Record, Base):
    __tablename__ = "dataset_blocks"
    __table_args__ = (CheckConstraint("scope IN ('REPORT','CONTENT')", name="ck_dataset_block_scope"),)
    dataset_id: Mapped[str] = mapped_column(ForeignKey("datasets.id"), index=True)
    scope: Mapped[str] = mapped_column(String(20))
    reason: Mapped[str] = mapped_column(String(1000))
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    version: Mapped[int] = mapped_column(Integer, default=1)
    created_by_id: Mapped[str] = mapped_column(ForeignKey("users.id"))
    released_by_id: Mapped[str | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    released_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    release_reason: Mapped[str | None] = mapped_column(String(1000), nullable=True)


class DatasetSecurityDependency(Record, Base):
    __tablename__ = "dataset_security_dependencies"
    __table_args__ = (UniqueConstraint("dataset_id", "source_dataset_id"),
                     CheckConstraint("dataset_id != source_dataset_id", name="ck_security_not_self"))
    dataset_id: Mapped[str] = mapped_column(ForeignKey("datasets.id"), index=True)
    source_dataset_id: Mapped[str] = mapped_column(ForeignKey("datasets.id"), index=True)
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    version: Mapped[int] = mapped_column(Integer, default=1)
    released_by_id: Mapped[str | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    release_reason: Mapped[str | None] = mapped_column(String(1000), nullable=True)


class StrictApproval(Record, Base):
    """Index only; the immutable run and committed artifacts remain the evidence."""
    __tablename__ = "strict_approvals"
    __table_args__ = (UniqueConstraint("run_id"),)
    run_id: Mapped[str] = mapped_column(ForeignKey("runs.id"), index=True)
    input_version_id: Mapped[str] = mapped_column(ForeignKey("dataset_versions.id"), index=True)
    output_version_id: Mapped[str] = mapped_column(ForeignKey("dataset_versions.id"), unique=True)
    contract_id: Mapped[str] = mapped_column(ForeignKey("configurations.id"), index=True)
    contract_revision_id: Mapped[str] = mapped_column(ForeignKey("configurations.id"))
    evidence_hash: Mapped[str] = mapped_column(String(64))
    governance_snapshot: Mapped[dict] = mapped_column(JSON)
