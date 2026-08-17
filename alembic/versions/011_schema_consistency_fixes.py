"""Schema consistency fixes: plan_scan_paths JSONB->ARRAY, features nullable+default.

Revision ID: 011
Revises: 010
Create Date: 2026-03-18
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "011"
down_revision = "010"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # 1. Convert plan_scan_paths from JSONB to TEXT[]
    # Cannot use subquery in ALTER COLUMN USING, so use temp column approach
    op.execute("""
        ALTER TABLE project_contexts
        ADD COLUMN plan_scan_paths_new TEXT[] NOT NULL DEFAULT '{}'::TEXT[]
    """)
    op.execute("""
        UPDATE project_contexts
        SET plan_scan_paths_new = COALESCE(
            (SELECT array_agg(elem) FROM jsonb_array_elements_text(plan_scan_paths) AS elem),
            '{}'::TEXT[]
        )
    """)
    op.execute("ALTER TABLE project_contexts DROP COLUMN plan_scan_paths")
    op.execute("ALTER TABLE project_contexts RENAME COLUMN plan_scan_paths_new TO plan_scan_paths")

    # 2. features.embedding: ensure nullable
    op.alter_column("features", "embedding", nullable=True)

    # 3. features.status: fix server_default
    op.alter_column(
        "features",
        "status",
        server_default=sa.text("'planned'"),
    )


def downgrade() -> None:
    op.execute("""
        ALTER TABLE project_contexts
        ADD COLUMN plan_scan_paths_old JSONB DEFAULT '[]'::JSONB
    """)
    op.execute("""
        UPDATE project_contexts
        SET plan_scan_paths_old = COALESCE(to_jsonb(plan_scan_paths), '[]'::JSONB)
    """)
    op.execute("ALTER TABLE project_contexts DROP COLUMN plan_scan_paths")
    op.execute("ALTER TABLE project_contexts RENAME COLUMN plan_scan_paths_old TO plan_scan_paths")
    op.alter_column("features", "status", server_default="planned")
