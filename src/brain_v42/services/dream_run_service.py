"""DreamRunService — read-only briefing helpers over dream_runs.

Two queries that power the session-start killswitch and last-failure sections.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from typing import TYPE_CHECKING, cast

import sqlalchemy as sa

from brain_v42.db.tables import dream_runs as _default_dream_runs
from brain_v42.dream_degradation import DEGRADED_PREFIX
from brain_v42.dream_killswitches import KILLSWITCHES_PATH, parse_killswitches

if TYPE_CHECKING:
    from pathlib import Path

    from sqlalchemy import Table
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker


def _read_killswitch_flags(path: Path | None) -> dict[str, bool]:
    """enabled/dry flags from the systemd drop-in — the killswitch source of truth.

    Returns {} if the file is unreadable (CI, dev without the drop-in): the
    caller then falls back on the phase's presence in dream_runs (historical
    behaviour, graceful degradation).
    """
    ks_path = path if path is not None else KILLSWITCHES_PATH
    try:
        return parse_killswitches(ks_path.read_text())
    except OSError:
        return {}


@dataclass(frozen=True)
class KillswitchState:
    last_run_date: date | None
    promote_enabled: bool
    promote_dry: bool
    reorg_enabled: bool
    reorg_dry: bool
    promote_clean_dry_nights: int
    reorg_clean_dry_nights: int
    extract_enabled: bool = False
    extract_dry: bool = True
    extract_clean_dry_nights: int = 0
    roadmap_enabled: bool = False
    roadmap_dry: bool = True
    roadmap_clean_dry_nights: int = 0
    sweep_enabled: bool = False
    sweep_dry: bool = True
    sweep_clean_dry_nights: int = 0


@dataclass(frozen=True)
class LastFailureRow:
    phase: str
    run_date: date
    error_message: str | None


class DreamRunService:
    # Convention: `self._sf` mirrors roadmap_service.py.
    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        *,
        table: Table | None = None,
    ) -> None:
        self._sf = session_factory
        self._t = table if table is not None else _default_dream_runs

    async def killswitch_state(
        self, within_days: int = 7, killswitches_path: Path | None = None
    ) -> KillswitchState:
        t = self._t
        cutoff = date.today() - timedelta(days=within_days)
        ks = _read_killswitch_flags(killswitches_path)
        async with self._sf() as session:
            last_date = (
                await session.execute(
                    sa.select(sa.func.max(t.c.run_date)).where(t.c.run_date >= cutoff)
                )
            ).scalar()
            if last_date is None:
                return KillswitchState(
                    last_run_date=None,
                    promote_enabled=False,
                    promote_dry=False,
                    reorg_enabled=False,
                    reorg_dry=False,
                    promote_clean_dry_nights=0,
                    reorg_clean_dry_nights=0,
                )

            rows = (
                (
                    await session.execute(
                        sa.select(t.c.phase, t.c.phase_dry_run).where(t.c.run_date == last_date)
                    )
                )
                .mappings()
                .all()
            )
            phases = {r["phase"]: r for r in rows}

            def phase_enabled(name: str) -> bool:
                # Source of truth = the killswitch file; fall back on presence
                # in the last run if the file is unreadable. Decouples the
                # enabled state from the pre-flight gate, which skips
                # promote/reorg when the corpus is unchanged and used to display
                # "disabled" wrongly (learning 6d5ee46b).
                return ks.get(name, name in phases)

            def phase_dry(name: str, file_default: bool) -> bool:
                # Real mode = what the phase did on the last run; if absent
                # (pre-flight skip), fall back on the file's declared DRY_RUN.
                if name in phases:
                    return bool(phases[name]["phase_dry_run"])
                return ks.get(f"{name}_dry", file_default)

            promote_enabled = phase_enabled("promote")
            reorg_enabled = phase_enabled("reorg")
            promote_dry = phase_dry("promote", False)  # pas de DRY_RUN fichier → wet
            reorg_dry = phase_dry("reorg", False)  # don't-care si disabled ; défaut legacy

            promote_streak = await self._clean_dry_streak(session, "promote")
            reorg_streak = await self._clean_dry_streak(session, "reorg")

            extract_enabled = phase_enabled("extract")
            extract_dry = phase_dry("extract", True)
            extract_streak = await self._clean_dry_streak(session, "extract")

            roadmap_enabled = phase_enabled("roadmap")
            roadmap_dry = phase_dry("roadmap", True)
            roadmap_streak = await self._clean_dry_streak(session, "roadmap")

            sweep_enabled = phase_enabled("sweep")
            sweep_dry = phase_dry("sweep", True)
            sweep_streak = await self._clean_dry_streak(session, "sweep")

        return KillswitchState(
            last_run_date=last_date,
            promote_enabled=promote_enabled,
            promote_dry=promote_dry,
            reorg_enabled=reorg_enabled,
            reorg_dry=reorg_dry,
            promote_clean_dry_nights=promote_streak,
            reorg_clean_dry_nights=reorg_streak,
            extract_enabled=extract_enabled,
            extract_dry=extract_dry,
            extract_clean_dry_nights=extract_streak,
            roadmap_enabled=roadmap_enabled,
            roadmap_dry=roadmap_dry,
            roadmap_clean_dry_nights=roadmap_streak,
            sweep_enabled=sweep_enabled,
            sweep_dry=sweep_dry,
            sweep_clean_dry_nights=sweep_streak,
        )

    async def _clean_dry_streak(self, session: AsyncSession, phase: str) -> int:
        # dream.sh emits done|timeout|fail; anything != 'done' = failure.
        #
        # A DEGRADED night counts as a reset even though it is 'done': it was
        # served by the FALLBACK model, so it proves nothing about the intended
        # model. The predicate is on the PREFIX and not on the presence of a
        # message — `extract` legitimately writes "N ticket(s) deferred…" on
        # 'done' runs, and counting those as degraded would zero this counter
        # every night.
        t = self._t
        reset_date = (
            await session.execute(
                sa.select(sa.func.max(t.c.run_date))
                .where(t.c.phase == phase)
                .where(
                    sa.or_(
                        t.c.status != "done",
                        t.c.phase_dry_run.is_(False),
                        t.c.error_message.like(f"{DEGRADED_PREFIX}%"),
                    )
                )
            )
        ).scalar()
        model_change_date = await self._last_model_change(session, phase)
        if model_change_date is not None and (reset_date is None or model_change_date > reset_date):
            reset_date = model_change_date
        # DISTINCT run_date: a manual re-run on the same day is not one more
        # "night" (roadmap verification finding, 2026-07-04).
        stmt = (
            sa.select(sa.func.count(sa.func.distinct(t.c.run_date)))
            .select_from(t)
            .where(t.c.phase == phase)
            .where(t.c.status == "done")
            .where(t.c.phase_dry_run.is_(True))
        )
        if reset_date is not None:
            stmt = stmt.where(t.c.run_date > reset_date)
        return int((await session.execute(stmt)).scalar() or 0)

    async def _last_model_change(self, session: AsyncSession, phase: str) -> date | None:
        """Last night the phase changed model, otherwise None.

        Clean nights from ANOTHER model are no evidence for the current model.
        On 2026-08-05, ROADMAP displayed "22 clean DRY nights", ten of them
        produced by its FALLBACK model after the primary died — and that counter
        was being used as an argument to switch to WET.

        A model that was never recorded (NULL) is a constant value like any
        other: it triggers no reset for as long as it does not change.
        """
        t = self._t
        rows = (
            await session.execute(
                sa.select(t.c.run_date, t.c.model)
                .where(t.c.phase == phase)
                .where(t.c.status == "done")
                .order_by(t.c.run_date.desc(), t.c.created_at.desc())
            )
        ).all()
        if not rows:
            return None
        current = rows[0][1]
        for run_date, model in rows:
            if model != current:
                return cast(date, run_date)
        return None

    async def last_failure(self, within_days: int = 7) -> LastFailureRow | None:
        # dream.sh emits done|timeout|fail; anything != 'done' = failure.
        t = self._t
        cutoff = datetime.now(tz=UTC) - timedelta(days=within_days)
        stmt = (
            sa.select(t)
            .where(t.c.status != "done")
            .where(t.c.created_at >= cutoff)
            .order_by(t.c.created_at.desc())
            .limit(1)
        )
        async with self._sf() as session:
            row = (await session.execute(stmt)).mappings().first()
        if row is None:
            return None
        return LastFailureRow(
            phase=row["phase"],
            run_date=row["run_date"],
            error_message=row["error_message"],
        )
