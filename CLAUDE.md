# CLAUDE.md - brain_v42

## Overview

**Second Cerveau v42 — persistent memory for Claude Code over MCP.**

- **Type**: mcp-server
- **Stack**: Python 3.12+, FastMCP 3.x, SQLAlchemy 2.0 async, asyncpg, Alembic, pgvector, httpx, Pydantic 2, structlog
- **Status**: in active production; maintenance, hardening and the Sol Ultra roadmap in progress

**MCP transport**: production = HTTP loopback `http://127.0.0.1:8765/mcp`; configuration default and dev/fallback = `stdio`.

**Embedding topology**: production/default = local unified endpoint `http://localhost:8003`; the personal `dev-pc` deployment is a superseded rollback/reference path, now private.

---

## ⚠️ Living state does NOT live in this file

**This document describes RULES and a SHAPE. Everything that can be measured lives in
brain, or on the machine.**

This is not a style preference, it is a correction. This file announced a wrong schema
revision for ten days and a wrong killswitch for ten nights, and it contradicted itself
on the same number three paragraphs apart — because it copied measurements. A document
that copies a measurement cannot know it has aged: it stays green through the drift.

| What you want to know | Where to read it |
|---|---|
| Alembic head **in production** | `brain_fact_get("alembic_head")` |
| Head shipped **with the release** | `brain_fact_get("alembic_head_shipped")` |
| **Live** immutable release | `brain_fact_get("live_release_sha")` |
| **Declared** Dream killswitches | `brain_fact_get("dream_killswitches_declared")` |
| Last Dream night | `brain_fact_get("dream_last_night")` |
| Graph projection lag | `brain_fact_get("graph_projection_lag")` |
| Full catalogue of facts | `brain_fact_list` |
| Focus, roadmap, tickets, blockers, the night's failure | the `brain_session_start` briefing |
| Decisions, learnings, snippets, runbooks, ADRs | `brain_search` |
| State of an observable delivery | `brain_delivery_get` / `brain_delivery_list` |

A fact is measured against a **source identity declared independently by the
operator**: it cannot read the wrong database without saying so. A value copied into a
document can.

**If no fact covers what you are looking for: measure it, and say where the measurement
comes from.** Never copy a value found in a document — this one included.

Fallback commands, when the MCP server is unreachable:

```bash
# Schema head in production
docker exec brain_v42_postgres psql -U brain -d brain -Atc "select version_num from alembic_version;"
# SHA of the live release
systemctl --user show brain-mcp-http.service -p ExecStart | grep -o 'releases/[0-9a-f]\{8\}'
# Declared killswitches
cat ~/.config/systemd/user/brain-v42-dream.service.d/killswitches.conf
```

---

## TDD Workflow (MANDATORY)

Every new feature follows Red-Green-Refactor.

```
1. Describe the wanted feature
2. Write the TEST (it MUST fail) ─────────────── RED
3. Check the failure, and that it fails for the RIGHT reason
4. Implement the MINIMUM ─────────────────────── GREEN
5. Check that the test passes
6. REFACTOR if needed ────────────────────────── REFACTOR
7. Commit
```

| Command | Action |
|----------|--------|
| "TDD mode" | Enables the strict workflow |
| "Red phase" | Tests first, and they must fail |
| "Green phase" | The minimum to pass |
| "Refactor phase" | Improve, tests green |

**Strict rules**
- **NEVER** implement without a test that fails first.
- **NEVER** modify a test to make code pass.
- Minimum coverage **60%** (CI blocks below it).
- One test = one behaviour, not one implementation.

---

## Second Cerveau (persistent memory)

Claude Code has a persistent memory through the `brain_*` MCP tools.
**Use search and capitalisation PROACTIVELY.**

### Project Key

```
brain-v42
```

> ⚠️ **Always `brain-v42` (hyphen), NEVER `brain_v42` (underscore) nor `brain`.**
> The folder, the Python package and the GitNexus index are named `brain_v42`
> (underscore) — a misleading reflex. Validation converts the historical aliases
> `brain` and `brain_v42`, and rejects everything else. Write `brain-v42` explicitly
> in every call.

### Explicit session lifecycle

> **Strict exception — session lifecycle:** call `brain_session_start`,
> `brain_session_list`, `brain_session_resume`, `brain_session_capture`,
> `brain_session_heartbeat`, `brain_session_end` or `brain_session_abandon` only
> after the user's corresponding explicit command.
> No hook, auto-close, work delivery or end of response closes a session on the agent or client side.
> A delivered feature may update the roadmap, never close Brain.
>
> **Only exception, server-side:** the Dream `sweep` phase — shipped disabled and dry (`BRAIN_DREAM_SWEEP_ENABLED=false`, `BRAIN_DREAM_SWEEP_DRY_RUN=true`) — abandons an open session with no heartbeat for 7 days, with `abandonment_reason='auto_stale_7d'`.
> It writes neither a summary nor a `next_focus` and never touches the project focus.
> No agent, no hook and no client gains this right: `start`, `resume`, `end` and `abandon` remain explicit user commands.
>
> *(The two killswitch values quoted above are the CODE defaults, pinned by the
> documentation contract. The live state of the night is measured — see above.)*

| User command | Call |
|---|---|
| `brain session start` | `brain_session_start(project_key="brain-v42", client_key="<stable key>")` |
| `brain session list` | `brain_session_list(project_key="brain-v42", status="open")` |
| `brain session resume` | `brain_session_resume(session_id=..., expected_client_key=...)` |
| `brain session capture` | `brain_session_capture(session_id=..., expected_client_key=..., knowledge_ids=[...])` |
| `brain session heartbeat` | `brain_session_heartbeat(session_id=..., expected_client_key=...)` |
| `brain session end` | `brain_session_end(session_id=..., expected_client_key=..., summary=..., next_focus=..., expected_focus_revision=...)` |
| `brain session abandon` | `brain_session_abandon(session_id=..., expected_client_key=..., reason=...)` |

The v4 lifecycle rests on migration 037, which itself depends on 036. It has been active
in production since 24 July 2026.

**Keep `client_key`, `session.id` and `started_focus_revision` together.** The server
refuses an inconsistent pair before any mutation: it is a guard against mis-targeting
between parallel sessions, **not** authentication. Reuse the same `client_key` for the
retries of one session; use a distinct key per parallel session.

The capture ledger is **exclusive**: an artifact belongs to one session only. The server
requires the same project and a creation later than the session start. Provenance is
**declared** by the client, not proven.

`brain_session_end` attempts a compare-and-swap on the focus revision, then closes.
Current revision → `next_focus` applied, `focus_outcome="applied"`. Concurrent revision →
focus unchanged, session **closed anyway**, `focus_outcome="conflict"`. Identity, capture
or state errors stay fail-closed and leave the session open. `focus_revision` belongs to
the project, not to the session.

An open session with no heartbeat for 24 h exposes `is_stale=true`; `stale` is a
**derived** list filter, not a persisted status. The status stays `open`, and only the
7-day server-side sweep abandons a session without an explicit command.

> The arming state of the session flags (derived capture, auto-open, sweep) is an
> operator gesture that changes: **measure it in the environment of the live process**,
> do not read it here.

### Proactive capitalisation

| Situation | Tool |
|-----------|------|
| Before implementing something complex | `brain_search` — check whether it was already done |
| After a technical decision | `brain_log_decision`, with the WHY |
| After discovering a bug/fix | `brain_learn` |
| Reusable code | `brain_save_snippet` |
| Feature shipped | `brain_feature_update(feature, 'deployed'\|'done', project_key)` |
| Action to pick up later | `brain_ticket_create(...)` — self-tickets are valid |

### Choosing the right tool (MANDATORY — do NOT put everything in `brain_learn`)

| Question | Tool |
|----------|------|
| Did I choose between options? (A vs B, lib X vs Y) | `brain_log_decision` |
| Do I have a reusable pattern/code? | `brain_save_snippet` |
| Do I have a reproducible step-by-step procedure? | `brain_create_runbook` |
| Do I have a lasting architectural decision? | `brain_propose_adr` |
| Is it a pure insight/gotcha, with no code and no choice? | `brain_learn` |

**`brain_learn` is the LAST resort, not the default.**

### Tools

Session lifecycle: `brain_session_start` · `_list` · `_resume` · `_capture` ·
`_heartbeat` · `_end` · `_abandon`.
Knowledge: `brain_log_decision` · `brain_learn` · `brain_save_snippet` ·
`brain_create_runbook` · `brain_propose_adr` · `brain_search` · `brain_get`.
Project: `brain_update_project_focus` · `brain_get_roadmap` · `brain_feature_create` ·
`brain_feature_update`.
Measured facts: `brain_fact_list` · `brain_fact_get`.
Delivery: `brain_delivery_*`.

`brain_feature_create` serves an **explicit** roadmap intention only: inspect
`brain_get_roadmap` first, and the `project_context` must exist. It does not go through
`ClusterGuard` and does not deduplicate semantically.

The default MCP catalogue is `compact`: the session lifecycle tools stay visible, and
everything else goes through `brain_find_tool` / `brain_call_tool`.

---

## Hosts and access

Machine access — hosts, users, SSH keys, Docker contexts — is deliberately **not** written
here: the repository is public, and this file is published with it. It lives in brain,
where it belongs:
`brain_get(entity_type="learning", entity_id="2a23883d")`, or
`brain_search(query="dev machines access", project_key="brain-v42")`.

---

## Internal working material

Plans, specs, design notes, receipts and handoffs are dated work notes, not product
documentation, and this repository is public: they are written under `internal/`, the
private `brain-v42-internal` repository cloned at the repository root
(`git clone git@github.com:hawkixs/brain-v42-internal.git internal`; the clone is
git-ignored). Write new ones at the same relative path this repository used before ticket
`8dc6f0d2` moved the existing ones out, e.g. `internal/docs/superpowers/plans/<name>.md`,
`internal/docs/receipts/<date>-release-<sha>.md` — **never** directly under `docs/`.
`docs/` keeps only product documentation: how the software works, or how anyone would
operate it.

---

## Quick Start

From the canonical root — the main checkout, never a worktree:

```bash
uv sync --extra dev --python 3.12
source .venv/bin/activate
```

> **`pip install -e ".[dev]"` does not work here**, and it never has:
> `headless-agents` is a uv workspace member (`[tool.uv.sources]`), not a published
> distribution — pip looks for it on PyPI and fails. `--python 3.12` is mandatory:
> `requires-python` is `>=3.12`, so a bare `uv sync` picks the most recent interpreter
> on the machine while the whole CI targets 3.12.

```bash
pytest tests/unit -v                              # DB-backed tests skip without BRAIN_V42_TEST_DB_URL
pytest tests/integration -v                       # services required
pytest --cov=brain_v42 --cov-report=term-missing
uv run python -m brain_v42.mcp.server             # MCP stdio (dev/fallback)
```

---

## Project Structure

```
brain_v42/
├── src/brain_v42/
│   ├── config.py              # pydantic-settings — the single configuration surface
│   ├── db/                    # SQLAlchemy engine + tables
│   ├── models/                # Pydantic models
│   ├── repositories/          # CRUD + FTS + pgvector + graph adapters
│   ├── services/              # business logic, embedding, reranker, dream, dedup
│   ├── facts/                 # registry of measured facts
│   ├── metrics/               # sidecar + collector + cockpit
│   ├── automation/            # independent webhook/dedup runtime
│   └── mcp/                   # FastMCP server + brain_*/dream_* handlers
├── packages/headless-agents/  # uv workspace member, separate distribution
├── tests/{unit,integration}
├── alembic/versions/          # migrations (shipped inside the wheel)
├── scripts/                   # operational CLIs (dream.sh, canaries, repair)
├── services/                  # GPU embedding service + shim + supervisor
├── deploy/                    # systemd units, per-host compose, install.sh
└── docs/                      # ARCHITECTURE, SCHEMA, MCP_TOOLS, OPERATIONS, runbooks
```

The high-level module graph is **acyclic, checked in CI**
(`scripts/check_module_layering.py`). Beware: the guard is **static** — an import made at
runtime (`import_module`) is invisible to it, and bypasses the rule instead of respecting
it.

---

## Architecture

MCP over HTTP loopback in production, stdio in dev/fallback → PostgreSQL 16 + pgvector + Neo4j 5.

- **PG (5433)** — source of truth: CRUD, FTS, pgvector semantic search
- **Neo4j (bolt 7687, browser 7474)** — relation index, traversals, invisible to the LLM
- **GPU embedding** — Qodo-Embed-1-1.5B GGUF Q8_0 via llama.cpp + a Starlette shim on
  `localhost:8003`, 1536 dims; PyTorch rollback: `docker compose --profile legacy up -d embedding`
- **Reranker** — unified embedding endpoint `:8003/rerank` (ONNX cross-encoder on CPU)
- **Schema** managed by Alembic; the **repository** carries migration 057. The **live**
  head is measured, it is not read here.
- **Ledger/outbox** PostgreSQL → Neo4j, active in production
- **Network model**: personal agents on a trusted LAN; MCP, PostgreSQL, Neo4j and the
  dedicated automation runtime are constrained to loopback. Metrics are bound there by
  default but their bind stays configurable; while the legacy automation compatibility is
  active, its `/gitlab/webhook` route shares that bind. `:8003` carries **no application
  bearer** — what closes it is the bind, not authentication, and a client placed on
  `brain-net` reaches it without a token. Check the upstream network boundary, and never
  expose it to the Internet.

### Execution model: immutable release

The systemd writers run from `~/.local/share/brain-v42/releases/<sha>/`: a dedicated
venv, `brain_v42` installed as a **wheel** (not editable), `PYTHONSAFEPATH=1`. All of it is
set by the `90-immutable-release.conf` drop-ins.

**Consequence: a merge on `main` is active neither at the next restart nor on the next
night.** A new release has to be built and switched over, backed by the fail-closed
preflight `scripts/check_delivery_deployment.py`. Side effect: `dream.sh` computes its logs
relative to itself, so `logs/dream/` lives under `releases/<sha>/brain-v42/logs/dream/`,
no longer in the repository.

> The live SHA is **measured** (`brain_fact_get("live_release_sha")`), never copied.

### Observable delivery and attestations

`delivery_*` tables; `brain_delivery_*` tools. `brain_ticket_transition(action='resolve')`
refuses with `delivery_requirements_unsatisfied` while the observed proof is missing — that
refusal in the MCP log is the **expected behaviour**, not an error.

**Ledger/policy boundary with red-rail**: brain STORES the facts and validates their
**shape** (`kind` vocabulary, JSON payload, server-computed digest); it **never** judges the
`kind` and derives no completion refusal from it. red-rail owns the policy and computes the
DORA metrics by reading these rows. The API is published as data in
`docs/contracts/delivery_attestations.json`, for consumers that must not import
`brain_v42`.

### Network boundary and SEC2 residuals

The three paragraphs below are **synchronised by
`tests/unit/test_documentation_contract.py`** with ARCHITECTURE and OPERATIONS (README keeps
a shorter summary): they cannot drift apart between documents in silence. Re-measure them
together, or not at all.

**Tracked network boundary** (replayed 2026-08-23): MCP, PostgreSQL and Neo4j bind to loopback; metrics and automation default to loopback. The versioned Compose target binds the embedding host publish to loopback and the live runtime matches it — measured `127.0.0.1:8003`, with the host's own LAN address refusing the connection. Application bearer authentication is armed and enforcing: `MCP_HTTP_TOKEN` is set and non-empty in the live server process, and `POST /mcp` answers `401` both without a bearer and with a wrong one. The dedicated Docker client network exists and carries the clients: `brain-net` holds the embedding shim and both `auto-discord` containers. Repository-managed WAN isolation remains unproven — the repository manages no firewall rule at all. What would make this paragraph false again, and is watched by no test: a host-publish override reopening `:8003`, or `MCP_HTTP_TOKEN` cleared. `METRICS_HOST` has LEFT that list: since 2026-09-03 (`6c61b63`) a fail-closed validator refuses a non-loopback bind unless `METRICS_ALLOW_NON_LOOPBACK` names the decision, and under that opt-in the three POST receivers stay unregistered and say so on `/healthz`. Re-measure with `ss -ltnp`, `docker port` and an unauthenticated `POST /mcp` — do not copy this line forward.

**Embedding shim limits (ROLLED OUT 2026-08-21, temps 1)**: 8 MiB body, 5 s body-read timeout, 8 concurrent ingress reads, 100 embed texts, 128 rerank candidates, maximum JSON depth 64, one embedding calculation and one rerank calculation per worker. Saturation returns short `503` JSON with `Retry-After: 1`.

**SEC2 residuals** (replayed 2026-08-23): bearer authentication and the dedicated Docker client network are done — the coordinated `auto-discord` cutover happened, and both `auto-discord` containers sit on `brain-net`. One residual stands, and it is wider than previously written: the versioned legacy PyTorch profile remains unbounded — `services/embedding/main.py` carries no body cap, no read deadline, no concurrency semaphore and no `413`/`503` — and it preserves neither of the two DNS names its clients use. A `--profile legacy` rollback publishes `embedding` and `brain_v42_embedding` on `brain-net`, while the compose sets `EMBEDDING_URL=http://embedding-shim:8003` and the running bot, carrying no `EMBEDDING_URL` of its own, falls back to the code default `http://brain_v42_embedding_shim:8003`. Two names break, not one.

---

## Configuration

**Environment variables (shared `.env`):**

```bash
# Required
POSTGRES_URL=postgresql+asyncpg://brain:<password>@localhost:5433/brain

# Optional
LOG_LEVEL=INFO
EMBEDDING_SERVICE_URL=http://localhost:8003
EMBEDDING_DIMENSION=1536
RERANKER_URL=http://localhost:8003

# Production graph
GRAPH_ENABLED=true
GRAPH_LEDGER_WRITE_ENABLED=true

# MCP tool catalogue
BRAIN_MCP_PROFILE=compact

# Dream killswitches — CODE DEFAULTS, not the live state of the night (see below)
BRAIN_DREAM_EXTRACT_ENABLED=false
BRAIN_DREAM_EXTRACT_DRY_RUN=true
BRAIN_DREAM_ROADMAP_ENABLED=false
BRAIN_DREAM_ROADMAP_DRY_RUN=true
BRAIN_DREAM_SWEEP_ENABLED=false
BRAIN_DREAM_SWEEP_DRY_RUN=true
```

> **Add no `NEO4J_*` and no `GRAPH_PROJECTOR_*` key to the shared `.env`.** The projector
> reads its own private file `~/.config/brain-v42/graph-projector.env` (mode `0600`),
> which holds exactly its four keys:

```bash
GRAPH_PROJECTOR_ENABLED=true
GRAPH_PROJECTOR_NEO4J_URL=bolt://127.0.0.1:7687
GRAPH_PROJECTOR_NEO4J_USER=neo4j
GRAPH_PROJECTOR_NEO4J_PASSWORD=REPLACE_WITH_ROTATED_PASSWORD
```

**The CODE defaults of the Dream killswitches are NOT the live state of the night.** The
versioned `.env` ships them disabled; the systemd drop-in arms some of them. Read the
current value with `brain_fact_get("dream_killswitches_declared")`, never from a history
comment.

Private files, never in the shared `.env` and never read other than by key name:
`~/.config/brain-v42/mcp-token.env`, `graph-projector.env`, `delivery-observer.env`,
`delivery-mcp.env`.

### Dream: what to know without measuring it

- The night is **a loop inside one run**, never N systemd units: the `dream.sh` lock is
  global, so later invocations would exit 0 with "already running, skipping".
- The pool lives in `BRAIN_DREAM_PROJECT_POOL`, **comma-separated, never with spaces**:
  `Environment=` splits on unquoted whitespace and would silently drop projects.
- **Three sets not to confuse**: the POOL (loop phases); what `sweep` touches
  (**global**, every open session); the `roadmap` window (its own rotation, independent of
  the pool).
- The global phases — `extract`, `roadmap`, `sweep` — run **outside** the loop.
- Retry is a **night-wide** allocation (`BRAIN_DREAM_RETRY_BUDGET`), not a per-phase one.
- The `MCP_HTTP_DREAM_TOKENS` registry requires a **complete matrix** (six phases × every
  project of the pool). Adding a project without re-minting the registry fails its
  preflight, **and therefore the whole night**: fail-closed, and intended. Mint it with
  `scripts/mint_dream_capability_registry.py`, never with `echo` (the argument would stay
  in the shell history).
- Phases run on an **ordered chain of providers**, preflighted before the night. An
  exhausted link falls through to the next one; a link that is dead before any Brain call
  is replayable and withdrawn for the night, not charged to the budget. Each phase writes
  who served it to `logs/dream/<date>_<project>_<phase>.chain.json` — **read that file**:
  a green night on the first link proves nothing about the fallthrough.

---

## Tests

```bash
pytest tests/unit -v
pytest tests/integration -v
pytest --cov=brain_v42 --cov-report=term-missing
```

> **`brain_test` is SHARED and it is not empty.** An integration fixture places itself in
> the future or inside a rolled-back transaction — never a `TRUNCATE`.
>
> **`tests/integration/db/` runs on a DISPOSABLE database** per session
> (`brain_migration_*`, ticket `f7af0977`), and a test there refuses `brain` and
> `brain_test` by name. **Never** add an `alembic upgrade head` gate on a shared database:
> it would stay ahead of `main`, and the residue guard would refuse every later
> integration.
>
> **Outside the canonical root, export `POSTGRES_URL` before `pytest tests/unit`.** A
> worktree does not carry the gitignored `.env`: tests then fail for that reason alone,
> and "identical to `main`" proves "not caused by my branch", never "unimportant" — one
> of those reds was the very test catching a regression (learning `09900e36`).

---

## CI/CD

GitHub Actions is the **sole CI/CD authority** since the GitLab rail was retired (decision
`218028c7`); the GitLab project now only serves as the `archive-main` mirror.

**Three separate rails:**

- `.github/workflows/continuous-integration.yml` — on `pull_request` **only**: lint, tests
  and security on hosted `ubuntu-24.04`.
- `.github/workflows/continuous-delivery.yml` — on `push` to `main` only: `build-docker`
  alone, on hosted `ubuntu-24.04`, publishing `ghcr.io/hawkixs/brain-v42` with the
  workflow's own token (`packages: write`, this job only). **No repository secret.**
  **No deployment step**: rolling out a pushed digest remains a manual operator gesture,
  out of band.
- `.github/workflows/release.yml` — on push of a `v*` tag only: builds the wheel and the
  sdist, reuses `tests/unit/test_wheel_ships_migrations.py` to prove that the wheel ships
  the migrations, and refuses a tag that does not name the built version. Runs on hosted
  `ubuntu-24.04`. **No image**: an image would bake in the reranker's ONNX model, whose
  upstream licence NOTICE declares undetermined.

**No rail runs on a self-hosted runner** (ticket `03846021`, 2026-09-23). The former
brain-v42 runner was deleted by GitHub after fourteen days offline, and a public
repository must not run Docker as root on a VM it shares with private repositories: a
compromised job there would reach the other runners and their secrets.

The pinning gate `scripts/check_container_image_pins.py` covers `.github/workflows/`: a CI
image not pinned by digest is refused there.

---

## Code Conventions

**Python** — type hints everywhere; async/await for I/O; Pydantic for validation;
structured logging (never `print`); docstrings that say WHY.

```bash
ruff check src/ tests/
ruff format --check src/ tests/     # CI runs --check; `ruff check` alone is NOT ENOUGH
mypy src/
```

---

## Git Workflow (MANDATORY)

**Always commit, at minimum.** NEVER leave finished work uncommitted: as soon as a unit of
work is green, commit. It is the default behaviour, not an option.

- Atomic commits + Conventional Commits (`fix(...)`, `feat(...)`, `chore(...)`)
- Check green BEFORE committing: `pytest tests/unit`, `ruff check`, `ruff format --check`, `mypy src/`
- Split into logical commits (a security fix ≠ a docs chore)
- Push only on request; the local commit, for its part, is systematic

### The PR gate is NEVER bypassed

**Every change that reaches `main` goes through a pull request.** No exception, no "just
this once". If the user says "push" while the work sits on local `main`, it means: create a
branch, push it, open the PR — without asking again for confirmation of that detour.

Forbidden, and it is one prohibition under different names:

- `git push origin main` (or to any default branch);
- `gh pr merge --admin`, which overrides the required checks;
- `git commit --no-verify` / `git push --no-verify`;
- disabling, bypassing or "unblocking" a branch protection;
- merging a PR whose CI is not complete and green.

The reason is **mechanical**: `continuous-integration.yml` triggers on `pull_request` only.
A direct push to `main` therefore runs **no** lint, test or security gate — it only
triggers `continuous-delivery.yml`, which builds an image and tests nothing. Bypassing the
PR saves no time: it removes the verification, silently, and `main` is the base of every
immutable release.

If a PR is blocked by a gate that is wrong, the action is to **fix the gate in a PR**, not
to go around it.

### Language: ENGLISH on GitHub (MANDATORY)

**Every artifact published on GitHub is written in English.** Commit messages (subject AND
body), branch names, PR titles and descriptions, issues, review comments, release notes,
tag messages. It is the default, not an option to ask about again.

Scope = what goes to a GitHub remote. **Out of scope, deliberately**: the conversation
with the user (French) and untracked working notes. This file and `AGENTS.md` are tracked
and published since 2026-09-22 (ticket `5081f3ff`): they are in scope like any other file.

Repository files: every **new** pushed file is written in English. **Existing** French
docs stay consistently French until a translation pass that is explicitly decided — do not
inject English paragraphs into them: franglais by accretion is worse than either language.

**No gate watches this rule.** Its failure mode is a silent return to French with nothing
turning red. History is not rewritten: a repository with commits in another language is a
backlog to report, not a licence to carry on.

---

## Context

Successor of `datalake_v2`. The docs in `docs/` come from its planned redesign, but the
code starts from scratch.

**`datalake_v2` problems**: HTTP chain anti-pattern (7 containers, 4 hops); Neo4j used as a
plain CRUD store; sentence-transformers/PyTorch too heavy; FastAPI useless (the only
consumer is MCP); Redis unused, monitoring overkill.

**brain_v42 goal**: a minimal and performant architecture, TDD from scratch.

**Focus / current state**: kept in brain (`project_context`), the single source of truth.
On an explicit opening, read `brain_session_start(...).session.started_focus`; on an open
session, `brain_session_resume(...).current_focus`. **Never call `start` as a mere read.**
Deliberately not duplicated here.

---

# GitNexus — operating notes

This project is indexed by GitNexus as **brain-v42**. This section is written by hand:
GitNexus no longer injects a generated block into this file or into `AGENTS.md`
(ticket `07b9e892`), so change it like any other rule, through a pull request.

## Always Do

- **Run `impact({target: "symbolName", direction: "upstream"})` before modifying a
  symbol**, and report the blast radius: callers, affected processes, risk.
- **Run `detect_changes({scope: "all"})` before committing.** `partial: true` or
  `truncated: true` is not a clean check — a zero there means unseen, not unaffected:
  re-run it.
- **Warn** on HIGH or CRITICAL risk, and never waive it with `riskSharedAxes`.
- **Treat `risk: UNKNOWN` as unresolved, not as low.** An empty caller set can also mean
  callers the index cannot resolve (plain-object property access, dynamic dispatch,
  cross-language calls): confirm with a text search before treating the symbol as safe to
  change or delete.
- Explore with `query({search_query: "concept"})` rather than with grep; full context of a
  symbol: `context({name: "symbolName"})`. For a security review, `explain({target: ...})`
  lists taint findings when the index was built with `--pdg`.

## Never Do

- Never rename by find-and-replace — use `rename`, which understands the call graph.
- Never commit before the graph change analysis above.

> **A stale index answers CRITICAL/high wrongly.** Check freshness first
> (`gitnexus://repo/brain-v42/context`); if the index is stale, **measure the blast radius
> by hand and say so** rather than report a false verdict.

## Indexing rule

**Index the canonical root, never a worktree.** The registry once carried two `brain-v42`
entries — the root and a worktree 893 commits behind — and an `impact` could resolve
against the stale index without saying so. Check with `npx gitnexus list` that only one
entry exists.

## Reindex: always `--index-only`

Without it, `gitnexus analyze` copies standard skills into `.claude/skills/` and
`.agents/skills/` — reinstalling skills that were pruned on purpose — and writes a
generated section into this file and into `AGENTS.md`. The nightly reindex
(`scripts/gitnexus-nightly.sh`, cron 04:30) runs this line, pinned by
`tests/unit/test_gitnexus_nightly_script.py`; a manual reindex reuses it:

```bash
gitnexus analyze --embeddings --index-only --wal-checkpoint-threshold 67108864 .
```

A tool or a hook that suggests a bare `gitnexus analyze` (the stale-index hint does) is to
be read with that flag added. If a diff ever shows a `gitnexus:start` block reappearing in
this file, a reindex ran without it: revert the block, do not keep it.

## GitNexus skills are called by NAME, never by a path

Invoke them through the `Skill` tool, by name. Which ones a session has loaded, and from
which copy, is measured state: read it from the session's skill list, never from this
file. Skills nested one directory level deeper than the scan expects are never loaded,
and nothing turns red (learning `5d6451bf`).
