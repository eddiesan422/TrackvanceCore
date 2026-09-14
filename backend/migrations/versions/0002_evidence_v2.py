"""Add immutable artifacts, structured actors, lineage and versioned metrics.

Revision ID: 0002_evidence_v2
Revises: 0001_initial

Existing run/configuration/profile JSON and every stored artifact remain unchanged.
File registration is performed by the idempotent startup backfill after this schema
migration. Original display names are retained; uniquely matching users can be
resolved, while ambiguous historical names receive an explicit legacy identity.
"""

from uuid import NAMESPACE_URL, uuid5

import sqlalchemy as sa
from alembic import op

revision = "0002_evidence_v2"
down_revision = "0001_initial"
branch_labels = None
depends_on = None


def _record_columns():
    return [sa.Column("id", sa.String(64), primary_key=True),
            sa.Column("organization_id", sa.String(64), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False)]


def _nullable_reference(table: str, name: str, destination: str) -> None:
    if op.get_bind().dialect.name == "sqlite":
        # ADD nullable REFERENCES is supported natively by SQLite. This avoids
        # rebuilding a parent table, preserving all incoming foreign keys.
        op.execute(f'ALTER TABLE "{table}" ADD COLUMN "{name}" VARCHAR(64) REFERENCES "{destination}" (id)')
    else:
        op.add_column(table, sa.Column(name, sa.String(64), sa.ForeignKey(f"{destination}.id", name=f"fk_{table}_{name}"), nullable=True))


def _backfill_actor_columns() -> None:
    connection = op.get_bind()
    metadata = sa.MetaData()
    users = sa.Table("users", metadata, autoload_with=connection)
    identities: dict[tuple[str, str], list[str]] = {}
    for user in connection.execute(sa.select(users.c.organization_id, users.c.name, users.c.id)).mappings():
        identities.setdefault((user["organization_id"], user["name"]), []).append(user["id"])
    for table_name, display_column, type_column, id_column, legacy_column in [
        ("runs", "initiated_by", "initiated_by_type", "initiated_by_id", "initiated_by_legacy"),
        ("audit_events", "actor", "actor_type", "actor_id", "actor_legacy"),
    ]:
        table = sa.Table(table_name, metadata, autoload_with=connection)
        for row in connection.execute(sa.select(table)).mappings():
            display = row[display_column]
            matches = identities.get((row["organization_id"], display), [])
            identity_type = "USER" if len(matches) == 1 else "WORKER" if display.casefold() == "worker" else "SYSTEM"
            identity_id = matches[0] if len(matches) == 1 else str(uuid5(NAMESPACE_URL, f"trackvance:legacy-actor:{row['organization_id']}:{display}"))
            values = {type_column: identity_type, id_column: identity_id, legacy_column: True}
            if table_name == "audit_events" and row["subject_type"] == "run":
                values["run_id"] = row["subject_id"]
            connection.execute(table.update().where(table.c.id == row["id"]).values(**values))


def upgrade() -> None:
    op.create_table("artifacts", *_record_columns(),
        sa.Column("kind", sa.String(40), nullable=False),
        sa.Column("name", sa.String(240), nullable=False),
        sa.Column("path", sa.Text(), nullable=False, unique=True),
        sa.Column("sha256", sa.String(64), nullable=False),
        sa.Column("size_bytes", sa.BigInteger(), nullable=False),
        sa.Column("media_type", sa.String(120), nullable=False))
    op.create_index("ix_artifacts_organization_id", "artifacts", ["organization_id"])
    op.create_index("ix_artifacts_kind", "artifacts", ["kind"])
    op.create_table("artifact_links", *_record_columns(),
        sa.Column("relation", sa.String(40), nullable=False),
        sa.Column("source_type", sa.String(40), nullable=False),
        sa.Column("source_id", sa.String(64), nullable=False),
        sa.Column("target_type", sa.String(40), nullable=False),
        sa.Column("target_id", sa.String(64), nullable=False),
        sa.UniqueConstraint("organization_id", "relation", "source_type", "source_id", "target_type", "target_id", name="uq_artifact_link"))
    for name in ["organization_id", "source_id", "target_id"]:
        op.create_index(f"ix_artifact_links_{name}", "artifact_links", [name])
    _nullable_reference("dataset_versions", "original_artifact_id", "artifacts")
    _nullable_reference("dataset_versions", "canonical_artifact_id", "artifacts")
    op.add_column("dataset_versions", sa.Column("source_run_id", sa.String(64), nullable=True))
    op.create_index("ix_dataset_versions_source_run_id", "dataset_versions", ["source_run_id"])
    _nullable_reference("configurations", "previous_version_id", "configurations")
    op.create_index("ix_configurations_previous_version_id", "configurations", ["previous_version_id"], unique=True)
    for table, prefix in [("runs", "initiated_by"), ("audit_events", "actor")]:
        op.add_column(table, sa.Column(f"{prefix}_type", sa.String(20), nullable=False, server_default="SYSTEM"))
        op.add_column(table, sa.Column(f"{prefix}_id", sa.String(64), nullable=False, server_default="system:legacy"))
        op.add_column(table, sa.Column(f"{prefix}_legacy", sa.Boolean(), nullable=False, server_default=sa.false()))
    op.add_column("audit_events", sa.Column("request_id", sa.String(128), nullable=True))
    op.add_column("audit_events", sa.Column("run_id", sa.String(64), nullable=True))
    op.create_index("ix_audit_events_request_id", "audit_events", ["request_id"])
    op.create_index("ix_audit_events_run_id", "audit_events", ["run_id"])
    op.create_table("metric_history", *_record_columns(),
        sa.Column("monitor_id", sa.String(64), sa.ForeignKey("configurations.id"), nullable=False),
        sa.Column("run_id", sa.String(64), sa.ForeignKey("runs.id"), nullable=False),
        sa.Column("metric_key", sa.String(200), nullable=False),
        sa.Column("dimensions", sa.JSON(), nullable=False),
        sa.Column("dimension_hash", sa.String(64), nullable=False),
        sa.Column("numeric_value", sa.JSON(), nullable=True),
        sa.Column("method", sa.String(60), nullable=False),
        sa.Column("metric_definition_version", sa.Integer(), nullable=False),
        sa.Column("observed_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("run_id", "metric_key", "dimension_hash", name="uq_metric_run_key"))
    for name in ["organization_id", "monitor_id", "run_id", "metric_key", "observed_at"]:
        op.create_index(f"ix_metric_history_{name}", "metric_history", [name])
    _backfill_actor_columns()


def downgrade() -> None:
    op.drop_table("metric_history")
    for name in ["request_id", "run_id"]:
        op.drop_index(f"ix_audit_events_{name}", table_name="audit_events")
        op.drop_column("audit_events", name)
    for table, prefix in [("runs", "initiated_by"), ("audit_events", "actor")]:
        for suffix in ["legacy", "id", "type"]:
            op.drop_column(table, f"{prefix}_{suffix}")
    op.drop_index("ix_configurations_previous_version_id", table_name="configurations")
    if op.get_bind().dialect.name != "sqlite":
        op.drop_constraint("fk_configurations_previous_version_id", "configurations", type_="foreignkey")
    op.drop_column("configurations", "previous_version_id")
    op.drop_index("ix_dataset_versions_source_run_id", table_name="dataset_versions")
    op.drop_column("dataset_versions", "source_run_id")
    for name in ["canonical_artifact_id", "original_artifact_id"]:
        if op.get_bind().dialect.name != "sqlite":
            op.drop_constraint(f"fk_dataset_versions_{name}", "dataset_versions", type_="foreignkey")
        op.drop_column("dataset_versions", name)
    op.drop_table("artifact_links")
    op.drop_table("artifacts")
