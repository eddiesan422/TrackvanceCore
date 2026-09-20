"""Local schedules with immutable settings and durable, idempotent occurrences."""

import sqlalchemy as sa
from alembic import op

revision = "0007_monitor_scheduling"
down_revision = "0006_local_identity_exceptions"
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
        "monitor_schedules", *record_columns(),
        sa.Column("monitor_id", sa.String(64), sa.ForeignKey("configurations.id"), nullable=False, unique=True),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("next_run_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_table(
        "monitor_schedule_versions", *record_columns(),
        sa.Column("schedule_id", sa.String(64), sa.ForeignKey("monitor_schedules.id"), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("interval_seconds", sa.Integer(), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("starts_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("actor_id", sa.String(64), nullable=False),
        sa.UniqueConstraint("schedule_id", "version"),
    )
    op.create_table(
        "monitor_occurrences", *record_columns(),
        sa.Column("schedule_id", sa.String(64), sa.ForeignKey("monitor_schedules.id"), nullable=False),
        sa.Column("schedule_version_id", sa.String(64), sa.ForeignKey("monitor_schedule_versions.id"), nullable=False),
        sa.Column("monitor_id", sa.String(64), sa.ForeignKey("configurations.id"), nullable=False),
        sa.Column("dataset_version_id", sa.String(64), sa.ForeignKey("dataset_versions.id"), nullable=True),
        sa.Column("run_id", sa.String(64), sa.ForeignKey("runs.id"), nullable=True, unique=True),
        sa.Column("planned_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("dispatched_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("status", sa.String(30), nullable=False),
        sa.Column("reason_code", sa.String(60), nullable=True),
        sa.Column("coalesced_intervals", sa.Integer(), nullable=False),
        sa.UniqueConstraint("schedule_id", "planned_at"),
    )
    for table in ("monitor_schedules", "monitor_schedule_versions", "monitor_occurrences"):
        op.create_index(f"ix_{table}_organization_id", table, ["organization_id"])
    op.create_index("ix_monitor_schedules_next_run_at", "monitor_schedules", ["next_run_at"])
    op.create_index("ix_monitor_schedule_versions_schedule_id", "monitor_schedule_versions", ["schedule_id"])
    op.create_index("ix_monitor_occurrences_schedule_id", "monitor_occurrences", ["schedule_id"])
    op.create_index("ix_monitor_occurrences_monitor_id", "monitor_occurrences", ["monitor_id"])


def downgrade() -> None:
    op.drop_table("monitor_occurrences")
    op.drop_table("monitor_schedule_versions")
    op.drop_table("monitor_schedules")
