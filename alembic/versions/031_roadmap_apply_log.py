"""Provenance d'apply — roadmap_curation_proposals.apply_log.

Constat 2026-07-05 : les merges roadmap sont irréversibles en pratique
(artifacts re-pointés puis liens doublons DELETE sans trace — 86 artifacts
commingés sur « architect plan data-lab-endpoints », défusion impraticable).
apply_log capture, au moment de l'apply, de quoi réverser :
- merge   : into, statut/nom antérieurs du perdant, artifacts déplacés,
            liens doublons supprimés
- archive : statut antérieur
- status  : statut antérieur
- rename  : nom antérieur

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
