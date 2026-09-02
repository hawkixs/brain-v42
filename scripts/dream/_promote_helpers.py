"""Small helpers invoked by dream.sh for the PROMOTE phase.

Kept in Python (not bash) because the logic touches SQLAlchemy Core Tables
and datetime comparisons that are ugly in shell. Each sub-command emits a
single line on stdout so dream.sh can capture it cleanly.

CLI dispatch:
    python -m scripts.dream._promote_helpers recent-promotions [--limit 10]
    python -m scripts.dream._promote_helpers dream-run-id --date YYYY-MM-DD
    python -m scripts.dream._promote_helpers record-empty-pool --date YYYY-MM-DD
                                             --project-key KEY
                                             [--duration-seconds 0]

`recent-promotions` prints a JSON array of the last N dream_promotions
rows for the PROMOTE prompt's calibration context.
`dream-run-id` prints the most recent dream_runs.id for (phase='promote',
run_date=<date>) so the validator can flip its status to 'partial' on
integrity failure.
`record-empty-pool` writes the dream_runs row for a night where the maturity
filter returned nothing — see `_record_empty_pool` for why that row exists.
"""

from __future__ import annotations

import argparse
import asyncio
import datetime as dt
import json
import sys

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from brain_v42.config import Settings
from brain_v42.db.tables import dream_promotions, dream_runs

# Status of the "empty pool" row. `done` is no cosmetic choice: it is the ONLY
# non-failure status in the system. post_run_alert excludes
# {fail, partial, timeout}, but collector_dream and
# DreamRunService.last_failure count anything `!= 'done'` as a failure. An
# invented "neutral" status (skipped, noop) would move the false alarm from the
# alert to the briefing instead of putting it out.
EMPTY_POOL_STATUS = "done"
EMPTY_POOL_MESSAGE = (
    "empty candidate pool — no learning met the promotion maturity filter "
    "(access_count_human >= 3); nothing to promote, phase did not run"
)


def _build_factory(postgres_url: str) -> async_sessionmaker[AsyncSession]:
    engine = create_async_engine(postgres_url, pool_pre_ping=True)
    return async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)


async def _recent_promotions(
    session_factory: async_sessionmaker[AsyncSession], limit: int
) -> list[dict]:
    async with session_factory() as session:
        result = await session.execute(
            sa.select(dream_promotions).order_by(dream_promotions.c.created_at.desc()).limit(limit)
        )
        rows = result.mappings().all()
        return [
            {
                "id": row["id"],
                "dream_run_id": row["dream_run_id"],
                "source_learning_id": (
                    str(row["source_learning_id"]) if row["source_learning_id"] else None
                ),
                "target_type": row["target_type"],
                "target_adr_id": (str(row["target_adr_id"]) if row["target_adr_id"] else None),
                "target_runbook_id": (
                    str(row["target_runbook_id"]) if row["target_runbook_id"] else None
                ),
                "cosine_observed": row["cosine_observed"],
                "skipped_reason": row["skipped_reason"],
                "created_at": row["created_at"].isoformat() if row["created_at"] else None,
            }
            for row in rows
        ]


def dream_run_id_statement(run_date: dt.date, project_key: str) -> sa.Select:
    """SELECT one project's `promote` row for a given date.

    The project filter is not cosmetic. Its caller, `promote_validate`, WRITES
    on the row returned: it marks it `partial` and backfills
    `dream_promotions.dream_run_id`. Without the filter, across several
    projects, the query returns the day's last `promote` row whatever the
    project — and once written, the wrong attribution can no longer be
    recovered from the rows (spec §12).

    In the current sequential loop, the missing filter happened to return
    the right row: each project writes then immediately reads back. But
    the correctness then rested on "nobody writes between my write and
    my read", an invariant that nothing enforces and that the loop does
    not declare.

    `ORDER BY id DESC LIMIT 1` stays: a project can have two rows on the same
    day after a manual re-run, and it is the last one that counts.
    """
    return (
        sa.select(dream_runs.c.id)
        .where(
            sa.and_(
                dream_runs.c.phase == "promote",
                dream_runs.c.run_date == run_date,
                dream_runs.c.project_key == project_key,
            )
        )
        .order_by(dream_runs.c.id.desc())
        .limit(1)
    )


async def _dream_run_id(
    session_factory: async_sessionmaker[AsyncSession],
    run_date: dt.date,
    project_key: str,
) -> int | None:
    async with session_factory() as session:
        result = await session.execute(dream_run_id_statement(run_date, project_key))
        return result.scalar_one_or_none()


async def _record_empty_pool(
    session_factory: async_sessionmaker[AsyncSession],
    run_date: dt.date,
    duration_s: float,
    *,
    project_key: str,
) -> None:
    """INSERT the dream_runs row of a night whose candidate pool is empty.

    Without it, the promote phase — expected for as long as its killswitch is
    open — is ABSENT from dream_runs, and the alert manufactures a synthetic
    `partial` every night. Since migration 041 (maturity filter on
    `access_count_human`, no backfill) the pool is legitimately empty: that
    false alarm would push the operator into undoing 041.

    The right answer is to make the phase OBSERVED, never to remove it from the
    expected phases: a promote that CRASHES still writes no row at all and
    still fires the alert.

    `model` stays NULL — no model was called. `phase_dry_run` stays false:
    nothing ran, so the night is not a clean dry rehearsal and must not feed
    `_clean_dry_streak`.

    `project_key` is REQUIRED and has no default. `promote` is a PER-PROJECT
    phase, so the real key, never the global-phase sentinel — and this is the
    site the spec §14.2 inventory had forgotten, while filing this very file
    among the readers. Since 041's maturity filter, the pool is legitimately
    empty and this is the path that writes the `promote` row on most nights:
    leaving it NULL would make the "NULL = written before 042" semantics lie,
    with nothing to signal it.
    """
    statement = sa.insert(dream_runs).values(
        run_date=run_date,
        phase="promote",
        status=EMPTY_POOL_STATUS,
        model=None,
        duration_s=duration_s,
        error_message=EMPTY_POOL_MESSAGE,
        project_key=project_key,
        phase_dry_run=False,
    )
    async with session_factory() as session:
        await session.execute(statement)
        await session.commit()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="cmd", required=True)

    rp = sub.add_parser("recent-promotions")
    rp.add_argument("--limit", type=int, default=10)

    di = sub.add_parser("dream-run-id")
    di.add_argument("--date", required=True, help="ISO date (YYYY-MM-DD)")
    di.add_argument(
        "--project-key",
        required=True,
        help="Project the promote row belongs to — required, deliberately without a default",
    )

    ep = sub.add_parser("record-empty-pool")
    ep.add_argument("--date", required=True, help="ISO date (YYYY-MM-DD)")
    ep.add_argument("--duration-seconds", type=float, default=0.0)
    ep.add_argument(
        "--project-key",
        required=True,
        help="Project the phase ran for — required, deliberately without a default",
    )

    args = parser.parse_args(argv)
    session_factory = _build_factory(Settings().postgres_url)

    if args.cmd == "recent-promotions":
        rows = asyncio.run(_recent_promotions(session_factory, args.limit))
        json.dump(rows, sys.stdout)
        sys.stdout.write("\n")
        return 0

    if args.cmd == "dream-run-id":
        try:
            run_date = dt.date.fromisoformat(args.date)
        except ValueError as exc:
            print(f"invalid --date: {exc}", file=sys.stderr)
            return 1
        row_id = asyncio.run(_dream_run_id(session_factory, run_date, args.project_key))
        sys.stdout.write(f"{row_id if row_id is not None else ''}\n")
        return 0

    if args.cmd == "record-empty-pool":
        try:
            run_date = dt.date.fromisoformat(args.date)
        except ValueError as exc:
            print(f"invalid --date: {exc}", file=sys.stderr)
            return 1
        try:
            asyncio.run(
                _record_empty_pool(
                    session_factory,
                    run_date,
                    args.duration_seconds,
                    project_key=args.project_key,
                )
            )
        except Exception as exc:  # noqa: BLE001 — rc != 0 suffit ; dream.sh journalise un WARN
            # Never swallow: without a row, the synthetic alert comes back.
            # Noisy but observable — that is the direction of travel.
            print(f"record-empty-pool failed: {exc!r}", file=sys.stderr)
            return 1
        return 0

    return 2


if __name__ == "__main__":
    sys.exit(main())
