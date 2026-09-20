# Measured facts and claims

**Date:** 2026-09-19 — **revision 6 on 2026-09-20** (slice 2 implementation notes, below revision 5). Revision 2 answered the review of
revision 1 (`…-review-codex-astra.md`, verdict REWORK); revision 3 answered the review of
revision 2 (`…-review-2-codex-terra.md`: lot A PATCH_THEN_SHIP, lot B REWORK, ten findings);
revision 4 answered the confirmation review of revision 3 (`…-review-3-codex-terra.md`: lot A
PATCH_THEN_SHIP, lot B REWORK, four findings); revision 5 answers the confirmation review of
revision 4 (`…-review-4-codex-terra.md`: **both lots PATCH_THEN_SHIP**, four findings, all
mechanical — applied below and the review loop closed; the next review reads the pull
request with its tests)

**Status:** proposed specification for ADR #27 (`fb9b75bd`, accepted 2026-09-19); lot A is
specified to implementation depth, lot B to data-model depth. Slice 1 of lot A (§5.5, the
registry, the tools, the briefing line) is merged (PR #158, 2026-09-20) and awaits its release;
slice 2 (§5.6, five facts on three targets) is under implementation on `feat/facts-slice-2`

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

### Revision 3 — the ten findings of the second review

| Finding | Answer | Section |
| --- | --- | --- |
| P0-1 — the expected identity came from the same DSN the probe connects with: self-confirming | the expected identity of `production` is **declared independently by the operator** (`BRAIN_FACTS_PRODUCTION_IDENTITY`; revision 3 used the database OID, revision 4 replaced it with the cluster system identifier and made the address mandatory), never derived from the DSN; absent → no production probe ships; every declared field is compared | §5.3 rule 4 |
| P0-2 — "read-only autocommit" cannot give one snapshot for value and identity | one explicit `session.begin()` + `SET TRANSACTION READ ONLY, ISOLATION LEVEL REPEATABLE READ` covering the probe query, the identity query and the Alembic read, as the precedent does (`plan_index_repair_store.py:222-225`); a real `INSERT` is refused by PostgreSQL in the test | §5.3 |
| P1-3 — a `probe` claim (inline) could never be verified | inline probes leave lot B: `fact_name` is NOT NULL, the `probe` column does not exist; lot C may reintroduce them with server-resolved descriptors | §6.1 |
| P1-4 — a claim's `project_key` could name another project than its anchor | the trigger enforces `entity_type` **and** `project_key` equality with `brain_entities`, and `scope_kind = 'project'` | §6.1 |
| P1-5 — historical claims depended on the mutable current descriptor | policies and the validity bound are **resolved at write time and stored on the occurrence**; verification reads the stored values; a definition bump yields `definition_changed` | §6.1, §6.4 |
| P1-6 — `claims: []` bypassed compare-and-swap | every destructive `claims` mutation, `[]` included, requires `expected_active_claim_ids` | §6.5 |
| P1-7 — replay of an unreadable verdict had nothing to compare | every verdict row stores a `request_fingerprint` over the whole canonical request; replays compare it, unreadable included | §6.2 |
| P2-8 — claim JSON and pointers unbounded | a closed `ClaimInput` validator with byte, depth, pointer and array bounds, refused before any write | §6.5 |
| P2-9 — `measure_many` accepted an unbounded iterable | a bounded, duplicate-free sequence of at most 32 names; the briefing passes `briefing_names()` only | §5.4 |
| P3-10 — the precedent was cited as a mutation transaction | corrected: it is a read-only snapshot transaction | §4 |

Revision 1's §6.1 `replaces_id` and revision 2's optional `replaces` were in tension (finding
3 of the second review, "partially lifted"): revision 3 settles it — a re-assertion after
retirement **must** name its predecessor, and the server refuses a predecessor that is not a
retired occurrence of the same entity (§6.5).

### Revision 4 — the four findings of the confirmation review

| Finding | Answer | Section |
| --- | --- | --- |
| P0 — a database OID is a catalogue identifier, not an instance identity; the address was optional | the declared identity is the **cluster system identifier** (`pg_control_system().system_identifier`, 64-bit, assigned at `initdb`, measured `7612696091383607335` on production on 2026-09-20), the database name, the server address and the port — **all four mandatory, all four compared**; `pg_control_system()` not executable → `identity_unreadable`, no production fact | §5.3 rule 4 |
| P1 — lot D needs the historical fact definition, not only the resolved values | lot B adds an immutable `knowledge_fact_definitions` table keyed by `(fact_name, definition_version)`, written at server start by insert-if-absent and refused on digest drift; claims reference it by FK | §6.0, §6.1, §6.8 |
| P1 — the fingerprint was an outcome fingerprint, computed after measuring | `request_fingerprint` is computed from the immutable inputs **before** measuring and looked up first; the outcome fingerprint is kept separately for audit | §6.2, §6.3 |
| P1 — `replaces` accepted any retired same-key occurrence | the trigger accepts only the **latest** retired occurrence of `(entity, key)` as predecessor: the chain can neither fork nor skip | §6.1, §6.5 |

### Revision 5 — the four findings of the fourth review

| Finding | Answer | Section |
| --- | --- | --- |
| P1 — §5.1 and §5.3 defined the PostgreSQL identity differently; a colon-delimited setting is ambiguous for IPv6 | one typed `SourceIdentity` for PostgreSQL: `system_identifier`, `database`, `server_addr`, `server_port` — the Alembic revision is a **fact**, not an identity field; the setting is a JSON object with four validated keys | §5.1, §5.3 rule 4 |
| P1 — the definition digest had no payload recipe; `value_keys` could not be built without measuring | `Probe.value_schema` is declared on the probe (keys → JSON types); the definition row's digest is the canonical digest (§5.2 recipe, domain `brain-v42-fact-definition:v1`) of `{fact_name, definition_version, target, ttl_seconds, timeout_seconds, policies, value_schema}`, `registered_at` excluded | §5.3, §6.0 |
| P2 — the precedent uses `READ ONLY` only, not `REPEATABLE READ` | citation corrected: the precedent gives the shape (`session.begin()` + `SET TRANSACTION READ ONLY`); this design adds `ISOLATION LEVEL REPEATABLE READ` explicitly | §4, §5.3 |
| P3 — migration 033 backfills seven entity kinds, not five | claims are intentionally limited to the five knowledge kinds; `feature` and `plan` are excluded and the reason is stated | §4, §6.1 |

Answers of the fourth review adopted: a changed container address fails production facts
closed until re-declared, no Compose pin for that reason alone; a definition digest drift
disables **that fact only** (`unreadable (definition_drift)`, its claims refused) with a
high-severity journal event, the server starts (§6.0); a physical clone that keeps all four
identity fields and takes over the endpoint is a documented root-of-trust limit (§5.3).

### Revision 6 — slice 2 implementation notes (2026-09-20)

Slice 1 shipped one target (`production`) and one typed identity. Slice 2 adds the two target
kinds §5.6 needs and settles what §5.3 rule 4 left open for them — "each declared before its
first probe ships":

| Point | Decision | Section |
| --- | --- | --- |
| Identity of `live_release` | `ReleaseIdentity(release_sha, package_version)`. **Measured** from the running process: `release_sha` is the 40-hex segment of `brain_v42.__file__`'s resolved path matching `/releases/<sha>/`, `package_version` is `brain_v42.release.package_version()`. A process that does not run from a release (development, `dev`) has no readable identity — `identity_unreadable`, never a `"dev"` value. **Declared** by the release tooling as `BRAIN_FACTS_LIVE_RELEASE_IDENTITY='{"release_sha":"<40 hex>","package_version":"<version>"}'`, rendered into `brain-mcp-http`'s `90-immutable-release.conf` next to the path it pins — one file, one SHA, no second drop-in to drift. Undeclared → the two `live_release` facts are refused at composition and the briefing says so, exactly as for `production`. | §5.3 rule 4, §5.6 |
| Identity of `host` | `HostIdentity(hostname)`. Measured with `socket.gethostname()`; declared once by the operator as `BRAIN_FACTS_HOST_IDENTITY='{"hostname":"<name>"}'` in `92-facts-identity.conf`, beside the production declaration. | §5.3 rule 4, §5.6 |
| `Measured.source` | the union `Identity = SourceIdentity \| ReleaseIdentity \| HostIdentity`; every identity exposes `as_dict()` (the JSON the tools serve) and `from_mapping()` (the declaration parser, every key mandatory, deep validation in the model); the registry compares whole identities, never a subset. | §5.2, §5.4 |
| `host` reader | `read_text_under(root, relative)`: refuses an absolute or `..` path, a path whose resolved form leaves `root`, a symlink anywhere under `root`, and a file over 64 KiB; returns text and the file's mtime as whole epoch seconds. The `host` source session exposes it and nothing else. | §5.3 rule 1 |
| `alembic_head` | value `{"revision": "<stamped>"}`; a database with no `alembic_version` row raises (unreadable), it is not a value. The briefing line is byte-identical to today's `- Schéma : 054`; when the fact is registered the briefing renders it **instead of** the `SchemaStateService` read, so the section carries one `Schéma` line, now bound to the verified production identity; when it is not registered the legacy read stays. `SchemaStateService.current_revision` and the probe share `read_current_revision(session)`. Unreadable renders `- Schéma : illisible (<code>)`. | §5.6, §5.7 |
| `alembic_head_shipped` | value `{"revision": "<head>"}` from `head_of_versions_strict(directory)`: a missing directory, an unreadable or unparsable revision file, a file over the size bound, or a chain with zero or several heads **raises**; memoised per process. TTL is the process lifetime (declared as 3650 days), timeout 1 s. Line `- Tête Alembic livrée : 054`. | §5.6, §5.7 |
| `live_release_sha` | value `{"release_sha": "<40 hex>", "package_version": "<version>"}` — the measured identity, published as a fact so a claim can name it. TTL process lifetime, timeout 1 s. Line `- Release vivante : b4f7194d (paquet 0.6.0)` (the SHA cut to eight characters in prose only). | §5.6, §5.7 |
| `dream_killswitches_declared` | value: the nine keys `brain_v42.dream_killswitches._KS_KEYS` names (`promote`, `reorg`, `reorg_dry`, `extract`, `extract_dry`, `roadmap`, `roadmap_dry`, `sweep`, `sweep_dry`) each carrying the **raw string** the drop-in holds (`""` when absent — a typo like `True` must stay visible, a boolean would hide it), plus `file_mtime_epoch` (int). Read only through the `host` reader, root `~/.config/systemd/user`, relative `brain-v42-dream.service.d/killswitches.conf`; a missing or unreadable file raises. `briefing=True`, line `- Killswitches déclarés : PROMOTE on, REORG on wet, EXTRACT on wet, ROADMAP off, SWEEP on wet (drop-in modifié le 2026-09-15 14:10 UTC)`; a value neither `true` nor `false` renders raw, `REORG_DRY_RUN='True' (illisible → dry)`. The DB-derived history stays a separate fact. | §5.6, §5.7 |
| `dream_last_night` | value: the aggregate of the latest `run_date` in `dream_runs` — `run_date` (ISO string), `rows`, `done`, `fail`, `timeout`, `partial`, `other` (rows whose status is none of the four), `wet` (`phase_dry_run` false), `dry`, `projects` (distinct `project_key`), `finished_at_epoch` (max `created_at`); no night at all raises. `value_schema` is scalars only, so per-phase detail stays with the Dream tools. `briefing=False`: the briefing already renders the night from the same table. | §5.6 |
| Layering | one more named edge, `facts → brain_v42.dream_killswitches` (a leaf module: the parser of the drop-in, shared with the DB-derived state so the two readers agree on the key table). | §5.10 |
| Catalogue order | `graph_projection_lag`, `alembic_head`, `live_release_sha`, `alembic_head_shipped`, `dream_killswitches_declared`, `dream_last_night`. | §5.7 |

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
| Verified database identity | `plan_index_repair_store.py:222-225,291-298` (`session.begin()` then `SET TRANSACTION READ ONLY`; `current_database()`, `inet_server_addr()`, `inet_server_port()` read inside that read-only transaction) | The precedent for the source identity and for the shape `begin()` + `SET TRANSACTION READ ONLY`; this design **adds** `ISOLATION LEVEL REPEATABLE READ` for a single snapshot, which the precedent does not use (§5.3) |
| ► Shipped Alembic head and package version | `brain_v42.release` (`shipped_alembic_head`, `package_version`, `head_of_versions`) | `package_version` is a distribution version, never a SHA; `head_of_versions` skips unreadable files. §5.6 specifies strict variants |
| ► Model liveness | `scripts/probe_model_liveness.py:146,161` (POST inference, 90 s, verdicts ALIVE/GONE/BUSY/OTHER) | **Leaves lot A**: it calls a model and spends quota. Lot C sonde with an explicit inference budget; BUSY and OTHER map to unreadable |
| Canonical digest recipe | `src/brain_v42/models/delivery_hashes.py:25,70,82` (no floats, string keys, depth ≤ 64, NUL and surrogates refused, domain prefix `brain-delivery-<domain>:v1\n`) | The measurement recipe of §5.2 follows the same rules under its own domain prefix; **integers only** |
| Append-only evidence with idempotency | `delivery_attestations`, `src/brain_v42/db/delivery_tables.py:472`; replay compares content, `pg_delivery_attestations.py:150` | The template of `knowledge_claim_verdicts` (§6.2); ► append-only there is by code path (`054_delivery_attestations.py:11`), here it is by trigger and grant |
| Durable entity anchor with tombstones | `brain_entities` (migration 033, `033_graph_relation_ledger.py:16-24` backfills **seven** kinds: the five knowledge kinds plus `feature` and `plan`): `id`, `entity_type`, `entity_key`, `source_uuid`, `project_key`, `lifecycle` (active/archived/deleted), `revision`, `deleted_at` — coverage measured 2026-09-20: 3815 learnings, 1267 decisions, 119 ADRs, 176 runbooks, 191 snippets, equal to the five knowledge tables; 919 features and 207 plans also anchored | The FK anchor of `knowledge_claims` (§6.1); claims are limited to the five knowledge kinds — a `feature` is on its way out (ADR #25) and a `plan` is an indexed document, not an assertion |
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
  in the same transaction as the value and compared with what the composition root declared for
  the target. One typed `SourceIdentity` per target kind; for PostgreSQL:
  `system_identifier` (`pg_control_system()`), `database` (`current_database()`),
  `server_addr` (`inet_server_addr()`), `server_port` (`inet_server_port()`). The stamped
  Alembic revision is **not** an identity field — it is the fact `alembic_head` and changes
  legitimately. The 2026-09-12 incident that motivates the identity: a probe pointed at `brain_test` (then at 052) would have falsified a true
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
    value_schema: Mapping[str, Literal["int", "bool", "string", "null|int"]]   # every key of the value and its JSON type, declared
    async def measure(self, source: SourceSession) -> Mapping[str, JSON]: ...
```

`value_schema` is declared, not inferred: the registry refuses a measured value whose keys or
types differ from it (`Unreadable(error_code="value_not_canonical", where=...)`), and lot B's
definition rows (§6.0) carry it without measuring anything.

```python
```

The registry, not the probe, opens the source. `SourceSession` is what the composition root
built for the target: for PostgreSQL targets it wraps an `AsyncSession` inside **one explicit
transaction** — `async with session.begin():` then
`SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY` — the shape of the precedent
(`plan_index_repair_store.py:222-225`, which sets `READ ONLY` only) plus the isolation level
this design needs for one snapshot.
The probe's query, the identity query (`pg_control_system().system_identifier`,
`current_database()`, `inet_server_addr()`, `inet_server_port()`) and the `alembic_version`
read all run inside that transaction, so value and identity come from one connection and **one
snapshot**; a probe that tried to write fails with PostgreSQL's read-only error, not with a
mock. The transaction is rolled back on exit; nothing is committed.

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
4. **Verified target, declared independently.** The identity a target must have is **not
   derived from the DSN the probe connects with** — that would let a wrong DSN confirm itself
   (second review, P0-1). For `production` the operator declares it once, as a non-secret
   setting with **four mandatory fields**, a JSON object so an IPv6 address cannot be
   misread (fourth review, P1):
   `BRAIN_FACTS_PRODUCTION_IDENTITY='{"system_identifier": "7612696091383607335", "database": "brain", "server_addr": "172.18.0.2", "server_port": 5432}'`
   — validated key by key at composition (a 64-bit unsigned decimal string, an identifier, an
   IP address literal, a port): the cluster's `pg_control_system().system_identifier` (a 64-bit identifier assigned at
   `initdb`, collision-resistant, kept by a physical restore and changed by a logical restore
   into a fresh cluster — measured `7612696091383607335` on production on 2026-09-20), the
   database name, and the server address and port as PostgreSQL sees the connection
   (`inet_server_addr()`, `inet_server_port()` — the container's own address and port, not the
   published one; `NULL` on a Unix socket, which the declaration may not use). The registry
   compares **all four** after every run; nothing is optional (third review, P0). A mismatch
   is `Unreadable(error_code="target_mismatch")`, logged once at warning level with the four
   measured fields. An identity that could not be read — including `pg_control_system()` not
   executable by the role — is `identity_unreadable`. **When the setting is absent or
   malformed, no `production` probe can be registered** (`UnverifiableTargetError` at
   composition, so the service starts without production facts and says so in the journal
   rather than measuring an unverified database). The settings model loads a malformed declaration without
   refusing it — the refusal happens when the composition root reads it, so a typo in a
   drop-in degrades the catalogue and never takes the service down (implementation note,
   2026-09-20); the briefing then names the fact that was not registered and why. The DR runbook gains the line: a logical
   restore into a new cluster, or a container recreated on another address, changes the
   identity and the operator re-declares it — the facts fail closed until then, visibly.
   **Root-of-trust limit, stated:** a physical clone of the cluster that keeps all four fields
   and takes over the endpoint is indistinguishable from the original by this check; detecting
   that is deployment control's job, not a probe's. The same rule holds for every other target
   kind: `host` and `live_release` identities are
   `hostname` (declared in `BRAIN_FACTS_HOST_IDENTITY`) + release SHA parsed from
   `brain_v42.__file__` (`releases/<sha>/`) + `package_version()`; `github` identity is the API
   origin plus the authenticated principal returned by the credential's own endpoint;
   `provider` identity is the endpoint origin plus the `/healthz` body's identity fields — each
   declared before its first probe ships. Slice 1 needs only `production`.
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
    async def measure_many(self, names: Sequence[str], *, max_age: timedelta | None = None,
                           budget: timedelta | None = None) -> dict[str, Measurement]
        # at most 32 names, no duplicates, every name registered — else ValueError before any task
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
- **Refresh budget.** Two token buckets per process: one per fact (`per_fact_per_minute`,
  10 by default) and one per **target** (`per_target_per_minute`, 30 by default — the shared
  database pays for every fact on it), both charged by every probe run a caller forced
  (`max_age` shorter than the TTL). Saturation of either returns
  `Unreadable(error_code="refresh_budget")` immediately — **never a cached value presented as
  fresh**. Buckets are never keyed by a caller header: `X-Brain-Agent` is declared and
  spoofable. The briefing's own reads use `max_age=None` and are not charged.
- **Bounded batches.** `measure_many` takes a sequence of at most 32 distinct registered
  names and refuses anything else with `ValueError` before creating a task; the briefing
  passes `briefing_names()` and nothing a caller supplied.
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
  `Unreadable(error_code="briefing_budget")`. The briefing passes a 4 s budget. Runs already
  admitted complete, so the honest bound is "the briefing adds at most the budget plus one
  deadline (queue wait + timeout)", about 9 s with the slice-1 defaults — not "the slowest
  probe", and not the budget alone: with more facts than slots there are waves, and revision
  1's claim was false (implementation note, 2026-09-20).
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
| `alembic_head` | production | 60 s / 3 s | `SchemaStateService.current_revision` inside a `SourceSession` (the shared `read_current_revision(session)`); value `{"revision"}`, no row → unreadable; the briefing's `Schéma` line comes from this fact when it is registered (revision 6) | 2 |
| `alembic_head_shipped` | live_release | process lifetime / 1 s | a **strict** `head_of_versions`: any unreadable or unparsable revision file → raise (today's version skips it and may return an older head, `release.py:90`); memoised once per process; the identity is `ReleaseIdentity` (revision 6) | 2 |
| `live_release_sha` | live_release | process lifetime / 1 s | parsed from the running package's own path (`releases/<sha>/`); `unreadable` when the process does not run from a release (development), never `"dev"` as a value; value `{"release_sha", "package_version"}` (revision 6) | 2 |
| `dream_killswitches_declared` | host | 60 s / 1 s | reads **only** the drop-in file through the restricted reader and returns the raw flag strings plus the file's mtime (revision 6); a missing or unreadable file is `unreadable`, never a set of disabled flags; the DB-derived history (`last_run_date`, clean dry nights) is a **separate** fact `dream_last_night` on `production`, so "declared on disk" and "what the last night did" never blend into one value as `killswitch_state` blends them today (`dream_run_service.py:29,113,136`) | 2 |
| `dream_last_night` | production | 60 s / 3 s | `dream_runs` of the latest `run_date`: the night's aggregate by status and dry flag (revision 6 — scalars only, per-phase detail stays with the Dream tools); absence of any night is `unreadable`, not "all disabled" | 2 |
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
`brain_v42.release`, `brain_v42.services.schema_state_service`, `brain_v42.config` (for the
declared identities), `brain_v42.dream_killswitches` (slice 2, the drop-in parser), the
standard library.
`brain_v42.mcp` and `brain_v42.metrics` import `facts`; `facts` never imports them. The
reviewer verified with the layering script that the proposed edges add no cycle; the
`facts → services` edge is a single module and is named in the test that pins it.

## 6. Lot B — claims and verdicts (data model)

### 6.0 `knowledge_fact_definitions`: the immutable history of the catalogue

Lot A keeps one live definition per fact in code. Lot B's claims and lot D's nodes need the
definition a claim was written against to stay readable after the catalogue moves (third
review, P1). The table is written by the **server at start**, never by a caller:

| Column | Type | Notes |
| --- | --- | --- |
| `fact_name`, `definition_version` | text, int — PK | |
| `target` | text CHECK in the `FactTarget` values | |
| `ttl_seconds`, `timeout_seconds` | int | |
| `policies` | jsonb object | the declared policies of that version |
| `value_schema` | jsonb object | the probe's declared `value_schema` (§5.3), so a stored `expected.path` can be checked against the shape it was written for |
| `digest` | char(64) | canonical digest (§5.2 recipe, domain prefix `brain-v42-fact-definition:v1\n`) of `{fact_name, definition_version, target, ttl_seconds, timeout_seconds, policies, value_schema}` — `registered_at` is **excluded** |
| `registered_at` | timestamptz, `now()` | first time this version was seen; not part of the digest |

At start, for every registered probe, the server inserts the row if absent; if a row exists
for `(fact_name, definition_version)` with a **different digest**, the server starts but
**that fact alone** is disabled — every read answers `Unreadable(error_code="definition_drift")`,
its claims refuse new verdicts with reason `definition_drift`, and a high-severity journal
event names the fact (fourth review, answer 2): a definition changed without a version bump is
the drift this design exists to make impossible, and one drifting fact must not take the
briefing's other facts down with it. `BEFORE UPDATE OR DELETE` trigger: refuse always; the
application role holds `SELECT, INSERT` only. Claims reference it by FK
`(fact_name, definition_version)`; lot D projects its rows as the `fact` nodes.

### 6.1 `knowledge_claims`: keys and occurrences

A **claim key** is the content identity of a claim: `sha256` of the canonical JSON of
`{statement, fact_name, expected, target, definition_version}` — the target and the definition
version are part of it (revision 1 left them out, so a catalogue change would have silently
re-keyed nothing). A **claim occurrence** is a row: the assertion of one
key by one entry, with its own lifecycle.

| Column | Type | Notes |
| --- | --- | --- |
| `id` | uuid PK | the occurrence |
| `seq` | bigint, identity, unique | server insertion order |
| `entity_ref_id` | uuid FK → `brain_entities.id` ON DELETE RESTRICT | the durable anchor; coverage is complete (§4). The claim write path resolves the anchor through the ledger's existing entity upsert inside the same transaction; a knowledge entity cannot be deleted while it has occurrences — the delete paths (`pg_learning.py:184` and its siblings) retire them first, in the same transaction |
| `entity_type` | text CHECK in (`learning`, `decision`, `adr`, `runbook`, `snippet`) | denormalised from the anchor; a `BEFORE INSERT` trigger refuses a row whose `entity_type` or `project_key` differs from the anchor's, or whose anchor is not `scope_kind = 'project'` and `lifecycle = 'active'` (second review, P1-4) |
| `project_key` | varchar(50) FK → `projects` | denormalised for scope filtering; canonical form; equal to the anchor's by trigger |
| `claim_key` | char(64) | content identity, §6.1 |
| `statement` | text, 1–500 chars | for humans |
| `fact_name` | text NOT NULL | a catalogue name, checked against the registry at write time. **No inline probes in lot B** (second review, P1-3): a claim that the registry cannot measure cannot be written; lot C may add inline probes with server-resolved, versioned descriptors |
| `definition_version` | int | the fact definition the claim was written against; FK `(fact_name, definition_version)` → `knowledge_fact_definitions` |
| `target` | text CHECK in the `FactTarget` values | copied from the catalogue at write time; equal to the definition's by trigger |
| `expected` | jsonb object, bounded (§6.5) | `{"path": "/lag_seconds", "op": "lte", "value": 300}` or `{"path": "/lag_seconds", "op": "lte", "policy": "late_after_seconds"}` — `op` in (`eq`, `ne`, `lt`, `lte`, `gt`, `gte`, `in`, `exists`); `regex` is gone; `exists` means "path present with a non-null value" |
| `expected_resolved` | jsonb object | the `expected` with any `policy` reference **resolved at write time** from the descriptor of `definition_version`; verification reads this column, never the live descriptor (second review, P1-5) |
| `validity_seconds` | int NOT NULL | how long a `holds` may be presented as current without a newer reading; materialised at write time from the input or from the descriptor's TTL × 4, never resolved later |
| `provenance` | text CHECK in (`measured`, `declared`) | whether the author obtained a server verdict at write time |
| `declared_by` | varchar(64) | the `X-Brain-Agent` identity or the human label — **declared** |
| `declared_at`, `recorded_at` | timestamptz | author's instant; server's `now()` |
| `retired_at` | timestamptz, nullable | the only column an UPDATE may touch, NULL → value, once |
| `replaces_id` | uuid FK → `knowledge_claims.id`, nullable | the occurrence this one supersedes (the `SUPERSEDES` material for lot D); **required** when the same key was previously asserted by the same entity and retired, and it must then be **the latest retired occurrence** of `(entity_ref_id, claim_key)` — the one with the greatest `seq` — enforced by trigger, so a chain can neither fork nor skip (third review, P1) |

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
| `request_fingerprint` | char(64) | `sha256` of the canonical JSON of the **immutable inputs** `{claim_id, issuer_identity, idempotency_key, expected_resolved, definition_version, validity_seconds}` — computable by a retrying caller **before** any measurement (third review, P1) |
| `outcome_fingerprint` | char(64) | `sha256` of the canonical JSON of `{verdict, reason, measurement}` — audit only, never part of idempotency |
| `idempotency_key` | varchar(200) | issuer-chosen; the service looks up `(claim_id, issuer_identity, idempotency_key)` **first**: found with the same `request_fingerprint` → the existing row is returned and nothing is measured; found with a different one → `idempotency_conflict`, as `pg_delivery_attestations.py:150` does; absent → measure, then insert. Unreadable rows replay exactly like the others |
| `emitted_at` | timestamptz | the measurement's instant; **refused if later than `now() + 60 s`** |
| `recorded_at` | timestamptz, `now()` | |

Unique `(claim_id, issuer_identity, idempotency_key)`; unique `(claim_id, observation_id)`;
index `(claim_id, seq DESC)`. `BEFORE UPDATE OR DELETE` trigger: refuse always; the application
role holds `SELECT, INSERT` only. Downgrade of the migration is fail-closed and names the
verdict rows it would destroy, after the 047/048 pattern.

### 6.3 The server measures; a caller only names a claim

There is no tool that accepts a measurement JSON. `brain_claim_verify(claim_id)` (lot B) and
the lot C phase both call the same service, which:

1. loads the occurrence and refuses if `retired_at` is set; computes the `request_fingerprint`
   and resolves the idempotency lookup of §6.2 — a replay returns here, before any probe runs;
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
3. `expected_resolved` — resolved and stored at write time, never re-resolved against the
   live descriptor — gives the right-hand side; a type mismatch between it and the operator's
   domain → `unreadable` (`type_mismatch`), never `falsified`.
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
  the response naming them — **under the same compare-and-swap as a replacement**: the request
  carries `expected_active_claim_ids`, and a set that differs refuses the retirement with
  `claims_conflict` (second review, P1-6: an empty list is the most destructive mutation, not
  an exception to the guard).
- `claims: [...]` → a **controlled replacement** under compare-and-swap: `expected_active_claim_ids`
  must equal the current active set or the request is refused with `claims_conflict` and
  nothing changes. Otherwise: an input whose key matches an active occurrence keeps it; an
  input with a new key creates an occurrence — and when that key was asserted before by this
  entity and retired, the input **must** name the retired occurrence in `replaces`, which
  becomes `replaces_id` (refused otherwise, `replacement_required`); active occurrences absent
  from the input are retired.

Every input passes a closed `ClaimInput` validator before any write (second review, P2-8):
`statement` 1–500 characters; `expected` ≤ 1024 bytes of canonical JSON, depth ≤ 4; `path` a
JSON pointer of ≤ 8 segments of ≤ 64 characters each, `~0`/`~1` escapes honoured; `value` a
scalar or an array of ≤ 32 scalars; `in` requires an array; `policy` must name a policy of the
descriptor; `validity_seconds` between 60 and 31 536 000; at most 10 inputs per entry. A
refused input names the rule; nothing is written.

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

- Tables of the migration: `knowledge_fact_definitions`, `knowledge_claims`,
  `knowledge_claim_verdicts`, their triggers, grants and the `knowledge_claim_current` view.
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
  test's registry (system identifier, database name, address, port — declared by the test, not
  derived from the DSN) → `target_mismatch`: the 2026-09-12 failure mode, reproduced and
  refused; a registry built without the declared identity refuses the production probe at
  registration. Collision cases by table, on the comparison function: same cluster and port
  with another database name (`brain_test_optout` exists on the same server), same name and
  port on another cluster (system identifier differs), same everything on another address —
  each `target_mismatch`; `pg_control_system()` revoked from the role → `identity_unreadable`.
- Value, identity and Alembic head are read in **one** `REPEATABLE READ` read-only
  transaction: a row inserted by a second connection between the probe's query and the identity
  query is invisible to the probe (snapshot proof); an injected `INSERT` in a test probe fails
  with PostgreSQL's `cannot execute INSERT in a read-only transaction`, not with a mock.
- `brain_session_start` and `brain_session_resume` both render the fact line through the
  shared loader; a Dream-scoped principal calling `brain_fact_get` is refused by the phase
  allowlist (no phase lists it in slice 1).
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

## 9. Open questions carried to the pull request

The six questions of revision 2 were answered by the second review and the answers are
adopted: the expected identity is declared independently (§5.3); buckets are per fact and per
target, never per caller header (§5.4); `entity_type` stays denormalised with a trigger that
also enforces the project (§6.1); a stale `holds` is neither holding nor falsified — it may
request a re-measurement and never drives archiving (§6.4); the active-set compare-and-swap
covers `[]` and no revision counter is added (§6.5); mismatch and the inability to obtain one
read-only observation transaction fail closed (§5.3).

The three questions of revision 3 were answered by the confirmation review and adopted:
the cluster system identifier replaces the OID and all four identity fields are mandatory
(§5.3); `knowledge_fact_definitions` keeps the historical definitions for lot D (§6.0); the
collision cases are tested by table (§7.1).

The three questions of revision 4 were answered by the fourth review and adopted (revision
5 table above). The review loop on the document closes here: four independent readings
(Astra, then Terra three times) took the findings from eleven to four, the last four
mechanical. The next independent reading is of the pull request of lot A, with its tests,
under the rule of the current focus.

1. Whether the `brain` role should keep superuser rights (measured `rolsuper = t` on
   2026-09-20) is outside this design but decides how `pg_control_system()` is granted; the
   design assumes an explicit `GRANT EXECUTE` to a non-superuser role.
2. Whether the metrics collector should expose the fact's `source` identity in `/metrics` as
   well, so the sidecar and the registry can be compared from outside.

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
