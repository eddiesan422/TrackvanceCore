"""Add REST acquisition identities and durable remote batch evidence."""

import sqlalchemy as sa
from alembic import op

revision = "0020_rest_connectors"
down_revision = "0019_governance_people"
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table("acquisition_runs") as batch:
        batch.drop_constraint("ck_acquisition_source_identity", type_="check")
        batch.create_check_constraint("ck_acquisition_source_identity", "(source_type = 'UPLOAD' AND upload_id IS NOT NULL AND connection_version_id IS NULL) OR (source_type IN ('POSTGRESQL','SQLSERVER','REST') AND upload_id IS NULL AND connection_version_id IS NOT NULL)")
    op.create_table("rest_delivery_batches",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("organization_id", sa.String(64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("run_id", sa.String(64), sa.ForeignKey("runs.id"), nullable=False),
        sa.Column("delivery_attempt_id", sa.String(64), sa.ForeignKey("delivery_attempts.id"), nullable=False),
        sa.Column("ordinal", sa.Integer, nullable=False),
        sa.Column("payload_sha256", sa.String(64), nullable=False),
        sa.Column("payload_artifact_id", sa.String(64), sa.ForeignKey("artifacts.id"), nullable=False),
        sa.Column("row_count", sa.BigInteger, nullable=False),
        sa.Column("bytes_sent", sa.BigInteger, nullable=False),
        sa.Column("attempts", sa.Integer, nullable=False),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("response_status", sa.Integer),
        sa.Column("response_sha256", sa.String(64)),
        sa.Column("error_code", sa.String(80)),
        sa.Column("confirmed_at", sa.DateTime(timezone=True)),
        sa.UniqueConstraint("run_id", "ordinal"),
        sa.CheckConstraint("status IN ('PREPARED','STARTED','COMMITTED','FAILED','UNKNOWN')", name="ck_rest_batch_status"))
    for column in ("organization_id", "run_id", "delivery_attempt_id", "status"):
        op.create_index(f"ix_rest_delivery_batches_{column}", "rest_delivery_batches", [column])


def downgrade():
    op.drop_table("rest_delivery_batches")
    with op.batch_alter_table("acquisition_runs") as batch:
        batch.drop_constraint("ck_acquisition_source_identity", type_="check")
        batch.create_check_constraint("ck_acquisition_source_identity", "(source_type = 'UPLOAD' AND upload_id IS NOT NULL AND connection_version_id IS NULL) OR (source_type IN ('POSTGRESQL','SQLSERVER') AND upload_id IS NULL AND connection_version_id IS NOT NULL)")
