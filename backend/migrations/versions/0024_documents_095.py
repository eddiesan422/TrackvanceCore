"""Add immutable operational document identities and a dedicated native Job lane."""
import sqlalchemy as sa
from alembic import op

revision = "0024_documents_095"
down_revision = "0023_acquisition_095"
branch_labels = None
depends_on = None


def record():
    return [sa.Column("id", sa.String(64), primary_key=True),
            sa.Column("organization_id", sa.String(64), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False)]


def fk(name, table, nullable=False):
    return sa.Column(name, sa.String(64), sa.ForeignKey(table + ".id"), nullable=nullable)


OLD = "(run_id IS NOT NULL AND acquisition_id IS NULL AND report_execution_id IS NULL AND lane IN ('DEFAULT','DELIVERY')) OR (run_id IS NULL AND acquisition_id IS NOT NULL AND report_execution_id IS NULL AND lane = 'ACQUISITION') OR (run_id IS NULL AND acquisition_id IS NULL AND report_execution_id IS NOT NULL AND lane = 'REPORT')"
NEW = "(run_id IS NOT NULL AND acquisition_id IS NULL AND report_execution_id IS NULL AND document_id IS NULL AND lane IN ('DEFAULT','DELIVERY')) OR (run_id IS NULL AND acquisition_id IS NOT NULL AND report_execution_id IS NULL AND document_id IS NULL AND lane = 'ACQUISITION') OR (run_id IS NULL AND acquisition_id IS NULL AND report_execution_id IS NOT NULL AND document_id IS NULL AND lane = 'REPORT') OR (run_id IS NULL AND acquisition_id IS NULL AND report_execution_id IS NULL AND document_id IS NOT NULL AND lane = 'DOCUMENT')"


def upgrade():
    op.create_table("document_families", *record(), sa.Column("name", sa.String(140), nullable=False),
        sa.Column("description", sa.Text(), nullable=False), fk("responsible_user_id", "users"),
        sa.Column("active_template_id", sa.String(64), nullable=True), sa.Column("output_datasets", sa.JSON(), nullable=False),
        sa.Column("next_cut_at", sa.DateTime(timezone=True), nullable=True), sa.Column("lock_version", sa.Integer(), nullable=False),
        sa.UniqueConstraint("organization_id", "name"))
    op.create_table("document_templates", *record(), fk("family_id", "document_families"),
        sa.Column("revision", sa.Integer(), nullable=False), sa.Column("definition", sa.JSON(), nullable=False),
        sa.Column("definition_hash", sa.String(64), nullable=False), fk("created_by_id", "users"),
        sa.Column("tested_extraction_id", sa.String(64), nullable=True), sa.UniqueConstraint("family_id", "revision"))
    op.create_table("document_originals", *record(), fk("family_id", "document_families"), fk("template_id", "document_templates"),
        fk("artifact_id", "artifacts"), sa.Column("filename", sa.String(240), nullable=False),
        sa.Column("sha256", sa.String(64), nullable=False), sa.Column("received_at", sa.DateTime(timezone=True), nullable=False),
        fk("uploaded_by_id", "users"), sa.Column("status", sa.String(30), nullable=False), sa.Column("progress", sa.Integer(), nullable=False),
        sa.Column("cancel_requested", sa.Boolean(), nullable=False), sa.Column("extraction_id", sa.String(64), nullable=True),
        fk("duplicate_of_id", "document_originals", True), fk("correction_of_id", "document_originals", True),
        sa.Column("is_example", sa.Boolean(), nullable=False), sa.Column("error_code", sa.String(80), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True), sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True))
    op.create_table("document_extractions", *record(), fk("document_id", "document_originals"), fk("template_id", "document_templates"),
        fk("artifact_id", "artifacts", True),
        sa.Column("revision", sa.Integer(), nullable=False), fk("previous_id", "document_extractions", True),
        sa.Column("payload", sa.JSON(), nullable=False), sa.Column("payload_hash", sa.String(64), nullable=False),
        sa.Column("engine", sa.JSON(), nullable=False), sa.Column("reviewed", sa.Boolean(), nullable=False),
        fk("actor_id", "users"), sa.Column("reason", sa.String(2000), nullable=False), sa.UniqueConstraint("document_id", "revision"))
    op.create_table("document_batches", *record(), fk("family_id", "document_families"), fk("template_id", "document_templates"),
        sa.Column("idempotency_key", sa.String(160), nullable=False), sa.Column("status", sa.String(30), nullable=False),
        sa.Column("window_start", sa.DateTime(timezone=True), nullable=True), sa.Column("window_end", sa.DateTime(timezone=True), nullable=True),
        sa.Column("settings", sa.JSON(), nullable=False), sa.Column("sealed_manifest", sa.JSON(), nullable=True),
        sa.Column("manifest_hash", sa.String(64), nullable=True), sa.Column("published_versions", sa.JSON(), nullable=False),
        fk("actor_id", "users"), sa.Column("blocked_reason", sa.String(120), nullable=True),
        sa.Column("wait_until", sa.DateTime(timezone=True), nullable=False), sa.UniqueConstraint("family_id", "idempotency_key"))
    op.create_table("document_batch_members", *record(), fk("batch_id", "document_batches"), fk("document_id", "document_originals"),
        fk("extraction_id", "document_extractions", True), sa.Column("status", sa.String(30), nullable=False),
        sa.Column("reason", sa.String(120), nullable=True), sa.UniqueConstraint("batch_id", "document_id"))
    op.create_table("document_publication_claims", *record(), fk("extraction_id", "document_extractions"),
        fk("document_id", "document_originals"), fk("batch_id", "document_batches"), sa.UniqueConstraint("extraction_id"))
    indexes = {"document_templates": ["family_id"], "document_originals": ["family_id", "sha256", "received_at", "status"],
               "document_extractions": ["document_id"], "document_batches": ["family_id", "status"],
               "document_batch_members": ["batch_id"], "document_publication_claims": ["document_id", "batch_id"]}
    for table in ("document_families", *indexes):
        for column in ["organization_id", *indexes.get(table, [])]:
            op.create_index(f"ix_{table}_{column}", table, [column])
    with op.batch_alter_table("jobs") as batch:
        batch.add_column(sa.Column("document_id", sa.String(64), nullable=True))
        batch.create_foreign_key("fk_jobs_document", "document_originals", ["document_id"], ["id"])
        batch.create_unique_constraint("uq_jobs_document", ["document_id"])
        batch.drop_constraint("ck_job_subject_lane", type_="check")
        batch.create_check_constraint("ck_job_subject_lane", NEW)


def downgrade():
    if op.get_bind().execute(sa.text("SELECT 1 FROM document_originals LIMIT 1")).first():
        raise RuntimeError("DOCUMENT_EVIDENCE_PRESERVATION: export/restore a compatible backup instead of dropping evidence.")
    with op.batch_alter_table("jobs") as batch:
        batch.drop_constraint("ck_job_subject_lane", type_="check")
        batch.drop_constraint("uq_jobs_document", type_="unique")
        batch.drop_constraint("fk_jobs_document", type_="foreignkey")
        batch.drop_column("document_id")
        batch.create_check_constraint("ck_job_subject_lane", OLD)
    for table in ("document_publication_claims", "document_batch_members", "document_batches", "document_extractions",
                  "document_originals", "document_templates", "document_families"):
        op.drop_table(table)
