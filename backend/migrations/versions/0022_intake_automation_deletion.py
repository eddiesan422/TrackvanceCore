"""Intake logical lifecycle, immutable schedules and all-version event occurrences."""

import sqlalchemy as sa
from alembic import op

revision = "0022_intake_automation_deletion"
down_revision = "0021_sentinel_automatic"
branch_labels = None
depends_on = None


def _record():
    return [sa.Column("id", sa.String(64), primary_key=True),
            sa.Column("organization_id", sa.String(64), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False)]


def _fk(name, table, nullable=False):
    return sa.Column(name, sa.String(64), sa.ForeignKey(table + ".id"), nullable=nullable)


def upgrade():
    op.create_table("intake_contract_states", *_record(),
        _fk("root_configuration_id", "configurations"), _fk("active_run_id", "runs", True),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        _fk("deleted_by_id", "users", True), sa.Column("deletion_note", sa.String(2000), nullable=True),
        sa.Column("deletion_impact", sa.JSON(), nullable=True), sa.UniqueConstraint("root_configuration_id"))
    op.create_table("intake_schedules", *_record(), _fk("root_configuration_id", "configurations"),
        sa.Column("version", sa.Integer(), nullable=False), sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("next_run_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False), sa.UniqueConstraint("root_configuration_id"))
    op.create_table("intake_schedule_versions", *_record(), _fk("schedule_id", "intake_schedules"),
        sa.Column("version", sa.Integer(), nullable=False), _fk("configuration_id", "configurations"),
        _fk("responsible_user_id", "users"), _fk("actor_id", "users"),
        sa.Column("enabled", sa.Boolean(), nullable=False), sa.Column("settings", sa.JSON(), nullable=False),
        sa.UniqueConstraint("schedule_id", "version"))
    op.create_table("intake_occurrences", *_record(), _fk("schedule_id", "intake_schedules"),
        _fk("schedule_version_id", "intake_schedule_versions"), _fk("configuration_id", "configurations"),
        sa.Column("trigger_key", sa.String(180), nullable=False), _fk("dataset_version_id", "dataset_versions", True),
        _fk("run_id", "runs", True), sa.Column("origin", sa.String(20), nullable=False),
        sa.Column("planned_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("status", sa.String(30), nullable=False), sa.Column("reason_code", sa.String(80), nullable=True),
        sa.Column("coalesced_intervals", sa.Integer(), nullable=False),
        sa.UniqueConstraint("schedule_id", "trigger_key"), sa.UniqueConstraint("run_id"))
    for table in ("intake_contract_states", "intake_schedules", "intake_schedule_versions", "intake_occurrences"):
        op.create_index("ix_" + table + "_organization_id", table, ["organization_id"])
    op.create_index("ix_intake_schedules_next_run_at", "intake_schedules", ["next_run_at"])
    op.create_index("ix_intake_schedule_versions_schedule_id", "intake_schedule_versions", ["schedule_id"])
    op.create_index("ix_intake_occurrences_schedule_id", "intake_occurrences", ["schedule_id"])
    # Administrator remains the only initial delete authority. Existing custom
    # role grants are preserved; no implicit scheduling grants are backfilled.
    connection = op.get_bind()
    roles = sa.table("roles", sa.column("id"), sa.column("system_key"))
    grants = sa.table("role_permissions", sa.column("role_id"), sa.column("permission_code"))
    for role_id in connection.execute(sa.select(roles.c.id).where(roles.c.system_key == "ADMINISTRATOR")).scalars():
        for permission in ("intake:delete", "intake:schedule"):
            if connection.execute(sa.select(grants.c.role_id).where(grants.c.role_id == role_id,
                    grants.c.permission_code == permission)).first() is None:
                connection.execute(grants.insert().values(role_id=role_id, permission_code=permission))


def downgrade():
    op.execute("DELETE FROM role_permissions WHERE permission_code IN ('intake:delete','intake:schedule')")
    for table in ("intake_occurrences", "intake_schedule_versions", "intake_schedules", "intake_contract_states"):
        op.drop_table(table)
