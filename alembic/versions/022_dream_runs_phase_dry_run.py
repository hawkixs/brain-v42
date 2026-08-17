"""Add phase_dry_run BOOLEAN to dream_runs.

Supports per-phase DRY/WET classification needed by the session-start
killswitch briefing. Each dream phase now records whether it ran dry
or wet, decoupled from the global DRY_RUN flag (a REORG sub-flag
already exists; this generalises the model).

Default false: legacy rows are backfilled as WET, which matches the
historical reality where REORG was disabled and PROMOTE ran WET.

Revision ID: 022
Revises: 021
Create Date: 2026-05-14
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "022"
down_revision = "021"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "dream_runs",
        sa.Column(
            "phase_dry_run",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
    )


def downgrade() -> None:
    op.drop_column("dream_runs", "phase_dry_run")
