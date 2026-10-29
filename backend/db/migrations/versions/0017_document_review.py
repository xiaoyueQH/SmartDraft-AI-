"""Persist document review candidates and recoverable source revisions."""

import sqlalchemy as sa
from alembic import op

revision = "0017_document_review"
down_revision = "0016_operation_action"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "document_reviews",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("project_id", sa.Uuid(), sa.ForeignKey("delivery_projects.id"), nullable=False),
        sa.Column("item_id", sa.Uuid(), sa.ForeignKey("project_items.id"), nullable=False),
        sa.Column("actor_id", sa.Uuid(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("request_id", sa.Uuid(), nullable=False),
        sa.Column("fingerprint", sa.String(64), nullable=False),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("source", sa.JSON(), nullable=False),
        sa.Column("blocks", sa.JSON(), nullable=False),
        sa.Column("report", sa.JSON(), nullable=False),
        sa.Column("candidate", sa.Text(), nullable=False),
        sa.Column("events", sa.JSON(), nullable=False),
        sa.Column("error", sa.String(500), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index(
        "uq_document_review_request", "document_reviews", ["actor_id", "request_id"], unique=True
    )
    op.create_index(
        "uq_document_review_running",
        "document_reviews",
        ["item_id"],
        unique=True,
        postgresql_where=sa.text("status = 'running'"),
    )
    op.create_index(
        "ix_document_review_item_created", "document_reviews", ["item_id", "created_at"]
    )
    op.create_table(
        "document_revisions",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("project_id", sa.Uuid(), sa.ForeignKey("delivery_projects.id"), nullable=False),
        sa.Column("item_id", sa.Uuid(), sa.ForeignKey("project_items.id"), nullable=False),
        sa.Column("actor_id", sa.Uuid(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("title", sa.String(160), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("questions", sa.JSON(), nullable=False),
        sa.Column("blocks", sa.JSON(), nullable=False),
        sa.Column("action", sa.String(32), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index(
        "uq_document_revision_version", "document_revisions", ["item_id", "version"], unique=True
    )


def downgrade():
    connection = op.get_bind()
    if connection.scalar(sa.text("SELECT count(*) FROM document_reviews")) or connection.scalar(
        sa.text("SELECT count(*) FROM document_revisions")
    ):
        raise RuntimeError("Refusing to discard document review history")
    op.drop_table("document_revisions")
    op.drop_table("document_reviews")
