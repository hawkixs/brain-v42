# Measured facts and claims

**Date:** 2026-09-19

**Status:** proposed specification for ADR #27 (`fb9b75bd`, accepted 2026-09-19); lot A is
specified to implementation depth, lot B to data-model depth; nothing here is implemented

**Project:** `brain-v42`

**Source baseline:** `df64e40cbf10c9b2af5f61ad0e2e60c5af5443cc` (main, after PR #152)

**Carrier ticket:** `59a5a9c7` (lots A, B, C, D)

**Delivery target:** lot A — a closed registry of measured facts rendered live in the session
briefing and readable by name through MCP, with `graph_projection_lag` as its first fact,
running on the production MCP service without a schema change. Lot B — the data model of
structured claims and append-only verdicts, ready for a migration numbered 055 or later.

## 1. Purpose

A Brain entry is prose. Prose mixes two natures that age differently: durable reasoning,
which stays right for years, and measured facts, which expire the day the world moves. The
decay mechanism retires what is no longer *read*; it knows nothing about what is no longer
*true*. Three measured failures motivate this specification:

- `CLAUDE.md` asserted an Alembic head that production had left, three times, for two days
  each; the session briefing was given a measured `Schéma :` line on 2026-08-04 precisely so
  that a reader sees the contradiction (`_section_technical_state`,
  `src/brain_v42/mcp/tools/session_tools.py:412`).
- The Neo4j projection froze on 2026-09-07 at 14:00 CEST (ticket `416266ec`) and stayed frozen
  for days. The metrics sidecar computed `oldest_pending_age_seconds` and a `projector.healthy`
  flag the whole time (`src/brain_v42/metrics/collector.py:587`), and nobody read them into a
  briefing. The fact existed; it had no name and no reader.
- Learning `5abe9cda` named its own falsification condition, the condition came true the same
  day, and the false statement was repeated the next morning. Archiving the whole entry threw
  away reasoning that was still right.

ADR #27 fixes the model: an entry is durable reasoning plus zero or more measurable **claims**,
each tied to a named **fact** with one read-only, bounded **probe** and a named **target**;
verdicts live at the granularity of the claim, never of the entry; prose references the name of
a fact and never copies its value.

This specification says what to build for lot A, what the tables of lot B are, and what neither
lot does.

## 2. Ownership and sources of authority

| Concern | Authority |
| --- | --- |
| The catalogue of facts (names, targets, probes, bounds) | Code, registered at the composition root; closed at runtime |
| The value of a fact | The named target, read by its probe at measurement time; never prose |
| What a claim asserts and what it expects | The author of the entry (agent or human), stored with the entry |
| A verdict on a claim | A measurement compared with the expectation, mechanically; robots write verdicts, never archive |
| Whether an entry is archived | A REORG proposal informed by verdicts, executed by a human or by the archive-only guard already in production; never by a verdict |
| Rendering in the briefing, search, and rendered documents | Brain, at read time, from the live value |

Two words carry the whole doctrine and are used consistently below: **measured** (a value a
probe read from its target, with the instant of reading) and **declared** (a value somebody
wrote). A briefing line is measured. A claim's `expected` is declared. A verdict is measured.

## 3. Scope and non-goals

In scope:

- **Lot A** — package `brain_v42.facts`: `Measurement`, `Probe`, `FactRegistry`, the first
  probe `graph_projection_lag`, its rendering in the briefing's `### État technique (mesuré)`
  section, and two read-only MCP tools, `brain_fact_list` and `brain_fact_get`. No schema.
- **Lot B (data model only)** — tables `knowledge_claims` and `knowledge_claim_verdicts`, their
  constraints, the verdict computation, and the write and read paths that lot B's
  implementation will carry. The migration itself, its tests and its apply window are lot B's
  own delivery.

Out of scope, and stated so the reviewer does not look for them:

- Lot C (the nightly VERIFY phase) and lot D (the Neo4j projection of claims and facts). Both
  consume this design; both are specified when their turn comes. Constraints they impose are
  named in §5.9 and §6.7.
- Catch-up of the existing stock (thousands of entries without claims). The past stays readable
  as dated opinion; the frontier is dated.
- Any automatic archiving, deletion, or freshness change driven by a verdict.
- Any LLM in the measurement path. Probes are mechanical. Prose-judged claims are proposals of
  lot C, never verdicts.
- Persisting facts in lot A. A verdict row (lot B) is the persistence of a measurement.

## 4. Existing integration points

Every one of these is reused or extended; none is duplicated.

| Existing piece | Location | Role in this design |
| --- | --- | --- |
| Measured schema revision in the briefing | `SchemaStateService.current_revision`, `src/brain_v42/services/schema_state_service.py` | Becomes the probe of fact `alembic_head` (lot A, slice 2); the rendered line does not change |
| Killswitch state | `DreamRunService.killswitch_state`, `src/brain_v42/services/dream_run_service.py:100` | Becomes the probe of fact `dream_killswitches` (slice 2) |
| Graph outbox and projector statistics | `MetricsCollector`, `src/brain_v42/metrics/collector.py:587-741` | Its SQL is the probe of `graph_projection_lag`; extracted into the repository layer and shared (§5.5) |
| Projection inventory | `PgGraphLedger.projection_inventory`, `src/brain_v42/repositories/pg_graph_ledger.py:764` | Same table, recovery-oriented; the shared query lands next to it |
| Shipped Alembic head and package version | `brain_v42.release` (`shipped_alembic_head`, `package_version`) | Probes of `alembic_head_shipped` and `live_release_sha` (slice 2) |
| Model liveness | `scripts/probe_model_liveness.py` (verdicts ALIVE / GONE / OTHER) | Probe of `model_liveness`, nightly only (lot C) |
| Briefing technical section | `_section_technical_state`, `session_tools.py:412`; fixture `tests/fixtures/briefing_full.md` | Gains one line per briefing fact; the fixture is unchanged when no registry is wired |
| Append-only evidence with idempotency | `delivery_attestations`, `src/brain_v42/db/delivery_tables.py:472` | The template of `knowledge_claim_verdicts` (§6.2) |
| Capability scope per Dream phase | `src/brain_v42/mcp/dream_capabilities.py`, `test_dream_prompts_match_phase_allowlists.py` | New tools are absent from every phase allowlist until lot C names them |
| Module layering DAG | `scripts/check_module_layering.py`, `tests/unit/test_module_layering.py` | `facts` must be a new node importing only downward (§5.10) |
| Graceful degradation of the briefing | `load_briefing`, `session_tools.py:541` | Facts follow the technical-section rule: a failure renders, it never vanishes |

## 5. Lot A — the facts registry

### 5.1 Vocabulary

- **Fact**: a named, measurable property of one target. Name grammar `^[a-z][a-z0-9_]{0,63}$`,
  flat snake_case, unique in the catalogue. The name is the stable handle prose refers to.
- **Target**: where the probe reads. Closed enumeration `FactTarget`:
  `production` (the production PostgreSQL), `brain_test` (the test database), `live_release`
  (the running process and its installed package), `repository` (the checked-out source tree),
  `host` (files and units on the host), `github`, `provider` (a model or embedding endpoint).
  A probe names exactly one target. The 2026-09-12 incident that motivates this field: a probe
  pointed at `brain_test` (then at 052) would have falsified a true statement about production.
- **Probe**: the single read-only, bounded reader of a fact.
- **Measurement**: what a probe returned, with when, how long, and from where.
- **Unreadable**: a measurement that did not happen (timeout, refused connection, missing
  table, malformed answer). Unreadable is a status, never a value; it is never rendered as a
  number and, in lot B, never becomes *falsified*.

### 5.2 Measurement

```python
@dataclass(frozen=True, slots=True)
class Measurement:
    fact: str                       # catalogue name
    target: FactTarget
    status: Literal["measured", "unreadable"]
    value: Mapping[str, JSON] | None   # canonical JSON object, <= 4 KiB; None when unreadable
    digest: str | None              # sha256 of the canonical JSON of `value`; None when unreadable
    measured_at: datetime           # UTC, the instant the probe returned (or failed)
    duration_ms: int
    error_code: str | None          # unreadable only: exception class name or a probe-declared code
    source: Literal["probe", "cache"]
    ttl_seconds: int                # the probe's TTL, so a reader can judge the age
```

`value` is data, never text for humans: rendering belongs to the reader (the briefing renders
French, the tool renders JSON, a document renderer will render its own prose). Canonical JSON
means sorted keys, no whitespace, UTF-8 — the same convention `delivery_attestations` uses for
its digest, so a verdict payload of lot B can carry the digest unchanged.

### 5.3 Probe contract

```python
class Probe(Protocol):
    name: str
    target: FactTarget
    ttl: timedelta        # cache validity of a measured value
    timeout: timedelta    # hard bound the registry applies with asyncio.wait_for
    briefing: bool        # rendered in the session briefing (cheap probes only)
    async def measure(self) -> Mapping[str, JSON]: ...
```

Rules, all of them tested:

1. **Read-only.** A probe on `production` or `brain_test` issues `SELECT` only, inside a
   session the registry opens with no transaction of its own to commit. A probe on `host`
   opens files read-only. A probe on `github` or `provider` sends `GET` (or the provider's
   documented read verb) only. A probe never calls a tool, a model, or another probe.
2. **Bounded.** The registry applies `timeout`; a probe that exceeds it is cancelled and the
   measurement is `unreadable` with `error_code="TimeoutError"`. A probe bounds its own rows
   (`LIMIT`, `per_page`) and its own response size. Defaults: 3 s on a database, 1 s on the
   host, 10 s on the network; `briefing=True` probes may not declare more than 3 s.
3. **One probe per fact.** Registering a second probe under a name already registered raises
   `DuplicateFactError`. The metrics collector, the briefing and lot B read the same query.
4. **Raising is the failure protocol.** A probe that cannot measure raises; it never returns a
   sentinel value. The registry turns the exception into an `unreadable` measurement carrying
   the exception's class name, and logs it at warning level with the fact name — once per
   failure, not once per reader.
5. **No side effect on the target**, including no `access_log` row, no metrics counter, no
   heartbeat: a fact that is read a thousand times must look exactly like a fact read once.

### 5.4 Registry

```python
class FactRegistry:
    def __init__(self, *, clock: Callable[[], datetime] = _utcnow, concurrency: int = 4) -> None: ...
    def register(self, probe: Probe) -> None            # DuplicateFactError, InvalidFactNameError, RegistryFrozenError
    def freeze(self) -> None                             # closes the catalogue; called once at the end of composition
    def names(self) -> tuple[str, ...]
    def describe(self, name: str) -> FactDescriptor      # name, target, ttl, timeout, briefing; UnknownFactError
    async def measure(self, name: str, *, max_age: timedelta | None = None) -> Measurement
    async def measure_many(self, names: Iterable[str], *, max_age: timedelta | None = None) -> dict[str, Measurement]
    def briefing_names(self) -> tuple[str, ...]          # registration order, briefing=True only
```

Semantics:

- **Closed catalogue.** `register` is legal only before `freeze()`. The composition root
  (`src/brain_v42/mcp/server.py`) registers every probe, freezes, and hands the registry to the
  tools. Nothing at runtime — no tool, no configuration key, no phase — adds a fact. This is
  what makes a fact name a stable handle and the registry a non-surface for injection.
- **Cache with TTL.** A measured value is served from cache while
  `now - measured_at <= min(ttl, max_age)`; a cached answer carries `source="cache"` and its
  original `measured_at`, so the reader can render the age. The cache is per process and lives
  as long as the process: two MCP transports (HTTP in production, stdio in development) are two
  caches, and that is acceptable because a value is at most `ttl` old in each.
- **Unreadable is cached briefly.** An `unreadable` measurement is served from cache for
  `min(ttl, 30 s)`. Without this, a briefing that asks for eight facts while PostgreSQL is
  down would pay eight timeouts; with it, the second reader inside thirty seconds pays nothing
  and still sees `unreadable`. The reviewer is asked whether thirty seconds is right (§9).
- **Single flight.** Concurrent readers of the same stale fact share one in-flight measurement;
  the registry keeps one `asyncio.Task` per name while it runs. A stampede on session start
  (several agents opening at once) costs one probe run.
- **Bounded concurrency.** `measure_many` runs at most `concurrency` probes at once
  (`asyncio.Semaphore`), so a briefing never opens more than four database sessions for facts.
- **Unknown names fail closed.** `measure("nope")` raises `UnknownFactError`; the MCP tool maps
  it to the business error `unknown_fact` with the catalogue's names in the message.
- **Clock injection.** Tests drive the TTL with a fake clock; no test sleeps.

### 5.5 The first fact: `graph_projection_lag`

Target `production`. TTL 15 s. Timeout 3 s. `briefing=True`.

The probe runs the query the metrics collector runs today, moved out of the collector into
`brain_v42.repositories.pg_graph_ledger.read_projection_state(session) -> ProjectionState`
so that the collector, the probe and (later) the recovery tool read the same SQL. The
collector is rewired onto the shared function in a second commit of the same pull request,
after `impact()` on `collect_db_stats` has been read and reported; its `/metrics` output does
not change shape.

Value (all keys always present):

```json
{
  "pending": 0,
  "ready": 0,
  "claimed": 0,
  "exhausted": 0,
  "lag_seconds": 0.0,
  "generation": 93,
  "armed": true,
  "lease_active": true,
  "recovery_active": false,
  "healthy": true
}
```

- `lag_seconds` is `oldest_pending_age_seconds` of the collector: the age of the oldest
  undelivered, non-exhausted outbox row, `0.0` when nothing is pending. It is the number the
  name promises.
- `healthy` is the collector's predicate, unchanged: `armed and lease_active and not
  recovery_active`. It is computed in SQL and returned as a value so the briefing, the tool and
  a lot B claim (`{"path": "/healthy", "op": "eq", "value": true}`) read the same verdict.
- `generation` is `null` when the lease row is absent (a fresh database), never `-1`: the
  collector's `-1` sentinel stays in the collector's own output for compatibility.

What the 2026-09-07 freeze would have produced under this probe, from the ticket's numbers:
`pending=114`, `lag_seconds` growing from 0 to 28 800 within the first eight hours,
`armed=false`, `healthy=false` — a loud line in every briefing from the first session after
14:00 CEST.

### 5.6 Rendering in the briefing

`_section_technical_state` gains a `facts: Sequence[Measurement]` argument (default empty, so
the legacy call shape renders exactly the pinned fixture). Each briefing fact renders one line
after `Schéma`, in the registration order, in French because the section is rendered output:

| Case | Line |
| --- | --- |
| Measured, healthy, `lag_seconds <= 300` | `- Projection graphe : à jour — 0 en attente, génération 93 armée, bail tenu` |
| Measured, `not healthy` or `lag_seconds > 300` | `- Projection graphe : EN RETARD de 4j 2h — 114 en attente, génération 70 NON armée, bail tenu` |
| Measured, exhausted rows | the loud form plus `, 3 épuisées` |
| Unreadable | `- Projection graphe : illisible (TimeoutError)` |
| Served from cache older than 60 s | the line plus ` (mesuré il y a 3 min)` |

The 300 s threshold is the renderer's, not the probe's: the projector polls every five seconds,
a row pending five minutes is abnormal, and the number is a rendering choice a future fact may
not share. The loud form carries the numbers a reader needs to open the right runbook
(`docs/GRAPH_LEDGER_RUNBOOK.md`, section "Incident and projection recovery 035") without a
second call.

Unreadable renders; it never vanishes. This is the technical section's existing rule
(`schema_unavailable`), and it is the whole point: silence would send the reader back to the
prose the section exists to contradict.

Rendering is a pure function of the `Measurement` (`render_fact_line(measurement, now)`),
tested without a database. A fact the renderer does not know how to render (a future fact
without a dedicated line) renders generically as `- <name> : <canonical JSON, cut at 120
characters>` — visible, ugly, and therefore fixed quickly.

### 5.7 MCP tools

Two read-only tools, registered in the compact and native profiles, absent from every Dream
phase allowlist until lot C names them, and never counted as an access on any entity.

- `brain_fact_list()` → `[{name, target, ttl_seconds, timeout_seconds, briefing, last: {status,
  measured_at, source} | null}]`, in registration order. It reads the cache; it measures nothing.
- `brain_fact_get(name: str, max_age_seconds: int | None = None)` → the `Measurement` as JSON.
  `max_age_seconds` lets a caller demand a fresher value than the TTL; it cannot bypass a
  probe's timeout or force a probe outside its target. Errors: `unknown_fact` (business error,
  names the catalogue); an unreadable measurement is a normal result with `status="unreadable"`,
  not an error — the caller asked what the fact is, and "not readable right now" is the answer.

These two tools are what lets prose say `see fact graph_projection_lag` and a reader resolve
the name to a live value. They are also the read side lot C's VERIFY phase will use for the
cheap facts, which is why they exist before lot C does.

### 5.8 Bounds and failure modes

| Situation | Behaviour |
| --- | --- |
| PostgreSQL unreachable | `graph_projection_lag` is `unreadable (OSError)` within 3 s, cached 15 s; the briefing renders the line; `Schéma` renders `indisponible` as today |
| A probe hangs | cancelled at `timeout`; `unreadable (TimeoutError)`; the briefing's total added latency is bounded by the slowest briefing probe, not by their sum (they run concurrently, four at a time) |
| The lease row is missing | measured, `generation=null`, `healthy=false`, loud line naming a missing lease |
| Twelve sessions start in the same second | one probe run per fact (single flight), eleven cache hits |
| A caller loops on `brain_fact_get` with `max_age_seconds=0` | each call pays one probe, bounded by `timeout`; the tool is read-only and the probe has no side effect, so the cost is the caller's alone. No rate limit in lot A; the reviewer is asked whether one is needed (§9) |
| A probe returns a value over 4 KiB | the registry rejects it as `unreadable (ValueTooLarge)`: a fact is a number, not a corpus |

### 5.9 Constraints lot C and lot D impose on lot A

- Lot C needs a way to run the expensive probes (`model_liveness`, `factory_forge`) from a
  Dream phase with its own capability scope: `brain_fact_get` will be added to the `verify`
  allowlist then, and expensive probes carry `briefing=False` from day one so a briefing never
  pays them.
- Lot D needs facts as graph nodes: the fact name is the node key. Nothing in lot A stores a
  fact, so lot D projects from the catalogue (`brain_fact_list`) and from lot B's rows.

### 5.10 Placement and layering

New package `src/brain_v42/facts/` with `model.py`, `registry.py`, `render.py` (the French
lines), and `probes/graph_projection_lag.py`. Imports allowed: `brain_v42.db`,
`brain_v42.repositories`, `brain_v42.release`, the standard library. `brain_v42.mcp` and
`brain_v42.metrics` import `facts`; `facts` never imports them. `scripts/check_module_layering.py`
sees a new node with downward edges only and stays green with no baseline change.

## 6. Lot B — claims and verdicts (data model)

### 6.1 `knowledge_claims`

One row per claim an entry asserts. Insert-only in practice (a claim is edited by inserting a
new one and marking the old `retired_at`), so the verdict history of a claim never refers to a
statement that changed under it.

| Column | Type | Notes |
| --- | --- | --- |
| `id` | uuid PK | `gen_random_uuid()` |
| `entity_type` | text CHECK in (`learning`, `decision`, `adr`, `runbook`, `snippet`) | the asserting entry |
| `entity_id` | uuid | no database FK across five tables; the service checks existence in the same transaction |
| `project_key` | varchar(50) | denormalised from the entry for scope filtering; canonical form enforced |
| `statement` | text, 1–500 chars | what the entry asserts, for humans |
| `fact_name` | text, nullable | a catalogue name, checked against the registry at write time |
| `probe` | jsonb, nullable | inline probe `{kind, params}` from a closed set of kinds (lot C); exactly one of `fact_name`, `probe` is non-null (CHECK) |
| `target` | text CHECK in the `FactTarget` values | copied from the probe at write time, so the row says where it reads even if the catalogue moves |
| `expected` | jsonb object | `{"path": "/lag_seconds", "op": "lte", "value": 300}`; `op` in (`eq`, `ne`, `lt`, `lte`, `gt`, `gte`, `in`, `regex`, `present`) |
| `provenance` | text CHECK in (`measured`, `declared`) | whether the author measured when writing (a verdict row is expected) or only declared |
| `declared_by` | varchar(64) | the `X-Brain-Agent` identity or the human label |
| `declared_at` | timestamptz | author's instant |
| `recorded_at` | timestamptz, `now()` | server's instant |
| `retired_at` | timestamptz, nullable | set when the entry retires the claim; never deleted |
| `digest` | char(64) | sha256 of the canonical (`entity_type`, `entity_id`, `statement`, `fact_name`/`probe`, `expected`) |

Unique `(entity_type, entity_id, digest)` — the same claim written twice is one row. Index
`(fact_name)` partial `WHERE fact_name IS NOT NULL AND retired_at IS NULL` — "every live claim
on this fact" is the query lot D's hub and lot C's batch both run.

### 6.2 `knowledge_claim_verdicts` — append-only, after `delivery_attestations`

| Column | Type | Notes |
| --- | --- | --- |
| `id` | uuid PK | |
| `claim_id` | uuid FK → `knowledge_claims.id` ON DELETE RESTRICT | |
| `verdict` | text CHECK in (`holds`, `falsified`, `unreadable`) | tient / falsifié / illisible |
| `measurement` | jsonb object | the `Measurement` of §5.2 verbatim, including `target`, `measured_at`, `digest` |
| `digest` | char(64) | sha256 of the canonical `measurement`; two rows with the same digest are the same reading |
| `issuer_identity` | varchar(200) | `mcp:<X-Brain-Agent>`, `dream:verify:<run_id>`, `human:<label>` |
| `issuer_kind` | text CHECK in (`robot`, `human`) | |
| `idempotency_key` | varchar(200) | issuer-chosen; a replayed write is one row |
| `emitted_at` | timestamptz | the measurement's instant, from the issuer |
| `recorded_at` | timestamptz, `now()` | the server's instant |

Unique `(claim_id, issuer_identity, idempotency_key)`. Index `(claim_id, emitted_at DESC, id
DESC)` — the first row is the current verdict; a view `knowledge_claim_current` exposes it,
and no column of any table ever holds a "last verdict" that an UPDATE could rewrite. Role
grants: the application role may `INSERT` and `SELECT`; nobody has `UPDATE` or `DELETE`; the
downgrade is fail-closed and names the verdict rows it would destroy, after the 047/048
pattern.

Rules carried by the service, tested as negative cases:

- `unreadable` is never `falsified`: a comparison is attempted only on a `measured`
  measurement whose `path` resolves; anything else records `unreadable` with the reason.
- A verdict never touches the entry: no `freshness_status`, no `updated_at`, no tag, no
  `access_count`. The 043 trigger and the decay never see a verdict.
- A verdict on a claim whose `target` differs from the measurement's `target` is refused
  (`target_mismatch`): the 2026-09-12 failure mode, made impossible rather than unlikely.

### 6.3 Verdict computation

`compare(expected, measurement) -> Verdict`, pure and tested by table:

1. `measurement.status != "measured"` → `unreadable` (`reason=measurement.error_code`).
2. `expected.path` is a JSON pointer into `measurement.value`; absent → `unreadable`
   (`reason="path_absent"`).
3. `op` applied to the resolved value and `expected.value`; type mismatch (a string where a
   number is expected) → `unreadable` (`reason="type_mismatch"`), never `falsified`.
4. Otherwise `holds` or `falsified`.

### 6.4 Write paths

`brain_learn`, `brain_log_decision`, `brain_propose_adr`, `brain_create_runbook` and
`brain_save_snippet` accept an optional `claims: list[ClaimInput]` (at most 10 per entry). The
entry and its claims are written in one transaction. For a claim whose probe is cheap
(`briefing=True` in the catalogue), the service measures immediately and records the first
verdict in the same request, so an author who declares a false fact learns it before the tool
returns. Expensive probes leave the claim without a verdict until lot C's phase runs.

`brain_update` accepts `claims` under the same rule; a claim it does not repeat is retired, not
deleted.

### 6.5 Read paths

`brain_get` and `brain_search` append to each entry a compact verdict suffix, French because
rendered: `[claims : 2 tiennent · 1 FALSIFIÉ le 2026-09-19 (alembic_head mesuré 054, attendu
053)]`, or `[claims : 1 illisible]`, or nothing when the entry has no claim. The briefing's
recap lines carry the same suffix. A falsified claim changes the ranking of nothing in lot B:
what to do with a falsified entry is REORG's proposal, informed by the counts.

### 6.6 Migration and delivery constraints

- Number: the next free revision at merge time, 055 or later (054 is `delivery_attestations`,
  applied 2026-09-18). The head pin (`_REQUIRED_ALEMBIC_HEAD`), the DR contract (v12) and the
  documentation contract move in the same change, after runbook `22189c08` ("the seven guards").
- Applied in the merge window of its pull request (ADR #17); the writers that use the tables
  ship in the same release. The 2026-09-18 handoff (modes build / prove / cutover / canary /
  rollback) is the procedure; the private-target attestation is dry-run before the window.
- Downgrade: fail-closed; names the claim and verdict rows it would destroy; opt-in
  `-x allow_claims_downgrade=yes`.

### 6.7 Constraints from lot C and lot D

- Lot C writes verdicts only through a tool of its own (`brain_claim_verdict_record`),
  allowlisted for the `verify` phase alone, scoped to the phase's project, `issuer_kind=robot`.
- Lot D projects `ASSERTS` (entry → claim), `MEASURED_BY` (claim → fact) and `SUPERSEDES`
  through the existing ledger and projector; the claim tables therefore get outbox rows like
  every projected table, and the projection's own health is — fittingly — the first fact.

## 7. Verification and delivery

### 7.1 Required automated evidence (lot A)

Unit, no database:

- Registry: name grammar; duplicate refused; registration after `freeze` refused; TTL hit and
  miss with a fake clock; `max_age` shorter than TTL forces a probe; timeout → `unreadable`
  with `error_code="TimeoutError"`; a raising probe → `unreadable` with the class name, logged
  once; unreadable cached for `min(ttl, 30 s)`; single flight under `asyncio.gather` of twenty
  readers (one probe call counted); `measure_many` never exceeds `concurrency`; unknown name →
  `UnknownFactError`; a value over 4 KiB → `unreadable (ValueTooLarge)`.
- Renderer: the five cases of §5.6 by table, including the generic fallback and the age suffix
  boundary at 60 s.
- Briefing composer: the legacy call shape renders `tests/fixtures/briefing_full.md` unchanged;
  with one measured fact the line appears after `Schéma`; with an unreadable fact the line
  renders `illisible`; a registry failure never removes the section.
- Tools: `brain_fact_list` order and shape; `brain_fact_get` unknown name → business error
  `unknown_fact`; `max_age_seconds` forwarded; neither tool writes `access_log`
  (assert on the fake repository).
- Composition: the server registers exactly the catalogue of §5.5 (slice 1: one name), frozen;
  `test_module_layering` passes with no baseline change; the documentation contract and
  `docs/MCP_TOOLS.md` list the two tools.
- The metrics collector, rewired on `read_projection_state`, produces the same `/metrics`
  shape as before (existing tests stay green untouched).

Integration, against the test database (`tests/integration/db`):

- Empty outbox, armed lease → `lag_seconds == 0.0`, `healthy is True`.
- One undelivered row created 600 s ago → `lag_seconds >= 600`, loud rendering.
- Lease row with `neo4j_armed_generation <> generation` → `healthy is False`.
- No lease row → `generation is None`, `healthy is False`.
- The probe issues no statement other than `SELECT` (captured by the session's statement log).

### 7.2 Release sequence (lot A)

1. Independent review of the pull request before it is opened, with the tests read by the
   reviewer (rule of the current focus).
2. Merge; build the immutable release and cut over the eight writers with
   `docs/runbooks/2026-09-07-observable-delivery-workflows.md`, preflight
   `scripts/check_delivery_deployment.py`. No migration, so no apply window and no dump; the
   06:00–13:00 exclusion still applies to the cutover.
3. Canary on production: `brain_session_start` renders the `Projection graphe` line with
   `status=measured`; `brain_fact_get("graph_projection_lag")` returns `source="probe"` then,
   inside 15 s, `source="cache"` with the same `digest`; `/metrics` of the sidecar still carries
   `graph_outbox` with its previous keys.
4. Receipt under `docs/receipts/` with the measured values of the canary.

Rollback: the previous release's drop-ins (`90-immutable-release.conf`), the documented path;
nothing to restore in the database.

## 8. Alternatives considered

- **Read the metrics sidecar from the briefing.** Rejected: an HTTP hop inside a tool call to a
  process with its own failure modes; no fact naming; the sidecar is loopback-only by design and
  is not a read API for agents.
- **Persist facts in a table now.** Rejected for lot A: the ticket says "sans migration", and a
  persisted fact without a claim is a value copied forward by a machine instead of a human. The
  verdict row is the persistence, tied to what it proves.
- **An open catalogue (probes declared in configuration or by tools).** Rejected: an SQL string
  in a configuration key is an injection surface and an unbounded query; a closed catalogue in
  code is reviewed like code.
- **Model-judged facts.** Rejected: a verdict must be reproducible from a probe and a target;
  prose judgement is a proposal of lot C with a human in the loop.
- **A single `facts` table replacing `SchemaStateService` and the killswitch reader.** Rejected
  as a first step: the two existing readers become probes without moving; nothing is rewritten
  to fit the abstraction before the abstraction has carried one new fact.

## 9. Questions for the independent review

1. Is caching an `unreadable` measurement for `min(ttl, 30 s)` right, or should unreadable
   never be served from cache so that recovery is visible on the very next read?
2. Should the loud threshold (300 s) live in the renderer, as specified, or be a declared
   attribute of the probe so that lot B claims and the briefing cannot diverge on "what is late"?
3. Is a flat snake_case name enough, or should facts be namespaced (`graph.projection_lag`)
   given that lot B stores the name as a string and lot D uses it as a node key?
4. Does `brain_fact_get(max_age_seconds=0)` need a rate limit in lot A, given that probes are
   side-effect-free and individually bounded?
5. In §6.1, is "no database FK, existence checked by the service" acceptable for a
   polymorphic `entity_id`, or should the five entity tables each carry their own claims table?
6. Is the `expected` operator set (§6.1) minimal and sufficient for the five immediate targets
   named by the ticket (copied Alembic revisions, stale "X is dead", moved paths, live release
   SHA, a self-contradicting document)?
7. What in §5.8 is missing — which failure would make the first fact lie rather than say
   `unreadable`?

## 10. Sources

- ADR #27 `fb9b75bd` (accepted 2026-09-19); ticket `59a5a9c7` (body corrected 2026-09-19).
- `src/brain_v42/mcp/tools/session_tools.py` (`_section_technical_state`, `load_briefing`);
  `src/brain_v42/services/schema_state_service.py`; `src/brain_v42/services/dream_run_service.py`;
  `src/brain_v42/metrics/collector.py`; `src/brain_v42/repositories/pg_graph_ledger.py`;
  `src/brain_v42/release.py`; `src/brain_v42/db/delivery_tables.py`;
  `src/brain_v42/mcp/dream_capabilities.py`; `scripts/check_module_layering.py`.
- Ticket `416266ec` (projection freeze, measured 2026-09-07); measured recovery state of
  2026-09-19 00:49Z (generation 93 armed, outbox empty).
- `docs/superpowers/specs/2026-09-07-observable-delivery-workflows-design.md` (the shape of
  this document and of `delivery_attestations`).
