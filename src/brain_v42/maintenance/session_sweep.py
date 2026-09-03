"""Dream `sweep` phase — drain the open sessions that show no sign of life.

Spec: docs/superpowers/specs/2026-08-07-session-lifecycle-sweep-design.md
M-G: docs/design/refonte-projets-sessions/SPEC-M-G.md §4 (eligibility threshold).

Deterministic and model-free: no LLM call, no network. The ``dream_runs`` row
therefore carries ``model = NULL`` — an already accepted shape, observed on
``extract`` and on the ``roadmap`` run of 2026-08-05.

**TWO rules, ONE statement, two counters.** The 7 d rule abandons sessions with
no heartbeat; the 4 h rule closes unobserved ``agent`` tracers as
``closed_inactive``. The two outcomes are counted SEPARATELY: ``abandoned``
carries a reason and never a ledger, ``closed_inactive`` carries its ledger and
no reason — adding them would erase the distinction 046 cost a migration to
create.

Shipped DRY: ``--wet`` is the only path that writes. And the 4 h rule is shipped
CLOSED on top of that, behind ``BRAIN_SESSION_INACTIVE_SWEEP_ENABLED``: this
phase runs WET every night from the repository, so merging the rule without a
flag would arm it from the following night.

Usage:
    python -m brain_v42.maintenance.session_sweep           # dry (default)
    python -m brain_v42.maintenance.session_sweep --wet     # applies
"""

from __future__ import annotations

import argparse
import asyncio
import sys
import time
from datetime import date, timedelta
from typing import Any

import sqlalchemy as sa

from brain_v42.dream_run_project_key import GLOBAL_PHASE_PROJECT_KEY
from brain_v42.models.brain_session import (
    AGENT_INACTIVE_AFTER,
    AUTO_STALE_AFTER,
    BrainSessionStatus,
    BrainSessionSweepResult,
)

_MAX_ERROR_CHARS = 2000


def _positive_int(value: str) -> int:
    number = int(value)
    if number < 1:
        raise argparse.ArgumentTypeError(f"doit être >= 1 (reçu : {number})")
    return number


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="session_sweep",
        description="Abandonner les sessions ouvertes sans heartbeat depuis N jours.",
    )
    parser.add_argument(
        "--wet",
        action="store_true",
        help="applique les abandons (défaut : dry, aucune écriture)",
    )
    parser.add_argument(
        "--older-than-days",
        type=_positive_int,
        # Default READ from the constant, never copied: two copies of one
        # threshold is the class of defect learning 8dc7e042 records.
        default=AUTO_STALE_AFTER.days,
        help=f"seuil en jours (défaut : {AUTO_STALE_AFTER.days}, depuis AUTO_STALE_AFTER)",
    )
    return parser


def render_report(result: BrainSessionSweepResult) -> str:
    """Text report of the sweep, for the night's dated log.

    The two outcomes are named SEPARATELY in the header and on every line. A log
    saying "17 sessions processed" would let a hurried reader conclude "17
    abandonments" — and the gap between the two rules is precisely what the
    observation window is watching.

    ``inactive_cutoff=off`` says the 4 h rule is CLOSED. Leaving it out would let
    a night with zero closures read as "no inactive tracer", when the rule was
    not even evaluated — a silent ceiling.
    """
    mode = "DRY" if result.dry_run else "WET"
    cutoff = result.cutoff.isoformat(timespec="seconds")
    inactive = (
        "off"
        if result.inactive_cutoff is None
        else result.inactive_cutoff.isoformat(timespec="seconds")
    )
    header = f"sweep [{mode}] stale_cutoff={cutoff} inactive_cutoff={inactive}"

    tallied = _tally(result)
    if not result.candidates:
        return f"{header} — aucune session à tarir"

    verb = "auraient reçu" if result.dry_run else "ont reçu"
    lines = [
        f"{header} — {len(result.candidates)} sessions {verb} : "
        f"{tallied[BrainSessionStatus.ABANDONED]} abandoned (7 j), "
        f"{tallied[BrainSessionStatus.CLOSED_INACTIVE]} closed_inactive (4 h)"
    ]
    lines.extend(
        f"  {candidate.outcome.value:<16} {candidate.project_key:<16} "
        f"{candidate.client_key:<40} "
        f"heartbeat={candidate.last_heartbeat_at.isoformat(timespec='seconds')} "
        f"observed={_stamp(candidate.last_observed_at)}"
        for candidate in result.candidates
    )
    return "\n".join(lines)


def _tally(result: BrainSessionSweepResult) -> dict[BrainSessionStatus, int]:
    """Count per outcome, in DRY too.

    The RESULT counters stay at zero in DRY — that is their contract, so no log
    reads "17 closed" where nothing was written. The report needs the other
    number, the enumeration: it derives it here, under a conditional verb.
    """
    tally = dict.fromkeys((BrainSessionStatus.ABANDONED, BrainSessionStatus.CLOSED_INACTIVE), 0)
    for candidate in result.candidates:
        tally[candidate.outcome] += 1
    return tally


def _stamp(value: Any) -> str:
    """``NULL`` reads "never observed", never "observed a long time ago"."""
    return "never" if value is None else value.isoformat(timespec="seconds")


async def record_dream_run(
    session_factory: Any,
    status: str,
    dry: bool,
    duration_s: float,
    error: str | None,
    closed_inactive_count: int | None = None,
) -> None:
    """INSERT dream_runs for phase='sweep'. Best-effort — never raises.

    `model` stays NULL: the phase calls no model. `project_key` receives the
    sentinel: `sweep` is a GLOBAL phase, it sits outside the loop and sweeps
    every project's sessions at once. The sentinel enters through a bound
    parameter, never through a flag — `test_dream_sh_sweep.py` pins
    `sweep_args` to `["--wet"]` and refuses any further argument.
    """
    try:
        async with session_factory() as session:
            async with session.begin():
                await session.execute(
                    sa.text(
                        "INSERT INTO dream_runs "
                        "(run_date, phase, status, duration_s, error_message, "
                        "project_key, phase_dry_run, model, closed_inactive_count) "
                        "VALUES (:run_date, 'sweep', :status, :duration_s, "
                        ":error_message, :project_key, :phase_dry_run, NULL, "
                        ":closed_inactive_count)"
                    ),
                    {
                        "run_date": date.today(),
                        "status": status,
                        "duration_s": duration_s,
                        "error_message": error,
                        "project_key": GLOBAL_PHASE_PROJECT_KEY,
                        "phase_dry_run": dry,
                        # NULL — never 0 — when the night FAILED before
                        # counting: "not evaluated" is not "zero closures", and
                        # that half of ticket 24ca3b73 stands.
                        #
                        # What changed on 2026-09-03 (554db5f8): a DRY night now
                        # records what it WOULD have closed instead of NULL. The
                        # rehearsal evaluates the 4 h rule for real — it just
                        # does not write — so throwing its number away lost the
                        # only measurement the night produced. `phase_dry_run`
                        # sits on the SAME row, so a consumer that wants real
                        # closures alone filters on it; measured before the
                        # change, no consumer read this column at all.
                        "closed_inactive_count": closed_inactive_count,
                    },
                )
    except Exception as exc:  # noqa: BLE001 — the trace must never kill the phase
        print(f"! warning: could not record dream_run: {exc}", file=sys.stderr)


async def _run(args: argparse.Namespace) -> int:
    from pydantic import ValidationError  # noqa: PLC0415

    from brain_v42.config import Settings  # noqa: PLC0415
    from brain_v42.db.engine import get_session_factory  # noqa: PLC0415
    from brain_v42.repositories.pg_brain_session import PgBrainSessionRepo  # noqa: PLC0415

    try:
        settings = Settings()  # type: ignore[call-arg]
    except ValidationError as exc:
        print(f"Config invalide: {exc}", file=sys.stderr)
        return 2

    # `None` closes the 4 h rule AND says so in the report. The flag is read
    # HERE, once, and never in the repository: the repository receives a
    # threshold or nothing, so a test can prove the rule without standing up a
    # configuration, and the arming decision stays in one place.
    close_inactive_after = (
        AGENT_INACTIVE_AFTER if settings.brain_session_inactive_sweep_enabled else None
    )

    session_factory = get_session_factory()
    dry = not args.wet
    started = time.monotonic()
    try:
        result = await PgBrainSessionRepo(session_factory).sweep_open_sessions(
            older_than=timedelta(days=args.older_than_days),
            close_inactive_after=close_inactive_after,
            # Derived from the threshold ACTUALLY used, never left to
            # sweep_open_sessions' default: at the default threshold this
            # reproduces the AUTO_STALE_ABANDONMENT_REASON constant exactly
            # (pinned by
            # test_default_threshold_reason_matches_the_module_constant); at any
            # other threshold the constant would lie about what was really
            # measured (Task 1 review finding, adjudicated).
            reason=f"auto_stale_{args.older_than_days}d",
            dry_run=dry,
        )
    except Exception as exc:  # noqa: BLE001 — traduit en row dream_runs + rc=1
        detail = f"{type(exc).__name__}: {exc}"[:_MAX_ERROR_CHARS]
        await record_dream_run(
            session_factory, "fail", dry=dry, duration_s=time.monotonic() - started, error=detail
        )
        print(f"sweep: FAIL — {detail}", file=sys.stderr)
        return 1

    print(render_report(result), flush=True)
    await record_dream_run(
        session_factory,
        "done",
        dry=dry,
        duration_s=time.monotonic() - started,
        error=None,
        # The 049 series, in BOTH modes. WET reports what it closed; DRY reports
        # what it would have — counted from the candidates the rehearsal actually
        # evaluated, since `BrainSessionSweepResult` zeroes its counters in dry
        # (`counted = [] if dry_run else candidates`) and that zero would read as
        # "nothing to close". Never added to abandonments, which are the event of
        # opposite meaning (ticket 24ca3b73).
        closed_inactive_count=(
            sum(
                1
                for candidate in result.candidates
                if candidate.outcome is BrainSessionStatus.CLOSED_INACTIVE
            )
            if dry
            else result.closed_inactive_count
        ),
    )
    return 0


def main() -> int:
    return asyncio.run(_run(build_parser().parse_args()))


if __name__ == "__main__":
    sys.exit(main())
