"""Durable acquisitions and uploads; one subject per leased job."""

import sqlalchemy as sa
from alembic import op

revision = "0013_async_acquisition"
down_revision = "0012_delivery_target_audit"
branch_labels = None
depends_on = None


def _record():
    return [sa.Column("id", sa.String(64), primary_key=True),
            sa.Column("organization_id", sa.String(64), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False)]


def upgrade() -> None:
    op.create_table("acquisition_uploads", *_record(),
        sa.Column("user_id", sa.String(64), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("filename", sa.String(240), nullable=False),
        sa.Column("path", sa.Text(), nullable=False),
        sa.Column("sha256", sa.String(64), nullable=False),
        sa.Column("size_bytes", sa.BigInteger(), nullable=False),
        sa.Column("source_format", sa.String(30), nullable=False),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("status IN ('RECEIVED','REGISTERED','CONSUMED','EXPIRED')", name="ck_acquisition_upload_status"))
    for column in ("organization_id", "user_id", "expires_at"):
        op.create_index(f"ix_acquisition_uploads_{column}", "acquisition_uploads", [column])
    op.create_table("acquisition_runs", *_record(),
        sa.Column("dataset_id", sa.String(64), sa.ForeignKey("datasets.id"), nullable=False),
        sa.Column("upload_id", sa.String(64), sa.ForeignKey("acquisition_uploads.id"), nullable=True, unique=True),
        sa.Column("connection_version_id", sa.String(64), sa.ForeignKey("external_connection_versions.id"), nullable=True),
        sa.Column("source_type", sa.String(30), nullable=False),
        sa.Column("filename", sa.String(240), nullable=False),
        sa.Column("source_snapshot", sa.JSON(), nullable=False),
        sa.Column("reader_options", sa.JSON(), nullable=False),
        sa.Column("column_overrides", sa.JSON(), nullable=False),
        sa.Column("effective_limits", sa.JSON(), nullable=False),
        sa.Column("request_hash", sa.String(64), nullable=False),
        sa.Column("idempotency_key", sa.String(128), nullable=True),
        sa.Column("initiated_by_id", sa.String(64), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("initiated_by_name", sa.String(200), nullable=False),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("stage", sa.String(50), nullable=False),
        sa.Column("attempt_id", sa.String(64), nullable=True),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("processed_rows", sa.BigInteger(), nullable=False),
        sa.Column("processed_bytes", sa.BigInteger(), nullable=False),
        sa.Column("total_rows", sa.BigInteger(), nullable=True),
        sa.Column("total_bytes", sa.BigInteger(), nullable=True),
        sa.Column("cancel_requested", sa.Boolean(), nullable=False),
        sa.Column("output_version_id", sa.String(64), sa.ForeignKey("dataset_versions.id"), nullable=True, unique=True),
        sa.Column("error_code", sa.String(80), nullable=True),
        sa.Column("error_message", sa.String(500), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint("organization_id", "initiated_by_id", "idempotency_key"),
        sa.CheckConstraint("status IN ('QUEUED','RUNNING','SUCCESS','FAILED','CANCELLED')", name="ck_acquisition_status"),
        sa.CheckConstraint("(source_type = 'UPLOAD' AND upload_id IS NOT NULL AND connection_version_id IS NULL) OR (source_type IN ('POSTGRESQL','SQLSERVER') AND upload_id IS NULL AND connection_version_id IS NOT NULL)", name="ck_acquisition_source_identity"))
    for column in ("organization_id", "dataset_id", "initiated_by_id", "status"):
        op.create_index(f"ix_acquisition_runs_{column}", "acquisition_runs", [column])
    op.create_index("ix_acquisition_org_dataset_created", "acquisition_runs", ["organization_id", "dataset_id", "created_at"])
    with op.batch_alter_table("jobs") as batch:
        batch.alter_column("run_id", existing_type=sa.String(64), nullable=True)
        batch.add_column(sa.Column("acquisition_id", sa.String(64), nullable=True))
        batch.create_foreign_key("fk_jobs_acquisition", "acquisition_runs", ["acquisition_id"], ["id"])
        batch.create_unique_constraint("uq_jobs_acquisition", ["acquisition_id"])
        batch.create_check_constraint("ck_job_subject_lane", "(run_id IS NOT NULL AND acquisition_id IS NULL AND lane IN ('DEFAULT','DELIVERY')) OR (run_id IS NULL AND acquisition_id IS NOT NULL AND lane = 'ACQUISITION')")


def downgrade() -> None:
    # Never turn acquisitions into fictitious runs. Downgrade is only supported
    # on a quiescent disposable database after removing the new job subjects.
    connection = op.get_bind()
    if connection.execute(sa.text("SELECT COUNT(*) FROM jobs WHERE acquisition_id IS NOT NULL")).scalar():
        raise RuntimeError("La base contiene trabajos de adquisición; no admite downgrade sin preservarlos.")
    with op.batch_alter_table("jobs") as batch:
        batch.drop_constraint("ck_job_subject_lane", type_="check")
        batch.drop_constraint("uq_jobs_acquisition", type_="unique")
        batch.drop_constraint("fk_jobs_acquisition", type_="foreignkey")
        batch.drop_column("acquisition_id")
        batch.alter_column("run_id", existing_type=sa.String(64), nullable=False)
    op.drop_table("acquisition_runs")
    op.drop_table("acquisition_uploads")
