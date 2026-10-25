"""Attach each new conversation pair to its actual execution trace."""

import sqlalchemy as sa
from alembic import op

revision = "0013_message_run"
down_revision = "0012_record_management"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("project_messages", sa.Column("run_id", sa.Uuid(), nullable=True))
    op.create_foreign_key(
        "fk_project_message_run", "project_messages", "project_runs", ["run_id"], ["id"]
    )
    op.create_index("ix_project_messages_run_id", "project_messages", ["run_id"])


def downgrade():
    op.drop_index("ix_project_messages_run_id", table_name="project_messages")
    op.drop_constraint("fk_project_message_run", "project_messages", type_="foreignkey")
    op.drop_column("project_messages", "run_id")
