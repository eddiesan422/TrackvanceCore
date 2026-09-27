"""Irreversible physical target audit policy and immutable attempt snapshots."""

import sqlalchemy as sa
from alembic import op

revision = "0012_delivery_target_audit"
down_revision = "0011_notification_delivery"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "delivery_target_policies",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("organization_id", sa.String(64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("destination_id", sa.String(64), sa.ForeignKey("delivery_destinations.id"), nullable=False),
        sa.Column("target_fingerprint", sa.String(64), nullable=False),
        sa.Column("sink_type", sa.String(30), nullable=False),
        sa.Column("host_snapshot", sa.String(253), nullable=False),
        sa.Column("port", sa.Integer(), nullable=False),
        sa.Column("database", sa.String(128), nullable=False),
        sa.Column("schema_name", sa.String(128), nullable=False),
        sa.Column("table_name", sa.String(128), nullable=False),
        sa.Column("audit_columns_required", sa.Boolean(), nullable=False),
        sa.Column("enabled_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("enabled_by_user_id", sa.String(64), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("enabled_by_username", sa.String(128), nullable=False),
        sa.Column("materialized_at", sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint("organization_id", "target_fingerprint"),
        sa.CheckConstraint("audit_columns_required = true", name="ck_delivery_audit_required"),
    )
    for column in ("organization_id", "destination_id"):
        op.create_index(f"ix_delivery_target_policies_{column}", "delivery_target_policies", [column])
    op.add_column("delivery_attempts", sa.Column("system_audit", sa.JSON(), nullable=False, server_default="{}"))
    with op.batch_alter_table("delivery_attempts") as batch:
        batch.alter_column("system_audit", server_default=None)


def downgrade() -> None:
    # Controlled metadata downgrade cannot remove columns already published to
    # independent remote databases; those remain under the target owner's control.
    with op.batch_alter_table("delivery_attempts") as batch:
        batch.drop_column("system_audit")
    op.drop_table("delivery_target_policies")
