"""Separate initial business discovery from confirmed projects."""

import sqlalchemy as sa
from alembic import op

revision = "0011_consultation_first"
down_revision = "0010_delivery_projects"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "delivery_projects",
        sa.Column("phase", sa.String(16), nullable=False, server_default="discovery"),
    )
    op.add_column(
        "delivery_projects",
        sa.Column("discovery", sa.JSON(), nullable=False, server_default=sa.text("'{}'")),
    )
    op.execute("UPDATE delivery_projects SET phase = 'project' WHERE revision > 0")
    op.create_check_constraint(
        "ck_project_phase", "delivery_projects", "phase IN ('discovery','project')"
    )


def downgrade():
    op.drop_constraint("ck_project_phase", "delivery_projects", type_="check")
    op.drop_column("delivery_projects", "discovery")
    op.drop_column("delivery_projects", "phase")
