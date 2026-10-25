"""Record lifecycle and audit, preserving requirement and conversation history."""

import sqlalchemy as sa
from alembic import op

revision = "0012_record_management"
down_revision = "0011_consultation_first"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "delivery_projects", sa.Column("version", sa.Integer(), nullable=False, server_default="1")
    )
    op.add_column(
        "delivery_projects", sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True)
    )
    op.add_column("delivery_projects", sa.Column("deleted_by", sa.Uuid(), nullable=True))
    op.create_foreign_key(
        "fk_project_deleted_by", "delivery_projects", "users", ["deleted_by"], ["id"]
    )
    op.create_index(
        "ix_delivery_projects_deleted_created", "delivery_projects", ["deleted_at", "created_at"]
    )
    op.create_table(
        "project_record_events",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("project_id", sa.Uuid(), sa.ForeignKey("delivery_projects.id"), nullable=False),
        sa.Column("actor_id", sa.Uuid(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("request_id", sa.Uuid(), nullable=False),
        sa.Column("action", sa.String(24), nullable=False),
        sa.Column("snapshot", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index(
        "ix_record_events_project_created", "project_record_events", ["project_id", "created_at"]
    )


def downgrade():
    op.drop_table("project_record_events")
    op.drop_index("ix_delivery_projects_deleted_created", table_name="delivery_projects")
    op.drop_constraint("fk_project_deleted_by", "delivery_projects", type_="foreignkey")
    for column in ("deleted_by", "deleted_at", "version"):
        op.drop_column("delivery_projects", column)
