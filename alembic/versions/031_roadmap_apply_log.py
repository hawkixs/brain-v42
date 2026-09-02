"""Apply provenance — roadmap_curation_proposals.apply_log.

Measured 2026-07-05: roadmap merges are irreversible in practice (artifacts
re-pointed, then duplicate links DELETEd without a trace — 86 artifacts
commingled on "architect plan data-lab-endpoints", unmergeable by hand).
apply_log captures, at apply time, enough to reverse:
- merge   : into, the loser's prior status/name, artifacts moved,
            duplicate links removed
- archive : prior status
- status  : prior status
- rename  : prior name

Revision ID: 031
Revises: 030
"""

from alembic import op

revision = "031"
down_revision = "030"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE roadmap_curation_proposals ADD COLUMN apply_log JSONB")


def downgrade() -> None:
    op.execute("ALTER TABLE roadmap_curation_proposals DROP COLUMN IF EXISTS apply_log")
