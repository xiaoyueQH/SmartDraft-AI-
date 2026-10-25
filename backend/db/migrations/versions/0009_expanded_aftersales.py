"""expanded_aftersales"""

import sqlalchemy as sa
from alembic import op

revision = "0009_expanded_aftersales"
down_revision = "0008_catalog_candidates"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "aftersale_settings",
        sa.Column("id", sa.Integer(), autoincrement=False, nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("shipping_payer", sa.String(length=16), nullable=False),
        sa.Column("return_address", sa.String(length=500), nullable=False),
        sa.Column("instructions", sa.String(length=1000), nullable=False),
        sa.CheckConstraint("id = 1", name="ck_aftersale_settings_single"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_table(
        "aftersale_setting_changes",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("actor_id", sa.Uuid(), nullable=False),
        sa.Column("fingerprint", sa.String(length=64), nullable=False),
        sa.Column("snapshot", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["actor_id"],
            ["users.id"],
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_table(
        "aftersale_evidence",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("order_id", sa.Uuid(), nullable=False),
        sa.Column("owner_id", sa.Uuid(), nullable=False),
        sa.Column("case_id", sa.Uuid(), nullable=True),
        sa.Column("mime", sa.String(length=32), nullable=False),
        sa.Column("digest", sa.String(length=64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["case_id"],
            ["return_cases.id"],
        ),
        sa.ForeignKeyConstraint(
            ["order_id"],
            ["orders.id"],
        ),
        sa.ForeignKeyConstraint(
            ["owner_id"],
            ["users.id"],
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        op.f("ix_aftersale_evidence_case_id"), "aftersale_evidence", ["case_id"], unique=False
    )
    op.create_index(
        op.f("ix_aftersale_evidence_order_id"), "aftersale_evidence", ["order_id"], unique=False
    )
    op.add_column(
        "inventory_reservations",
        sa.Column("released_quantity", sa.Integer(), server_default="0", nullable=False),
    )
    op.add_column("order_events", sa.Column("case_id", sa.Uuid(), nullable=True))
    op.alter_column(
        "order_events",
        "description",
        existing_type=sa.VARCHAR(length=300),
        type_=sa.String(length=1000),
        existing_nullable=False,
    )
    op.create_foreign_key(
        "fk_order_events_case", "order_events", "return_cases", ["case_id"], ["id"]
    )
    op.drop_constraint(op.f("refunds_order_id_key"), "refunds", type_="unique")
    op.create_index(op.f("ix_refunds_order_id"), "refunds", ["order_id"], unique=False)
    op.add_column(
        "return_cases",
        sa.Column("kind", sa.String(length=20), server_default="return_refund", nullable=False),
    )
    op.add_column(
        "return_cases", sa.Column("quantity", sa.Integer(), server_default="1", nullable=False)
    )
    op.add_column(
        "return_cases", sa.Column("reasons", sa.JSON(), server_default="[]", nullable=False)
    )
    op.add_column(
        "return_cases",
        sa.Column("settings_snapshot", sa.JSON(), server_default="{}", nullable=False),
    )
    op.add_column(
        "return_cases",
        sa.Column("exchange_reserved", sa.Boolean(), server_default="false", nullable=False),
    )
    op.add_column(
        "return_cases", sa.Column("exchange_carrier", sa.String(length=64), nullable=True)
    )
    op.add_column(
        "return_cases", sa.Column("exchange_tracking_number", sa.String(length=64), nullable=True)
    )
    op.alter_column(
        "return_cases",
        "reason",
        existing_type=sa.VARCHAR(length=300),
        type_=sa.String(length=1000),
        existing_nullable=False,
    )
    op.drop_constraint(op.f("return_cases_order_id_key"), "return_cases", type_="unique")
    op.create_index(
        "uq_returns_active_order",
        "return_cases",
        ["order_id"],
        unique=True,
        postgresql_where=sa.text("status NOT IN ('closed','rejected','withdrawn')"),
        sqlite_where=sa.text("status NOT IN ('closed','rejected','withdrawn')"),
    )
    op.drop_constraint("ck_returns_status", "return_cases", type_="check")
    op.drop_constraint("ck_returns_amount", "return_cases", type_="check")
    op.create_check_constraint(
        "ck_returns_status",
        "return_cases",
        "status IN ('requested','needs_info','approved','rejected','withdrawn',"
        "'returning','needs_attention','received','exchange_shipped','closed')",
    )
    op.create_check_constraint("ck_returns_amount", "return_cases", "refund_cents >= 0")
    op.create_check_constraint(
        "ck_returns_kind", "return_cases", "kind IN ('refund_only','return_refund','exchange')"
    )
    op.create_check_constraint("ck_returns_quantity", "return_cases", "quantity > 0")
    op.create_check_constraint(
        "ck_reservations_released_quantity",
        "inventory_reservations",
        "released_quantity >= 0 AND released_quantity <= quantity",
    )
    op.execute(
        "UPDATE inventory_reservations SET released_quantity = quantity "
        "WHERE released_at IS NOT NULL"
    )
    op.execute(
        "UPDATE return_cases c SET quantity = "
        "(SELECT COALESCE(SUM(i.quantity), 1) FROM order_items i WHERE i.order_id = c.order_id)"
    )
    settings = sa.table(
        "aftersale_settings",
        sa.column("id", sa.Integer()),
        sa.column("version", sa.Integer()),
        sa.column("shipping_payer", sa.String()),
        sa.column("return_address", sa.String()),
        sa.column("instructions", sa.String()),
    )
    op.bulk_insert(
        settings,
        [
            {
                "id": 1,
                "version": 1,
                "shipping_payer": "merchant",
                "return_address": "演示寄回地址，请联系管理员确认，请勿实际寄送",
                "instructions": (
                    "请保持商品、配件和包装完整。寄回前与商家确认地址，登记寄回运单。"
                    "当前为演示业务，不结算运费。"
                ),
            }
        ],
    )
    # 旧版没有寄回配置记录，显式标记未知，不替历史工单推定承担方或真实地址。
    op.execute(
        sa.text(
            "UPDATE return_cases SET settings_snapshot = CAST(:snapshot AS JSON) "
            "WHERE settings_snapshot::jsonb = '{}'::jsonb"
        ).bindparams(
            snapshot=(
                '{"version":0,"shipping_payer":null,'
                '"return_address":"历史工单未记录寄回地址，请与管理员确认后再寄回",'
                '"instructions":"历史记录未确定运费承担方，请人工确认"}'
            )
        )
    )


def downgrade():
    # 多工单和部分退款无法无损降为旧版一单一工单，禁止破坏性回退。
    raise RuntimeError("此迁移包含多工单历史，请从备份恢复旧版，不执行破坏性降级")
