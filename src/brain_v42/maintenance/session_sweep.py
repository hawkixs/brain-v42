"""Phase Dream `sweep` — tarir les sessions ouvertes sans signe de vie.

Spec : docs/superpowers/specs/2026-08-07-session-lifecycle-sweep-design.md

Déterministe et sans modèle : aucun appel LLM, aucun réseau. La row
``dream_runs`` porte donc ``model = NULL`` — forme déjà admise, observée sur
``extract`` et sur le run ``roadmap`` du 2026-08-05.

Livré DRY : ``--wet`` est le seul chemin qui écrit.

Usage:
    python -m brain_v42.maintenance.session_sweep           # dry (défaut)
    python -m brain_v42.maintenance.session_sweep --wet     # applique
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
from brain_v42.models.brain_session import AUTO_STALE_AFTER, BrainSessionSweepResult

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
        # Défaut LU de la constante, jamais recopié : deux exemplaires d'un
        # même seuil, c'est le défaut de classe du learning 8dc7e042.
        default=AUTO_STALE_AFTER.days,
        help=f"seuil en jours (défaut : {AUTO_STALE_AFTER.days}, depuis AUTO_STALE_AFTER)",
    )
    return parser


def render_report(result: BrainSessionSweepResult) -> str:
    """Rapport texte du balayage, pour le log daté de la nuit."""
    mode = "DRY" if result.dry_run else "WET"
    cutoff = result.cutoff.isoformat(timespec="seconds")
    count = len(result.candidates)
    if count == 0:
        return f"sweep [{mode}] cutoff={cutoff} — aucune session à abandonner"

    verb = "auraient été abandonnées" if result.dry_run else "ont été abandonnées"
    lines = [f"sweep [{mode}] cutoff={cutoff} — {count} sessions {verb}"]
    lines.extend(
        f"  {candidate.project_key:<16} {candidate.client_key:<40} "
        f"{candidate.last_heartbeat_at.isoformat(timespec='seconds')}"
        for candidate in result.candidates
    )
    return "\n".join(lines)


async def record_dream_run(
    session_factory: Any,
    status: str,
    dry: bool,
    duration_s: float,
    error: str | None,
) -> None:
    """INSERT dream_runs pour phase='sweep'. Best-effort — ne lève jamais.

    `model` reste NULL : la phase n'appelle aucun modèle. `project_key` reçoit
    la sentinelle : `sweep` est une phase GLOBALE, elle sort de la boucle et
    balaie les sessions de tous les projets d'un coup. La sentinelle entre par
    un paramètre lié, jamais par un flag — `test_dream_sh_sweep.py` épingle
    `sweep_args` à `["--wet"]` et refuse tout argument de plus.
    """
    try:
        async with session_factory() as session:
            async with session.begin():
                await session.execute(
                    sa.text(
                        "INSERT INTO dream_runs "
                        "(run_date, phase, status, duration_s, error_message, "
                        "project_key, phase_dry_run, model) "
                        "VALUES (:run_date, 'sweep', :status, :duration_s, "
                        ":error_message, :project_key, :phase_dry_run, NULL)"
                    ),
                    {
                        "run_date": date.today(),
                        "status": status,
                        "duration_s": duration_s,
                        "error_message": error,
                        "project_key": GLOBAL_PHASE_PROJECT_KEY,
                        "phase_dry_run": dry,
                    },
                )
    except Exception as exc:  # noqa: BLE001 — la trace ne doit jamais tuer la phase
        print(f"! warning: could not record dream_run: {exc}", file=sys.stderr)


async def _run(args: argparse.Namespace) -> int:
    from pydantic import ValidationError  # noqa: PLC0415

    from brain_v42.config import Settings  # noqa: PLC0415
    from brain_v42.db.engine import get_session_factory  # noqa: PLC0415
    from brain_v42.repositories.pg_brain_session import PgBrainSessionRepo  # noqa: PLC0415

    try:
        Settings()  # type: ignore[call-arg]
    except ValidationError as exc:
        print(f"Config invalide: {exc}", file=sys.stderr)
        return 2

    session_factory = get_session_factory()
    dry = not args.wet
    started = time.monotonic()
    try:
        result = await PgBrainSessionRepo(session_factory).abandon_stale(
            older_than=timedelta(days=args.older_than_days),
            # Dérivé du seuil RÉELLEMENT utilisé, jamais laissé au défaut de
            # abandon_stale : au seuil par défaut ça reproduit exactement la
            # constante AUTO_STALE_ABANDONMENT_REASON (épinglé par
            # test_default_threshold_reason_matches_the_module_constant) ;
            # à tout autre seuil, la constante mentirait sur ce qui a été
            # réellement mesuré (finding de revue de la Task 1, adjudiqué).
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
        session_factory, "done", dry=dry, duration_s=time.monotonic() - started, error=None
    )
    return 0


def main() -> int:
    return asyncio.run(_run(build_parser().parse_args()))


if __name__ == "__main__":
    sys.exit(main())
