"""Retain fact provenance independently of bounded consultation state."""

import sqlalchemy as sa
from alembic import op

revision = "0019_fact_evidence"
down_revision = "0018_current_requirement"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "project_fact_evidence",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("project_id", sa.Uuid(), sa.ForeignKey("delivery_projects.id"), nullable=False),
        sa.Column("recorded_run_id", sa.Uuid(), sa.ForeignKey("project_runs.id"), nullable=False),
        sa.Column("source", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_project_fact_evidence_project_id", "project_fact_evidence", ["project_id"])
    op.create_index(
        "ix_project_fact_evidence_recorded_run_id", "project_fact_evidence", ["recorded_run_id"]
    )


def downgrade():
    op.drop_table("project_fact_evidence")
