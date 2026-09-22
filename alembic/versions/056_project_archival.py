"""056 — a project can leave the default views without losing a line.

The operator wants projects off the DISK while their knowledge stays in the
brain. That verb did not exist: `project_contexts` carried 29 columns and not
one of them was a lifecycle.

Nothing existing was reusable. `current_phase` is free prose — one project
already says "télémétrie historique — pas de développement actif" in it, which
no query can read. `project_group` is a grouping (red 38, watchk 4, lyriks 4,
unistra 2, datalake 2), so writing a lifecycle into it would destroy the real
group. `metadata` is empty on every row, and a JSON key is not a queryable
column either.

Nullable, no default, no backfill: `archived_at IS NULL` IS the definition of
active, so every one of the 63 existing projects stays active and this
migration rewrites no row.

`archived_reason` is not decoration. An archive nobody can explain is a state
that outlives its reason, and the session lifecycle already sets the precedent
with `brain_sessions.abandonment_reason`. The CHECK keeps the pair honest: a
reason without a date describes nothing.

ARCHIVING IS NOT DELETING, and this migration deletes nothing either. The
downgrade drops the two columns, which un-archives every project at once —
silently, in every default view. It therefore refuses while any project is
archived unless the operator names the decision.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import context, op

revision = "056"
down_revision = "055"
branch_labels = None
depends_on = None

#: A named acknowledgement: dropping these columns makes every archived project
#: reappear in every default view, with no record that it had ever left.
_OPT_IN = "allow_archival_downgrade"


def upgrade() -> None:
    op.add_column(
        "project_contexts",
        sa.Column("archived_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column("project_contexts", sa.Column("archived_reason", sa.Text(), nullable=True))
    op.create_check_constraint(
        "ck_project_contexts_archived_reason_needs_a_date",
        "project_contexts",
        "archived_reason IS NULL OR archived_at IS NOT NULL",
    )
    # Partial, because the default views ask "which keys are archived?" and the
    # answer is a handful of rows out of 63. A full index on a mostly-NULL
    # column would be paid for on every write and read on none of them.
    op.create_index(
        "idx_project_contexts_archived",
        "project_contexts",
        ["project_key"],
        postgresql_where=sa.text("archived_at IS NOT NULL"),
    )


def downgrade() -> None:
    bind = op.get_bind()
    archived = bind.execute(
        sa.text("SELECT count(*) FROM project_contexts WHERE archived_at IS NOT NULL")
    ).scalar_one()
    opted_in = (context.get_x_argument(as_dictionary=True) or {}).get(_OPT_IN) == "yes"
    if archived and not opted_in:
        raise RuntimeError(
            f"{archived} project(s) are archived; dropping these columns makes them "
            f"reappear in every default view with no record that they ever left. "
            f"Re-run with -x {_OPT_IN}=yes to accept that."
        )
    op.drop_index("idx_project_contexts_archived", table_name="project_contexts")
    op.drop_constraint(
        "ck_project_contexts_archived_reason_needs_a_date",
        "project_contexts",
        type_="check",
    )
    op.drop_column("project_contexts", "archived_reason")
    op.drop_column("project_contexts", "archived_at")
