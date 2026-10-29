"""Keep one current unpublished initial requirement per project."""

import sqlalchemy as sa
from alembic import op

revision = "0018_current_requirement"
down_revision = "0017_document_review"
branch_labels = None
depends_on = None


def upgrade():
    connection = op.get_bind()
    if connection.scalar(
        sa.text("SELECT count(*) FROM project_runs WHERE status = 'running'")
    ) or connection.scalar(
        sa.text("SELECT count(*) FROM document_reviews WHERE status = 'running'")
    ):
        raise RuntimeError("Finish active Agent/review runs before consolidating requirements")
    connection.execute(sa.text("LOCK TABLE project_items IN SHARE ROW EXCLUSIVE MODE"))
    connection.execute(
        sa.text("""
        CREATE TEMP TABLE sf_requirement_obsolete ON COMMIT DROP AS
        SELECT id, project_id FROM (
            SELECT i.id, i.project_id, p.revision,
                row_number() OVER (
                    PARTITION BY i.project_id ORDER BY i.created_at DESC, i.id DESC
                ) AS position
            FROM project_items i JOIN delivery_projects p ON p.id = i.project_id
            WHERE i.kind = 'specification' AND i.published_revision IS NULL
        ) ranked WHERE position > 1 OR revision > 0
    """)
    )
    for table in ("consultation_checkpoint_writes", "consultation_checkpoints"):
        connection.execute(
            sa.text(f"""
            DELETE FROM {table} WHERE thread_id IN (
                SELECT 'review:' || r.id::text FROM document_reviews r
                JOIN sf_requirement_obsolete old ON old.id = r.item_id
            )
        """)
        )
    connection.execute(
        sa.text("""
        UPDATE project_operations SET result = '{"_superseded_requirement": true}'::json
        WHERE result->>'id' IN (SELECT id::text FROM sf_requirement_obsolete)
           OR result->'item'->>'id' IN (SELECT id::text FROM sf_requirement_obsolete)
    """)
    )
    connection.execute(
        sa.text(
            "DELETE FROM document_reviews WHERE item_id IN (SELECT id FROM sf_requirement_obsolete)"
        )
    )
    connection.execute(
        sa.text(
            "DELETE FROM document_revisions "
            "WHERE item_id IN (SELECT id FROM sf_requirement_obsolete)"
        )
    )
    connection.execute(
        sa.text("""
        UPDATE delivery_projects SET version = version + 1
        WHERE id IN (SELECT project_id FROM sf_requirement_obsolete)
    """)
    )
    connection.execute(
        sa.text("DELETE FROM project_items WHERE id IN (SELECT id FROM sf_requirement_obsolete)")
    )
    op.create_index(
        "uq_current_requirement",
        "project_items",
        ["project_id"],
        unique=True,
        postgresql_where=sa.text("kind = 'specification' AND published_revision IS NULL"),
    )


def downgrade():
    # Removed duplicates require restoring the pre-migration backup, not a schema downgrade.
    op.drop_index("uq_current_requirement", table_name="project_items")
