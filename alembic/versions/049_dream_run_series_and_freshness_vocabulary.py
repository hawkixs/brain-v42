"""The sweep's time series, the rail that under-declared, and two new words.

Revision ID: 049
Revises: 048

Three objects of one family — nullable ADD COLUMN + CHECK widening — grouped
under criterion (c) of signed decision 9d22bc6a: their downgrades can fail
INDEPENDENTLY, and each carries its own named refusal. M-C (the checkpoint
table) does NOT belong here: its delivery-product approval is still due (ticket
d04dc588). M-D stays ISOLATED and takes the next head.

1. ``dream_runs.closed_inactive_count`` (ticket 24ca3b73, C9 of the corridor):
   the distinct counter existed in the report and in the model, NOT in the
   database — a night closing 200 tracers for inactivity left no time series at
   all. Abandoning and closing-for-inactivity are two events of opposite
   meaning; conflating them was the failure mode the ticket named.

2. ``dream_runs.thinking_tokens`` (ticket 76e11c9f): the agy rail generated
   962 thinking for 1554 output on the run measured on 2026-08-11 — ~38% of the
   tokens were counted NOWHERE, even though the rail order codex→agy→claude was
   settled on a compared cost. The column makes the measurement honest; rails
   that do not separate thinking leave NULL.

3. ``freshness_source`` vocabulary: ``plan_reindex`` (the plan upsert sets
   ``freshness_status='fresh'`` on a re-edited file — a legitimate unarchival
   but INVISIBLE as long as it does not declare itself, ticket 55a21fb8) and
   ``manual_update`` (reserved, unused — the exact template of ``judgment``,
   which lived reserved in 043's CHECK until step 1 consumed it; the ruling on
   stamping human writes stays open and will find the word already
   admitted).

NULLABLE AND WITHOUT A DEFAULT, the doctrine of 040-041-042-046-048: ``NULL``
means "written before 049" (or: a rail/phase that does not measure this
dimension). No backfill — a retroactive zero would lie about uncounted nights.
"""

from __future__ import annotations

from alembic import context, op

revision = "049"
down_revision = "048"
branch_labels = None
depends_on = None

#: NAMED opt-ins, one per destruction — never a generic flag (the template of
#: 039/046/048): three independent refusals, because it is the independence of
#: the downgrades that made this grouping legitimate (9d22bc6a, criterion (c)).
_SERIES_OPT_IN = "allow_sweep_series_downgrade"
_THINKING_OPT_IN = "allow_thinking_tokens_downgrade"
_VOCABULARY_OPT_IN = "allow_freshness_vocabulary_downgrade"

_SOURCES_BEFORE = ("merge", "judgment", "score", "revive")
_SOURCES_AFTER = (*_SOURCES_BEFORE, "manual_update", "plan_reindex")
_NEW_SOURCES = ("manual_update", "plan_reindex")

#: The six tables tracked by the decay — the same six as 043, which installed
#: these CHECKs. The order follows the constraint names, for stable messages.
_DECAY_TABLES = ("adrs", "decisions", "indexed_plans", "learnings", "runbooks", "snippets")


def _check_sql(table: str, sources: tuple[str, ...]) -> str:
    values = ", ".join(f"'{source}'" for source in sources)
    return (
        f"ALTER TABLE {table} ADD CONSTRAINT ck_{table}_freshness_source "
        f"CHECK (freshness_source IS NULL OR freshness_source IN ({values}))"
    )


def upgrade() -> None:
    # GENUINELY replayable (048's template): someone will replay these lines by
    # hand during the cutover, so the idempotence promise must hold everywhere.
    op.execute("ALTER TABLE dream_runs ADD COLUMN IF NOT EXISTS closed_inactive_count INTEGER")
    op.execute("ALTER TABLE dream_runs ADD COLUMN IF NOT EXISTS thinking_tokens INTEGER")
    for table in _DECAY_TABLES:
        op.execute(f"ALTER TABLE {table} DROP CONSTRAINT IF EXISTS ck_{table}_freshness_source")
        op.execute(_check_sql(table, _SOURCES_AFTER))


def downgrade() -> None:
    arguments = context.get_x_argument(as_dictionary=True)

    # 1. The sweep's series: destroying it erases the only night-by-night trace
    #    of closures for inactivity — the closed sessions remain, but the "how
    #    many that night" cannot be rebuilt after the fact.
    series_opted = arguments.get(_SERIES_OPT_IN) == "yes"
    op.execute(
        f"""
        DO $$
        DECLARE
            nights bigint;
            dates text;
        BEGIN
            SELECT count(*), coalesce(string_agg(DISTINCT run_date::text, ', '), '')
              INTO nights, dates
            FROM dream_runs
            WHERE closed_inactive_count IS NOT NULL;

            IF {"FALSE" if series_opted else "TRUE"} AND nights > 0 THEN
                RAISE EXCEPTION
                    'cannot downgrade 049: % dream_runs row(s) carry the closed_inactive '
                    'series (dates: %). Dropping the column erases the only per-night '
                    'record of inactivity closures. Export it, or rerun with '
                    '-x {_SERIES_OPT_IN}=yes',
                    nights, dates;
            END IF;
        END;
        $$
        """
    )

    # 2. The thinking_tokens: destroying them returns the agy rail to the ~38%
    #    under-declaration the column existed to close.
    thinking_opted = arguments.get(_THINKING_OPT_IN) == "yes"
    op.execute(
        f"""
        DO $$
        DECLARE
            measured bigint;
            dates text;
        BEGIN
            SELECT count(*), coalesce(string_agg(DISTINCT run_date::text, ', '), '')
              INTO measured, dates
            FROM dream_runs
            WHERE thinking_tokens IS NOT NULL;

            IF {"FALSE" if thinking_opted else "TRUE"} AND measured > 0 THEN
                RAISE EXCEPTION
                    'cannot downgrade 049: % dream_runs row(s) carry measured '
                    'thinking_tokens (dates: %). Dropping the column returns the agy '
                    'rail to its ~38%% under-declaration — the cost comparison that '
                    'ordered the provider chain becomes false again. Export it, or '
                    'rerun with -x {_THINKING_OPT_IN}=yes',
                    measured, dates;
            END IF;
        END;
        $$
        """
    )

    # 3. The vocabulary: restoring 043's CHECK is IMPOSSIBLE while rows carry
    #    the new values. The opt-in resets them to NULL — an ABSENT provenance
    #    is seen, a silently erased provenance is believed (043's words) — and
    #    only then re-installs the old CHECK.
    vocabulary_opted = arguments.get(_VOCABULARY_OPT_IN) == "yes"
    new_values = ", ".join(f"'{source}'" for source in _NEW_SOURCES)
    for table in _DECAY_TABLES:
        op.execute(
            f"""
            DO $$
            DECLARE
                carriers bigint;
                ids text;
            BEGIN
                SELECT count(*), coalesce(string_agg(id::text, ', '), '')
                  INTO carriers, ids
                FROM {table}
                WHERE freshness_source IN ({new_values});

                IF carriers > 0 THEN
                    IF {"FALSE" if vocabulary_opted else "TRUE"} THEN
                        RAISE EXCEPTION
                            'cannot downgrade 049: % row(s) of {table} declare a '
                            'provenance the 043 vocabulary cannot hold (%). Restoring '
                            'the old CHECK would fail outright; opting in NULLs these '
                            'provenances — a visible absence — before restoring it. '
                            'Record them elsewhere, or rerun with -x {_VOCABULARY_OPT_IN}=yes',
                            carriers, ids;
                    END IF;
                    UPDATE {table} SET freshness_source = NULL
                     WHERE freshness_source IN ({new_values});
                END IF;
            END;
            $$
            """
        )
        op.execute(f"ALTER TABLE {table} DROP CONSTRAINT IF EXISTS ck_{table}_freshness_source")
        op.execute(_check_sql(table, _SOURCES_BEFORE))

    op.execute("ALTER TABLE dream_runs DROP COLUMN IF EXISTS thinking_tokens")
    op.execute("ALTER TABLE dream_runs DROP COLUMN IF EXISTS closed_inactive_count")
