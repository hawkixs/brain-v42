# Switch the embedding provider (a planned operation, never a failover)

Written 2026-09-21, while deciding whether brain-v42 can move to red-base with
a hosted embedding provider instead of the GPU on the PC server.

## Authority and stop conditions

- A provider switch is an operator decision. Nothing in the nightly pipeline,
  no hook and no agent may arm one.
- Never cut over while `brain-v42-dream` is running.
- Forbidden windows: 06:00–09:00 and 13:00 (operator decision, 2026-09-20).
- **Stop before step 1 while `features` has no bulk re-embed.** Of the 3159
  rows outside `regen_embeddings.py`, that is the only one with no path at
  all, and stale vectors there corrupt `cluster_guard`'s deduplication rather
  than merely a search result. See the coverage section.

## Why this is planned and never a failover

Each of the nine vector tables carries exactly one `embedding` column and no
column records which model produced it. Qodo-Embed-1-1.5B and
codestral-embed-2505 both emit 1536 dimensions, so pointing the backend at the
other one raises nothing anywhere: pgvector keeps answering and every cosine
distance silently becomes noise. A dimension mismatch would at least crash.

So there is no hot standby. Two models cannot share a column, and a second
column would mean double-writing on every write path — which keeps the GPU
permanently alive on the PC server and defeats the reason for moving.

What covers an outage instead is already built: `brain_service.fan_out`
catches `EmbeddingUnavailable` and degrades the whole search to FTS-only,
reported through `search_mode`, with `score_kind_by_type` naming the
provenance so a rank is never rendered as a calibrated score. Writes persist
with a NULL embedding and `embedding_backfill` fills them once the endpoint
returns.

Deliberate switching is cheap; emergency switching is impossible. A full
reindex is ~3.1 M tokens: at codestral-embed's 0.15 $/M that is **0.47 $**
standard, **0.23 $** on the batch API. You cannot reindex 8 800 rows during a
ten-minute outage, and you never need to.

## What the degraded mode costs (measured 2026-09-21)

Measured with `bench/embedding_v2/run_retrieval_bench.py` on the live corpus:
an 8 137-row ranking pool, 879 gold queries from `bench/embedding_v1`
(36 dropped, their entity no longer exists). Headline figures cover the six
types `brain_search` actually serves — decision, learning, snippet, runbook,
adr, plan.

| Candidate | recall@1 | recall@5 | recall@10 | MRR | nDCG@10 | p50 |
|---|---:|---:|---:|---:|---:|---:|
| `fts` (PostgreSQL, the outage path) | 0.213 | **0.234** | 0.237 | 0.223 | 0.227 | 3.6 ms |
| `shim` (qodo, today's production) | 0.542 | **0.828** | 0.881 | 0.673 | 0.722 | 32.1 ms |

**The degraded mode recovers 28 % of production recall.** Losing the endpoint
does not make search slightly worse; it takes finding the right document in
the top five from five times in six down to roughly one time in four.

Per type, recall@5, the same run:

| Type | `fts` | `shim` |
|---|---:|---:|
| adr | 0.111 | 1.000 |
| runbook | **0.000** | 1.000 |
| feature | **0.000** | 0.980 |
| decision | 0.253 | 0.960 |
| snippet | 0.400 | 0.933 |
| plan | 0.167 | 0.833 |
| learning | 0.233 | 0.803 |
| plan_chunk | 0.210 | 0.704 |

Note also that qodo scores 0.828 here against 0.965 in the April v1 report.
Nothing regressed: April ranked against 305 documents and this ranks against
8137. It is the same model measured on a harder, realistic pool — which is
why an April figure must never be quoted against a v2 one.

Two structural limits of the degraded path, not tuning issues:

- `features` is the one pooled table with **no `search_vector` column**. The
  lexical fallback cannot return a feature by construction. It does not matter
  for `brain_search`, which never searches features, but it does for anything
  reading features semantically.
- Scores from `ts_rank_cd` are merged across tables to build one global
  ranking, while production searches each type separately and merges with RRF.
  Read the per-type numbers as the trustworthy ones.

Do not copy these figures forward. Re-measure: the pool grows, and recall@k
falls as it does.

## Coverage gap — read before anything else

`scripts/regen_embeddings.py` reindexes **five** of the nine vector tables.
Measured 2026-09-21:

| Table | Embedded rows | Reindex tool |
|---|---:|---|
| learnings | 3861 | `regen_embeddings.py` |
| decisions | 1293 | `regen_embeddings.py` |
| snippets | 195 | `regen_embeddings.py` |
| runbooks | 181 | `regen_embeddings.py` |
| adrs | 122 | `regen_embeddings.py` |
| **indexed_plan_chunks** | **1792** | **none** |
| **features** | **920** | **none** |
| **gitlab_events** | **239** | **none** |
| **indexed_plans** | **208** | **none** |

3159 rows of 8811 — 36 % of the corpus — are outside `regen_embeddings.py`.
They are not all the same problem, and the difference is what makes this
tractable:

- **`indexed_plans` + `indexed_plan_chunks` (2000 rows) — a step, but not the
  obvious one.** `plan_indexer.index_path()` embeds both when it processes a
  file, so re-running plan indexing looks like the answer. It is not, by
  itself: `_is_unchanged` skips any file whose SHA256 still matches the stored
  `content_hash`, and after a provider switch every plan file is unchanged.
  A plain re-run would report `skipped` for all 208 plans and rewrite nothing,
  which is the worst outcome — a green run that did nothing.

  The intended lever is the staleness flag, the same one migration 014 used to
  force re-chunking. Measured 2026-09-21: all 208 rows are `fresh`.

  ```sql
  UPDATE indexed_plans SET freshness_status = 'stale';
  ```

  then re-run plan indexing; chunks follow through
  `upsert_plan_with_chunks`, so no duplicate row is created. This is the
  `plan` type `brain_search` serves, so it is not optional.
- **`features` (920 rows) — the real gap.** `features.embedding` is written
  only on create and update, by `feature_linker` and `cluster_guard`. There is
  no bulk path at all. It is also the one that silently corrupts a decision
  rather than a search result: `cluster_guard` deduplicates semantically, so
  stale feature vectors mean dedup comparing two models' vectors and merging
  or splitting on noise.
- **`gitlab_events` (239 rows) — dead, leave it.** Last row processed
  2026-06-24 and the GitLab rail was retired 2026-08-18 (decision
  `218028c7`). No search path reads it. Stale vectors there cost nothing;
  say so rather than build a tool for it.

So the blocker before a switch is **one table**: a bulk re-embed for
`features`. Everything else is a step to schedule or a non-issue to declare.

## Sequence

### 1. Preflight — prove the corpus matches the model you are leaving

```bash
python scripts/check_embedding_model_drift.py --per-type 8
```

Requires exit 0. Exit 2 means the run was incomplete — a saturated endpoint
answers 503 and the check refuses to call four fifths of an answer a pass.
Retry when the endpoint is idle rather than lowering `--per-type`.

Note the reported `NOT CHECKED` line: it names the rows this check cannot see.

### 2. Dump

Take and verify a dump before touching anything, as for a migration. The
vectors are the expensive part to rebuild, not the rows.

### 3. Arm the provider

Nothing in the code needs to change. `OpenAIWire` ships and is deployed, so
the switch is configuration:

```bash
BRAIN_EMBEDDING_BACKEND=openai
BRAIN_EMBEDDING_SERVICE_URL=https://api.mistral.ai
BRAIN_EMBEDDING_MODEL=codestral-embed-2505
BRAIN_EMBEDDING_TOKEN_FILE=~/.config/brain-v42/<provider>.key
```

The key goes in a file, never in an inline environment value — same rule as
the MCP bearer. `codestral-embed-2505` is the model that fits: 1536 dimensions
by default, exactly the nine `vector(1536)` columns, so no migration. Leave
`output_dtype` alone; pgvector stores float32 and int8 would be a second
change measured at the same time as the first.

**Do not restart the writers yet.** Between arming and the end of the reindex
the corpus does not match the configured model, and that is precisely the
state `check_embedding_model_drift.py` exists to refuse.

### 4. Reindex

Every table, in one window. The batch API halves the price and this is not
time-sensitive work.

```bash
python scripts/regen_embeddings.py            # the five covered tables (5652 rows)
# mark plans stale, THEN re-run plan indexing       (indexed_plans + chunks, 2000 rows)
#   UPDATE indexed_plans SET freshness_status = 'stale';   -- else every file is skipped
# then the features bulk re-embed                   (920 rows — tool to be written)
# gitlab_events (239 rows) is dead: leave it stale, deliberately
```

The order does not matter, but the completeness does. Track the four as a
checklist and do not restart the writers until every line is done.

### 5. Verify

```bash
python scripts/check_embedding_model_drift.py --per-type 8   # must exit 0
python bench/embedding_v2/run_retrieval_bench.py --candidate openai \
  --base-url https://api.mistral.ai --model codestral-embed-2505
```

Exit 0 on the drift check proves the sampled five tables were rewritten by the
configured model. It proves nothing about the other four — check those by the
means chosen when closing the coverage gap.

The bench proves quality did not collapse. Compare against the qodo row of the
same report, on the same pool: a number from a different pool size is not a
comparison.

### 6. Restart the writers

Only now. Then confirm `brain_search` returns sane results on a handful of
known queries.

## Rollback

Rolling back is the same operation in the other direction, at the same price:
restore the previous configuration and reindex again. It is NOT enough to
revert the environment variables — the corpus would then hold the new
provider's vectors while the old model answers the queries, which is the exact
failure this runbook exists to prevent.

The fast path is the dump from step 2: restoring it brings back vectors that
match the old model in one move. That is why step 2 is not optional.

## What would make this document false, and is watched by no test

- A reindex tool appearing for the four uncovered tables, which would retire
  the coverage-gap section.
- A model column landing in the schema, which would turn the empirical drift
  check into a declared one.
- Provider pricing moving, which changes only the cost sentences.
- The pool growing, which lowers every recall figure quoted here.
