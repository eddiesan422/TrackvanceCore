"""Metadata-only reusable notification delivery records.

Revision ID: 0011_notification_delivery
Revises: 0010_dynamic_rbac_identity
"""
import sqlalchemy as sa
from alembic import op

revision = "0011_notification_delivery"
down_revision = "0010_dynamic_rbac_identity"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table("notification_deliveries",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("organization_id", sa.String(64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("event_type", sa.String(80), nullable=False),
        sa.Column("template_key", sa.String(80), nullable=False),
        sa.Column("channel", sa.String(20), nullable=False),
        sa.Column("recipient_type", sa.String(20), nullable=False),
        sa.Column("recipient_user_id", sa.String(64), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("recipient_email_snapshot", sa.String(200), nullable=False),
        sa.Column("provider_key", sa.String(40), nullable=False),
        sa.Column("attempt_number", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("error_code", sa.String(80)),
        sa.Column("sent_at", sa.DateTime(timezone=True)),
        sa.Column("failed_at", sa.DateTime(timezone=True)))
    op.create_index("ix_notification_deliveries_organization_id", "notification_deliveries", ["organization_id"])
    op.create_index("ix_notification_deliveries_recipient_user_id", "notification_deliveries", ["recipient_user_id"])


def downgrade() -> None:
    op.drop_table("notification_deliveries")
