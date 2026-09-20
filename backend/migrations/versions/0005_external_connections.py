"""Organization-scoped external sources and immutable connection configurations.

Revision ID: 0005_external_connections
Revises: 0004_exception_validation
"""

import sqlalchemy as sa
from alembic import op

revision = "0005_external_connections"
down_revision = "0004_exception_validation"
branch_labels = None
depends_on = None


def record_columns() -> list[sa.Column]:
    return [
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("organization_id", sa.String(64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    ]


def upgrade() -> None:
    op.create_table(
        "external_connections", *record_columns(),
        sa.Column("name", sa.String(160), nullable=False),
        sa.Column("source_type", sa.String(30), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("deleted", sa.Boolean(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("last_test_status", sa.String(20), nullable=False),
        sa.Column("last_test_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_test_message", sa.String(240), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_table(
        "external_connection_versions", *record_columns(),
        sa.Column("connection_id", sa.String(64), sa.ForeignKey("external_connections.id"), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("host", sa.String(253), nullable=False),
        sa.Column("port", sa.Integer(), nullable=False),
        sa.Column("database", sa.String(128), nullable=False),
        sa.Column("username", sa.String(128), nullable=False),
        sa.Column("options", sa.JSON(), nullable=False),
        sa.Column("secret_reference", sa.String(240), nullable=False),
        sa.Column("config_hash", sa.String(64), nullable=False),
        sa.UniqueConstraint("connection_id", "version"),
    )
    op.create_table(
        "dataset_source_bindings", *record_columns(),
        sa.Column("dataset_id", sa.String(64), sa.ForeignKey("datasets.id"), nullable=False, unique=True),
        sa.Column("connection_id", sa.String(64), sa.ForeignKey("external_connections.id"), nullable=False),
        sa.Column("schema_name", sa.String(128), nullable=False),
        sa.Column("object_name", sa.String(128), nullable=False),
        sa.Column("object_kind", sa.String(20), nullable=False),
        sa.Column("column_overrides", sa.JSON(), nullable=False),
    )
    for table in ("external_connections", "external_connection_versions", "dataset_source_bindings"):
        op.create_index(f"ix_{table}_organization_id", table, ["organization_id"])
    for table in ("external_connection_versions", "dataset_source_bindings"):
        op.create_index(f"ix_{table}_connection_id", table, ["connection_id"])


def downgrade() -> None:
    op.drop_table("dataset_source_bindings")
    op.drop_table("external_connection_versions")
    op.drop_table("external_connections")
