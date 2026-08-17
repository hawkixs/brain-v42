"""Roadmap curée — archived, merged_into, roadmap_curation_proposals.

Spec 2026-07-04 §1. Pattern 029 (proposer-only, review humaine → apply).
- features.status : + 'archived' dans le CHECK.
- features.merged_into : FK self-ref SANS ON DELETE (les features ne sont
  jamais DELETE — le FK feature_artifacts est ON DELETE CASCADE, supprimer
  effacerait l'historique de liage). Aucun CHECK ne contraint merged_into
  (gotcha postgres-check-vs-on-delete-set-null).

Revision ID: 030
Revises: 029
"""

from alembic import op

revision = "030"
down_revision = "029"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # 1. CHECK statut : + 'archived' (constraint auto-nommée par la 005).
    op.execute("ALTER TABLE features DROP CONSTRAINT features_status_check")
    op.execute(
        """
        ALTER TABLE features ADD CONSTRAINT features_status_check
        CHECK (status IN ('planned', 'research', 'design', 'building',
                          'deployed', 'done', 'archived'))
        """
    )

    # 2. merged_into — même pattern que decisions/learnings (007), avec FK.
    op.execute("ALTER TABLE features ADD COLUMN merged_into UUID REFERENCES features(id)")

    # 3. Table proposals — miroir de ticket_extraction_proposals (029).
    op.execute(
        """
        CREATE TABLE roadmap_curation_proposals (
            id BIGSERIAL PRIMARY KEY,
            op VARCHAR(10) NOT NULL,
            feature_id UUID NOT NULL REFERENCES features(id) ON DELETE CASCADE,
            payload JSONB NOT NULL,
            rationale TEXT,
            status VARCHAR(10) NOT NULL DEFAULT 'proposed',
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            applied_at TIMESTAMPTZ,

            CONSTRAINT rcp_op_valid CHECK (op IN ('merge', 'archive', 'status', 'rename')),
            CONSTRAINT rcp_status_valid CHECK (status IN ('proposed', 'applied', 'rejected'))
        )
        """
    )
    op.execute("CREATE INDEX idx_rcp_status ON roadmap_curation_proposals (status)")
    op.execute("CREATE INDEX idx_rcp_feature ON roadmap_curation_proposals (feature_id)")


def downgrade() -> None:
    # Les rows 'archived' violeraient le CHECK restauré → retour à 'research'
    # (état pré-curation ClusterGuard). Perte d'info assumée en downgrade.
    op.execute("UPDATE features SET status = 'research' WHERE status = 'archived'")
    op.execute("DROP TABLE IF EXISTS roadmap_curation_proposals;")
    op.execute("ALTER TABLE features DROP COLUMN IF EXISTS merged_into;")
    op.execute("ALTER TABLE features DROP CONSTRAINT features_status_check")
    op.execute(
        """
        ALTER TABLE features ADD CONSTRAINT features_status_check
        CHECK (status IN ('planned', 'research', 'design', 'building', 'deployed', 'done'))
        """
    )
