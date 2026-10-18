"""Add bounded diagnostic evidence to native acquisition occurrences."""
import sqlalchemy as sa
from alembic import op

revision = "0026_acquisition_097"
down_revision = "0025_comparison_095"
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table("acquisition_source_configurations") as batch:
        batch.add_column(sa.Column("dispatch_diagnostic", sa.JSON(), nullable=True))
    with op.batch_alter_table("acquisition_occurrences") as batch:
        batch.add_column(sa.Column("dispatched_at", sa.DateTime(timezone=True), nullable=True))
        batch.add_column(sa.Column("coalesced_intervals", sa.Integer(), nullable=False, server_default="0"))
        batch.add_column(sa.Column("reason_message", sa.String(500), nullable=True))
        batch.add_column(sa.Column("error_details", sa.JSON(), nullable=True))


def downgrade():
    bind = op.get_bind()
    if bind.execute(sa.text("SELECT COUNT(*) FROM acquisition_occurrences WHERE dispatched_at IS NOT NULL OR coalesced_intervals <> 0 OR reason_message IS NOT NULL OR (error_details IS NOT NULL AND CAST(error_details AS TEXT) <> 'null')")).scalar() or bind.execute(sa.text("SELECT COUNT(*) FROM acquisition_source_configurations WHERE dispatch_diagnostic IS NOT NULL AND CAST(dispatch_diagnostic AS TEXT) <> 'null'")).scalar():
        raise RuntimeError("ACQUISITION_DIAGNOSTIC_HISTORY_PREVENTS_DOWNGRADE")
    with op.batch_alter_table("acquisition_occurrences") as batch:
        for name in ("error_details", "reason_message", "coalesced_intervals", "dispatched_at"):
            batch.drop_column(name)
    with op.batch_alter_table("acquisition_source_configurations") as batch:
        batch.drop_column("dispatch_diagnostic")
