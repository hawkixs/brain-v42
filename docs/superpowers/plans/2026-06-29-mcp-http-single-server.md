# Single HTTP MCP server — Implementation Plan (Plan 1 of 3: Server Foundation)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax. Dispatch ONE subagent per task — never a large parallel burst (a transient server throttle trips on big fan-outs; ≤3 concurrent is safe).

**Goal:** Make brain-v42 runnable as ONE long-lived streamable-HTTP MCP server bound to 127.0.0.1 with a single shared asyncpg pool, secure against DNS-rebinding/BadHost, supervised by systemd — **alongside** the existing stdio path (selected by env), with zero behaviour change when `BRAIN_MCP_TRANSPORT=stdio`.

**Architecture:** FastMCP streamable HTTP (`stateless_http=True, json_response=True`) on `127.0.0.1:PORT`. Background flushers + the shared SQLAlchemy/asyncpg engine + Neo4j driver move into a FastMCP `lifespan` so they run identically under both transports. A custom ASGI middleware enforces Host/Origin (jlowin FastMCP does NOT wire it). The stale-MCP reaper gets a carve-out so it never SIGTERMs the new long-lived server.

**Tech Stack:** Python 3.12 (deploy) / 3.14 (local venv), FastMCP 3.4.1, mcp 1.x, Starlette ≥1.0.1, uvicorn, SQLAlchemy 2.0 async + asyncpg, Pydantic 2, structlog, systemd --user.

**Scope note — this is Plan 1 of 3.** Plan 2 = per-agent observability re-key (collector refactor, `process_metrics` migration, cross-repo `/metrics` contract). Plan 3 = fleet cutover (client configs, red-shrik MCPClient→HTTP, gemini, canary, decommission stdio). Plan 1 makes the server *exist and run safely*; it does NOT migrate any client and does NOT change the metrics schema. Verified design: `docs/superpowers/specs/2026-06-29-mcp-http-single-server-design.md`.

## Global Constraints (every task inherits these — verbatim from spec)

- **Bind 127.0.0.1 only, non-overridable.** Default host `127.0.0.1`, never `0.0.0.0`. (bind-0.0.0.0 is the #1 real-world MCP exposure.)
- **Version floors (pyproject + lock):** `fastmcp>=3.4.1,<4` (3.4.1 floors Starlette≥1.0.1 for CVE-2026-48710), explicit `starlette>=1.0.1`, `mcp>=1.27,<2`, `sqlalchemy[asyncio]>=2.0.44`, `asyncpg>=0.30`. New transitive dep `uncalled-for>=0.2.0` must appear in the lock. Keep `ruff` pinned 0.15.18, `neo4j>=5,<7`.
- **Versions are authoritative via `.venv/bin/python -c "import importlib.metadata…"` — NEVER `pip`** (the venv's pip reads the wrong environment; documented gotcha 7bc821a1).
- **`X-Brain-Agent` is a spoofable observability LABEL, never an authz boundary.** `project_key` (tool argument) remains the data-scoping carrier, untouched.
- **`stateless_http` / `json_response` are NOT `FastMCP()` constructor kwargs** (removed in v3 → `TypeError`). They go on `run_http_async()`/`http_app()` or env `FASTMCP_STATELESS_HTTP`/`FASTMCP_JSON_RESPONSE`.
- **TDD, DRY, YAGNI, frequent commits.** Gate green before commit: `pytest tests/unit`, `ruff check`, `ruff format --check`, `mypy src/`. Run `gitnexus_impact` before editing a symbol; `gitnexus_detect_changes` before committing.
- **Feature branch:** `feat/mcp-http-server-foundation` off `main`. Never implement on `main`.

## File Structure (Plan 1)

| File | Responsibility | Action |
|---|---|---|
| `pyproject.toml` | dependency floors | Modify (pins) |
| `src/brain_v42/config.py` | Settings: transport/host/port fields | Modify (~62-64) |
| `src/brain_v42/db/engine.py` | shared pool sizing + recycle | Modify (~38-43) |
| `src/brain_v42/mcp/server.py` | transport branch + lifespan refactor | Modify (~96-107, 455-546) |
| `src/brain_v42/mcp/lifespan.py` | FastMCP lifespan hosting flushers/engine/neo4j | **Create** |
| `src/brain_v42/mcp/http_security.py` | Host/Origin ASGI middleware | **Create** |
| `src/brain_v42/mcp/health.py` | `/health` route (liveness for WatchdogSec) | **Create** |
| `src/brain_v42/maintenance/reap_stale_mcp.py` | reaper carve-out for the http server | Modify (~60, 139-141) |
| `deploy/systemd/brain-mcp-http.service.tmpl` | supervised unit + SPOF hardening | **Create** |
| `tests/unit/mcp/test_http_transport.py` … | per-task tests | Create |

---

## Batch 0 — Prereqs & API capture (SEQUENTIAL, blocks everything)

### Task 0.1: Branch + dependency bump + lock + version assertion

**Files:** Modify `pyproject.toml`; Create `tests/unit/test_dep_floors.py`.

**Interfaces:** Produces: a venv resolving fastmcp≥3.4.1, starlette≥1.0.1, mcp≥1.27,<2; later tasks import these.

- [ ] **Step 1: Branch.** `git checkout -b feat/mcp-http-server-foundation`
- [ ] **Step 2: Write the failing test** (first `grep -rln "importlib.metadata" tests/unit` — if `test_pyproject_deps.py` already asserts versions, EXTEND it instead of creating a duplicate version-guard file) — `tests/unit/test_dep_floors.py`:
```python
import importlib.metadata as m
from packaging.version import Version

def test_security_floors_met():
    assert Version(m.version("fastmcp")) >= Version("3.4.1")
    assert Version(m.version("starlette")) >= Version("1.0.1")   # CVE-2026-48710 BadHost
    assert Version("1.27") <= Version(m.version("mcp")) < Version("2")
    assert Version(m.version("sqlalchemy")) >= Version("2.0.44")
    assert Version(m.version("asyncpg")) >= Version("0.30")
```
- [ ] **Step 3: Run — expect FAIL** (`fastmcp 3.1.0`, `starlette 0.52.1` installed): `.venv/bin/python -m pytest tests/unit/test_dep_floors.py -v`
- [ ] **Step 4: Edit pyproject.toml** — set `fastmcp>=3.4.1,<4`; add `mcp>=1.27,<2`, `starlette>=1.0.1`; bump `sqlalchemy[asyncio]>=2.0.44`, `asyncpg>=0.30`.
- [ ] **Step 5: Resolve + lock:** `uv pip install -e ".[dev]" --python .venv/bin/python` then regenerate lock if used (`uv lock`). Confirm `uncalled-for` appears: `.venv/bin/python -c "import importlib.metadata as m; print(m.version('uncalled-for'))"`.
- [ ] **Step 6: Run — expect PASS.** Then `ruff check`, `ruff format --check`, `mypy src/` (the bump may surface FastMCP API breaks — capture them in 0.2, do NOT fix blindly here).
- [ ] **Step 7: Commit** `chore(deps): bump fastmcp>=3.4.1 + explicit starlette>=1.0.1 security floors (CVE-2026-48710)`

### Task 0.2: API-capture spike — confirm the FastMCP 3.4.x surfaces the plan depends on (NO production code)

**Why:** Plan 1 references `run_http_async`/`http_app`/`lifespan`/`TransportSecuritySettings`. These were verified against 3.1.0; 3.4.x must be confirmed before Batches 2-3 write code. This is a throwaway capture, not a test.

- [ ] **Step 1:** With the bumped venv, capture signatures into a scratch note `docs/superpowers/plans/_batch0-api-capture.md`:
```bash
.venv/bin/python - <<'PY'
import inspect, fastmcp
from fastmcp import FastMCP
print("fastmcp", fastmcp.__version__)
print("run_http_async:", inspect.signature(FastMCP.run_http_async))
print("http_app     :", inspect.signature(FastMCP.http_app))
print("__init__     :", inspect.signature(FastMCP.__init__))
# Confirm stateless_http/json_response are NOT on __init__ (must be on run_http_async/http_app)
PY
```
- [ ] **Step 2:** Confirm the Host/Origin security surface. Grep the installed source for the real param names (the spec flags these as unverified on 3.4.x):
```bash
grep -rn "security_settings\|TransportSecuritySettings\|enable_dns_rebinding\|allowed_hosts\|allowed_origins\|middleware" .venv/lib/python*/site-packages/fastmcp/server/http.py | head
.venv/bin/python -c "from fastmcp.server.http import create_streamable_http_app as f; import inspect; print(inspect.signature(f))"
```
- [ ] **Step 3:** Confirm `http_app(middleware=[...])` injection point exists AND whether `run_http_async` accepts `middleware`/`security_settings`. Record the EXACT call shape Batch 2/3 must use. Confirm `lifespan=` is accepted by `FastMCP()` or `http_app()`. **CRITICAL (architect H5):** confirm the lifespan actually FIRES under `run_http_async` (not only when you build the app via `http_app()`) — grep `run_http_async`/`_lifespan_manager` in `.venv/lib/python*/site-packages/fastmcp/server/` and trace whether `run_http_async` enters `server._lifespan_manager()`. If it does NOT, Task 2.3 must build the app via `http_app(lifespan=...)` + run uvicorn explicitly, else flushers never start under HTTP (H5 unfixed).
- [ ] **Step 4:** Confirm `get_http_headers` import path + lowercasing: `.venv/bin/python -c "from fastmcp.server.dependencies import get_http_headers; print(get_http_headers)"`.
- [ ] **Step 5:** Record findings in `_batch0-api-capture.md`. **If any API differs from this plan's assumption, update the affected task inline before implementing it.** Commit the capture: `docs(plan): batch-0 fastmcp 3.4.x API capture`.

---

## Batch 1 — Config plumbing (H8)

### Task 1.1: Add transport/host/port settings (default stdio + 127.0.0.1)

**Files:** Modify `src/brain_v42/config.py` (~62-64); Test `tests/unit/test_config_transport.py`.
**Interfaces:** Produces: `settings.brain_mcp_transport: Literal["stdio","http"]`, `settings.mcp_http_host: str`, `settings.mcp_http_port: int`.

- [ ] **Step 1: gitnexus impact** on `Settings`: `gitnexus_impact({target:"Settings", direction:"upstream"})`. Report blast radius.
- [ ] **Step 2: Write the failing test:**
```python
from brain_v42.config import Settings

def test_transport_defaults_to_stdio_and_loopback():
    s = Settings()
    assert s.brain_mcp_transport == "stdio"
    assert s.mcp_http_host == "127.0.0.1"
    assert isinstance(s.mcp_http_port, int)

def test_http_host_rejects_0_0_0_0(monkeypatch):
    monkeypatch.setenv("BRAIN_MCP_HTTP_HOST", "0.0.0.0")
    import pytest
    with pytest.raises(ValueError):
        Settings()
```
- [ ] **Step 3: Run — expect FAIL** (`extra='ignore'` drops the field today): `.venv/bin/python -m pytest tests/unit/test_config_transport.py -v`
- [ ] **Step 4: Implement** in `config.py`:
```python
from typing import Literal
from pydantic import field_validator
# inside Settings:
brain_mcp_transport: Literal["stdio", "http"] = "stdio"
mcp_http_host: str = "127.0.0.1"
mcp_http_port: int = 8765

@field_validator("mcp_http_host")
@classmethod
def _loopback_only(cls, v: str) -> str:
    if v not in {"127.0.0.1", "localhost", "::1"}:
        raise ValueError(f"mcp_http_host must be loopback, got {v!r} (bind-0.0.0.0 forbidden)")
    return v
```
- [ ] **Step 5: Run — expect PASS.**
- [ ] **Step 6: Gate + commit** `feat(config): add BRAIN_MCP_TRANSPORT/host/port, loopback-only validated (default stdio)`

---

## Batch 2 — Shared pool + lifespan refactor (H4, H5, H3, pool)

### Task 2.1: Size the single shared pool + add pool_recycle

**Files:** Modify `src/brain_v42/db/engine.py` (~38-43); Test `tests/unit/db/test_engine_pool.py`.

- [ ] **Step 1: gitnexus impact** on the engine factory.
- [ ] **Step 2: Write the failing test:**
```python
from brain_v42.db.engine import get_engine  # adjust to real factory name from engine.py

def test_shared_pool_sizing():
    eng = get_engine()
    assert eng.pool.size() == 20
    assert eng.pool._max_overflow == 10        # hard cap 30 vs PG max_connections=100
    assert eng.pool._recycle == 1800
    assert eng.pool._pre_ping is True
```
- [ ] **Step 3: Run — expect FAIL** (today 5/10, no recycle).
- [ ] **Step 4: Implement** — in `create_async_engine(...)` set `pool_size=20, max_overflow=10, pool_recycle=1800, pool_pre_ping=True`.
- [ ] **Step 5: Run — expect PASS.** **Step 6: Commit** `perf(db): size shared pool 20+10 + pool_recycle=1800 for the single long-lived server`

### Task 2.2: Extract a FastMCP lifespan hosting flushers + engine + neo4j (H4/H5)

**Files:** Create `src/brain_v42/mcp/lifespan.py`; Modify `src/brain_v42/mcp/server.py` (455-546); Test `tests/integration/mcp/test_lifespan.py`.
**Interfaces:** Produces: `make_lifespan(settings, services) -> AsyncContextManager` that, on enter, starts decay/metrics/timeseries flushers + plan-index task; on exit, stops them, `await dispose_engine()`, `await close_neo4j_driver(...)`. Consumed by both transports.

- [ ] **Step 1: gitnexus impact** on `run_with_background_tasks` (server.py:455-546) — report whether stdio path is affected.
- [ ] **Step 2: Write the failing integration test** (real PG). **MUST set `metrics_enabled=True`** — flushers are gated behind it (server.py:463); without it the test silently no-ops (false green). Assert: entering the lifespan starts the metrics flusher (a row appears in `process_metrics` within one flush interval) and exiting disposes the engine (`dispose_engine` sets `_engine=None`). Add a second test asserting the stdio path's SIGTERM→graceful-exit still runs the lifespan `__aexit__` (cleanup fires on signal, not only on natural return).
```python
import pytest
@pytest.mark.asyncio
async def test_lifespan_starts_flushers_and_disposes_engine(monkeypatch):
    monkeypatch.setenv("METRICS_ENABLED", "true")   # else flushers gated off (server.py:463)
    from brain_v42.mcp.lifespan import make_lifespan
    # build settings+services as server.py does; async-with the cm; assert a process_metrics row exists; after exit assert engine disposed (_engine is None)
    ...
```
- [ ] **Step 3: Run — expect FAIL** (module does not exist).
- [ ] **Step 4: Implement** `make_lifespan` by lifting the body of `run_with_background_tasks` (server.py:455-538) into an `@asynccontextmanager`; the `finally`-block cleanup becomes the post-`yield` section. **Use the `lifespan=` shape confirmed in Task 0.2.** Critical refactor hygiene (prevents the double-cleanup the architect flagged):
  - **DELETE the original `finally`-block cleanup (server.py:528-538) and the manual `_install_signal_handlers` for the HTTP path** — the lifespan is now the SOLE owner of flusher-stop + `dispose_engine` + `close_neo4j_driver`. Running both = double `flusher.stop()` → raises on already-stopped tasks.
  - **Keep a signal→`shutdown_event` bridge ONLY for the stdio branch** (stdio has no uvicorn to install handlers): SIGTERM must set the event so `run_async(stdio)` returns and the lifespan `__aexit__` runs. The HTTP branch delegates signals to uvicorn (which drives its own shutdown → lifespan exit).
  - **Add a bounded PG connect-retry in `__aenter__`** (e.g. 5×2s backoff) before starting flushers — the engine has no connect-retry and `After=network-online` does NOT guarantee PG ready; without this the unit crash-loops on cold start and trips `StartLimitBurst` (see Task 5.3).
- [ ] **Step 5: Run — expect PASS** + full `pytest tests/unit` (no stdio regression) + the stdio-signal-shutdown assertion from Step 2.
- [ ] **Step 6: Commit** `refactor(mcp): host flushers+engine+neo4j in a FastMCP lifespan (transport-agnostic)`

### Task 2.3: HTTP transport branch with graceful-shutdown override (C1/H3)

**Files:** Modify `src/brain_v42/mcp/server.py` (entry ~540-546); Test `tests/integration/mcp/test_http_transport.py`.

- [ ] **Step 1: Write the failing integration test** — start the server with `BRAIN_MCP_TRANSPORT=http` + `METRICS_ENABLED=true` on an ephemeral loopback port in a background task; assert `GET http://127.0.0.1:PORT/mcp` (path confirmed in 0.2) returns a valid MCP initialize handshake; assert `tools/list` returns the brain tools; **and assert a `process_metrics` row appears within one flush interval** — this proves the lifespan actually FIRED under `run_http_async` (architect H5: some FastMCP versions only run the lifespan via `http_app()`; if the row never appears, flushers are dead under HTTP and Task 0.2's lifespan-fires check failed).
- [ ] **Step 2: Run — expect FAIL.**
- [ ] **Step 3: Implement** the transport branch + the `--http-server` CLI arg. The `__main__` block (server.py:356) has NO argparse today — add minimal parsing so `--http-server` sets transport=http AND serves as the reaper sentinel consumed by Task 4.1 (created HERE, before Batch 4/5 consume it). **Per Global Constraints, do NOT pass stateless_http/json_response to `FastMCP()`** — pass to the run call (shape from Task 0.2):
```python
# __main__: if "--http-server" in sys.argv: settings.brain_mcp_transport = "http"
if settings.brain_mcp_transport == "http":
    await mcp.run_http_async(
        transport="http", host=settings.mcp_http_host, port=settings.mcp_http_port,
        stateless_http=True, json_response=True,
        uvicorn_config={"timeout_graceful_shutdown": 10},   # H3: FastMCP hardcodes 0 → cancels in-flight
    )
else:
    await mcp.run_async(transport="stdio")
```
- [ ] **Step 4: Run — expect PASS** (incl. the flusher-row-under-HTTP assertion); also re-run stdio integration test (unchanged).
- [ ] **Step 5: Commit** `feat(mcp): BRAIN_MCP_TRANSPORT=http + --http-server arg, streamable-HTTP entry with graceful-shutdown override`

---

## Batch 3 — Security: Host/Origin enforcement + error masking (H1/C1)

### Task 3.1: Host/Origin ASGI middleware (jlowin FastMCP wires NONE)

**Files:** Create `src/brain_v42/mcp/http_security.py`; Test `tests/integration/mcp/test_http_security.py`.
**Interfaces:** Produces: `host_origin_guard(allowed_hosts: set[str], allowed_origins: set[str]) -> ASGIMiddleware` returning 421 on bad Host, 403 on bad Origin.

- [ ] **Step 1: Write the failing test** — with the http server up, a request carrying `Host: evil.com` → **421**; `Origin: http://evil.com` → **403**; `Host: 127.0.0.1:PORT` + no/loopback Origin → **200**. (Concurrent-safe; uses httpx against the live server.)
- [ ] **Step 2: Run — expect FAIL** (no guard → DNS-rebinding OPEN, the CVE-2025-66416 scenario).
- [ ] **Step 3: Implement** the middleware (pure Starlette/ASGI; allowlist `{127.0.0.1:*, localhost:*, [::1]:*}` built from settings — **explicitly populated**, empty rejects everything). Wire it via the injection point confirmed in Task 0.2 (`http_app(middleware=[...])` or `run_http_async(middleware=...)`). If 3.4.x exposes a real `TransportSecuritySettings`/`security_settings` passthrough (Task 0.2), prefer that AND keep the explicit allowlist; otherwise this middleware is the enforcement.
- [ ] **Step 4: Run — expect PASS.** **Step 5: Commit** `feat(security): explicit Host/Origin guard for the HTTP MCP server (DNS-rebinding, CVE-2025-66416)`

### Task 3.2: mask_error_details (don't leak the codex_ro DSN/paths)

**Files:** Modify the `FastMCP(...)` construction (server.py:113); Test `tests/integration/mcp/test_error_masking.py`.

- [ ] **Step 1: Write the failing test** — induce a tool error and assert the HTTP response body contains NO DSN substring (`@localhost:5433`), file path, or traceback frame.
- [ ] **Step 2: Run — expect FAIL.**
- [ ] **Step 3: Implement** — pass `mask_error_details=True` to `FastMCP(...)` (confirm kwarg exists in 0.2; else set the documented equivalent). Ensure intentional `ToolError` messages still surface.
- [ ] **Step 4: Run — expect PASS.** **Step 5: Commit** `feat(security): mask_error_details on the HTTP server (no DSN/path/traceback leakage)`

---

## Batch 4 — Reaper carve-out (C3, must land BEFORE any go-live)

### Task 4.1: Exclude the long-lived HTTP server from the stale-MCP reaper

**Files:** Modify `src/brain_v42/maintenance/reap_stale_mcp.py` (`is_brain_server_cmd` ~60 / `build_mcp_server_procs` ~237); Test: **EXTEND `tests/unit/test_reap_stale_mcp.py`** (flat path — `tests/unit/maintenance/` does NOT exist; reaper tests live here and use a `_proc(...)` factory that builds `McpServerProc` directly — reuse it).
**Layer note:** `select_stale_mcp_sessions(Iterable[McpServerProc])` is a pure age/group function with NO command visibility — it cannot see the sentinel. The carve-out MUST therefore live in `is_brain_server_cmd`/`build_mcp_server_procs` (exclude the `--http-server` proc from the candidate set entirely), and the test must target THAT layer (or the ps-snapshot → `build_mcp_server_procs` path), not `select_stale_mcp_sessions`. The `--http-server` sentinel already exists from Task 2.3.

- [ ] **Step 1: gitnexus impact** on `is_brain_server_cmd` / `build_mcp_server_procs`.
- [ ] **Step 2: Write the failing test** in `tests/unit/test_reap_stale_mcp.py` — build a ps snapshot (mirror the file's existing `_proc`/`parse_ps_snapshot` fixtures) with a `python -m brain_v42.mcp.server --http-server` line, parent `systemd --user`, age > 48h; assert `build_mcp_server_procs(snapshot)` does **NOT** include it (so it can never reach `select_stale_mcp_sessions`).
```python
def test_http_server_excluded_from_candidates():
    snap = _ps_snapshot([_ps_line(cmd=".../python -m brain_v42.mcp.server --http-server",
                                  etimes=200_000, ppid=SYSTEMD_USER_PID)])
    procs = build_mcp_server_procs(snap)
    assert all("--http-server" not in p.cmd for p in procs)   # never a reap candidate
```
- [ ] **Step 3: Run — expect FAIL** (today it becomes a candidate and the over-cap branch reaps it, :139-141).
- [ ] **Step 4: Implement** — in `is_brain_server_cmd` (or the filter inside `build_mcp_server_procs`), return False / skip when the cmdline contains the `--http-server` sentinel, so the long-lived server never enters the candidate set.
- [ ] **Step 5: Run — expect PASS** + full `pytest tests/unit/test_reap_stale_mcp.py`. **Step 6: Commit** `fix(reaper): never SIGTERM the long-lived --http-server (prevents 15-min fleet flap, C3)`

---

## Batch 5 — systemd unit + SPOF hardening (Open Q8)

### Task 5.1: `/health` liveness route

**Files:** Create `src/brain_v42/mcp/health.py`; wire into the http app (Task 0.2 injection point); Test `tests/integration/mcp/test_health.py`.

- [ ] **Step 1: Write the failing test** — `GET /health` returns 200 + JSON `{"status":"ok","pool":{"checkedout":N,"size":20}}` while the server holds the pool; a wedged-PG simulation returns 503.
- [ ] **Step 2: Run — expect FAIL.**
- [ ] **Step 3: Implement** a route that does a `SELECT 1` via the shared pool with a **bounded checkout** (`engine.connect()` under `asyncio.wait_for(..., timeout=2)`, or a dedicated 1-conn checkout) so a wedged/saturated pool returns 503 **fast** rather than blocking the watchdog probe itself; 200 on success, 503 on timeout/failure. This is what the external watchdog timer (Task 5.3) and red-monitor probe (a `Restart=always` cannot detect a wedged-but-alive loop).
- [ ] **Step 4: Run — expect PASS.** **Step 5: Commit** `feat(mcp): /health liveness route (pool + PG SELECT 1) for watchdog`

### Task 5.2: httpx + Neo4j pool ceilings for the shared client/driver

**Files:** Modify embedding httpx client construction + Neo4j driver construction (grep `httpx.AsyncClient(` and `GraphDatabase`/`AsyncGraphDatabase.driver` in `src/brain_v42/`); Tests alongside.

- [ ] **Step 1: gitnexus impact** on the embedding service client + neo4j driver factory.
- [ ] **Step 2: Write the failing test** asserting the httpx client is built with explicit `limits=httpx.Limits(max_connections=…)` and the neo4j driver with `max_connection_pool_size=…` (today: defaults; one shared client funnels the whole fleet → the GPU 503s under load).
- [ ] **Step 3: Run — expect FAIL. Step 4: Implement** explicit, bounded limits (values: httpx `max_connections=20`, `max_keepalive_connections=10`; neo4j `max_connection_pool_size=20` — tune later). **Step 5: Run PASS. Step 6: Commit** `perf(clients): bound shared httpx/neo4j pools for the single-server topology`

### Task 5.3: Author the systemd unit (from real `.tmpl`, NOT the non-existent brain-metrics.service)

**Files:** Create `deploy/systemd/brain-mcp-http.service.tmpl` (model on `deploy/systemd/brain-v42-dream.service.tmpl`); doc in `deploy/README` if present.

- [ ] **Step 1:** Author the unit:
**Default to `Type=simple` + an external `/health` watchdog timer** (architect: `Type=notify`/`WatchdogSec` needs uvicorn to emit `sd_notify`, which it does not — that's the stretch goal, not the go-live default). The bounded PG connect-retry in the lifespan (Task 2.2) handles cold-start, so `Restart=always` won't crash-loop on PG-not-ready and trip `StartLimitBurst`.
```ini
# brain-mcp-http.service.tmpl
[Unit]
Description=brain-v42 MCP HTTP server (shared pool)
After=network-online.target
Requires=network-online.target
[Service]
Type=simple
ExecStart=/bin/bash -lc '__REPO_ROOT__/.venv/bin/python -m brain_v42.mcp.server --http-server'
EnvironmentFile=__REPO_ROOT__/.env
Restart=always
RestartSec=2
StartLimitIntervalSec=300
StartLimitBurst=5
TimeoutStopSec=15
# NOT IOSchedulingClass=idle (cold-start penalty, blocker P1)
[Install]
WantedBy=default.target
```
```ini
# brain-mcp-http-watchdog.service.tmpl (oneshot, fired by a :30s .timer)
[Service]
Type=oneshot
ExecStart=/bin/bash -lc 'curl -fsS 127.0.0.1:__PORT__/health || systemctl --user restart brain-mcp-http'
```
- [ ] **Step 1:** Author both units (model on `deploy/systemd/brain-v42-dream.service.tmpl`; `--http-server` arg + transport already implemented in Task 2.3). `Type=notify`/`WatchdogSec` (via an `sdnotify` READY=1 from inside the lifespan `__aenter__` post-bind + a WATCHDOG=1 ping task) is a documented STRETCH variant — note it, don't block go-live on it.
- [ ] **Step 2: ⚠️ DO NOT `systemctl enable`/`start` in prod until Plan 2 (composite-PK metrics fix, spec C2) merges** — a prod HTTP server writing the `unknown` metrics bucket collides with live concurrent stdio writers. Dev-only `start` (no concurrent stdio writers) is safe for testing.
- [ ] **Step 3:** Manual verification (documented, not auto-run in CI): `systemctl --user daemon-reload && systemctl --user start brain-mcp-http && curl -s 127.0.0.1:PORT/health` → `{"status":"ok",...}`.
- [ ] **Step 4: Commit** `feat(deploy): systemd --user unit + external /health watchdog for the HTTP MCP server (crash-loop limits + PG ordering)`

---

## Plan 1 Self-Review (checklist)

- **Spec coverage:** C1 (Task 2.3 + Global Constraints), C3 (Batch 4), H1 (3.1), H2 (0.1), H3 (2.3), H4/H5 (2.2), H8 (1.1), pool+recycle (2.1), security/mask (3.x), SPOF/systemd (Batch 5). **Deferred to Plan 2:** C2, H6, H7, cross-repo contract. **Deferred to Plan 3:** H9, H10, gemini, canary. ✅ every Plan-1-scoped spec item maps to a task.
- **Placeholder scan:** API-gated tasks (2.2/2.3/3.1/3.2/5.1) explicitly depend on the Task 0.2 capture — that is a *real spike with exact commands*, not a placeholder. No "TBD"/"handle errors" left.
- **Type consistency:** `make_lifespan`, `host_origin_guard`, `--http-server` sentinel, `brain_mcp_transport`/`mcp_http_host`/`mcp_http_port` used consistently across tasks.

## Open decisions baked in (veto before execution)

Q3 interpreter → standardize on **3.12** (Docker deploy target); reconcile the local 3.14 venv separately. Q7 → `:9200` bind stays **out of scope** (separate hardening ticket). Q8 → **SPOF accepted, hardening mandatory** (Batch 5). Remaining (Q1 active_agents field, Q2 composite PK, Q4 TRUNCATE, Q5 gemini, Q6 brain-self header) belong to **Plan 2/3** and are defaulted in the spec.

## Execution handoff

Plan 1 complete. Plans 2 (metrics re-key + cross-repo contract) and 3 (fleet cutover) follow once Plan 1 merges and the open decisions are confirmed. Recommended execution: **subagent-driven**, ONE subagent per task with two-stage review between tasks (≤3 concurrent — no large bursts).
