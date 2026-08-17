"""Add access_log table for memory decay.

Revision ID: 006
Revises: 005
Create Date: 2026-03-13
"""

from __future__ import annotations

from alembic import op

revision = "006"
down_revision = "005"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        CREATE TABLE access_log (
            id          BIGSERIAL PRIMARY KEY,
            entity_type VARCHAR(20) NOT NULL,
            entity_id   UUID NOT NULL,
            access_type VARCHAR(20) NOT NULL,
            accessed_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )
    """)
    op.execute("CREATE INDEX idx_access_log_entity ON access_log(entity_type, entity_id)")
    op.execute("CREATE INDEX idx_access_log_time ON access_log(accessed_at)")


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS access_log;")
