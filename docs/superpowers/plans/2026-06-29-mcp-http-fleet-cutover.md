# Fleet cutover stdio→HTTP + decommission — Implementation Plan (Plan 3 of 3)

> **For agentic workers:** REQUIRED SUB-SKILL: superpowers:subagent-driven-development. This plan touches LIVE infrastructure (the brain server every agent — and this session — depends on) and external repos. Migrate ONE client at a time with a rollback ready. ≤3 concurrent agents. Steps use `- [ ]`.

**Goal:** Point every TRUE brain MCP-stdio client at the single 127.0.0.1 HTTP server with a distinct `X-Brain-Agent`, retire the N stdio servers, and collapse `process_metrics` to its end-state — proving per-agent observability survives end-to-end before decommission.

**Architecture:** Static `--header` for fixed single-purpose clients; `headersHelper` (a shell snippet deriving the agent name from `$AGENT_NAME`/cwd) for the global `~/.claude.json` entry shared by ~41 projects, so identity is per-project without 41 hand edits and survives reconnect backoff. red-shrik's hand-rolled stdio `MCPClient` is replaced by an HTTP client. Cutover is dual-run behind `BRAIN_MCP_TRANSPORT`, client-by-client, reaper as the residual-stdio net, with a one-command rollback (revert the client's config to stdio).

**Depends on:** Plan 1 (server) + Plan 2 (metrics re-key). **This is the last plan.** Verified design: items H9, H10, Q5(gemini), Cross-repo §, Migration §.

## Global Constraints (inherit Plans 1-2 + these)

- **One client at a time, rollback ready.** Rollback = revert that client's MCP config from `{type:http,url,headers}` back to `{type:stdio,command}`. The reaper stays active as the residual-stdio safety net for the whole window.
- **`X-Brain-Agent` distinct per client.** A client that resolves to `unknown` must be logged/alerted (it means a misconfig), never silently accepted as fine.
- **`project_key` is untouched.** It stays a tool argument (red-shrik keeps `project_key="red-shrik:agent"`); the header does NOT scope data.
- **`start` ≠ `enable`.** The unit must be `systemctl --user start` (running, serving `127.0.0.1:PORT`, dual-run) from Batch 1 BEFORE any client flip — else nothing serves the port and every migrated client breaks (requirements-judge CRITICAL). `enable` (boot persistence) is a SEPARATE, later step. **Do NOT `systemctl enable` the prod unit until Plan 2 (composite PK) is merged + deployed** (spec C2); Plan 3 Batch 5 is the first point prod-`enable` is safe.
- **TRUE stdio clients only** (verified): `~/.claude.json` global (41 projects), `ReD_v1/.mcp.json`, `brain_v42/.mcp.json`, `ReD_v1/.gemini/settings.json`, red-shrik (`MCPClient`), red-writer (per-project override if present). **OUT OF SCOPE:** red-codex (PG DSN), red-data (own connector), dream/cross-project jobs (in-process SQLAlchemy). SquadHubZ/poc_lyriks/watchk/auto_discord do NOT declare brain.

## File Structure (Plan 3)

| File | Responsibility | Action |
|---|---|---|
| `~/.claude.json` | global brain server → http + headersHelper | Modify (outside repo) |
| `ReD_v1/.mcp.json`, `brain_v42/.mcp.json` | brain server → http | Modify |
| `ReD_v1/.gemini/settings.json` | brain → httpUrl (if supported) | Modify / keep-stdio |
| `ReD_v1/projects/red-shrik/src/shrik/mcp_client.py` | stdio JSON-RPC → HTTP client | Modify (code) |
| `ReD_v1/projects/red-shrik/config/shrik.yaml` (35-41), `src/shrik/models.py` (43-48) | drop stdio mcp_command pin | Modify |
| `alembic/versions/026_*_collapse_pid.py` | composite PK → bare agent_name | **Create** |
| `deploy/systemd/*.service` | prod-enable the unit | enable |

---

## Batch 1 — Verify the client mechanism, then migrate the low-risk clients (canary)

### Task 1.1: Empirically confirm `claude mcp add --transport http --header` semantics (CLI 2.1.195)

**Why:** The whole scheme is unfounded if the static header doesn't ride every JSON-RPC POST (spec open item). Confirm BEFORE touching any real config.

- [ ] **Step 1:** Stand up the Plan-1 HTTP server (dev) with a logging middleware printing `get_http_headers()` per call.
- [ ] **Step 2:** `claude mcp add --transport http --header "X-Brain-Agent: canary" brain-http http://127.0.0.1:PORT/mcp` in a scratch dir; inspect `~/.claude.json` → confirm it persisted `{type:"http", url, headers:{"X-Brain-Agent":"canary"}}`.
- [ ] **Step 3:** Run 3 sequential brain tool calls from a `claude` session bound to it; assert the server log shows `x-brain-agent: canary` on calls 2 and 3 (not just `initialize`). If it only appears on the handshake, STOP — switch the whole plan to `headersHelper` (which re-runs per connection) and re-confirm. Record the result in `docs/superpowers/plans/_plan3-client-capture.md`.
- [ ] **Step 4:** Confirm `headersHelper` AND fix the identity source. **Architect CRITICAL: `$AGENT_NAME` is UNDEFINED for the 41 interactive `claude` sessions → empty header → everything buckets to `unknown`.** For interactive projects derive identity from the working dir: `headersHelper` = `printf '{"X-Brain-Agent":"%s"}' "$(basename "$PWD")"`; assert two different project dirs yield two different agent names. For the red-shrik fleet (launched with a known identity) a static `--header X-Brain-Agent: red-shrik` per launch is simpler than headersHelper. Verify the helper runs at connect and `claude` doesn't clobber the header. Commit the capture doc. **(Resolves spec Open Q6 — interactive/brain-self identity = `basename $PWD`.)**

### Task 1.2: Canary — migrate brain_v42's own `.mcp.json` + ONE interactive project, validate live per-agent metrics

- [ ] **Step 0 (the port must be SERVING first):** `systemctl --user start brain-mcp-http` — **`start`, NOT `enable`** (dual-run: the unit serves `127.0.0.1:PORT` now; `enable` = boot persistence, reserved for Batch 5). Confirm `curl -fsS 127.0.0.1:PORT/health` → ok. Nothing below works until the port is served.
- [ ] **Step 1:** Edit `brain_v42/.mcp.json`: brain server → `{type:"http", url:"http://127.0.0.1:PORT/mcp", headers:{"X-Brain-Agent":"brain-v42"}}`. Keep a commented stdio block for instant rollback.
- [ ] **Step 2:** Restart the brain_v42 Claude session; run `brain_search`. Verify via `:9200/metrics` that an `agent_name="brain-v42"` row appears with the call counted (Plan 2 wiring proven end-to-end on a real client).
- [ ] **Step 3:** Migrate ONE interactive project entry (pick a low-traffic one from the 41) using `headersHelper` deriving the name from the project dir; verify its calls land under that agent name, not `brain-v42`.
- [ ] **Step 4: Rollback drill** — revert one client to stdio, confirm it still works (proves the rollback path). Re-apply. **Step 5: Commit** (repo-side `.mcp.json`) `chore(cutover): canary brain_v42 self + 1 project to HTTP transport`.

---

## Batch 2 — red-shrik: replace the hand-rolled stdio MCPClient with an HTTP client (H9 — CODE, not config)

### Task 2.1: HTTP transport for red-shrik's brain client, preserving retry/backoff + project_key

**Files (red-shrik repo):** `src/shrik/mcp_client.py`, `config/shrik.yaml` (35-41), `src/shrik/models.py` (43-48); Tests `test_mcp_client_retry.py`, `test_brain_bootstrap.py`.

- [ ] **Step 1: gitnexus/grep** the `MCPClient` surface used by `entry.py:236-238` + `knowledge.py:21,47` (`BRAIN_PROJECT_KEY="red-shrik:agent"`).
- [ ] **Step 2: Write the failing test** — point red-shrik at a stub streamable-HTTP MCP server **that captures request headers**; assert a `brain_learn` call (a) reaches it over HTTP, (b) carries the client-side header `X-Brain-Agent: red-shrik` (set in red-shrik's HTTP client, NOT via the server's `instrument_tool`), (c) still passes `project_key="red-shrik:agent"` as a tool ARG, (d) honors the existing retry/backoff on a simulated **HTTP 503** — assert the retry fires via the SAME path using the existing retry-config constant (the 35s embedding backoff), NOT keyed on a stdio-specific exception type.
- [ ] **Step 3: Run — expect FAIL.**
- [ ] **Step 4: Implement** — replace the line-delimited-JSON-over-subprocess transport in `mcp_client.py` with `fastmcp.Client` (streamable HTTP) or an httpx JSON-RPC client targeting `http://127.0.0.1:PORT/mcp`, injecting the static `X-Brain-Agent: red-shrik` header. Remove the `mcp_command`/`mcp_cwd` stdio pin from `shrik.yaml:35-41` + `models.py:43-48` defaults (replace with `brain_http_url`). Keep ALL retry/backoff config and the `project_key` tool-arg path unchanged.
- [ ] **Step 5: Run — expect PASS** + `pytest` red-shrik suite (esp. `test_mcp_client_retry.py`, `test_brain_bootstrap.py`). **Step 6: Commit (red-shrik repo)** `feat(shrik): brain client over HTTP transport (X-Brain-Agent), drop embedded stdio server`.
- [ ] **Step 7:** Deploy red-shrik (`systemctl restart red-shrik`) — this also kills its embedded stdio brain child (the code-pinning drift source, learning 1292e4b9). Verify a `red-shrik:agent`/`red-shrik` metrics row appears.

---

## Batch 3 — gemini client (Q5: may be stdio-only)

### Task 3.1: Verify gemini HTTP-MCP support; migrate or document the stdio exception

**Files:** `ReD_v1/.gemini/settings.json`.

- [ ] **Step 1:** Check whether gemini settings support an HTTP-transport MCP server with per-server headers (look for `httpUrl`/`url`+`headers` keys in the gemini MCP schema for the installed gemini version). Record the finding.
- [ ] **Step 2a (if supported):** Edit `.gemini/settings.json` brain entry → `httpUrl: http://127.0.0.1:PORT/mcp` + headers `X-Brain-Agent: gemini-red-lab`; restart a gemini session; verify the agent row appears.
- [ ] **Step 2b (if NOT supported):** Keep gemini on stdio brain. **This is a documented permanent exception** — the "all stdio disappears" premise becomes "all stdio EXCEPT gemini." Note that the reaper carve-out (Plan 1 C3) must still allow this single legitimate stdio server, and the global "no `python -m brain_v42.mcp.server` stdio process remains" assertion (Batch 5) must whitelist the gemini one.
- [ ] **Step 3: Commit** the config change or the documented exception.

---

## Batch 4 — Roll the remaining fleet + the global ~/.claude.json (H10)

### Task 4.1: Migrate `ReD_v1/.mcp.json`, red-writer override, and the global 41-project entry

- [ ] **Step 1:** `ReD_v1/.mcp.json` + any red-writer per-project override → HTTP + distinct `X-Brain-Agent`. Restart, verify rows.
- [ ] **Step 2:** Global `~/.claude.json` `mcpServers.brain-v42` → `{type:"http", url, headersHelper: 'printf '{"X-Brain-Agent":"%s"}' "$(basename "$PWD")"'}` — the `basename $PWD` source confirmed in Task 1.1 Step 4 (**NOT `$AGENT_NAME`**, which is undefined for interactive sessions). Since 41 projects share this single default, headersHelper is mandatory (no per-project edits). Verify two different project dirs produce two different agent rows.
- [ ] **Step 3:** Assert NO client now resolves to `unknown` under normal use: query `:9200/metrics`, confirm `unknown` row is empty/absent (a non-empty `unknown` row = a misconfigured client to fix).
- [ ] **Step 4:** Soak 24h: watch `active_agents`, error rates, the GPU embedding 503 rate under the now-shared httpx pool (Plan 1 Task 5.2 caps). **Step 5: Commit** the repo-tracked config changes `chore(cutover): migrate remaining fleet + global headersHelper to HTTP`.

---

## Batch 5 — Decommission stdio + collapse PK + prod-enable

### Task 5.1: Assert no residual stdio brain server, then prod-enable the unit

- [ ] **Step 1:** `pgrep -af "python -m brain_v42.mcp.server"` — assert the ONLY match is the `--http-server` systemd process (plus the documented gemini exception from Batch 3 if applicable). No `claude`-spawned or red-shrik-spawned stdio children remain.
- [ ] **Step 2:** `systemctl --user enable --now brain-mcp-http` + the watchdog timer (prod-enable is now safe: composite PK from Plan 2 is live, so any residual `unknown` writer can't corrupt other agents). Verify `/health` + per-agent metrics + a full brain tool round-trip from a live agent.
- [ ] **Step 3:** Confirm PG connection count dropped to the single pool's ceiling (`SELECT count(*) FROM pg_stat_activity WHERE datname='brain'` ≤ ~30, vs the 60+/100 incident). **Step 4: Commit/document** the go-live.

### Task 5.2: Migration 026 — collapse composite PK → bare agent_name (end-state)

**Files:** Create `alembic/versions/026_collapse_pid_pk.py`; Test `tests/integration/db/test_migration_026.py`.

- [ ] **Step 1: Write the failing test** — after 026, `process_metrics` PK is `(agent_name)` only; `pid` is a plain nullable attribute; a re-flush upserts one row per agent regardless of pid.
- [ ] **Step 2: Run — expect FAIL. Step 3: Implement** — ONLY after Step 1 of 5.1 proves no concurrent stdio writers remain: dedupe leftover rows FIRST (keep latest `updated_at` per agent_name), THEN `op.drop_constraint("process_metrics_pkey", "process_metrics", type_="primary")` + `op.create_primary_key("process_metrics_pkey", "process_metrics", ["agent_name"])` (name the constraint explicitly — generic "drop PK" fails in Alembic). Update `tables.py` + the flusher `ON CONFLICT (agent_name)`. Downgrade restores the composite `(agent_name, pid)` PK by name.
- [ ] **Step 4: Run — expect PASS** + canary (head 025→026). **Step 5: Commit** `feat(db): migration 026 — collapse process_metrics PK to agent_name (post-cutover end-state)`.

### Task 5.3: Cutover-proven gate + reaper demotion

- [ ] **Step 1:** Cutover is "proven" when: 1 dream night runs clean against the HTTP server AND the fleet is stable on HTTP for 48h AND `active_agents` matches the live fleet AND PG conns stay bounded. Record evidence.
- [ ] **Step 2:** Demote the reaper to log-only/safety-net (it should now find no stale stdio sessions); keep the timer as a guardrail. **Step 3:** Update `brain_update_project_focus` (the migration is DONE) + `brain_learn` any new gotchas. **Step 4: Commit/document.**

---

## Plan 3 Self-Review

- **Spec coverage:** H9 (Batch 2 red-shrik code), H10 (Batch 1.1 mechanism + Batch 4 headersHelper/41-projects), Q5 gemini (Batch 3, with documented stdio-exception path), Migration § (Batch 3.1/5.2 PK collapse), canary+dual-run+rollback (Batch 1.2/global constraints), decommission + prod-enable gate (Batch 5). ✅
- **Live-infra safety:** one-client-at-a-time + rollback drill (1.2), prod-enable gated on Plan 2 + the no-residual-stdio assertion (5.1), reaper net throughout. The gemini stdio-exception is explicitly carried into the reaper carve-out + the decommission assertion (no false "done").
- **Type/flow consistency:** `X-Brain-Agent` header + `project_key` arg kept orthogonal; composite-PK (Plan 2) → bare agent_name (5.2) sequencing respected (collapse only after no concurrent stdio writers).

## Execution handoff

Plans 1→2→3 in order. Plan 3 is ops-heavy and cross-repo (red-shrik, ~/.claude.json, gemini) — several commits land OUTSIDE this repo. Recommended: subagent-driven for the repo-side code (red-shrik client, migration 026), human-in-the-loop for the live config flips + prod-enable (each is a material, fleet-visible action).
