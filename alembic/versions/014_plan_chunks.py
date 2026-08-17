# alembic/versions/014_plan_chunks.py
"""Plan chunking support: extend indexed_plans + create indexed_plan_chunks.

Revision ID: 014
Revises: 013
Create Date: 2026-04-07
"""

from __future__ import annotations

from alembic import op

revision = "014"
down_revision = "013"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # ── 1. Extend indexed_plans ───────────────────────────────────────────────
    op.execute("""
        ALTER TABLE indexed_plans
            ADD COLUMN content TEXT NOT NULL DEFAULT '',
            ADD COLUMN summary TEXT,
            ADD COLUMN search_vector TSVECTOR,
            ADD COLUMN tags VARCHAR[] NOT NULL DEFAULT '{}',
            ADD COLUMN metadata JSONB NOT NULL DEFAULT '{}',
            ADD COLUMN status VARCHAR(20) NOT NULL DEFAULT 'active'
                CHECK (status IN ('draft', 'active', 'archived')),
            ADD COLUMN chunk_count INTEGER NOT NULL DEFAULT 0,
            ADD COLUMN word_count INTEGER NOT NULL DEFAULT 0,
            ADD COLUMN access_count INTEGER NOT NULL DEFAULT 0,
            ADD COLUMN last_accessed_at TIMESTAMPTZ,
            ADD COLUMN freshness_status VARCHAR(20) NOT NULL DEFAULT 'fresh'
                CHECK (freshness_status IN ('fresh', 'stale', 'archived')),
            ADD COLUMN indexed_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
    """)

    op.execute("CREATE INDEX idx_indexed_plans_tags ON indexed_plans USING GIN(tags)")
    op.execute(
        "CREATE INDEX idx_indexed_plans_search_vector ON indexed_plans USING GIN(search_vector)"
    )
    op.execute(
        "CREATE INDEX idx_indexed_plans_pk_status_fresh "
        "ON indexed_plans(project_key, status, freshness_status)"
    )

    # ── 2. Create indexed_plan_chunks ─────────────────────────────────────────
    op.execute("""
        CREATE TABLE indexed_plan_chunks (
            id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            plan_id UUID NOT NULL REFERENCES indexed_plans(id) ON DELETE CASCADE,
            section_title VARCHAR(500) NOT NULL,
            section_path VARCHAR(1000) NOT NULL,
            content TEXT NOT NULL,
            section_order INTEGER NOT NULL,
            word_count INTEGER NOT NULL DEFAULT 0,
            embedding VECTOR(1536) NOT NULL,
            search_vector TSVECTOR,
            tags VARCHAR[] NOT NULL DEFAULT '{}',
            project_key VARCHAR(50) NOT NULL,
            plan_type VARCHAR(20) NOT NULL
                CHECK (plan_type IN ('spec', 'plan')),
            status VARCHAR(20) NOT NULL DEFAULT 'active'
                CHECK (status IN ('draft', 'active', 'archived')),
            access_count INTEGER NOT NULL DEFAULT 0,
            last_accessed_at TIMESTAMPTZ,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )
    """)

    op.execute("CREATE INDEX idx_plan_chunks_plan_id ON indexed_plan_chunks(plan_id)")
    op.execute("""
        CREATE INDEX idx_plan_chunks_embedding ON indexed_plan_chunks
        USING hnsw (embedding vector_cosine_ops) WITH (m = 16, ef_construction = 64)
    """)
    op.execute("CREATE INDEX idx_plan_chunks_tags ON indexed_plan_chunks USING GIN(tags)")
    op.execute(
        "CREATE INDEX idx_plan_chunks_search_vector ON indexed_plan_chunks USING GIN(search_vector)"
    )
    op.execute(
        "CREATE INDEX idx_plan_chunks_pk_type ON indexed_plan_chunks(project_key, plan_type)"
    )

    # ── 3. Backfill existing indexed_plans rows ───────────────────────────────
    # File reads are deferred to the first brain_reindex_plans run.
    # Existing plans are marked stale so the next reindex picks them up.
    op.execute("UPDATE indexed_plans SET freshness_status = 'stale', indexed_at = updated_at")


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS indexed_plan_chunks CASCADE")

    op.execute("DROP INDEX IF EXISTS idx_indexed_plans_tags")
    op.execute("DROP INDEX IF EXISTS idx_indexed_plans_search_vector")
    op.execute("DROP INDEX IF EXISTS idx_indexed_plans_pk_status_fresh")

    op.execute("""
        ALTER TABLE indexed_plans
            DROP COLUMN IF EXISTS indexed_at,
            DROP COLUMN IF EXISTS freshness_status,
            DROP COLUMN IF EXISTS last_accessed_at,
            DROP COLUMN IF EXISTS access_count,
            DROP COLUMN IF EXISTS word_count,
            DROP COLUMN IF EXISTS chunk_count,
            DROP COLUMN IF EXISTS status,
            DROP COLUMN IF EXISTS metadata,
            DROP COLUMN IF EXISTS tags,
            DROP COLUMN IF EXISTS search_vector,
            DROP COLUMN IF EXISTS summary,
            DROP COLUMN IF EXISTS content
    """)
