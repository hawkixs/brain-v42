"""Tell the dream's metabolism apart from human activity.

Revision ID: 041
Revises: 040

`updated_at` conflates two questions: who touched the row, and whether the
content changed. A counter write — `decay_flusher` incrementing `access_count`
after a plain read — rejuvenates it exactly like a human rewrite. PROMOTE's
anti-rejudgement cache compares a verdict against that date, so it dies on
every read, and the same learning was re-evaluated 23 nights in a row for the
very same verdict.

Three columns, no backfill. A NULL `content_updated_at` and an
`access_count_human` of 0 read as "never measured" and repair themselves on
the first real signal.

`content_updated_at` is written by a TRIGGER, unlike `focus_updated_at`
(revision 040) which is written by application code. The divergence is
deliberate: the WHEN ... IS DISTINCT FROM clause gives the VALUE semantics
040 was reaching for — rewriting the same text rejuvenates nothing — and
entity content has many writers (brain_learn, brain_update, REORG, CLEAN
merges, backfill scripts) where the focus has exactly one. An invariant held
by convention across N writers is forgotten by writer N+1; it has already
happened here (see repositories/pg_learning.py). Finally `content_updated_at`
DRIVES a guard, where `focus_updated_at` informs a human: the level of
guarantee required is not the same.

The `public.update_updated_at()` function is NOT modified: revision 039 pins
it by SHA256 and by length, and making it conditional would leave 039
impossible to downgrade.
"""

import sqlalchemy as sa
from alembic import op

revision = "041"
down_revision = "040"
branch_labels = None
depends_on = None

# Tables tracked by the decay: all of them get the human counter, because
# decay_flusher._ENTITY_TABLES updates them uniformly.
_COUNTER_TABLES = (
    "learnings",
    "decisions",
    "snippets",
    "runbooks",
    "adrs",
    "indexed_plans",
)

# Knowledge tables: content columns, per table. `indexed_plans` is absent —
# neither a promotion candidate, nor part of the preflight signal.
_CONTENT_COLUMNS = {
    "learnings": ("topic", "insight"),
    "decisions": ("title", "description", "reasoning", "consequences"),
    "snippets": ("title", "code"),
    "runbooks": ("title", "description", "trigger", "steps"),
    "adrs": ("title", "context", "decision", "consequences"),
}

_CREATE_FUNCTION = """
CREATE FUNCTION public.stamp_content_updated_at()
RETURNS trigger
LANGUAGE plpgsql
AS $function$
BEGIN
    NEW.content_updated_at := CURRENT_TIMESTAMP;
    RETURN NEW;
END;
$function$;
"""


def upgrade() -> None:
    op.add_column(
        "access_log",
        sa.Column(
            "actor",
            sa.String(64),
            nullable=False,
            server_default=sa.text("'unknown'"),
        ),
    )

    for table in _COUNTER_TABLES:
        op.add_column(
            table,
            sa.Column(
                "access_count_human",
                sa.Integer(),
                nullable=False,
                server_default=sa.text("0"),
            ),
        )

    for table in _CONTENT_COLUMNS:
        op.add_column(
            table,
            sa.Column("content_updated_at", sa.DateTime(timezone=True), nullable=True),
        )

    op.execute(_CREATE_FUNCTION)

    for table, columns in _CONTENT_COLUMNS.items():
        column_list = ", ".join(columns)
        predicate = " OR ".join(f"OLD.{c} IS DISTINCT FROM NEW.{c}" for c in columns)
        op.execute(
            f"""
            CREATE TRIGGER trg_{table}_content_updated
            BEFORE UPDATE OF {column_list} ON public.{table}
            FOR EACH ROW
            WHEN ({predicate})
            EXECUTE FUNCTION public.stamp_content_updated_at();
            """
        )


def downgrade() -> None:
    for table in _CONTENT_COLUMNS:
        op.execute(f"DROP TRIGGER IF EXISTS trg_{table}_content_updated ON public.{table};")
    op.execute("DROP FUNCTION IF EXISTS public.stamp_content_updated_at();")
    for table in _CONTENT_COLUMNS:
        op.drop_column(table, "content_updated_at")
    for table in _COUNTER_TABLES:
        op.drop_column(table, "access_count_human")
    op.drop_column("access_log", "actor")
