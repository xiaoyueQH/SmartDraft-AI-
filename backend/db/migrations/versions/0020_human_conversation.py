"""Human handover, message authors and resumable silent extraction cursors."""

import sqlalchemy as sa
from alembic import op

revision = "0020_human_conversation"
down_revision = "0019_fact_evidence"
branch_labels = None
depends_on = None


def upgrade():
    for name, kind, default in (
        ("conversation_mode", sa.String(16), "ai"),
        ("conversation_version", sa.Integer(), "1"),
        ("message_seq", sa.Integer(), "0"),
        ("facts_cursor", sa.Integer(), "0"),
        ("extraction_until", sa.Integer(), "0"),
        ("extraction_error", sa.String(300), ""),
    ):
        op.add_column(
            "delivery_projects", sa.Column(name, kind, nullable=False, server_default=default)
        )
    op.add_column(
        "delivery_projects",
        sa.Column("support_id", sa.Uuid(), sa.ForeignKey("users.id"), nullable=True),
    )
    op.add_column(
        "delivery_projects",
        sa.Column("extraction_started_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column("project_messages", sa.Column("sequence", sa.Integer(), nullable=True))
    op.add_column(
        "project_messages",
        sa.Column("sender_id", sa.Uuid(), sa.ForeignKey("users.id"), nullable=True),
    )
    op.add_column(
        "project_messages",
        sa.Column("sender_type", sa.String(16), nullable=False, server_default="legacy"),
    )
    op.add_column(
        "project_messages",
        sa.Column("sender_name", sa.String(100), nullable=False, server_default=""),
    )
    op.add_column("project_messages", sa.Column("reply_to_id", sa.Uuid(), nullable=True))
    op.execute("""WITH ranked AS (
        SELECT id, row_number() OVER (PARTITION BY project_id ORDER BY created_at, id) AS seq
        FROM project_messages
    ) UPDATE project_messages m SET sequence = ranked.seq FROM ranked WHERE m.id = ranked.id""")
    op.execute("""UPDATE project_messages m SET sender_id = r.actor_id,
        sender_type = CASE WHEN r.actor_id = p.owner_id THEN 'customer' ELSE 'support' END,
        sender_name = u.display_name
        FROM project_runs r, delivery_projects p, users u
        WHERE m.run_id = r.id AND m.project_id = p.id AND r.actor_id = u.id AND m.role = 'user'""")
    op.execute(
        "UPDATE project_messages SET sender_type = 'ai', sender_name = 'AI顾问' "
        "WHERE role = 'assistant'"
    )
    op.execute("UPDATE project_messages SET role = 'assistant' WHERE sender_type = 'support'")
    op.execute("""UPDATE delivery_projects p SET message_seq = COALESCE(
        (SELECT max(m.sequence) FROM project_messages m WHERE m.project_id=p.id), 0)""")
    op.execute(
        "UPDATE delivery_projects SET facts_cursor = message_seq "
        "WHERE NOT COALESCE((consultation->>'memory_pending')::boolean, false)"
    )
    op.create_index(
        "uq_project_message_seq", "project_messages", ["project_id", "sequence"], unique=True
    )
    op.create_check_constraint(
        "ck_conversation_mode", "delivery_projects", "conversation_mode IN ('ai','pending','human')"
    )
    op.create_check_constraint(
        "ck_message_sender_type",
        "project_messages",
        "sender_type IN ('customer','support','ai','system','legacy')",
    )


def downgrade():
    op.drop_constraint("ck_conversation_mode", "delivery_projects", type_="check")
    op.drop_constraint("ck_message_sender_type", "project_messages", type_="check")
    op.drop_index("uq_project_message_seq", table_name="project_messages")
    for name in ("reply_to_id", "sender_name", "sender_type", "sender_id", "sequence"):
        op.drop_column("project_messages", name)
    for name in (
        "extraction_started_at",
        "support_id",
        "extraction_error",
        "extraction_until",
        "facts_cursor",
        "message_seq",
        "conversation_version",
        "conversation_mode",
    ):
        op.drop_column("delivery_projects", name)
