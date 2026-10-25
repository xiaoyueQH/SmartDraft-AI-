"""Product lifecycle, optimistic stock edits and idempotent checkout."""

import sqlalchemy as sa
from alembic import op

revision = "0006_catalog_lifecycle"
down_revision = "0005_catalog_imports"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "products", sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true())
    )
    op.add_column("products", sa.Column("deleted_at", sa.DateTime(timezone=True)))
    op.add_column(
        "products", sa.Column("version", sa.Integer(), nullable=False, server_default="1")
    )
    op.create_check_constraint("ck_products_version", "products", "version > 0")
    op.add_column("orders", sa.Column("request_id", sa.Uuid()))
    op.add_column("orders", sa.Column("request_fingerprint", sa.String(64)))
    op.create_unique_constraint("uq_orders_user_request", "orders", ["user_id", "request_id"])
    op.create_table(
        "product_changes",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("product_id", sa.Uuid(), sa.ForeignKey("products.id"), nullable=False),
        sa.Column("actor_id", sa.Uuid(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("action", sa.String(16), nullable=False),
        sa.Column("fingerprint", sa.String(64), nullable=False),
        sa.Column("result_snapshot", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )


def downgrade():
    op.drop_table("product_changes")
    op.drop_constraint("uq_orders_user_request", "orders", type_="unique")
    op.drop_column("orders", "request_fingerprint")
    op.drop_column("orders", "request_id")
    op.drop_constraint("ck_products_version", "products", type_="check")
    op.drop_column("products", "version")
    op.drop_column("products", "deleted_at")
    op.drop_column("products", "is_active")
