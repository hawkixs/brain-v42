# Per-agent metrics re-key + cross-repo /metrics contract — Implementation Plan (Plan 2 of 3)

> **For agentic workers:** REQUIRED SUB-SKILL: superpowers:subagent-driven-development. ONE subagent per task, ≤3 concurrent (a transient throttle trips on big fan-outs). Steps use `- [ ]` checkboxes.

**Goal:** Carry per-AGENT observability (tool/error/latency/cost) into the request layer of the single HTTP server, re-keying `process_metrics` from `pid` to `(agent_name, pid)` — **migration-safe through the stdio↔http coexistence window** — and expose the new shape to red-monitor/red-data WITHOUT silently breaking the live cockpit.

**Architecture:** The `X-Brain-Agent` header is read **directly inside the `instrument_tool` wrapper** via `get_http_headers()` (same task as `record_tool_call`; no ASGI-middleware contextvar relay — that collapses to `unknown` under anyio `start_soon`). The collector gains an agent dimension (`dict[agent_name][tool_name]`); process-global counters (embedding/reranker/graph/cost/buckets/RSS) live in a reserved `agent_name='_process'` row (not duplicated per agent). `process_metrics` PK becomes the **composite `(agent_name, pid)`** so concurrent stdio writers + the http `unknown` bucket never collide on `ON CONFLICT` during migration; collapse to a bare `agent_name` PK happens only in Plan 3 after the last stdio agent retires.

**Depends on:** Plan 1 (HTTP transport + `get_http_headers` available under HTTP). **Precedes:** Plan 3 (fleet cutover). Verified design: `docs/superpowers/specs/2026-06-29-mcp-http-single-server-design.md` (items C2, H6, H7 + Cross-repo §).

## Global Constraints (inherit Plan 1's + these)

- **`X-Brain-Agent` is a spoofable observability LABEL.** Read lowercase `x-brain-agent` (`get_http_headers()` lowercases names); fallback `"unknown"`; never raises (`get_http_headers()` returns `{}` off-HTTP). NEVER use `get_http_request()` (raises under stdio).
- **NO locks.** `record_tool_call` has no `await` → atomic on the single asyncio loop. An `asyncio.Lock` would force it `async` and ripple through `instrument.py` + every call site — forbidden.
- **Composite PK `(agent_name, pid)`** through this plan. `agent_name` is a non-null sentinel (`'unknown'`/`'_process'` are literals, never NULL). Migration is **additive** (nullable column + backfill + dual-write), never an in-place PK swap against live writers.
- **Process-global counters go to the `_process` row only** (embedding/reranker/graph/cost/latency-buckets/RSS) — emitting them per-agent ×N over-counts in `collect_process_metrics`.
- **`active_processes` keeps its name + meaning** for back-compat; a NEW `active_agents` field carries the per-agent count. No silent semantic flip on the live dashboard.
- TDD, frequent commits. Branch: `feat/mcp-http-metrics-rekey` off `feat/mcp-http-server-foundation` (or main after Plan 1 merges).

## File Structure (Plan 2)

| File | Responsibility | Action |
|---|---|---|
| `src/brain_v42/metrics/instrument.py` | read X-Brain-Agent, pass agent to record_tool_call | Modify (~25-36) |
| `src/brain_v42/metrics/collector.py` | agent dimension on tool stats; `_process` row split | Modify (39, 107-123, 152-156, 298+, 348+) |
| `src/brain_v42/metrics/flusher.py` | upsert one row per agent; composite-PK conflict; cleanup | Modify (119-160) |
| `src/brain_v42/db/tables.py` | `process_metrics` agent_name col + composite PK | Modify (~409) |
| `alembic/versions/025_*_process_metrics_agent.py` | additive migration | **Create** |
| `src/brain_v42/metrics/collector_db.py` | `active_agents` field; exclude `_process` from agent count | Modify (~175) |
| `tests/integration/metrics/test_per_agent_metrics.py` … | tests | Create |

---

## Batch 1 — Agent identity at the request layer (H6)

### Task 1.1: Read X-Brain-Agent inside instrument_tool and thread it to record_tool_call

**Files:** Modify `src/brain_v42/metrics/instrument.py` (20-38) + `collector.py:record_tool_call` (107); Test `tests/integration/metrics/test_agent_attribution.py`.
**Interfaces:** Produces: `record_tool_call(tool_name, latency_ms, error=False, agent="unknown")`; `instrument_tool` reads the header and passes `agent`.

- [ ] **Step 1: gitnexus impact** on `record_tool_call` and `instrument_tool` (both High-traffic).
- [ ] **Step 2: Write the failing CONCURRENT test** (a serial test false-greens H6). Start the Plan-1 HTTP server; fire two tool calls concurrently with distinct headers via `asyncio.gather`; assert each agent's bucket is isolated:
```python
@pytest.mark.asyncio
async def test_concurrent_agents_land_in_distinct_buckets():
    # two fastmcp.Client HTTP sessions; `call` is a REAL client round-trip helper (not a stub),
    # headers X-Brain-Agent: red-shrik / red-codex
    await asyncio.gather(call(client_a, "brain_search", ...), call(client_b, "brain_search", ...))
    # Batch 1 only reshapes the raw _tool_stats store; the agent-keyed get_flush_data() shape lands
    # in Task 2.1 — assert the RAW store here, else this test can't green after Step 4 (code-quality HIGH).
    assert "red-shrik" in collector._tool_stats and "red-codex" in collector._tool_stats
    assert "brain_search" in collector._tool_stats["red-shrik"]
    assert "red-shrik" not in collector._tool_stats["red-codex"]   # no cross-contamination
```
- [ ] **Step 3: Run — expect FAIL** (no agent dimension yet).
- [ ] **Step 4: Implement** — in `instrument.py` wrapper (runs in-task, in `finally`):
```python
from fastmcp.server.dependencies import get_http_headers
# inside wrapper, before collector.record_tool_call(...):
agent = (get_http_headers() or {}).get("x-brain-agent", "unknown")
collector.record_tool_call(tool_name, latency_ms, error=error, agent=agent)
```
Add `agent: str = "unknown"` param to `record_tool_call` (default keeps stdio + existing callers working). Bucketing logic lands in Task 2.1; for now store under `self._tool_stats.setdefault(agent, {})`.
- [ ] **Step 5: Run — expect PASS** + `pytest tests/unit` (stdio path → `unknown` bucket, no regression).
- [ ] **Step 6: Commit** `feat(metrics): attribute tool calls to X-Brain-Agent in the instrument wrapper (H6)`

---

## Batch 2 — Collector agent dimension + `_process` split (H7)

### Task 2.1: Re-shape `_tool_stats` to `dict[agent_name][tool_name]`; process-globals to `_process`

**Files:** Modify `collector.py` (39, 107-156, `get_flush_data` ~298, `get_metrics` ~348); Test `tests/unit/metrics/test_collector_agent_dim.py`.
**Interfaces:** Produces: `get_flush_data()` returns `{agent_name: {tools, embedding?, reranker?, graph?, cost?, buckets?}}` where process-global blocks appear ONLY under `"_process"`.

- [ ] **Step 1: Write the failing test** — record two agents' tool calls + one embedding call; assert `get_flush_data()` has `red-shrik`/`red-codex` tool rows AND the embedding/reranker/graph/cost/bucket blocks appear under `"_process"` ONLY (not duplicated per agent):
```python
def test_process_globals_isolated_to_process_row():
    c = MetricsCollector(...)
    c.record_tool_call("brain_search", 50, agent="red-shrik")
    c.record_tool_call("brain_learn", 50, agent="red-codex")
    c.record_embedding_request(20)        # process-global
    c.record_reranker_call(...)           # process-global pseudo-tool
    fd = c.get_flush_data()
    assert set(fd) >= {"red-shrik", "red-codex", "_process"}
    assert "embedding" in fd["_process"] and "embedding" not in fd["red-shrik"]
    # pseudo-tools (_reranker/_graph/_cost/_buckets) appear ONLY on _process, never per-agent (×N guard)
    assert "_reranker" not in fd["red-shrik"]["tools"] and "_reranker" not in fd["red-codex"]["tools"]
    assert fd["red-shrik"]["tools"].keys() == {"brain_search"}
```
- [ ] **Step 2: Run — expect FAIL.**
- [ ] **Step 3: Implement** — `self._tool_stats: dict[str, dict[str, dict[str, Any]]]` keyed `[agent][tool]`. `record_tool_call(tool_name, latency_ms, error, agent="unknown")` writes under `self._tool_stats.setdefault(agent, {})`. KEEP `_embedding_stats/_reranker_stats/_graph_stats/_latency_buckets/_cost_by_model/_tool_latencies/_counter_snapshots` process-global (no agent key). `get_flush_data()` emits one entry per agent (tools only) + a `"_process"` entry carrying the global blocks + RSS. **The pseudo-tool keys the flusher injects today (`_reranker/_graph/_cost/_buckets`, flusher.py:85-114) must land ONLY in the `_process` entry's tool dict — never per-agent, else `collect_process_metrics` sums them ×N across agent rows (architect H7a).** Rewrite `snapshot_counters()` (collector.py:154) to sum over the NESTED `self._tool_stats[agent][tool]["calls"/"errors"]` across all agents — the current flat-dict sum breaks under the reshape; fix it in THIS task or unit tests go red. **No locks** (record_tool_call has no `await`).
- [ ] **Step 4: Run — expect PASS** + `pytest tests/unit/metrics`. **Step 5: Commit** `refactor(metrics): per-agent tool stats + _process row for global counters (H7)`

### Task 2.2: Dirty-set tracking so idle agents don't get reaped while the server is alive

**Files:** Modify `collector.py` (add `_dirty_agents: set[str]`); Test in the same file.

- [ ] **Step 1: Write the failing test** — record agent A, flush, then record only agent B; assert the flush payload marks only B dirty (A's row should NOT be re-upserted every cycle, else its `updated_at` never ages and the 1h cleanup never fires).
- [ ] **Step 2: Run — expect FAIL. Step 3: Implement** — `record_tool_call` adds `agent` to `self._dirty_agents`; `get_flush_data(dirty_only=True)` returns only dirty agents **plus `_process` UNCONDITIONALLY** (record_embedding/reranker/graph/cost do NOT touch `_dirty_agents`, so a pure-embedding cycle would otherwise skip `_process` and freeze its RSS/global counters — architect MEDIUM); flusher clears the set after a successful upsert. **Step 4: PASS. Step 5: Commit** `feat(metrics): flush only dirty agents + always _process (idle agents age out via 1h cleanup, H7b)`

---

## Batch 3 — Migration-safe re-key (C2)

### Task 3.1: Alembic 025 — additive `agent_name` + composite PK

**Files:** Create `alembic/versions/025_process_metrics_agent_name.py`; Modify `src/brain_v42/db/tables.py` (~409); Test `tests/integration/db/test_migration_025.py` + the alembic canary.

- [ ] **Step 1: gitnexus impact** on the `process_metrics` table object.
- [ ] **Step 2: Write the failing test** — upgrade to 025; assert `process_metrics` PK is `(agent_name, pid)`, `agent_name` is NOT NULL with server_default `'unknown'`; downgrade restores PK `(pid)`. Re-run the alembic head canary (head 024→025).
- [ ] **Step 3: Run — expect FAIL** (revision absent).
- [ ] **Step 4: Implement** the migration **additively**: `add_column('process_metrics', Column('agent_name', String, nullable=True))` → `UPDATE process_metrics SET agent_name='unknown' WHERE agent_name IS NULL` → `alter_column(... nullable=False, server_default='unknown')` → drop PK `(pid)` → add PK `(agent_name, pid)`. Update `tables.py:409` to the composite PK + `agent_name` column. Downgrade reverses (drop composite PK, drop column, restore `(pid)`).
- [ ] **Step 5: Run — expect PASS** + canary green. **Step 6: Commit** `feat(db): migration 025 — process_metrics composite PK (agent_name, pid), additive (C2)`

### Task 3.2: Flusher — one upsert per agent, composite-PK conflict, fixed stop()/cleanup

**Files:** Modify `flusher.py` (119-160, stop() ~48-66); Test `tests/integration/metrics/test_flusher_per_agent.py`.

- [ ] **Step 1: Write the failing test** — run one flush with two dirty agents + `_process`; assert THREE rows in `process_metrics` keyed `(agent_name, pid)`; a second flush of only agent B updates B's `updated_at` and leaves A's stale (so A ages out after 1h). Assert `stop()` does NOT delete other agents' rows.
- [ ] **Step 2: Run — expect FAIL.**
- [ ] **Step 3: Implement** — iterate `get_flush_data(dirty_only=True)`; per entry upsert with `agent_name` + `pid`, `ON CONFLICT (agent_name, pid) DO UPDATE`:
```sql
INSERT INTO process_metrics (agent_name, pid, started_at, updated_at, tool_stats, embedding_stats, memory_rss_bytes)
VALUES (:agent_name, :pid, :started_at, NOW(), CAST(:tool_stats AS jsonb), CAST(:embedding_stats AS jsonb), :rss)
ON CONFLICT (agent_name, pid) DO UPDATE SET updated_at = NOW(), tool_stats = CAST(:tool_stats AS jsonb), embedding_stats = CAST(:embedding_stats AS jsonb), memory_rss_bytes = :rss
```
embedding_stats/RSS only on the `_process` row. **Fix `stop()`**: delete ONLY this server's own rows by `(agent_name, pid)` for the agents IT wrote — or skip the per-shutdown delete entirely (the 1h cleanup handles it); never `DELETE WHERE pid=:pid` blanket (would wipe other agents on a shared server). Keep the `>1h` cleanup unchanged (now ages idle agent rows correctly). **Deploy ordering (architect MEDIUM):** migration 025 (Task 3.1) must be live on EVERY running server before this `ON CONFLICT (agent_name, pid)` flusher ships — during Plan 3 dual-run an OLD flusher still firing `ON CONFLICT (pid)` after the PK swap raises "no unique constraint matching ON CONFLICT" (caught → one lost flush cycle, tolerable, not data loss). Sequence: deploy 025 everywhere → THEN ship this flusher.
- [ ] **Step 4: Run — expect PASS** + `pytest tests/integration/metrics`. **Step 5: Commit** `feat(metrics): flusher upserts one row per agent on composite PK; safe stop() (C2/H7)`

---

## Batch 4 — Cross-repo /metrics contract (red-triad)

### Task 4.1: `collector_db` — add `active_agents`, keep `active_processes`; exclude `_process`

**Files:** Modify `src/brain_v42/metrics/collector_db.py` (~146-175); Test `tests/unit/metrics/test_collector_db_active_agents.py`.

- [ ] **Step 1: gitnexus impact** on `collect_process_metrics` (consumed by `server.py:122` cross_process → `/metrics` JSON → red-monitor).
- [ ] **Step 2: Write the failing test** — seed `process_metrics` with rows for `red-shrik`, `red-codex`, `_process` (one pid); assert the cockpit payload exposes `active_agents == 2` (excludes `_process`) AND retains `active_processes` (now = distinct pids = 1) for back-compat; assert per-agent tool/error/latency aggregates are correct and process-globals come from the `_process` row only (no ×N).
- [ ] **Step 3: Run — expect FAIL.**
- [ ] **Step 4: Implement** — `collect_process_metrics` groups by `agent_name`; `active_agents = count(distinct agent_name WHERE agent_name != '_process' AND updated_at > now()-60s)`; `active_processes = count(distinct pid)` (back-compat). Surface both in the cross_process block. Aggregate process-globals from `_process`.
- [ ] **Step 5: Run — expect PASS.** **Step 6: Commit** `feat(metrics): expose active_agents (per-agent) + retain active_processes (back-compat)`

### Task 4.2: /metrics contract test + cross-repo coordination note (red-monitor + red-data)

**Files:** Create `tests/integration/metrics/test_metrics_contract.py`; Doc `docs/superpowers/plans/_plan2-redtriad-coordination.md`.

- [ ] **Step 1: Write the contract test** — `GET :9200/metrics` (sidecar) returns JSON whose `cross_process` block has BOTH `active_processes` (int) and `active_agents` (int), and a per-agent breakdown list; lock the shape so a future change can't silently drop a field.
- [ ] **Step 2: Run — implement the sidecar surface if needed; expect PASS.**
- [ ] **Step 3: Author the coordination doc** — the EXTERNAL consumers that must adapt (separate repos, separate PRs, NOT auto-changed here):
  - `red-monitor`: cockpit contract `docs/specs/2026-04-19-brain-cockpit-api-contract.md`; frontend `BrainInfra.jsx:141-142` (render `active_agents` alongside `active_processes`); `red-agent ServiceCollector` (5s scrape) — confirm it tolerates the new field.
  - `red-data`: bronze/silver `service_name=brain-v42` ingestion (`tests/test_silver_metrics.py`) — confirm the new per-agent shape doesn't break the silver schema.
  - **Sidecar/process split (architect CRITICAL):** the `:9200` metrics sidecar is a SEPARATE process (`metrics/__main__.py`) with its OWN `MetricsCollector`; the re-keyed in-memory collector lives in the MCP HTTP **server** process. Per-agent data reaches the sidecar ONLY via the DB (`process_metrics` → `collect_process_metrics` → the `cross_process` block). The `/metrics` TOP-LEVEL `tools`/`buckets`/`embedding_service` blocks reflect the SIDECAR's own near-empty collector — red-monitor MUST read per-agent data from `cross_process`, NOT top-level `tools` (verify `BrainInfra.jsx`). The contract test (Step 1) already targets `cross_process` — keep it there.
  - **Open Q7 (`:9200` bind `0.0.0.0`→`127.0.0.1`) is DEFERRED out of this migration** — red-monitor scrapes `:9200` cross-host over the LAN, so tightening the bind first requires moving red-monitor to a local agent/tunnel. Track as a separate hardening ticket; do NOT bundle it here (closes the requirements-judge Q7 gap explicitly).
  - **Sequencing:** land brain-v42 Plan 2 (additive — old fields retained) FIRST so external consumers keep working unchanged; their adoption of `active_agents` is a follow-up, non-blocking.
- [ ] **Step 4: Commit** `test(metrics): /metrics contract guard + red-triad coordination doc`

---

## Plan 2 Self-Review

- **Spec coverage:** C2 (Batch 3 composite PK additive), H6 (Batch 1 concurrent test), H7 (Batch 2 agent dim + `_process` split + dirty-set; Batch 3 flusher/stop fixes), Cross-repo § (Batch 4 active_agents + contract test + coordination doc). ✅
- **No-locks constraint** explicit in Task 2.1. **`_process` over-count** addressed (2.1/3.2/4.1). **Idle-agent aging** (2.2). **stop() blanket-delete** fixed (3.2).
- **Type consistency:** `record_tool_call(..., agent=)`, `get_flush_data(dirty_only=)`, `_tool_stats[agent][tool]`, `agent_name`/`_process` sentinels, composite PK `(agent_name, pid)` used consistently.
- **RSS honesty:** stays a `_process`-row gauge (spec), never per-agent.

## Execution handoff

Plan 2 follows Plan 1's merge. Recommended: subagent-driven, ONE task at a time, ≤3 concurrent reviewers. Plan 3 (fleet cutover) collapses the composite PK to bare `agent_name` only AFTER the last stdio client is retired.
