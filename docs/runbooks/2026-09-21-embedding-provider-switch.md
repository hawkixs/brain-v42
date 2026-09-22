# Switch the embedding provider (a planned operation, never a failover)

Written 2026-09-21, while deciding whether brain-v42 can move to red-base with
a hosted embedding provider instead of the GPU on the PC server.

## Authority and stop conditions

- A provider switch is an operator decision. Nothing in the nightly pipeline,
  no hook and no agent may arm one.
- Never cut over while `brain-v42-dream` is running.
- Forbidden windows: 06:00–09:00 and 13:00 (operator decision, 2026-09-20).
- **Do not restart the writers until every line of the step 4 checklist is
  done.** Between arming and the end of the reindex the corpus does not match
  the configured model; a partial reindex leaves two models' vectors in one
  column, which is the failure this runbook exists to prevent.

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

By query shape, recall@5 — this is the number that decides operational
guidance during an outage:

| Gold variant | `fts` | `shim` | FTS recovers |
|---|---:|---:|---:|
| `keyword-bag` | 0.410 | 0.911 | 45 % |
| `literal-paraphrase` | 0.201 | 0.915 | 22 % |
| `abstract-question` | **0.003** | 0.676 | **0.5 %** |

The degraded mode does not degrade evenly, it collapses by query shape. On a
natural question — the way an agent actually searches a memory — FTS finds
the right document three times in a thousand. **During an endpoint outage the
only usable strategy is keyword queries**, and even then you get under half of
what vectors give. Say this to whoever is on call; "search is degraded" does
not convey it.

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

`scripts/regen_embeddings.py` reindexes **six** of the nine vector tables.
Measured 2026-09-21:

| Table | Embedded rows | Reindex tool |
|---|---:|---|
| learnings | 3861 | `regen_embeddings.py` |
| decisions | 1293 | `regen_embeddings.py` |
| snippets | 195 | `regen_embeddings.py` |
| runbooks | 181 | `regen_embeddings.py` |
| adrs | 122 | `regen_embeddings.py` |
| features | 920 | `regen_embeddings.py` (since 2026-09-21) |
| **indexed_plan_chunks** | **1792** | **none** |
| **gitlab_events** | **239** | **none** |
| **indexed_plans** | **208** | **none** |

2239 rows of 8811 — 25 % of the corpus — are outside `regen_embeddings.py`.
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

  Re-running it does **not** reopen the plans' feature assignments. Since
  2026-09-21 the indexer separates a vector refresh from a new signal: a file
  whose stored `content_hash` still matches is re-embedded and upserted, and
  `cluster_guard.resolve()` is not called for it at all. Without that
  separation this step was the switch's real hazard — `signal_type="plan"` is
  in `CREATING_SIGNALS`, so link-only mode does not stop it, and a reindexed
  `features` column moves scores across `COSINE_LINK`. Measured on the 239
  already-linked plans, a third of them fell out of the direct-link band and
  into the reranker's grey zone. The run reports `linked=0` for such files and
  logs `plan_indexer.resolution_skipped`; that zero means no link was MADE,
  not that the plan is unlinked.
- **`features` (920 rows) — was the real gap, now closed, and the closure
  redefined the column.** `features.embedding` used to be written only on
  create and update by `feature_linker` and `cluster_guard`, with no bulk
  path. It is the table that silently corrupts a decision rather than a
  search result: `cluster_guard` links at `COSINE_LINK = 0.70` and across two
  models cosine sits near zero, so a stale corpus would link nothing and mint
  a fresh feature per signal — the pseudo-feature flood whose tap has been
  shut since 2026-08-03. `regen_embeddings.py` now rewrites it from
  `description`. Read that as a redefinition, not a restoration: measured on
  60 random rows the stored vector matched `embed(description)` on 3 % of
  them (median similarity 0.81), because `_create_feature` stored the
  caller's embedding, computed from the originating artifact's text, which no
  column records. `description` is the only text reproducible from the row
  alone — which is what makes the column verifiable by
  `check_embedding_model_drift.py` instead of unverifiable forever.
- **`gitlab_events` (239 rows) — dead, leave it.** Last row processed
  2026-06-24 and the GitLab rail was retired 2026-08-18 (decision
  `218028c7`). No search path reads it. Stale vectors there cost nothing;
  say so rather than build a tool for it.

Nothing blocks a switch any more. What remains is one step to schedule
(plans, through the staleness flag) and one non-issue to declare
(`gitlab_events`).

## Sequence

### 1. Preflight — prove the corpus matches the model you are leaving

```bash
python scripts/check_embedding_model_drift.py --per-type 8
```

Requires exit 0. Exit 2 means the run was incomplete — a saturated endpoint
answers 503 and the check refuses to call four fifths of an answer a pass.
Retry when the endpoint is idle rather than lowering `--per-type`.

**Read the per-type lines, not only the verdict.** The sample is spread across
the six covered types, so a type written entirely by another model is a
minority of the rows: the global median does not move and the check says
MATCH. Since 2026-09-21 the report breaks the sample down per type and flags
any whose median is below the threshold.

One of them is expected to be flagged today, and only one: `feature`. Measured
2026-09-21, median 0.7937 with 8 of 8 below threshold, while every other type
sat at 1.0000. That is not model drift — the column was never
`embed(description)`, so the check cannot reproduce what is stored until the
reindex redefines it. **Any other type below threshold is a real finding and
stops the window.** After step 5, `feature` must join the others.

The verdict itself still reads the global median. Gating on per-type medians
would fail this preflight today, on that known and expected condition, which is
how a check becomes one nobody runs.

Telling "unverifiable-by-construction" apart from "written by another model" is
done, in one direction: a row whose own columns cannot reproduce its embedding
input is refused rather than scored, and counted on the `UNVERIFIABLE` line
with the reason. Today that is `gitlab_events` alone — the ingestor embeds
`text[:2000]` and stores `text[:500]`, so 127 of its 239 rows can never be
verified whatever the checker does. Closing that needs a wider column, not a
better check, and the table is dead by decision `218028c7`.

The `NOT CHECKED` line is gone with the blind spot it named. All nine vector
tables are sampled: `indexed_plans`, `indexed_plan_chunks` and `gitlab_events`
joined on 2026-09-22 when their write paths and this checker started calling
the same composers in `brain_v42.services.embedding_text`. **Read the per-type
breakdown for them especially.** They are NOT rewritten by
`regen_embeddings.py`: plans come back only through a plan reindex after being
marked stale, so a switch can leave 2239 rows on the old model while the six
rewritten tables all read fresh. Measured on 2026-09-22, mid-trial, that is
exactly what the corpus looked like — the six reindexed types scored ~0.00
against qodo while `plan` sat at 0.9998 and `plan_chunk` at 0.9979, still
carrying the old model's vectors.

### 2. Dump

Take and verify a dump before touching anything, as for a migration. The
vectors are the expensive part to rebuild, not the rows.

### 3. Stop the writers

Nothing above this line touched production. Everything below it does.

```bash
# The watchdog timer FIRST. It probes /health every 30 s and restarts the MCP,
# so stopping the server before the timer resurrects it within half a minute.
systemctl --user stop brain-mcp-http-watchdog.timer
systemctl --user stop brain-mcp-http.service
systemctl --user show brain-mcp-http.service -p MainPID --value   # must print 0
```

Measured 2026-09-21: four systemd units build an embedding client — the MCP,
`brain-metrics`, `brain-v42-automation` and `brain-v42-dream` — and all four
have the repository checkout as their working directory, which is how they read
the `.env` armed in the next step. Check which of them are active and stop those
too; on that date only the MCP and `brain-metrics` were. `brain-v42-dream`
triggers at 06:00, so a window that ends before then does not need to fight it,
but the forbidden-window rule above is what keeps that true.

The reason is not the `.env`: a running process loaded its settings at startup
and will not re-read them. The reason is that a live writer embeds with the OLD
model while the reindex is sweeping, and any row it writes afterwards is a qodo
vector in a codestral corpus that no checklist line would catch.

### 4. Arm the provider

Nothing in the code needs to change. `OpenAIWire` ships and is deployed, so
the switch is configuration:

```bash
BRAIN_EMBEDDING_BACKEND=openai
BRAIN_EMBEDDING_SERVICE_URL=https://api.mistral.ai
BRAIN_EMBEDDING_MODEL=codestral-embed-2505
BRAIN_EMBEDDING_TOKEN_FILE=/home/hawixs/.config/brain-v42/<provider>.key
```

Write the path absolute. `~` is expanded since 2026-09-21, but the existing
`.env` uses absolute paths and a token file is not the place to rely on a
recent fix.

The key goes in a file, never in an inline environment value — same rule as
the MCP bearer. `codestral-embed-2505` is the model that fits: 1536 dimensions
by default, exactly the nine `vector(1536)` columns, so no migration. Leave
`output_dtype` alone; pgvector stores float32 and int8 would be a second
change measured at the same time as the first.

**Do not restart the writers yet.** Between arming and the end of the reindex
the corpus does not match the configured model, and that is precisely the
state `check_embedding_model_drift.py` exists to refuse.

One consequence of the token file worth knowing before writing it:
`_resolve_shim_bearer` resolves it for **both** clients, the embedding one and
the reranker one. `BRAIN_EMBEDDING_API_KEY` is per-client; the file is not. So
pointing it at the hosted provider's key also presents that key to the local
reranker shim on every search. Measured 2026-09-21: the shim accepts any
bearer, including none, so nothing breaks — but a paid key travels to a service
that neither needs nor validates it, on a port the SEC2 note describes as
reachable from `brain-net` without a token.

### 5. Reindex

Every table, in one window. The batch API halves the price and this is not
time-sensitive work.

```bash
python scripts/regen_embeddings.py            # the six covered tables (6572 rows)
# mark plans stale, THEN re-run plan indexing       (indexed_plans + chunks, 2000 rows)
#   UPDATE indexed_plans SET freshness_status = 'stale';   -- else every file is skipped
#   feature assignments are NOT reopened: unchanged content skips resolve()
# gitlab_events (239 rows) is dead: leave it stale, deliberately
```

The order does not matter, but the completeness does. Track the three as a
checklist and do not restart the writers until every line is done. Plan
indexing restarts on its own when the MCP starts, so step 7 covers it — but
only if the rows were marked stale first.

**`regen_embeddings.py` must exit 0.** It does not stop on a failing batch: it
counts the rows, prints the error and carries on, so a single 429 drops up to
`--batch-size` rows and the run still reaches its summary. The exit code is the
only completeness signal, the script is idempotent, and a re-run costs cents.
A drift check will not rescue you here — a handful of stale rows does not move
a median.

### 6. Verify

```bash
python scripts/check_embedding_model_drift.py --per-type 8   # must exit 0
python bench/embedding_v2/run_retrieval_bench.py --candidate openai \
  --base-url https://api.mistral.ai --model codestral-embed-2505
```

Exit 0 on the drift check proves the sampled six tables were rewritten by the
configured model — `features` among them since 2026-09-21. It proves nothing
about the other three: plans and their chunks are covered by the re-run of plan
indexing, and `gitlab_events` is declared dead rather than checked.

The bench proves quality did not collapse. Compare against the qodo row of the
same report, on the same pool: a number from a different pool size is not a
comparison.

### 7. Restart the writers

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

- A reindex tool appearing for plans or their chunks outside the indexer,
  which would shrink the coverage-gap section again.
- A model column landing in the schema, which would turn the empirical drift
  check into a declared one.
- Provider pricing moving, which changes only the cost sentences.
- The pool growing, which lowers every recall figure quoted here.
