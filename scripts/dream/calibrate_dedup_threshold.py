#!/usr/bin/env python3
"""Calibrate the PROMOTE-phase dedup cosine threshold.

Computes pairwise cosine similarities across the ADR + Runbook corpus
(all projects) and reports percentile statistics for two strata:

  - cross-project pairs : proxy for the NULL distribution (definitely
                          unrelated by construction; entities from
                          different project_keys rarely overlap
                          semantically).
  - same-project pairs  : may contain true near-duplicates and is shown
                          for context only.

Decision rule (Batch 9 step 1):
  - If 0.85 >= p95(cross-project) → current threshold is safe
    (false-positive rate < 5% on unrelated pairs).
  - Else → recommend min(0.95, p99(cross-project) + 0.02).

The 0.85 threshold is hard-coded in scripts/dream/phase_promote.md
and used by the PROMOTE phase to decide whether a mature insight is
a near-duplicate of an existing ADR / Runbook (and should be skipped
rather than graduated).

Usage:
    python -m scripts.dream.calibrate_dedup_threshold

Refs:
  - NVIDIA NeMo SemDedup (in-cluster pairwise cosine threshold)
  - Milvus / OpenAI threshold tuning patterns (distribution-driven)
"""

from __future__ import annotations

import asyncio
import json
import sys

import numpy as np
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from brain_v42.config import Settings

_CURRENT_THRESHOLD = 0.85
_NULL_FP_TARGET = 0.05  # we tolerate at most 5% false positives on null pairs


_FETCH_SQL = sa.text(
    """
    SELECT id, project_key, embedding
    FROM adrs
    WHERE embedding IS NOT NULL
    UNION ALL
    SELECT id, project_key, embedding
    FROM runbooks
    WHERE embedding IS NOT NULL
    """
)


def _l2_normalize(matrix: np.ndarray) -> np.ndarray:
    norms = np.linalg.norm(matrix, axis=1, keepdims=True)
    norms[norms == 0] = 1.0
    return matrix / norms


def _percentiles(values: np.ndarray) -> dict[str, float]:
    if values.size == 0:
        return {p: float("nan") for p in ("p50", "p90", "p95", "p99")}
    return {
        "p50": float(np.percentile(values, 50)),
        "p90": float(np.percentile(values, 90)),
        "p95": float(np.percentile(values, 95)),
        "p99": float(np.percentile(values, 99)),
    }


async def _fetch_embeddings() -> tuple[np.ndarray, np.ndarray]:
    settings = Settings()
    engine = create_async_engine(settings.postgres_url)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with factory() as session:
            rows = (await session.execute(_FETCH_SQL)).all()
    finally:
        await engine.dispose()

    # pgvector returns embeddings as JSON-formatted strings via raw SQL.
    parsed = [_parse_embedding(r.embedding) for r in rows]
    embeddings = np.array(parsed, dtype=np.float32)
    project_keys = np.array([r.project_key or "" for r in rows], dtype=object)
    return embeddings, project_keys


def _parse_embedding(raw: str | list[float]) -> list[float]:
    if isinstance(raw, str):
        return json.loads(raw)
    return list(raw)


def _split_pairwise_cosines(
    matrix: np.ndarray, projects: np.ndarray
) -> tuple[np.ndarray, np.ndarray]:
    """Compute upper-triangular pairwise cosines, split by cross/same project."""
    sim = matrix @ matrix.T  # already L2-normalized → cosine
    n = sim.shape[0]
    iu = np.triu_indices(n, k=1)
    cosines = sim[iu]
    same = projects[iu[0]] == projects[iu[1]]
    return cosines[~same], cosines[same]


def _format_row(label: str, stats: dict[str, float], n: int) -> str:
    return (
        f"{label:<22} n={n:>7,d}  "
        f"p50={stats['p50']:.3f}  p90={stats['p90']:.3f}  "
        f"p95={stats['p95']:.3f}  p99={stats['p99']:.3f}"
    )


def _verdict(cross_stats: dict[str, float]) -> tuple[str, float | None]:
    if np.isnan(cross_stats["p95"]):
        return ("UNKNOWN — no cross-project pairs (single-project corpus)", None)
    p95 = cross_stats["p95"]
    p99 = cross_stats["p99"]
    if _CURRENT_THRESHOLD >= p95:
        margin = _CURRENT_THRESHOLD - p95
        return (
            f"SAFE — 0.85 ≥ p95 of unrelated pairs ({p95:.3f}); "
            f"margin = {margin:+.3f}; expected FP-rate ≤ {_NULL_FP_TARGET:.0%}",
            None,
        )
    suggested = min(0.95, round(p99 + 0.02, 2))
    return (
        f"TOO LOW — p95 of unrelated pairs ({p95:.3f}) > 0.85. "
        f"Recommend raising to {suggested:.2f} (= min(0.95, p99 + 0.02))",
        suggested,
    )


async def main() -> int:
    print("Fetching ADR + Runbook embeddings (all projects)…", file=sys.stderr)
    embeddings, projects = await _fetch_embeddings()
    n = len(embeddings)
    if n < 2:
        print(f"✗ Only {n} embedded entities — need ≥ 2 to form pairs.", file=sys.stderr)
        return 2

    print(f"  → {n} entities across {len(set(projects))} project(s)", file=sys.stderr)
    matrix = _l2_normalize(embeddings)
    cross, same = _split_pairwise_cosines(matrix, projects)

    cross_stats = _percentiles(cross)
    same_stats = _percentiles(same)

    print()
    print("Pairwise cosine percentiles")
    print("─" * 78)
    print(_format_row("cross-project (NULL)", cross_stats, cross.size))
    print(_format_row("same-project (mixed)", same_stats, same.size))
    print("─" * 78)
    print()

    verdict, suggested = _verdict(cross_stats)
    print(f"Current threshold (phase_promote.md): {_CURRENT_THRESHOLD:.2f}")
    print(f"Verdict: {verdict}")
    if suggested is not None:
        print(
            f"Action: edit scripts/dream/phase_promote.md and replace 0.85 with "
            f"{suggested:.2f} before the rollout in Batch 9."
        )
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
