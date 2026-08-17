# Single long-running HTTP MCP server (shared asyncpg pool) — verified design

**Status:** brainstorm → design, **adversarially verified + research-validated**. Do NOT implement until the Open Questions are resolved and a plan is approved.
**Date:** 2026-06-29
**Project key:** `brain-v42`
**Direction decision:** `e92fa375-d3d5-4e50-a6e3-de825d336b1f` (active).

## Provenance

Two background multi-agent workflows, both adversarially checked against the **real installed code** (`.venv`) and **2026 primary sources** (PyPI, NVD/GHSA, gofastmcp.com, modelcontextprotocol.io):

- **Design verification** — 9 lenses → adversarial refutation (52–63/66 High/Critical findings survived) → clean synthesis.
- **Tech-landscape research** — 6 angles → clean synthesis. **Verdict: "the latest tech VALIDATES the chosen direction, no fundamental change required."**

## Authoritative installed versions (via `.venv/bin/python` importlib.metadata — NOT pip)

```
fastmcp 3.1.0   mcp 1.26.0   starlette 0.52.1 (VULNERABLE, BadHost)   uvicorn 0.41.0
sqlalchemy 2.0.47   asyncpg 0.31.0   opentelemetry-api 1.39.1   opentelemetry-sdk 1.40.0
structlog 25.5.0   neo4j 6.1.0   (fastmcp-slim NOT installed)   interpreter: python3.14
```
> ⚠️ A research agent reported "fastmcp 3.4.2 / sqlalchemy 2.0.50 already installed" — **that was the documented pip-wrong-env trap** (it read a different environment). The authoritative install is **fastmcp 3.1.0 + starlette 0.52.1**, so the bump (`3.1→3.4.1`) and the `starlette>=1.0.1` pin are **real security must-dos, not cosmetic**.
> Current pyproject pins (loose): `fastmcp>=3.1,<4`, `sqlalchemy[asyncio]>=2.0`, `asyncpg>=0.29`, `neo4j>=5.0,<7`. No `mcp`/`starlette`/`otel` pins.

---

## Research verdict (validates direction; YAGNI-rejects alternatives)

Direct localhost streamable-HTTP (FastMCP, `stateless_http`, 127.0.0.1) is the right baseline. **Rejected (real but overkill / dead-end for our many-clients-one-backend localhost topology):**
- **UDS** — dead end at the *client*: no URL scheme for a `.sock` (WHATWG-URL gap); Claude Code's http transport needs a URL.
- **fastmcp-remote / stdio shim** — reintroduces one process per session (the exact thing we kill). FastMCP 3.4's headline feature is the wrong direction for us.
- **MCP gateways/multiplexers** — solve the inverse problem (federate many servers → one client); never pool DB connections.
- **WebSocket** — moot for request/response; loses `--transport` + OAuth.
- **pgbouncer/pgcat** — asyncpg's own FAQ: native pool wins for one co-located service; txn-mode breaks asyncpg prepared statements.
- **OAuth 2.1 / CORS allow_origins / OTLP wire exporters** — over-engineering for loopback (spec: auth is SHOULD not MUST); OTLP reintroduces the protobuf<7 vs grpcio conflict.
- **mcp SDK v2 / 2026-07-28 stateless RC / Tasks primitive** — alpha/RC, breaking, lands after our window. **Watch-only.** Our 2025-11-25-based design is forward-compatible.

**Sharpening insights (not redirects):**
1. The actual saturation fix is the **explicit pool ceiling (20+10) + the missing `pool_recycle=1800`** — that's the load-bearing edit, not the transport choice.
2. Per-agent observability is **mostly already solved**: FastMCP 3.3+ emits tool/error/latency spans natively (protobuf-free api/sdk). We only ADD a thin middleware to stamp the agent. **RSS must be honestly demoted to a process-level gauge** (one shared process can't decompose resident memory per agent).
3. stdio→HTTP opens a real new attack surface → make localhost controls **explicit and tested**.

---

## Corrected architecture

One long-lived process `brain-mcp-http.service` (systemd **--user**), bound **127.0.0.1:PORT**, streamable HTTP, `stateless_http=True, json_response=True`, **one shared asyncpg pool initialized in the FastMCP lifespan**. All TRUE MCP-stdio brain clients become HTTP clients via `claude mcp add --transport http`. Per-request agent identity rides an **`X-Brain-Agent` header** — **metrics/observability ONLY**.

### Two identities — DO NOT conflate
- **`project_key`** (tool *argument*; red-shrik hardcodes `red-shrik:agent`) — **data scoping**, unchanged. The migration does NOT touch it. Add a test: header changes don't move which `project_key` a write lands in.
- **`X-Brain-Agent`** (HTTP *header*, new) — **metrics bucketing only**. Client-supplied → **spoofable → never an authz boundary**.

---

## MUST-FIX before implementation (verified against installed code)

### CRITICAL
- **C1 — `FastMCP(stateless_http=True, json_response=True)` crashes at boot.** REMOVED `__init__` kwargs in v3 → `TypeError` (server.py:107-138). → pass to `mcp.run_async(transport="http", host=, port=, stateless_http=True, json_response=True)` or env `FASTMCP_STATELESS_HTTP`/`FASTMCP_JSON_RESPONSE`. Test: server constructs + run_async accepts these.
- **C2 — Bare `agent_name` PK corrupts metrics during the stdio+http coexistence window.** Under stdio there is no header → bucket `unknown`; every concurrent stdio process + the http `unknown` bucket collide on `ON CONFLICT(agent_name='unknown')`, clobbering each other (the pid-keyed isolation that made concurrent stdio safe is lost the moment the PK moves). → **composite PK `(agent_name, pid)`** through migration; collapse to bare `agent_name` only **after the last stdio agent is retired**. `unknown` must be a literal non-null sentinel (PK can't be NULL). Migration additive (nullable `agent_name` first, dual-write), not an in-place PK swap against live writers.
- **C3 — The stale-MCP reaper SIGTERMs the new HTTP server → 15-min flap loop.** `brain-mcp-http.service` runs `python -m brain_v42.mcp.server` → matches `_BRAIN_SERVER_RE` (reap_stale_mcp.py:60); long-lived → crosses `max_age_sec=48h`; over-cap branch reaps it even as newest member (:139-141). Every 15 min + `Restart=always` = guaranteed fleet-wide flap once `--execute` is on. → reaper carve-out BEFORE migration (sentinel arg / detect `BRAIN_MCP_TRANSPORT=http` / protect `systemd --user`-rooted procs). Test: ps snapshot with the HTTP service → `select_stale_mcp_sessions` never returns it even at age>48h.

### HIGH
- **H1 — "Host/Origin validation ON" is FALSE for jlowin/fastmcp.** `create_streamable_http_app` builds `StreamableHTTPSessionManager` WITHOUT `security_settings` → `TransportSecurityMiddleware(None)` → `enable_dns_rebinding_protection=False` (http.py:301-307, mcp/server/transport_security.py:39-42). The SDK 1.23 localhost auto-enable lives in `mcp.server.fastmcp.FastMCP`, never on this code path. `http_app()`/`run_http_async()` expose no security param. → **write a custom ASGI middleware** via `http_app(middleware=[...])` that 421s on Host ∉ {127.0.0.1:*, localhost:*} and 403s on disallowed Origin; **explicitly populate the allowlist** (empty → rejects everything, python-sdk#1798). Integration test forging a bad Host.
- **H2 — `starlette>=1.0.1` (BadHost) NOT guaranteed by the named bumps; currently unmet.** mcp 1.28.1 floors only `starlette>=0.27/>=0.48`. The `>=1.0.1` floor exists only in `fastmcp-slim[server]`, added in **3.4.1 not 3.4.0** (`>=3.4,<4` permits 3.4.0). red-shrik proves it leaks (fastmcp 3.4.2 + starlette 1.0.0). Installed = 0.52.1 (vulnerable). → explicit `starlette>=1.0.1` + `fastmcp>=3.4.1` in pyproject; CI assert `version('starlette')>=1.0.1`. Bump pulls a **new transitive dep `uncalled-for>=0.2.0`** (DI engine) — pin in lock.
- **H3 — No graceful drain on SIGTERM.** FastMCP hardcodes uvicorn `timeout_graceful_shutdown=0` (transport.py:257-262) → in-flight `brain_*` writes **cancelled, not drained** every restart. → `run_http_async(..., uvicorn_config={"timeout_graceful_shutdown": 10})` + systemd `TimeoutStopSec` higher.
- **H4 — "Minimal 2-line transport change" is false.** stdio path wraps `run_async` in a bespoke `run_with_background_tasks()` (asyncio.run + asyncio.wait + custom `loop.add_signal_handler`, server.py:96-107,455-546). `run_http_async` enters its OWN lifespan AND uvicorn installs its OWN signal handlers → overrides our `shutdown_event`; the finally-cleanup (flusher stop, dispose_engine, neo4j close, :528-538) may be skipped or double-run → **leaks the asyncpg pool across restarts** (the drift we're fixing). → move flusher lifecycle + dispose into a **FastMCP `lifespan=`** so it runs regardless of transport.
- **H5 — Background flushers may silently never start under `run_http_async()`** (they live inside the stdio wrapper). → same fix as H4 (host in lifespan). This is the load-bearing reason H4 must be done.
- **H6 — ASGI-middleware→contextvar relay silently collapses all metrics to `unknown`.** MCP dispatches via anyio `tg.start_soon`; ContextVars copy at spawn → a value set in the outer ASGI task may not reach the handler task (or leaks across agents). → **read `get_http_headers().get("x-brain-agent","unknown")` DIRECTLY inside `instrument_tool`'s wrapper** (instrument.py:25-36; same task as request_ctx). Key on **lowercase** `x-brain-agent` (get_http_headers lowercases, dependencies.py:461). Use `get_http_headers()` (returns `{}` off-HTTP) NOT `get_http_request()` (raises in stdio). Test: **concurrent** 2-agent `asyncio.gather` asserting distinct non-`unknown` buckets (a serial test false-greens).
- **H7 — Re-key is a pervasive collector refactor, not "add a column."** Collector keyed by `tool_name` only (collector.py:39); `record_tool_call` has no agent arg (:107). Demux touches record_tool_call, get_flush_data(298), get_metrics(348), flusher SQL, pseudo-tools `_reranker/_graph/_cost/_buckets` (flusher.py:85-114). Three breakages: (a) **process-level singletons double-count** (embedding/reranker/graph/cost have no agent dim → ×N in collect_process_metrics:146-157) → keep them in a separate `agent_name='_process'` row; (b) **idle agents never age out** if the flusher upserts all known agents every 30s → upsert only agents **dirty since last flush**; (c) `stop()` deletes by pid (flusher.py:60) → delete by agent_name or skip. **No lock** (record_tool_call has no `await`, atomic on the loop; asyncio.Lock would ripple async). Tests: two-agent upsert, idle-agent-survives-1h, no double-count.
- **H8 — No env/config plumbing for transport/port/host.** `config.py` Settings has no `brain_mcp_transport`/mcp host/port (only `metrics_port=9200`, `metrics_host='0.0.0.0'`, :62-64); `extra='ignore'` (:36) silently drops env. → add validated pydantic fields, **host default `127.0.0.1`** (not 0.0.0.0), wire through the transport branch.
- **H9 — red-shrik cutover is a CODE change, not config.** Its brain client is a hand-rolled line-delimited JSON-RPC stdio client (`mcp_client.py`) spawned via `MCPClient(command=cfg.memory.mcp_command)` (entry.py:236-238), pinned in `shrik.yaml:35-41` + `models.py:43-48`. → replace `MCPClient` with an httpx/`fastmcp.Client` streamable transport injecting `X-Brain-Agent`, preserve tuned retry/backoff (test_mcp_client_retry.py, 35s embedding backoff), keep `project_key="red-shrik:agent"` as a tool arg. Re-run test_brain_bootstrap.py + test_mcp_client_retry.py before rollout.
- **H10 — Header-only identity collapses to one bucket: the global `~/.claude.json` brain-v42 entry is shared by ~41 projects.** One global `--header` makes every session in every project report the same agent_name (only red-writer has a per-project override). → generate **one per-project HTTP `mcpServers` entry with a distinct `X-Brain-Agent`**; log/assert when the header resolves to `unknown`. Verify empirically (CLI 2.1.195) that static `--header` persists per-server (`type:http,url,headers`) and rides every JSON-RPC POST.

---

## Cross-repo blast radius (group: red-triad) — a JSON-contract break, not an internal refactor

There is **no process-tree parse in red-monitor to "drop"** (phantom task). red-monitor scrapes `GET localhost:9200/metrics` (configs/agent.yaml:6-9), served by the standalone `python -m brain_v42.metrics` sidecar. The re-key changes the JSON **shape** (per-pid→per-agent) and the **semantics of `active_processes`** (`collector_db.py:175` `len(rows)` becomes "distinct active agents"). Consumed by: cockpit API contract (`red-monitor/docs/specs/2026-04-19-brain-cockpit-api-contract.md:36,121`), frontend (`BrainInfra.jsx:141-142` "N active", `Brain.jsx:153` active_convs), red-agent `ServiceCollector` (5s `/metrics` scrape), and **red-data** bronze/silver (`test_silver_metrics.py:73-151`).
- **Silent-outage risk:** the instant the single server goes live but before the per-agent flusher is correct, `active_processes` collapses to `1` → looks like a fleet outage on the live dashboard.
- **Coordination (red-triad):** decide field strategy (Open Q1); update `collect_process_metrics` + cockpit contract + `BrainInfra.jsx` + red-data **together**; add a contract test on the `/metrics:9200` shape.

## Corrected migration scope

**TRUE MCP-stdio brain clients (migrate):**
| Client | Config site | Note |
|---|---|---|
| Claude Code sessions (global) | `~/.claude.json` `mcpServers.brain-v42` | shared by ~41 projects → per-project HTTP entries (H10) |
| red-writer | per-project `~/.claude.json` override | |
| brain-v42 self | `brain_v42/.mcp.json` | own-session header value unspecified (Open Q6) |
| red-lab | `red-lab/mcp/brain-v42.json` + `.gemini/settings.json` | **gemini HTTP-MCP + header support unverified** (Open Q5) |
| red-shrik | `shrik.yaml:35-41` + `models.py:43-48` (`MCPClient`) | **code change** (H9) |
| SquadHubZ / poc_lyriks_v2 / watchk / watchk_claude_version / auto_discord | each has `.mcp.json` | **brain-v42 presence unverified per-file** — grep each before declaring done |

**OUT OF SCOPE (DB-DSN/connector clients, unaffected):** red-codex (direct PG DSN, `codex_ro` on `codex_brain_entity_v1`), red-data (own asyncpg connector + own MCP), dream/cross-project jobs (in-process SQLAlchemy, not MCP). Note: `remote-control --name` is the Discord-bot session manager, unrelated to the brain MCP.
Post-cutover assertion: grep `brain_v42.mcp.server` repo-wide + `~/.claude*`, migrate each, assert **no `python -m brain_v42.mcp.server` stdio process remains**.

---

## Research-validated additions (fold into the plan)

- **Pool (db/engine.py):** `pool_size=20, max_overflow=10` (cap 30 vs max_connections=100, ~70 headroom), **ADD `pool_recycle=1800`** (absent today; the load-bearing edit for a 24/7 pool), keep `pool_pre_ping=True`. No pgbouncer. Tighten pins `sqlalchemy[asyncio]>=2.0.44` (InvalidCachedStatementError auto-recovery + asyncpg teardown fixes), `asyncpg>=0.30`. One-time smoke test: DDL on a live pooled connection self-recovers.
- **Observability:** add `opentelemetry-api` + `opentelemetry-sdk` ONLY (no OTLP exporter, no ASGI instrumentor → stays protobuf-free; api/sdk already at 1.39.1/1.40.0). Always-on baseline = `structlog.contextvars` bind (asyncio-task-isolated) + `process_metrics` re-key; gate the OTel SDK behind `BRAIN_OTEL_ENABLED` (default off). Optional `on_call_tool` middleware to stamp `span.set_attribute("brain.agent", name)` on FastMCP's native span. **RSS = process-level gauge** (`agent_name='_process'`), not per-agent.
- **Security:** explicit `TransportSecuritySettings` (or custom middleware per H1) `enable_dns_rebinding_protection=True`, allowlist `127.0.0.1:*/localhost:*/[::1]:*` only; never mutate host post-construction (graphiti #1205); `mask_error_details=True` (don't leak the codex_ro DSN/paths); bind 127.0.0.1 non-overridable. Both CVE IDs confirmed real & correctly mapped.
- **Pins/version guard:** `fastmcp>=3.4.1,<4`, `mcp>=1.27,<2`, explicit `starlette>=1.0.1`; startup/test assert `fastmcp.__version__>=3.4` (catch resolve drift; pins permit lower than the installed 3.1.0 — and the install must be bumped).
- **Client identity:** static `--header` (fixed fleet) or `headersHelper` (shell reading `$AGENT_NAME`, re-run on every connect/reconnect → zero per-agent edits, survives backoff). Document X-Brain-Agent as a spoofable label.

## Confirmed-correct (keep these)

- `mcp.run_async(transport="http", host=, port=)` is the correct entry; default served path **`/mcp`** (clients use `http://127.0.0.1:PORT/mcp`, not `/sse`).
- CVE IDs real & mapped (CVE-2025-66416 mcp DNS-rebinding fixed 1.23.0; CVE-2026-48710 starlette BadHost fixed 1.0.1). Bump targets exist on PyPI (fastmcp 3.4.2, mcp 1.28.1, starlette 1.0.1+; 2.0 alpha-only so `<2` meaningful).
- Long-lived HTTP keeps the event loop alive → `create_task` flushers persist (same keepalive as stdio).
- `get_http_headers()` never raises, returns `{}` under stdio → stdio-backward-compatible; `x-brain-agent` not in the default exclude set.
- **Per-request DB session scoping already correct** for a shared pool: each repo call yields a fresh `AsyncSession` from the shared `async_sessionmaker` (pg_base.py:57-62); only `_engine`/`_session_factory` are singletons.
- `record_tool_call` + collector mutators are synchronous/atomic on the loop → **no locks** (adding them is a regression).
- Write-flushers already tolerate N concurrent writers (Timeseries `ON CONFLICT` last-writer-wins; Decay snapshot-boundary) → the single HTTP run is a strict subset of today's concurrency.
- The otel/protobuf conflict is a pre-existing shared-venv leak, orthogonal to this bump (fastmcp's only otel dep is `opentelemetry-api`).

## Open questions for the user (resolve before locking the plan)

1. **`active_processes` field** — silently redefine as "distinct active agents", or add a new `active_agents` field + keep `active_processes` for back-compat? (Live dashboard number + red-triad coordination.)
2. **process_metrics PK** — composite `(agent_name, pid)` (safe through coexistence; recommended) vs bare `agent_name` (cleaner end-state, unsafe during dual-run)? Recommend composite-then-collapse.
3. **Production interpreter — 3.12 or 3.14?** venv is 3.14, pyproject floors 3.12 — changes the mcp-imposed starlette floor + resolved graph. Pin the interpreter.
4. **Migration data** — TRUNCATE legacy pid rows (fine given 1h retention) or backfill synthetic agent_name?
5. **gemini agents (red-lab)** — does gemini support HTTP-MCP + per-server headers? If stdio-only they **cannot** join the shared server (breaks "all stdio disappears").
6. **`X-Brain-Agent` for brain's own session + dream jobs** — what value, set how (no `project_context` tool; project_key lives only in focus text)?
7. **Tighten `0.0.0.0:9200` metrics bind to 127.0.0.1** as part of the security cutover, or is the LAN exposure (red-monitor cross-host scrape) intentional / out of scope?
8. **Single-point-of-failure acceptance** — one shared process = one crash/deadlock/pool-exhaustion takes the **whole fleet** offline (no per-session isolation). Plan needs: real `/health` route + systemd `WatchdogSec`/`Type=notify` (Restart=always can't detect a wedged-but-alive loop), `StartLimitIntervalSec`/`StartLimitBurst` crash-loop trip, `After=/Requires=` PG readiness (engine has no connect-retry), explicit **httpx `max_connections` + Neo4j `max_connection_pool_size`** (one shared client/driver funnels all fleet embedding/rerank/graph; the GPU already 503s under load). Confirm these as hard gates. The design's "model on `brain-metrics.service`" baseline **does not exist** — author from `deploy/systemd/*.tmpl`.

## Sequence

brainstorm ✅ → **this verified spec** ✅ → resolve Open Questions → `pattern-auto` Phase 1 (writing-plans, docs/plans/) → Phase 2 (3-judge critique) → Phase 3 (subagent exec on a feature branch) → Phase 4 (final review + merge). Dual-run behind `BRAIN_MCP_TRANSPORT`; reaper carve-out FIRST (C3); migrate red-monitor/contract first (canary), then fleet agent-by-agent; composite PK through coexistence; cutover proven = 1 dream night + fleet stable on HTTP.
