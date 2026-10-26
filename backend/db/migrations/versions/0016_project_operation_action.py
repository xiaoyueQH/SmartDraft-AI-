"""Allow operation names together with their full item UUID in audit receipts."""

import sqlalchemy as sa
from alembic import op

revision = "0016_operation_action"
down_revision = "0015_run_cache_version"
branch_labels = None
depends_on = None


def upgrade():
    op.alter_column(
        "project_operations", "action", type_=sa.String(64), existing_type=sa.String(40)
    )


def downgrade():
    # Never truncate audit identities or discard idempotency receipts on downgrade.
    connection = op.get_bind()
    if connection.scalar(
        sa.text("SELECT count(*) FROM project_operations WHERE length(action) > 40")
    ):
        raise RuntimeError("Cannot narrow action while full item audit receipts exist")
    op.alter_column(
        "project_operations", "action", type_=sa.String(40), existing_type=sa.String(64)
    )
