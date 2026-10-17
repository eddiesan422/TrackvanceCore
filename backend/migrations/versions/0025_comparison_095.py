"""Durable full-population version comparisons using the native fenced job queue."""
import sqlalchemy as sa
from alembic import op

revision = "0025_comparison_095"
down_revision = "0024_documents_095"
branch_labels = None
depends_on = None


def _constraint(comparison=False):
    common = " AND comparison_id IS NULL" if comparison else ""
    return (
        "(run_id IS NOT NULL AND acquisition_id IS NULL AND report_execution_id IS NULL AND document_id IS NULL" + common + " AND lane IN ('DEFAULT','DELIVERY')) OR "
        "(run_id IS NULL AND acquisition_id IS NOT NULL AND report_execution_id IS NULL AND document_id IS NULL" + common + " AND lane='ACQUISITION') OR "
        "(run_id IS NULL AND acquisition_id IS NULL AND report_execution_id IS NOT NULL AND document_id IS NULL" + common + " AND lane='REPORT') OR "
        "(run_id IS NULL AND acquisition_id IS NULL AND report_execution_id IS NULL AND document_id IS NOT NULL" + common + " AND lane='DOCUMENT')"
        + (" OR (run_id IS NULL AND acquisition_id IS NULL AND report_execution_id IS NULL AND document_id IS NULL AND comparison_id IS NOT NULL AND lane='COMPARISON')" if comparison else "")
    )


def upgrade():
    op.create_table("version_comparisons",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("organization_id", sa.String(64), nullable=False, index=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("before_version_id", sa.String(64), sa.ForeignKey("dataset_versions.id"), nullable=False, index=True),
        sa.Column("after_version_id", sa.String(64), sa.ForeignKey("dataset_versions.id"), nullable=False, index=True),
        sa.Column("actor_id", sa.String(64), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("settings", sa.JSON(), nullable=False), sa.Column("status", sa.String(30), nullable=False, index=True),
        sa.Column("progress_percent", sa.Integer(), nullable=False),
        sa.Column("progress_stage", sa.String(160), nullable=False),
        sa.Column("cancel_requested", sa.Boolean(), nullable=False),
        sa.Column("summary", sa.JSON(), nullable=False),
        sa.Column("artifact_id", sa.String(64), sa.ForeignKey("artifacts.id"), nullable=True),
        sa.Column("error_code", sa.String(80), nullable=True), sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True))
    with op.batch_alter_table("jobs") as batch:
        batch.add_column(sa.Column("comparison_id", sa.String(64), nullable=True))
        batch.create_foreign_key("fk_jobs_comparison_id", "version_comparisons", ["comparison_id"], ["id"])
        batch.create_unique_constraint("uq_jobs_comparison_id", ["comparison_id"])
        batch.drop_constraint("ck_job_subject_lane", type_="check")
        batch.create_check_constraint("ck_job_subject_lane", _constraint(True))


def downgrade():
    bind = op.get_bind()
    if bind.execute(sa.text("SELECT COUNT(*) FROM version_comparisons")).scalar():
        raise RuntimeError("No se puede retirar evidencia histórica de comparaciones.")
    with op.batch_alter_table("jobs") as batch:
        batch.drop_constraint("ck_job_subject_lane", type_="check")
        batch.drop_constraint("uq_jobs_comparison_id", type_="unique")
        batch.drop_constraint("fk_jobs_comparison_id", type_="foreignkey")
        batch.drop_column("comparison_id")
        batch.create_check_constraint("ck_job_subject_lane", _constraint())
    op.drop_table("version_comparisons")
