"""Structured, append-only operational observations of UNKNOWN deliveries.

Revision ID: 0009_delivery_reviews
Revises: 0008_data_delivery

No historical attempt, run, metric or artifact is rewritten.
"""

import sqlalchemy as sa
from alembic import op

revision = "0009_delivery_reviews"
down_revision = "0008_data_delivery"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "delivery_reviews",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("organization_id", sa.String(64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("run_id", sa.String(64), sa.ForeignKey("runs.id"), nullable=False),
        sa.Column(
            "delivery_attempt_id", sa.String(64),
            sa.ForeignKey("delivery_attempts.id"), nullable=False,
        ),
        sa.Column("reviewer_id", sa.String(64), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("reviewer_name", sa.String(200), nullable=False),
        sa.Column("outcome", sa.String(40), nullable=False),
        sa.Column("note", sa.String(4000), nullable=False),
        sa.Column("verified_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "outcome IN ('REMOTE_COMMIT_OBSERVED', 'REMOTE_NOT_COMMITTED_OBSERVED', 'INCONCLUSIVE')",
            name="ck_delivery_review_outcome",
        ),
    )
    for column in ("organization_id", "run_id", "delivery_attempt_id", "reviewer_id"):
        op.create_index(f"ix_delivery_reviews_{column}", "delivery_reviews", [column])


def downgrade() -> None:
    op.drop_table("delivery_reviews")
