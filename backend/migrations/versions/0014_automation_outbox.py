"""Versioned Delivery automation, target guards and independent outbox consumers."""

import sqlalchemy as sa
from alembic import op

revision = "0014_automation_outbox"
down_revision = "0013_async_acquisition"
branch_labels = None
depends_on = None


def _record():
    return [sa.Column("id", sa.String(64), primary_key=True),
            sa.Column("organization_id", sa.String(64), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False)]


def _fk(name, table, nullable=False):
    return sa.Column(name, sa.String(64), sa.ForeignKey(table + ".id"), nullable=nullable)


def upgrade() -> None:
    op.create_table("delivery_automations", *_record(),
        sa.Column("name", sa.String(160), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        _fk("responsible_user_id", "users"),
        sa.Column("next_run_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False))
    op.create_table("delivery_automation_versions", *_record(),
        _fk("automation_id", "delivery_automations"),
        sa.Column("version", sa.Integer(), nullable=False),
        _fk("configuration_id", "configurations"), _fk("responsible_user_id", "users"),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("settings", sa.JSON(), nullable=False), _fk("actor_id", "users"),
        sa.UniqueConstraint("automation_id", "version"))
    op.create_table("delivery_occurrences", *_record(),
        _fk("automation_id", "delivery_automations"),
        _fk("automation_version_id", "delivery_automation_versions"),
        sa.Column("trigger_key", sa.String(180), nullable=False),
        sa.Column("request_hash", sa.String(64), nullable=True),
        sa.Column("origin", sa.String(20), nullable=False),
        sa.Column("planned_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("dispatched_at", sa.DateTime(timezone=True), nullable=False),
        _fk("source_run_id", "runs", True), _fk("dataset_version_id", "dataset_versions", True),
        _fk("run_id", "runs", True), sa.Column("status", sa.String(30), nullable=False),
        sa.Column("reason_code", sa.String(80), nullable=True),
        sa.Column("coalesced_intervals", sa.Integer(), nullable=False),
        sa.UniqueConstraint("automation_id", "trigger_key"), sa.UniqueConstraint("run_id"))
    op.create_table("delivery_input_claims", *_record(),
        _fk("automation_id", "delivery_automations"), _fk("dataset_version_id", "dataset_versions"),
        _fk("occurrence_id", "delivery_occurrences"),
        sa.UniqueConstraint("automation_id", "dataset_version_id"))
    op.create_table("delivery_target_guards", *_record(),
        sa.Column("target_fingerprint", sa.String(64), nullable=False),
        _fk("active_run_id", "runs", True), _fk("unknown_run_id", "runs", True),
        sa.UniqueConstraint("organization_id", "target_fingerprint"))
    op.create_table("delivery_target_decisions", *_record(),
        _fk("guard_id", "delivery_target_guards"), _fk("run_id", "runs"),
        _fk("review_id", "delivery_reviews"), _fk("actor_id", "users"),
        sa.Column("note", sa.String(2000), nullable=False), sa.UniqueConstraint("run_id"))
    op.create_table("outbox_events", *_record(),
        sa.Column("dedupe_key", sa.String(220), nullable=False),
        sa.Column("event_type", sa.String(60), nullable=False),
        sa.Column("aggregate_type", sa.String(30), nullable=False),
        sa.Column("aggregate_id", sa.String(64), nullable=False),
        sa.Column("module", sa.String(20), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.UniqueConstraint("organization_id", "dedupe_key"))
    op.create_table("event_consumptions", *_record(),
        _fk("event_id", "outbox_events"), sa.Column("consumer", sa.String(20), nullable=False),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("available_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("lease_owner", sa.String(64), nullable=True),
        sa.Column("lease_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column("error_code", sa.String(80), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint("event_id", "consumer"),
        sa.CheckConstraint("attempts >= 0 AND attempts <= 5", name="ck_event_attempts"))
    op.create_table("internal_notifications", *_record(),
        _fk("event_id", "outbox_events"), _fk("recipient_user_id", "users"),
        sa.Column("module", sa.String(20), nullable=False),
        sa.Column("origin", sa.String(20), nullable=False),
        sa.Column("status", sa.String(30), nullable=False),
        sa.Column("decision", sa.String(40), nullable=True),
        sa.Column("description", sa.String(500), nullable=False),
        sa.Column("resource_type", sa.String(30), nullable=False),
        sa.Column("resource_id", sa.String(64), nullable=False),
        sa.Column("detail_url", sa.String(200), nullable=False),
        sa.Column("read_at", sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint("event_id", "recipient_user_id"))
    for table in ("delivery_automations", "delivery_automation_versions", "delivery_occurrences",
                  "delivery_input_claims", "delivery_target_guards", "delivery_target_decisions",
                  "outbox_events", "event_consumptions", "internal_notifications"):
        op.create_index(f"ix_{table}_organization_id", table, ["organization_id"])
    for table, column in (("delivery_automations", "responsible_user_id"),
                          ("delivery_automations", "next_run_at"),
                          ("delivery_automation_versions", "automation_id"),
                          ("delivery_occurrences", "automation_id")):
        op.create_index(f"ix_{table}_{column}", table, [column])
    op.create_index("ix_event_consumer_pending", "event_consumptions", ["consumer", "status", "available_at"])
    op.create_index("ix_notification_inbox", "internal_notifications",
                    ["organization_id", "recipient_user_id", "read_at", "created_at"])


def downgrade() -> None:
    for table in ("internal_notifications", "event_consumptions", "outbox_events",
                  "delivery_target_decisions", "delivery_target_guards", "delivery_input_claims",
                  "delivery_occurrences", "delivery_automation_versions", "delivery_automations"):
        op.drop_table(table)
