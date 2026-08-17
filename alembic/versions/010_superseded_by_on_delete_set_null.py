"""Fix: decisions.superseded_by FK ON DELETE SET NULL.

Prevents FK violation when deleting a superseding decision.
Without this, DELETE on decisions referenced by superseded_by fails
with ForeignKeyViolationError.

Revision ID: 010
Revises: 009
Create Date: 2026-03-16
"""

from __future__ import annotations

from alembic import op

revision = "010"
down_revision = "009"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE decisions DROP CONSTRAINT decisions_superseded_by_fkey")
    op.execute(
        "ALTER TABLE decisions ADD CONSTRAINT decisions_superseded_by_fkey"
        " FOREIGN KEY (superseded_by) REFERENCES decisions(id)"
        " ON DELETE SET NULL"
    )


def downgrade() -> None:
    op.execute("ALTER TABLE decisions DROP CONSTRAINT decisions_superseded_by_fkey")
    op.execute(
        "ALTER TABLE decisions ADD CONSTRAINT decisions_superseded_by_fkey"
        " FOREIGN KEY (superseded_by) REFERENCES decisions(id)"
    )
