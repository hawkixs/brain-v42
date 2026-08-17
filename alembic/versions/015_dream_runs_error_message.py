# alembic/versions/015_dream_runs_error_message.py
"""Add error_message column to dream_runs.

Captures the last lines of phase stderr/stdout on fail/timeout so post-mortems
don't require journalctl retention (journald trims after 7 days).

Revision ID: 015
Revises: 014
Create Date: 2026-04-15
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "015"
down_revision = "014"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "dream_runs",
        sa.Column("error_message", sa.Text, nullable=True),
    )


def downgrade() -> None:
    op.drop_column("dream_runs", "error_message")
