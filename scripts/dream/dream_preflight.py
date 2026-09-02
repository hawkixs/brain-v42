"""Pre-flight gate: skip the expensive Opus phases when the corpus is unchanged.

Background: ~40% of nights the Opus phases (synth/promote/reorg) re-process a
corpus that has not changed since the previous run, decide nothing (0
tool_calls), and burn ~$1 each (2026-06-22 audit). This gate short-circuits
those phases when the brain corpus is provably unchanged since the last dream
run, while keeping the cheap sonnet phases (scan/clean/connect) for visibility.

Conservative by design: it only emits SKIP when it can PROVE the corpus is
static; any uncertainty (no prior run, query error, NULL timestamps) => RUN.

Caveat: the change signal is `created_at`/`content_updated_at` only — NOT
`last_accessed_at`. Including access time would make the gate never skip (reads
happen constantly). The trade-off is that an entity that becomes promote-eligible
purely via access-count crossing a threshold (no create/update) could be skipped
for a night; promote already has its own empty-pool skip, and such transitions
are a slow trickle, so this is an accepted v1 limitation.

Usage:
    python -m scripts.dream.dream_preflight --date 2026-06-22
    -> prints "RUN" or "SKIP: <reason>" on stdout; dream.sh skips the Opus
       phases iff the line starts with "SKIP".
"""

from __future__ import annotations

import argparse
import asyncio
from datetime import date, datetime

from brain_v42.db.dsn import resolve_postgres_dsn

try:
    import asyncpg
except ImportError:  # pragma: no cover — asyncpg absent only in minimal envs
    asyncpg = None

# Entity tables whose creation/modification means the Opus phases have new work.
_ENTITY_TABLES = ("decisions", "learnings", "snippets", "runbooks", "adrs")


def _mutation_sql() -> str:
    """Build the query for the non-dream-origin mutation signal.

    Two exclusions compared with the original version:

    - `content_updated_at` replaces `updated_at`, which moved on every counter
      write — the main cause of the 48 RUN against 2 SKIP measured over 50
      nights;
    - entities tagged `dream:generated` leave the signal, otherwise SYNTH
      guarantees, by creating its insights, that the next night will
      par-dessus sa propre production.

    `tags` is NOT NULL DEFAULT '{}' on all five tables (verified 2026-08-06,
    zero NULL in the database), so `ANY(tags)` cannot return NULL and make a
    row vanish from the signal. Were that constraint ever to fall, a COALESCE
    would be needed: a NULL predicate would exclude the row and produce a SKIP
    wrongly, which this module promises is impossible.

    `greatest()` carries the SAME load here, and it is less visible.
    `content_updated_at` ships with 041 without a backfill: measured
    2026-08-08, it is NULL on 2739 learnings out of 2740. The signal
    therefore only holds because ANSI/PostgreSQL's `GREATEST` **ignores
    NULLs** and falls back on `created_at` — verified in the database, not
    assumed. That is a PostgreSQL-specific semantics: MySQL and Oracle return
    NULL as soon as one argument is NULL, which would cancel each table's
    `max()` and produce a permanent SKIP. No test covers this behaviour — the
    module's only test counts string occurrences in the SQL. Do not replace
    `greatest` with an "equivalent" expression without measuring how it
    behaves on NULL.

    An accepted blind spot: the exclusion works by entity tag, not by writing
    actor, so a human edit to the content of an entity already tagged
    `dream:generated` stays invisible to the mutation signal — worst case, one
    deferred SYNTH night.
    """
    return " UNION ALL ".join(
        f"SELECT max(greatest(created_at, content_updated_at)) AS ts FROM {t} "
        f"WHERE NOT ('dream:generated' = ANY(tags))"
        for t in _ENTITY_TABLES
    )


def should_skip_opus_phases(
    latest_mutation: datetime | None,
    last_run: datetime | None,
) -> bool:
    """Return True iff the Opus phases can be safely skipped.

    Skips only when the corpus is provably unchanged since the previous dream
    run (no entity created or updated strictly after it). Conservative: any
    unknown (None) returns False so the phases run.
    """
    if last_run is None or latest_mutation is None:
        return False
    return latest_mutation <= last_run


async def _fetch_signals(dsn: str, current_date: date) -> tuple[datetime | None, datetime | None]:
    """Return (latest_mutation, last_run) read from PG.

    last_run excludes the current run_date so the rows this very night already
    inserted (scan/clean/connect) cannot be mistaken for the previous run.
    """
    conn = await asyncpg.connect(dsn)
    try:
        last_run: datetime | None = await conn.fetchval(
            "SELECT max(created_at) FROM dream_runs WHERE run_date < $1",
            current_date,
        )
        latest_mutation: datetime | None = await conn.fetchval(
            f"SELECT max(ts) FROM ({_mutation_sql()}) m"
        )
        return latest_mutation, last_run
    finally:
        await conn.close()


def main() -> None:
    parser = argparse.ArgumentParser(description="Dream Opus-phase pre-flight gate")
    parser.add_argument("--date", required=True, help="Current run date (YYYY-MM-DD)")
    args = parser.parse_args()
    current_date = date.fromisoformat(args.date)

    if asyncpg is None:
        print("RUN (asyncpg unavailable)")
        return

    # The resolution is INSIDE the try: it raises when nothing configures
    # POSTGRES_URL, and this gate must stay fail-open — a configuration error
    # must never skip the Opus phases by mistake. The `brain:brain` literal that
    # used to live here survived the password rotation by printing
    # `RUN (preflight error: …)` night after night: correct for this gate, but
    # no proof at all that the configuration was sound.
    try:
        latest_mutation, last_run = asyncio.run(
            _fetch_signals(resolve_postgres_dsn(), current_date)
        )
    except Exception as exc:  # fail-safe: any error must NOT cause a wrong skip
        print(f"RUN (preflight error: {exc})")
        return

    if should_skip_opus_phases(latest_mutation, last_run):
        print(
            f"SKIP: corpus unchanged since last run {last_run} (latest mutation {latest_mutation})"
        )
    else:
        print("RUN")


if __name__ == "__main__":
    main()
