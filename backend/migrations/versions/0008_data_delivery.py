"""Data Delivery destinations, immutable revisions, attempts and job lanes.

Revision ID: 0008_data_delivery
Revises: 0007_monitor_scheduling
"""

import sqlalchemy as sa
from alembic import op

revision = "0008_data_delivery"
down_revision = "0007_monitor_scheduling"
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
        "delivery_destinations",
        *record_columns(),
        sa.Column("name", sa.String(160), nullable=False),
        sa.Column("sink_type", sa.String(30), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("deleted", sa.Boolean(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("last_test_status", sa.String(20), nullable=False),
        sa.Column("last_test_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_test_message", sa.String(240), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_table(
        "delivery_destination_versions",
        *record_columns(),
        sa.Column(
            "destination_id",
            sa.String(64),
            sa.ForeignKey("delivery_destinations.id"),
            nullable=False,
        ),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("config", sa.JSON(), nullable=False),
        sa.Column("secret_reference", sa.String(240), nullable=False),
        sa.Column("config_hash", sa.String(64), nullable=False),
        sa.UniqueConstraint("destination_id", "version"),
    )
    op.create_table(
        "delivery_attempts",
        *record_columns(),
        sa.Column("run_id", sa.String(64), sa.ForeignKey("runs.id"), nullable=False),
        sa.Column(
            "destination_version_id",
            sa.String(64),
            sa.ForeignKey("delivery_destination_versions.id"),
            nullable=False,
        ),
        sa.Column("attempt_number", sa.Integer(), nullable=False),
        sa.Column("idempotency_key", sa.String(64), nullable=False),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("target_locator", sa.String(300), nullable=False),
        sa.Column("rows_attempted", sa.BigInteger(), nullable=True),
        sa.Column("rows_written", sa.BigInteger(), nullable=True),
        sa.Column("rows_inserted", sa.BigInteger(), nullable=True),
        sa.Column("rows_updated", sa.BigInteger(), nullable=True),
        sa.Column("bytes_sent", sa.BigInteger(), nullable=True),
        sa.Column("remote_reference", sa.String(300), nullable=True),
        sa.Column("error_code", sa.String(80), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint("run_id", "attempt_number"),
    )
    op.add_column(
        "jobs",
        sa.Column("lane", sa.String(20), nullable=False, server_default="DEFAULT"),
    )
    op.create_index("ix_jobs_lane", "jobs", ["lane"])
    op.create_index(
        "ix_jobs_lane_status_created_at", "jobs", ["lane", "status", "created_at"]
    )
    for table in (
        "delivery_destinations",
        "delivery_destination_versions",
        "delivery_attempts",
    ):
        op.create_index(f"ix_{table}_organization_id", table, ["organization_id"])
    op.create_index(
        "ix_delivery_destination_versions_destination_id",
        "delivery_destination_versions",
        ["destination_id"],
    )
    op.create_index("ix_delivery_attempts_run_id", "delivery_attempts", ["run_id"])
    op.create_index(
        "ix_delivery_attempts_destination_version_id",
        "delivery_attempts",
        ["destination_version_id"],
    )
    op.create_index(
        "ix_delivery_attempts_idempotency_key",
        "delivery_attempts",
        ["idempotency_key"],
    )
    op.create_index("ix_delivery_attempts_status", "delivery_attempts", ["status"])


def downgrade() -> None:
    op.drop_index("ix_jobs_lane_status_created_at", table_name="jobs")
    op.drop_index("ix_jobs_lane", table_name="jobs")
    op.drop_column("jobs", "lane")
    op.drop_table("delivery_attempts")
    op.drop_table("delivery_destination_versions")
    op.drop_table("delivery_destinations")
