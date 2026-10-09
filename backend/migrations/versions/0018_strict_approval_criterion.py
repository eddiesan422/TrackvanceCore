"""Version the approval index without changing historical runs or decisions."""
import sqlalchemy as sa
from alembic import op

revision = "0018_strict_approval_criterion"
down_revision = "0017_catalog_reports"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("strict_approvals", sa.Column("criterion_version", sa.Integer(), nullable=False, server_default="1"))


def downgrade():
    with op.batch_alter_table("strict_approvals") as batch_op:
        batch_op.drop_column("criterion_version")
