"""Add search_log and process_metrics tables for persistent cross-process metrics.

search_log: one row per search tool call, tracks quality over time (30-day retention).
process_metrics: one row per alive MCP process, periodic upsert of in-memory counters.

Revision ID: 004
Revises: 003
Create Date: 2026-03-12
"""

from __future__ import annotations

from alembic import op

revision = "004"
down_revision = "003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        CREATE TABLE search_log (
            id BIGSERIAL PRIMARY KEY,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            tool_name VARCHAR(50) NOT NULL,
            project_key VARCHAR(50),
            result_count INTEGER NOT NULL,
            top_score DOUBLE PRECISION,
            avg_score DOUBLE PRECISION,
            latency_ms DOUBLE PRECISION NOT NULL
        )
    """)
    op.execute("CREATE INDEX idx_search_log_created ON search_log (created_at DESC)")
    op.execute("CREATE INDEX idx_search_log_tool ON search_log (tool_name)")

    op.execute("""
        CREATE TABLE process_metrics (
            pid INTEGER PRIMARY KEY,
            started_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            tool_stats JSONB NOT NULL DEFAULT '{}',
            embedding_stats JSONB NOT NULL DEFAULT '{}',
            memory_rss_bytes BIGINT NOT NULL DEFAULT 0
        )
    """)


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS process_metrics")
    op.execute("DROP TABLE IF EXISTS search_log")
