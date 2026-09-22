# brain-v42

> Persistent memory for coding agents, served over MCP.

brain-v42 gives Claude Code, Codex and any other MCP client a durable second brain:
decisions, learnings, code snippets, runbooks, ADRs, tickets and project roadmaps —
stored in PostgreSQL, retrieved by full-text + semantic search with reranking, and
consolidated every night by an agent pipeline.

- **Typed knowledge, not a notes dump** — a decision records its WHY and alternatives;
  a snippet records its intent; a runbook records executable steps. Each type has its
  own lifecycle (supersession chains, ADR acceptance, learning validation).
- **Explicit session lifecycle** — the user owns every session boundary. Sessions
  capture the artifacts they produced, and closing is fail-closed: a session ends with
  either captured knowledge or an explicit "nothing to capture" reason, never silence.
- **Search that ranks** — pgvector semantic search + PostgreSQL FTS, fused and
  re-ranked by a cross-encoder.
- **Nightly consolidation ("dream")** — an agent pipeline cleans orphan links, merges
  duplicates, synthesises learnings and proposes promotions, behind per-phase
  killswitches that all ship closed.
- **Multi-project** — per-project focus with compare-and-swap revisions, roadmaps,
  cross-project tickets.
- **Observable delivery** — versioned delivery contracts bind addressed work to a
  pull request, persisted CI evidence, integration, and policy-governed fulfillment.
- **Measured facts, not remembered ones** — a closed catalogue of named probes reads
  live state (schema head, running release, declared killswitches) against a source
  identity the operator declared independently, so a briefing states what *is* rather
  than what someone last wrote down.

## Architecture

```
Claude Code / Codex (MCP client)
       │ HTTP loopback :8765/mcp (production) · stdio (dev/fallback)
  brain-v42 (FastMCP)
       ├── SQLAlchemy async ─▶ PostgreSQL 16 + pgvector   (source of truth)
       ├── HTTP ─────────────▶ embedding endpoint :8003   (optional, pluggable)
       ├── HTTP ─────────────▶ :8003/rerank               (optional reranker)
       └── bolt ─────────────▶ Neo4j 5 Community          (relationship index, optional)
```

**MCP transport**: production = HTTP loopback `http://127.0.0.1:8765/mcp`; configuration default and dev/fallback = `stdio`.

PostgreSQL is the single source of truth. Neo4j is a disposable projection fed by a
relational ledger/outbox — it can always be rebuilt from PostgreSQL, never the other
way around. The canonical path is active in production since 22 July 2026; design and
evidence live in [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) and the
[graph ledger runbook](docs/GRAPH_LEDGER_RUNBOOK.md).

Embeddings are optional and pluggable, and they degrade gracefully when the endpoint
is away — `brain_search` falls back to full-text search, writes persist with a `NULL`
embedding and are backfilled later. An install with no embedding endpoint at all
works.

Two wire formats ship, selected by `BRAIN_EMBEDDING_BACKEND`:

| Backend | Wire | Use it for |
|---------|------|------------|
| `shim` (default) | `POST /embed`, `POST /embed/query`, `GET /healthz` | The bundled reference stack (`services/`), serving Qodo-Embed-1-1.5B as GGUF via llama.cpp on a local GPU |
| `openai` | `POST /v1/embeddings` | Any OpenAI-compatible endpoint — Ollama, vLLM, llama.cpp server, LM Studio, TEI, Jina, Mistral, Voyage, OpenAI |

So a machine without a GPU needs no bundled stack. Point it at whatever serves
embeddings, for example a local Ollama:

```bash
BRAIN_EMBEDDING_BACKEND=openai
BRAIN_EMBEDDING_SERVICE_URL=http://localhost:11434
BRAIN_EMBEDDING_MODEL=nomic-embed-text
EMBEDDING_DIMENSION=768   # unprefixed on purpose — see Dimension below
```

Reranking is separately pluggable via `BRAIN_RERANK_BACKEND` (`shim`, or `cohere`
for the `POST /v1/rerank` shape implemented by TEI, Jina and vLLM), and stays
best-effort: an unavailable reranker falls back to RRF ordering rather than
failing a search.

### Instruction prefixes

Asymmetric models (Qodo, E5, BGE) expect queries and documents to be marked
differently. Both prefixes default to empty, which is correct for symmetric
models and reproduces the unprefixed behaviour exactly:

```bash
BRAIN_EMBEDDING_QUERY_PREFIX="query: "        # applied to searches only
BRAIN_EMBEDDING_DOCUMENT_PREFIX="passage: "   # applied to everything written
```

Keep the trailing space if the model's card shows one — it is part of the prefix.
Changing the **query** prefix is free. Changing the **document** prefix on a
populated corpus requires a full `scripts/regen_embeddings.py` pass, or the column
ends up holding two incompatible vector populations with nothing to flag it.

### Dimension

`EMBEDDING_DIMENSION` is chosen at install time and must be ≤ 2000, the ceiling
pgvector's HNSW index accepts. Switching models later means re-embedding the corpus
(`scripts/regen_embeddings.py`).

Set it **unprefixed**. Every other setting here also answers to a `BRAIN_`-prefixed
name, but the ORM column widths are read straight from `EMBEDDING_DIMENSION` in
`db/tables.py`, so `BRAIN_EMBEDDING_DIMENSION=768` alone would leave the tables at
1536 while the rest of the process believed 768.

One caveat to know before a non-default install: the ORM honours this setting, but
four migrations (`002`, `005`, `009`, `014`) hardcode `vector(1536)`, so a fresh
`alembic upgrade head` creates 1536-wide columns whatever the setting says. Until
that is fixed, a non-1536 install needs the columns retyped and their HNSW indexes
rebuilt by hand after migrating. If you are running the reference stack at 1536,
this does not affect you.

## Quick start

```bash
git clone https://github.com/hawkixs/brain-v42 && cd brain-v42
uv sync --extra dev --python 3.12      # creates .venv; see "Development" for why not pip
source .venv/bin/activate

# 1. Local Neo4j secret (skip if you run without the graph)
install -d -m 0700 .secrets
read -rsp "Neo4j password (same value as NEO4J_PASSWORD in .env): " PW
(umask 0022; printf 'neo4j/%s\n' "$PW" > .secrets/neo4j-auth); unset PW

# 2. Databases (PostgreSQL 16 + pgvector, Neo4j)
docker compose up -d

# 3. Migrations
export POSTGRES_URL="postgresql+asyncpg://brain:REPLACE_WITH_PASSWORD@localhost:5433/brain"
BRAIN_ALEMBIC_ALLOW_PROD=1 alembic upgrade head

# 4. Run the MCP server (stdio)
python -m brain_v42.mcp.server
```

Wire it into Claude Code — `.mcp.json` at the repo root already targets the production
HTTP loopback endpoint; for a plain stdio dev setup:

```bash
claude mcp add brain-v42 -- python -m brain_v42.mcp.server
```

`BRAIN_ALEMBIC_ALLOW_PROD` is required only when the database name is exactly `brain`;
keep it a one-command opt-in, never exported persistently. Alembic rejects DSN query
parameters; use the plain form above with host, port, username and password all present.

## MCP tools

| Domain | Tools |
|--------|-------|
| Search & list | `brain_search`, `brain_list`, `brain_get`, `brain_update`, `brain_delete` |
| Graph traversal | `brain_get_neighbors`, `brain_graph_path` |
| Session lifecycle | `brain_session_start`, `brain_session_list`, `brain_session_resume`, `brain_session_capture`, `brain_session_heartbeat`, `brain_session_checkpoint`, `brain_session_end`, `brain_session_abandon` |
| Project context | `brain_set_project_context`, `brain_update_project_focus`, `brain_focus_history`, `brain_list_projects`, `brain_list_project_groups`, `brain_project_archive`, `brain_project_unarchive` |
| Decisions | `brain_log_decision`, `brain_supersede_decision`, `brain_get_supersession_chain` |
| Learnings | `brain_learn`, `brain_validate_learning` |
| Snippets | `brain_save_snippet`, `brain_use_snippet` |
| Runbooks | `brain_create_runbook`, `brain_promote_runbook`, `brain_get_runbook`, `brain_execute_runbook` |
| ADRs | `brain_propose_adr`, `brain_promote_adr`, `brain_accept_adr`, `brain_deprecate_adr` |
| Coordination | `brain_ticket_create`, `brain_ticket_reply`, `brain_ticket_transition`, `brain_ticket_list`, `brain_ticket_get` |
| Observable delivery | `brain_delivery_contract_set`, `brain_delivery_bind_pr`, `brain_delivery_get`, `brain_delivery_list`, `brain_delivery_refresh`, `brain_delivery_claim`, `brain_delivery_claim_renew`, `brain_delivery_claim_release`, `brain_delivery_accept`, `brain_delivery_attest`, `brain_delivery_attestation_list` |
| Dream / graph | `brain_get_clusters`, `brain_backfill_links_batch`, `brain_consolidation_candidates`, `brain_merge_entities`, `brain_refresh_entity`, `brain_reindex_plans`, `brain_list_orphans_for_classification`, `brain_assign_domain`, `brain_list_curation_proposals`, `brain_reject_curation_proposals`, `brain_apply_curation_proposal` |
| Roadmap & decay | `brain_get_roadmap`, `brain_feature_create`, `brain_feature_update`, `brain_decay_status` |
| Workflow guidance | `brain_workflow_guide` |
| Measured facts | `brain_fact_list`, `brain_fact_get` |
| Claims | `brain_claim_verify` |

Full catalog with signatures: `docs/MCP_TOOLS.md`.

The default catalog profile is `compact`: the seven session lifecycle tools stay
visible, and every other tool is reached through two gateways — `brain_find_tool`
to discover, `brain_call_tool` to invoke. Set `BRAIN_MCP_PROFILE=native` to expose
every tool directly.

## Observable delivery

One delivery path is `contract → PR binding → persisted PR/CI observation →
integration receipt → explicit requester acceptance`. A merge receipt proves the
contracted revision reached its target branch. Contracts with
`acceptance_mode=explicit` require the requester to accept the current attempt and
delivery digest; automatic contracts can produce fulfillment without that decision.

Brain stores and evaluates this evidence. It does not launch agents, choose work,
push commits, merge pull requests, or deploy releases. External orchestrators keep
those responsibilities. Delivery reads use persisted observations and make no
GitHub calls. With `BRAIN_DELIVERY_ENABLED=false`, mutations pause while reads and
the completion guard for existing contracts remain active.

### Ledger and policy: the boundary with red-rail

Brain is the **ledger**; [red-rail](https://github.com/hawkixs) is **policy**. The
split is deliberate and it is the reason attestations exist as a separate table
(ticket `04bc1f4a`).

`brain_delivery_attest` records one issuer-declared fact about a ticket's delivery
workflow — `released`, `deployed`, `rolled_back`, `incident_detected`, `gate_passed`
and their kin. Brain validates the **form** and nothing else: the kind must match
`^[a-z][a-z0-9_]{0,63}$`, the payload must be a bounded JSON object, and the server —
never the issuer — computes the digest. Brain never judges what a kind *means*, never
derives a completion refusal from an attestation, and never updates or deletes one.
The well-known kinds above are documentation, not an allowlist.

That restraint is what makes the rows usable: red-rail reads them to compute DORA
metrics under its own policy, which can change without a schema migration here.
Attestations also arrive legitimately after a ticket has closed — an incident or a
rollback does not wait for a workflow state — so no ticket-status restriction applies.

For consumers that must not import `brain_v42`, the whole API is published as data in
[`docs/contracts/delivery_attestations.json`](docs/contracts/delivery_attestations.json)
— kinds, bounds, the digest recipe with its test vectors, the stable error codes and
the list scopes — and a unit test keeps that file equal to the code.

The observer is a separate process with separate credentials. Its dedicated
`~/.config/brain-v42/delivery-observer.env` must be an owned, regular, non-symlink
file with mode `0600`. It carries `BRAIN_DELIVERY_ENABLED=true`, an explicit
`BRAIN_DELIVERY_POSTGRES_URL`, the repository registry, and either a dedicated
GitHub token or a complete GitHub App credential set. Keep the observer's GitHub
credentials out of the shared application environment; the application keeps its
own existing PostgreSQL configuration. The immutable service command is:

```text
<release>/venv/bin/python -m brain_v42.delivery_observer --env-file ~/.config/brain-v42/delivery-observer.env
```

Each host release lives under
`~/.local/share/brain-v42/releases/<full-source-sha>/` and retains the same-SHA
source archive, wheel, lock, copied Python 3.12 environment, and hashed manifest.
The archive supplies Dream and root scripts that the wheel does not install. Build
and installed-wheel checks do not attest a rollout. Follow the
[immutable delivery release and canary runbook](docs/runbooks/2026-09-07-observable-delivery-workflows.md)
for preflight, activation, evidence capture, and compatible forward rollback.

## Sessions

The user controls every session boundary: `start`, `resume`, `end` and `abandon` are
explicit commands, never inferred by a hook, an agent or a client. Sessions capture
the durable artifacts they produced into an exclusive ledger, and closing is
fail-closed: captured knowledge or an explicit "nothing to capture" reason, never
silence.

After 24 hours without a heartbeat, an open session exposes `is_stale=true`; the marker
is derived, the persistent status stays `open`, and only the 7-day server-side sweep
ever abandons a session without an explicit user command.

The full lifecycle contract (capture rules, focus semantics, briefing) lives in
[`docs/MCP_TOOLS.md`](docs/MCP_TOOLS.md); the contract is v4 and still evolving.

## Configuration (.env)

```bash
# Required
POSTGRES_URL=postgresql+asyncpg://brain:REPLACE_WITH_PASSWORD@localhost:5433/brain

# Optional — semantic search and reranking
EMBEDDING_SERVICE_URL=http://localhost:8003
EMBEDDING_DIMENSION=1536              # <= 2000 (pgvector HNSW ceiling)
RERANKER_URL=http://localhost:8003

# Optional — point at any OpenAI-compatible endpoint instead of the bundled stack
BRAIN_EMBEDDING_BACKEND=shim          # shim (default) or openai
BRAIN_EMBEDDING_MODEL=qodo            # model name sent by the openai backend
BRAIN_EMBEDDING_QUERY_PREFIX=         # e.g. "query: " for asymmetric models
BRAIN_EMBEDDING_DOCUMENT_PREFIX=      # changing this needs a full re-embed
BRAIN_RERANK_BACKEND=shim             # shim (default) or cohere

# Optional — relationship graph (safe defaults for a fresh environment)
GRAPH_ENABLED=false
GRAPH_LEDGER_WRITE_ENABLED=false

# Tool catalog profile
BRAIN_MCP_PROFILE=compact   # compact (default) or native

# Observable delivery MCP mutations; reads and existing completion guards remain active
BRAIN_DELIVERY_ENABLED=false

LOG_LEVEL=INFO
```

Never place `MCP_HTTP_TOKEN` or `MCP_HTTP_DREAM_TOKENS` in the shared `.env`: bearer
tokens live in a private `0600` file (`~/.config/brain-v42/mcp-token.env`), and the
graph projector credential in its own (`~/.config/brain-v42/graph-projector.env`).
`BRAIN_EMBEDDING_API_KEY` and `BRAIN_RERANK_API_KEY` follow the same rule. Note that
pointing either endpoint at a hosted provider sends the text being embedded off this
machine — the rest of this deployment is loopback-bound, that step is not.
Full reference — every variable, the private secret files, preflights and rollout
gates: [`docs/OPERATIONS.md`](docs/OPERATIONS.md).

## Network trust model

The deployment targets personal agents on a trusted LAN. MCP, PostgreSQL and Neo4j
bind to loopback; metrics and automation default to loopback.

**Embedding topology**: production/default = local unified endpoint `http://localhost:8003`; `deploy/dev-pc` is a superseded rollback/reference path.

The reranker shares the unified embedding endpoint `:8003/rerank`. Treat `:8003` as
LAN-exposed until you have proved the live bind yourself, and never expose it — or the
MCP port — to the Internet. Repository code alone does not prove a live firewall state.

## Dream mode

Nightly agent pipeline (`scripts/dream.sh`: scan → clean → connect → synth → promote →
reorg) plus server-side ticket-extraction, roadmap-curation and session-sweep jobs.
Every mutating phase sits behind a killswitch and every killswitch ships closed;
dry-run is the shipped default. Each phase runs under an exact MCP tool allowlist,
and each phase holds a capability bearer scoped to its `(project, phase)` pair, so a
phase sees only the project it was started for.

Phases run on an **ordered chain of agent providers** (`BRAIN_DREAM_AGENT_PROVIDERS`),
each preflighted before the night starts. When a link is exhausted or unreachable the
run falls through to the next rather than failing the phase, and a link that dies
before any Brain call is replayable — it is retired for the night and not charged to
the retry budget, because a provider that never reached Brain performed no work to
redo. Every phase writes which link served it, and what it fell through, to
`logs/dream/<date>_<project>_<phase>.chain.json`. Read that file rather than assuming
the first link ran: a night can be entirely green on one provider and prove nothing
about the fallthrough.
Details: [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) and
[`docs/OPERATIONS.md`](docs/OPERATIONS.md).

## Production state

The repository migration target is migration 056. No page in this repository proves a
live schema head — **measure it, do not read it here**:

```bash
docker exec brain_v42_postgres psql -U brain -d brain -Atc "select version_num from alembic_version;"
```

The running build names itself: `GET /health` returns `version` (the installed
distribution) and `alembic_head` (the revision shipped with it), both measured, never
written by hand.

## Measured facts

The sentence above — *measure it, do not read it here* — is the rule. The facts
registry is the mechanism that enforces it, so the session briefing can state the
live schema head, the running release and the declared Dream killswitches without
anyone retyping them into a document.

A **fact** is a named, versioned reader. Each one declares its target, its TTL, its
timeout, its policies and the exact shape of the value it returns, and the catalogue
is closed: probes are registered once at composition and then frozen, so no runtime
caller can install a reader of its own.

What makes a reading trustworthy is not the probe but the **source identity**, and the
identity a probe is checked against is declared *independently by the operator* —
never derived from the connection the probe uses. A PostgreSQL reading must match a
cluster system identifier, database, address and port the operator wrote down; a
`live_release` reading must match the release SHA and package version the release
tooling rendered; a `host` reading must match a declared hostname. Undeclared means no
fact: the probe is refused at registration and the briefing says which one is missing
and why, rather than leaving a silent gap. This closes the obvious hole — a probe that
reports its own DSN back to you proves nothing about which database it reached.

A measurement has exactly two shapes and no third: `Measured`, carrying a bounded
canonical JSON value and a digest over it, or `Unreadable`, carrying a closed
`error_code`. A timeout, an unexpected identity or an over-large value each produce an
`Unreadable` that renders as such — never a stale value dressed up as current.

Three targets ship today (`production`, `live_release`, `host`) and the catalogue is
declared in `src/brain_v42/facts/composition.py`. Read it with `brain_fact_list` and
`brain_fact_get`; facts declared `briefing=true` also render as lines in the session
briefing. Design: [`docs/superpowers/specs/2026-09-19-measured-facts-and-claims-design.md`](docs/superpowers/specs/2026-09-19-measured-facts-and-claims-design.md).

## Development

```bash
pytest tests/unit -v                          # no PostgreSQL required
pytest --cov=brain_v42 --cov-report=term-missing
ruff check src/ tests/ && ruff format --check src/ tests/
mypy src/
```

- **Stack**: Python 3.12+, FastMCP 3.x, SQLAlchemy 2.0 async + asyncpg, Alembic,
  Pydantic 2, structlog.
- **TDD is mandatory** — red, green, refactor; tests are never edited to make code pass.
- **Coverage floor**: 60% (CI blocks below).
- **Install with `uv sync --extra dev --python 3.12`, not with pip.** `pip install -e ".[dev]"`
  fails on this layout and always has: `headless-agents` is a uv *workspace member*
  (`[tool.uv.workspace]` + `[tool.uv.sources]` in `pyproject.toml`), not a published
  distribution, so pip looks for it on PyPI and stops with `No matching distribution
  found for headless-agents`. The dev toolchain is pinned exactly in `uv.lock`, so a
  synced environment resolves to the versions CI runs.
- **Pin `--python 3.12` explicitly.** `requires-python` is `>=3.12`, so a bare
  `uv sync` on a fresh clone picks the newest interpreter it can find — measured
  3.14 — while every CI job, the release job and `[tool.mypy]` target 3.12. Matching
  CI is the whole point of the lock; an unpinned interpreter quietly gives it up.

## Project layout

```
brain-v42/
├── src/brain_v42/
│   ├── config.py              # pydantic-settings — single config surface
│   ├── db/                    # SQLAlchemy engine + tables
│   ├── models/                # Pydantic models
│   ├── repositories/          # CRUD + FTS + pgvector + graph adapters
│   ├── services/              # business logic, embedding, reranker, dream, dedup
│   ├── metrics/               # sidecar + collector + cockpit endpoint
│   ├── automation/            # independent webhook/dedup runtime (:9201)
│   └── mcp/                   # FastMCP server + brain_*/dream_* tool handlers
├── tests/{unit,integration}
├── alembic/versions/          # migrations (shipped inside the wheel)
├── scripts/                   # operational CLIs (dream.sh, canaries, repair)
├── services/                  # GPU embedding service + shim + supervisor
├── deploy/                    # systemd units, per-host compose, install.sh
└── docs/                      # ARCHITECTURE, SCHEMA, MCP_TOOLS, OPERATIONS, runbooks
```

The top-level module graph is enforced acyclic in CI
(`scripts/check_module_layering.py`): any module can still be extracted into a
standalone service without dragging a cycle with it.

## CI/CD

Stages: lint → test → security → build. Security gates: pip-audit, bandit, gitleaks,
container-image pin checks. Docker images are built and pushed on `main`; there is no
deploy stage — rollout to a host is always a manual, out-of-band step. Releases are
tag-driven: the release rail builds the wheel + sdist, proves the wheel ships its
migrations, and attaches both to the GitHub release.

## Versioning

- The shipped version is **0.6.1**, and it stays `0.x` on purpose: a `1.0.0` would promise
  a stable interface and a way back, and this project has neither yet.
- **No lossless downgrade is promised, at any version.** Several migrations protect stored
  history: **037** refuses when a session capture would be lost, **039** requires an explicit
  operator opt-in, **053** refuses once delivery workflow history exists, **054** refuses
  once a delivery attestation exists, **055** refuses once a claim, a verdict or a fact
  definition exists, and **056** refuses while a project is archived; those two accept a
  named operator opt-in.
- Follow the release's operator runbook for recovery. For **0.6.1** (as for 0.6.0 and 0.5.0), use the
  [compatible forward rollback](docs/runbooks/2026-09-07-observable-delivery-workflows.md#compatible-forward-rollback)
  and keep the repository's migration target in place: the head the release ships, never a
  lower one.
  Rollback means selecting a release that supports that head or deploying a forward fix;
  it never means `alembic downgrade`, and never means restoring an older dump over a live
  database.
- **0.6.0** ships the `headless-agents` workspace member (`packages/headless-agents/`,
  version `0.1.0`) as a second distribution that `brain_v42` depends on; its own version
  moves independently of this one.

## License

Source code: [Apache-2.0](LICENSE).

**Model weights are not covered by that license**, and this is not a formality. The
production embedding model, `Qodo/Qodo-Embed-1-1.5B`, is published under
QodoAI-Open-RAIL-M — a license carrying use-based restrictions, not a permissive one.
No weights are stored in or distributed by this repository: every model is downloaded
from its upstream host at build time, by the operator, who accepts each model's terms
directly from its publisher. See [NOTICE](NOTICE) before redistributing anything.
