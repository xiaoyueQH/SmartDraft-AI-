"""One bounded inverse patch per current unpublished document."""

import sqlalchemy as sa
from alembic import op

revision = "0024_document_undo"
down_revision = "0023_document_metadata"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "document_undo",
        sa.Column(
            "item_id",
            sa.Uuid(),
            sa.ForeignKey("project_items.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("id", sa.Uuid(), nullable=False, unique=True),
        sa.Column("project_id", sa.Uuid(), sa.ForeignKey("delivery_projects.id"), nullable=False),
        sa.Column("run_id", sa.Uuid(), sa.ForeignKey("project_runs.id"), nullable=False),
        sa.Column("before_version", sa.Integer(), nullable=False),
        sa.Column("after_version", sa.Integer(), nullable=False),
        sa.Column("before_hash", sa.String(64), nullable=False),
        sa.Column("after_hash", sa.String(64), nullable=False),
        sa.Column("edits", sa.JSON(), nullable=False),
        sa.Column("before_questions", sa.JSON(), nullable=False),
        sa.Column("after_questions", sa.JSON(), nullable=False),
        sa.Column("before_blocks", sa.JSON(), nullable=False),
    )
    op.create_index("ix_document_undo_project_id", "document_undo", ["project_id"])


def downgrade():
    op.drop_table("document_undo")
