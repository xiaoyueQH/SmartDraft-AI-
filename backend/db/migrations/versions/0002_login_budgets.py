"""Shared login rate budgets."""

import sqlalchemy as sa
from alembic import op

revision = "0002_login_budgets"
down_revision = "0001_auth"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "auth_login_buckets",
        sa.Column("key", sa.String(64), primary_key=True),
        sa.Column("window_started", sa.DateTime(timezone=True), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False),
    )
    op.create_index("ix_auth_login_buckets_window", "auth_login_buckets", ["window_started"])


def downgrade():
    op.drop_table("auth_login_buckets")
