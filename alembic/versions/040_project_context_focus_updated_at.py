"""Date the focus itself, which the row-level timestamp cannot do.

Revision ID: 040
Revises: 039

The row-level timestamp on `project_contexts` moves on any write to the row —
counters included — so it dates the row, not the prose. Reading "focus written
N days ago" off it would mint a number whose label does not match its
truth-maker, which is the very defect this column exists to remove.

No backfill, deliberately. Nobody knows when the existing focus paragraphs were
authored, and copying a row timestamp in would manufacture that missing fact
instead of admitting it. NULL reads as "never measured" and self-heals on the
first real focus write.

The column is written by application code only, never by a trigger. Revision
039 pins its trigger function by SHA256 of the source and by byte length, so
editing that body here would make 039 undowngradable — and a trigger could not
implement the rule anyway, since it must fire only when the focus text actually
changes.
"""

import sqlalchemy as sa
from alembic import op

revision = "040"
down_revision = "039"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "project_contexts",
        sa.Column("focus_updated_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("project_contexts", "focus_updated_at")
