"""044 — human recency, the term 041 left contaminated.

Spec `docs/superpowers/specs/2026-08-08-dream-v2-design.md` §5.2.

041 gave `access_count_human` to the six tables tracked by the decay. It
repairs `freq_factor`, weight **0.2**. It leaves `access_factor` untouched,
weight **0.3** — the heaviest after age — which reads `last_accessed_at` with
no human variant.

Measured on `learnings` when the spec was written: **1,779 entities** carry a
non-null `last_accessed_at` with `access_count_human = 0`. Their recency term
is therefore driven by MACHINE reads alone. Substituting the counter alone
repairs 0.2 of the 0.5 of weight driven by reading; the rest keeps alive
whatever the machine re-reads — that is, keeps alive whatever the dream has
just read, which is exactly the loop we mean to cut.

THE AGGREGATE ALREADY KNOWS. `repositories/pg_access_log.py` groups by
`access_log.actor` and tests `is_human_actor` to fill `count_human`; the
`max_accessed` exists too, but it is folded over ALL actors. A
`max_accessed_human` is one line inside a loop that already exists. What was
missing is the column to put it in — here it is, on the same six tables.

Nullable and without backfill, like 041 and 043: `NULL` means "no human read
recorded since the column existed", never "never read by a human". A backfill
from `last_accessed_at` would copy over precisely the contaminated signal we
are separating.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "044"
down_revision = "043"
branch_labels = None
depends_on = None

# The same six tables as 041 and 043 — the set tracked by the decay.
_DECAY_TABLES = (
    "learnings",
    "decisions",
    "snippets",
    "runbooks",
    "adrs",
    "indexed_plans",
)


def upgrade() -> None:
    for table in _DECAY_TABLES:
        op.add_column(
            table,
            sa.Column("last_accessed_at_human", sa.DateTime(timezone=True), nullable=True),
        )


def downgrade() -> None:
    for table in _DECAY_TABLES:
        op.drop_column(table, "last_accessed_at_human")
