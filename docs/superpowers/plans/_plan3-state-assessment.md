# Plan 3 — State Assessment (brain MCP stdio→HTTP fleet cutover + full live deploy)

> Plan 3 = brain MCP `stdio` → single shared HTTP server fleet cutover ("deploy tout").
> Plan 1 (HTTP server code) is code-complete & merged. Plan 2 (per-agent metrics, composite PK migration 025) is live.
> Synthesis date: 2026-06-30. Verified live against the repo + running host (uid 1001, host 192.168.1.12).

---

## Current state vs plan

### DONE — Plan 1 server code surface (merged, verified runnable)

| Surface | State | Evidence (verified this assessment) |
|---|---|---|
| HTTP transport entrypoint | DONE | `src/brain_v42/mcp/server.py:88` `_apply_http_server_arg()` sets `BRAIN_MCP_TRANSPORT=http` when the literal `--http-server` token is in `sys.argv`; called at `server.py:525` BEFORE `get_settings()` so the lru_cache picks it up. Token deliberately kept in `argv` for reaper cmdline-exclusion. |
| HTTP bind | DONE | `server.py:496` `await mcp.run_http_async(host=settings.mcp_http_host, port=settings.mcp_http_port, …, uvicorn_config={"timeout_graceful_shutdown": 10})`. Binds **`127.0.0.1:8765`** (loopback-only, enforced by config validator). |
| `/health` liveness | DONE | `server.py:219` `@mcp.custom_route("/health", methods=["GET"])`. Bounded `SELECT 1` under `asyncio.timeout(2)`; returns `200 {"status":"ok","pool":{…}}` or `503 {"status":"degraded"}` on pool wedge/timeout. This is exactly what the systemd watchdog probes. |
| Import smoke | DONE | `python -c "import brain_v42.mcp.server"` → `import OK`, `has health_check: True`. Server module is importable with current deps. |
| systemd unit templates | DONE (committed, NOT installed) | `deploy/systemd/brain-mcp-http.service.tmpl`, `…-watchdog.service.tmpl`, `…-watchdog.timer.tmpl`; wired into `install.sh` gated (commit `076fe8a`, "gated, no prod auto-enable"). |
| Metrics sidecar `:9200` | DONE & LIVE (independent of MCP HTTP) | `brain-metrics.service` installed/enabled/active ~5d. `ss -ltnp` shows `0.0.0.0:9200` LISTEN. **Separate standalone process** (`python -m brain_v42.metrics`); the MCP server never instantiates `MetricsServer`. Starting/stopping `brain-mcp-http` does NOT affect `:9200`. |
| Plan 2 metrics PK | DONE & LIVE | alembic head = **025** (`(agent_name, pid)` composite PK). Verified `alembic heads` → `025 (head)`. |

### REMAINS — the 5 Plan-3 batches

| Batch | Scope | Track |
|---|---|---|
| B1 | red-shrik / Gemini HTTP client rewrite + tests (`.gemini/settings.json`) | mostly autonomous (config + test), live flip = human |
| B2 | migration 026 (collapse PK `(agent_name,pid)` → bare `agent_name`) + flusher/table-def + tests | autonomous code; live apply = human |
| B3 | global Claude `~/.claude.json` `brain-v42` stdio→http + per-client X-Brain-Agent header strategy | autonomous verification doc; live flip = human |
| B4 | `systemctl --user start` the HTTP unit (dual-run, no boot-enable) | LIVE / human go-no-go |
| B5 | boot-enable + watchdog timer + linger + prod-enable end-state | LIVE / human go-no-go |

### EXACT start commands + ports (load-bearing)

```bash
# Prereq for ANY systemctl --user in this env (uid 1001, no session bus otherwise):
export XDG_RUNTIME_DIR=/run/user/1001

# Generate the units WITHOUT touching the already-enabled dream/graph-recon timers:
./deploy/systemd/install.sh --dry-run      # writes the 3 brain-mcp-http* unit files only; NO systemctl reload/enable
systemctl --user daemon-reload             # pick up the new unit files

# Dual-run START (start only = no boot persistence, watchdog stays OFF):
systemctl --user start brain-mcp-http
```

- **MCP HTTP server port: `127.0.0.1:8765`** (`config.py:63-64` `mcp_http_host='127.0.0.1'`, `mcp_http_port=8765`; loopback-only by validator).
- **Metrics port: `:9200`** (`config.py:77-78` `metrics_port=9200`, `metrics_host='0.0.0.0'`; `.env:4 METRICS_PORT=9200`). Already live via `brain-metrics.service`, independent of B4.

> Note: running `install.sh` in **full** (non-dry-run) mode also daemon-reloads and re-enables the dream + graph-recon timers (already enabled today) and explicitly logs that `brain-mcp-http` is generated but NOT enabled. Use `--dry-run` to avoid re-touching the other timers.

---

## Execution plan (sequenced)

### TRACK A — AUTONOMOUS (repo-side, TDD, NO live infra touched)

Order: A1 → A2 → A3 (independent; can be parallelized, but A2 must land before the B-live migration apply).

**A1 — migration 026: collapse PK `(agent_name, pid)` → bare `agent_name`**
- New file: `alembic/versions/026_*.py` with `revision='026'`, `down_revision='025'`.
- Upgrade body, STRICT ORDER (dedupe BEFORE PK or it fails on duplicate-key):
  1. dedupe `DELETE` keeping `MAX(updated_at)` row per `agent_name`;
  2. `op.drop_constraint('process_metrics_pkey', 'process_metrics', type_='primary')`;
  3. `op.create_primary_key('process_metrics_pkey', 'process_metrics', ['agent_name'])` — **pass the explicit name** (single-col create does NOT auto-derive `process_metrics_pkey`).
- Downgrade: re-create `create_primary_key('process_metrics_pkey', …, ['agent_name','pid'])` (lossy — deduped rows are unrecoverable; acceptable, metrics age out in 1h).
- Edit `src/brain_v42/db/tables.py:412` — remove `primary_key=True` on `pid` (keep the column; do NOT drop it — `collector_db.py:197` `active_processes` = distinct pid back-compat).
- Edit `src/brain_v42/metrics/flusher.py:171-185` — drop `pid` from INSERT column list + params (`flusher.py:155-156`), change `ON CONFLICT (agent_name, pid)` → `ON CONFLICT (agent_name)` (`flusher.py:178`).
- Update doc-comments: `flusher.py:49-58` (`stop()`), `flusher.py:118-133` (`_flush` DEPLOY GATE) — they describe the composite-PK contract.
- Tests: add/adjust migration canary + the metrics contract test (recent `c422b7a`/`be0025f` and `tests/unit/test_alembic_env.py` may assert the `(agent_name,pid)` shape) — RED first, then green.

**A2 — red-shrik / Gemini HTTP client rewrite + test**
- File: `/home/hawixs/hawkixs_infra/git_repo/ReD_v1/.gemini/settings.json` → `mcpServers.brain-v42`.
- Target shape (gemini 0.38.2, **confirmed** array-of-`{name,value}`, NOT Claude object form):
  ```json
  { "url": "<BRAIN_HTTP_URL>", "type": "http",
    "headers": [{ "name": "X-Brain-Agent", "value": "ReD_v1" }] }
  ```
  (or `httpUrl` for Streamable HTTP — see open question Q-URL.)
- Test: a config-shape assertion that the header is an array-of-objects and `X-Brain-Agent=ReD_v1` (a Claude-style object would silently drop the header).
- Note: ReD_v1's Claude `.mcp.json` has NO brain entry — the gemini file is the ONLY ReD_v1 brain client. red-data / red-codex / red-writer are OUT OF SCOPE.

**A3 — header-capture verification doc (global Claude `~/.claude.json`)**
- Deliverable: a short verification doc proving the chosen X-Brain-Agent mechanism works with the **installed** Claude Code (2.1.195).
- CRITICAL FINDING (verified): `claude mcp add --transport http` supports **static `--header` only**. There is **NO `headersHelper` / dynamic `$PWD`-deriving header key** in the installed CLI — the term appears only in changelogs/marketplace docs and this session's transcripts, never in the binary surface. So the "headersHelper emits `basename $PWD`" plan from the inventory is **not supported as written** and must be replaced.
- The doc must resolve: since a single global `~/.claude.json` `brain-v42` entry is shared by ~41 projects with no dynamic header, either (a) accept a single static `X-Brain-Agent` for all Claude projects, or (b) make brain-v42 a per-project entry with a per-project static header (loses the single-shared-default benefit). `AGENT_NAME` is unset, so `$AGENT_NAME`-based plans are also out.

### TRACK B — LIVE / HUMAN GO-NO-GO (fleet-visible, can break the running session/fleet)

Order is strict: B4 (start, prove green) → B-migration-apply → B-client-flips (one at a time) → B5 (enable end-state). Every step below needs sign-off because it is fleet-visible and the orchestrating session itself talks to brain.

**B4 — `systemctl --user start brain-mcp-http` (dual-run, watchdog OFF)**
- Action: `export XDG_RUNTIME_DIR=/run/user/1001; ./deploy/systemd/install.sh --dry-run; systemctl --user daemon-reload; systemctl --user start brain-mcp-http`. Then `curl -fsS 127.0.0.1:8765/health` must return 200.
- Rollback: `systemctl --user stop brain-mcp-http` (no boot persistence was added; nothing to disable). `:9200` and all stdio clients are unaffected.
- Why sign-off: first time the HTTP server binds `:8765` on the live host; a crash-loop (`Restart=always RestartSec=2`) or a bad `.env`/`EnvironmentFile` makes the unit thrash. Keep the **watchdog timer DISABLED** the entire dual-run window — its ExecStart unconditionally `systemctl --user restart brain-mcp-http` on any `/health` miss, turning a transient 503 into a restart loop.

**B-migration-apply — `alembic upgrade head` to 026 on the live DB**
- Action: apply 026 (collapse PK). MUST be sequenced so that a single-key PK and a composite-targeting flusher never coexist: ship the new flusher (`ON CONFLICT (agent_name)`) everywhere, THEN apply. An OLD flusher firing `ON CONFLICT (agent_name, pid)` after the swap raises "no unique constraint matching ON CONFLICT".
- Rollback: `alembic downgrade 025` (re-creates composite PK; lossy on deduped rows — acceptable, metrics ephemeral).
- Why sign-off: live DDL on the metrics table + deploy-ordering hazard (reverse of the 025 gate). Must confirm the stdio↔http coexistence window is CLOSED (single shared HTTP server end-state) before collapsing — otherwise two writers race on one `agent_name` row (last-writer-wins; acceptable for metrics but a behavior change vs what 025 was built to avoid).

**B3-flip — global Claude `~/.claude.json` `brain-v42` stdio→http**
- Action: rewrite the single global `mcpServers.brain-v42` from `type:stdio` to `type:http` with `url:<BRAIN_HTTP_URL>` + static header (per A3 outcome). The `POSTGRES_URL`/`METRICS_ENABLED`/`EMBEDDING_SERVICE_URL`/`RERANKER_URL` env keys move server-side.
- Rollback: restore the prior stdio block (keep a verbatim backup of the entry before editing).
- Why sign-off: this one entry is inherited by **~41 projects at once** with no per-project fallback. A bad URL/header breaks brain for every Claude project simultaneously — including the session doing the cutover. Flip and immediately verify in one project before declaring green.

**B1-flip — red-shrik / Gemini restart**
- Action: deploy the A2 `.gemini/settings.json` change, then restart red-shrik so gemini reconnects over HTTP.
- Rollback: restore the prior stdio `.gemini` block + restart.
- Why sign-off: restarts a live fleet agent; if the header array shape is wrong the server may reject/mis-attribute the agent (no `X-Brain-Agent` sent).

**B5 — boot-enable end-state (prod-enable)**
- Action: `systemctl --user enable --now brain-mcp-http`; `systemctl --user enable --now brain-mcp-http-watchdog.timer`; `sudo loginctl enable-linger hawixs` (so user units survive logout/reboot).
- Rollback: `systemctl --user disable --now brain-mcp-http brain-mcp-http-watchdog.timer`; optionally `sudo loginctl disable-linger hawixs`.
- Why sign-off: makes the HTTP server the persistent prod end-state and arms the auto-restart watchdog. Only do this once dual-run + all client flips are proven green, because from here a `/health` 503 triggers automatic restarts.

---

## Top risks + rollback (per live step)

| Live step | Top risk | Rollback |
|---|---|---|
| B4 start | Crash-loop via `Restart=always/RestartSec=2` if `.env`/`EnvironmentFile` missing or import fails on host; watchdog (if armed) amplifies into restart storm | `systemctl --user stop brain-mcp-http`; keep watchdog timer OFF during dual-run |
| B-migration 026 | (1) `create_primary_key(['agent_name'])` FAILS on duplicate-key unless dedupe runs first; (2) old flusher + new PK → "no unique constraint matching ON CONFLICT"; (3) constraint name not pinned → downgrade can't drop by name | `alembic downgrade 025`; ship new flusher before applying; always pass explicit `'process_metrics_pkey'` |
| B3-flip (global Claude) | Single shared entry → bad URL/header breaks brain for ~41 projects + the cutover session itself; no per-project fallback | restore verbatim stdio backup of the `brain-v42` entry; verify one project before declaring green |
| B1-flip (gemini) | Claude-object header shape written into `.gemini` silently drops `X-Brain-Agent` (gemini expects array-of-`{name,value}`) → agent rejected/mis-attributed | restore prior stdio `.gemini` block + restart red-shrik |
| B5 enable | Armed watchdog force-restarts on any transient `/health` 503 (pool saturation → degraded); linger makes a bad unit persist across reboot | `systemctl --user disable --now brain-mcp-http brain-mcp-http-watchdog.timer`; `loginctl disable-linger hawixs` |

---

## Open questions to resolve BEFORE live

1. **Claude CLI HTTP + header support — RESOLVED (verified this assessment).** Installed `claude` = **2.1.195**. `claude mcp add --transport http <name> <url> --header "K: V"` IS supported (stdio/sse/http transports; `-H/--header`). BUT: **no `headersHelper` / dynamic-`$PWD` header key exists** in the installed CLI — only static `--header`. The inventory's "headersHelper deriving X-Brain-Agent=basename $PWD" plan is NOT supported as written. **Decision needed:** single static `X-Brain-Agent` for all ~41 Claude projects, OR per-project static-header entries (drops the single-shared-default). `AGENT_NAME` is unset, so `$AGENT_NAME` derivation is also out.
2. **Gemini HTTP-MCP support — RESOLVED (verified).** gemini **0.38.2** supports HTTP MCP with per-server headers, headers as an **array of `{name,value}`**. Remaining: confirm `httpUrl` (Streamable HTTP) vs `url`+`type:"http"`/`"sse"` is the right transport for the brain HTTP server impl (run_http_async = Streamable HTTP → likely `httpUrl`).
3. **Exact PORT/URL value — PARTIALLY RESOLVED.** Port = **8765** on **127.0.0.1** (loopback-only, verified `config.py:63-64` + `run_http_async`). The canonical shared `BRAIN_HTTP_URL` string clients must use (e.g. `http://127.0.0.1:8765/mcp` — decision e92fa375) is NOT yet present in any inventoried config and MUST be fixed before any client flip. Confirm the path segment FastMCP serves Streamable HTTP on.
4. **Is the HTTP server confirmed runnable now? — YES (verified).** Module imports clean; `/health` route present; `run_http_async` binds `mcp_http_port`; `:8765` not yet listening (no live bind attempted, by design). The only un-exercised path is the actual bind on the live host under the systemd `EnvironmentFile=.env` — prove it in B4 before any client flip.
5. **systemctl --user env prereq.** `export XDG_RUNTIME_DIR=/run/user/1001` is REQUIRED in this env or systemctl fails with "Failed to connect to bus". Every B-step command must set it (or run inside the user's logged-in session).
6. **install.sh mode.** Run **`--dry-run`** to generate ONLY the `brain-mcp-http*` unit files without re-touching the already-enabled dream/graph-recon timers (full mode re-enables them). Confirm this is the intended path for B4.
7. **Watchdog window.** Confirm the `/health` watchdog timer stays DISABLED for the entire dual-run window and is enabled only with the B5 boot-enable step (its ExecStart unconditionally restarts on a miss).
8. **026 `pid` column fate.** End-state PK = bare `agent_name`. KEEP `pid` as a plain non-PK column (preserves `collector_db.py:197` `active_processes` = distinct-pid back-compat). If Plan 3 later DROPS `pid`, `collector_db.py:125` SELECT + `:197` count + the `active_processes` response key all break — out of scope for 026, flag for later.
9. **stdio↔http coexistence closed before 026?** Collapsing to `agent_name` PK reintroduces the cross-writer collision 025 was built to avoid IF stdio writers can still run. Confirm single-shared-HTTP end-state (all clients flipped) before applying 026.
10. **loginctl linger state.** Confirm linger is/ isn't already enabled for `hawixs` before B5; `install.sh` warns if `Linger!=yes`. Needed so units survive reboot.

---

*Synthesis of 5 structured findings; live facts re-verified 2026-06-30 against repo HEAD (alembic head 025) and host 192.168.1.12 (uid 1001). Key correction vs inventory: `headersHelper` is NOT a supported Claude Code config key on the installed CLI 2.1.195.*
