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


# ─── Dedup shadow verdict (W25 lot 1) ───────────────────────────────────────
#
# Raw pgvector cosine similarity computed IN-PROCESS for candidates[0] ONLY,
# against the top-3 same-project ADRs and top-3 same-project runbooks. No
# network call: the embeddings involved are already persisted. This is a
# SHADOW measure — see phase_promote.md — the model still decides; nothing
# here changes who gets promoted (W25-promote-nearest-tool-design.md §5).

_DEDUP_FAMILY_TABLES: dict[str, str] = {"adr": "adrs", "runbook": "runbooks"}

# Filters copied from consolidation.py's `_find_pairs` self-join (merged_into
# IS NULL, freshness_status <> 'archived', embedding IS NOT NULL): the same
# "is this row a live, embeddable duplicate candidate" gate two features now
# share. Table name is interpolated (not bound), but the only two values ever
# passed are the literals "adrs" / "runbooks" from `_DEDUP_FAMILY_TABLES`
# above — never external input.
_DEDUP_TOP3_TEMPLATE = """
    WITH src AS (
        SELECT embedding FROM learnings WHERE id = :learning_id
    )
    SELECT x.id, x.title,
           1 - (x.embedding <=> src.embedding) AS raw_cosine
    FROM {table} x, src
    WHERE x.project_key = :pk
      AND x.merged_into IS NULL
      AND x.freshness_status <> 'archived'
      AND x.embedding IS NOT NULL
    ORDER BY x.embedding <=> src.embedding
    LIMIT 3
"""

_DEDUP_NULL_COUNT_TEMPLATE = """
    SELECT count(*) FROM {table} x
    WHERE x.project_key = :pk
      AND x.merged_into IS NULL
      AND x.freshness_status <> 'archived'
      AND x.embedding IS NULL
"""

_SOURCE_HAS_EMBEDDING_SQL = sa.text(
    "SELECT embedding IS NOT NULL FROM learnings WHERE id = :learning_id"
)


def classify_band(raw_cosine: float | None, *, clear_below: float, block_above: float) -> str:
    """Three-way verdict from a single raw pgvector cosine.

    Bounds are per-family (Settings.promote_dedup_*), measured from the live
    corpus (W25-promote-nearest-tool-design.md §1.3): below `clear_below` no
    historical duplicate-proxy was ever observed that low; above
    `block_above` no historical non-duplicate was ever observed that high.
    Both bounds are themselves observed values on the OPPOSITE population, so
    the interval is closed on both ends: `raw_cosine == clear_below` and
    `raw_cosine == block_above` are "borderline", not "clear"/"block".

    `raw_cosine is None` means the family has no comparison row at all (e.g.
    a brand-new project with zero ADRs yet): nothing to be a duplicate of, so
    "clear".
    """
    if raw_cosine is None:
        return "clear"
    if raw_cosine < clear_below:
        return "clear"
    if raw_cosine > block_above:
        return "block"
    return "borderline"


async def _compute_family_dedup(
    session: AsyncSession,
    *,
    learning_id: str,
    project_key: str,
    family: str,
    clear_below: float,
    block_above: float,
) -> dict:
    table = _DEDUP_FAMILY_TABLES[family]
    null_excluded = (
        await session.execute(
            sa.text(_DEDUP_NULL_COUNT_TEMPLATE.format(table=table)), {"pk": project_key}
        )
    ).scalar_one()
    rows = (
        (
            await session.execute(
                sa.text(_DEDUP_TOP3_TEMPLATE.format(table=table)),
                {"learning_id": learning_id, "pk": project_key},
            )
        )
        .mappings()
        .all()
    )
    # Unrounded rows: `classify_band` MUST see the true value asyncpg
    # returned. `raw_cosine` here is a Python double computed by Postgres'
    # `1 - (a <=> b)` -- measured live at 16 significant digits
    # (0.6450062431625778) -- and the per-family bounds above are
    # themselves observed values at 3-decimal precision (closed interval,
    # see classify_band's docstring): rounding BEFORE classifying could
    # flip a value that truly sits just past a bound into the wrong band.
    top3_unrounded = [
        {"id": str(r["id"]), "title": r["title"], "raw_cosine": float(r["raw_cosine"])}
        for r in rows
    ]
    nearest_unrounded = top3_unrounded[0] if top3_unrounded else None
    band = classify_band(
        nearest_unrounded["raw_cosine"] if nearest_unrounded else None,
        clear_below=clear_below,
        block_above=block_above,
    )
    # Rounded for injection into the prompt: promote_validate's fabrication
    # cross-check asks the model to copy this number VERBATIM, and every one
    # of the 15 historical `cosine_observed` values the model has ever
    # reported carries 2 decimals -- 16 significant digits is not a number a
    # model can transcribe faithfully. 4 decimals keeps far more precision
    # than any model has ever reported while staying transcribable.
    top3 = [{**entry, "raw_cosine": round(entry["raw_cosine"], 4)} for entry in top3_unrounded]
    nearest = top3[0] if top3 else None
    return {
        "band": band,
        "nearest_id": nearest["id"] if nearest else None,
        "nearest_title": nearest["title"] if nearest else None,
        "nearest_raw_cosine": nearest["raw_cosine"] if nearest else None,
        "top3": top3,
        "null_embedding_excluded": int(null_excluded),
    }


async def compute_dedup(
    session: AsyncSession,
    learning_id: str,
    project_key: str,
    settings: Settings,
) -> dict:
    """Raw-cosine dedup verdict for ONE learning against both families.

    Pure in-process SQL against already-persisted embeddings — the same
    `1 - (a <=> b)` similarity expression `pg_base.search_vector` uses, never
    the ADR repository's inverted `distance = 1.0 - similarity`
    (test_promote_prepare_dedup_sign.py guards this). No embedding-service
    call: nothing here can raise EmbeddingUnavailable.
    """
    has_embedding = (
        await session.execute(_SOURCE_HAS_EMBEDDING_SQL, {"learning_id": learning_id})
    ).scalar_one_or_none()
    result: dict = {"score_kind": "raw_cosine_pgvector"}
    if not has_embedding:
        # Source learning itself has no embedding (15/3552 measured): nothing
        # can be computed for EITHER family. Distinct from "clear" — "clear"
        # means we looked and found nothing close; "unavailable" means we
        # could not look at all. Never fail open: phase_promote.md maps this
        # straight to target_type="dedup_unavailable".
        for family in _DEDUP_FAMILY_TABLES:
            result[family] = {
                "band": "unavailable",
                "nearest_id": None,
                "nearest_title": None,
                "nearest_raw_cosine": None,
                "top3": [],
                "null_embedding_excluded": 0,
            }
        return result

    result["adr"] = await _compute_family_dedup(
        session,
        learning_id=learning_id,
        project_key=project_key,
        family="adr",
        clear_below=settings.promote_dedup_borderline_low_adr,
        block_above=settings.promote_dedup_block_adr,
    )
    result["runbook"] = await _compute_family_dedup(
        session,
        learning_id=learning_id,
        project_key=project_key,
        family="runbook",
        clear_below=settings.promote_dedup_borderline_low_runbook,
        block_above=settings.promote_dedup_block_runbook,
    )
    return result


async def attach_dedup_verdict(
    session_factory: async_sessionmaker[AsyncSession],
    candidates: list[dict],
    project_key: str,
    settings: Settings,
) -> list[dict]:
    """Attach the dedup verdict to `candidates[0]` ONLY, in place.

    The top level MUST stay a JSON array: `dream.sh:949` runs `jq 'length'`
    on this script's stdout and compares it to 0 to pick the empty-pool path
    (`dream.sh:950-989`). Wrapping the payload in an object would return the
    key count instead of the pool size and silently break that branch — so
    this mutates `candidates[0]["dedup"]` and returns the SAME list, never
    re-shapes the top level. A no-op on an empty pool.
    """
    if not candidates:
        return candidates
    async with session_factory() as session:
        candidates[0]["dedup"] = await compute_dedup(
            session, candidates[0]["id"], project_key, settings
        )
    return candidates


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

    async def _build_pool() -> list[dict]:
        candidates = await fetch_candidates(session_factory, args.project_key, args.limit)
        return await attach_dedup_verdict(session_factory, candidates, args.project_key, settings)

    candidates = asyncio.run(_build_pool())
    json.dump(candidates, sys.stdout)
    sys.stdout.write("\n")


if __name__ == "__main__":
    main()
