"""catalog_candidates"""

import sqlalchemy as sa
from alembic import op

revision = "0008_catalog_candidates"
down_revision = "8704cf35bad5"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "catalog_candidates",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("actor_id", sa.Uuid(), nullable=False),
        sa.Column("sku", sa.String(length=32), nullable=False),
        sa.Column("name", sa.String(length=120), nullable=False),
        sa.Column("description", sa.String(length=1000), nullable=False),
        sa.Column("price_cents", sa.Integer(), nullable=False),
        sa.Column("stock", sa.Integer(), nullable=False),
        sa.Column("fingerprint", sa.String(length=64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("price_cents > 0", name="ck_candidates_price"),
        sa.CheckConstraint("stock >= 0", name="ck_candidates_stock"),
        sa.ForeignKeyConstraint(
            ["actor_id"],
            ["users.id"],
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("sku"),
    )


def downgrade():
    op.drop_table("catalog_candidates")
