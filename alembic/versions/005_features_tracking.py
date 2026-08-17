"""Add features and feature_artifacts tables for roadmap tracking.

Revision ID: 005
Revises: 004
Create Date: 2026-03-13
"""

from __future__ import annotations

from alembic import op

revision = "005"
down_revision = "004"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # ── 1. Create features table ─────────────────────────────────────────────
    op.execute("""
        CREATE TABLE features (
            id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            project_key VARCHAR(50) NOT NULL,
            name VARCHAR(200) NOT NULL,
            description TEXT NOT NULL,
            embedding VECTOR(1536),
            status VARCHAR(20) NOT NULL DEFAULT 'planned'
                CHECK (status IN ('planned', 'research', 'design', 'building', 'deployed', 'done')),
            status_updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )
    """)

    # ── 2. Features indexes ──────────────────────────────────────────────────
    op.execute("CREATE INDEX idx_features_project_key ON features(project_key)")
    op.execute("CREATE INDEX idx_features_status ON features(status)")
    op.execute("""
        CREATE INDEX idx_features_embedding ON features
        USING hnsw (embedding vector_cosine_ops) WITH (m = 16, ef_construction = 64)
    """)

    # ── 3. Features updated_at trigger ───────────────────────────────────────
    op.execute("""
        CREATE TRIGGER set_features_updated_at
        BEFORE UPDATE ON features
        FOR EACH ROW EXECUTE FUNCTION update_updated_at()
    """)

    # ── 4. Create feature_artifacts table ────────────────────────────────────
    op.execute("""
        CREATE TABLE feature_artifacts (
            feature_id UUID NOT NULL REFERENCES features(id) ON DELETE CASCADE,
            artifact_type VARCHAR(20) NOT NULL
                CHECK (artifact_type IN ('learning', 'decision', 'snippet', 'runbook', 'adr')),
            artifact_id UUID NOT NULL,
            similarity_score FLOAT NOT NULL,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            UNIQUE (feature_id, artifact_type, artifact_id)
        )
    """)

    # ── 5. Feature artifacts indexes ─────────────────────────────────────────
    op.execute("CREATE INDEX idx_feature_artifacts_feature_id ON feature_artifacts(feature_id)")
    op.execute(
        "CREATE INDEX idx_feature_artifacts_artifact ON feature_artifacts(artifact_type, artifact_id)"
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS feature_artifacts CASCADE")
    op.execute("DROP TABLE IF EXISTS features CASCADE")
