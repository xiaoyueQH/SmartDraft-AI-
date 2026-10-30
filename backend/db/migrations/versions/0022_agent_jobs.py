"""Durable job references and independent worker liveness."""

import sqlalchemy as sa
from alembic import op

revision = "0022_agent_jobs"
down_revision = "0021_flow_recovery"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "agent_jobs",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("kind", sa.String(12), nullable=False),
        sa.Column("target_id", sa.Uuid(), nullable=False),
        sa.Column("project_id", sa.Uuid(), sa.ForeignKey("delivery_projects.id"), nullable=False),
        sa.Column("actor_id", sa.Uuid(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("session_id", sa.Uuid(), nullable=False),
        sa.Column("item_id", sa.Uuid()),
        sa.Column("status", sa.String(12), nullable=False),
        sa.Column("token", sa.Uuid()),
        sa.Column("worker_id", sa.String(64), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("settled", sa.Boolean(), nullable=False),
        sa.Column("error", sa.String(300), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("queued_until", sa.DateTime(timezone=True), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True)),
        sa.Column("lease_until", sa.DateTime(timezone=True)),
        sa.Column("finished_at", sa.DateTime(timezone=True)),
        sa.CheckConstraint("kind IN ('run','review')", name="ck_agent_job_kind"),
        sa.CheckConstraint(
            "status IN ('queued','running','completed','failed')", name="ck_agent_job_status"
        ),
    )
    op.create_index("uq_agent_job_target", "agent_jobs", ["kind", "target_id"], unique=True)
    op.create_index("ix_agent_job_claim", "agent_jobs", ["status", "created_at", "id"])
    op.create_index("ix_agent_job_lease", "agent_jobs", ["status", "lease_until"])
    op.create_index("ix_agent_job_actor", "agent_jobs", ["actor_id", "status"])
    op.create_table(
        "agent_workers",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("heartbeat_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("accepting", sa.Boolean(), nullable=False),
    )
    op.create_index("ix_agent_workers_heartbeat_at", "agent_workers", ["heartbeat_at"])


def downgrade():
    # Do not silently drop accepted work during a rollback.
    bind = op.get_bind()
    if bind.scalar(
        sa.text(
            "SELECT count(*) FROM agent_jobs WHERE status IN ('queued','running') OR NOT settled"
        )
    ):
        raise RuntimeError("仍有待处理或待收尾任务，请先停止受理并完成任务后再回退")
    op.drop_table("agent_workers")
    op.drop_table("agent_jobs")
