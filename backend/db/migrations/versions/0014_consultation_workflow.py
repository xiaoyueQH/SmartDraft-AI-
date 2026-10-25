"""Persist consultation checkpoints, run options and global tool control."""

import sqlalchemy as sa
from alembic import op

revision = "0014_consultation_workflow"
down_revision = "0013_message_run"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "delivery_projects",
        sa.Column("consultation", sa.JSON(), nullable=False, server_default="{}"),
    )
    op.alter_column("delivery_projects", "stack", type_=sa.String(200), existing_type=sa.String(32))
    op.add_column(
        "project_runs", sa.Column("options", sa.JSON(), nullable=False, server_default="{}")
    )
    settings = op.create_table(
        "agent_tool_settings",
        sa.Column("name", sa.String(40), primary_key=True),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
    )
    op.bulk_insert(settings, [{"name": "web_search", "enabled": False, "version": 1}])
    op.create_table(
        "agent_tool_changes",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("actor_id", sa.Uuid(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("request_id", sa.Uuid(), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("expected_version", sa.Integer(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index(
        "uq_tool_change_request", "agent_tool_changes", ["actor_id", "request_id"], unique=True
    )

    for name, writes in (
        ("consultation_checkpoints", False),
        ("consultation_checkpoint_writes", True),
    ):
        columns = [
            sa.Column(
                "project_id", sa.Uuid(), sa.ForeignKey("delivery_projects.id"), primary_key=True
            ),
            sa.Column("thread_id", sa.String(100), primary_key=True),
            sa.Column("namespace", sa.String(100), primary_key=True),
            sa.Column("checkpoint_id", sa.String(80), primary_key=True),
            sa.Column("encoding", sa.String(40), nullable=False),
            sa.Column("payload", sa.LargeBinary(), nullable=False),
        ]
        if writes:
            columns += [
                sa.Column("task_id", sa.String(100), primary_key=True),
                sa.Column("position", sa.Integer(), primary_key=True),
                sa.Column("channel", sa.String(100), nullable=False),
            ]
        else:
            columns += [
                sa.Column("parent_id", sa.String(80), nullable=True),
                sa.Column("metadata_encoding", sa.String(40), nullable=False),
                sa.Column("metadata_payload", sa.LargeBinary(), nullable=False),
            ]
        op.create_table(name, *columns)


def downgrade():
    op.drop_table("consultation_checkpoint_writes")
    op.drop_table("consultation_checkpoints")
    op.drop_index("uq_tool_change_request", table_name="agent_tool_changes")
    op.drop_table("agent_tool_changes")
    op.drop_table("agent_tool_settings")
    op.drop_column("project_runs", "options")
    op.drop_column("delivery_projects", "consultation")
    op.alter_column("delivery_projects", "stack", type_=sa.String(32), existing_type=sa.String(200))
