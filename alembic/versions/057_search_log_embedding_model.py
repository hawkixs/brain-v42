"""Attribute a logged search to the embedding model that served it.

Revision ID: 057
Revises: 056

Ticket 4fac067a: judging the codestral trial needs to split `search_log` rows by
which embedding model produced them, and the table has no such column
(synthesis `sidecar-synthesis.md` §1.4, §2 T1.8; operator decision `1669d429`).
This is the prerequisite alone — the `tallies.search_by_model` aggregation
itself is Tier 2 and stays out of this migration.

TEXT, NULLABLE, NO DEFAULT, NO BACKFILL. The column stores the configured
embedding_model NAME ONLY (`config.py`'s `embedding_model`) — never its
`embedding_backend`, and never a caller-supplied value. `NULL` means "no
embedding model attributed to this row": a row "written before 057", the same
doctrine as 040/041/042/046/048/049, or a row logged for a search no embedding
model served — one that ran `search_mode == "fts_fallback"` (`brain_service.py`,
embedding service down, served by FTS alone), or one whose `project_group`
resolved to no project and was answered empty before any embedding call. A
`server_default` would retroactively label every pre-057 row with whatever
model happens to be configured the day this migration runs, inventing an
attribution none of those searches ever had.
`TEXT` rather than a bounded `VARCHAR`: 045 already had to widen `dream_runs.model`
from 30 to 120 characters because a real configured model name did not fit, and
an embedding model name is the same kind of operator-chosen, unbounded string.

DOWNGRADE IS A PLAIN `drop_column`, unlike 049 or 056's named refusals. Those
guarded columns that could not be recomputed after the fact on tables meant to
last for good. `search_log` is different in kind: it already carries a 30-day
retention policy enforced by `MetricsFlusher` (`flusher.py`), so anything this
column could ever attribute has a hard expiry built in regardless of any
migration. A guard here would protect data that disappears on its own within a
month.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "057"
down_revision = "056"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "search_log",
        sa.Column("embedding_model", sa.Text(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("search_log", "embedding_model")
