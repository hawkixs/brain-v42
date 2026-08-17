"""Roadmap V2: indexed_plans, gitlab_events, feature pinning, project scan config.

Revision ID: 009
Revises: 008
Create Date: 2026-03-14
"""

from __future__ import annotations

from alembic import op

revision = "009"
down_revision = "008"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # ── 1. ALTER features: add pinned column ──────────────────────────────────
    op.execute("ALTER TABLE features ADD COLUMN pinned BOOLEAN DEFAULT FALSE")

    # ── 2. ALTER project_contexts: add plan_scan_paths + gitlab_project_path ──
    op.execute("ALTER TABLE project_contexts ADD COLUMN plan_scan_paths JSONB DEFAULT '[]'")
    op.execute("ALTER TABLE project_contexts ADD COLUMN gitlab_project_path VARCHAR(200)")

    # ── 3. CREATE indexed_plans table ─────────────────────────────────────────
    op.execute("""
        CREATE TABLE indexed_plans (
            id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            file_path VARCHAR(500) UNIQUE NOT NULL,
            title VARCHAR(200) NOT NULL,
            plan_type VARCHAR(20) NOT NULL
                CHECK (plan_type IN ('spec', 'plan')),
            project_key VARCHAR(50) NOT NULL,
            content_hash VARCHAR(64) NOT NULL,
            embedding VECTOR(1536),
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )
    """)

    # ── 4. indexed_plans indexes ──────────────────────────────────────────────
    op.execute("CREATE INDEX idx_indexed_plans_project_key ON indexed_plans(project_key)")
    op.execute("""
        CREATE INDEX idx_indexed_plans_embedding ON indexed_plans
        USING hnsw (embedding vector_cosine_ops) WITH (m = 16, ef_construction = 64)
    """)

    # ── 5. indexed_plans updated_at trigger ───────────────────────────────────
    op.execute("""
        CREATE TRIGGER set_indexed_plans_updated_at
        BEFORE UPDATE ON indexed_plans
        FOR EACH ROW EXECUTE FUNCTION update_updated_at()
    """)

    # ── 6. CREATE gitlab_events table ─────────────────────────────────────────
    op.execute("""
        CREATE TABLE gitlab_events (
            id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            gitlab_event_id VARCHAR(100) UNIQUE,
            event_type VARCHAR(30) NOT NULL,
            project_key VARCHAR(50) NOT NULL,
            gitlab_project_id INTEGER,
            ref VARCHAR(200),
            title VARCHAR(500),
            embedding VECTOR(1536),
            feature_id UUID REFERENCES features(id),
            processed_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )
    """)

    # ── 7. gitlab_events indexes ──────────────────────────────────────────────
    op.execute("CREATE INDEX idx_gitlab_events_project_key ON gitlab_events(project_key)")
    op.execute("CREATE INDEX idx_gitlab_events_event_type ON gitlab_events(event_type)")
    op.execute("CREATE INDEX idx_gitlab_events_feature_id ON gitlab_events(feature_id)")
    op.execute("""
        CREATE INDEX idx_gitlab_events_embedding ON gitlab_events
        USING hnsw (embedding vector_cosine_ops) WITH (m = 16, ef_construction = 64)
    """)

    # ── 8. Update feature_artifacts CHECK to include 'plan', 'gitlab_event' ──
    op.execute(
        "ALTER TABLE feature_artifacts DROP CONSTRAINT feature_artifacts_artifact_type_check"
    )
    op.execute("""
        ALTER TABLE feature_artifacts ADD CONSTRAINT feature_artifacts_artifact_type_check
        CHECK (artifact_type IN ('learning', 'decision', 'snippet', 'runbook', 'adr', 'plan', 'gitlab_event'))
    """)


def downgrade() -> None:
    # Restore original CHECK constraint
    op.execute(
        "ALTER TABLE feature_artifacts DROP CONSTRAINT feature_artifacts_artifact_type_check"
    )
    op.execute("""
        ALTER TABLE feature_artifacts ADD CONSTRAINT feature_artifacts_artifact_type_check
        CHECK (artifact_type IN ('learning', 'decision', 'snippet', 'runbook', 'adr'))
    """)

    op.execute("DROP TABLE IF EXISTS gitlab_events CASCADE")
    op.execute("DROP TABLE IF EXISTS indexed_plans CASCADE")
    op.execute("ALTER TABLE project_contexts DROP COLUMN IF EXISTS gitlab_project_path")
    op.execute("ALTER TABLE project_contexts DROP COLUMN IF EXISTS plan_scan_paths")
    op.execute("ALTER TABLE features DROP COLUMN IF EXISTS pinned")
