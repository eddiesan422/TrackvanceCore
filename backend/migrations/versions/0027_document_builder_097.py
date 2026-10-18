"""Independent document references, optimistic drafts and exact test snapshots."""
import sqlalchemy as sa
from alembic import op

revision = "0027_document_builder_097"
down_revision = "0026_acquisition_097"
branch_labels = None
depends_on = None


def record():
    return [sa.Column("id", sa.String(64), primary_key=True),
            sa.Column("organization_id", sa.String(64), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False)]


def fk(name, table, nullable=False):
    return sa.Column(name, sa.String(64), sa.ForeignKey(table + ".id"), nullable=nullable)


def historical_job_constraint():
    """Exact subject/lane semantics of 0025, independent of reflected SQL syntax."""
    subjects = ("run_id", "acquisition_id", "report_execution_id", "document_id", "comparison_id")
    lanes = ("lane IN ('DEFAULT','DELIVERY')", "lane = 'ACQUISITION'", "lane = 'REPORT'", "lane = 'DOCUMENT'", "lane = 'COMPARISON'")
    return " OR ".join("(" + " AND ".join(column + (" IS NOT NULL" if column == subject else " IS NULL") for column in subjects)
                       + " AND " + lane + ")" for subject, lane in zip(subjects, lanes, strict=True))


def upgrade():
    op.create_table("document_references", *record(), fk("family_id", "document_families"), fk("artifact_id", "artifacts"),
        sa.Column("filename", sa.String(240), nullable=False), sa.Column("sha256", sa.String(64), nullable=False),
        fk("actor_id", "users"), sa.Column("role", sa.String(20), nullable=False),
        sa.Column("association", sa.String(120), nullable=True), sa.Column("status", sa.String(20), nullable=False),
        sa.Column("configuration_used", sa.Boolean(), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=True), sa.Column("payload_hash", sa.String(64), nullable=True),
        sa.Column("engine", sa.JSON(), nullable=True), sa.Column("error_code", sa.String(80), nullable=True))
    op.create_table("document_drafts", *record(), fk("family_id", "document_families"),
        sa.Column("name", sa.String(140), nullable=False), sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("definition", sa.JSON(), nullable=False), sa.Column("definition_hash", sa.String(64), nullable=False),
        sa.Column("reference_ids", sa.JSON(), nullable=False), sa.Column("discovered_from", sa.JSON(), nullable=False),
        fk("source_template_id", "document_templates", True), fk("actor_id", "users"))
    op.create_table("document_builder_tests", *record(), fk("draft_id", "document_drafts"),
        sa.Column("draft_version", sa.Integer(), nullable=False), sa.Column("snapshot", sa.JSON(), nullable=False),
        sa.Column("snapshot_hash", sa.String(64), nullable=False), sa.Column("automatic_results", sa.JSON(), nullable=False),
        sa.Column("automatic_results_hash", sa.String(64), nullable=False),
        sa.Column("reviewed_results", sa.JSON(), nullable=False), sa.Column("review_version", sa.Integer(), nullable=False),
        sa.Column("review_history", sa.JSON(), nullable=False), fk("activated_template_id", "document_templates", True),
        fk("actor_id", "users"), sa.UniqueConstraint("draft_id", "snapshot_hash"))
    for table, parent in (("document_references", "family_id"), ("document_drafts", "family_id"), ("document_builder_tests", "draft_id")):
        for column in ("organization_id", parent):
            op.create_index(f"ix_{table}_{column}", table, [column])
    old = historical_job_constraint()
    with op.batch_alter_table("jobs") as batch:
        batch.add_column(sa.Column("document_reference_id", sa.String(64), nullable=True))
        batch.create_foreign_key("fk_jobs_document_reference", "document_references", ["document_reference_id"], ["id"])
        batch.create_unique_constraint("uq_jobs_document_reference", ["document_reference_id"])
        batch.drop_constraint("ck_job_subject_lane", type_="check")
        batch.create_check_constraint("ck_job_subject_lane", "(document_reference_id IS NULL AND (" + old + ")) OR (document_reference_id IS NOT NULL AND run_id IS NULL AND acquisition_id IS NULL AND report_execution_id IS NULL AND document_id IS NULL AND comparison_id IS NULL AND lane = 'DOCUMENT')")


def downgrade():
    if op.get_bind().execute(sa.text("SELECT 1 FROM document_references UNION ALL SELECT 1 FROM document_drafts UNION ALL SELECT 1 FROM document_builder_tests UNION ALL SELECT 1 FROM jobs WHERE document_reference_id IS NOT NULL LIMIT 1")).first():
        raise RuntimeError("DOCUMENT_EVIDENCE_PRESERVATION: restore a compatible backup; drafts and references are evidence.")
    old = historical_job_constraint()
    with op.batch_alter_table("jobs") as batch:
        batch.drop_constraint("ck_job_subject_lane", type_="check")
        batch.drop_constraint("uq_jobs_document_reference", type_="unique")
        batch.drop_constraint("fk_jobs_document_reference", type_="foreignkey")
        batch.drop_column("document_reference_id")
        batch.create_check_constraint("ck_job_subject_lane", old)
    for table in ("document_builder_tests", "document_drafts", "document_references"):
        op.drop_table(table)
