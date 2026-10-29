"""Durable support deadlines, unanswered-message recovery and workflow reminders."""

import sqlalchemy as sa
from alembic import op

revision = "0021_flow_recovery"
down_revision = "0020_human_conversation"
branch_labels = None
depends_on = None


def upgrade():
    for name in ("support_wait_since", "support_activity_at", "support_extended_until"):
        op.add_column("delivery_projects", sa.Column(name, sa.DateTime(timezone=True)))
    op.add_column(
        "delivery_projects",
        sa.Column("support_notice", sa.String(24), nullable=False, server_default=""),
    )
    op.add_column(
        "delivery_projects",
        sa.Column("human_only", sa.Boolean(), nullable=False, server_default=sa.false()),
    )
    op.add_column(
        "delivery_projects",
        sa.Column("answered_through", sa.Integer(), nullable=False, server_default="0"),
    )
    for name in ("auto_reply_message_id", "auto_reply_request_id"):
        op.add_column("delivery_projects", sa.Column(name, sa.Uuid()))
    op.add_column("project_messages", sa.Column("login_session_id", sa.Uuid()))
    for name in ("workflow_activity_at", "workflow_reminded_at"):
        op.add_column("project_items", sa.Column(name, sa.DateTime(timezone=True)))
    op.execute("""UPDATE delivery_projects p SET answered_through = COALESCE(
        (SELECT max(sequence) FROM project_messages m WHERE m.project_id=p.id
         AND m.sender_type IN ('ai','support')),0),
        support_wait_since = CASE WHEN conversation_mode='human' THEN now() ELSE NULL END,
        support_activity_at = CASE WHEN conversation_mode='human' THEN now() ELSE NULL END""")
    op.execute("UPDATE project_items SET workflow_activity_at=created_at")
    op.execute("""UPDATE delivery_projects p SET human_only=true
        WHERE conversation_mode IN ('human','pending') AND EXISTS (
          SELECT 1 FROM project_messages m,
            LATERAL regexp_split_to_table(m.content, '[，,。；;！!？?\\n]') AS words(part)
          WHERE m.project_id=p.id AND m.sender_type='customer' AND
            trim(words.part) ~* (
              '^(请|我|现在|这次|本次)?(只要(人工|真人)(客服)?|'
              || '不要(AI|人工智能|机器人)(回复|回答|接待)?)(了|吧|谢谢)?$')
        )""")
    op.create_index(
        "ix_project_support_due", "delivery_projects", ["conversation_mode", "support_wait_since"]
    )


def downgrade():
    op.drop_index("ix_project_support_due", table_name="delivery_projects")
    for name in ("workflow_activity_at", "workflow_reminded_at"):
        op.drop_column("project_items", name)
    op.drop_column("project_messages", "login_session_id")
    for name in (
        "auto_reply_request_id",
        "auto_reply_message_id",
        "answered_through",
        "human_only",
        "support_notice",
        "support_extended_until",
        "support_activity_at",
        "support_wait_since",
    ):
        op.drop_column("delivery_projects", name)
