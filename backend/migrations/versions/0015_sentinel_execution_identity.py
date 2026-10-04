"""Resolve only provable Sentinel actors; unowned active schedules are paused."""

import sqlalchemy as sa
from alembic import op

revision = "0015_sentinel_execution_identity"
down_revision = "0014_automation_outbox"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("monitor_schedules", sa.Column("legacy_enabled_before_identity", sa.Boolean(), nullable=True))
    with op.batch_alter_table("monitor_schedule_versions") as batch:
        batch.add_column(sa.Column("responsible_user_id", sa.String(64), nullable=True))
        batch.create_foreign_key("fk_monitor_schedule_responsible", "users", ["responsible_user_id"], ["id"])
    op.execute(sa.text("""UPDATE monitor_schedule_versions SET responsible_user_id=actor_id
        WHERE EXISTS (SELECT 1 FROM users WHERE users.id=monitor_schedule_versions.actor_id
                      AND users.organization_id=monitor_schedule_versions.organization_id)"""))
    op.execute(sa.text("""UPDATE monitor_schedules SET legacy_enabled_before_identity=enabled, enabled=false
        WHERE NOT EXISTS (SELECT 1 FROM monitor_schedule_versions v
            WHERE v.schedule_id=monitor_schedules.id AND v.version=monitor_schedules.version
              AND v.responsible_user_id IS NOT NULL)"""))


def downgrade():
    op.execute(sa.text("UPDATE monitor_schedules SET enabled=legacy_enabled_before_identity WHERE legacy_enabled_before_identity IS NOT NULL"))
    with op.batch_alter_table("monitor_schedule_versions") as batch:
        batch.drop_constraint("fk_monitor_schedule_responsible", type_="foreignkey")
        batch.drop_column("responsible_user_id")
    op.drop_column("monitor_schedules", "legacy_enabled_before_identity")
