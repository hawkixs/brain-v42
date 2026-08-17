"""Add metrics_timeseries table for 24h histories (rps/p95/err_rate/cost).

Revision ID: 018
Revises: 017
Create Date: 2026-04-19
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "018"
down_revision = "017"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "metrics_timeseries",
        sa.Column("bucket_ts", sa.DateTime(timezone=True), nullable=False),
        sa.Column("metric", sa.String(50), nullable=False),
        sa.Column("value", sa.Float, nullable=False),
        sa.PrimaryKeyConstraint("bucket_ts", "metric"),
    )
    op.create_index(
        "idx_metrics_ts_metric",
        "metrics_timeseries",
        ["metric", sa.text("bucket_ts DESC")],
    )


def downgrade() -> None:
    op.drop_index("idx_metrics_ts_metric", table_name="metrics_timeseries")
    op.drop_table("metrics_timeseries")
