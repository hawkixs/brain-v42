#!/usr/bin/env python3
"""Build the PROMOTE-phase candidate pool and emit top-N as JSON.

Invoked by dream.sh before the LLM call. dream.sh captures the JSON array
from stdout and injects it into the PROMOTE prompt (spec §3.3 step 1).

Usage:
    python -m scripts.dream.promote_prepare --project-key brain-v42 --limit 10

Exits 0 with a JSON array on stdout. Empty pool = empty array `[]`.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from brain_v42.config import Settings

_CANDIDATE_SQL = sa.text(
    """
    SELECT l.id, l.topic, l.insight AS content, l.tags, l.metadata,
           l.confidence, l.access_count, l.access_count_human, l.created_at
    FROM learnings l
    WHERE (NOW() - l.created_at) >= INTERVAL '7 days'
      AND l.access_count_human >= 3
      AND NOT (l.confidence = 'low' AND l.access_count < 5)
      AND ('dream:generated' != ALL(l.tags)
           OR l.validated_at IS NOT NULL
           OR l.confidence != 'low')
      -- Spec C insulation: learnings emitted by the cross-project resonance
      -- script tag themselves EXCLUDE_FROM_PROMOTE; promoting them would
      -- close a feedback loop (resonance → ADR → resonance).
      AND 'EXCLUDE_FROM_PROMOTE' != ALL(l.tags)
      AND l.project_key = :pk
      AND NOT EXISTS (
          SELECT 1 FROM dream_promotions p
          WHERE p.source_learning_id = l.id
            AND (
                (p.target_type = 'adr' AND p.target_adr_id IS NOT NULL)
                OR (p.target_type = 'runbook' AND p.target_runbook_id IS NOT NULL)
                OR p.target_type = 'skipped_dedup'
            )
      )
      -- Terminal-unpromotable cache: skip a learning already judged
      -- classification_uncertain on its CURRENT version. La comparaison porte
      -- sur content_updated_at, PAS sur updated_at : ce dernier bouge à chaque
      -- écriture de compteur, donc une simple lecture par une phase ultérieure
      -- du dream invalidait le verdict rendu deux minutes plus tôt (observé :
      -- un learning réévalué 23 nuits d'affilée). Le repli sur created_at est
      -- délibéré — sans backfill, content_updated_at est NULL, et se replier
      -- sur updated_at reproduirait le défaut à l'identique.
      AND NOT EXISTS (
          SELECT 1 FROM dream_promotions u
          WHERE u.source_learning_id = l.id
            AND u.target_type = 'classification_uncertain'
            AND u.created_at >= COALESCE(l.content_updated_at, l.created_at)
      )
    -- Rank on the counter the maturity gate admits on. `access_count` is
    -- inflated by the dream's own reads, so ordering on it lets the phase
    -- choose its own winner — and the prompt evaluates candidates[0] ONLY.
    -- The tie-break stays `created_at`: human counters are small integers, so
    -- ties are frequent and the secondary key decides the evaluated slot often;
    -- putting `access_count` back there would hand it straight back to agent
    -- traffic. Exact `created_at` ties are left unordered — production rows
    -- carry microsecond timestamps from distinct INSERTs.
    ORDER BY l.access_count_human DESC, l.created_at DESC
    LIMIT :lim
    """
)


async def fetch_candidates(
    session_factory: async_sessionmaker[AsyncSession],
    project_key: str,
    limit: int = 10,
) -> list[dict]:
    """Execute the maturity + dedup filter and return top-N candidates.

    Filter (spec §3.3 step 1, ADR #4 second-gate):
      - age(NOW(), created_at) >= 7 days
      - access_count_human >= 3
      - NOT (confidence='low' AND access_count < 5)
      - dream:generated rows additionally require validated_at IS NOT NULL
        OR confidence != 'low' (ADR #4 — block echo-drift auto-promotion)
      - project_key = <arg>
      - NOT EXISTS live ADR/runbook promotion OR skipped_dedup entry

    Ranked by access_count_human DESC, then created_at DESC.

    The payload carries BOTH counters: `access_count_human` is the evidence the
    maturity gate and the ranking act on, `access_count` is the total the
    low-confidence guard still reads. The judge sees the pair and can tell them
    apart — see the legend in phase_promote.md.
    """
    async with session_factory() as session:
        result = await session.execute(_CANDIDATE_SQL, {"pk": project_key, "lim": limit})
        rows = result.mappings().all()
        return [
            {
                "id": str(r["id"]),
                "topic": r["topic"],
                "content": r["content"],
                "tags": list(r["tags"] or []),
                "metadata": dict(r["metadata"] or {}),
                "confidence": r["confidence"],
                "access_count": r["access_count"],
                "access_count_human": r["access_count_human"],
                "created_at": r["created_at"].isoformat(),
            }
            for r in rows
        ]


def _build_factory(postgres_url: str) -> async_sessionmaker[AsyncSession]:
    engine = create_async_engine(postgres_url, pool_pre_ping=True)
    return async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project-key", required=True)
    parser.add_argument("--limit", type=int, default=10)
    args = parser.parse_args(argv)

    settings = Settings()
    session_factory = _build_factory(settings.postgres_url)

    candidates = asyncio.run(fetch_candidates(session_factory, args.project_key, args.limit))
    json.dump(candidates, sys.stdout)
    sys.stdout.write("\n")


if __name__ == "__main__":
    main()
