"""Dream runs metrics table.

Stores OTEL telemetry from dream.sh phase executions.
One row per phase per run. Queried by the metrics sidecar.

Revision ID: 013
Revises: 012
Create Date: 2026-04-05
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "013"
down_revision = "012"


def upgrade() -> None:
    op.create_table(
        "dream_runs",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("run_date", sa.Date, nullable=False),
        sa.Column("phase", sa.String(10), nullable=False),
        sa.Column("model", sa.String(30)),
        sa.Column("status", sa.String(10), nullable=False),
        sa.Column("duration_s", sa.Float),
        sa.Column("input_tokens", sa.Integer, server_default="0"),
        sa.Column("output_tokens", sa.Integer, server_default="0"),
        sa.Column("cache_read_tokens", sa.Integer, server_default="0"),
        sa.Column("cache_creation_tokens", sa.Integer, server_default="0"),
        sa.Column("cost_usd", sa.Float, server_default="0"),
        sa.Column("api_calls", sa.Integer, server_default="0"),
        sa.Column("tool_calls", sa.Integer, server_default="0"),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("NOW()"),
        ),
    )
    op.create_index(
        "idx_dream_runs_date", "dream_runs", ["run_date"], postgresql_ops={"run_date": "DESC"}
    )


def downgrade() -> None:
    op.drop_index("idx_dream_runs_date")
    op.drop_table("dream_runs")
