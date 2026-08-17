"""Re-key process_metrics to composite PK (agent_name, pid).

Adds `agent_name` column (NOT NULL, server_default 'unknown') and promotes
the primary key from the single-column `(pid)` to the composite
`(agent_name, pid)`. This allows the single HTTP server to write one row per
agent while concurrent stdio writers keep using 'unknown' / '_process' as the
sentinel bucket, with no ON CONFLICT collisions during the stdio↔http
coexistence window.

Additive-safe upgrade order:
  1. Add nullable column
  2. Backfill existing rows (0 rows in practice; guard is idempotent for prod)
  3. Set NOT NULL + server_default
  4. Drop old single-col PK
  5. Create composite PK

Downgrade is fully reversible:
  1. Drop composite PK
  2. Restore single-col PK on pid
  3. Drop agent_name column

Revision ID: 025
Revises: 024
Create Date: 2026-06-30
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "025"
down_revision = "024"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # 1. Add as nullable first (safe against live writers that don't know about it yet)
    op.add_column("process_metrics", sa.Column("agent_name", sa.String(), nullable=True))

    # 2. Backfill any existing rows (brain_test has 0; guard is idempotent)
    op.execute("UPDATE process_metrics SET agent_name = 'unknown' WHERE agent_name IS NULL")

    # 3. Enforce NOT NULL + set server_default so future raw INSERTs still satisfy constraint
    op.alter_column(
        "process_metrics",
        "agent_name",
        nullable=False,
        server_default=sa.text("'unknown'"),
    )

    # 4. Drop old single-col PK
    op.drop_constraint("process_metrics_pkey", "process_metrics", type_="primary")

    # 5. Create composite PK
    op.create_primary_key("process_metrics_pkey", "process_metrics", ["agent_name", "pid"])


def downgrade() -> None:
    # 1. Drop composite PK
    op.drop_constraint("process_metrics_pkey", "process_metrics", type_="primary")

    # 2. Restore single-col PK on pid
    op.create_primary_key("process_metrics_pkey", "process_metrics", ["pid"])

    # 3. Remove agent_name column entirely
    op.drop_column("process_metrics", "agent_name")
