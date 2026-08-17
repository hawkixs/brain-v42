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

# Statut de la row « pool vide ». `done` n'est pas un choix cosmétique : c'est
# le SEUL statut non-échec du système. post_run_alert exclut
# {fail, partial, timeout}, mais collector_dream et
# DreamRunService.last_failure comptent tout `!= 'done'` comme un échec. Un
# statut « neutre » inventé (skipped, noop) déplacerait la fausse alarme de
# l'alerte vers le briefing au lieu de l'éteindre.
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
    """SELECT de la ligne `promote` d'un projet pour une date.

    Le filtre de projet n'est pas cosmétique. L'appelant, `promote_validate`,
    ÉCRIT sur la ligne rendue : il la marque `partial` et backfille
    `dream_promotions.dream_run_id`. Sans le filtre, à plusieurs projets, la
    requête rend la dernière ligne `promote` de la journée, quel que soit le
    projet — et l'attribution fausse n'est plus récupérable depuis les lignes
    une fois écrite (spec §12).

    Dans la boucle séquentielle actuelle, l'absence de filtre donnait
    fortuitement la bonne ligne : chaque projet écrit puis relit aussitôt. Mais
    la correction reposait alors sur « personne n'écrit entre mon écriture et
    ma lecture », un invariant que rien n'impose et que la boucle ne déclare
    pas.

    `ORDER BY id DESC LIMIT 1` reste : un projet peut avoir deux lignes le même
    jour après un re-run manuel, et c'est la dernière qui compte.
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
    """INSERT la row dream_runs d'une nuit à pool de candidats vide.

    Sans elle, la phase promote — attendue tant que son killswitch est ouvert —
    est ABSENTE de dream_runs, et l'alerte fabrique un `partial` de synthèse
    chaque nuit. Depuis la migration 041 (filtre de maturité sur
    `access_count_human`, sans backfill) le pool est légitimement vide : cette
    fausse alarme pousserait l'opérateur à défaire la 041.

    La bonne réponse est de rendre la phase OBSERVÉE, jamais de la retirer des
    phases attendues : un promote qui CRASHE n'écrit toujours aucune row et
    déclenche encore l'alerte.

    `model` reste NULL — aucun modèle n'a été appelé. `phase_dry_run` reste
    faux : rien n'a tourné, donc la nuit n'est pas une répétition à blanc
    propre et ne doit pas nourrir `_clean_dry_streak`.

    `project_key` est REQUIS et sans défaut. `promote` est une phase PAR
    PROJET, donc la vraie clé, jamais la sentinelle des phases globales — et
    c'est ce site que l'inventaire de la spec §14.2 avait oublié tout en
    rangeant ce fichier parmi les lecteurs. Depuis le filtre de maturité de la
    041, le pool est légitimement vide et c'est ce chemin-là qui écrit la ligne
    `promote` la plupart des nuits : le laisser à NULL ferait mentir la
    sémantique « NULL = écrit avant la 042 », sans que rien ne le signale.
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
            # Ne jamais avaler : sans row, l'alerte de synthèse revient. Bruyant
            # mais observable — c'est le sens de la marche.
            print(f"record-empty-pool failed: {exc!r}", file=sys.stderr)
            return 1
        return 0

    return 2


if __name__ == "__main__":
    sys.exit(main())
