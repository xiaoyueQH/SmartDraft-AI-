"""Version-bound derived Markdown indexes; no customer body backfill or rewrite."""

import sqlalchemy as sa
from alembic import op

revision = "0023_document_metadata"
down_revision = "0022_agent_jobs"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "document_metadata",
        sa.Column(
            "item_id",
            sa.Uuid(),
            sa.ForeignKey("project_items.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("project_id", sa.Uuid(), sa.ForeignKey("delivery_projects.id"), nullable=False),
        sa.Column("content_version", sa.Integer(), nullable=False),
        sa.Column("content_hash", sa.String(64), nullable=False),
        sa.Column("parser_version", sa.Integer(), nullable=False),
        sa.Column("manifest", sa.JSON(), nullable=False),
    )
    op.create_index("ix_document_metadata_project_id", "document_metadata", ["project_id"])


def downgrade():
    # Only the rebuildable index is removed; project_items and history remain untouched.
    op.drop_table("document_metadata")
