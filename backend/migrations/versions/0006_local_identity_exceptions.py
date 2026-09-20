"""Local user administration and operational exception workflow.

Revision ID: 0006_local_identity_exceptions
Revises: 0005_external_connections
"""

import sqlalchemy as sa
from alembic import op

revision = "0006_local_identity_exceptions"
down_revision = "0005_external_connections"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("users") as batch:
        batch.add_column(sa.Column("version", sa.Integer(), nullable=False, server_default="1"))
        batch.add_column(sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True))
        batch.add_column(sa.Column("password_changed_at", sa.DateTime(timezone=True), nullable=True))
    op.execute(sa.text("UPDATE users SET updated_at = created_at"))
    with op.batch_alter_table("users") as batch:
        batch.alter_column("updated_at", existing_type=sa.DateTime(timezone=True), nullable=False)
        batch.alter_column("version", existing_type=sa.Integer(), server_default=None)
    with op.batch_alter_table("exceptions") as batch:
        batch.add_column(sa.Column("assigned_user_id", sa.String(64), nullable=True))
        batch.add_column(sa.Column("priority", sa.String(20), nullable=True))
        batch.add_column(sa.Column("sla_hours", sa.Integer(), nullable=True))
        batch.add_column(sa.Column("due_at", sa.DateTime(timezone=True), nullable=True))
        batch.add_column(sa.Column("reopened_at", sa.DateTime(timezone=True), nullable=True))
        batch.add_column(sa.Column("auto_resolve_enabled", sa.Boolean(), nullable=False, server_default=sa.false()))
        batch.create_foreign_key("fk_exceptions_assigned_user_id_users", "users", ["assigned_user_id"], ["id"])
        batch.create_index("ix_exceptions_assigned_user_id", ["assigned_user_id"])
        batch.create_index("ix_exceptions_due_at", ["due_at"])
    # Display owners remain historical; do not infer identity from a possibly ambiguous name.
    op.execute(sa.text("UPDATE exceptions SET priority = severity"))
    with op.batch_alter_table("exceptions") as batch:
        batch.alter_column("priority", existing_type=sa.String(20), nullable=False)
        batch.alter_column("auto_resolve_enabled", existing_type=sa.Boolean(), server_default=None)
    op.create_table(
        "exception_attachments",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("organization_id", sa.String(64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("exception_id", sa.String(64), sa.ForeignKey("exceptions.id"), nullable=False),
        sa.Column("artifact_id", sa.String(64), sa.ForeignKey("artifacts.id"), nullable=False, unique=True),
        sa.Column("uploaded_by_id", sa.String(64), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("description", sa.String(500), nullable=False),
    )
    op.create_index("ix_exception_attachments_organization_id", "exception_attachments", ["organization_id"])
    op.create_index("ix_exception_attachments_exception_id", "exception_attachments", ["exception_id"])


def downgrade() -> None:
    op.drop_table("exception_attachments")
    with op.batch_alter_table("exceptions") as batch:
        batch.drop_index("ix_exceptions_due_at")
        batch.drop_index("ix_exceptions_assigned_user_id")
        batch.drop_constraint("fk_exceptions_assigned_user_id_users", type_="foreignkey")
        for name in ("auto_resolve_enabled", "reopened_at", "due_at", "sla_hours", "priority", "assigned_user_id"):
            batch.drop_column(name)
    with op.batch_alter_table("users") as batch:
        for name in ("password_changed_at", "updated_at", "version"):
            batch.drop_column(name)
