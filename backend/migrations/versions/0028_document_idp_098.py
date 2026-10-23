"""Add provider administration and immutable per-job inference snapshots."""
import sqlalchemy as sa
from alembic import op

revision = "0028_document_idp_098"
down_revision = "0027_document_builder_097"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table("document_engine_settings",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("organization_id", sa.String(64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("scope_key", sa.String(64), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("configuration", sa.JSON(), nullable=False),
        sa.Column("configuration_hash", sa.String(64), nullable=False),
        sa.Column("actor_id", sa.String(64), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("reason", sa.String(2000), nullable=False),
        sa.UniqueConstraint("organization_id", "scope_key"))
    op.create_index("ix_document_engine_settings_organization_id", "document_engine_settings", ["organization_id"])
    for table in ("document_originals", "document_references"):
        with op.batch_alter_table(table) as batch:
            batch.add_column(sa.Column("engine_snapshot", sa.JSON(), nullable=True))
            batch.add_column(sa.Column("engine_snapshot_hash", sa.String(64), nullable=True))
    with op.batch_alter_table("document_originals") as batch:
        batch.add_column(sa.Column("processing_phase", sa.String(30), nullable=True))


def downgrade():
    # Removing provider provenance after work starts would misrepresent history.
    if op.get_bind().execute(sa.text(
        "SELECT 1 FROM document_engine_settings UNION ALL "
        "SELECT 1 FROM document_originals WHERE engine_snapshot IS NOT NULL OR engine_snapshot_hash IS NOT NULL OR processing_phase IS NOT NULL UNION ALL "
        "SELECT 1 FROM document_references WHERE engine_snapshot IS NOT NULL OR engine_snapshot_hash IS NOT NULL LIMIT 1")).first():
        raise RuntimeError("DOCUMENT_IDP_EVIDENCE_PRESERVATION: restore a compatible backup; provider snapshots are evidence.")
    for table in ("document_originals", "document_references"):
        with op.batch_alter_table(table) as batch:
            batch.drop_column("engine_snapshot_hash")
            batch.drop_column("engine_snapshot")
    with op.batch_alter_table("document_originals") as batch:
        batch.drop_column("processing_phase")
    op.drop_table("document_engine_settings")
