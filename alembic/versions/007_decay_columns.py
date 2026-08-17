"""Add decay columns to entity tables.

Revision ID: 007
Revises: 006
Create Date: 2026-03-13
"""

from __future__ import annotations

from alembic import op

revision = "007"
down_revision = "006"
branch_labels = None
depends_on = None

_TABLES = ("decisions", "learnings", "snippets", "runbooks", "adrs")


def upgrade() -> None:
    for table in _TABLES:
        op.execute(f"""
            ALTER TABLE {table}
            ADD COLUMN last_accessed_at TIMESTAMPTZ,
            ADD COLUMN access_count INTEGER DEFAULT 0,
            ADD COLUMN freshness_status VARCHAR(10) DEFAULT 'fresh',
            ADD COLUMN merged_into UUID;
        """)


def downgrade() -> None:
    for table in _TABLES:
        op.execute(f"""
            ALTER TABLE {table}
            DROP COLUMN IF EXISTS last_accessed_at,
            DROP COLUMN IF EXISTS access_count,
            DROP COLUMN IF EXISTS freshness_status,
            DROP COLUMN IF EXISTS merged_into;
        """)
