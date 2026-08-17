"""Phase E — run the 3-candidate embedding benchmark.

For each candidate:
  1. docker restart (fresh VRAM)
  2. wait healthy
  3. embed 305 corpus entities (from same stratified sample as gen_gold.py)
  4. embed 915 queries (measure single-text p50/p95/p99)
  5. cosine similarity → rank → recall@{1,5,10}, MRR, nDCG@10
  6. sample VRAM at warmup / after 100 / after 1000 inferences

Writes bench/embedding_v1/results_v1.json.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import math
import statistics
import subprocess
import sys
import time
import urllib.request
from dataclasses import asdict, dataclass, field
from pathlib import Path

import asyncpg

BENCH_DIR = Path(__file__).parent
GOLD_PATH = BENCH_DIR / "gold_v1.jsonl"
RESULTS_PATH = BENCH_DIR / "results_v1.json"
PG_DSN = "postgresql://brain:brain@localhost:5433/brain"
DEV_PC = "192.168.1.11"
DOCKER_CONTEXT = "dev-pc"
GPU_TOTAL_MIB = 16303  # RTX 5070 Ti 16 GiB

# Import from gen_gold so the stratified sample stays consistent.
sys.path.insert(0, str(BENCH_DIR))
from gen_gold import QUERIES, SAMPLE_SIZES, SEED  # noqa: E402


@dataclass
class Candidate:
    name: str
    container: str
    url: str
    backend: str  # "native" or "llama_cpp"
    dim: int
    query_task: str | None = None  # e.g. "retrieval.query" for jina
    matryoshka_dim: int | None = None  # truncate+renorm (qwen3 native 2560 → 1024)


CANDIDATES = [
    Candidate("qodo", "bench_qodo", f"http://{DEV_PC}:8023/embed", "native", 1536),
    Candidate(
        "qwen3-4b",
        "bench_qwen3_4b",
        f"http://{DEV_PC}:8024/embedding",
        "llama_cpp",
        1024,
        matryoshka_dim=1024,
    ),
    Candidate(
        "jina-v3",
        "bench_jina_v3",
        f"http://{DEV_PC}:8025/embed",
        "native",
        1024,
        query_task="retrieval.query",
    ),
]


# ─────────────────────────── HTTP helpers ────────────────────────────────


def _http_post(url: str, payload: dict, timeout: float = 120.0) -> object:
    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read())


def _norm(vec: list[float]) -> list[float]:
    """L2-normalize (llama.cpp doesn't always normalize)."""
    s = math.sqrt(sum(v * v for v in vec))
    if s == 0.0:
        return vec
    return [v / s for v in vec]


def _unwrap_llama_emb(raw) -> list[float]:
    """llama.cpp returns `embedding: [[float,...]]` (outer list wraps the pooled
    vector). Unwrap to a flat list."""
    if raw and isinstance(raw[0], list):
        return raw[0]
    return raw


def _truncate_renorm(vec: list[float], target_dim: int | None) -> list[float]:
    if target_dim is None or len(vec) <= target_dim:
        return vec
    vec = vec[:target_dim]
    return _norm(vec)


def embed_batch(cand: Candidate, texts: list[str], is_query: bool) -> list[list[float]]:
    if cand.backend == "native":
        # qodo + jina share the same API shape
        url = cand.url
        if cand.query_task and is_query:
            url = f"{url}?task={cand.query_task}"
        return _http_post(url, {"texts": texts})  # type: ignore[return-value]
    if cand.backend == "llama_cpp":
        # llama.cpp /embedding accepts {"content": [list]} and returns
        # [{"index": i, "embedding": [[float,...]]}] for batches (the inner
        # [[...]] is why single-level _norm blew up on the first smoke run).
        data = _http_post(cand.url, {"content": texts})
        if isinstance(data, dict) and "embedding" in data:
            raw = _unwrap_llama_emb(data["embedding"])
            return [_truncate_renorm(_norm(raw), cand.matryoshka_dim)]
        out = []
        for d in data:  # type: ignore[assignment]
            raw = _unwrap_llama_emb(d["embedding"])
            out.append(_truncate_renorm(_norm(raw), cand.matryoshka_dim))
        return out
    raise ValueError(f"unknown backend: {cand.backend}")


def embed_single_timed(cand: Candidate, text: str, is_query: bool) -> tuple[list[float], float]:
    start = time.monotonic()
    vecs = embed_batch(cand, [text], is_query=is_query)
    latency_ms = (time.monotonic() - start) * 1000
    return vecs[0], latency_ms


# ─────────────────────── container lifecycle ─────────────────────────────


def restart_container(container: str) -> None:
    subprocess.run(
        ["docker", "--context", DOCKER_CONTEXT, "restart", container],
        check=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def stop_container(container: str) -> None:
    subprocess.run(
        ["docker", "--context", DOCKER_CONTEXT, "stop", container],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def isolate_gpu_for(target_container: str, all_containers: list[str]) -> None:
    """Stop sibling bench containers so VRAM attribution is clean.

    Without this, the VRAM peak sample includes the baseline of the other
    services loaded on the same GPU, inflating the number by ~8 GiB and
    causing the reliability gate to false-fail.
    """
    for c in all_containers:
        if c != target_container:
            stop_container(c)


def wait_healthy(container: str, cand: Candidate, timeout_s: int = 300) -> bool:
    """Wait for docker healthy + service responds."""
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        try:
            r = subprocess.run(
                [
                    "docker",
                    "--context",
                    DOCKER_CONTEXT,
                    "inspect",
                    "--format",
                    "{{.State.Health.Status}}",
                    container,
                ],
                capture_output=True,
                text=True,
                timeout=10,
            )
            status = r.stdout.strip()
        except subprocess.TimeoutExpired:
            status = "?"
        if status == "healthy":
            # Also confirm /embed works — healthcheck might have been last
            # probed before a transient OOM window.
            try:
                vecs = embed_batch(cand, ["warmup"], is_query=False)
                if vecs and len(vecs[0]) == cand.dim:
                    return True
            except Exception:  # noqa: BLE001
                pass
        time.sleep(3)
    return False


def sample_vram() -> tuple[int, int]:
    """Return (used_mib, free_mib) from dev-pc nvidia-smi via SSH."""
    r = subprocess.run(
        [
            "ssh",
            "arman@" + DEV_PC,
            "nvidia-smi --query-gpu=memory.used,memory.free --format=csv,noheader,nounits",
        ],
        capture_output=True,
        text=True,
        timeout=15,
    )
    line = r.stdout.strip().split("\n")[0]
    used, free = (int(x.strip()) for x in line.split(","))
    return used, free


# ─────────────────────── corpus loader ───────────────────────────────────


async def load_corpus() -> list[tuple[str, str, str]]:
    """Return (etype, id, combined_text) matching the gen_gold sample."""
    conn = await asyncpg.connect(PG_DSN)
    try:
        items: list[tuple[str, str, str]] = []
        for etype, sql in QUERIES.items():
            k = SAMPLE_SIZES[etype]
            rows = await conn.fetch(sql, SEED, k)
            for row in rows:
                title = (row["title"] or "").strip()
                body = (row["body"] or "").strip()
                # Truncate to keep all backends within their limits (jina 8k, qodo 512, qwen3 8k).
                combined = (title + "\n\n" + body)[:2000]
                items.append((etype, row["id"], combined))
        return items
    finally:
        await conn.close()


def load_queries(path: Path) -> list[dict]:
    queries = []
    with path.open() as f:
        for line in f:
            if line.strip():
                queries.append(json.loads(line))
    return queries


# ─────────────────────── metrics ─────────────────────────────────────────


@dataclass
class QueryResult:
    query_id: str
    variant: str
    gold_type: str
    gold_rank: int  # 1-indexed, or 0 if not in top-50


@dataclass
class CandidateResult:
    name: str
    reliability_flag: bool
    reliability_reason: str = ""
    vram_used_warmup_mib: int = 0
    vram_used_after100_mib: int = 0
    vram_used_after1000_mib: int = 0
    vram_peak_mib: int = 0
    latency_p50_ms: float = 0.0
    latency_p95_ms: float = 0.0
    latency_p99_ms: float = 0.0
    recall_at_1: float = 0.0
    recall_at_5: float = 0.0
    recall_at_10: float = 0.0
    mrr: float = 0.0
    ndcg_at_10: float = 0.0
    per_type: dict[str, dict[str, float]] = field(default_factory=dict)
    per_variant: dict[str, dict[str, float]] = field(default_factory=dict)
    total_queries: int = 0
    total_corpus: int = 0


def cosine_rank_all(
    query_vec: list[float],
    corpus_vecs: list[list[float]],
    corpus_ids: list[str],
    top_k: int = 50,
) -> list[tuple[str, float]]:
    """Return top_k (id, score) sorted DESC."""
    # Both corpus and query are already normalized, so dot = cosine
    scores = [
        (cid, sum(q * c for q, c in zip(query_vec, v, strict=True)))
        for cid, v in zip(corpus_ids, corpus_vecs, strict=True)
    ]
    scores.sort(key=lambda x: x[1], reverse=True)
    return scores[:top_k]


def compute_metrics(results: list[QueryResult]) -> dict[str, float]:
    if not results:
        return {
            "recall@1": 0.0,
            "recall@5": 0.0,
            "recall@10": 0.0,
            "mrr": 0.0,
            "ndcg@10": 0.0,
            "n": 0,
        }
    r1 = sum(1 for r in results if r.gold_rank == 1) / len(results)
    r5 = sum(1 for r in results if 1 <= r.gold_rank <= 5) / len(results)
    r10 = sum(1 for r in results if 1 <= r.gold_rank <= 10) / len(results)
    mrr = sum((1.0 / r.gold_rank) if r.gold_rank >= 1 else 0.0 for r in results) / len(results)
    ndcg = sum(
        (1.0 / math.log2(r.gold_rank + 1)) if 1 <= r.gold_rank <= 10 else 0.0 for r in results
    ) / len(results)
    return {
        "recall@1": r1,
        "recall@5": r5,
        "recall@10": r10,
        "mrr": mrr,
        "ndcg@10": ndcg,
        "n": len(results),
    }


# ─────────────────────── main bench loop ─────────────────────────────────


def run_candidate(
    cand: Candidate,
    corpus: list[tuple[str, str, str]],
    queries: list[dict],
    all_containers: list[str],
) -> CandidateResult:
    print(f"\n━━━ {cand.name} ━━━", flush=True)
    res = CandidateResult(name=cand.name, reliability_flag=True)
    res.total_corpus = len(corpus)
    res.total_queries = len(queries)

    # 1. Stop siblings — GPU isolation, clean VRAM attribution
    print("  stopping other bench containers …", flush=True)
    for c in all_containers:
        if c != cand.container:
            stop_container(c)

    # 2. Fresh container for the target candidate
    print(f"  restart {cand.container} …", flush=True)
    restart_container(cand.container)
    if not wait_healthy(cand.container, cand):
        res.reliability_flag = False
        res.reliability_reason = "container did not become healthy"
        return res

    # 2. Warmup VRAM sample
    used, _ = sample_vram()
    res.vram_used_warmup_mib = used
    res.vram_peak_mib = used

    # 3. Embed corpus (in batches of 32 for practicality)
    print(f"  embedding corpus ({len(corpus)} items) …", flush=True)
    corpus_vecs: list[list[float]] = []
    corpus_ids = [cid for _, cid, _ in corpus]
    BATCH = 16
    try:
        for i in range(0, len(corpus), BATCH):
            chunk = [txt for _, _, txt in corpus[i : i + BATCH]]
            vecs = embed_batch(cand, chunk, is_query=False)
            corpus_vecs.extend(vecs)
    except Exception as exc:  # noqa: BLE001
        res.reliability_flag = False
        res.reliability_reason = f"corpus embedding failed: {exc}"
        return res

    used, _ = sample_vram()
    res.vram_peak_mib = max(res.vram_peak_mib, used)

    # 4. Embed queries one-by-one (measure single-text latency)
    print(f"  embedding {len(queries)} queries …", flush=True)
    query_results: list[QueryResult] = []
    latencies: list[float] = []

    inferences = len(corpus)  # Already did one per corpus item

    for idx, q in enumerate(queries):
        try:
            qvec, lat = embed_single_timed(cand, q["query"], is_query=True)
        except Exception as exc:  # noqa: BLE001
            res.reliability_flag = False
            res.reliability_reason = f"query {q['query_id']} embed failed: {exc}"
            break
        latencies.append(lat)
        inferences += 1

        ranked = cosine_rank_all(qvec, corpus_vecs, corpus_ids, top_k=50)
        rank = 0
        for r_idx, (cid, _) in enumerate(ranked, 1):
            if cid == q["gold_id"]:
                rank = r_idx
                break
        query_results.append(
            QueryResult(
                query_id=q["query_id"],
                variant=q["variant"],
                gold_type=q["gold_type"],
                gold_rank=rank,
            )
        )

        if inferences == 100:
            used, _ = sample_vram()
            res.vram_used_after100_mib = used
            res.vram_peak_mib = max(res.vram_peak_mib, used)
        if inferences == 1000:
            used, _ = sample_vram()
            res.vram_used_after1000_mib = used
            res.vram_peak_mib = max(res.vram_peak_mib, used)

        if (idx + 1) % 100 == 0:
            print(f"    {idx + 1}/{len(queries)} queries processed", flush=True)

    # Final VRAM sample
    used, _ = sample_vram()
    res.vram_peak_mib = max(res.vram_peak_mib, used)

    # VRAM threshold check
    if res.vram_peak_mib > 0.90 * GPU_TOTAL_MIB:
        res.reliability_flag = False
        if not res.reliability_reason:
            res.reliability_reason = f"VRAM peak {res.vram_peak_mib} MiB > 90% of {GPU_TOTAL_MIB}"

    # 5. Aggregate metrics
    if latencies:
        sorted_lat = sorted(latencies)
        res.latency_p50_ms = statistics.median(sorted_lat)
        res.latency_p95_ms = sorted_lat[int(len(sorted_lat) * 0.95)]
        res.latency_p99_ms = sorted_lat[min(int(len(sorted_lat) * 0.99), len(sorted_lat) - 1)]

    overall = compute_metrics(query_results)
    res.recall_at_1 = overall["recall@1"]
    res.recall_at_5 = overall["recall@5"]
    res.recall_at_10 = overall["recall@10"]
    res.mrr = overall["mrr"]
    res.ndcg_at_10 = overall["ndcg@10"]

    # Per-type and per-variant
    by_type: dict[str, list[QueryResult]] = {}
    by_variant: dict[str, list[QueryResult]] = {}
    for r in query_results:
        by_type.setdefault(r.gold_type, []).append(r)
        by_variant.setdefault(r.variant, []).append(r)
    res.per_type = {k: compute_metrics(v) for k, v in by_type.items()}
    res.per_variant = {k: compute_metrics(v) for k, v in by_variant.items()}

    print(
        f"  recall@10={res.recall_at_10:.3f}  mrr={res.mrr:.3f}  "
        f"p50={res.latency_p50_ms:.0f}ms  vram_peak={res.vram_peak_mib} MiB  "
        f"reliability={'OK' if res.reliability_flag else 'FAIL'}",
        flush=True,
    )
    return res


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument(
        "--only", nargs="*", default=None, help="Limit to specific candidate names (debug)"
    )
    ap.add_argument("--limit-queries", type=int, default=None)
    ap.add_argument("--output", default=str(RESULTS_PATH))
    args = ap.parse_args()

    if not GOLD_PATH.exists():
        print(f"ERROR: {GOLD_PATH} not found — run gen_gold.py first", file=sys.stderr)
        return 1

    print("Loading corpus from Postgres …")
    corpus = await load_corpus()
    print(f"  corpus: {len(corpus)} entities")

    print("Loading gold queries …")
    queries = load_queries(GOLD_PATH)
    if args.limit_queries:
        queries = queries[: args.limit_queries]
    print(f"  queries: {len(queries)}")

    targets = CANDIDATES
    if args.only:
        targets = [c for c in CANDIDATES if c.name in args.only]

    all_containers = [c.container for c in CANDIDATES]
    all_results: list[CandidateResult] = []
    for cand in targets:
        try:
            r = run_candidate(cand, corpus, queries, all_containers)
        except Exception as exc:  # noqa: BLE001
            r = CandidateResult(
                name=cand.name,
                reliability_flag=False,
                reliability_reason=f"unhandled: {exc}",
            )
        all_results.append(r)

    out = {
        "bench_version": "v1",
        "seed": SEED,
        "gpu_total_mib": GPU_TOTAL_MIB,
        "n_corpus": len(corpus),
        "n_queries": len(queries),
        "candidates": [asdict(r) for r in all_results],
    }
    Path(args.output).write_text(json.dumps(out, indent=2))
    print(f"\nResults written to {args.output}")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
