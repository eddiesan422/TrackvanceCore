"""Dynamic roles, stable usernames, first access, external identities and OIDC state.

Revision ID: 0010_dynamic_rbac_identity
Revises: 0009_delivery_reviews
"""
import re
from datetime import UTC, datetime
from uuid import NAMESPACE_URL, uuid5

import sqlalchemy as sa
from alembic import op

revision = "0010_dynamic_rbac_identity"
down_revision = "0009_delivery_reviews"
branch_labels = None
depends_on = None

# Frozen 0.6.0 seed, deliberately independent of the evolving runtime catalog.
READ = {f"{group}:read" for group in ("datasets", "connections", "intake", "recon", "sentinel", "delivery", "destinations", "exceptions", "rules", "runs")}
EXPORT = {"exports:download", "artifacts:download"}
AUTHOR = {"datasets:write", "runs:execute", "exceptions:write", "connections:use", "destinations:use", "sentinel:schedule", "delivery:overwrite", "delivery:alter_target", "delivery:review_unknown", "delivery:repair_evidence"} | {f"{group}:{action}" for group in ("intake", "recon", "sentinel", "delivery") for action in ("configure", "execute")}
GRANTS = {
    "Administrator": set(),
    "Data Owner / Lead": READ | EXPORT | AUTHOR | {"audit:read", "exceptions:close", "connections:manage", "destinations:manage"},
    "Data Analyst": READ | EXPORT | AUTHOR | {"audit:read"},
    "Operations": READ | EXPORT | {"exceptions:write"},
    "Auditor": READ | EXPORT | {"audit:read", "users:read", "roles:read", "system:read"},
}


def identity(org, name):
    return str(uuid5(NAMESPACE_URL, f"trackvance:role:{org}:{name.casefold()}"))


def upgrade() -> None:
    op.create_table("roles",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("organization_id", sa.String(64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("name", sa.String(120), nullable=False),
        sa.Column("normalized_name", sa.String(120), nullable=False),
        sa.Column("description", sa.String(2000), nullable=False),
        sa.Column("system_key", sa.String(40)),
        sa.Column("active", sa.Boolean(), nullable=False),
        sa.Column("deleted", sa.Boolean(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("deleted_at", sa.DateTime(timezone=True)),
        sa.UniqueConstraint("organization_id", "normalized_name"),
        sa.UniqueConstraint("organization_id", "system_key"))
    op.create_index("ix_roles_organization_id", "roles", ["organization_id"])
    op.create_table("role_permissions",
        sa.Column("role_id", sa.String(64), sa.ForeignKey("roles.id"), primary_key=True),
        sa.Column("permission_code", sa.String(80), primary_key=True))
    with op.batch_alter_table("users") as batch:
        batch.add_column(sa.Column("role_id", sa.String(64), nullable=True))
        batch.add_column(sa.Column("username", sa.String(80), nullable=True))
        batch.add_column(sa.Column("first_name", sa.String(100), nullable=True))
        batch.add_column(sa.Column("last_name", sa.String(100), nullable=True))
        batch.add_column(sa.Column("deleted", sa.Boolean(), nullable=False, server_default=sa.false()))
        batch.add_column(sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True))
        batch.add_column(sa.Column("must_change_password", sa.Boolean(), nullable=False, server_default=sa.false()))
        batch.add_column(sa.Column("temporary_password_expires_at", sa.DateTime(timezone=True), nullable=True))
        batch.add_column(sa.Column("last_login_at", sa.DateTime(timezone=True), nullable=True))
        batch.create_foreign_key("fk_users_role_id_roles", "roles", ["role_id"], ["id"])
        batch.create_index("ix_users_role_id", ["role_id"])
        batch.create_unique_constraint("uq_users_username", ["username"])
    connection = op.get_bind()
    metadata = sa.MetaData()
    users = sa.Table("users", metadata, autoload_with=connection)
    roles = sa.Table("roles", metadata, autoload_with=connection)
    permissions = sa.Table("role_permissions", metadata, autoload_with=connection)
    rows = connection.execute(sa.select(users).order_by(users.c.email, users.c.id)).mappings().all()
    organizations = {row["organization_id"] for row in rows} | {"org-trackvance-demo"}
    now = datetime.now(UTC)
    for org in sorted(organizations):
        names = set(GRANTS) | {"Data Owner / Lead" if row["role"] == "Data Owner" else row["role"] for row in rows if row["organization_id"] == org}
        for name in sorted(names):
            role_id = identity(org, name)
            connection.execute(roles.insert().values(id=role_id, organization_id=org, created_at=now,
                name=name, normalized_name=name.casefold(), description="Rol inicial migrado de Trackvance 0.5.1",
                system_key="ADMINISTRATOR" if name == "Administrator" else None,
                active=name in GRANTS, deleted=False, version=1, updated_at=now))
            for code in sorted(GRANTS.get(name, set())):
                connection.execute(permissions.insert().values(role_id=role_id, permission_code=code))
    occupied: set[str] = set()
    for row in rows:
        base = re.sub(r"[^a-z0-9._-]", "-", row["email"].split("@", 1)[0].casefold()).strip(".-_")
        base = (base if len(base) >= 3 else "user-" + base)[:80]
        username, suffix = base, 1
        while username in occupied:
            suffix += 1
            tail = f"-{suffix}"
            username = base[:80-len(tail)] + tail
        occupied.add(username)
        name = "Data Owner / Lead" if row["role"] == "Data Owner" else row["role"]
        connection.execute(users.update().where(users.c.id == row["id"]).values(
            username=username, role_id=identity(row["organization_id"], name)))
    with op.batch_alter_table("users") as batch:
        batch.alter_column("username", existing_type=sa.String(80), nullable=False)
        batch.alter_column("role_id", existing_type=sa.String(64), nullable=False)
        batch.alter_column("deleted", existing_type=sa.Boolean(), server_default=None)
        batch.alter_column("must_change_password", existing_type=sa.Boolean(), server_default=None)
    with op.batch_alter_table("sessions") as batch:
        batch.add_column(sa.Column("authentication_method", sa.String(30), nullable=False, server_default="LOCAL"))
    with op.batch_alter_table("sessions") as batch:
        batch.alter_column("authentication_method", existing_type=sa.String(30), server_default=None)
    op.create_table("external_identities",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("organization_id", sa.String(64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("user_id", sa.String(64), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("provider", sa.String(30), nullable=False),
        sa.Column("issuer", sa.String(400), nullable=False),
        sa.Column("subject", sa.String(255), nullable=False),
        sa.Column("email_at_link", sa.String(200), nullable=False),
        sa.Column("linked_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_login_at", sa.DateTime(timezone=True)),
        sa.UniqueConstraint("provider", "issuer", "subject"), sa.UniqueConstraint("user_id", "provider"))
    op.create_index("ix_external_identities_organization_id", "external_identities", ["organization_id"])
    op.create_index("ix_external_identities_user_id", "external_identities", ["user_id"])
    op.create_table("oidc_login_attempts",
        sa.Column("state_hash", sa.String(64), primary_key=True),
        sa.Column("provider", sa.String(30), nullable=False),
        sa.Column("browser_hash", sa.String(64), nullable=False),
        sa.Column("nonce", sa.String(100), nullable=False),
        sa.Column("code_verifier", sa.String(128), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("consumed_at", sa.DateTime(timezone=True)))


def downgrade() -> None:
    op.drop_table("oidc_login_attempts")
    op.drop_table("external_identities")
    with op.batch_alter_table("sessions") as batch:
        batch.drop_column("authentication_method")
    with op.batch_alter_table("users") as batch:
        batch.drop_constraint("fk_users_role_id_roles", type_="foreignkey")
        batch.drop_constraint("uq_users_username", type_="unique")
        batch.drop_index("ix_users_role_id")
        for name in ("role_id", "username", "first_name", "last_name", "deleted", "deleted_at", "must_change_password", "temporary_password_expires_at", "last_login_at"):
            batch.drop_column(name)
    op.drop_table("role_permissions")
    op.drop_table("roles")
