"""Add source-reader metadata to immutable dataset versions.

Revision ID: 0003_dataset_ingestion_metadata
Revises: 0002_evidence_v2

Historical versions receive an empty object and keep the legacy CSV/Parquet
fallback. New versions record the reader, its effective non-secret options,
source schema and record-numbering semantics.
"""

import sqlalchemy as sa
from alembic import op

revision = "0003_dataset_ingestion_metadata"
down_revision = "0002_evidence_v2"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "dataset_versions",
        sa.Column(
            "ingestion_metadata",
            sa.JSON(),
            nullable=False,
            server_default=sa.text("'{}'"),
        ),
    )


def downgrade() -> None:
    op.drop_column("dataset_versions", "ingestion_metadata")
