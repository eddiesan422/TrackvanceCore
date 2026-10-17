"""Provisional first acquisition, durable retry and native source update schedules.

Historical Dataset/Version/Binding identities and existing mappings remain unchanged.
"""
import sqlalchemy as sa
from alembic import op

revision = "0023_acquisition_095"
down_revision = "0022_intake_automation_deletion"
branch_labels = None
depends_on = None


def _record():
    return [sa.Column("id", sa.String(64), primary_key=True), sa.Column("organization_id", sa.String(64), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False)]


def _fk(name, target, nullable=False, **kwargs):
    return sa.Column(name, sa.String(64), sa.ForeignKey(target + ".id"), nullable=nullable, **kwargs)


def upgrade():
    with op.batch_alter_table("acquisition_runs") as batch:
        batch.alter_column("dataset_id", existing_type=sa.String(64), nullable=True)
        batch.add_column(sa.Column("retry_state", sa.JSON(), nullable=False, server_default="{}"))
    op.create_table("acquisition_name_reservations", *_record(), sa.Column("name", sa.String(160), nullable=False),
        _fk("acquisition_id", "acquisition_runs", unique=True), sa.UniqueConstraint("organization_id", "name"))
    op.create_index("ix_acquisition_name_reservations_organization_id", "acquisition_name_reservations", ["organization_id"])
    op.create_table("acquisition_source_configurations", *_record(), _fk("dataset_id", "datasets", unique=True),
        sa.Column("version", sa.Integer(), nullable=False), sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("next_run_at", sa.DateTime(timezone=True)), sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_attempt_at", sa.DateTime(timezone=True)), sa.Column("last_query_success_at", sa.DateTime(timezone=True)),
        sa.Column("last_publication_at", sa.DateTime(timezone=True)))
    op.create_table("acquisition_source_revisions", *_record(), _fk("configuration_id", "acquisition_source_configurations"),
        sa.Column("version", sa.Integer(), nullable=False), _fk("actor_id", "users"), sa.Column("settings", sa.JSON(), nullable=False),
        sa.UniqueConstraint("configuration_id", "version"))
    op.create_table("acquisition_occurrences", *_record(), _fk("configuration_id", "acquisition_source_configurations"),
        _fk("revision_id", "acquisition_source_revisions"), sa.Column("planned_at", sa.DateTime(timezone=True), nullable=False),
        _fk("acquisition_id", "acquisition_runs", nullable=True), sa.Column("status", sa.String(30), nullable=False),
        sa.Column("reason_code", sa.String(80)), sa.UniqueConstraint("configuration_id", "planned_at"))
    for table in ("acquisition_source_configurations", "acquisition_source_revisions", "acquisition_occurrences"):
        op.create_index("ix_" + table + "_organization_id", table, ["organization_id"])
    for table in ("acquisition_source_revisions", "acquisition_occurrences"):
        op.create_index("ix_" + table + "_configuration_id", table, ["configuration_id"])


def downgrade():
    # A downgrade cannot erase evidence from initial acquisitions without datasets.
    if op.get_bind().execute(sa.text("SELECT COUNT(*) FROM acquisition_runs WHERE dataset_id IS NULL")).scalar():
        raise RuntimeError("ACQUISITION_HISTORY_PREVENTS_DOWNGRADE")
    for table in ("acquisition_occurrences", "acquisition_source_revisions", "acquisition_source_configurations", "acquisition_name_reservations"):
        op.drop_table(table)
    with op.batch_alter_table("acquisition_runs") as batch:
        batch.drop_column("retry_state")
        batch.alter_column("dataset_id", existing_type=sa.String(64), nullable=False)
