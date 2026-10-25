"""Track mock catalog imports without changing historical products or orders."""

import sqlalchemy as sa
from alembic import op

revision = "0005_catalog_imports"
down_revision = "0004_cancellation"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "catalog_imports",
        sa.Column("source", sa.String(32), nullable=False),
        sa.Column("source_id", sa.String(64), nullable=False),
        sa.Column("product_id", sa.Uuid(), nullable=False),
        sa.Column("actor_id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("candidate_snapshot", sa.JSON(), nullable=False),
        sa.ForeignKeyConstraint(["product_id"], ["products.id"]),
        sa.ForeignKeyConstraint(["actor_id"], ["users.id"]),
        sa.PrimaryKeyConstraint("source", "source_id"),
        sa.UniqueConstraint("product_id"),
    )


def downgrade():
    op.drop_table("catalog_imports")
