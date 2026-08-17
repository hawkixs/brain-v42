"""Validation du cutover embedding — gold bench v1 contre une baseline.

Trois mesures sur l'endpoint cible (contrat natif /embed, /rerank) :

  self   — corpus ET queries embeddés par la cible (qualité du modèle
           servi, harnais identique à bench/embedding_v1/run_bench.py).
  cross  — corpus = vecteurs STOCKÉS en PG (embeddés par le PyTorch
           fp16 historique), queries embeddées par la cible. C'est le
           scénario réel post-cutover : requêtes GGUF contre un corpus
           fp16 non ré-embeddé.
  rerank — scores /rerank sur des paires déterministes (parity ONNX vs
           CrossEncoder PyTorch).

Usage :
  # baseline (PyTorch encore en prod)
  python scripts/embedding_cutover_check.py \\
      --url http://localhost:8003 \\
      --output bench/embedding_v1/cutover/baseline_pytorch.json

  # post-cutover (shim en place) + gates
  python scripts/embedding_cutover_check.py \\
      --url http://localhost:8003 \\
      --output bench/embedding_v1/cutover/candidate_gguf.json \\
      --baseline bench/embedding_v1/cutover/baseline_pytorch.json

Gates (candidate vs baseline) : dMRR_self >= -0.01,
drecall@10_self >= -0.005, dMRR_cross >= -0.01, pearson_rerank >= 0.995.
Exit 2 = échantillon cross insuffisant (ni PASS ni FAIL).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import math
import sys
import urllib.request
from pathlib import Path

import asyncpg

REPO = Path(__file__).resolve().parents[1]
BENCH = REPO / "bench" / "embedding_v1"
sys.path.insert(0, str(BENCH))

from run_bench import (  # noqa: E402
    PG_DSN,
    QueryResult,
    compute_metrics,
    cosine_rank_all,
    load_corpus,
)

GOLD_PATH = BENCH / "gold_v1.jsonl"

# etype (gen_gold) -> table PG. Les tables sans colonne embedding sont
# exclues du mode cross à l'exécution (log explicite, pas de cap silencieux).
TABLES = {
    "learning": "learnings",
    "feature": "features",
    "plan_chunk": "indexed_plan_chunks",
    "decision": "decisions",
    "snippet": "snippets",
    "plan": "indexed_plans",
    "runbook": "runbooks",
    "adr": "adrs",
}

RERANK_PAIRS = 20
# 8 textes max par batch : le service PyTorch prod (et le shim GGUF) retourne
# HTTP 500 pour des payloads > ~12 KB (≈ 13 textes à 2000 chars). Batch=8
# est le plafond sûr validé empiriquement sur l'ensemble du corpus (305 items).
EMBED_BATCH = 8
# En dessous de ce nombre de queries gold résolvables en mode cross, le
# gate dMRR_cross n'est pas statistiquement significatif → exit 2 (ni
# PASS ni FAIL : échantillon insuffisant, à investiguer avant cutover).
MIN_CROSS_GOLD = 100

GATES = {
    "d_mrr_self": -0.01,
    "d_recall10_self": -0.005,
    "d_mrr_cross": -0.01,
    "pearson_rerank": 0.995,
}


def _post(url: str, payload: dict, timeout: float = 300.0) -> object:
    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read())


def embed_texts(base_url: str, texts: list[str]) -> list[list[float]]:
    out: list[list[float]] = []
    for i in range(0, len(texts), EMBED_BATCH):
        chunk = texts[i : i + EMBED_BATCH]
        vecs = _post(f"{base_url}/embed", {"texts": chunk})
        out.extend(vecs)  # type: ignore[arg-type]
    return out


def load_gold() -> list[dict]:
    gold = []
    with GOLD_PATH.open() as f:
        for line in f:
            if line.strip():
                gold.append(json.loads(line))
    return gold


async def load_stored_vectors(
    ids_by_type: dict[str, list[str]],
) -> dict[str, list[float]]:
    """Vecteurs PG stockés (fp16 historique) pour les ids du corpus."""
    conn = await asyncpg.connect(PG_DSN)
    stored: dict[str, list[float]] = {}
    try:
        for etype, ids in ids_by_type.items():
            table = TABLES[etype]
            try:
                rows = await conn.fetch(
                    f"SELECT id::text AS id, embedding::text AS emb "
                    f"FROM {table} "  # table depuis le dict fermé TABLES
                    f"WHERE id::text = ANY($1) AND embedding IS NOT NULL",
                    ids,
                )
            except asyncpg.PostgresError as exc:
                print(f"  ! {table}: exclu du mode cross ({exc})")
                continue
            for row in rows:
                stored[row["id"]] = json.loads(row["emb"])
            missing = len(ids) - len(rows)
            if missing:
                print(f"  ! {table}: {missing}/{len(ids)} sans embedding stocké")
    finally:
        await conn.close()
    return stored


def evaluate(
    gold: list[dict],
    query_vecs: dict[str, list[float]],
    corpus_ids: list[str],
    corpus_vecs: list[list[float]],
) -> dict[str, float]:
    results: list[QueryResult] = []
    for q in gold:
        qvec = query_vecs[q["query_id"]]
        ranked = cosine_rank_all(qvec, corpus_vecs, corpus_ids, top_k=50)
        rank = 0
        for idx, (cid, _score) in enumerate(ranked, 1):
            if cid == q["gold_id"]:
                rank = idx
                break
        results.append(QueryResult(q["query_id"], q["variant"], q["gold_type"], rank))
    return compute_metrics(results)


def build_rerank_pairs(
    gold: list[dict], corpus: list[tuple[str, str, str]]
) -> list[tuple[str, list[str]]]:
    """Paires déterministes : (query, [texte gold, distracteur fixe])."""
    if not corpus:
        return []
    text_by_id = {cid: text for _etype, cid, text in corpus}
    pairs: list[tuple[str, list[str]]] = []
    for i, q in enumerate(gold):
        if len(pairs) >= RERANK_PAIRS:
            break
        gold_text = text_by_id.get(q["gold_id"])
        if not gold_text:
            continue
        distractor = corpus[(i * 7) % len(corpus)][2]
        pairs.append((q["query"], [gold_text[:1000], distractor[:1000]]))
    return pairs


def rerank_scores(base_url: str, pairs: list[tuple[str, list[str]]]) -> list[float]:
    scores: list[float] = []
    for query, candidates in pairs:
        out = _post(
            f"{base_url}/rerank",
            {"query": query, "candidates": candidates},
            timeout=60.0,
        )
        scores.extend(out["scores"])  # type: ignore[index]
    return scores


def pearson(a: list[float], b: list[float]) -> float:
    n = len(a)
    if n == 0 or n != len(b):
        return 0.0
    ma = sum(a) / n
    mb = sum(b) / n
    cov = sum((x - ma) * (y - mb) for x, y in zip(a, b, strict=True))
    va = math.sqrt(sum((x - ma) ** 2 for x in a))
    vb = math.sqrt(sum((y - mb) ** 2 for y in b))
    if va == 0.0 or vb == 0.0:
        return 0.0
    return cov / (va * vb)


def compare_with_baseline(candidate: dict, baseline: dict) -> int:
    d_mrr_self = candidate["self"]["mrr"] - baseline["self"]["mrr"]
    d_r10_self = candidate["self"]["recall@10"] - baseline["self"]["recall@10"]
    d_mrr_cross = candidate["cross"]["mrr"] - baseline["cross"]["mrr"]
    corr = pearson(candidate["rerank_scores"], baseline["rerank_scores"])

    checks = [
        ("dMRR_self", d_mrr_self, GATES["d_mrr_self"]),
        ("dRecall@10_self", d_r10_self, GATES["d_recall10_self"]),
        ("dMRR_cross", d_mrr_cross, GATES["d_mrr_cross"]),
        ("pearson_rerank", corr, GATES["pearson_rerank"]),
    ]
    failed = False
    print("\n━━━ Gates vs baseline ━━━")
    for name, value, floor in checks:
        ok = value >= floor
        failed = failed or not ok
        print(f"  {'PASS' if ok else 'FAIL'}  {name}={value:+.4f} (gate >= {floor})")
    print("VERDICT:", "FAIL — rollback requis" if failed else "PASS — cutover validé")
    return 1 if failed else 0


async def run(args: argparse.Namespace) -> int:
    print(f"Cible : {args.url}")
    corpus = await load_corpus()
    print(f"Corpus stratifié : {len(corpus)} entités")

    present = {cid for _etype, cid, _text in corpus}
    gold_all = load_gold()
    gold = [q for q in gold_all if q["gold_id"] in present]
    print(f"Gold : {len(gold)}/{len(gold_all)} queries gardées (gold_id présent)")
    if args.limit_queries:
        gold = gold[: args.limit_queries]

    print(f"Embedding de {len(gold)} queries via la cible …")
    qvec_list = embed_texts(args.url, [q["query"] for q in gold])
    query_vecs = {q["query_id"]: v for q, v in zip(gold, qvec_list, strict=True)}

    print("Mode SELF : embedding du corpus via la cible …")
    corpus_ids = [cid for _e, cid, _t in corpus]
    corpus_vecs = embed_texts(args.url, [t for _e, _c, t in corpus])
    self_metrics = evaluate(gold, query_vecs, corpus_ids, corpus_vecs)
    print(f"  self : mrr={self_metrics['mrr']:.4f} r@10={self_metrics['recall@10']:.4f}")

    print("Mode CROSS : vecteurs stockés PG …")
    ids_by_type: dict[str, list[str]] = {}
    for etype, cid, _text in corpus:
        ids_by_type.setdefault(etype, []).append(cid)
    stored = await load_stored_vectors(ids_by_type)
    cross_ids = [cid for cid in corpus_ids if cid in stored]
    cross_vecs = [stored[cid] for cid in cross_ids]
    gold_cross = [q for q in gold if q["gold_id"] in stored]
    print(f"  corpus cross : {len(cross_ids)}/{len(corpus_ids)} — gold : {len(gold_cross)}")
    if len(gold_cross) < MIN_CROSS_GOLD:
        print(
            f"ÉCHANTILLON CROSS INSUFFISANT ({len(gold_cross)} < {MIN_CROSS_GOLD}) "
            "— gate non significatif, investiguer avant cutover"
        )
        return 2
    cross_metrics = evaluate(gold_cross, query_vecs, cross_ids, cross_vecs)
    print(f"  cross : mrr={cross_metrics['mrr']:.4f} r@10={cross_metrics['recall@10']:.4f}")

    print("Parity RERANK …")
    pairs = build_rerank_pairs(gold, corpus)
    scores = rerank_scores(args.url, pairs)
    print(f"  {len(scores)} scores collectés")

    out = {
        "url": args.url,
        "self": self_metrics,
        "cross": cross_metrics,
        "rerank_scores": scores,
        "n_gold_kept": len(gold),
        "n_cross_corpus": len(cross_ids),
    }
    out_path = Path(args.output)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(out, indent=2))
    print(f"Résultats → {out_path}")

    if args.baseline:
        baseline = json.loads(Path(args.baseline).read_text())
        return compare_with_baseline(out, baseline)
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--url", required=True)
    ap.add_argument("--output", required=True)
    ap.add_argument("--baseline", default=None)
    ap.add_argument("--limit-queries", type=int, default=None)
    args = ap.parse_args()
    return asyncio.run(run(args))


if __name__ == "__main__":
    sys.exit(main())
