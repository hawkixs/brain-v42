"""Fix: merged_into self-FK ON DELETE SET NULL on the 5 entity tables.

Root cause (2026-06-22 audit): merged_into was a bare UUID column with NO
foreign key, so deleting a merge TARGET left every inbound merged_into pointer
dangling — the DB never nulled them. 54 learnings were observed pointing at a
merged_into target that no longer exists. Merge itself is clean (it archives the
source with merged_into set); the orphaning happens on a later DELETE of a
target via any path (brain_delete, manual surgery, future reorg).

This migration (a) nulls all pre-existing dangling pointers across the 5 tables
so the constraint can be added and the audit's 54 are repaired, then (b) adds a
self-referential FK ON DELETE SET NULL so any future target-delete self-heals
the inbound pointers. There is no CHECK on merged_into, so SET NULL is safe
(no CHECK-vs-SET-NULL interaction). Same belt-and-suspenders pattern as 019.

Revision ID: 023
Revises: 022
Create Date: 2026-06-22
"""

from __future__ import annotations

from alembic import op

revision = "023"
down_revision = "022"
branch_labels = None
depends_on = None

# Entity tables carrying a self-referential merged_into pointer.
_TABLES = ("decisions", "learnings", "snippets", "runbooks", "adrs")


def upgrade() -> None:
    for table in _TABLES:
        # 1. Repair pre-existing dangling pointers (target row deleted): required
        #    before ADD CONSTRAINT, and fixes the 2026-06-22 audit residual.
        op.execute(
            f"UPDATE {table} SET merged_into = NULL "
            f"WHERE merged_into IS NOT NULL "
            f"AND NOT EXISTS (SELECT 1 FROM {table} t2 WHERE t2.id = {table}.merged_into)"
        )
        # 2. Self-referential FK: deleting a merge target now nulls inbound
        #    merged_into pointers instead of orphaning them.
        op.execute(
            f"ALTER TABLE {table} ADD CONSTRAINT {table}_merged_into_fkey "
            f"FOREIGN KEY (merged_into) REFERENCES {table}(id) ON DELETE SET NULL"
        )


def downgrade() -> None:
    for table in _TABLES:
        op.execute(f"ALTER TABLE {table} DROP CONSTRAINT {table}_merged_into_fkey")
