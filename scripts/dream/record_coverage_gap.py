"""Pose la ligne `dream_runs` qui porte le verdict de couverture d'une nuit.

Ticket `0a9c067e`. Sa leçon centrale : une alerte que personne ne lit est
indiscernable d'une alerte absente. Le comparateur de couverture avait raison
trois nuits de suite et rien ne s'est passé, parce que sa sortie n'atteignait
que le fichier daté.

`post_run_alert` reste READ-ONLY — c'est un contrat épinglé par test. Le writer
vit donc ici, dans un module séparé appelé par `dream.sh` quand le verdict
escalade. La ligne qu'il pose atteint DEUX lecteurs existants sans une ligne de
code chez eux :

- `DreamRunService.last_failure` → « ### Last failure » du briefing de session ;
- `collect_nightly_ops` → `/metrics` `nightly.last_failure`.

Prix assumé et dit : étant la plus récente, elle prend la place « Last failure »
d'un échec de phase de la même nuit, et `/metrics` `last_run.status` passe à
`partial` ces nuits-là — ce qui est vrai.

Calque de `_promote_helpers._record_empty_pool`, avec ses deux propriétés :

- il n'élève JAMAIS — une erreur de télémétrie ne tue pas une nuit ;
- il RAPPORTE quand même, par son code retour. « Best-effort » n'est pas « rend
  toujours 0 » : c'est précisément le code retour de `record-empty-pool` qui
  rend observable une ligne `dream_runs` perdue. Son appelant l'encadre donc par
  `set +e`, `set -euo pipefail` étant actif dans `dream.sh`.

CLI:
    python -m scripts.dream.record_coverage_gap --date 2026-08-18 \\
        --summary "COVERAGE mode=manifest …" [--detail "COVERAGE_SILENT …"]

Exit codes:
    0  → ligne posée (ou mise à jour)
    1  → échec — un WARN est imprimé sur stderr, aucune exception ne remonte
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

# 8 caractères — `dream_runs.phase` est un `varchar(10)`, mesuré, et un test lit
# la longueur dans les métadonnées réelles plutôt que de recopier le nombre.
COVERAGE_PHASE = "coverage"

# `fail`, pas un statut neuf. `collector_dream` et `DreamRunService.last_failure`
# comptent tout `!= 'done'` comme un échec, et `codex_dream_run_v1` projette
# `status` vers `codex_ro` : inventer une valeur serait un changement de contrat
# externe pour ne rien gagner. Ici l'échec est réel, il n'y a rien à adoucir.
COVERAGE_STATUS = "fail"

# `error_message` est du `text` non borné — mesuré. On borne quand même : un
# rapport illisible n'est pas lu, ce qui est le défaut d'origine du ticket.
MAX_ERROR_MESSAGE_CHARS = 4000

EMPTY_VERDICT_MESSAGE = (
    "dream_runs coverage gap reported by scripts.dream.post_run_alert "
    "(no machine line captured — see the dated log)"
)


def _build_factory(postgres_url: str) -> async_sessionmaker[AsyncSession]:
    engine = create_async_engine(postgres_url, pool_pre_ping=True)
    return async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)


def build_error_message(summary: str, detail: str | None) -> str:
    """La ligne machine, puis les paires fautives. Jamais vide, jamais démesuré."""
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
    """Écrit UNE ligne `coverage` pour la nuit, idempotente par `run_date`.

    Un rejeu manuel du matin met la ligne à jour au lieu d'en empiler une
    seconde : deux verdicts contradictoires pour la même nuit vaudraient moins
    que pas de verdict du tout.

    `project_key` porte la sentinelle des phases globales : `coverage` juge la
    nuit entière, pas un projet. Elle ne transite JAMAIS par
    `canonicalize_project_key`, dont le motif la rejette.
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
    except Exception as exc:  # noqa: BLE001 — n'élève jamais ; le rc porte l'échec
        print(f"WARN record-coverage-gap failed: {exc!r}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
