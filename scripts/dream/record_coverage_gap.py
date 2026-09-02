"""Write the `dream_runs` row carrying a night's coverage verdict.

Ticket `0a9c067e`. Its central lesson: an alert nobody reads is
indistinguishable from an absent alert. The coverage comparator was right three
nights running and nothing happened, because its output reached the dated file
and nothing else.

`post_run_alert` stays READ-ONLY — that is a contract pinned by a test. The
writer therefore lives here, in a separate module called by `dream.sh` when the
verdict escalates. The row it writes reaches TWO existing readers without a
line of code on their side:

- `DreamRunService.last_failure` → the session briefing's "### Last failure";
- `collect_nightly_ops` → `/metrics` `nightly.last_failure`.

An accepted price, stated: being the most recent, it takes the "Last failure"
slot from a phase failure of the same night, and `/metrics` `last_run.status`
turns `partial` on those nights — which is true.

Modelled on `_promote_helpers._record_empty_pool`, with its two properties:

- it NEVER raises — a telemetry error does not kill a night;
- it REPORTS all the same, through its exit code. "Best-effort" is not "always
  returns 0": it is precisely `record-empty-pool`'s exit code that makes a lost
  `dream_runs` row observable. Its caller therefore wraps it in `set +e`,
  `set -euo pipefail` being active in `dream.sh`.

CLI:
    python -m scripts.dream.record_coverage_gap --date 2026-08-18 \\
        --summary "COVERAGE mode=manifest …" [--detail "COVERAGE_SILENT …"]

Exit codes:
    0  → row written (or updated)
    1  → failure — a WARN is printed on stderr, no exception propagates
"""

from __future__ import annotations

import argparse
import asyncio
import datetime as dt
import sys

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from brain_v42.config import Settings
from brain_v42.db.tables import dream_runs
from brain_v42.dream_run_project_key import GLOBAL_PHASE_PROJECT_KEY

# 8 characters — `dream_runs.phase` is a `varchar(10)`, measured, and a test
# reads the length from the real metadata rather than copying the number.
COVERAGE_PHASE = "coverage"

# `fail`, not a new status. `collector_dream` and
# `DreamRunService.last_failure` count anything `!= 'done'` as a failure, and
# `codex_dream_run_v1` projects `status` to `codex_ro`: inventing a value would
# be an external contract change for no gain. Here the failure is real.
COVERAGE_STATUS = "fail"

# `error_message` is unbounded `text` — measured. We bound it anyway: an
# unreadable report goes unread, which is the ticket's original defect.
MAX_ERROR_MESSAGE_CHARS = 4000

EMPTY_VERDICT_MESSAGE = (
    "dream_runs coverage gap reported by scripts.dream.post_run_alert "
    "(no machine line captured — see the dated log)"
)


def _build_factory(postgres_url: str) -> async_sessionmaker[AsyncSession]:
    engine = create_async_engine(postgres_url, pool_pre_ping=True)
    return async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)


def build_error_message(summary: str, detail: str | None) -> str:
    """The machine line, then the offending pairs. Never empty, never huge."""
    parts = [part.strip() for part in (summary, detail or "") if part and part.strip()]
    message = "\n".join(parts) if parts else EMPTY_VERDICT_MESSAGE
    if len(message) <= MAX_ERROR_MESSAGE_CHARS:
        return message
    marker = "… (truncated)"
    return message[: MAX_ERROR_MESSAGE_CHARS - len(marker)] + marker


def _existing_row_statement(run_date: dt.date) -> sa.Select:
    return (
        sa.select(dream_runs.c.id)
        .where(dream_runs.c.run_date == run_date, dream_runs.c.phase == COVERAGE_PHASE)
        .order_by(dream_runs.c.id.desc())
        .limit(1)
    )


async def record_coverage_gap(
    session_factory: async_sessionmaker[AsyncSession],
    run_date: dt.date,
    *,
    summary: str,
    detail: str | None = None,
) -> None:
    """Write ONE `coverage` row for the night, idempotent on `run_date`.

    A manual morning replay updates the row instead of stacking a second one:
    two contradictory verdicts for the same night would be worth less than no
    verdict at all.

    `project_key` carries the global-phase sentinel: `coverage` judges the whole
    night, not one project. It NEVER travels through
    `canonicalize_project_key`, whose pattern rejects it.
    """
    message = build_error_message(summary, detail)
    async with session_factory() as session:
        existing = await session.execute(_existing_row_statement(run_date))
        row_id = existing.scalar_one_or_none()
        statement: sa.Update | sa.Insert
        if row_id is None:
            statement = sa.insert(dream_runs).values(
                run_date=run_date,
                phase=COVERAGE_PHASE,
                status=COVERAGE_STATUS,
                model=None,
                duration_s=0.0,
                error_message=message,
                project_key=GLOBAL_PHASE_PROJECT_KEY,
                phase_dry_run=False,
            )
        else:
            statement = (
                sa.update(dream_runs)
                .where(dream_runs.c.id == row_id)
                .values(status=COVERAGE_STATUS, error_message=message)
            )
        await session.execute(statement)
        await session.commit()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--date", required=True, help="Run date (YYYY-MM-DD)")
    parser.add_argument("--summary", default="", help="La ligne machine COVERAGE de la nuit")
    parser.add_argument("--detail", default="", help="La ligne COVERAGE_SILENT, si elle existe")
    args = parser.parse_args(argv)

    try:
        run_date = dt.date.fromisoformat(args.date)
    except ValueError as exc:
        print(f"invalid --date: {exc}", file=sys.stderr)
        return 1

    try:
        session_factory = _build_factory(Settings().postgres_url)
        asyncio.run(
            record_coverage_gap(session_factory, run_date, summary=args.summary, detail=args.detail)
        )
    except Exception as exc:  # noqa: BLE001 — never raises; the rc carries it
        print(f"WARN record-coverage-gap failed: {exc!r}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
