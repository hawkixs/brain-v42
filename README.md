# brain-v42

Persistent, typed project memory exposed through the Model Context Protocol (MCP). It stores decisions, learnings, snippets, runbooks, architecture decision records (ADRs), project focus, tickets, claims, and delivery evidence. PostgreSQL is the source of truth; PostgreSQL full-text search and pgvector provide retrieval, with optional reranking. Production uses hosted Mistral embeddings and hosted Voyage reranking with an OpenRouter Cohere fallback. Neo4j is an optional relationship projection.

brain-v42 is a Python 3.12 application built with FastMCP 3.x, SQLAlchemy's async engine, Pydantic, and Alembic. Release 0.7.3 ships no migration and keeps recovery contract v26: session relay and start answers return the project focus once instead of twice, with an optional compact briefing, on top of 0.7.2 (an explicit, tested bound on held MCP sessions (`MCP_HTTP_MAX_SESSIONS`, default 6000) that evicts the least recently active idle session at the cap instead of refusing every new one, the restore drill's asyncpg database URL scheme, and 4g/2g memory limits for the red-base MCP and PostgreSQL containers), 0.7.1 (migration 068 with recovery contract v26: merge chains are refused at commit by a deferred constraint trigger on the six tables carrying `merged_into`, the migrate preflight refuses the upgrade while any chain remains, and the chain repair also flattens cross-project chains under a separate expected count; it also ships the knowledge lifecycle run orchestration, its command line and real adapters, and a bounded wait for a lifecycle batch, still dormant (nothing schedules them), and mcp 1.30.0 (CVE-2026-59951)), 0.7.0 (migrations 066 and 067, recovery contract v25: the knowledge lifecycle tables and their dormant repository, pure decision, facts registry and batch extraction client (nothing runs them yet), exact project scopes with an own-focus grant for scoped writers, the relay shrink override requiring an elevation, feature chains covered by the historical chain repair, lifecycle refusals that name the corrective call, embedding transport errors degraded instead of raised, one shared stderr logging setup for the MCP server and the gateway, red-cockpit allowed on the metrics read routes through `METRICS_READ_CLIENTS`, and sharded CI test suites), 0.6.18 (search and consolidation hardening, MCP ergonomics, operability fixes, an hourly embedding backlog runner shipped dry and authenticated metrics reads for peers, with no migration and recovery contract v23), 0.6.17 (metrics M1 and MCP ergonomics), 0.6.16 (security and integrity follow-ups of lot 0.6.11), 0.6.15 (rerank budget), 0.6.14 (direct Voyage rerank) and 0.6.13 (programmes/v1, migration 065, recovery contract v23), and migrations 063 to 068. See [Architecture](docs/ARCHITECTURE.md), [MCP tool reference](docs/MCP_TOOLS.md), and [Operations](docs/OPERATIONS.md) for detailed contracts and procedures.

## Architecture

```text
MCP clients ── authenticated HTTP BIND_ADDRESS:8765/mcp (production)
               or loopback HTTP /mcp (local) or stdio (dev/fallback)
                    │
              brain-v42 / FastMCP
               ├── PostgreSQL 16 + pgvector (source of truth)
               ├── hosted Mistral embeddings (OpenAI-compatible API)
               ├── hosted Voyage rerank → OpenRouter Cohere fallback
               └── Neo4j 5 (optional relationship projection)
```

**MCP transport**: production (red-base) = HTTP on the host's WireGuard address (`BIND_ADDRESS`, port 8765, path `/mcp`) in credentials mode, every call authenticated; local HTTP (workstation/dev) = loopback `http://127.0.0.1:8765/mcp` (the configuration default bind); configuration default and dev/fallback transport = `stdio`.

PostgreSQL is the single source of truth. Neo4j is a disposable projection fed by a
relational ledger/outbox — it can always be rebuilt from PostgreSQL, never the other
way around. The canonical path is active in production since 22 July 2026; design and
evidence live in [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) and the
[graph ledger runbook](docs/GRAPH_LEDGER_RUNBOOK.md).

Embeddings are optional. When the configured service is unavailable, search falls
back to full-text retrieval and writes can be stored without an embedding for later
backfill. Production configures hosted Mistral `codestral-embed-2505` embeddings
and hosted reranking through `deploy/compose.yaml`. Hosted providers receive the
text sent for embedding or reranking. The `shim` code defaults support an optional,
local endpoint; the development `docker-compose.yml` can start one (`embedding-shim`)
for local use only. The `openai` backend supports OpenAI-compatible embeddings;
`voyage` uses Voyage directly and `cohere` supports Cohere-style APIs such as
OpenRouter, TEI, Jina and vLLM. See the production contracts below.

`EMBEDDING_DIMENSION` defaults to 1536 and must be at most 2000 for pgvector's HNSW
index. The ORM supports another dimension, but migrations 002, 005, 009, and 014
create 1536-dimensional columns. A non-default dimension therefore needs a deliberate
schema adjustment and index rebuild after migration. Changing the document prefix on
an existing corpus requires regenerating embeddings with
`scripts/regen_embeddings.py`.

## Quick start

```bash
git clone https://github.com/hawkixs/brain-v42
cd brain-v42
uv sync --extra dev --python 3.12
source .venv/bin/activate
cp .env.example .env
```

Set `POSTGRES_URL` in `.env` for your PostgreSQL instance. For the repository's local
Compose stack, set its PostgreSQL password consistently in `.env`, prepare the
Compose-required Neo4j and embedding bearer secret files, and create the external
`brain-net` network. Start the database services with `docker compose up -d postgres
neo4j`; the optional embedding services require their model and GPU setup. See
`docker-compose.yml` and [Operations](docs/OPERATIONS.md) for the complete local
service setup and secret handling.

For a local Compose setup, create its external network and secret files:

```bash
docker network create brain-net
install -d -m 0700 .secrets
read -rsp "Neo4j password: " PW
(umask 0022; printf 'neo4j/%s\n' "$PW" > .secrets/neo4j-auth)
unset PW
(umask 0177; openssl rand -hex 32 > .secrets/embedding-shim-bearer)
```

Run migrations and start the stdio server:

```bash
export POSTGRES_URL="$(grep -E '^POSTGRES_URL=' .env | cut -d= -f2-)"
BRAIN_ALEMBIC_ALLOW_PROD=1 alembic upgrade head
python -m brain_v42.mcp.server
```

The opt-in is required only when the database name is exactly `brain`; never export it persistently.

Alembic reads its URL from the environment, not directly from `.env`, and rejects DSN
query parameters: use the plain `postgresql+asyncpg://user:password@host:port/db` form. Local HTTP in the
default `shared_token` mode requires bearer authentication; an empty token is refused
unless the explicit development-only unauthenticated option is enabled. Production
(red-base) uses credentials mode instead, where `MCP_HTTP_TOKEN` must be absent. For local stdio MCP configuration:

```bash
claude mcp add brain-v42 -- python -m brain_v42.mcp.server
```

## MCP tools

The `compact` profile is the default. It keeps the nine session lifecycle tools visible;
other tools are discovered through the catalog search gateway and invoked through the
catalog call gateway.
The `native` profile exposes the registered tools directly. Inputs are bounded and
HTTP request bodies have a configured size cap. Graph traversal tools require graph
support to be enabled.

| Domain | Tools |
|--------|-------|
| Search and generic CRUD | `brain_search`, `brain_get`, `brain_list`, `brain_update`, `brain_delete` |
| Graph traversal | `brain_get_neighbors`, `brain_graph_path` |
| Session lifecycle | `brain_session_start`, `brain_session_capture`, `brain_session_heartbeat`, `brain_session_checkpoint`, `brain_session_end`, `brain_session_relay`, `brain_session_list`, `brain_session_resume`, `brain_session_bind`, `brain_session_abandon` |
| Focus slots | `brain_slot_open`, `brain_slot_list`, `brain_slot_close` |
| Programme progress | `brain_programme_upsert`, `brain_programme_gate_upsert`, `brain_programme_gate_report`, `brain_programme_request`, `brain_programme_list` |
| Project context | `brain_focus_history`, `brain_project_focus_get`, `brain_set_project_context`, `brain_project_archive`, `brain_project_unarchive`, `brain_list_projects`, `brain_update_project_focus`, `brain_list_project_groups` |
| Decisions | `brain_log_decision`, `brain_supersede_decision`, `brain_get_supersession_chain` |
| Learnings | `brain_learn`, `brain_validate_learning` |
| Snippets | `brain_save_snippet`, `brain_use_snippet` |
| Runbooks | `brain_create_runbook`, `brain_promote_runbook`, `brain_get_runbook`, `brain_execute_runbook` |
| ADRs | `brain_propose_adr`, `brain_promote_adr`, `brain_accept_adr`, `brain_deprecate_adr` |
| Tickets and planning | `brain_ticket_create`, `brain_ticket_reply`, `brain_ticket_transition`, `brain_ticket_plan`, `brain_ticket_list`, `brain_ticket_get`, `brain_reindex_plans` |
| Observable delivery | `brain_delivery_contract_set`, `brain_delivery_bind_pr`, `brain_delivery_get`, `brain_delivery_list`, `brain_delivery_refresh`, `brain_delivery_claim`, `brain_delivery_claim_renew`, `brain_delivery_claim_release`, `brain_delivery_accept`, `brain_delivery_attest`, `brain_delivery_attestation_list` |
| Dream and graph maintenance | `brain_backfill_links_batch`, `brain_get_clusters`, `brain_list_orphans_for_classification`, `brain_assign_domain`, `brain_list_curation_proposals`, `brain_reject_curation_proposals`, `brain_apply_curation_proposal`, `brain_decay_status`, `brain_refresh_entity`, `brain_consolidation_candidates`, `brain_merge_entities` |
| Roadmap | `brain_feature_create`, `brain_get_roadmap`, `brain_feature_update` |
| Measured facts | `brain_fact_list`, `brain_fact_get` |
| Claims | `brain_claim_verify`, `brain_claim_list`, `brain_claim_history` |
| Workflow guidance | `brain_workflow_guide` |

Full catalog, signatures, authorization behavior, and limits:
[`docs/MCP_TOOLS.md`](docs/MCP_TOOLS.md). The two catalog gateways of the `compact` profile,
`brain_find_tool` and `brain_call_tool`, are provided by the profile rather than registered
as tools, so they are not counted above.

## Observable delivery

Delivery records connect a ticket contract, pull request binding, persisted CI and
integration observations, and requester acceptance. The observer is a separate
process. Reads use persisted observations; they do not call GitHub. Delivery mutations
can be disabled while reads and completion checks for existing contracts remain
available. Tickets can carry a planned `target_release`.

### Ledger and policy: the boundary with red-rail

Brain is the **ledger**; [red-rail](https://github.com/hawkixs) is **policy**. Brain
stores attestation facts and validates their shape: the kind syntax, bounded JSON
payload, and server-computed digest. It does not judge what a kind means or derive a
completion refusal from an attestation. Attestations are append-only. red-rail reads
the rows to calculate delivery policy and DORA metrics. The API contract is published
as [`docs/contracts/delivery_attestations.json`](docs/contracts/delivery_attestations.json)
for consumers that must not import `brain_v42`.

## Sessions

The user controls every session boundary: `start`, `resume`, `end` and `abandon` are
explicit commands, never inferred by a hook, an agent or a client. Sessions capture
the durable artifacts they produced into an exclusive ledger, and closing is
fail-closed: captured knowledge or an explicit "nothing to capture" reason, never
silence.

HTTP connections also have server-managed agent trace sessions. See
[`docs/OPERATIONS.md`](docs/OPERATIONS.md), "Agent traces and the inactivity sweep",
for their lifecycle. Derived capture and exact absorption are limited to the
connections on which an operator session was observed. Operator sessions can bind to
focus slots and relay their focus. Unbound session end and project-focus updates reject
a shrink below 70 percent unless the operator explicitly overrides the guard.

After 24 hours without a heartbeat, an open session exposes `is_stale=true`; the marker
is derived, the persistent status stays `open`, and only the 7-day server-side sweep
ever abandons a session without an explicit user command.

See [`docs/MCP_TOOLS.md`](docs/MCP_TOOLS.md) for the full lifecycle and slot contracts.

## Configuration (.env)

The settings surface is defined in `src/brain_v42/config.py`. Common settings:

```bash
# Required
POSTGRES_URL=postgresql+asyncpg://brain:change-me-locally@localhost:5433/brain

# Optional local search endpoint, supplied separately (not production)
EMBEDDING_SERVICE_URL=http://localhost:8003
EMBEDDING_DIMENSION=1536
BRAIN_EMBEDDING_BACKEND=shim       # shim or openai
BRAIN_EMBEDDING_MODEL=qodo
BRAIN_EMBEDDING_QUERY_PREFIX=
BRAIN_EMBEDDING_DOCUMENT_PREFIX=
RERANKER_URL=http://localhost:8003
BRAIN_RERANK_BACKEND=shim          # shim, cohere, voyage or none

# Optional graph features
GRAPH_ENABLED=false
GRAPH_LEDGER_WRITE_ENABLED=false

# MCP behavior
BRAIN_MCP_PROFILE=compact          # compact or native
BRAIN_MCP_TRANSPORT=stdio          # stdio or http

# Optional services
METRICS_ENABLED=false
BRAIN_DELIVERY_ENABLED=false
LOG_LEVEL=INFO
```

For a no-GPU setup, point the OpenAI-compatible backend at a local endpoint such as Ollama:
set `BRAIN_EMBEDDING_BACKEND=openai`, `EMBEDDING_SERVICE_URL` to its URL,
`BRAIN_EMBEDDING_MODEL` to a model it serves, and `EMBEDDING_DIMENSION=768`.

Local HTTP binds to loopback by default; the default `shared_token` auth mode requires
a non-empty `MCP_HTTP_TOKEN`. Production (red-base) uses
`BRAIN_MCP_AUTH_MODE=credentials`: every MCP call needs a valid client credential,
and `MCP_HTTP_TOKEN` must be absent.
Never place `MCP_HTTP_TOKEN` or `MCP_HTTP_DREAM_TOKENS` in the shared `.env`; keep
them in private, permission-restricted secret files such as `mcp-token.env`.
`GRAPH_PROJECTOR_*`, provider API
keys, and delivery observer credentials also require private configuration. Set
`EMBEDDING_DIMENSION` unprefixed: the ORM reads it from the environment directly.
See
[`docs/OPERATIONS.md`](docs/OPERATIONS.md) for the complete variable and secret
reference.

## Network trust model

The deployment targets personal agents on a trusted LAN. Production (red-base)
publishes MCP on `BIND_ADDRESS:8765` in credentials mode and metrics on
`BIND_ADDRESS:9200`; PostgreSQL and Neo4j publish no host port. Local HTTP and
development database publishes default to loopback, as do metrics and automation.

**Embedding topology**: production uses hosted embeddings via `deploy/compose.yaml` (`x-embedding-env`: `EMBEDDING_BACKEND=openai`, Mistral `codestral-embed-2505`, 1536 dimensions). The local endpoint `http://localhost:8003` is optional for development only; the former workstation deployment is retired.

**Reranker**: production uses hosted Voyage `rerank-3-lite` with OpenRouter `cohere/rerank-4-fast` fallback via `deploy/compose.yaml` (`x-rerank-env`). Rollback replaces that block with `BRAIN_RERANK_BACKEND="none"` alone.

Production calls hosted HTTPS APIs with separate provider credentials. If you run a
local endpoint on `:8003`, verify its bind and authentication before using it. Never
expose a local shim or the MCP port to the Internet. Repository configuration alone
does not prove the live firewall state.

## Dream mode

Nightly agent pipeline (`scripts/dream.sh`: scan → clean → connect → synth → promote →
reorg) plus server-side ticket-extraction, roadmap-curation and session-sweep jobs. Mutating phases are guarded
by per-phase settings and capability scopes. They ship disabled and dry by default;
operators can arm or suspend them. Provider chains can be configured, and phase
execution records which provider served it. See [Architecture](docs/ARCHITECTURE.md)
and [Operations](docs/OPERATIONS.md). Each phase writes
`logs/dream/<date>_<project>_<phase>.chain.json`; read it, because a green night on
the first provider proves nothing about fallthrough.

## Production state

The repository migration target is migration 068 (merge-chain guards; recovery contract v26 candidate). No page in this repository proves a
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
`error_code`. A timeout, an unexpected identity or an over-large value each produce
an `Unreadable` that renders as such — never a stale value dressed up as current.

Three targets ship today (`production`, `live_release`, `host`) and the catalogue is
declared in `src/brain_v42/facts/composition.py`. Read it with `brain_fact_list` and
`brain_fact_get`; facts declared `briefing=true` also render as lines in the session
briefing.

## Development

```bash
pytest tests/unit -v
pytest --cov=brain_v42 --cov-report=term-missing
ruff check src/ tests/
ruff format --check src/ tests/
mypy src/
```

The project requires Python 3.12.7 or newer; CI targets Python 3.12. Install with
`uv sync --extra dev --python 3.12` so the locked Git
dependency and interpreter match CI. Unit tests do not require PostgreSQL. Tests that
need a database require an isolated test database through `BRAIN_V42_TEST_DB_URL` and
refuse production database targets.
A session also refuses to start when the filesystem holding its temporary files
(`--basetemp`, else `PYTEST_DEBUG_TEMPROOT`, else the system temp directory) has less free space than 5 % of its size,
at least 5 GiB: a disk filled by old test directories once took a PostgreSQL on the same
disk down. `BRAIN_V42_TEST_MIN_FREE_GB` sets the floor explicitly; `0` disables it.

## Project layout

```text
brain-v42/
├── src/brain_v42/
│   ├── config.py              # settings
│   ├── db/                    # SQLAlchemy engine and tables
│   ├── models/                # Pydantic models and contracts
│   ├── repositories/          # PostgreSQL, search, and graph adapters
│   ├── services/              # business logic, delivery, search, and Dream
│   ├── facts/                 # measured-fact registry
│   ├── metrics/               # sidecar and collectors
│   ├── automation/            # webhook and dedup runtime
│   └── mcp/                   # FastMCP server and tools
├── tests/{unit,integration}
├── alembic/versions/          # migrations shipped in the wheel
├── scripts/                   # operational commands
├── services/                  # embedding services and supervisor
├── deploy/                    # service definitions and installers
└── docs/                      # architecture, schema, tools, and operations
```

The high-level module graph is acyclic and checked in CI by
`scripts/check_module_layering.py`. The check is static; runtime imports are not
visible to it.

## CI/CD

GitHub Actions is the CI/CD authority. Pull requests run lint, tests, security checks,
and a Docker image build and smoke check on hosted runners. Pushes to `main` build and
publish the container image; this rail does not deploy it. Version tags build and
publish the wheel and source distribution after verifying that the wheel contains
Alembic migrations and that the tag matches the package version. No rail deploys a
release automatically.

## Roadmap

Production runs on red-base through `deploy/compose.yaml`, deployed by the red-rail
release rail as one Compose project: PostgreSQL, Neo4j, the MCP server,
metrics sidecar, delivery observer, a one-shot migration service that proves the
recovery contract, and Compose-scheduled work. Clients reach MCP over a private
WireGuard network in credentials mode; `MCP_HTTP_TOKEN` must be absent. Only MCP
(`BIND_ADDRESS:8765`) and metrics (`BIND_ADDRESS:9200`) publish host ports, and
unauthenticated `POST /mcp` returns `401`. The former workstation systemd deployment
is retired. Each client service uses a scoped credential (read, write, delivery, or
admin), with admin access granted only through time-boxed operator elevation.
Delivery attestation issuers are planned to be proven against their credential in
attestation API v1.1. Priorities are search and embedding quality, disaster-recovery
proof, and metrics. Operators can suspend Dream and re-arm it later.

## Versioning

- The shipped version is **0.7.3**, and it stays `0.x` on purpose: a `1.0.0` would promise
  a stable interface and a way back, and this project has neither yet.
- **No lossless downgrade is promised, at any version.** Several migrations protect stored
  history: **037** refuses when a session capture would be lost, **039** requires an explicit
  operator opt-in, **053** refuses once delivery workflow history exists, **054** refuses
  once a delivery attestation exists, **055** refuses once a claim, a verdict or a fact
  definition exists, **056** refuses while a project is archived (those two accept a
  named operator opt-in), **058** refuses once a server-extracted claim exists, **059**
  refuses while a ticket carries a target_release, and **060** refuses while a focus slot,
  a slot history row or a slot-bound session exists (both accept a named operator opt-in).
- Follow the release's operator runbook for recovery. Keep the repository's migration
  target in place: the head the release ships, never a lower one. Rollback means
  selecting a release that supports that head or deploying a forward fix; it never
  means `alembic downgrade`, and never means restoring an older dump over a live database.
- **0.6.0** introduced the `headless-agents` dependency. It is maintained in
  `hawkixs/red-ha` and pinned to tag `v0.5.4` in `pyproject.toml`.

## License

Source code: [Apache-2.0](LICENSE).

**Model weights are not covered by that license.** No weights are stored in or
distributed by this repository; operators download models from their publishers and
accept the applicable terms. See [NOTICE](NOTICE) before redistributing anything.
