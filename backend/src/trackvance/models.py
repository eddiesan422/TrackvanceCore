from datetime import datetime
from typing import Any
from uuid import uuid4

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    DateTime,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from .config import ORG_ID
from .db import Base, utcnow


def uid() -> str:
    return str(uuid4())


class Record:
    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=uid)
    organization_id: Mapped[str] = mapped_column(String(64), default=ORG_ID, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class User(Record, Base):
    __tablename__ = "users"
    name: Mapped[str] = mapped_column(String(200))
    email: Mapped[str] = mapped_column(String(200), unique=True)
    role: Mapped[str] = mapped_column(String(40), default="Administrator")
    password_hash: Mapped[str] = mapped_column(Text)
    active: Mapped[bool] = mapped_column(Boolean, default=True)


class AuthSession(Record, Base):
    __tablename__ = "sessions"
    token_hash: Mapped[str] = mapped_column(String(64), unique=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"))
    csrf_token: Mapped[str] = mapped_column(String(100))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class Dataset(Record, Base):
    __tablename__ = "datasets"
    __table_args__ = (UniqueConstraint("organization_id", "name"),)
    name: Mapped[str] = mapped_column(String(160))
    description: Mapped[str] = mapped_column(Text, default="")
    domain: Mapped[str] = mapped_column(String(80), default="Operaciones")
    owner: Mapped[str] = mapped_column(String(120), default="Equipo de datos")
    criticality: Mapped[str] = mapped_column(String(20), default="HIGH")
    status: Mapped[str] = mapped_column(String(20), default="ACTIVE")


class DatasetVersion(Record, Base):
    __tablename__ = "dataset_versions"
    __table_args__ = (UniqueConstraint("dataset_id", "version"),)
    dataset_id: Mapped[str] = mapped_column(ForeignKey("datasets.id"), index=True)
    version: Mapped[int] = mapped_column(Integer)
    filename: Mapped[str] = mapped_column(String(240))
    source_type: Mapped[str] = mapped_column(String(30), default="UPLOAD")
    sha256: Mapped[str] = mapped_column(String(64))
    schema_hash: Mapped[str] = mapped_column(String(64))
    size_bytes: Mapped[int] = mapped_column(Integer)
    row_count: Mapped[int] = mapped_column(Integer)
    column_count: Mapped[int] = mapped_column(Integer)
    profile_status: Mapped[str] = mapped_column(String(20), default="READY")
    original_path: Mapped[str] = mapped_column(Text)
    canonical_path: Mapped[str] = mapped_column(Text)
    schema_json: Mapped[list] = mapped_column(JSON)
    profile: Mapped[dict] = mapped_column(JSON)
    ingestion_metadata: Mapped[dict] = mapped_column(JSON, default=dict)
    parent_version_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    original_artifact_id: Mapped[str | None] = mapped_column(ForeignKey("artifacts.id"), nullable=True)
    canonical_artifact_id: Mapped[str | None] = mapped_column(ForeignKey("artifacts.id"), nullable=True)
    source_run_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)


class Configuration(Record, Base):
    """Immutable published configuration snapshot; updates create a new row/version."""
    __tablename__ = "configurations"
    name: Mapped[str] = mapped_column(String(160))
    module: Mapped[str] = mapped_column(String(20), index=True)
    version: Mapped[int] = mapped_column(Integer, default=1)
    dataset_id: Mapped[str] = mapped_column(ForeignKey("datasets.id"))
    target_dataset_id: Mapped[str | None] = mapped_column(ForeignKey("datasets.id"), nullable=True)
    owner: Mapped[str] = mapped_column(String(120), default="Equipo de datos")
    description: Mapped[str] = mapped_column(Text, default="")
    status: Mapped[str] = mapped_column(String(20), default="PUBLISHED")
    config: Mapped[dict] = mapped_column(JSON)
    previous_version_id: Mapped[str | None] = mapped_column(ForeignKey("configurations.id"), nullable=True, unique=True, index=True)


class Run(Record, Base):
    __tablename__ = "runs"
    module: Mapped[str] = mapped_column(String(20), index=True)
    name: Mapped[str] = mapped_column(String(160))
    status: Mapped[str] = mapped_column(String(30), default="QUEUED", index=True)
    decision: Mapped[str | None] = mapped_column(String(40), nullable=True)
    config_id: Mapped[str] = mapped_column(ForeignKey("configurations.id"))
    dataset_version_id: Mapped[str] = mapped_column(ForeignKey("dataset_versions.id"))
    target_version_id: Mapped[str | None] = mapped_column(ForeignKey("dataset_versions.id"), nullable=True)
    initiated_by: Mapped[str] = mapped_column(String(64))
    initiated_by_type: Mapped[str] = mapped_column(String(20), default="SYSTEM")
    initiated_by_id: Mapped[str] = mapped_column(String(64), default="system:legacy")
    initiated_by_legacy: Mapped[bool] = mapped_column(Boolean, default=False)
    progress_stage: Mapped[str] = mapped_column(String(50), default="En cola")
    progress_percent: Mapped[int] = mapped_column(Integer, default=0)
    metrics: Mapped[dict] = mapped_column(JSON, default=dict)
    execution_plan: Mapped[dict] = mapped_column(JSON, default=dict)
    result_path: Mapped[str | None] = mapped_column(Text, nullable=True)
    evidence_path: Mapped[str | None] = mapped_column(Text, nullable=True)
    output_version_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    cancel_requested: Mapped[bool] = mapped_column(Boolean, default=False)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class Job(Record, Base):
    __tablename__ = "jobs"
    run_id: Mapped[str] = mapped_column(ForeignKey("runs.id"), unique=True)
    status: Mapped[str] = mapped_column(String(20), default="QUEUED", index=True)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    lease_owner: Mapped[str | None] = mapped_column(String(64), nullable=True)
    lease_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)


class Finding(Record, Base):
    __tablename__ = "findings"
    __table_args__ = (UniqueConstraint("run_id", "fingerprint"),)
    run_id: Mapped[str] = mapped_column(ForeignKey("runs.id"), index=True)
    title: Mapped[str] = mapped_column(String(240))
    code: Mapped[str] = mapped_column(String(80))
    severity: Mapped[str] = mapped_column(String(20))
    fingerprint: Mapped[str] = mapped_column(String(64))
    details: Mapped[dict] = mapped_column(JSON)


class ExceptionCase(Record, Base):
    __tablename__ = "exceptions"
    display_id: Mapped[str] = mapped_column(String(30), unique=True)
    finding_id: Mapped[str | None] = mapped_column(ForeignKey("findings.id"), nullable=True, unique=True)
    run_id: Mapped[str] = mapped_column(ForeignKey("runs.id"))
    configuration_id: Mapped[str] = mapped_column(ForeignKey("configurations.id"), index=True)
    validation_run_id: Mapped[str | None] = mapped_column(ForeignKey("runs.id"), nullable=True, index=True)
    title: Mapped[str] = mapped_column(String(240))
    module: Mapped[str] = mapped_column(String(20))
    severity: Mapped[str] = mapped_column(String(20))
    state: Mapped[str] = mapped_column(String(30), default="OPEN", index=True)
    owner: Mapped[str] = mapped_column(String(120), default="Sin asignar")
    root_cause: Mapped[str] = mapped_column(Text, default="")
    resolution: Mapped[str] = mapped_column(Text, default="")
    administrative_reason: Mapped[str] = mapped_column(Text, default="")
    validation_evidence: Mapped[dict] = mapped_column(JSON, default=dict)
    validated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    version: Mapped[int] = mapped_column(Integer, default=1)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    events: Mapped[list] = mapped_column(JSON, default=list)


class AuditEvent(Record, Base):
    __tablename__ = "audit_events"
    event_type: Mapped[str] = mapped_column(String(80))
    actor: Mapped[str] = mapped_column(String(120))
    actor_type: Mapped[str] = mapped_column(String(20), default="SYSTEM")
    actor_id: Mapped[str] = mapped_column(String(64), default="system:legacy")
    actor_legacy: Mapped[bool] = mapped_column(Boolean, default=False)
    request_id: Mapped[str | None] = mapped_column(String(128), nullable=True, index=True)
    run_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    subject_type: Mapped[str] = mapped_column(String(60))
    subject_id: Mapped[str] = mapped_column(String(64))
    message: Mapped[str] = mapped_column(Text)
    metadata_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)


class IdempotencyKey(Record, Base):
    __tablename__ = "idempotency_keys"
    __table_args__ = (UniqueConstraint("organization_id", "route", "key"),)
    route: Mapped[str] = mapped_column(String(180))
    key: Mapped[str] = mapped_column(String(128))
    request_hash: Mapped[str] = mapped_column(String(64))
    run_id: Mapped[str] = mapped_column(ForeignKey("runs.id"))


class Artifact(Record, Base):
    """Immutable file identity. A path is internal metadata and never a client filename."""

    __tablename__ = "artifacts"
    kind: Mapped[str] = mapped_column(String(40), index=True)
    name: Mapped[str] = mapped_column(String(240))
    path: Mapped[str] = mapped_column(Text, unique=True)
    sha256: Mapped[str] = mapped_column(String(64))
    size_bytes: Mapped[int] = mapped_column(BigInteger)
    media_type: Mapped[str] = mapped_column(String(120))


class ArtifactLink(Record, Base):
    """Directed lineage: source entity has relation to target entity."""

    __tablename__ = "artifact_links"
    __table_args__ = (UniqueConstraint("organization_id", "relation", "source_type", "source_id", "target_type", "target_id", name="uq_artifact_link"),)
    relation: Mapped[str] = mapped_column(String(40))
    source_type: Mapped[str] = mapped_column(String(40))
    source_id: Mapped[str] = mapped_column(String(64), index=True)
    target_type: Mapped[str] = mapped_column(String(40))
    target_id: Mapped[str] = mapped_column(String(64), index=True)


class SentinelMetricHistory(Record, Base):
    __tablename__ = "metric_history"
    __table_args__ = (UniqueConstraint("run_id", "metric_key", "dimension_hash", name="uq_metric_run_key"),)
    monitor_id: Mapped[str] = mapped_column(ForeignKey("configurations.id"), index=True)
    run_id: Mapped[str] = mapped_column(ForeignKey("runs.id"), index=True)
    metric_key: Mapped[str] = mapped_column(String(200), index=True)
    dimensions: Mapped[dict] = mapped_column(JSON, default=dict)
    dimension_hash: Mapped[str] = mapped_column(String(64))
    numeric_value: Mapped[float | int | None] = mapped_column(JSON, nullable=True)
    method: Mapped[str] = mapped_column(String(60))
    metric_definition_version: Mapped[int] = mapped_column(Integer)
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
