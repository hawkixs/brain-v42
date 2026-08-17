# Embedding Benchmark v1 — Qodo vs Qwen3-4B vs jina-v3

See protocol: `docs/benchmarks/embedding_v1.md`

## Hardware target

All 3 services run on **dev-pc (192.168.1.11, RTX 5070 Ti 16 GiB)** via docker context `dev-pc`.

## Layout

```
bench/embedding_v1/
├── docker-compose.yml         3 services (8023/qodo, 8024/qwen3-4b, 8025/jina-v3)
├── models/                    bind-mounted, git-ignored
│   ├── qwen3-embedding-4b-q4_k_m.gguf
│   └── jina/                  HF cache
├── jina-v3-server/            FastAPI wrapper for sentence-transformers
├── download_models.sh         one-shot model fetch
├── gen_gold.py                Phase D — synth 915 queries from brain corpus
├── run_bench.py               Phase E — measurement loop
├── report.py                  Phase F — render markdown + ADR draft
└── gold_v1.jsonl / results_v1.json / report_v1.md   (generated, git-ignored)
```

## Usage

```bash
# 1. Models (one-shot, ~4 GB total)
bash download_models.sh

# 2. Stand up the stack on dev-pc
docker --context dev-pc compose -f docker-compose.yml up -d

# 3. Sanity check (waits for healthy, pokes each /embed)
python3 sanity_check.py

# 4. Generate gold dataset
python3 gen_gold.py           # → gold_v1.jsonl (~915 queries)

# 5. Run benchmark
python3 run_bench.py          # → results_v1.json (~1h)

# 6. Render report
python3 report.py             # → report_v1.md
```

## Notes

- Prod brain-v42 on PC Serveur is NOT touched. This bench lives 100% on dev-pc.
- Models cached in `models/` via bind mount (inspectable, easy to clean).
- `gold_v1.jsonl` uses seed=2026-04-12 for reproducibility.
