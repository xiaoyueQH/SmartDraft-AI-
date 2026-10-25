"""Version execution snapshots for safe cache reuse across processes."""

import sqlalchemy as sa
from alembic import op

revision = "0015_run_cache_version"
down_revision = "0014_consultation_workflow"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "project_runs", sa.Column("cache_version", sa.Integer(), nullable=False, server_default="1")
    )


def downgrade():
    op.drop_column("project_runs", "cache_version")
