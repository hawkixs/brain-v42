"""Dream v3 — relax dream_promotions CHECK to admit tombstone state.

Migration 016 constrained target_type='adr' to require target_adr_id NOT NULL,
and likewise for runbook. But the same migration also declared
target_adr_id / target_runbook_id FKs with ON DELETE SET NULL so the audit
trail survives a target hard-delete.

Those two decisions conflict: when an ADR is hard-deleted, PG tries to
SET NULL target_adr_id in the audit row, which now has shape
(target_type='adr', target_adr_id=NULL) — and PG evaluates CHECK on the
resulting row, so the DELETE fails. CHECK constraints are not deferrable,
so no workaround exists at runtime.

Fix: widen the CHECK to admit the tombstone shape. After this migration:
- target_type='adr'      -> target_adr_id may be NULL (tombstone) or NOT NULL
                            (live); target_runbook_id MUST be NULL.
- target_type='runbook'  -> symmetric.
- other target_types     -> both FK columns MUST be NULL (unchanged).

The PROMOTE candidate filter already treats target_adr_id IS NULL as "target
no longer alive, source re-qualifies" (scripts/dream/promote_prepare.py).
Nothing else changes.

Revision ID: 017
Revises: 016
Create Date: 2026-04-18
"""

from __future__ import annotations

from alembic import op

revision = "017"
down_revision = "016"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE dream_promotions DROP CONSTRAINT dream_promotions_target_shape")
    op.execute(
        """
        ALTER TABLE dream_promotions
        ADD CONSTRAINT dream_promotions_target_shape CHECK (
            (target_type = 'adr' AND target_runbook_id IS NULL)
            OR (target_type = 'runbook' AND target_adr_id IS NULL)
            OR (target_type IN ('skipped_dedup', 'dry_run',
                                'classification_uncertain', 'dedup_unavailable')
                AND target_adr_id IS NULL AND target_runbook_id IS NULL)
        )
        """
    )


def downgrade() -> None:
    op.execute("ALTER TABLE dream_promotions DROP CONSTRAINT dream_promotions_target_shape")
    op.execute(
        """
        ALTER TABLE dream_promotions
        ADD CONSTRAINT dream_promotions_target_shape CHECK (
            (target_type = 'adr'
                AND target_adr_id IS NOT NULL AND target_runbook_id IS NULL)
            OR (target_type = 'runbook'
                AND target_runbook_id IS NOT NULL AND target_adr_id IS NULL)
            OR (target_type IN ('skipped_dedup', 'dry_run',
                                'classification_uncertain', 'dedup_unavailable')
                AND target_adr_id IS NULL AND target_runbook_id IS NULL)
        )
        """
    )
