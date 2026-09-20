"""Read the latest Dream night through a caller-owned database snapshot."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from typing import TYPE_CHECKING

import sqlalchemy as sa

from brain_v42.db.tables import dream_runs
from brain_v42.dream_run_project_key import GLOBAL_PHASE_PROJECT_KEY

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession


@dataclass(frozen=True, slots=True)
class LastNight:
    """The scalar aggregate of one run date: rows by status and by dry flag."""

    run_date: date
    rows: int
    done: int
    fail: int
    timeout: int
    partial: int
    other: int
    wet: int
    dry: int
    projects: int
    #: `created_at` is nullable in the table: a night whose rows all lack it has
    #: no finishing instant, and the probe says so instead of inventing one.
    finished_at: datetime | None


_STATUS_COUNTS = tuple(
    sa.func.count().filter(dream_runs.c.status == status).label(status)
    for status in ("done", "fail", "timeout", "partial")
)
_DONE, _FAIL, _TIMEOUT, _PARTIAL = _STATUS_COUNTS
_ROWS = sa.func.count()

# One statement keeps the choice of latest date and every aggregate in the
# source transaction the registry opened. `phase_dry_run` is NOT NULL with a
# server default of false (tables.py), so `wet + dry == rows` by construction.
_LAST_NIGHT_SQL = (
    sa.select(
        dream_runs.c.run_date,
        _ROWS.label("rows"),
        *_STATUS_COUNTS,
        (_ROWS - (_DONE + _FAIL + _TIMEOUT + _PARTIAL)).label("other"),
        sa.func.count().filter(dream_runs.c.phase_dry_run.is_(False)).label("wet"),
        sa.func.count().filter(dream_runs.c.phase_dry_run.is_(True)).label("dry"),
        # The global phases (extract, roadmap, sweep) write the '*' sentinel: a
        # row of the night, never a project. NULL (pre-042 rows) is already
        # left out by count(distinct).
        sa.func.count(sa.distinct(dream_runs.c.project_key))
        .filter(dream_runs.c.project_key != GLOBAL_PHASE_PROJECT_KEY)
        .label("projects"),
        sa.func.max(dream_runs.c.created_at).label("finished_at"),
    )
    .where(dream_runs.c.run_date == sa.select(sa.func.max(dream_runs.c.run_date)).scalar_subquery())
    .group_by(dream_runs.c.run_date)
)


async def read_last_night(session: AsyncSession) -> LastNight | None:
    """Read the latest run date's scalar summary without opening another snapshot."""
    row = (await session.execute(_LAST_NIGHT_SQL)).mappings().one_or_none()
    if row is None:
        return None
    return LastNight(
        run_date=row["run_date"],
        rows=int(row["rows"]),
        done=int(row["done"]),
        fail=int(row["fail"]),
        timeout=int(row["timeout"]),
        partial=int(row["partial"]),
        other=int(row["other"]),
        wet=int(row["wet"]),
        dry=int(row["dry"]),
        projects=int(row["projects"]),
        finished_at=row["finished_at"],
    )
