"""Product snapshots orders and simulated shipment records"""

import sqlalchemy as sa
from alembic import op

revision = "0003_orders"
down_revision = "0002_login_budgets"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "products",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("sku", sa.String(length=32), nullable=False),
        sa.Column("name", sa.String(length=120), nullable=False),
        sa.Column("description", sa.String(length=1000), nullable=False),
        sa.Column("price_cents", sa.Integer(), nullable=False),
        sa.Column("stock", sa.Integer(), nullable=False),
        sa.Column("is_demo", sa.Boolean(), nullable=False),
        sa.CheckConstraint("price_cents >= 0", name="ck_products_price"),
        sa.CheckConstraint("stock >= 0", name="ck_products_stock"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("sku"),
    )
    op.create_table(
        "orders",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("number", sa.String(length=40), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("status", sa.String(length=24), nullable=False),
        sa.Column("total_cents", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("is_demo", sa.Boolean(), nullable=False),
        sa.CheckConstraint(
            "status IN ('pending_payment', 'paid', 'shipped', 'delivered', 'cancelled')",
            name="ck_orders_status",
        ),
        sa.CheckConstraint("total_cents >= 0", name="ck_orders_total"),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("number"),
    )
    op.create_index("ix_orders_created", "orders", ["created_at", "id"], unique=False)
    op.create_index(
        "ix_orders_status_created", "orders", ["status", "created_at", "id"], unique=False
    )
    op.create_index(
        "ix_orders_user_created", "orders", ["user_id", "created_at", "id"], unique=False
    )
    op.create_table(
        "order_items",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("order_id", sa.Uuid(), nullable=False),
        sa.Column("product_id", sa.Uuid(), nullable=False),
        sa.Column("sku", sa.String(length=32), nullable=False),
        sa.Column("product_name", sa.String(length=120), nullable=False),
        sa.Column("unit_price_cents", sa.Integer(), nullable=False),
        sa.Column("quantity", sa.Integer(), nullable=False),
        sa.CheckConstraint("quantity > 0", name="ck_order_items_quantity"),
        sa.CheckConstraint("unit_price_cents >= 0", name="ck_order_items_price"),
        sa.ForeignKeyConstraint(["order_id"], ["orders.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["product_id"],
            ["products.id"],
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_order_items_order", "order_items", ["order_id", "id"], unique=False)
    op.create_table(
        "shipments",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("order_id", sa.Uuid(), nullable=False),
        sa.Column("carrier", sa.String(length=64), nullable=False),
        sa.Column("tracking_number", sa.String(length=64), nullable=False),
        sa.Column("is_demo", sa.Boolean(), nullable=False),
        sa.ForeignKeyConstraint(["order_id"], ["orders.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_shipments_order", "shipments", ["order_id", "id"], unique=False)
    op.create_table(
        "shipment_events",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("shipment_id", sa.Uuid(), nullable=False),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("description", sa.String(length=300), nullable=False),
        sa.ForeignKeyConstraint(["shipment_id"], ["shipments.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_shipment_events_time",
        "shipment_events",
        ["shipment_id", "occurred_at", "id"],
        unique=False,
    )


def downgrade():
    op.drop_index("ix_shipment_events_time", table_name="shipment_events")
    op.drop_table("shipment_events")
    op.drop_index("ix_shipments_order", table_name="shipments")
    op.drop_table("shipments")
    op.drop_index("ix_order_items_order", table_name="order_items")
    op.drop_table("order_items")
    op.drop_index("ix_orders_user_created", table_name="orders")
    op.drop_index("ix_orders_status_created", table_name="orders")
    op.drop_index("ix_orders_created", table_name="orders")
    op.drop_table("orders")
    op.drop_table("products")
