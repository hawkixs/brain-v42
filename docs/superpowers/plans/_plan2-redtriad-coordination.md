# Plan 2 — Red-Triad Coordination (brain-v42 Metrics Re-key)

**Status:** informational — for red-monitor and red-data maintainers  
**brain-v42 branch:** `feat/mcp-http-metrics-rekey` (Plan 2, Tasks 1–4)  
**Date:** 2026-06-30

---

## What changed in brain-v42

Plan 2 adds per-agent metric tracking to the `/metrics` endpoint.  
The change is **additive** — all existing fields are retained unchanged.

### New fields in `cross_process` (at `GET :9200/metrics`)

```json
{
  "cross_process": {
    "active_processes": 1,
    "active_agents": 2,
    "total_memory_rss_bytes": 12345,
    "tools": { ... },
    "embedding": { ... },
    "by_agent": {
      "red-shrik": {
        "calls": 10,
        "errors": 1,
        "recent_errors": 0,
        "avg_latency_ms": 50.0
      },
      "red-codex": {
        "calls": 8,
        "errors": 0,
        "recent_errors": 1,
        "avg_latency_ms": 36.25
      }
    }
  }
}
```

| Field | Type | Notes |
|---|---|---|
| `active_processes` | `int` | Back-compat: distinct active pids |
| `active_agents` | `int` | NEW: distinct real agent names (excludes `_process`) |
| `by_agent` | `dict[str, AgentEntry]` | NEW: per-agent breakdown |
| `by_agent[name].calls` | `int` | Total tool calls for this agent |
| `by_agent[name].errors` | `int` | Total errors |
| `by_agent[name].recent_errors` | `int` | Errors in last 1h window |
| `by_agent[name].avg_latency_ms` | `float` | Rounded to 2dp (distinct from global `tools` 1dp) |

---

## Sidecar / process split — CRITICAL

The `:9200` metrics sidecar (`brain_v42.metrics.__main__`) is a **separate
process** with its own in-memory `MetricsCollector`.

- The re-keyed collector (per-agent tracking via `x-brain-agent` header) lives
  in the **MCP HTTP server process** — it is flushed every 5s to the
  `process_metrics` PostgreSQL table.
- The sidecar reads `process_metrics` via `collect_process_metrics` and
  assembles the `cross_process` block from the DB.
- The **top-level** `tools`, `buckets`, and `embedding_service` blocks in
  `/metrics` reflect the sidecar's own near-empty in-memory collector.

**Consequence for consumers:** read per-agent data from `cross_process`,
NOT from the top-level `tools` block.

```
MCP HTTP process                 Sidecar (:9200)
─────────────────────            ──────────────────────────────
MetricsCollector (per-agent)     MetricsCollector (own, near-empty)
    │ flush every 5s                  │ collect_process_metrics()
    ▼                                 ▼
process_metrics (PG)  ──────────►  cross_process block in /metrics
```

---

## red-monitor — required follow-ups

**Files of interest:**
- `docs/specs/2026-04-19-brain-cockpit-api-contract.md` — cockpit contract
- `src/frontend/BrainInfra.jsx` (or equivalent) — renders active processes
- `src/collectors/ServiceCollector.*` — 5s scrape of `:9200/metrics`

**Actions (separate PR, non-blocking):**

1. **Render `active_agents`** alongside `active_processes` in the BrainInfra
   card. The field is already present in `cross_process`; the UI just needs
   to reference it.

2. **Per-agent table/chart**: `cross_process.by_agent` is now populated.
   red-monitor can surface it as an expandable panel (agent name → calls /
   errors / avg latency). This is optional/deferred.

3. **Confirm scrape tolerance**: the `ServiceCollector` 5s scrape issues a GET
   to `:9200/metrics`. The new `active_agents` and `by_agent` fields appear
   in `cross_process` — no existing field is removed. The scraper should
   tolerate new fields without changes. Verify the scraper does not assert an
   exact field set.

4. **Do NOT read per-agent data from top-level `tools`** — that block is the
   sidecar's own in-memory aggregation (near-empty). Per-agent data is only
   in `cross_process.by_agent`.

---

## red-data — required follow-ups

**Files of interest:**
- `tests/test_silver_metrics.py` — silver-layer schema validation for `service_name=brain-v42`

**Actions (separate PR, non-blocking):**

1. **Verify silver schema tolerance**: the bronze/silver ingestion pipeline
   receives the `/metrics` JSON. New keys (`active_agents`, `by_agent`) are
   additive — they should not break an existing silver schema that selects a
   known column subset. Confirm `test_silver_metrics.py` passes without changes.

2. **Adopt `by_agent` in silver** (optional follow-up): if the pipeline
   currently flattens `cross_process.tools` into silver rows, it can
   additionally flatten `cross_process.by_agent` to get per-agent time-series.
   Not required to unblock Plan 2 merge.

---

## Open Q7 — `:9200` bind address (DEFERRED)

The sidecar currently binds `0.0.0.0:9200` (all interfaces). Tightening to
`127.0.0.1:9200` would prevent cross-host access, but red-monitor scrapes
`:9200` over the LAN (192.168.1.12 → 192.168.1.11).

Hardening the bind first requires either:
- Moving the red-monitor scraper to a local agent on the same host, or
- Setting up a local proxy / SSH tunnel.

**Decision: defer to a separate hardening ticket.** Do NOT bundle with Plan 2.

---

## Sequencing summary

| Step | Repo | Blocking? |
|---|---|---|
| Plan 2 merge (brain-v42) | brain-v42 | — |
| ServiceCollector tolerance check | red-monitor | Non-blocking |
| `active_agents` render in BrainInfra | red-monitor | Non-blocking |
| Silver schema tolerance check | red-data | Non-blocking |
| `by_agent` silver ingestion | red-data | Non-blocking |
| `:9200` bind hardening | brain-v42 + red-monitor | Separate ticket |

brain-v42 Plan 2 is additive: all existing fields retained. External consumers
keep working unchanged after Plan 2 merges. Adoption of `active_agents` and
`by_agent` is a separate, non-blocking follow-up for each consumer.
