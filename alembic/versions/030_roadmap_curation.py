"""Curated roadmap — archived, merged_into, roadmap_curation_proposals.

Spec 2026-07-04 §1. Pattern 029 (proposer-only, human review → apply).
- features.status: + 'archived' in the CHECK.
- features.merged_into: a self-referencing FK with NO ON DELETE (features are
  never DELETEd — the feature_artifacts FK is ON DELETE CASCADE, so deleting
  would erase the linking history). No CHECK constrains merged_into
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
    # 1. Status CHECK: + 'archived' (constraint auto-named by 005).
    op.execute("ALTER TABLE features DROP CONSTRAINT features_status_check")
    op.execute(
        """
        ALTER TABLE features ADD CONSTRAINT features_status_check
        CHECK (status IN ('planned', 'research', 'design', 'building',
                          'deployed', 'done', 'archived'))
        """
    )

    # 2. merged_into — same pattern as decisions/learnings (007), with an FK.
    op.execute("ALTER TABLE features ADD COLUMN merged_into UUID REFERENCES features(id)")

    # 3. proposals table — mirrors ticket_extraction_proposals (029).
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
    # 'archived' rows would violate the restored CHECK → back to 'research'
    # (the pre-curation ClusterGuard state). Downgrade accepts the info loss.
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
