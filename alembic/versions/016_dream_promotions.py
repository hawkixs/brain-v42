"""Dream v3 — dream_promotions audit table for PROMOTE phase.

One row per PROMOTE-phase candidate evaluation outcome. Covers:
- Successful ADR promotion (target_type='adr', target_adr_id set)
- Successful Runbook promotion (target_type='runbook', target_runbook_id set)
- Dedup-skip, classification-uncertain, dedup-unavailable, dry-run paths
  (target_type set, both target FKs NULL)

Design constraints (see docs/superpowers/specs/2026-04-17-dream-v3-actionability-design.md):
- source_learning_id is nullable + ON DELETE SET NULL to preserve audit
  history when a learning is hard-deleted.
- CHECK constraint enforces target_type <-> FK-nullness coherence so
  inconsistent rows cannot be inserted via direct SQL or buggy tools.
- Partial unique index on source_learning_id WHERE target_type IN
  ('adr','runbook') prevents the SELECT-then-INSERT race under READ
  COMMITTED from double-materializing the same learning.

Revision ID: 016
Revises: 015
Create Date: 2026-04-17
"""

from __future__ import annotations

from alembic import op

revision = "016"
down_revision = "015"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE dream_promotions (
            id                  BIGSERIAL PRIMARY KEY,
            dream_run_id        INTEGER REFERENCES dream_runs(id) ON DELETE SET NULL,
            source_learning_id  UUID REFERENCES learnings(id) ON DELETE SET NULL,
            target_type         VARCHAR(30) NOT NULL,
            target_adr_id       UUID REFERENCES adrs(id) ON DELETE SET NULL,
            target_runbook_id   UUID REFERENCES runbooks(id) ON DELETE SET NULL,
            cosine_observed     FLOAT,
            skipped_reason      VARCHAR(100),
            created_at          TIMESTAMPTZ DEFAULT NOW(),

            CONSTRAINT dream_promotions_target_shape CHECK (
                (target_type = 'adr'
                    AND target_adr_id IS NOT NULL AND target_runbook_id IS NULL)
                OR (target_type = 'runbook'
                    AND target_runbook_id IS NOT NULL AND target_adr_id IS NULL)
                OR (target_type IN ('skipped_dedup', 'dry_run',
                                    'classification_uncertain', 'dedup_unavailable')
                    AND target_adr_id IS NULL AND target_runbook_id IS NULL)
            )
        )
        """
    )
    op.execute("CREATE INDEX idx_dream_promotions_source ON dream_promotions(source_learning_id)")
    op.execute("CREATE INDEX idx_dream_promotions_created ON dream_promotions(created_at DESC)")
    op.execute(
        "CREATE UNIQUE INDEX idx_dream_promotions_source_materialized"
        " ON dream_promotions(source_learning_id)"
        " WHERE target_type IN ('adr', 'runbook')"
        " AND source_learning_id IS NOT NULL"
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS dream_promotions;")
