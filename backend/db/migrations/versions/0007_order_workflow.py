"""order_fulfilment_returns"""

import sqlalchemy as sa
from alembic import op

revision = "8704cf35bad5"
down_revision = "0006_catalog_lifecycle"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "order_events",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("order_id", sa.Uuid(), nullable=False),
        sa.Column("action", sa.String(length=24), nullable=False),
        sa.Column("description", sa.String(length=300), nullable=False),
        sa.Column("actor_username", sa.String(length=32), nullable=False),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["order_id"],
            ["orders.id"],
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_order_events_time", "order_events", ["order_id", "occurred_at", "id"], unique=False
    )
    op.create_table(
        "payments",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("order_id", sa.Uuid(), nullable=False),
        sa.Column("amount_cents", sa.Integer(), nullable=False),
        sa.Column("channel", sa.String(length=16), nullable=False),
        sa.Column("paid_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("amount_cents > 0", name="ck_payments_amount"),
        sa.ForeignKeyConstraint(
            ["order_id"],
            ["orders.id"],
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("order_id"),
    )
    op.create_table(
        "return_cases",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("order_id", sa.Uuid(), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("reason", sa.String(length=300), nullable=False),
        sa.Column("review_note", sa.String(length=300), nullable=False),
        sa.Column("carrier", sa.String(length=64), nullable=True),
        sa.Column("tracking_number", sa.String(length=64), nullable=True),
        sa.Column("refund_cents", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("restocked_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "status IN ('requested','approved','rejected','returning','received','closed')",
            name="ck_returns_status",
        ),
        sa.CheckConstraint("refund_cents > 0", name="ck_returns_amount"),
        sa.ForeignKeyConstraint(
            ["order_id"],
            ["orders.id"],
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("order_id"),
    )
    op.create_index("ix_returns_created", "return_cases", ["created_at", "id"], unique=False)
    op.create_table(
        "workflow_actions",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("order_id", sa.Uuid(), nullable=False),
        sa.Column("actor_id", sa.Uuid(), nullable=False),
        sa.Column("fingerprint", sa.String(length=64), nullable=False),
        sa.Column("result_snapshot", sa.JSON(), nullable=False),
        sa.ForeignKeyConstraint(
            ["actor_id"],
            ["users.id"],
        ),
        sa.ForeignKeyConstraint(
            ["order_id"],
            ["orders.id"],
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_table(
        "refunds",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("order_id", sa.Uuid(), nullable=False),
        sa.Column("case_id", sa.Uuid(), nullable=True),
        sa.Column("amount_cents", sa.Integer(), nullable=False),
        sa.Column("channel", sa.String(length=16), nullable=False),
        sa.Column("refunded_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("amount_cents > 0", name="ck_refunds_amount"),
        sa.ForeignKeyConstraint(
            ["case_id"],
            ["return_cases.id"],
        ),
        sa.ForeignKeyConstraint(
            ["order_id"],
            ["orders.id"],
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("case_id"),
        sa.UniqueConstraint("order_id"),
    )
    op.add_column(
        "inventory_reservations",
        sa.Column("consumed_at", sa.DateTime(timezone=True), nullable=True),
    )

    op.create_check_constraint(
        "ck_reservations_terminal",
        "inventory_reservations",
        "released_at IS NULL OR consumed_at IS NULL",
    )


def downgrade():
    op.drop_constraint("ck_reservations_terminal", "inventory_reservations", type_="check")
    op.drop_column("inventory_reservations", "consumed_at")
    op.drop_table("refunds")
    op.drop_table("workflow_actions")
    op.drop_index("ix_returns_created", table_name="return_cases")
    op.drop_table("return_cases")
    op.drop_table("payments")
    op.drop_index("ix_order_events_time", table_name="order_events")
    op.drop_table("order_events")
