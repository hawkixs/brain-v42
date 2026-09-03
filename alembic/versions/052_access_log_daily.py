"""The access journal that survives its own aggregation.

Revision ID: 052
Revises: 051

Ticket b93e32be, option (b), decided on the brief of 2026-09-03.

WHAT THIS REPAIRS. `access_log` is a QUEUE, not a journal: `DecayFlusher` drains
it every 300 s, and `pg_access_log.aggregate_in_session` folds the actor into a
boolean through `is_human_actor()` before deleting the rows. The counters
`access_count_human` and `last_accessed_at_human` are therefore decided ONCE, at
flush time, on top of source events that no longer exist. If the human/machine
rule ever changes — a new system prefix, `_unexpanded` requalified, a surveyed
machine actor added — nothing can be recomputed.

That loss is already consumed and it GROWS. Measured 2026-09-03: 1 019 learnings
carry `access_count_human > 0` and 993 carry `last_accessed_at_human`, for ZERO
rows in `access_log` — against 754/718 on 2026-08-16 and 837/807 on 2026-08-21.
The measured flow is ~590 access events a day, ~101 of them human. This is the
only debt in the book whose cost of waiting is strictly increasing and not
recoverable.

WHY A SEPARATE TABLE RATHER THAN A LONGER `access_log` RETENTION. ADR #21,
accepted 2026-09-03, rules that `access_log` stays a transient aggregation queue
and that "any future need for exhaustive audit or durable attribution requires
SEPARATE STORAGE". Extending the queue's retention would contradict it. Storage
was not the argument either way: 365 days of raw rows is ~39 MB and this table
is ~45 MB/year, against 72 MB for `learnings` today.

WHAT IT KEEPS AND WHAT IT DROPS. It keeps the ACTOR STRING, verbatim, per day —
which is exactly what makes an `is_human_actor` requalification replayable
forever. It drops the intra-day timestamp and `access_type`: this answers "how
many times did actor X read entity Y on day D", never "at 14:03:22".

WIDTHS COPIED FROM THE SOURCE, deliberately: `entity_type VARCHAR(20)` and
`actor VARCHAR(64)` are `access_log`'s own. A wider column here would silently
accept what the queue cannot hold; a narrower one would truncate on the way in.
The coupling is real and is the point — this table stores that queue and nothing
else.

`actor` IS `NOT NULL`, AND NO SENTINEL IS INVENTED. `access_log.actor` is itself
`NOT NULL` (measured), so no NULL can ever reach this table and the primary key
is safe as written. Mapping a missing actor onto `'unknown'` was considered and
REFUSED, and the source is what settles it: `access_log.actor` carries
`server_default 'unknown'`, so `'unknown'` is a value production ALREADY writes,
and `is_human_actor` reads it as non-human; a synthetic third meaning stacked on
the same string would destroy the very distinction this table exists to preserve.

NOT NULLABLE, unlike 040-041-042-046-048-049 — and that is not a departure from
the doctrine but its consequence. Those were columns ADDED to populated tables,
where `NULL` had to mean "written before the migration". This is a NEW table:
every row is written by the writer that ships with it, so there is no "before"
to represent. No backfill either, and for a harder reason than usual: the source
events of everything preceding this migration were deleted years of flushes ago.
"""

from __future__ import annotations

from alembic import context, op

revision = "052"
down_revision = "051"
branch_labels = None
depends_on = None

#: NAMED opt-in, the template of 039/046/048/049 — never a generic flag.
_OPT_IN = "allow_access_log_daily_downgrade"


def upgrade() -> None:
    # GENUINELY replayable (048/049's template): an operator will replay these
    # lines by hand during the cutover, so idempotence must hold everywhere.
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS access_log_daily (
            entity_type      VARCHAR(20)  NOT NULL,
            entity_id        UUID         NOT NULL,
            actor            VARCHAR(64)  NOT NULL,
            day              DATE         NOT NULL,
            count            INTEGER      NOT NULL,
            last_accessed_at TIMESTAMPTZ  NOT NULL,
            CONSTRAINT pk_access_log_daily
                PRIMARY KEY (entity_type, entity_id, actor, day)
        )
        """
    )
    # The read the decay and any future reclassification actually make: "this
    # entity, over this window". The primary key cannot serve it — its leading
    # columns are right, but `actor` sits between `entity_id` and `day`, so a
    # range on `day` alone cannot use it.
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS ix_access_log_daily_entity_day
            ON access_log_daily (entity_type, entity_id, day)
        """
    )


def downgrade() -> None:
    arguments = context.get_x_argument(as_dictionary=True)
    opted = arguments.get(_OPT_IN) == "yes"

    # The refusal NAMES the days, because that is what is destroyed: the source
    # events were deleted by the flush 300 s after they happened, so this table
    # is the ONLY remaining trace of who read what. Dropping it does not degrade
    # a signal, it removes the last copy — and puts `access_count_human` back to
    # being unrecomputable, which is the whole reason b93e32be exists.
    op.execute(
        f"""
        DO $$
        DECLARE
            days bigint;
            rows_kept bigint;
            span text;
        BEGIN
            SELECT count(DISTINCT day), count(*),
                   coalesce(min(day)::text || ' → ' || max(day)::text, '')
              INTO days, rows_kept, span
            FROM access_log_daily;

            IF {"FALSE" if opted else "TRUE"} AND rows_kept > 0 THEN
                RAISE EXCEPTION
                    'cannot downgrade 052: access_log_daily holds % row(s) over % day(s) '
                    '(%). Their source events in access_log were deleted by the decay '
                    'flush minutes after they happened, so this table is the ONLY '
                    'surviving record of which actor read which entity. Dropping it '
                    'makes access_count_human unrecomputable again — the exact loss '
                    'ticket b93e32be was opened to stop. Export it, or rerun with '
                    '-x {_OPT_IN}=yes',
                    rows_kept, days, span;
            END IF;
        END;
        $$
        """
    )
    op.execute("DROP INDEX IF EXISTS ix_access_log_daily_entity_day")
    op.execute("DROP TABLE IF EXISTS access_log_daily")
