"""Migration: vector(384) -> vector(1536) for GPU embedding service.

Drops existing 384-dim embedding columns and recreates them as 1536-dim.
All existing embeddings become NULL -- FTS continues to work.
Recreates HNSW indexes with vector_cosine_ops.

Revision ID: 002
Revises: 001
Create Date: 2026-03-02
"""

from __future__ import annotations

from alembic import op

# Revision identifiers
revision = "002"
down_revision = "001"
branch_labels = None
depends_on = None

TABLES = ["decisions", "learnings", "snippets", "runbooks", "adrs"]


def upgrade() -> None:
    """Widen embedding columns from vector(384) to vector(1536)."""
    for table in TABLES:
        op.execute(f"DROP INDEX IF EXISTS idx_{table}_embedding")
        op.execute(f"ALTER TABLE {table} DROP COLUMN IF EXISTS embedding")
        op.execute(f"ALTER TABLE {table} ADD COLUMN embedding vector(1536)")
        op.execute(f"""
            CREATE INDEX idx_{table}_embedding ON {table}
            USING hnsw (embedding vector_cosine_ops)
            WITH (m = 16, ef_construction = 64)
        """)


def downgrade() -> None:
    """Restore embedding columns to vector(384)."""
    for table in TABLES:
        op.execute(f"DROP INDEX IF EXISTS idx_{table}_embedding")
        op.execute(f"ALTER TABLE {table} DROP COLUMN IF EXISTS embedding")
        op.execute(f"ALTER TABLE {table} ADD COLUMN embedding vector(384)")
        op.execute(f"""
            CREATE INDEX idx_{table}_embedding ON {table}
            USING hnsw (embedding vector_cosine_ops)
            WITH (m = 16, ef_construction = 64)
        """)
