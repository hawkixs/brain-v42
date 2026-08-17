"""Add consolidation_log table for memory decay.

Revision ID: 008
Revises: 007
Create Date: 2026-03-13
"""

from __future__ import annotations

from alembic import op

revision = "008"
down_revision = "007"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        CREATE TABLE consolidation_log (
            id            BIGSERIAL PRIMARY KEY,
            source_id     UUID NOT NULL,
            target_id     UUID NOT NULL,
            entity_type   VARCHAR(20) NOT NULL,
            similarity    FLOAT NOT NULL,
            action        VARCHAR(20) NOT NULL,
            created_at    TIMESTAMPTZ DEFAULT NOW()
        );
    """)


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS consolidation_log;")
