#!/usr/bin/env python3
"""Retrieval quality of the CONFIGURABLE backends, plus the degraded mode.

Why a second runner rather than an extension of `bench/embedding_v1`. The v1
bench answered "which model do we buy a GPU for": it restarts containers on
dev-pc, samples VRAM over SSH and ranks 915 queries against a 305-entity pool.
Two of its three legs do not apply to a hosted provider (no container, no
VRAM), and a 305-document pool is the wrong question now — this runner ranks
against the WHOLE corpus, because recall@5 on 305 documents is mechanically
easy and would flatter both candidates equally while hiding the difference
that matters.

Three candidate kinds, all ranked over the same pool with the same metrics
(imported from v1, so a number here is comparable to a number there once the
pool size is stated):

``shim``    the private three-route contract — today's production endpoint
``openai``  POST /v1/embeddings — Mistral, Voyage, Ollama, vLLM, TEI, …
``fts``     no embedding at all: PostgreSQL `search_vector`, which is what
            `brain_search` actually falls back to when the endpoint is down

The `fts` leg is the point of this runner. Whether the degraded mode is good
enough decides whether a hosted provider on the hot path is acceptable at all,
and it costs nothing to measure — no GPU, no API key, no provider.

Two measured facts shape the reading, both dated 2026-09-21:

* `brain_search` fans out to SIX types only — decision, learning, snippet,
  runbook, adr, plan. `feature` rows carry an embedding but no search path
  ever reaches them, so their gold queries are reported and excluded from the
  headline.
* `features` is the one pooled table with NO `search_vector` column. The
  lexical fallback cannot return a feature by construction, not by accident.

Usage:
    python bench/embedding_v2/run_retrieval_bench.py --candidate fts
    python bench/embedding_v2/run_retrieval_bench.py --candidate shim --candidate fts
    BRAIN_BENCH_API_KEY_FILE=~/.config/brain-v42/mistral.key \\
      python bench/embedding_v2/run_retrieval_bench.py \\
      --candidate openai --base-url https://api.mistral.ai \\
      --model codestral-embed-2505
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import statistics
import sys
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

import asyncpg
import numpy as np

BENCH_V2 = Path(__file__).resolve().parent
BENCH_V1 = BENCH_V2.parent / "embedding_v1"
REPO_ROOT = BENCH_V2.parents[1]

sys.path.insert(0, str(BENCH_V1))
sys.path.insert(0, str(REPO_ROOT / "src"))

from run_bench import compute_metrics, load_queries  # noqa: E402

from brain_v42.config import Settings  # noqa: E402
from brain_v42.services.embedding_factory import (  # noqa: E402
    build_embedding_service,
    settings_for_standalone_script,
)
from brain_v42.services.gpu_embedding_service import EmbeddingUnavailable  # noqa: E402

GOLD_PATH = BENCH_V1 / "gold_v1.jsonl"
DEFAULT_OUTPUT = BENCH_V2 / "results_v2.json"

#: Types `brain_service.fan_out` actually dispatches to (measured 2026-09-21,
#: `brain_service.py:125`). `feature` is pooled as a distractor but excluded
#: from the headline: no production search path retrieves one.
SERVED_TYPES = ("decision", "learning", "snippet", "runbook", "adr", "plan")

#: The full pool, same columns and same text composition as `gen_gold.QUERIES`
#: so a gold id always resolves, minus the stratified LIMIT.
POOL_SQL: dict[str, str] = {
    "learning": "SELECT id::text AS id, topic AS title, insight AS body "
    "FROM learnings WHERE merged_into IS NULL",
    "feature": "SELECT id::text AS id, name AS title, description AS body FROM features",
    "plan_chunk": "SELECT id::text AS id, section_title AS title, content AS body "
    "FROM indexed_plan_chunks",
    "decision": "SELECT id::text AS id, title, COALESCE(reasoning, description) AS body "
    "FROM decisions WHERE merged_into IS NULL AND superseded_by IS NULL",
    "snippet": "SELECT id::text AS id, title, intention AS body "
    "FROM snippets WHERE merged_into IS NULL",
    "plan": "SELECT id::text AS id, title, COALESCE(summary, substr(content,1,500)) AS body "
    "FROM indexed_plans",
    "runbook": "SELECT id::text AS id, title, description AS body "
    "FROM runbooks WHERE merged_into IS NULL",
    "adr": "SELECT id::text AS id, title, COALESCE(context, decision) AS body "
    "FROM adrs WHERE merged_into IS NULL",
}

#: Tables carrying `search_vector` (measured 2026-09-21). `features` is absent
#: on purpose — the lexical fallback cannot reach a feature at all.
FTS_TABLES: dict[str, str] = {
    "learning": "learnings",
    "decision": "decisions",
    "snippet": "snippets",
    "runbook": "runbooks",
    "adr": "adrs",
    "plan": "indexed_plans",
    "plan_chunk": "indexed_plan_chunks",
}

#: v1 truncated corpus text at 2000 chars so every backend stayed inside its
#: context. Kept identical: changing it would make the two reports
#: incomparable for no gain.
TEXT_CHARS = 2000

#: Rank beyond which a hit counts as a miss, as in v1.
TOP_K = 50

#: USD per million input tokens, for the cost column. Tokens are estimated at
#: 4 chars each — an order of magnitude, never an invoice.
PRICE_PER_MTOK = {"codestral-embed-2505": 0.15, "mistral-embed": 0.10}


@dataclass
class CandidateSpec:
    name: str
    kind: str  # "shim" | "openai" | "fts"
    base_url: str = ""
    model: str = ""


@dataclass
class CandidateResult:
    name: str
    kind: str
    model: str = ""
    ok: bool = True
    failure: str = ""
    pool_size: int = 0
    scored_queries: int = 0
    latency_p50_ms: float = 0.0
    latency_p95_ms: float = 0.0
    corpus_seconds: float = 0.0
    estimated_tokens: int = 0
    estimated_usd: float = 0.0
    headline: dict[str, float] = field(default_factory=dict)
    all_types: dict[str, float] = field(default_factory=dict)
    per_type: dict[str, dict[str, float]] = field(default_factory=dict)
    per_variant: dict[str, dict[str, float]] = field(default_factory=dict)
    #: One row per scored query. Persisted so a breakdown nobody thought of
    #: today is a re-analysis of this file rather than another full re-embed
    #: of the corpus -- which costs ten minutes on the GPU and real money on a
    #: hosted provider.
    ranks: list[dict[str, object]] = field(default_factory=list)


# ──────────────────────────── corpus + gold ──────────────────────────────


async def load_pool(conn: asyncpg.Connection) -> list[tuple[str, str, str]]:
    """Every embeddable row, as (type, id, text). The ranking pool."""
    pool: list[tuple[str, str, str]] = []
    for etype, sql in POOL_SQL.items():
        for row in await conn.fetch(sql):
            title = (row["title"] or "").strip()
            body = (row["body"] or "").strip()
            pool.append((etype, row["id"], (title + "\n\n" + body)[:TEXT_CHARS]))
    return pool


def resolve_gold(queries: list[dict], pool_ids: set[str]) -> tuple[list[dict], int]:
    """Drop gold rows whose entity no longer exists.

    The gold was minted 2026-04-12 against a 305-entity sample; entities have
    been merged, superseded and deleted since. A query whose target is gone is
    unanswerable and would count as a miss for every candidate equally —
    quietly depressing all scores rather than discriminating between them.
    """
    kept = [q for q in queries if q["gold_id"] in pool_ids]
    return kept, len(queries) - len(kept)


# ──────────────────────────── vector legs ────────────────────────────────


def build_service(spec: CandidateSpec, base_settings: Settings) -> object:
    """A client for one candidate, through the production factory.

    Going through the factory rather than raw HTTP is deliberate: the retry,
    the 503 `gpu_busy` handling and the `EmbeddingUnavailable` contract are
    what production experiences, so a candidate that only looks good without
    them has not been measured under the conditions it would run in.
    """
    overrides: dict[str, object] = {"embedding_backend": spec.kind}
    if spec.base_url:
        overrides["embedding_service_url"] = spec.base_url
    if spec.model:
        overrides["embedding_model"] = spec.model
    key_file = os.environ.get("BRAIN_BENCH_API_KEY_FILE", "")
    if key_file and spec.kind == "openai":
        overrides["embedding_api_key"] = Path(key_file).expanduser().read_text().strip()
    return build_embedding_service(base_settings.model_copy(update=overrides))


async def embed_pool(
    svc: object, texts: list[str], batch: int, result: CandidateResult
) -> np.ndarray | None:
    started = time.monotonic()
    vectors: list[list[float]] = []
    for index in range(0, len(texts), batch):
        chunk = texts[index : index + batch]
        try:
            vectors.extend(await svc.embed_texts(chunk))  # type: ignore[attr-defined]
        except EmbeddingUnavailable as exc:
            result.ok = False
            result.failure = f"corpus embedding failed at {index}: {exc}"
            return None
        if index and index % (batch * 50) == 0:
            print(f"    {index}/{len(texts)} pooled", flush=True)
    result.corpus_seconds = time.monotonic() - started
    return _normalise(np.asarray(vectors, dtype=np.float32))


def _normalise(matrix: np.ndarray) -> np.ndarray:
    norms = np.linalg.norm(matrix, axis=1, keepdims=True)
    norms[norms == 0.0] = 1.0
    return matrix / norms


async def run_vector_candidate(
    spec: CandidateSpec,
    pool: list[tuple[str, str, str]],
    queries: list[dict],
    settings: Settings,
    batch: int,
) -> CandidateResult:
    result = CandidateResult(name=spec.name, kind=spec.kind, model=spec.model)
    result.pool_size = len(pool)
    svc = build_service(spec, settings)

    print(f"  embedding pool ({len(pool)} rows) …", flush=True)
    pool_matrix = await embed_pool(svc, [text for _, _, text in pool], batch, result)
    if pool_matrix is None:
        return result

    pool_ids = [entity_id for _, entity_id, _ in pool]
    latencies: list[float] = []
    ranks: list[tuple[dict, int]] = []

    print(f"  embedding {len(queries)} queries …", flush=True)
    for query in queries:
        started = time.monotonic()
        try:
            vector = await svc.embed_query(query["query"])  # type: ignore[attr-defined]
        except EmbeddingUnavailable as exc:
            result.ok = False
            result.failure = f"query embedding failed: {exc}"
            return result
        latencies.append((time.monotonic() - started) * 1000)

        scores = pool_matrix @ _normalise(np.asarray([vector], dtype=np.float32))[0]
        top = np.argpartition(-scores, min(TOP_K, len(scores) - 1))[:TOP_K]
        top = top[np.argsort(-scores[top])]
        rank = 0
        for position, index in enumerate(top, 1):
            if pool_ids[index] == query["gold_id"]:
                rank = position
                break
        ranks.append((query, rank))

    _finish(result, ranks, latencies)
    chars = sum(len(text) for _, _, text in pool) + sum(len(q["query"]) for q in queries)
    result.estimated_tokens = chars // 4
    result.estimated_usd = result.estimated_tokens / 1_000_000 * PRICE_PER_MTOK.get(spec.model, 0.0)
    return result


# ────────────────────────────── fts leg ──────────────────────────────────


async def run_fts_candidate(
    conn: asyncpg.Connection,
    pool: list[tuple[str, str, str]],
    queries: list[dict],
) -> CandidateResult:
    """Rank with PostgreSQL alone — the path a provider outage actually takes.

    Scores from `ts_rank_cd` are merged across tables to produce one global
    ranking, which is an approximation: production searches each type
    separately and merges with RRF. The approximation is stated rather than
    hidden, and it is the same for every query, so the per-type numbers below
    remain the trustworthy ones.
    """
    result = CandidateResult(name="fts", kind="fts")
    result.pool_size = len(pool)
    ranks: list[tuple[dict, int]] = []
    latencies: list[float] = []

    union = " UNION ALL ".join(
        f"SELECT id::text AS id, ts_rank_cd(search_vector, q) AS score "  # noqa: S608
        f"FROM {table}, websearch_to_tsquery('english', $1) q WHERE search_vector @@ q"
        for table in FTS_TABLES.values()
    )
    sql = f"SELECT id, score FROM ({union}) hits ORDER BY score DESC LIMIT {TOP_K}"  # noqa: S608

    for index, query in enumerate(queries):
        started = time.monotonic()
        rows = await conn.fetch(sql, query["query"])
        latencies.append((time.monotonic() - started) * 1000)
        rank = 0
        for position, row in enumerate(rows, 1):
            if row["id"] == query["gold_id"]:
                rank = position
                break
        ranks.append((query, rank))
        if index and index % 200 == 0:
            print(f"    {index}/{len(queries)} queries", flush=True)

    _finish(result, ranks, latencies)
    return result


# ─────────────────────────────── scoring ─────────────────────────────────


def _finish(result: CandidateResult, ranks: list[tuple[dict, int]], latencies: list[float]) -> None:
    from run_bench import QueryResult

    def as_results(pairs: list[tuple[dict, int]]) -> list[QueryResult]:
        return [
            QueryResult(
                query_id=q["query_id"],
                variant=q["variant"],
                gold_type=q["gold_type"],
                gold_rank=rank,
            )
            for q, rank in pairs
        ]

    result.scored_queries = len(ranks)
    result.all_types = compute_metrics(as_results(ranks))
    result.headline = compute_metrics(
        as_results([p for p in ranks if p[0]["gold_type"] in SERVED_TYPES])
    )
    by_type: dict[str, list[tuple[dict, int]]] = {}
    by_variant: dict[str, list[tuple[dict, int]]] = {}
    for query, rank in ranks:
        by_type.setdefault(query["gold_type"], []).append((query, rank))
        by_variant.setdefault(query["variant"], []).append((query, rank))
    result.per_type = {k: compute_metrics(as_results(v)) for k, v in by_type.items()}
    result.per_variant = {k: compute_metrics(as_results(v)) for k, v in by_variant.items()}
    result.ranks = [
        {
            "query_id": q["query_id"],
            "gold_type": q["gold_type"],
            "variant": q["variant"],
            "rank": rank,
        }
        for q, rank in ranks
    ]

    if latencies:
        ordered = sorted(latencies)
        result.latency_p50_ms = statistics.median(ordered)
        result.latency_p95_ms = ordered[min(int(len(ordered) * 0.95), len(ordered) - 1)]


# ──────────────────────────────── main ───────────────────────────────────


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--candidate", action="append", default=[], help="shim | openai | fts")
    parser.add_argument("--base-url", default="", help="Base URL for the openai candidate.")
    parser.add_argument("--model", default="", help="Model id for the openai candidate.")
    parser.add_argument("--batch", type=int, default=16)
    parser.add_argument("--limit-queries", type=int, default=None)
    parser.add_argument("--postgres-url", default="postgresql://brain@localhost:5433/brain")
    parser.add_argument("--output", default=str(DEFAULT_OUTPUT))
    args = parser.parse_args()

    wanted = args.candidate or ["fts"]
    settings = settings_for_standalone_script(args.postgres_url)
    dsn = settings.postgres_url.replace("postgresql+asyncpg://", "postgresql://", 1)
    conn = await asyncpg.connect(dsn)

    try:
        print("Loading pool from PostgreSQL …", flush=True)
        pool = await load_pool(conn)
        pool_ids = {entity_id for _, entity_id, _ in pool}
        print(f"  pool: {len(pool)} rows")

        queries = load_queries(GOLD_PATH)
        queries, dropped = resolve_gold(queries, pool_ids)
        if args.limit_queries:
            queries = queries[: args.limit_queries]
        print(f"  gold: {len(queries)} queries ({dropped} dropped — entity gone)")

        results: list[CandidateResult] = []
        for name in wanted:
            print(f"\n━━━ {name} ━━━", flush=True)
            if name == "fts":
                results.append(await run_fts_candidate(conn, pool, queries))
            else:
                spec = CandidateSpec(
                    name=args.model or name,
                    kind=name,
                    base_url=args.base_url,
                    model=args.model,
                )
                results.append(
                    await run_vector_candidate(spec, pool, queries, settings, args.batch)
                )
            last = results[-1]
            if last.ok:
                print(
                    f"  headline recall@5={last.headline['recall@5']:.3f} "
                    f"nDCG@10={last.headline['ndcg@10']:.3f} p50={last.latency_p50_ms:.0f}ms",
                    flush=True,
                )
            else:
                print(f"  FAILED: {last.failure}", flush=True)
    finally:
        await conn.close()

    Path(args.output).write_text(
        json.dumps(
            {
                "bench_version": "v2",
                "pool_size": len(pool),
                "gold_queries": len(queries),
                "gold_dropped": dropped,
                "served_types": list(SERVED_TYPES),
                "candidates": [asdict(r) for r in results],
            },
            indent=2,
        )
    )
    print(f"\nResults written to {args.output}")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
