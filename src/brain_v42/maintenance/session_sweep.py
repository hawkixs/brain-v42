"""Phase Dream `sweep` — tarir les sessions ouvertes sans signe de vie.

Spec : docs/superpowers/specs/2026-08-07-session-lifecycle-sweep-design.md
M-G : docs/design/refonte-projets-sessions/SPEC-M-G.md §4 (seuil d'éligibilité).

Déterministe et sans modèle : aucun appel LLM, aucun réseau. La row
``dream_runs`` porte donc ``model = NULL`` — forme déjà admise, observée sur
``extract`` et sur le run ``roadmap`` du 2026-08-05.

**DEUX règles, UN statement, deux compteurs.** La règle 7 j abandonne les
sessions sans heartbeat ; la règle 4 h ferme les traçantes ``agent`` inobservées
en ``closed_inactive``. Les deux issues sont comptées SÉPARÉMENT : ``abandoned``
porte une raison et jamais de ledger, ``closed_inactive`` porte son ledger et
aucune raison — les additionner effacerait la distinction que la 046 a coûté une
migration à créer.

Livré DRY : ``--wet`` est le seul chemin qui écrit. Et la règle 4 h est livrée
FERMÉE par-dessus, derrière ``BRAIN_SESSION_INACTIVE_SWEEP_ENABLED`` : cette
phase tourne WET toutes les nuits depuis le dépôt, donc merger la règle sans
drapeau l'armerait dès la nuit suivante.

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
        # Défaut LU de la constante, jamais recopié : deux exemplaires d'un
        # même seuil, c'est le défaut de classe du learning 8dc7e042.
        default=AUTO_STALE_AFTER.days,
        help=f"seuil en jours (défaut : {AUTO_STALE_AFTER.days}, depuis AUTO_STALE_AFTER)",
    )
    return parser


def render_report(result: BrainSessionSweepResult) -> str:
    """Rapport texte du balayage, pour le log daté de la nuit.

    Les deux issues sont nommées SÉPARÉMENT sur l'en-tête et sur chaque ligne.
    Un journal qui dirait « 17 sessions traitées » laisserait un lecteur pressé
    conclure « 17 abandons » — et l'écart entre les deux règles est précisément
    ce qu'on surveille pendant la fenêtre d'observation.

    ``inactive_cutoff=off`` dit que la règle 4 h est FERMÉE. Ne pas l'écrire
    laisserait lire une nuit à zéro fermeture comme « aucune traçante inactive »,
    alors que la règle n'a même pas été évaluée — un plafond silencieux.
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
    """Compter par issue, y compris en DRY.

    Les compteurs du RÉSULTAT restent à zéro en DRY — c'est leur contrat, pour
    qu'aucun journal ne lise « 17 fermées » là où rien n'a été écrit. Le rapport
    a besoin de l'autre chiffre, celui du dénombrement : il le dérive ici, sous
    un verbe au conditionnel.
    """
    tally = dict.fromkeys((BrainSessionStatus.ABANDONED, BrainSessionStatus.CLOSED_INACTIVE), 0)
    for candidate in result.candidates:
        tally[candidate.outcome] += 1
    return tally


def _stamp(value: Any) -> str:
    """``NULL`` se lit « jamais observée », jamais « observée il y a longtemps »."""
    return "never" if value is None else value.isoformat(timespec="seconds")


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
        settings = Settings()  # type: ignore[call-arg]
    except ValidationError as exc:
        print(f"Config invalide: {exc}", file=sys.stderr)
        return 2

    # `None` ferme la règle des 4 h ET le dit au rapport. Le drapeau est lu ICI,
    # une fois, et jamais dans le dépôt : le dépôt reçoit un seuil ou rien, donc
    # un test peut prouver la règle sans monter de configuration, et la décision
    # d'armement reste à un seul endroit.
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
            # Dérivé du seuil RÉELLEMENT utilisé, jamais laissé au défaut de
            # sweep_open_sessions : au seuil par défaut ça reproduit exactement
            # la constante AUTO_STALE_ABANDONMENT_REASON (épinglé par
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
