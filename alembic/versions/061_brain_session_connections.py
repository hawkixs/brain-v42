"""061 — retain the connections seen by an operator session's lifecycle calls.

Idle eviction changes the transport while the operator session stays open. Its
seen set keeps exact absorption possible even when a coordinating session makes
the temporal window ambiguous.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID

revision = "061"
down_revision = "060"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "brain_session_connections",
        sa.Column(
            "session_id",
            UUID(as_uuid=True),
            sa.ForeignKey("brain_sessions.id", ondelete="CASCADE"),
            primary_key=True,
            nullable=False,
        ),
        sa.Column("connection_id", sa.String(64), primary_key=True, nullable=False),
        sa.Column(
            "first_seen_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("clock_timestamp()"),
        ),
        sa.CheckConstraint(
            "COALESCE(btrim(connection_id) <> '', false)",
            name="brain_session_connections_connection_nonblank",
        ),
    )


def downgrade() -> None:
    op.drop_table("brain_session_connections")
