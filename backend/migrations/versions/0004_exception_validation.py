"""Add technical validation and administrative closure evidence to exceptions.

Revision ID: 0004_exception_validation
Revises: 0003_dataset_ingestion_metadata

The originating configuration is backfilled from the immutable originating run.
No historical run, finding, state, or event is rewritten.
"""

import sqlalchemy as sa
from alembic import op

revision = "0004_exception_validation"
down_revision = "0003_dataset_ingestion_metadata"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("exceptions") as batch:
        batch.add_column(sa.Column("configuration_id", sa.String(length=64), nullable=True))
        batch.add_column(sa.Column("validation_run_id", sa.String(length=64), nullable=True))
        batch.add_column(
            sa.Column(
                "administrative_reason",
                sa.Text(),
                nullable=False,
                server_default=sa.text("''"),
            )
        )
        batch.add_column(
            sa.Column(
                "validation_evidence",
                sa.JSON(),
                nullable=False,
                server_default=sa.text("'{}'"),
            )
        )
        batch.add_column(sa.Column("validated_at", sa.DateTime(timezone=True), nullable=True))
        batch.create_foreign_key(
            "fk_exceptions_configuration_id_configurations",
            "configurations",
            ["configuration_id"],
            ["id"],
        )
        batch.create_foreign_key(
            "fk_exceptions_validation_run_id_runs",
            "runs",
            ["validation_run_id"],
            ["id"],
        )
        batch.create_index("ix_exceptions_configuration_id", ["configuration_id"])
        batch.create_index("ix_exceptions_validation_run_id", ["validation_run_id"])

    op.execute(
        sa.text(
            """
            UPDATE exceptions
               SET configuration_id = (
                   SELECT runs.config_id
                     FROM runs
                    WHERE runs.id = exceptions.run_id
               )
             WHERE configuration_id IS NULL
            """
        )
    )

    with op.batch_alter_table("exceptions") as batch:
        batch.alter_column(
            "configuration_id",
            existing_type=sa.String(length=64),
            nullable=False,
        )
        batch.alter_column(
            "administrative_reason",
            existing_type=sa.Text(),
            server_default=None,
        )
        batch.alter_column(
            "validation_evidence",
            existing_type=sa.JSON(),
            server_default=None,
        )


def downgrade() -> None:
    with op.batch_alter_table("exceptions") as batch:
        batch.drop_index("ix_exceptions_validation_run_id")
        batch.drop_index("ix_exceptions_configuration_id")
        batch.drop_constraint("fk_exceptions_validation_run_id_runs", type_="foreignkey")
        batch.drop_constraint("fk_exceptions_configuration_id_configurations", type_="foreignkey")
        batch.drop_column("validated_at")
        batch.drop_column("validation_evidence")
        batch.drop_column("administrative_reason")
        batch.drop_column("validation_run_id")
        batch.drop_column("configuration_id")
