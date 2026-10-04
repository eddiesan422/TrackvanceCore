"""Preserve structured acquisition diagnostics without rewriting history."""

import sqlalchemy as sa
from alembic import op

revision = "0016_acquisition_diagnostics"
down_revision = "0015_sentinel_execution_identity"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("acquisition_runs", sa.Column("error_details", sa.JSON(), nullable=True))
    op.add_column("acquisition_runs", sa.Column("error_reference", sa.String(64), nullable=True))


def downgrade():
    op.drop_column("acquisition_runs", "error_reference")
    op.drop_column("acquisition_runs", "error_details")
