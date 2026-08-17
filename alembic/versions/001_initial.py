"""Initial schema: 6 tables + pgvector + full-text search + triggers.

Revision ID: 001
Revises: None
Create Date: 2026-02-28

Creates all 6 tables for brain_v42:
  - decisions       (technical decisions with reasoning)
  - learnings       (insights and learnings)
  - snippets        (reusable code snippets)
  - runbooks        (operational runbooks)
  - adrs            (Architecture Decision Records)
  - project_contexts (per-project context and metadata)

All tables use:
  - UUID primary keys (gen_random_uuid())
  - updated_at trigger for automatic timestamp updates
  - Full-text search via tsvector GENERATED ALWAYS AS STORED columns (5 tables)
  - Semantic search via pgvector HNSW indexes on embedding columns (5 tables)
  - GIN indexes for full-text and array searches

NOTE: This migration uses op.execute() with raw SQL strings throughout.
      Alembic's DSL (op.create_table etc.) does NOT support:
        - vector(384) type (requires pgvector extension)
        - GENERATED ALWAYS AS ... STORED computed columns
        - USING hnsw (...) index type
      This is an intentional architectural choice documented in SCHEMA.md.
"""

from __future__ import annotations

from alembic import op

# Revision identifiers
revision = "001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Create the full brain_v42 schema from scratch."""

    # ── 1. Enable pgvector extension ─────────────────────────────────────────
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")

    # ── 2a. Create immutable_array_to_string() helper (GENERATED ALWAYS AS requires IMMUTABLE) ──
    # array_to_string() is STABLE in PostgreSQL — not IMMUTABLE — so it cannot be used
    # directly in a GENERATED ALWAYS AS expression. We wrap it in an IMMUTABLE SQL function
    # which is valid because for a fixed text array and separator the result never changes.
    op.execute("""
        CREATE OR REPLACE FUNCTION immutable_array_to_string(TEXT[], TEXT)
        RETURNS TEXT AS $$
            SELECT array_to_string($1, $2)
        $$ LANGUAGE SQL IMMUTABLE STRICT PARALLEL SAFE
    """)

    # ── 2b. Create update_updated_at() trigger function ───────────────────────
    op.execute("""
        CREATE OR REPLACE FUNCTION update_updated_at()
        RETURNS TRIGGER AS $$
        BEGIN
            NEW.updated_at = NOW();
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql
    """)

    # ── 3. Create decisions table ─────────────────────────────────────────────
    op.execute("""
        CREATE TABLE decisions (
            id             UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            title          VARCHAR(200) NOT NULL,
            description    TEXT NOT NULL,
            reasoning      TEXT NOT NULL,
            alternatives   TEXT[] NOT NULL DEFAULT '{}',
            consequences   TEXT,
            project_key    VARCHAR(50),
            tags           TEXT[] NOT NULL DEFAULT '{}',
            status         VARCHAR(20) NOT NULL DEFAULT 'active'
                               CHECK (status IN ('active', 'superseded', 'deprecated')),
            superseded_by  UUID REFERENCES decisions(id),
            embedding      vector(384),
            metadata       JSONB NOT NULL DEFAULT '{}',
            search_vector  TSVECTOR,
            created_at     TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            updated_at     TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )
    """)

    # Add GENERATED ALWAYS AS search_vector (must be done via ALTER TABLE)
    # because PostgreSQL does not allow GENERATED columns that reference other
    # columns with function calls in the same CREATE TABLE statement.
    op.execute("""
        ALTER TABLE decisions
        DROP COLUMN search_vector,
        ADD COLUMN search_vector TSVECTOR GENERATED ALWAYS AS (
            setweight(to_tsvector('english', coalesce(title, '')), 'A') ||
            setweight(to_tsvector('english', coalesce(description, '')), 'B') ||
            setweight(to_tsvector('english', coalesce(reasoning, '')), 'C') ||
            setweight(to_tsvector('english', immutable_array_to_string(tags, ' ')), 'D')
        ) STORED
    """)

    # ── 4. Create learnings table ─────────────────────────────────────────────
    op.execute("""
        CREATE TABLE learnings (
            id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            topic         VARCHAR(200) NOT NULL,
            insight       TEXT NOT NULL,
            source        TEXT,
            source_type   VARCHAR(20) NOT NULL DEFAULT 'experience'
                              CHECK (source_type IN ('experience', 'documentation', 'code_review', 'bug', 'external')),
            confidence    VARCHAR(10) NOT NULL DEFAULT 'medium'
                              CHECK (confidence IN ('low', 'medium', 'high')),
            project_key   VARCHAR(50),
            tags          TEXT[] NOT NULL DEFAULT '{}',
            validated_at  TIMESTAMPTZ,
            embedding     vector(384),
            metadata      JSONB NOT NULL DEFAULT '{}',
            search_vector TSVECTOR,
            created_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            updated_at    TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )
    """)

    op.execute("""
        ALTER TABLE learnings
        DROP COLUMN search_vector,
        ADD COLUMN search_vector TSVECTOR GENERATED ALWAYS AS (
            setweight(to_tsvector('english', coalesce(topic, '')), 'A') ||
            setweight(to_tsvector('english', coalesce(insight, '')), 'B') ||
            setweight(to_tsvector('english', coalesce(source, '')), 'C') ||
            setweight(to_tsvector('english', immutable_array_to_string(tags, ' ')), 'D')
        ) STORED
    """)

    # ── 5. Create snippets table ──────────────────────────────────────────────
    op.execute("""
        CREATE TABLE snippets (
            id             UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            title          VARCHAR(200) NOT NULL,
            intention      TEXT NOT NULL,
            code           TEXT NOT NULL,
            language       VARCHAR(50) NOT NULL,
            dependencies   TEXT[] NOT NULL DEFAULT '{}',
            usage_example  TEXT,
            gotchas        TEXT,
            project_key    VARCHAR(50),
            tags           TEXT[] NOT NULL DEFAULT '{}',
            use_count      INTEGER NOT NULL DEFAULT 0,
            last_used_at   TIMESTAMPTZ,
            embedding      vector(384),
            metadata       JSONB NOT NULL DEFAULT '{}',
            search_vector  TSVECTOR,
            created_at     TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            updated_at     TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )
    """)

    op.execute("""
        ALTER TABLE snippets
        DROP COLUMN search_vector,
        ADD COLUMN search_vector TSVECTOR GENERATED ALWAYS AS (
            setweight(to_tsvector('english', coalesce(title, '')), 'A') ||
            setweight(to_tsvector('english', coalesce(intention, '')), 'B') ||
            setweight(to_tsvector('english', coalesce(language, '')), 'C') ||
            setweight(to_tsvector('english', immutable_array_to_string(tags, ' ')), 'D')
        ) STORED
    """)

    # ── 6. Create runbooks table ──────────────────────────────────────────────
    op.execute("""
        CREATE TABLE runbooks (
            id                    UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            title                 VARCHAR(200) NOT NULL,
            description           TEXT NOT NULL,
            project_key           VARCHAR(50) NOT NULL,
            trigger               TEXT NOT NULL,
            prerequisites         TEXT[] NOT NULL DEFAULT '{}',
            steps                 JSONB NOT NULL DEFAULT '[]',
            rollback_steps        JSONB NOT NULL DEFAULT '[]',
            estimated_duration    VARCHAR(50),
            tags                  TEXT[] NOT NULL DEFAULT '{}',
            execution_count       INTEGER NOT NULL DEFAULT 0,
            last_executed_at      TIMESTAMPTZ,
            last_execution_status VARCHAR(20),
            embedding             vector(384),
            metadata              JSONB NOT NULL DEFAULT '{}',
            search_vector         TSVECTOR,
            created_at            TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            updated_at            TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            CONSTRAINT uq_runbooks_title_project UNIQUE (title, project_key)
        )
    """)

    op.execute("""
        ALTER TABLE runbooks
        DROP COLUMN search_vector,
        ADD COLUMN search_vector TSVECTOR GENERATED ALWAYS AS (
            setweight(to_tsvector('english', coalesce(title, '')), 'A') ||
            setweight(to_tsvector('english', coalesce(description, '')), 'B') ||
            setweight(to_tsvector('english', coalesce(trigger, '')), 'C') ||
            setweight(to_tsvector('english', immutable_array_to_string(tags, ' ')), 'D')
        ) STORED
    """)

    # ── 7. Create adrs table ──────────────────────────────────────────────────
    op.execute("""
        CREATE TABLE adrs (
            id                      UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            number                  INTEGER NOT NULL,
            title                   VARCHAR(200) NOT NULL,
            context                 TEXT NOT NULL,
            decision                TEXT NOT NULL,
            consequences            TEXT NOT NULL,
            alternatives_considered JSONB NOT NULL DEFAULT '[]',
            project_key             VARCHAR(50) NOT NULL,
            tags                    TEXT[] NOT NULL DEFAULT '{}',
            status                  VARCHAR(20) NOT NULL DEFAULT 'proposed'
                                        CHECK (status IN ('proposed', 'accepted', 'deprecated', 'superseded')),
            decided_at              TIMESTAMPTZ,
            superseded_by           INTEGER,
            embedding               vector(384),
            metadata                JSONB NOT NULL DEFAULT '{}',
            search_vector           TSVECTOR,
            created_at              TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            updated_at              TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            CONSTRAINT uq_adrs_number_project UNIQUE (number, project_key)
        )
    """)

    op.execute("""
        ALTER TABLE adrs
        DROP COLUMN search_vector,
        ADD COLUMN search_vector TSVECTOR GENERATED ALWAYS AS (
            setweight(to_tsvector('english', coalesce(title, '')), 'A') ||
            setweight(to_tsvector('english', coalesce(decision, '')), 'B') ||
            setweight(to_tsvector('english', coalesce(context, '')), 'C') ||
            setweight(to_tsvector('english', immutable_array_to_string(tags, ' ')), 'D')
        ) STORED
    """)

    # ── 8. Create project_contexts table ─────────────────────────────────────
    # Note: No embedding or search_vector column on this table (per SCHEMA.md)
    op.execute("""
        CREATE TABLE project_contexts (
            id               UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            project_key      VARCHAR(50) NOT NULL UNIQUE,
            name             VARCHAR(200) NOT NULL,
            description      TEXT NOT NULL,
            languages        TEXT[] NOT NULL DEFAULT '{}',
            frameworks       TEXT[] NOT NULL DEFAULT '{}',
            databases        TEXT[] NOT NULL DEFAULT '{}',
            code_style       TEXT,
            git_workflow     TEXT,
            test_strategy    TEXT,
            current_phase    TEXT,
            current_focus    TEXT,
            blockers         TEXT[] NOT NULL DEFAULT '{}',
            related_projects TEXT[] NOT NULL DEFAULT '{}',
            local_path       TEXT,
            repo_url         TEXT,
            decisions_count  INTEGER NOT NULL DEFAULT 0,
            learnings_count  INTEGER NOT NULL DEFAULT 0,
            snippets_count   INTEGER NOT NULL DEFAULT 0,
            runbooks_count   INTEGER NOT NULL DEFAULT 0,
            adrs_count       INTEGER NOT NULL DEFAULT 0,
            metadata         JSONB NOT NULL DEFAULT '{}',
            created_at       TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            updated_at       TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )
    """)

    # ── 9. Create all indexes ─────────────────────────────────────────────────

    # decisions indexes
    op.execute("CREATE INDEX idx_decisions_search ON decisions USING GIN (search_vector)")
    op.execute("CREATE INDEX idx_decisions_project ON decisions (project_key)")
    op.execute("CREATE INDEX idx_decisions_status ON decisions (status)")
    op.execute("CREATE INDEX idx_decisions_tags ON decisions USING GIN (tags)")
    op.execute("CREATE INDEX idx_decisions_created ON decisions (created_at DESC)")
    op.execute("""
        CREATE INDEX idx_decisions_embedding ON decisions
        USING hnsw (embedding vector_cosine_ops)
        WITH (m = 16, ef_construction = 64)
    """)

    # learnings indexes
    op.execute("CREATE INDEX idx_learnings_search ON learnings USING GIN (search_vector)")
    op.execute("CREATE INDEX idx_learnings_project ON learnings (project_key)")
    op.execute("CREATE INDEX idx_learnings_confidence ON learnings (confidence)")
    op.execute("CREATE INDEX idx_learnings_tags ON learnings USING GIN (tags)")
    op.execute("CREATE INDEX idx_learnings_created ON learnings (created_at DESC)")
    op.execute("""
        CREATE INDEX idx_learnings_embedding ON learnings
        USING hnsw (embedding vector_cosine_ops)
        WITH (m = 16, ef_construction = 64)
    """)

    # snippets indexes
    op.execute("CREATE INDEX idx_snippets_search ON snippets USING GIN (search_vector)")
    op.execute("CREATE INDEX idx_snippets_language ON snippets (language)")
    op.execute("CREATE INDEX idx_snippets_project ON snippets (project_key)")
    op.execute("CREATE INDEX idx_snippets_tags ON snippets USING GIN (tags)")
    op.execute("CREATE INDEX idx_snippets_use_count ON snippets (use_count DESC)")
    op.execute("CREATE INDEX idx_snippets_created ON snippets (created_at DESC)")
    op.execute("""
        CREATE INDEX idx_snippets_embedding ON snippets
        USING hnsw (embedding vector_cosine_ops)
        WITH (m = 16, ef_construction = 64)
    """)

    # runbooks indexes
    op.execute("CREATE INDEX idx_runbooks_search ON runbooks USING GIN (search_vector)")
    op.execute("CREATE INDEX idx_runbooks_project ON runbooks (project_key)")
    op.execute("CREATE INDEX idx_runbooks_tags ON runbooks USING GIN (tags)")
    op.execute("CREATE INDEX idx_runbooks_created ON runbooks (created_at DESC)")
    op.execute("""
        CREATE INDEX idx_runbooks_embedding ON runbooks
        USING hnsw (embedding vector_cosine_ops)
        WITH (m = 16, ef_construction = 64)
    """)

    # adrs indexes
    op.execute("CREATE INDEX idx_adrs_search ON adrs USING GIN (search_vector)")
    op.execute("CREATE INDEX idx_adrs_project ON adrs (project_key)")
    op.execute("CREATE INDEX idx_adrs_status ON adrs (status)")
    op.execute("CREATE INDEX idx_adrs_tags ON adrs USING GIN (tags)")
    op.execute("CREATE INDEX idx_adrs_number ON adrs (project_key, number DESC)")
    op.execute("CREATE INDEX idx_adrs_created ON adrs (created_at DESC)")
    op.execute("""
        CREATE INDEX idx_adrs_embedding ON adrs
        USING hnsw (embedding vector_cosine_ops)
        WITH (m = 16, ef_construction = 64)
    """)

    # project_contexts indexes
    op.execute("CREATE INDEX idx_project_contexts_key ON project_contexts (project_key)")
    op.execute(
        "CREATE INDEX idx_project_contexts_languages ON project_contexts USING GIN (languages)"
    )
    op.execute(
        "CREATE INDEX idx_project_contexts_frameworks ON project_contexts USING GIN (frameworks)"
    )

    # ── 10. Create updated_at triggers for all 6 tables ──────────────────────
    op.execute("""
        CREATE TRIGGER trg_decisions_updated
        BEFORE UPDATE ON decisions
        FOR EACH ROW EXECUTE FUNCTION update_updated_at()
    """)

    op.execute("""
        CREATE TRIGGER trg_learnings_updated
        BEFORE UPDATE ON learnings
        FOR EACH ROW EXECUTE FUNCTION update_updated_at()
    """)

    op.execute("""
        CREATE TRIGGER trg_snippets_updated
        BEFORE UPDATE ON snippets
        FOR EACH ROW EXECUTE FUNCTION update_updated_at()
    """)

    op.execute("""
        CREATE TRIGGER trg_runbooks_updated
        BEFORE UPDATE ON runbooks
        FOR EACH ROW EXECUTE FUNCTION update_updated_at()
    """)

    op.execute("""
        CREATE TRIGGER trg_adrs_updated
        BEFORE UPDATE ON adrs
        FOR EACH ROW EXECUTE FUNCTION update_updated_at()
    """)

    op.execute("""
        CREATE TRIGGER trg_project_contexts_updated
        BEFORE UPDATE ON project_contexts
        FOR EACH ROW EXECUTE FUNCTION update_updated_at()
    """)


def downgrade() -> None:
    """Drop the full brain_v42 schema in reverse dependency order.

    IMPORTANT: Drop triggers BEFORE tables (PostgreSQL enforces trigger ownership).
    Drop tables in reverse order of creation (respecting FK constraints).
    """

    # ── 1. Drop all triggers first ────────────────────────────────────────────
    op.execute("DROP TRIGGER IF EXISTS trg_project_contexts_updated ON project_contexts")
    op.execute("DROP TRIGGER IF EXISTS trg_adrs_updated ON adrs")
    op.execute("DROP TRIGGER IF EXISTS trg_runbooks_updated ON runbooks")
    op.execute("DROP TRIGGER IF EXISTS trg_snippets_updated ON snippets")
    op.execute("DROP TRIGGER IF EXISTS trg_learnings_updated ON learnings")
    op.execute("DROP TRIGGER IF EXISTS trg_decisions_updated ON decisions")

    # ── 2. Drop all tables (reverse creation order) ────────────────────────────
    # Drop tables without FK deps first, then decisions (has self-FK superseded_by)
    op.execute("DROP TABLE IF EXISTS project_contexts")
    op.execute("DROP TABLE IF EXISTS adrs")
    op.execute("DROP TABLE IF EXISTS runbooks")
    op.execute("DROP TABLE IF EXISTS snippets")
    op.execute("DROP TABLE IF EXISTS learnings")
    op.execute("DROP TABLE IF EXISTS decisions")

    # ── 3. Drop trigger function ──────────────────────────────────────────────
    op.execute("DROP FUNCTION IF EXISTS update_updated_at()")

    # ── 3b. Drop immutable helper function ────────────────────────────────────
    op.execute("DROP FUNCTION IF EXISTS immutable_array_to_string(TEXT[], TEXT)")

    # ── 4. Drop pgvector extension ────────────────────────────────────────────
    # Note: only drop if no other tables use vector type
    # (be conservative — don't drop extension in shared environments)
    # op.execute("DROP EXTENSION IF EXISTS vector")
