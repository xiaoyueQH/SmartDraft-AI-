"""Confirmed cancellation receipts and stock reservations"""

import sqlalchemy as sa
from alembic import op

revision = "0004_cancellation"
down_revision = "0003_orders"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "cancellation_confirmations",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("order_id", sa.Uuid(), nullable=False),
        sa.Column("actor_id", sa.Uuid(), nullable=False),
        sa.Column("session_id", sa.Uuid(), nullable=False),
        sa.Column("snapshot_hash", sa.String(length=64), nullable=False),
        sa.Column("reason", sa.String(length=300), nullable=False),
        sa.Column("from_status", sa.String(length=24), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["actor_id"],
            ["users.id"],
        ),
        sa.ForeignKeyConstraint(
            ["order_id"],
            ["orders.id"],
        ),
        sa.ForeignKeyConstraint(
            ["session_id"],
            ["auth_sessions.id"],
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_cancel_confirmations_owner",
        "cancellation_confirmations",
        ["order_id", "actor_id", "created_at"],
        unique=False,
    )
    op.create_table(
        "inventory_reservations",
        sa.Column("order_item_id", sa.Uuid(), nullable=False),
        sa.Column("quantity", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("released_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint("quantity > 0", name="ck_reservations_quantity"),
        sa.ForeignKeyConstraint(["order_item_id"], ["order_items.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("order_item_id"),
    )
    op.create_table(
        "order_cancellations",
        sa.Column("confirmation_id", sa.Uuid(), nullable=False),
        sa.Column("order_id", sa.Uuid(), nullable=False),
        sa.Column("actor_id", sa.Uuid(), nullable=False),
        sa.Column("actor_username", sa.String(length=32), nullable=False),
        sa.Column("actor_role", sa.String(length=16), nullable=False),
        sa.Column("from_status", sa.String(length=24), nullable=False),
        sa.Column("reason", sa.String(length=300), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("released_units", sa.Integer(), nullable=False),
        sa.Column("released_items", sa.JSON(), nullable=False),
        sa.Column("refund_status", sa.String(length=24), nullable=False),
        sa.Column("refund_cents", sa.Integer(), nullable=False),
        sa.Column("is_demo", sa.Boolean(), nullable=False),
        sa.CheckConstraint(
            "refund_status IN ('not_required', 'simulated')", name="ck_cancellations_refund_status"
        ),
        sa.CheckConstraint("refund_cents >= 0", name="ck_cancellations_refund"),
        sa.CheckConstraint("released_units > 0", name="ck_cancellations_released"),
        sa.ForeignKeyConstraint(
            ["actor_id"],
            ["users.id"],
        ),
        sa.ForeignKeyConstraint(
            ["confirmation_id"],
            ["cancellation_confirmations.id"],
        ),
        sa.ForeignKeyConstraint(
            ["order_id"],
            ["orders.id"],
        ),
        sa.PrimaryKeyConstraint("confirmation_id"),
        sa.UniqueConstraint("order_id"),
    )


def downgrade():
    op.drop_table("order_cancellations")
    op.drop_table("inventory_reservations")
    op.drop_index("ix_cancel_confirmations_owner", table_name="cancellation_confirmations")
    op.drop_table("cancellation_confirmations")
