# Measured facts and claims

**Date:** 2026-09-19 — **revision 2 on 2026-09-20**, answering the independent review of
revision 1 (`2026-09-19-measured-facts-and-claims-review-codex-astra.md`, verdict REWORK)

**Status:** proposed specification for ADR #27 (`fb9b75bd`, accepted 2026-09-19); lot A is
specified to implementation depth, lot B to data-model depth; nothing here is implemented

**Project:** `brain-v42`

**Source baseline:** `da96f31e50ccdb55b8bdfe2a78ecac6e4073ed0b` (main, after PR #153; the
delivery observer fix of PR #155 is live in release `b4f7194d`)

**Carrier ticket:** `59a5a9c7` (lots A, B, C, D)

**Delivery target:** lot A — a closed registry of measured facts, each bound to a verified source,
rendered live in the session briefing and readable by name through MCP, with
`graph_projection_lag` as its first fact, on the production MCP service without a schema change.
Lot B — the data model of structured claims and append-only verdicts, ready for a migration
numbered 055 or later.

## Revision 2 — what changed and why

The review of revision 1 found eleven defects, nine of them contracts under which a verdict
could lie. Each is answered in the section named below; the finding numbers are the review's.

| Finding | Answer | Section |
| --- | --- | --- |
| 1 — a declared target proves nothing about the source read | every probe is bound to a **verified source identity**, measured in the same round trip as the value, and compared with the identity the composition root declared for the target; facts carry a **definition version**; a verdict checks fact name, definition version and source identity before comparing anything | §5.3, §5.5, §6.3 |
| 2 — the "current verdict" was a max over an issuer-supplied instant | verdict rows carry a server sequence; "latest attempt", "last conclusive verdict" and "validity now" are three distinct reads; a future `emitted_at` is refused; expiry is derived at read time | §6.2, §6.4 |
| 3 — uniqueness forbade re-asserting a retired claim; digest ignored the target; no supersession link | claim **key** (content identity, includes target and definition version) versus claim **occurrence** (a row with its own lifecycle and a `replaces_id`); uniqueness on active occurrences only; `claims` PATCH semantics defined; compare-and-swap on the active set | §6.1, §6.5 |
| 4 — no durable referential anchor; claim content mutable; append-only by code path only | FK to `brain_entities` (coverage measured complete); `BEFORE UPDATE/DELETE` triggers; role grants; idempotent replay compares content | §6.1, §6.2, §6.7 |
| 5 — shared task, cancellation, shutdown, false latency bound | registry-owned tasks, shielded waits, identity-checked cleanup, `aclose()` in the server's shutdown order, one global producer bound, bounded queue wait, a briefing budget instead of a false promise | §5.4, §5.8 |
| 6 — wall clock for TTL; `max_age=0` undefined | monotonic clock for ages, wall clock only for published instants; `max_age=0` bypasses both caches and joins an in-flight run; negative ages refused | §5.4 |
| 7 — mutable payload under a frozen digest; incompatible digest recipe | `Measured` / `Unreadable` as two immutable types; the canonical JSON **text** is the stored form; a versioned recipe with a domain prefix, no floats, bounded depth and size | §5.2 |
| 8 — existing readers are not strict probes | each is specified as an adapter to write, with what it must stop doing; model liveness leaves lot A | §5.6 |
| 9 — who turns a declaration into a measurement | the server measures, always; a caller names a claim, never supplies a measurement; reasons persisted | §6.3, §6.6 |
| 10 — "up to date" overclaims; `healthy` is Python, not SQL | rendering says what is proved (outbox and lease, not Neo4j content); `healthy` is one shared Python predicate; exhausted rows stay loud | §5.5, §5.7 |
| 11 — canary and tests did not exercise the wired behaviour | canary forces a probe before reading the cache; briefing proved through the shared loader; native and compact gateways tested with their middleware; "no side effect" scoped to the probe | §7 |

The review's answers to the seven questions of revision 1 are adopted as written, with one
adjustment: the loud threshold becomes a **declared, versioned policy** on the fact descriptor
(§5.5) shared by the renderer and by lot B, not a renderer constant.

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

A fourth one happened while revision 1 was under review: on 2026-09-19 the delivery observer
failed every observation of a contract for an hour with only `provider_invalid_response` to
read (ticket `731ab364`). The fix shipped a bounded diagnostic and a rule this document now
carries everywhere: **a measurement names where it read and what refused it, never a value it
was not supposed to carry.**

ADR #27 fixes the model: an entry is durable reasoning plus zero or more measurable **claims**,
each tied to a named **fact** with one read-only, bounded **probe** and a named, verified
**target**; verdicts live at the granularity of the claim, never of the entry; prose references
the name of a fact and never copies its value.

## 2. Ownership and sources of authority

| Concern | Authority |
| --- | --- |
| The catalogue of facts (names, targets, probes, bounds, policies, definition versions) | Code, registered at the composition root; closed at runtime |
| The identity a target must have (database name, server address, host, release) | The composition root, from settings; never the probe author, never a caller |
| The value of a fact | The named target, read by its probe at measurement time; never prose |
| What a claim asserts and what it expects | The author of the entry (agent or human), stored with the entry — **declared** |
| A verdict on a claim | The server, from a measurement it produced itself, compared mechanically — **measured** |
| Whether an entry is archived | A REORG proposal informed by verdicts, executed by a human or by the archive-only guard already in production; never by a verdict |
| Rendering in the briefing, search, and rendered documents | Brain, at read time, from the live value and the fact's declared policy |

Two words carry the whole doctrine: **measured** (a value a probe read from a verified
target, with the instant of reading) and **declared** (a value somebody wrote). A briefing
line is measured. A claim's `expected` is declared. A verdict is measured. An identity a caller
sends in a header (`X-Brain-Agent`) is declared, and this document never lets it upgrade a
declaration into a measurement (§6.3).

## 3. Scope and non-goals

In scope:

- **Lot A** — package `brain_v42.facts`: `Measured`, `Unreadable`, `Probe`, `FactRegistry`,
  the verified source identity of PostgreSQL targets, the first probe `graph_projection_lag`,
  its rendering in the briefing's `### État technique (mesuré)` section, and two read-only MCP
  tools, `brain_fact_list` and `brain_fact_get`. No schema.
- **Lot B (data model only)** — tables `knowledge_claims` and `knowledge_claim_verdicts`, their
  constraints, triggers and grants, the verdict computation, and the write and read paths that
  lot B's implementation will carry. The migration itself, its tests and its apply window are
  lot B's own delivery.

Out of scope, and stated so the reviewer does not look for them:

- Lot C (the nightly VERIFY phase) and lot D (the Neo4j projection of claims and facts). Both
  consume this design; both are specified when their turn comes. Constraints they impose are
  named in §5.9 and §6.7. **Nothing in lot A or lot B publishes an outbox event for a claim or
  a fact**: the ledger's relation catalogue (`validate_relation_shape`,
  `src/brain_v42/repositories/pg_graph_ledger.py:162`) does not know those kinds, and extending
  it is lot D.
- Catch-up of the existing stock (thousands of entries without claims). The past stays readable
  as dated opinion; the frontier is dated.
- Any automatic archiving, deletion, or freshness change driven by a verdict.
- Any LLM in the measurement path. Probes are mechanical. Prose-judged claims are proposals of
  lot C, never verdicts.
- Persisting facts in lot A. A verdict row (lot B) is the persistence of a measurement.
- Proving the **content** of Neo4j. The first fact proves the PostgreSQL side of the
  projection (outbox and lease); the runbook names Neo4j restored inside a generation as a
  blind spot of that side (`docs/GRAPH_LEDGER_RUNBOOK.md:447`), and this document does not
  claim to cover it.

## 4. Existing integration points

Every one of these is reused or extended; none is duplicated. Revision 2 corrects three
claims revision 1 made about them (marked ►).

| Existing piece | Location | Role in this design |
| --- | --- | --- |
| Measured schema revision in the briefing | `SchemaStateService.current_revision`, `src/brain_v42/services/schema_state_service.py` | The query of fact `alembic_head` (slice 2); the service stays, the probe wraps it and adds the source identity |
| ► Killswitch state | `DreamRunService.killswitch_state`, `src/brain_v42/services/dream_run_service.py:29,113,136` | **Not a probe as is**: it masks an unreadable file with history and returns disabled flags when no recent night exists. §5.6 specifies the adapter |
| Graph outbox and projector statistics | `MetricsCollector`, `src/brain_v42/metrics/collector.py:587-742` | Its SQL is the probe of `graph_projection_lag`; extracted into the repository layer and shared; ► `healthy` is computed in Python at `:742`, not in SQL, and stays a shared Python predicate |
| Projection inventory | `PgGraphLedger.projection_inventory`, `src/brain_v42/repositories/pg_graph_ledger.py:764` | Same table, recovery-oriented; the shared query lands next to it |
| Verified database identity | `plan_index_repair_store.py:291` (`current_database()`, `inet_server_addr()`, `inet_server_port()` read inside the mutation transaction) | The precedent for the source identity of PostgreSQL targets (§5.3) |
| ► Shipped Alembic head and package version | `brain_v42.release` (`shipped_alembic_head`, `package_version`, `head_of_versions`) | `package_version` is a distribution version, never a SHA; `head_of_versions` skips unreadable files. §5.6 specifies strict variants |
| ► Model liveness | `scripts/probe_model_liveness.py:146,161` (POST inference, 90 s, verdicts ALIVE/GONE/BUSY/OTHER) | **Leaves lot A**: it calls a model and spends quota. Lot C sonde with an explicit inference budget; BUSY and OTHER map to unreadable |
| Canonical digest recipe | `src/brain_v42/models/delivery_hashes.py:25,70,82` (no floats, string keys, depth ≤ 64, NUL and surrogates refused, domain prefix `brain-delivery-<domain>:v1\n`) | The measurement recipe of §5.2 follows the same rules under its own domain prefix; **integers only** |
| Append-only evidence with idempotency | `delivery_attestations`, `src/brain_v42/db/delivery_tables.py:472`; replay compares content, `pg_delivery_attestations.py:150` | The template of `knowledge_claim_verdicts` (§6.2); ► append-only there is by code path (`054_delivery_attestations.py:11`), here it is by trigger and grant |
| Durable entity anchor with tombstones | `brain_entities` (migration 033): `id`, `entity_type`, `entity_key`, `source_uuid`, `project_key`, `lifecycle` (active/archived/deleted), `revision`, `deleted_at` — coverage measured 2026-09-20: 3815 learnings, 1267 decisions, 119 ADRs, 176 runbooks, 191 snippets, equal to the five knowledge tables | The FK anchor of `knowledge_claims` (§6.1) |
| Bounded diagnostic on a refused payload | `ProviderError.diagnostic`, `src/brain_v42/delivery_observer/transport.py`; `_diagnostic()` in `github.py` (PR #155) | The `Unreadable.error_code` / `where` convention of §5.2 |
| Briefing technical section | `_section_technical_state`, `session_tools.py:412`; fixture `tests/fixtures/briefing_full.md` | Gains one line per briefing fact; the fixture is unchanged when no registry is wired |
| Capability scope per Dream phase | `src/brain_v42/mcp/dream_capabilities.py`, `test_dream_prompts_match_phase_allowlists.py` | New tools are absent from every phase allowlist until lot C names them; `X-Brain-Agent` is declared, not verified (`dream_capabilities.py:280`) |
| Tool catalogue profiles | `src/brain_v42/mcp/tool_catalog.py:67` (compact gateways `brain_find_tool` / `brain_call_tool`) | Both profiles tested (§7.1) |
| Provenance middleware | `src/brain_v42/mcp/provenance_middleware.py:89`, `pg_brain_session.py:340` (a session may be auto-opened before any tool) | Why "no side effect" is scoped to the probe (§5.8) |
| Server shutdown order | `src/brain_v42/mcp/server.py:220` | Where `FactRegistry.aclose()` is called (§5.4) |
| Module layering DAG | `scripts/check_module_layering.py`, `tests/unit/test_module_layering.py` | `facts` is a new node importing only downward (§5.10) |
| Graceful degradation of the briefing | `load_briefing`, `session_tools.py:541` | Facts follow the technical-section rule: a failure renders, it never vanishes |
| Release without schema | `docs/receipts/2026-09-13-release-21ff55d2.md`, `docs/receipts/2026-09-19-release-b4f7194d.md` | The delivery path of lot A (§7.2) |
| Release with schema | handoff `~/.local/state/brain-v42-delivery/handoff-2026-09-18/` (modes build / prove / cutover / canary / rollback); `scripts/check_delivery_deployment.py:1086` pins the required head | The delivery path of lot B (§6.8); the pin moves with the migration |

## 5. Lot A — the facts registry

### 5.1 Vocabulary

- **Fact**: a named, measurable property of one target. Name grammar `^[a-z][a-z0-9_]{0,63}$`,
  flat snake_case, unique in the catalogue, **never reused with another meaning**: a fact that
  changes meaning is a new name. Each fact carries a **definition version** (`definition_version`,
  a positive integer bumped whenever the probe's query, value shape or policy changes); lot B
  claims record the version they were written against.
- **Target**: where the probe reads. Closed enumeration `FactTarget`:
  `production` (the production PostgreSQL), `brain_test` (the test database), `live_release`
  (the running process and its installed package), `repository` (the checked-out source tree),
  `host` (files and units on the host), `github`, `provider` (a model or embedding endpoint).
  A probe names exactly one target — and the registry **verifies** it (§5.3).
- **Source identity**: the measured, non-secret identity of what a probe actually read, taken
  in the same round trip as the value and compared with what the composition root declared for
  the target. For PostgreSQL targets: `current_database()`, `inet_server_addr()`,
  `inet_server_port()`, and the stamped Alembic revision. The 2026-09-12 incident that
  motivates it: a probe pointed at `brain_test` (then at 052) would have falsified a true
  statement about production; with an identity check it says `unreadable (target_mismatch)`.
- **Probe**: the single read-only, bounded reader of a fact.
- **Measurement**: either a `Measured` or an `Unreadable` (§5.2). Never a third thing.
- **Policy**: a declared, versioned rule attached to a fact descriptor that a reader may apply
  to a value (for example `late_after_seconds: 300`). It is data of the catalogue, not a
  measured property, and the renderer and lot B claims read the same one.
- **Unreadable**: a measurement that did not happen or could not be trusted (timeout, refused
  connection, missing table, malformed answer, wrong source identity, value over bound).
  Unreadable is a status, never a value; it is never rendered as a number and, in lot B, never
  becomes *falsified*.

### 5.2 Measurement: two immutable types and one canonical recipe

```python
@dataclass(frozen=True, slots=True)
class Measured:
    fact: str
    definition_version: int
    target: FactTarget
    source: SourceIdentity          # frozen dataclass, the measured identity of what was read
    value_json: str                 # CANONICAL JSON text, the stored form; <= 4096 bytes UTF-8
    digest: str                     # sha256 of the domain prefix + value_json
    observation_id: UUID            # one per probe run, never shared by two runs
    measured_at: datetime           # UTC wall clock, the instant the probe returned
    duration_ms: int
    ttl_seconds: int
    source_kind: Literal["probe", "cache"]

    @property
    def value(self) -> dict[str, JSON]:   # a FRESH object on every access, parsed from value_json
        ...

@dataclass(frozen=True, slots=True)
class Unreadable:
    fact: str
    definition_version: int
    target: FactTarget
    error_code: str                 # closed vocabulary below
    where: str | None               # "<ExceptionClass> in <probe function>", the PR #155 convention
    observation_id: UUID
    measured_at: datetime
    duration_ms: int
    ttl_seconds: int
    source_kind: Literal["probe", "cache"]

Measurement = Measured | Unreadable
```

Rules:

- **Immutability.** `Measured` holds no mutable container: the value lives as canonical JSON
  text and `value` parses it into a fresh object each time. A caller that mutates what it got
  mutates its own copy; the cache, the digest and the next reader are unaffected. `source` is a
  frozen dataclass of scalars. Tests mutate the returned mapping (top level and nested) and
  re-read the measurement.
- **Impossible states cannot be built.** Status is the type. There is no `value` on
  `Unreadable` and no `error_code` on `Measured`; a `Measured` whose `value_json` is not
  canonical or whose digest does not match cannot be constructed (`__post_init__` recomputes
  both and refuses). Any `Measurement` is JSON-serialisable for the tools with a `status`
  discriminator derived from the type.
- **Canonical recipe, versioned.** `brain_v42.facts.canonical`: `canonical_json(value)` —
  sorted keys, `(",", ":")` separators, `ensure_ascii=False`, UTF-8; value domain = `null`,
  `bool`, `int`, `str`, objects with string keys, arrays; **no floats** (durations in
  milliseconds, ages in whole seconds, ratios as parts per million integers), NUL and
  surrogates refused, depth ≤ 8, ≤ 4096 bytes after encoding. `digest = sha256(b"brain-v42-fact-measurement:v1\n" + value_json.encode())`. The recipe version is in the
  prefix: a change of recipe changes every digest, on purpose. Tests cover a float, a nested
  mutation, NaN reaching the recipe as a float, a 4097-byte value and a depth-9 value.
- **`observation_id` versus `digest`.** Two probe runs that read the same value share a
  digest and have different observation ids. Lot B stores both: the digest says *what*, the
  observation id says *which reading*.
- **`error_code` vocabulary** (closed, stable): `timeout`, `capacity_timeout`,
  `briefing_budget`, `refresh_budget`, `target_mismatch`, `probe_error`, `value_too_large`,
  `value_not_canonical`, `identity_unreadable`. `where` names the exception class and the probe
  function for `probe_error` and `value_not_canonical`, nothing else, and never an exception
  message.

### 5.3 Probe contract and verified source identity

```python
class Probe(Protocol):
    name: str
    definition_version: int
    target: FactTarget
    ttl: timedelta            # cache validity of a measured value
    timeout: timedelta        # hard bound the registry applies to measure()
    briefing: bool            # rendered in the session briefing (cheap probes only)
    policies: Mapping[str, int]   # declared, versioned by definition_version
    async def measure(self, source: SourceSession) -> Mapping[str, JSON]: ...
```

The registry, not the probe, opens the source. `SourceSession` is what the composition root
built for the target: for PostgreSQL targets it wraps an `AsyncSession` opened in **read-only
autocommit** mode (`SET TRANSACTION READ ONLY` is issued by the registry before the probe runs,
so a probe that tried to write would fail in PostgreSQL, not in a code review) and it exposes
`identity()` — the query of the precedent at `plan_index_repair_store.py:291` plus
`alembic_version`, executed in the same session immediately after the probe's own query, so
the identity and the value come from one connection and one moment.

Rules, all of them tested:

1. **Read-only, enforced.** PostgreSQL probes run in a read-only transaction; `host` probes
   receive a path-restricted reader (`read_text_under(root, relative)` that refuses `..`,
   symlinks out of `root` and files over 64 KiB); `github` and `provider` probes receive an
   HTTP client that permits `GET` only and rejects any other verb at the client boundary. A
   probe never calls a tool, a model, or another probe. Static check: the probe package imports
   nothing from `brain_v42.mcp`, `brain_v42.dream`, `brain_v42.agents` or `brain_v42.services`
   except `SchemaStateService` (layering test, §5.10).
2. **Bounded.** `timeout` applies to `measure()` **and** the identity query together; a probe
   also bounds its own rows and response size. Defaults: 3 s on a database, 1 s on the host,
   10 s on the network; `briefing=True` probes may not declare more than 3 s. Probes must be
   cooperative (asyncio-native I/O with their own timeouts): a blocking call cannot be cancelled
   by `wait_for`, so the registry runs every probe in the event loop and a probe that needs a
   thread is not a probe of this registry.
3. **One probe per fact.** A second registration under a name already registered raises
   `DuplicateFactError`, whatever the definition version.
4. **Verified target.** The composition root declares, per target, the identity it expects —
   for `production`: database name and server port from `settings.postgres_url`, never a
   secret — and the registry compares the measured identity with it after every run. A
   mismatch is `Unreadable(error_code="target_mismatch")` and the measured identity is logged
   once at warning level (database name and port only). A measured identity that could not be
   read is `identity_unreadable`. **No probe on a target whose identity the registry cannot
   measure ships**: `host` and `live_release` identities are `hostname` + release SHA parsed
   from `brain_v42.__file__` (`releases/<sha>/`) and `package_version()`; `github` identity is
   the API origin plus the authenticated principal returned by the credential's own endpoint,
   read once per process and refreshed with the token; `provider` identity is the endpoint
   origin plus the `/healthz` body's identity fields. Slice 1 needs only `production`.
5. **Raising is the failure protocol.** A probe that cannot measure raises; the registry turns
   the exception into `Unreadable(error_code="probe_error", where=...)` with the PR #155
   convention (class name + function, never a message), and logs it at warning level once per
   failure, not once per reader.
6. **No side effect on the target**: no `access_log` row, no metrics counter, no heartbeat, no
   write of any kind. This is a property of the **probe**; the MCP transport's own telemetry
   (client-activity POST per tool call, provenance middleware) applies to `brain_fact_get` as
   to every tool and is explicitly out of this rule (§5.8).

### 5.4 Registry

```python
class FactRegistry:
    def __init__(self, *, sources: Mapping[FactTarget, SourceFactory], expected: Mapping[FactTarget, SourceIdentity],
                 monotonic: Callable[[], float] = time.monotonic, wall: Callable[[], datetime] = _utcnow,
                 concurrency: int = 4, queue_timeout: timedelta = timedelta(seconds=2),
                 refresh_budget: RefreshBudget = RefreshBudget(per_fact_per_minute=10)) -> None: ...
    def register(self, probe: Probe) -> None        # DuplicateFactError, InvalidFactNameError, RegistryFrozenError, UnverifiableTargetError
    def freeze(self) -> None
    def names(self) -> tuple[str, ...]
    def describe(self, name: str) -> FactDescriptor  # name, definition_version, target, ttl, timeout, briefing, policies
    def briefing_names(self) -> tuple[str, ...]
    async def measure(self, name: str, *, max_age: timedelta | None = None) -> Measurement
    async def measure_many(self, names: Iterable[str], *, max_age: timedelta | None = None,
                           budget: timedelta | None = None) -> dict[str, Measurement]
    async def aclose(self) -> None
```

Semantics:

- **Closed catalogue.** `register` is legal only before `freeze()`; the composition root
  registers every probe, freezes, and hands the registry to the tools. A probe whose target
  has no `SourceFactory` or no expected identity is refused at registration
  (`UnverifiableTargetError`): the catalogue cannot hold a fact the registry could not verify.
- **Two clocks.** Ages, TTLs, timeouts and budgets use the injected **monotonic** clock;
  `measured_at` uses the wall clock and is never used for arithmetic. A wall clock stepping
  backwards cannot extend a cache entry; tests step both clocks independently.
- **Cache with TTL.** A `Measured` is served from cache while `mono_now - measured_mono <=
  min(ttl, max_age)`; the copy served carries `source_kind="cache"` and its original
  `measured_at` and `observation_id`. An `Unreadable` is cached for `min(ttl, 30 s)` under
  the same rule (`max_age` applies to it too). The cache is per process; two MCP transports are
  two caches, and a value is at most `ttl` old in each.
- **`max_age` exact rules.** `None` → the TTL. A positive value shorter than the TTL → that
  value. **Zero → bypass both caches, but join an in-flight run** (the caller asked for a fresh
  reading, and one is being taken). Negative → `ValueError` before anything runs. Zero is
  charged to the refresh budget (below); a positive `max_age` that misses the cache is charged
  too; a cache hit is free.
- **Refresh budget.** A per-process token bucket per fact, `per_fact_per_minute` (10 by
  default), charged by every probe run that a caller forced (`max_age` shorter than the TTL).
  Saturation returns `Unreadable(error_code="refresh_budget")` immediately — **never a cached
  value presented as fresh**. The briefing's own reads use `max_age=None` and are not charged.
- **Single flight, owned by the registry.** A stale fact triggers one `asyncio.Task` created
  and held by the registry in `_inflight[name]`; every reader awaits `asyncio.shield(task)`,
  so a cancelled reader (a client that disconnected mid-briefing) never cancels the
  measurement other readers wait for. Cleanup is a done-callback that removes the entry only
  if `_inflight.get(name) is task` (identity check: a newer task for the same name is never
  evicted by an older one's callback). Tests: cancel the first reader, cancel a follower,
  twenty concurrent readers count one probe run.
- **One global bound on producers.** A single `asyncio.Semaphore(concurrency)` guards every
  probe run — `measure()` and `measure_many()` alike — so the process never runs more than
  four probes at once whatever the mix of callers. Acquisition is bounded by `queue_timeout`;
  a caller that cannot get a slot in time receives `Unreadable(error_code="capacity_timeout")`
  and no probe runs for it. The deadline of one `measure()` is therefore
  `queue_timeout + timeout`, stated in the descriptor.
- **Briefing budget.** `measure_many(..., budget=...)` runs the facts under the global bound
  and stops handing out new runs once the budget elapses; facts not started render
  `Unreadable(error_code="briefing_budget")`. The briefing passes a 4 s budget. The honest
  bound is therefore "the briefing adds at most about 4 s", not "the slowest probe": with more
  facts than slots there are waves, and revision 1's claim was false.
- **Shutdown.** `aclose()` cancels the in-flight tasks, awaits their completion (each swallows
  its own `CancelledError` into `Unreadable`), and refuses new measurements afterwards
  (`RegistryClosedError`). The composition root calls it in the server's existing shutdown
  order (`server.py:220`) **before** the database engine is disposed, so no probe task
  outlives its connection factory.
- **Unknown names fail closed.** `measure("nope")` raises `UnknownFactError`; the MCP tool
  maps it to the business error `unknown_fact` naming the catalogue.

### 5.5 The first fact: `graph_projection_lag`

Definition version 1. Target `production`. TTL 15 s. Timeout 3 s. `briefing=True`.
Policies: `late_after_seconds: 300`.

The probe runs the query the metrics collector runs today, moved out of the collector into
`brain_v42.repositories.pg_graph_ledger.read_projection_state(session) -> ProjectionState`,
and the health predicate moves with it: `projection_health(state) -> bool` is **one Python
function** (`armed and lease_active and not recovery_active`, the collector's own at
`collector.py:742`) that the collector, the probe and lot B call. The collector is rewired
onto both in a second commit of the same pull request, after `impact()` on `collect_db_stats`
has been read and reported; its `/metrics` output does not change shape and keeps its
`available=false` sentinels and `-1` generation for its own consumers. **The shared query
propagates errors**; only the collector's adapter turns them into its sentinel zeros. A
probe that returned the collector's zeros on error would be the lie revision 1's reviewer
named first.

Value (all keys always present, integers only):

```json
{
  "pending": 0,
  "ready": 0,
  "claimed": 0,
  "exhausted": 0,
  "lag_seconds": 0,
  "generation": 93,
  "armed": true,
  "lease_active": true,
  "recovery_active": false,
  "healthy": true
}
```

- `lag_seconds` is the collector's `oldest_pending_age_seconds` truncated to a whole second:
  the age of the oldest undelivered, non-exhausted outbox row, `0` when nothing is pending. It
  is the number the name promises. The query's `now` is one SQL instant (`observed` CTE) for
  every column, as today.
- `healthy` is `projection_health(state)`, computed in Python from the four SQL booleans and
  returned in the value so the briefing, the tool and a lot B claim read one predicate.
- `generation` is `null` when the lease row is absent (a fresh database); `armed`,
  `lease_active` and `healthy` are then `false`.

**Scope of what it proves**: the PostgreSQL side of the projection — an outbox with no
observed backlog and a lease that is armed and held. It does **not** prove that Neo4j holds
the projected content; a Neo4j restored inside the same generation is invisible to it
(`docs/GRAPH_LEDGER_RUNBOOK.md:447,493`). The rendering says so (§5.7) and lot D owns the
content proof.

What the 2026-09-07 freeze would have produced: `pending=114`, `lag_seconds` growing past
28 800 within eight hours, `armed=false`, `healthy=false` — a loud line in every briefing from
the first session after 14:00 CEST.

### 5.6 The rest of the initial catalogue: adapters to write, not readers to reuse

| Fact | Target | TTL / timeout | Adapter, and what it must stop doing | Slice |
| --- | --- | --- | --- | --- |
| `alembic_head` | production | 60 s / 3 s | `SchemaStateService.current_revision` inside a `SourceSession`; the identity query already carries the revision, so the probe and the identity agree by construction | 2 |
| `alembic_head_shipped` | live_release | process lifetime / — | a **strict** `head_of_versions`: any unreadable or unparsable revision file → raise (today's version skips it and may return an older head, `release.py:90`); memoised once per process; the identity is the release SHA + `package_version()` | 2 |
| `live_release_sha` | live_release | process lifetime / — | parsed from the running package's own path (`releases/<sha>/`); `unreadable` when the process does not run from a release (development), never `"dev"` as a value | 2 |
| `dream_killswitches_declared` | host | 60 s / 1 s | reads **only** the drop-in file through the restricted reader and returns the parsed flags plus the file's mtime; a missing or unreadable file is `unreadable`, never a set of disabled flags; the DB-derived history (`last_run_date`, clean dry nights) is a **separate** fact `dream_last_night` on `production`, so "declared on disk" and "what the last night did" never blend into one value as `killswitch_state` blends them today (`dream_run_service.py:29,113,136`) | 2 |
| `dream_last_night` | production | 60 s / 3 s | `dream_runs` of the latest `run_date`: per phase status and dry flag; absence of any night is `unreadable`, not "all disabled" | 2 |
| `delivery_observer_collection` | production | 60 s / 3 s | `max(collection_finished_at)` of `delivery_confirmations` and the count of `error` outcomes in the last hour; the observer's own liveness as a fact | 3 |
| `embedding_endpoint` | provider | 60 s / 2 s | `GET /healthz` of the shim; identity = origin + the body's identity fields | 3 |
| `model_liveness` | provider | — | **not a lot A fact**: `probe_model_liveness.py` POSTs an inference (90 s, quota). Lot C sonde with an inference budget; ALIVE → holds, GONE → falsified, BUSY and OTHER → unreadable | C |
| `factory_forge` | github | 1 h / 10 s | `gh`-equivalent search of pull requests by author through the observer's transport; nightly only | C |

### 5.7 Rendering in the briefing

`_section_technical_state` gains a `facts: Sequence[Measurement]` argument (default empty, so
the legacy call shape renders exactly the pinned fixture). Each briefing fact renders one line
after `Schéma`, in the registration order, in French because the section is rendered output.
The renderer applies the fact's **declared policy**, never a constant of its own:

| Case | Line |
| --- | --- |
| Measured, `healthy`, `lag_seconds <= late_after_seconds`, `exhausted == 0` | `- Projection graphe : aucun retard observé dans l'outbox — 0 en attente, génération 93 armée, bail tenu` |
| Measured, `not healthy` or `lag_seconds > late_after_seconds` | `- Projection graphe : EN RETARD de 4j 2h — 114 en attente, génération 70 NON armée, bail tenu` |
| Measured, `exhausted > 0` (even with `pending == 0`) | the loud form plus `, 3 épuisées` |
| Unreadable | `- Projection graphe : illisible (timeout)` — and `illisible (cible inattendue)` for `target_mismatch`, the one case a reader must never mistake for a transient |
| Served from cache older than 60 s | the line plus ` (mesuré il y a 3 min)` |

"À jour" does not appear: the fact does not prove Neo4j's content and the line must not say
more than the probe measured. The loud form carries the numbers a reader needs to open
`docs/GRAPH_LEDGER_RUNBOOK.md`, section "Incident and projection recovery 035", without a
second call.

Unreadable renders; it never vanishes. Rendering is a pure function
`render_fact_line(measurement, descriptor, now_mono)` tested without a database. A fact the
renderer does not know renders generically as `- <name> : <value_json cut at 120 characters>`.

### 5.8 MCP tools

Two read-only tools, registered in the compact and native profiles, absent from every Dream
phase allowlist until lot C names them, and never counted as an access on any entity.

- `brain_fact_list()` → descriptors (`name, definition_version, target, ttl_seconds,
  timeout_seconds, deadline_seconds, briefing, policies`) with `last: {status, measured_at,
  source_kind, observation_id} | null` from the cache; it measures nothing.
- `brain_fact_get(name: str, max_age_seconds: int | None = None)` → the `Measurement` as JSON
  with a `status` discriminator. `max_age_seconds` follows §5.4 exactly (0 forces a probe and is
  charged to the refresh budget; negative is `invalid_argument`). Errors: `unknown_fact`. An
  unreadable measurement is a normal result, not an error.

**What "no side effect" means here.** The probe has none (§5.3 rule 6). The tool call itself
goes through the MCP transport like every other call: the provenance middleware may auto-open
a session (`provenance_middleware.py:89`), and the client-activity emitter posts one
observation to the sidecar. Those are properties of the transport, unchanged by this design,
and the tests assert the probe's silence (no statement other than `SELECT`, no `access_log`
row), not the transport's.

### 5.9 Constraints lot C and lot D impose on lot A

- Lot C runs the expensive sondes (`model_liveness`, `factory_forge`) from a Dream phase with
  its own capability scope; `brain_fact_get` is added to the `verify` allowlist then, and those
  sondes carry `briefing=False` from day one.
- Lot D needs facts as graph nodes keyed by name and definition version; lot A stores nothing,
  so lot D projects from the catalogue (`brain_fact_list`) and from lot B's rows — and only once
  the ledger's catalogue accepts `claim` and `fact` kinds and the `ASSERTS`, `MEASURED_BY` and
  `SUPERSEDES` relations (`pg_graph_ledger.py:162`). Until then no outbox row mentions them.

### 5.10 Placement and layering

New package `src/brain_v42/facts/` with `model.py`, `canonical.py`, `sources.py` (the source
factories and identity queries), `registry.py`, `render.py` (the French lines), and
`probes/graph_projection_lag.py`. Imports allowed: `brain_v42.db`, `brain_v42.repositories`,
`brain_v42.release`, `brain_v42.services.schema_state_service`, the standard library.
`brain_v42.mcp` and `brain_v42.metrics` import `facts`; `facts` never imports them. The
reviewer verified with the layering script that the proposed edges add no cycle; the
`facts → services` edge is a single module and is named in the test that pins it.

## 6. Lot B — claims and verdicts (data model)

### 6.1 `knowledge_claims`: keys and occurrences

A **claim key** is the content identity of a claim: `sha256` of the canonical JSON of
`{statement, fact_name | probe, params, expected, target, definition_version}` — the target
and the definition version are part of it (revision 1 left them out, so a catalogue change
would have silently re-keyed nothing). A **claim occurrence** is a row: the assertion of one
key by one entry, with its own lifecycle.

| Column | Type | Notes |
| --- | --- | --- |
| `id` | uuid PK | the occurrence |
| `seq` | bigint, identity, unique | server insertion order |
| `entity_ref_id` | uuid FK → `brain_entities.id` ON DELETE RESTRICT | the durable anchor; coverage is complete (§4). The claim write path resolves the anchor through the ledger's existing entity upsert inside the same transaction; a knowledge entity cannot be deleted while it has occurrences — the delete paths (`pg_learning.py:184` and its siblings) retire them first, in the same transaction |
| `entity_type` | text CHECK in (`learning`, `decision`, `adr`, `runbook`, `snippet`) | denormalised from the anchor, checked by trigger against it |
| `project_key` | varchar(50) FK → `projects` | denormalised for scope filtering; canonical form |
| `claim_key` | char(64) | content identity, §6.1 |
| `statement` | text, 1–500 chars | for humans |
| `fact_name` | text, nullable | a catalogue name, checked against the registry at write time |
| `probe` | jsonb, nullable | inline probe `{kind, params}` from a closed set of kinds (lot C); exactly one of `fact_name`, `probe` non-null (CHECK) |
| `params` | jsonb object | probe parameters (empty object for catalogue facts) |
| `definition_version` | int | the fact definition the claim was written against |
| `target` | text CHECK in the `FactTarget` values | copied from the catalogue at write time |
| `expected` | jsonb object | `{"path": "/lag_seconds", "op": "lte", "value": 300}` or `{"path": "/lag_seconds", "op": "lte", "policy": "late_after_seconds"}` — `op` in (`eq`, `ne`, `lt`, `lte`, `gt`, `gte`, `in`, `exists`); `regex` is gone; `exists` means "path present with a non-null value" |
| `validity_seconds` | int, nullable | how long a `holds` may be presented as current without a newer reading; `null` → the fact's TTL × 4 |
| `provenance` | text CHECK in (`measured`, `declared`) | whether the author obtained a server verdict at write time |
| `declared_by` | varchar(64) | the `X-Brain-Agent` identity or the human label — **declared** |
| `declared_at`, `recorded_at` | timestamptz | author's instant; server's `now()` |
| `retired_at` | timestamptz, nullable | the only column an UPDATE may touch, NULL → value, once |
| `replaces_id` | uuid FK → `knowledge_claims.id`, nullable | the occurrence this one supersedes (the `SUPERSEDES` material for lot D) |

Constraints: unique partial index `(entity_ref_id, claim_key) WHERE retired_at IS NULL` — the
same key is asserted at most once **at a time** by one entry, and re-asserting after
retirement creates a new occurrence with `replaces_id` set; index `(fact_name) WHERE fact_name
IS NOT NULL AND retired_at IS NULL` for "every live claim on this fact"; index
`(entity_ref_id) WHERE retired_at IS NULL`.

Immutability by SQL, not by code path: a `BEFORE UPDATE` trigger refuses any change other than
`retired_at` from NULL to a value; a `BEFORE DELETE` trigger refuses always. The application
role holds `SELECT, INSERT, UPDATE (retired_at)` and no `DELETE`. Negative SQL tests run as the
application role.

### 6.2 `knowledge_claim_verdicts` — append-only, after `delivery_attestations`, by trigger

| Column | Type | Notes |
| --- | --- | --- |
| `id` | uuid PK | |
| `seq` | bigint, identity, unique | **the order of verdicts**: server insertion order, the only order any read uses |
| `claim_id` | uuid FK → `knowledge_claims.id` ON DELETE RESTRICT | |
| `verdict` | text CHECK in (`holds`, `falsified`, `unreadable`) | tient / falsifié / illisible |
| `reason` | text, nullable | closed vocabulary: `path_absent`, `type_mismatch`, `target_mismatch`, `definition_changed`, `fact_mismatch`, `probe:<error_code>`; NULL for `holds` and `falsified` |
| `measurement` | jsonb object | the `Measurement` of §5.2 verbatim (`status`, `fact`, `definition_version`, `target`, `source`, `value_json` or `error_code`/`where`, `observation_id`, `measured_at`, `digest`) |
| `measurement_digest` | char(64) | the value digest of a `Measured`; NULL for an unreadable |
| `observation_id` | uuid | from the measurement; unique with `claim_id`: one reading yields at most one verdict row per claim |
| `issuer_identity` | varchar(200) | who **asked** for the verdict: `mcp:<X-Brain-Agent>`, `dream:verify:<run_id>`, `human:<label>` — declared |
| `issuer_kind` | text CHECK in (`robot`, `human`) | |
| `idempotency_key` | varchar(200) | issuer-chosen; a replayed request with the same key and the same `measurement` digest returns the existing row; the same key with a different measurement is refused with `idempotency_conflict`, as `pg_delivery_attestations.py:150` does |
| `emitted_at` | timestamptz | the measurement's instant; **refused if later than `now() + 60 s`** |
| `recorded_at` | timestamptz, `now()` | |

Unique `(claim_id, issuer_identity, idempotency_key)`; unique `(claim_id, observation_id)`;
index `(claim_id, seq DESC)`. `BEFORE UPDATE OR DELETE` trigger: refuse always; the application
role holds `SELECT, INSERT` only. Downgrade of the migration is fail-closed and names the
verdict rows it would destroy, after the 047/048 pattern.

### 6.3 The server measures; a caller only names a claim

There is no tool that accepts a measurement JSON. `brain_claim_verify(claim_id)` (lot B) and
the lot C phase both call the same service, which:

1. loads the occurrence and refuses if `retired_at` is set;
2. measures through the registry (`max_age` = the claim's `validity_seconds`, so a fresh cache
   entry is acceptable evidence and a stale one is re-read) — the measurement is therefore
   always **produced by the server**; `issuer_identity` records who asked, and that is all a
   declared identity ever records;
3. checks, before any comparison: `measurement.fact == claim.fact_name` (else `fact_mismatch`),
   `measurement.definition_version == claim.definition_version` (else `definition_changed`),
   `measurement.target == claim.target` and the measurement's source identity equals the
   registry's expected identity for that target (else `target_mismatch`) — each of these is an
   `unreadable` verdict with its reason, never `falsified`;
4. compares (§6.4) and inserts the verdict row.

A claim written with `provenance=measured` gets its first verdict in the same request and the
same transaction as the entry (`brain_learn` and the other writers, §6.5); if the probe is
expensive or unreadable at that moment, the claim is still written and the provenance is
downgraded to `declared` in the response, explicitly.

### 6.4 Verdict computation and the three reads

`compare(expected, measured, policies) -> (verdict, reason)`, pure and tested by table:

1. `Unreadable` → `unreadable`, reason `probe:<error_code>`.
2. `expected.path` is a JSON pointer into `measured.value`; absent → `unreadable`
   (`path_absent`). `exists` is the only op for which absence is a `falsified`.
3. `expected.value` or `expected.policy` (resolved against the descriptor's policies for the
   claim's definition version) gives the right-hand side; a type mismatch between the resolved
   value and the operator's domain → `unreadable` (`type_mismatch`), never `falsified`.
4. Otherwise `holds` or `falsified`.

Three reads, never confused:

- **History**: every row of the claim, by `seq`.
- **Latest attempt**: the row with the greatest `seq`, whatever its verdict.
- **Current validity**, derived at read time and never stored: the last conclusive row
  (`holds` or `falsified`, greatest `seq`) **and** its age against `validity_seconds`. A
  `holds` older than its validity renders `périmé` (stale), not `tient`; a latest attempt that
  is `unreadable` after a conclusive `holds` renders both: `dernier essai illisible (timeout),
  dernier verdict tenant le 2026-09-19`. No UPDATE ever rewrites a verdict to make it current.

### 6.5 Write paths and `claims` PATCH semantics

`brain_learn`, `brain_log_decision`, `brain_propose_adr`, `brain_create_runbook` and
`brain_save_snippet` accept an optional `claims: list[ClaimInput]` (at most 10 per entry),
written in one transaction with the entry.

`brain_update` semantics are explicit and tested one by one:

- `claims` **absent** → the entry's claims are unchanged.
- `claims: []` → every active occurrence of the entry is retired (`retired_at = now()`), with
  the response naming them.
- `claims: [...]` → a **controlled replacement** under compare-and-swap: the request carries
  `expected_active_claim_ids` (the set the caller saw); if the current active set differs, the
  request is refused with `claims_conflict` and nothing changes. Otherwise: an input whose key
  matches an active occurrence keeps it; an input with a new key creates an occurrence whose
  `replaces_id` is the retired occurrence the caller named in `replaces` (optional); active
  occurrences absent from the input are retired.

### 6.6 Read paths

`brain_get` and `brain_search` append to each entry a compact suffix built from the three
reads, French because rendered: `[claims : 2 tiennent · 1 FALSIFIÉ le 2026-09-19
(alembic_head mesuré 054, attendu 053)]`, `[claims : 1 périmé (tenait le 2026-09-01)]`,
`[claims : 1 illisible (cible inattendue)]`, or nothing when the entry has no active claim. The
briefing's recap lines carry the same suffix. A falsified claim changes the ranking of nothing
in lot B.

### 6.7 Constraints from lot C and lot D

- Lot C obtains verdicts only through `brain_claim_verify`, allowlisted for the `verify` phase
  alone, scoped to the phase's project, `issuer_kind=robot`; it never receives a tool that
  accepts a measurement.
- Lot D projects `ASSERTS`, `MEASURED_BY` and `SUPERSEDES` through the existing ledger once
  its catalogue accepts them; until then the claim tables emit no outbox rows.

### 6.8 Migration and delivery constraints

- Number: the next free revision at merge time, 055 or later. The head pin
  (`_REQUIRED_ALEMBIC_HEAD`), the DR contract (v12: tables, FKs, indexes, triggers), the
  documentation contract and the delivery preflight's pins
  (`scripts/check_delivery_deployment.py:1086` and its siblings) move in the same change, after
  runbook `22189c08` ("the seven guards"); the inventory of pins is written before the
  migration is, as a checklist in the pull request.
- Applied in the merge window of its pull request (ADR #17); the writers that use the tables
  ship in the same release. The 2026-09-18 handoff (modes build / prove / cutover / canary /
  rollback) is the procedure: `prove` restores a dump on a throwaway clone, migrates it and
  proves it with the new contract; the private-target attestation is dry-run before the
  window.
- Rollback: the previous release's drop-ins (the data stays, the writers stop touching it);
  `alembic downgrade` is fail-closed, names the rows it would destroy, and needs
  `-x allow_claims_downgrade=yes` — a data protection, not a rollback procedure.

## 7. Verification and delivery

### 7.1 Required automated evidence (lot A)

Unit, no database:

- Canonical recipe: sorted keys and separators; a float refused; NaN refused (as a float);
  NUL and surrogates refused; 4097 bytes refused; depth 9 refused; the digest prefix pinned.
- `Measured` / `Unreadable`: a non-canonical `value_json` refused; a wrong digest refused;
  `value` returns a fresh object (top-level and nested mutation, then re-read); JSON
  serialisation carries `status`.
- Registry: name grammar; duplicate refused; registration after `freeze` refused; a target
  without a source factory or expected identity refused; TTL hit and miss on the monotonic
  clock; the wall clock stepped backwards does not extend a cache entry; `max_age=None`,
  positive shorter than TTL, zero (bypass + join in-flight), negative (`ValueError`); unreadable
  cached `min(ttl, 30 s)` and subject to `max_age`; refresh budget saturation returns
  `refresh_budget`, never a cached value; single flight with twenty readers (one probe run);
  cancelling the first reader and cancelling a follower leave the run alive and the other
  readers served; done-callback identity (an old task's callback does not evict a new one);
  global semaphore never exceeded with direct and batch callers mixed; `queue_timeout` →
  `capacity_timeout` without a probe run; `measure_many` budget → `briefing_budget` for the
  facts not started; `aclose()` during a run → the run ends `Unreadable`, later `measure()`
  raises `RegistryClosedError`; `UnknownFactError`; a value over 4 KiB → `value_too_large`;
  a probe raising → `probe_error` with `where` naming class and function, logged once.
- Source identity: a measured identity that differs from the expected one (database name,
  port) → `target_mismatch`, logged once with name and port only; an identity query failing →
  `identity_unreadable`.
- Renderer: the five cases of §5.7 by table from the declared policy, including exhausted
  rows with `pending == 0`, the generic fallback, the `(cible inattendue)` form and the age
  suffix boundary at 60 s.
- Briefing composer: the legacy call shape renders `tests/fixtures/briefing_full.md` unchanged;
  with one measured fact the line appears after `Schéma`; with an unreadable fact the line
  renders `illisible`; a registry failure never removes the section; the section is built
  through the shared loader without a lifecycle call (the runbook's own proof form,
  `docs/runbooks/2026-09-07-observable-delivery-workflows.md:1948`).
- Tools: `brain_fact_list` order and shape; `brain_fact_get` unknown name → `unknown_fact`;
  `max_age_seconds` forwarded exactly; negative → `invalid_argument`; both profiles (native,
  and compact through `brain_call_tool`) with the provenance middleware installed; the probe
  writes nothing (statement log of the fake session shows `SELECT` only; the fake repository
  records no `access_log` row).
- Composition: the server registers exactly the catalogue of §5.5 (slice 1: one name),
  frozen, with the expected identity derived from settings; `aclose()` is called before
  `engine.dispose()` (order asserted with a recording fake); `test_module_layering` passes with
  the single `facts → services.schema_state_service` edge named; the documentation contract and
  `docs/MCP_TOOLS.md` list the two tools.
- The metrics collector, rewired on `read_projection_state` and `projection_health`, produces
  the same `/metrics` shape as before (existing tests stay green untouched), and its
  `available=false` path still yields its sentinels while the shared query raises.

Integration, against the test database (`tests/integration/db`):

- Empty outbox, armed lease → `lag_seconds == 0`, `healthy is True`.
- One undelivered row created 600 s ago → `lag_seconds >= 600`, loud rendering.
- Lease row with `neo4j_armed_generation <> generation` → `healthy is False`.
- No lease row → `generation is None`, `healthy is False`.
- Lease expired (`leased_until` in the past) → `lease_active is False`; `recovery_id` set →
  `recovery_active is True`; exhausted rows only → loud line with `pending == 0`.
- The identity read on `brain_test` **differs** from the production identity declared in the
  test's registry → `target_mismatch`: the 2026-09-12 failure mode, reproduced and refused.
- The probe issues no statement other than `SELECT` and runs in a read-only transaction (an
  injected `INSERT` in a test probe fails with PostgreSQL's read-only error, not with a mock).
- Shutdown during a running probe: `aclose()` returns, the connection is released, the engine
  disposes cleanly.

### 7.2 Release sequence (lot A)

1. Independent review of the pull request before it is opened, with the tests read by the
   reviewer (rule of the current focus).
2. Merge; build the immutable release and cut over the eight writers with the schema-less path
   (handoff 2026-09-19 script, receipts of 2026-09-13 and 2026-09-19). No migration, so no
   apply window and no dump; the 06:00–13:00 exclusion still applies.
3. Canary on production, in this order: `brain_fact_get("graph_projection_lag",
   max_age_seconds=0)` → `status=measured`, `source_kind=probe`, a `source` naming the
   production database and port; a second call inside 15 s → `source_kind=cache` with the same
   `observation_id` and `digest`; then a session briefing obtained through the shared loader
   renders the `Projection graphe` line; `/metrics` of the sidecar still carries `graph_outbox`
   with its previous keys; the MCP journal shows one `fact.target_mismatch` warning at most —
   zero is the expected count.
4. Receipt under `docs/receipts/` with the measured values of the canary.

Rollback: the previous release's drop-ins; nothing to restore in the database.

## 8. Alternatives considered

- **Read the metrics sidecar from the briefing.** Rejected: an HTTP hop inside a tool call to a
  process with its own failure modes; no fact naming; the sidecar is loopback-only by design
  and is not a read API for agents.
- **Persist facts in a table now.** Rejected for lot A: the ticket says "sans migration", and a
  persisted fact without a claim is a value copied forward by a machine instead of a human.
  The verdict row is the persistence, tied to what it proves.
- **An open catalogue (probes declared in configuration or by tools).** Rejected: an SQL
  string in a configuration key is an injection surface and an unbounded query; a closed
  catalogue in code is reviewed like code, and revision 2 adds that the catalogue cannot even
  hold a fact whose target it cannot verify.
- **Model-judged facts.** Rejected: a verdict must be reproducible from a probe and a verified
  target; prose judgement is a proposal of lot C with a human in the loop.
- **Accept a caller-supplied measurement with a digest.** Rejected (finding 9): a digest proves
  the content of a JSON, not that a probe ran; the server measures or there is no verdict.
- **Five claims tables, one per entity table.** Rejected (answer 5): `brain_entities` already
  anchors every knowledge entity with a lifecycle and coverage is complete; one table with an
  FK and a type check is simpler and keeps lot D's projection to one source.
- **A `last_verdict` column on the claim.** Rejected (finding 2): a mutable "current" is the
  thing that would be rewritten; the three reads of §6.4 are derived from the append-only rows.

## 9. Questions for the second review

1. §5.3 rule 4 makes the expected identity of `production` a value derived from
   `settings.postgres_url` at composition: is deriving it from the same setting the probe's
   connection uses enough, or should the operator declare it independently (a second source
   that would catch a wrong DSN, at the cost of one more private literal to keep true)?
2. §5.4 charges a forced refresh to a per-fact token bucket of 10 per minute per process. Is
   per fact the right granularity, or should the bucket be per caller identity as well?
3. §6.1 puts the `entity_type` CHECK on the claims table and verifies it against the anchor by
   trigger. Is the denormalisation worth the trigger, or should reads join `brain_entities`?
4. §6.4 renders a `holds` older than `validity_seconds` as `périmé`. Should a stale `holds`
   also count as "not holding" for REORG's proposal input, or only for display?
5. §6.5's compare-and-swap uses the set of active occurrence ids. Is that sufficient against a
   concurrent `claims: []` followed by a replacement, or is a per-entity claims revision
   counter (one more column on five tables) needed after all?
6. What, in §5.2–§5.5, still lets the first fact lie rather than say `unreadable`?

## 10. Sources

- ADR #27 `fb9b75bd` (accepted 2026-09-19); ticket `59a5a9c7` (body corrected 2026-09-19);
  ticket `731ab364` (observer diagnostic, resolved 2026-09-19).
- Review of revision 1: `docs/superpowers/specs/2026-09-19-measured-facts-and-claims-review-codex-astra.md`.
- `src/brain_v42/mcp/tools/session_tools.py`; `src/brain_v42/services/schema_state_service.py`;
  `src/brain_v42/services/dream_run_service.py`; `src/brain_v42/metrics/collector.py`;
  `src/brain_v42/repositories/pg_graph_ledger.py`; `src/brain_v42/maintenance/plan_index_repair_store.py`;
  `src/brain_v42/release.py`; `src/brain_v42/models/delivery_hashes.py`;
  `src/brain_v42/db/delivery_tables.py`; `src/brain_v42/repositories/pg_delivery_attestations.py`;
  `src/brain_v42/delivery_observer/transport.py`; `src/brain_v42/mcp/dream_capabilities.py`;
  `src/brain_v42/mcp/tool_catalog.py`; `src/brain_v42/mcp/provenance_middleware.py`;
  `src/brain_v42/mcp/server.py`; `scripts/check_module_layering.py`;
  `scripts/check_delivery_deployment.py`; `alembic/versions/033_graph_relation_ledger.py`.
- `brain_entities` coverage measured 2026-09-20 on production (read-only counts).
- Ticket `416266ec` (projection freeze, measured 2026-09-07); measured recovery state of
  2026-09-19 00:49Z (generation 93 armed, outbox empty).
- `docs/superpowers/specs/2026-09-07-observable-delivery-workflows-design.md` (the shape of
  this document and of `delivery_attestations`); `docs/GRAPH_LEDGER_RUNBOOK.md`.
