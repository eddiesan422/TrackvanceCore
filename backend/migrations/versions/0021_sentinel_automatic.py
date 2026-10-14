"""Dataset monitor identities, exact-version dispatch and metadata-only reviews."""

import sqlalchemy as sa
from alembic import op

revision = "0021_sentinel_automatic"
down_revision = "0020_rest_connectors"
branch_labels = None
depends_on = None


def common():
    return [sa.Column("id", sa.String(64), primary_key=True),
            sa.Column("organization_id", sa.String(64), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False)]


def upgrade():
    op.create_table("sentinel_automatic_monitors", *common(),
        sa.Column("dataset_id", sa.String(64), sa.ForeignKey("datasets.id"), nullable=False, unique=True),
        sa.Column("monitor_root_id", sa.String(64), sa.ForeignKey("configurations.id"), nullable=False, unique=True),
        sa.Column("responsible_user_id", sa.String(64), sa.ForeignKey("users.id"), nullable=True),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("review_interval_seconds", sa.Integer(), nullable=False),
        sa.Column("next_review_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False))
    op.create_table("sentinel_version_evaluations", *common(),
        sa.Column("automatic_monitor_id", sa.String(64), sa.ForeignKey("sentinel_automatic_monitors.id"), nullable=False),
        sa.Column("dataset_version_id", sa.String(64), sa.ForeignKey("dataset_versions.id"), nullable=False),
        sa.Column("monitor_id", sa.String(64), sa.ForeignKey("configurations.id"), nullable=False),
        sa.Column("event_id", sa.String(64), nullable=True),
        sa.Column("run_id", sa.String(64), sa.ForeignKey("runs.id"), nullable=True, unique=True),
        sa.Column("status", sa.String(30), nullable=False),
        sa.Column("reason_code", sa.String(80), nullable=True),
        sa.UniqueConstraint("automatic_monitor_id", "dataset_version_id", name="uq_sentinel_auto_version"))
    op.create_table("sentinel_light_reviews", *common(),
        sa.Column("automatic_monitor_id", sa.String(64), sa.ForeignKey("sentinel_automatic_monitors.id"), nullable=False),
        sa.Column("dataset_version_id", sa.String(64), sa.ForeignKey("dataset_versions.id"), nullable=True),
        sa.Column("planned_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("observed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("status", sa.String(30), nullable=False),
        sa.Column("reason_code", sa.String(80), nullable=True),
        sa.Column("metrics", sa.JSON(), nullable=False),
        sa.UniqueConstraint("automatic_monitor_id", "planned_at", name="uq_sentinel_light_slot"))
    op.create_table("sentinel_automatic_revisions", *common(),
        sa.Column("automatic_monitor_id", sa.String(64), sa.ForeignKey("sentinel_automatic_monitors.id"), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("actor_id", sa.String(64), sa.ForeignKey("users.id"), nullable=True),
        sa.Column("settings", sa.JSON(), nullable=False),
        sa.UniqueConstraint("automatic_monitor_id", "version", name="uq_sentinel_auto_revision"))
    for table in ("sentinel_automatic_monitors", "sentinel_version_evaluations", "sentinel_light_reviews", "sentinel_automatic_revisions"):
        op.create_index(f"ix_{table}_organization_id", table, ["organization_id"])
    op.create_index("ix_sentinel_automatic_monitors_next_review_at", "sentinel_automatic_monitors", ["next_review_at"])
    op.create_index("ix_sentinel_version_evaluations_automatic_monitor_id", "sentinel_version_evaluations", ["automatic_monitor_id"])
    op.create_index("ix_sentinel_version_evaluations_dataset_version_id", "sentinel_version_evaluations", ["dataset_version_id"])
    op.create_index("ix_sentinel_light_reviews_automatic_monitor_id", "sentinel_light_reviews", ["automatic_monitor_id"])
    op.create_index("ix_sentinel_automatic_revisions_automatic_monitor_id", "sentinel_automatic_revisions", ["automatic_monitor_id"])


def downgrade():
    for table in ("sentinel_automatic_revisions", "sentinel_light_reviews", "sentinel_version_evaluations", "sentinel_automatic_monitors"):
        op.drop_table(table)
