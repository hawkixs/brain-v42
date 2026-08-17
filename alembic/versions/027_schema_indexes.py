"""Add missing indexes: consolidation_log entity_type + indexed_plans updated_at.

Revision ID: 027
Revises: 026
Create Date: 2026-07-02

What this migration creates
---------------------------
1. idx_consolidation_log_entity_type (entity_type)
   - get_handled_pairs() in PgConsolidationLogRepo does:
       SELECT source_id, target_id FROM consolidation_log WHERE entity_type = ?
     With zero indexes on the table, every consolidation run does a full seq-scan.
     A B-tree on entity_type eliminates the scan (low-cardinality: ~5 values).

2. idx_indexed_plans_updated_at (updated_at DESC)
   - list_plans() in PgIndexedPlanRepo always appends ORDER BY updated_at DESC.
     The existing indexes (project_key, embedding, tags, search_vector, pk_status_fresh)
     cannot satisfy this sort; a dedicated descending index avoids a filesort.

What this migration does NOT do
--------------------------------
- Does NOT create idx_indexed_plans_tags, idx_indexed_plans_search_vector, or
  idx_indexed_plans_pk_status_fresh — these were already created in DB by migration
  014 (plan_chunks).  They are now *declared* in tables.py for autogenerate
  coverage, but no SQL action is needed here.

- Does NOT create indexed_plan_chunks table — it already exists from migration 014.
  Its declaration in tables.py is declaration-only (no DB change).

Downgrade is symmetric: drop the two indexes created above.
"""

from __future__ import annotations

from alembic import op

revision = "027"
down_revision = "026"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # 1. consolidation_log: B-tree on entity_type for get_handled_pairs WHERE clause.
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_consolidation_log_entity_type "
        "ON consolidation_log (entity_type)"
    )

    # 2. indexed_plans: descending index on updated_at for ORDER BY updated_at DESC.
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_indexed_plans_updated_at ON indexed_plans (updated_at DESC)"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS idx_indexed_plans_updated_at")
    op.execute("DROP INDEX IF EXISTS idx_consolidation_log_entity_type")
