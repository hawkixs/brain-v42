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
    """Bâtir la requête du signal de mutation d'origine non-dream.

    Deux exclusions par rapport à la version d'origine :

    - `content_updated_at` remplace `updated_at`, qui bougeait à chaque
      écriture de compteur — cause principale des 48 RUN pour 2 SKIP mesurés
      sur 50 nuits ;
    - les entités taguées `dream:generated` sortent du signal, sinon SYNTH
      garantit en créant ses insights que la nuit suivante synthétisera
      par-dessus sa propre production.

    `tags` est NOT NULL DEFAULT '{}' sur les cinq tables (vérifié le
    2026-08-06, zéro NULL en base), donc `ANY(tags)` ne peut pas rendre NULL
    et faire disparaître une ligne du signal. Si cette contrainte tombait un
    jour, il faudrait un COALESCE : un prédicat NULL exclurait la ligne et
    produirait un SKIP à tort, ce que ce module promet impossible.

    `greatest()` porte ici la MÊME charge, et c'est moins visible.
    `content_updated_at` est livré par la 041 sans backfill : mesuré le
    2026-08-08, il vaut NULL sur 2739 learnings sur 2740. Le signal ne tient
    donc que parce que `GREATEST` d'ANSI/PostgreSQL **ignore les NULL** et
    retombe sur `created_at` — vérifié en base, pas supposé. C'est une
    sémantique propre à PostgreSQL : MySQL et Oracle rendent NULL dès qu'un
    argument est NULL, ce qui annulerait le `max()` de chaque table et
    produirait un SKIP permanent. Aucun test ne couvre ce comportement — le
    seul test du module compte des occurrences de chaîne dans le SQL. Ne pas
    remplacer `greatest` par une expression « équivalente » sans mesurer son
    comportement sur NULL.

    Angle mort accepté : l'exclusion se fait par tag d'entité, pas par
    acteur d'écriture, donc une édition humaine du contenu d'une entité déjà
    taguée `dream:generated` reste invisible au signal de mutation — pire cas,
    une nuit SYNTH différée.
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

    # La résolution est DANS le try : elle lève quand rien ne configure
    # POSTGRES_URL, et cette porte doit rester fail-open — une erreur de
    # configuration ne doit jamais faire sauter les phases Opus par erreur. Le
    # littéral `brain:brain` qui vivait ici a survécu à la rotation du mot de
    # passe en imprimant `RUN (preflight error: …)` nuit après nuit : correct
    # pour cette porte, mais aucune preuve que la configuration allait bien.
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
