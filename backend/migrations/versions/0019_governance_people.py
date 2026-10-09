"""Shared people catalog with verified account links; preserve legacy assignments.

Revision ID: 0019_governance_people
Revises: 0018_strict_approval_criterion
"""
from uuid import NAMESPACE_URL, uuid5

import sqlalchemy as sa
from alembic import op

revision = "0019_governance_people"
down_revision = "0018_strict_approval_criterion"
branch_labels = None
depends_on = None

ROLES = ("business_owner", "steward", "technical_custodian")


def upgrade() -> None:
    op.create_table("governance_people",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("organization_id", sa.String(64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("name", sa.String(200), nullable=False),
        sa.Column("normalized_name", sa.String(200), nullable=False),
        sa.Column("reference", sa.String(320), nullable=True),
        sa.Column("normalized_reference", sa.String(320), nullable=True),
        sa.Column("email", sa.String(200), nullable=True),
        sa.Column("normalized_email", sa.String(200), nullable=True),
        sa.Column("user_id", sa.String(64), sa.ForeignKey("users.id"), nullable=True),
        sa.Column("active", sa.Boolean(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.UniqueConstraint("organization_id", "user_id"),
        sa.UniqueConstraint("organization_id", "normalized_reference"),
        sa.UniqueConstraint("organization_id", "normalized_email"))
    op.create_index("ix_governance_people_organization_id", "governance_people", ["organization_id"])
    for role in ROLES:
        with op.batch_alter_table("datasets") as batch:
            batch.add_column(sa.Column(f"{role}_person_id", sa.String(64), nullable=True))
            batch.create_foreign_key(f"fk_dataset_{role}_person", "governance_people", [f"{role}_person_id"], ["id"])
    connection = op.get_bind()
    metadata = sa.MetaData()
    datasets = sa.Table("datasets", metadata, autoload_with=connection)
    users = sa.Table("users", metadata, autoload_with=connection)
    people = sa.Table("governance_people", metadata, autoload_with=connection)
    permissions = sa.Table("role_permissions", metadata, autoload_with=connection)
    readers = list(connection.scalars(sa.select(permissions.c.role_id).where(permissions.c.permission_code == "datasets:read")))
    for role_id in readers:
        if connection.scalar(sa.select(permissions.c.role_id).where(permissions.c.role_id == role_id,
                permissions.c.permission_code == "people:read")) is None:
            connection.execute(permissions.insert().values(role_id=role_id, permission_code="people:read"))
    # Link only explicit account foreign keys. Owner text and names never match.
    for role in ROLES:
        rows = connection.execute(sa.select(users).join(datasets, datasets.c[f"{role}_id"] == users.c.id)
            .where(datasets.c.organization_id == users.c.organization_id).distinct()).mappings()
        for account in rows:
            person_id = str(uuid5(NAMESPACE_URL, f"trackvance:governance:{account['organization_id']}:{account['id']}"))
            if connection.scalar(sa.select(people.c.id).where(people.c.id == person_id)) is None:
                connection.execute(people.insert().values(id=person_id, organization_id=account["organization_id"],
                    created_at=account["created_at"], name=account["name"],
                    normalized_name=" ".join(account["name"].split()).casefold(),
                    reference=None, normalized_reference=None, email=None, normalized_email=None,
                    user_id=account["id"], active=account["active"] and not account["deleted"], version=1))
            connection.execute(datasets.update().where(datasets.c[f"{role}_id"] == account["id"],
                datasets.c.organization_id == account["organization_id"]).values({f"{role}_person_id": person_id}))


def downgrade() -> None:
    # This feature's grants have no meaning in the preceding permission catalog.
    connection = op.get_bind()
    permissions = sa.Table("role_permissions", sa.MetaData(), autoload_with=connection)
    connection.execute(permissions.delete().where(permissions.c.permission_code.in_(("people:read", "people:manage"))))
    for role in reversed(ROLES):
        with op.batch_alter_table("datasets") as batch:
            batch.drop_constraint(f"fk_dataset_{role}_person", type_="foreignkey")
            batch.drop_column(f"{role}_person_id")
    op.drop_index("ix_governance_people_organization_id", table_name="governance_people")
    op.drop_table("governance_people")
