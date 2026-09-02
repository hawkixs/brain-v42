# Phase 0 baseline — PROJECTS + SESSIONS redesign

**Content item #1 of Phase 0.** Measurement, zero mutation.

## Replay

```bash
python3 docs/design/refonte-projets-sessions/baseline/snapshot.py
```

Writes `snapshot-<timestamp>.json` to this directory. `--stdout` to write nothing.

**Never copy forward a snapshot: replay it.** That is the reason this
directory exists. The documented failure mode of this project is citing a stale measurement —
the "479 artifacts" were correct on 2026-08-08 and stale ten days later; the
"10/59 contexts with NULL focus" were correct and did not prove what they were
made to say.

## Read-only, guaranteed by the engine

`snapshot.py` wraps the query in `BEGIN READ ONLY` / `COMMIT`. This is not an
intention, it's Postgres enforcing it. **Proven both ways on 2026-08-19**:

```
BEGIN READ ONLY; UPDATE brain_sessions … WHERE false;
  → ERROR: cannot execute UPDATE in a read-only transaction
BEGIN;           UPDATE brain_sessions … WHERE false;
  → passes (0 rows)
```

A single statement, hence a single transaction, hence a **consistent** snapshot:
measurements spread across several transactions could contradict each other without
anyone noticing.

## Every measurement carries its caveat

The JSON does not contain only numbers. Each measurement carries `proves` and
`does_not_prove`. A number without its caveat is a trap — that is exactly what
produced the two errors cited above. **Read `does_not_prove` before citing
`value`.**

## First measurement — 2026-08-19, head `045`

| Measurement | Value | What it says |
|---|---|---|
| 30-day capture | **18.2%** (77/424 closed) | Recomputes B3's "18%". New breakdown: `ended` 23.5%, `abandoned` 3.5% |
| 30-day attribution | **30.4%** (319/1050) | **DECLINING** — 34% on 2026-08-16. B3 is worsening, not stagnating |
| `client_key` | **465 distinct / 469 sessions** | B9 quantified: reuse ratio 1.01. One key per session, or nearly |
| NULL focus | **10/59, all "never written"** | `focus_revision = 0` AND never dated. **Zero erasure**, confirms Q13's premise |
| Ambiguity | **23 open out of 28** in a project with ≥2 | Ceiling, per project and not per (project, actor) pair |
| Colon mass | **537** across six keys | `red-shrik:agent` 314 still outweighs `red-shrik` 246 |
| Session indexes | **3, none on an actor** | See the conclusion below |
| `access_log` | **0 rows** | NORMAL regime — it's a buffer flushed on every flush, not an instrument |

## Conclusion on M-A's index decision — WRITTEN, and the question has changed

Exit criterion from PLAN §2: "the conclusion — index needed or not — is
**written**." Here it is, with its measurement.

`EXPLAIN (ANALYZE, BUFFERS)` from 2026-08-19, cardinality 469 rows / 28 `open`:

| Form | Plan | Time | Buffers |
|---|---|---|---|
| A · D5 skeleton + correlated subquery | Bitmap Index Scan on `idx_brain_sessions_project_status_started` | 0.157 ms | **36** |
| B · equality on a NON-indexed column | **Seq Scan**, 468 rows rejected | 0.255 ms | **63** |
| C · full scan (upper bound) | Seq Scan, 469 rows | 0.085 ms | 63 |

**Three takeaways, in order of importance.**

1. **The question asked has become moot, and it is the 2026-08-19 framing that made it
   so.** It asked whether `started_by_actor` needs indexing, because
   the D5 emitter had to filter on it on every outermost call. Under the
   `(project, connection)` key (ADR §0bis.2), **the emitter no longer filters on the
   actor**: it does a lookup by connection. `started_by_actor` becomes informative —
   display in `list`, ghost triage — and drops off the hot path. **It does not need an
   index.**

2. **A NEW indexing question replaces it, and this one is serious.** Measurement B
   shows it: an equality on an uncovered column forces a **Seq Scan of the whole
   table**, 63 buffers versus 2 for A's index scan — a factor of 30 as of
   today, on the hottest path there is, an outermost tool call. **The
   connection column must be indexed.**

3. **The natural form is a UNIQUE index**, and that is more than an optimization. The
   "exactly one by construction" property that the framing invokes would then be
   **enforced by the database**, not merely asserted by the design. Without it, an
   insertion regression could create two sessions on the same connection and the model
   would lie silently.

**Detail supporting (1):** in plan A, the correlated counting subquery — the one
that implements "exactly one" — consumes **27 of the 36 buffers**, i.e. 75%, and runs
once per candidate row (`loops=3`). Under the connection key, this subquery
**disappears entirely**. The framing did not just shift the indexing question: it
removed three quarters of the statement's cost.

**Honest caveat:** at 469 rows everything is sub-millisecond and nothing would be felt
today. This conclusion concerns the **shape** of the plan — index scan versus full
scan — not a measured pain point. It holds because the fleet is growing and the
path is hot, not because something is slow now.

**R1.5 consequence:** adding an index to `brain_sessions` breaks `expected_session_indexes`
(**CLOSED** list, `brain-v42-v4.sql:404-412`, checked at `:665` and `:687`) **and**
`SESSION_INDEX_DEFINITION_MD5`. This index therefore travels in M-A alongside the
regeneration of **both** v4 assets.

## Files

| File | Role |
|---|---|
| `queries.sql` | The 12 measurements, a single statement each, each with `proves` / `does_not_prove` |
| `explain.sql` | The cost of the D5 statement — three forms compared |
| `snapshot.py` | Replays and dates. Read-only enforced by the transaction |
| `snapshot-*.json` | Dated snapshots. **History, not source of truth** — replay it |
