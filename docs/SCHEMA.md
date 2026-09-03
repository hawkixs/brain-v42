# Data Schema — brain_v42

**Delivery status:** the repository's target is 052. Revision 052 adds `access_log_daily`, the durable access journal (ticket b93e32be). `access_log` is a QUEUE — drained every 300 s, its actor folded into a boolean by `is_human_actor()` and its rows deleted — so `access_count_human` and `last_accessed_at_human` are decided ONCE and can never be recomputed if the human/machine rule changes. Measured 2026-09-03: 1 019 learnings carry a human count for ZERO surviving source events, against 754 on 2026-08-16; ~590 access events a day. The new table keeps the ACTOR STRING per `(entity_type, entity_id, actor, day)`, written by `pg_access_log.aggregate_in_session` inside the SAME transaction and BEFORE the queue's DELETE, which stays (ADR #21 keeps `access_log` transient). `count` accumulates and `last_accessed_at` only moves forward. NOT NULL throughout and no sentinel invented: `access_log.actor` is itself NOT NULL. **Fail-closed** downgrade naming the days it would destroy; opt-in `-x allow_access_log_daily_downgrade=yes`. Revision 051 adds `brain_session_checkpoints` (M-C), an append-only ledger of session checkpoints made idempotent by the KEY — `UNIQUE(session_id, seq)` with `ON CONFLICT DO NOTHING` — rather than by a CAS, because an agent retry is the norm and a CAS turns every retry into a conflict to be handled. A `BEFORE UPDATE OR DELETE` trigger refuses every rewrite, and that totality is bought by the FK's `ON DELETE RESTRICT`: a session carrying checkpoints becomes INDELIBLE, which is what append-only costs once the data enforces it instead of the code declaring it. Revision 050 adds `project_focus_history` (M-D), the append-only audit trail of project focus, plus the deferred constraint trigger that requires a history row at COMMIT — `brain_set_project_context` rewrites `current_focus` with no CAS, **including to NULL when the argument is omitted**, and until 050 the previous prose was not merely overwritten but UNRECOVERABLE. That trigger ships DISABLED, because between the `upgrade` and the MCP restart the live process still runs pre-050 code that writes no history row; arming it is a named operator gesture (`ALTER TABLE project_contexts ENABLE TRIGGER project_contexts_focus_history_required`). Both downgrades are fail-closed and NAME what they would destroy. Revision 049 carries three objects from the same family (nullable columns + widened CHECK), grouped under criterion (c) of decision 9d22bc6a — their downgrades fail independently, each behind its own named opt-in: `dream_runs.closed_inactive_count` (the night-by-night series of inactivity closures, distinct from abandonments), `dream_runs.thinking_tokens` (the agy rail was generating ~38% of tokens counted nowhere), and the `freshness_source` vocabulary widened with `manual_update` and `plan_reindex` on the six decay-tracked tables — the plan upsert now declares its provenance. Nullable, no default, no backfill (`NULL` = written before 049). Revision 048 adds
`brain_session_artifacts.attribution_mode`: it says BY WHICH KEY a row was attributed
— `explicit`, `derived_deposit`, `derived_connection` or `derived_window`. Nullable and with no
backfill (`NULL` = written before 048); a partial index on the DERIVED mode only, because
undoing a guess must be a query, not a scan; **fail-closed** downgrade that
counts AND NAMES the derived attributions it would make indistinguishable from an
explicit capture. Revision 047 removes the closure XOR
— "non-empty ledger XOR `nothing_to_capture_reason`" — from the `ended` branch of the
`brain_sessions_terminal_state_valid` CHECK. That check measured "did the client DECLARE"; the
derived capture would from now on feed its own signal from the server, and a check is hollow
as soon as the object it checks can influence its own signal. Above all, it made unclosable any
session whose ledger the server had filled. What remains: `summary` and `next_focus` non-
blank, and a reason that says something IF one is given. Its downgrade is fail-closed
and NAMES the closures it would destroy. Revision 046 gives sessions their
identity (`connection_id` + PARTIAL UNIQUE index, `started_by_actor`, `intent`, `nature`) and
the `closed_inactive` terminal state. Revision 045 widens `dream_runs.model` from
`varchar(30)` to `varchar(120)`: two of the five configured phase models did not fit, including
the WET fallback that was **already configured** (`nvidia/nemotron-3-super-120b-a12b`, 33 char.), and an overflow
made the entire ROW get lost — the `INSERT` is best-effort. It was applied in production on
16 August 2026 and **measured** at `045` right after: column at 120, `codex_dream_run_v1` view recreated
(it blocks the `ALTER` as long as it projects the column) with its `GRANT SELECT` to `codex_ro`
reapplied, and 32 tables unchanged. Revision 038 adds Dream ticket
extraction attempts, revision 039 isolates the timestamp trigger of
`project_contexts`, revision 040 adds `project_contexts.focus_updated_at`, revision 041
separates provenance from content (`access_log.actor`, `access_count_human`,
`content_updated_at`), revision 042 adds `dream_runs.project_key`, and revision 043 dates the freshness STATUS (`freshness_status_updated_at` + `freshness_source`) on the six tables tracked by decay. 043 was applied in production on 10 August 2026 and measured at `043` right after, with no backfill — zero dated rows. 042 was applied on 8 August 2026, dream
stopped, and production **measured** at `042` right after — 864 rows preserved, all with
`project_key IS NULL`, and 32 tables unchanged. No page of this repository proves a deployed head: no page of this repository proves a deployed head:
`select version_num from alembic_version`. This line claimed 037 for three days after the
cutover.

## PostgreSQL + pgvector — Tables

### pgvector Extension

```sql
CREATE EXTENSION IF NOT EXISTS vector;
```

The SQLAlchemy `METADATA` registry declares 33 tables, including the six graph
foundation tables below. A fresh schema at head 052 contains 35 `public` tables counting
`alembic_version`, which stays outside `METADATA`. Migrations 040 to 044 only add
columns and 045 adds none — it widens an existing column — so the count held at 32 from
038 through 049; 050, 051 and 052 each add exactly ONE table, and that is what moves it.
Measured on `brain` on 2026-09-03, right after the 049→051 cutover: 34 `public` base
tables — that measurement PREDATES 052, which is delivered but NOT applied to `brain`. The 35
above is what a fresh schema at head 052 contains; it is not a reading of production
tables (`select count(*) from information_schema.tables where table_schema='public' and
table_type='BASE TABLE'`). Re-measure it rather than copying this line. Migration 036 also maintains
ten `codex_*` views in total: nine new views and `codex_brain_entity_v1`, created in
024 then replaced in 036.

Migration 037 declares `down_revision = "036"`. The v4 lifecycle it carries has been running in
production since 24 July 2026, after sequential application of 036 then 037 and explicit
proof before the MCP restart. Revision 052 is the head of the repository. Revision 038 adds
`ticket_extraction_attempts`, 039 isolates the timestamp trigger of `project_contexts`, 040
adds `focus_updated_at`, and 041 adds the provenance columns — none of the four adds a
table after 038. The inventory distinguishes
the schema defined in the repository from the deployed state; any new environment or restore must
prove its own revision, and production itself must be measured, never copied.

## Canonical graph foundation (migrations 033–035)

Migration 033 creates five tables to reconstruct the graph from PostgreSQL. Migration
034 adds `graph_projection_leases`, which carries the projector's durable leadership and
fencing generation. Migration 035 extends this singleton with a resumable recovery
interlock. The foundation therefore always counts six tables. The business tables
remain the source of content; `brain_entities` projects their identity and lifecycle,
`entity_relations` carries the relational facts, and `graph_outbox` feeds Neo4j.

Installing the schema does not by itself activate this path. Production uses the canonical
ledger with `GRAPH_LEDGER_WRITE_ENABLED=true` since the 22 July cutover; its Alembic head
has since advanced and must be measured, it is not read here. The restore and graph rebuild at
head 035 remain historical proofs. The DR-v5 run `20260724_150315` renews the PostgreSQL
gate at head 037 with 24/24
checks; it proves neither the replay of roles, owners and ACLs, nor a fresh Neo4j rebuild,
nor the off-host encrypted copy.

### Table `projects`

```sql
CREATE TABLE projects (
    project_key VARCHAR(50) PRIMARY KEY,
    display_name VARCHAR(200),
    registry_status VARCHAR(16) NOT NULL DEFAULT 'unclaimed'
        CHECK (registry_status IN ('claimed', 'unclaimed', 'archived')),
    source VARCHAR(16) NOT NULL DEFAULT 'reference'
        CHECK (source IN ('context', 'reference', 'manual')),
    metadata JSONB NOT NULL DEFAULT '{}',
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CHECK (project_key ~ '^[a-z0-9]+([:-][a-z0-9]+)*$')
);
```

A project context creates a `claimed` entry; a plain reference creates an
`unclaimed` entry. Deleting the context moves the project back to `unclaimed/reference`, clears
its graph entity's `source_uuid`, and preserves that projectable identity.

### Table `project_aliases`

```sql
CREATE TABLE project_aliases (
    alias_key VARCHAR(128) PRIMARY KEY,
    project_key VARCHAR(50) NOT NULL
        REFERENCES projects(project_key) ON DELETE CASCADE,
    source VARCHAR(16) NOT NULL DEFAULT 'manual',
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX idx_project_aliases_project_key ON project_aliases(project_key);
```

The migration records known historical aliases, for example `brain_v42` →
`brain-v42` and `auto_discord` → `auto-discord`. It normalizes the project columns and
`project_contexts.related_projects` during the upgrade, then triggers apply the same
rule to subsequent writes. The downgrade does not restore the old spellings.

### Table `brain_entities`

```sql
CREATE TABLE brain_entities (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    entity_type VARCHAR(32) NOT NULL,
    entity_key TEXT NOT NULL,
    source_uuid UUID,
    project_key VARCHAR(50) REFERENCES projects(project_key) ON DELETE RESTRICT,
    scope_kind VARCHAR(16) NOT NULL,
    display_label TEXT,
    lifecycle VARCHAR(16) NOT NULL DEFAULT 'active'
        CHECK (lifecycle IN ('active', 'archived', 'deleted')),
    revision BIGINT NOT NULL DEFAULT 1,
    metadata JSONB NOT NULL DEFAULT '{}',
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    deleted_at TIMESTAMPTZ,
    UNIQUE (entity_type, entity_key),
    CHECK (
        (scope_kind = 'global' AND project_key IS NULL)
        OR (scope_kind = 'project' AND project_key IS NOT NULL)
    )
);

CREATE UNIQUE INDEX uq_brain_entities_source_uuid
    ON brain_entities(source_uuid) WHERE source_uuid IS NOT NULL;
CREATE INDEX idx_brain_entities_project_lifecycle
    ON brain_entities(project_key, lifecycle);
CREATE INDEX idx_brain_entities_type_lifecycle
    ON brain_entities(entity_type, lifecycle);
```

The registry covers Projects, the nine allowed Domains, and the Decision,
Learning, Snippet, Runbook, ADR, Feature and Plan entities. The business rows' UUIDs serve as
`source_uuid`; Project and Domain use their business key as `entity_key`. An `archived`
entity stays projected to preserve lineages. Only `deleted` requires its removal
in Neo4j.

### Table `entity_relations`

```sql
CREATE TABLE entity_relations (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    source_entity_id UUID NOT NULL
        REFERENCES brain_entities(id) ON DELETE RESTRICT,
    target_entity_id UUID NOT NULL
        REFERENCES brain_entities(id) ON DELETE RESTRICT,
    relation_type VARCHAR(32) NOT NULL CHECK (relation_type IN (
        'SUPERSEDES', 'MOTIVATED_BY', 'IMPLEMENTS', 'DOCUMENTS', 'USES',
        'RELATED_TO', 'CONTAINS', 'DEPENDS_ON', 'BELONGS_TO',
        'MERGED_INTO', 'BELONGS_TO_DOMAIN'
    )),
    origin VARCHAR(64) NOT NULL,
    origin_ref TEXT,
    confidence DOUBLE PRECISION CHECK (confidence >= 0.0 AND confidence <= 1.0),
    properties JSONB NOT NULL DEFAULT '{}',
    lifecycle VARCHAR(16) NOT NULL DEFAULT 'active'
        CHECK (lifecycle IN ('active', 'archived', 'deleted')),
    revision BIGINT NOT NULL DEFAULT 1,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    deleted_at TIMESTAMPTZ,
    UNIQUE (source_entity_id, target_entity_id, relation_type),
    CHECK (source_entity_id <> target_entity_id)
);
```

`RELATED_TO` relations receive a stable orientation so they stay unique. The
projectable properties are limited to `similarity`, `score`, `threshold`, `model`,
`model_version` and `method`; no free-form content or secret enters the outbox. A
material change increments `revision`.

### Table `graph_outbox`

```sql
CREATE TABLE graph_outbox (
    id BIGSERIAL PRIMARY KEY,
    event_id UUID NOT NULL DEFAULT gen_random_uuid() UNIQUE,
    entity_id UUID REFERENCES brain_entities(id) ON DELETE CASCADE,
    relation_id UUID REFERENCES entity_relations(id) ON DELETE CASCADE,
    aggregate_revision BIGINT NOT NULL,
    operation VARCHAR(16) NOT NULL CHECK (operation IN (
        'upsert_entity', 'delete_entity', 'upsert_relation', 'delete_relation'
    )),
    attempt_count INTEGER NOT NULL DEFAULT 0,
    available_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    leased_until TIMESTAMPTZ,
    lease_owner VARCHAR(128),
    lease_generation BIGINT,
    claim_version BIGINT NOT NULL DEFAULT 0,
    delivered_at TIMESTAMPTZ,
    last_error_code VARCHAR(100),
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (entity_id, aggregate_revision),
    UNIQUE (relation_id, aggregate_revision),
    CHECK (
        (entity_id IS NOT NULL AND relation_id IS NULL)
        OR (entity_id IS NULL AND relation_id IS NOT NULL)
    )
);

CREATE INDEX idx_graph_outbox_pending ON graph_outbox(available_at, id)
    WHERE delivered_at IS NULL;
```

The projector claims batches with `FOR UPDATE SKIP LOCKED` only under live
PostgreSQL leadership and while armed for Neo4j. Nominal selection prevents an eligible
revision from overtaking an earlier revision that is still pending. Each claim records
`lease_owner` and `lease_generation`, increments `claim_version`, and sets `leased_until`.
Renewal, acknowledgment and failure all require the same owner, the same generation, the
same claim version, and leases that are still valid. A failure releases the lease, applies an
exponential backoff capped at 300 seconds, and stores a bounded code. After
`GRAPH_OUTBOX_MAX_ATTEMPTS`, `last_error_code='max_attempts'` and
`available_at='infinity'` isolate the event. If a more recent revision of the same aggregate
is later projected successfully, the older terminal revisions are acknowledged with
`last_error_code='superseded'`; they therefore do not artificially keep the
`exhausted` counter above zero. If the configured limit is lowered, the next claim under a
live leader also normalizes events whose `attempt_count` already exceeds this new
limit to that same terminal state.

### Table `graph_projection_leases` (created in 034, extended in 035)

```sql
CREATE TABLE graph_projection_leases (
    slot VARCHAR(32) PRIMARY KEY,
    protocol_version INTEGER NOT NULL DEFAULT 2,
    generation BIGINT NOT NULL DEFAULT 0,
    owner VARCHAR(128),
    leased_until TIMESTAMPTZ,
    neo4j_armed_generation BIGINT,
    recovery_id UUID,
    recovery_phase VARCHAR(16) NOT NULL DEFAULT 'idle',
    last_completed_recovery_id UUID,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT graph_projection_leases_protocol_valid
        CHECK (protocol_version = 2),
    CONSTRAINT graph_projection_leases_armed_generation_valid
        CHECK (
            neo4j_armed_generation IS NULL
            OR neo4j_armed_generation = generation
        ),
    CONSTRAINT graph_projection_leases_recovery_state_valid
        CHECK (
            (
                recovery_id IS NULL
                AND recovery_phase = 'idle'
            )
            OR (
                recovery_id IS NOT NULL
                AND recovery_id IS DISTINCT FROM last_completed_recovery_id
                AND owner IS NOT NULL
                AND leased_until IS NOT NULL
                AND (
                    (
                        recovery_phase = 'prepared'
                        AND neo4j_armed_generation IS NULL
                    )
                    OR (
                        recovery_phase = 'neo_ready'
                        AND neo4j_armed_generation IS NOT NULL
                        AND neo4j_armed_generation = generation
                    )
                )
            )
        )
);
```

The runtime uses the `slot='neo4j'` row as its singleton; the primary key guarantees its
uniqueness. Migration 034 initializes it with protocol 2, generation `0` and armed
generation `0`. It also adds the two claim columns to `graph_outbox`, then
clears `lease_owner`, `leased_until` and `lease_generation` on undelivered events.
Migration 035 adds the three recovery columns and their constraint. Its downgrade only
removes them; the 034 downgrade then drops the table and the claim columns, without
restoring the lease coordinates the upgrade had cleared.

Protocol v2 enforces the following invariants:

- **Acquire.** The same owner keeps its generation and its armed state as long as its lease is alive.
  After expiry or in the absence of an owner, acquisition increments an unarmed generation,
  reuses an unarmed generation, and starts unarmed.
- **Arm.** After the generation's durable activation in Neo4j, PostgreSQL arms only
  the exact `(owner, generation)` tuple if its lease is still alive and the protocol equals `2`.
- **Claim.** Only the live, armed tuple can claim an event. Another generation
  can immediately take over an old claim, even if its expiry is in the future, and then
  increments `claim_version`. Renew, ACK and fail validate the owner, the generation, the
  claim version and both expiries; a stale validation does not modify the event.
- **Release.** PostgreSQL releases only the exact live tuple. It clears `owner` and
  `leased_until`, but keeps `neo4j_armed_generation`, so that the next acquisition
  increments the generation.

The projector normally keeps the armed tuple between polls; it calls Release on its
shutdown or after invalidation of a fence, a claim, or a CAS. Its lease covers at least
two poll intervals to avoid a generation change caused by configuration alone.

Normal Neo4j activation requires an existing protocol-2 fence with no `recovery_id`. It
accepts the exact armed tuple or, during arming, the immediately preceding generation.
It never creates a missing fence and never crosses an active recovery marker.

The 035 recovery enforces the following invariants:

- **Prepare.** Under the singleton's lock, a new recovery refuses a live runtime lease,
  increments `generation` exactly once, sets `recovery_id`, enters `prepared`,
  disarms the generation, and requeues all canonical revisions in the same transaction.
- **Interlock.** All runtime paths require `recovery_id IS NULL`. An active recovery
  refuses any other UUID, even after expiry. The same UUID can resume the expired lease
  without a new bump or a new requeue.
- **Neo4j reset.** A Neo4j transaction removes only the Brain projection labels and
  the `BrainProjectionCursor`s, then installs the exact tuple with its recovery marker. It
  preserves the `BrainProjectionFence` and nodes with no allowlisted label. It accepts an
  absent fence, its exact marker on a generation less than or equal, or a fence with no marker on
  a lower generation. During a PostgreSQL `neo_ready` resume, it also accepts the
  exact fence with no marker from the same owner, the case of a crash after Neo4j finalization; it refuses any
  foreign marker and any more recent finalized generation.
- **Neo ready.** PostgreSQL moves to `neo_ready` and arms the generation via exact CAS only
  after the Neo4j reset commits.
- **Resume neo_ready.** The same active UUID always replays the bounded reset before finalizing.
  The surviving fence and cursors are not proof of content integrity. A
  future fence, a wrong protocol, or a foreign marker is still refused.
- **Finalize.** Neo4j first removes the exact marker. PostgreSQL then copies `recovery_id`
  into `last_completed_recovery_id`, returns to `idle`, and releases the lease. This column makes
  the last completed recovery idempotent; it is not a historical log.

The SQL constraint requires an owner and a lease timestamp during `prepared` and `neo_ready`;
the repository's CAS operations, not the `CHECK`, enforce that this lease is still alive. The reset
remains destructive despite its allowlist: a dedicated Neo4j database remains mandatory. Option A does
not require a Neo4j backup, because PostgreSQL is the sole restored state and the projection is
rebuilt. CLI confirmations do not replace external proofs.

Migrations 034–035 provide the runtime fencing and its operator-driven recovery. They
attest neither a PostgreSQL restore at the exactly deployed head, nor a full Neo4j rebuild,
nor the isolation of legacy writers.

### Backfill and triggers of migration 033

The upgrade locks the source tables `SHARE ROW EXCLUSIVE`, normalizes the aliases, then
backfills projects, entities, memberships, supersessions and `MERGED_INTO` relations. It
creates an initial event for each current revision.

The triggers then maintain:

- the Project registry from `project_contexts`;
- projects only referenced from `indexed_plan_chunks`, `gitlab_events`,
  `brain_sessions`, `search_log`, the ticket tables and
  `project_contexts.related_projects`;
- the identities of the seven tables `decisions`, `learnings`, `snippets`, `runbooks`, `adrs`,
  `features` and `indexed_plans`;
- the `BELONGS_TO`, `SUPERSEDES` and `MERGED_INTO` relations derived from the business columns;
- the lifecycle of relations when their endpoints change;
- an outbox statement in the same transaction as each canonical change.

After migration, `project_contexts.project_key` is immutable. Renaming a project requires an
explicit migration operation; a direct `UPDATE` fails.

### Table `decisions`

```sql
CREATE TABLE decisions (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    title VARCHAR(200) NOT NULL,
    description TEXT NOT NULL,
    reasoning TEXT NOT NULL,
    alternatives TEXT[] DEFAULT '{}',
    consequences TEXT,
    project_key VARCHAR(50),
    tags TEXT[] DEFAULT '{}',
    status VARCHAR(20) NOT NULL DEFAULT 'active'
        CHECK (status IN ('active', 'superseded', 'deprecated')),
    superseded_by UUID REFERENCES decisions(id) ON DELETE SET NULL,
    embedding vector(1536),              -- pgvector type, Qodo-Embed-1-1.5B via GPU service
    metadata JSONB DEFAULT '{}',
    search_vector TSVECTOR,              -- GENERATED ALWAYS AS ... STORED column (created by migration 001)
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    -- decay columns (migration 007)
    last_accessed_at TIMESTAMPTZ,
    access_count INTEGER DEFAULT 0,
    freshness_status VARCHAR(10) DEFAULT 'fresh',
    merged_into UUID                     -- set by brain_merge_entities
);

CREATE INDEX idx_decisions_search ON decisions USING GIN (search_vector);
CREATE INDEX idx_decisions_project ON decisions (project_key);
CREATE INDEX idx_decisions_status ON decisions (status);
CREATE INDEX idx_decisions_tags ON decisions USING GIN (tags);
CREATE INDEX idx_decisions_created ON decisions (created_at DESC);
CREATE INDEX idx_decisions_embedding ON decisions USING hnsw (embedding vector_cosine_ops)
    WITH (m = 16, ef_construction = 64);
```

### Table `learnings`

```sql
CREATE TABLE learnings (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    topic VARCHAR(200) NOT NULL,
    insight TEXT NOT NULL,
    source TEXT,
    source_type VARCHAR(20) NOT NULL DEFAULT 'experience'
        CHECK (source_type IN ('documentation', 'experience', 'article', 'video', 'book',
                               'conversation', 'code_review', 'bug', 'external',
                               'research', 'automated')),
    confidence VARCHAR(10) NOT NULL DEFAULT 'medium'
        CHECK (confidence IN ('low', 'medium', 'high')),
    project_key VARCHAR(50),
    tags TEXT[] DEFAULT '{}',
    validated_at TIMESTAMPTZ,
    embedding vector(1536),
    metadata JSONB DEFAULT '{}',
    search_vector TSVECTOR,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    -- decay columns (migration 007)
    last_accessed_at TIMESTAMPTZ,
    access_count INTEGER DEFAULT 0,
    freshness_status VARCHAR(10) DEFAULT 'fresh',
    merged_into UUID
);

CREATE INDEX idx_learnings_search ON learnings USING GIN (search_vector);
CREATE INDEX idx_learnings_project ON learnings (project_key);
CREATE INDEX idx_learnings_confidence ON learnings (confidence);
CREATE INDEX idx_learnings_tags ON learnings USING GIN (tags);
CREATE INDEX idx_learnings_created ON learnings (created_at DESC);
CREATE INDEX idx_learnings_embedding ON learnings USING hnsw (embedding vector_cosine_ops)
    WITH (m = 16, ef_construction = 64);
```

### Table `snippets`

```sql
CREATE TABLE snippets (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    title VARCHAR(200) NOT NULL,
    intention TEXT NOT NULL,
    code TEXT NOT NULL,
    language VARCHAR(50) NOT NULL,
    dependencies TEXT[] DEFAULT '{}',
    usage_example TEXT,
    gotchas TEXT,
    project_key VARCHAR(50),
    tags TEXT[] DEFAULT '{}',
    use_count INTEGER NOT NULL DEFAULT 0,
    last_used_at TIMESTAMPTZ,
    embedding vector(1536),
    metadata JSONB DEFAULT '{}',
    search_vector TSVECTOR,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    -- decay columns (migration 007)
    last_accessed_at TIMESTAMPTZ,
    access_count INTEGER DEFAULT 0,
    freshness_status VARCHAR(10) DEFAULT 'fresh',
    merged_into UUID
);

CREATE INDEX idx_snippets_search ON snippets USING GIN (search_vector);
CREATE INDEX idx_snippets_language ON snippets (language);
CREATE INDEX idx_snippets_project ON snippets (project_key);
CREATE INDEX idx_snippets_tags ON snippets USING GIN (tags);
CREATE INDEX idx_snippets_use_count ON snippets (use_count DESC);
CREATE INDEX idx_snippets_created ON snippets (created_at DESC);
CREATE INDEX idx_snippets_embedding ON snippets USING hnsw (embedding vector_cosine_ops)
    WITH (m = 16, ef_construction = 64);
```

### Table `runbooks`

```sql
CREATE TABLE runbooks (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    title VARCHAR(200) NOT NULL,
    description TEXT NOT NULL,
    project_key VARCHAR(50) NOT NULL,
    trigger TEXT NOT NULL,
    prerequisites TEXT[] DEFAULT '{}',
    steps JSONB NOT NULL DEFAULT '[]',          -- serialized List[RunbookStep]
    rollback_steps JSONB DEFAULT '[]',          -- serialized List[RunbookStep]
    estimated_duration VARCHAR(50),
    tags TEXT[] DEFAULT '{}',
    execution_count INTEGER NOT NULL DEFAULT 0,
    last_executed_at TIMESTAMPTZ,
    last_execution_status VARCHAR(20)
        CHECK (last_execution_status IN ('success', 'failed', 'partial', 'skipped')),
    embedding vector(1536),
    metadata JSONB DEFAULT '{}',
    search_vector TSVECTOR,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    -- decay columns (migration 007)
    last_accessed_at TIMESTAMPTZ,
    access_count INTEGER DEFAULT 0,
    freshness_status VARCHAR(10) DEFAULT 'fresh',
    merged_into UUID,

    UNIQUE(title, project_key)
);

CREATE INDEX idx_runbooks_search ON runbooks USING GIN (search_vector);
CREATE INDEX idx_runbooks_project ON runbooks (project_key);
CREATE INDEX idx_runbooks_tags ON runbooks USING GIN (tags);
CREATE INDEX idx_runbooks_created ON runbooks (created_at DESC);
CREATE INDEX idx_runbooks_embedding ON runbooks USING hnsw (embedding vector_cosine_ops)
    WITH (m = 16, ef_construction = 64);
```

### Table `adrs`

```sql
CREATE TABLE adrs (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    number INTEGER NOT NULL,
    title VARCHAR(200) NOT NULL,
    context TEXT NOT NULL,
    decision TEXT NOT NULL,
    consequences TEXT NOT NULL,
    alternatives_considered JSONB DEFAULT '[]',  -- List[AlternativeConsidered]
    project_key VARCHAR(50) NOT NULL,
    tags TEXT[] DEFAULT '{}',
    status VARCHAR(20) NOT NULL DEFAULT 'proposed'
        CHECK (status IN ('proposed', 'accepted', 'deprecated', 'superseded')),
    decided_at TIMESTAMPTZ,
    superseded_by INTEGER,                       -- ADR number (not UUID)
    embedding vector(1536),
    metadata JSONB DEFAULT '{}',
    search_vector TSVECTOR,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    -- decay columns (migration 007)
    last_accessed_at TIMESTAMPTZ,
    access_count INTEGER DEFAULT 0,
    freshness_status VARCHAR(10) DEFAULT 'fresh',
    merged_into UUID,

    UNIQUE(number, project_key)
);

CREATE INDEX idx_adrs_search ON adrs USING GIN (search_vector);
CREATE INDEX idx_adrs_project ON adrs (project_key);
CREATE INDEX idx_adrs_status ON adrs (status);
CREATE INDEX idx_adrs_tags ON adrs USING GIN (tags);
CREATE INDEX idx_adrs_number ON adrs (project_key, number DESC);
CREATE INDEX idx_adrs_created ON adrs (created_at DESC);
CREATE INDEX idx_adrs_embedding ON adrs USING hnsw (embedding vector_cosine_ops)
    WITH (m = 16, ef_construction = 64);
```

### Table `project_contexts`

```sql
CREATE TABLE project_contexts (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    project_key VARCHAR(50) NOT NULL UNIQUE,
    name VARCHAR(200) NOT NULL,
    description TEXT NOT NULL,
    languages TEXT[] DEFAULT '{}',
    frameworks TEXT[] DEFAULT '{}',
    databases TEXT[] DEFAULT '{}',
    code_style TEXT,
    git_workflow TEXT,
    test_strategy TEXT,
    current_phase TEXT,
    current_focus TEXT,
    focus_revision BIGINT NOT NULL DEFAULT 0, -- migration 032; CAS for session end
    focus_updated_at TIMESTAMPTZ,             -- migration 040; NULL = never measured
    blockers TEXT[] DEFAULT '{}',
    related_projects TEXT[] DEFAULT '{}',
    local_path TEXT,
    repo_url TEXT,
    decisions_count INTEGER NOT NULL DEFAULT 0,
    learnings_count INTEGER NOT NULL DEFAULT 0,
    snippets_count INTEGER NOT NULL DEFAULT 0,
    runbooks_count INTEGER NOT NULL DEFAULT 0,
    adrs_count INTEGER NOT NULL DEFAULT 0,
    metadata JSONB DEFAULT '{}',
    plan_scan_paths TEXT[] DEFAULT '{}',         -- drives PlanIndexer
    gitlab_project_path VARCHAR(200),            -- drives webhook ingestion
    project_group VARCHAR(50),                   -- for cross-project search
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX idx_project_contexts_key ON project_contexts (project_key);
CREATE INDEX idx_project_contexts_languages ON project_contexts USING GIN (languages);
CREATE INDEX idx_project_contexts_frameworks ON project_contexts USING GIN (frameworks);
```

**Note**: no embedding and no search_vector for ProjectContext (no semantic search on projects).

Migration 032 also adds a `project_contexts_focus_revision_trigger` trigger.
Before each update of `current_focus`, it increments `focus_revision` only
if the focus value changes.

Migration 040 adds `focus_updated_at`, which dates the focus prose. `updated_at` cannot
do this: it moves on every write to the row, counters included. The column is written by the
application code via `brain_v42.db.focus_stamp`, never by a trigger, and under the same
`IS DISTINCT FROM` condition as `focus_revision` — rewriting the focus unchanged does not refresh it,
which is precisely what makes a recopy visible. No backfill: `NULL` means
"never measured" and gets fixed at the first real focus write.

Migration 033 normalizes `project_key` and `related_projects` via `project_aliases`, then
forbids direct changes to `project_key`. It also synchronizes the canonical Project,
its graph entity and their outbox event.

### Session tables (migrations 032 and 037)

```sql
CREATE TABLE brain_sessions (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    project_key VARCHAR(50) NOT NULL
        REFERENCES project_contexts(project_key) ON DELETE RESTRICT,
    client_key VARCHAR(128) NOT NULL CHECK (btrim(client_key) <> ''),
    status VARCHAR(20) NOT NULL DEFAULT 'open'
        CHECK (status IN ('open', 'ended', 'abandoned')),
    started_focus TEXT,
    started_focus_revision BIGINT NOT NULL,
    summary TEXT,
    next_focus TEXT,
    captured_knowledge_ids UUID[] NOT NULL DEFAULT '{}',
    nothing_to_capture_reason TEXT,
    abandonment_reason TEXT,
    end_expected_focus_revision BIGINT,
    focus_outcome VARCHAR(20),
    focus_at_end TEXT,
    focus_revision_at_end BIGINT,
    started_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    last_heartbeat_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    ended_at TIMESTAMPTZ,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT uq_brain_sessions_project_client
        UNIQUE (project_key, client_key),
    CONSTRAINT brain_sessions_focus_outcome_valid CHECK (
        focus_outcome IS NULL OR focus_outcome IN ('applied', 'conflict')
    ),
    CONSTRAINT brain_sessions_capture_ids_valid CHECK (
        cardinality(captured_knowledge_ids) <= 100
        AND array_position(captured_knowledge_ids, NULL) IS NULL
    ),
    CONSTRAINT brain_sessions_terminal_state_valid CHECK (
        (status = 'open'
            AND ended_at IS NULL
            AND summary IS NULL
            AND next_focus IS NULL
            AND cardinality(captured_knowledge_ids) = 0
            AND nothing_to_capture_reason IS NULL
            AND abandonment_reason IS NULL
            AND end_expected_focus_revision IS NULL
            AND focus_outcome IS NULL
            AND focus_at_end IS NULL
            AND focus_revision_at_end IS NULL)
        OR
        (status = 'ended'
            AND ended_at IS NOT NULL
            AND summary IS NOT NULL AND btrim(summary) <> ''
            AND next_focus IS NOT NULL AND btrim(next_focus) <> ''
            AND abandonment_reason IS NULL
            AND focus_outcome IS NOT NULL
            AND (end_expected_focus_revision IS NULL
                OR end_expected_focus_revision >= 0)
            AND (focus_revision_at_end IS NULL
                OR focus_revision_at_end >= 0)
            AND ((end_expected_focus_revision IS NULL
                    AND focus_outcome = 'applied'
                    AND focus_at_end = next_focus
                    AND focus_revision_at_end IS NULL)
                OR (end_expected_focus_revision IS NOT NULL
                    AND focus_revision_at_end IS NOT NULL
                    AND ((focus_outcome = 'applied'
                            AND focus_at_end = next_focus
                            AND focus_revision_at_end =
                                end_expected_focus_revision + 1)
                        OR (focus_outcome = 'conflict'
                            AND focus_revision_at_end <>
                                end_expected_focus_revision))))
            AND ((cardinality(captured_knowledge_ids) > 0
                    AND nothing_to_capture_reason IS NULL)
                OR (cardinality(captured_knowledge_ids) = 0
                    AND nothing_to_capture_reason IS NOT NULL
                    AND btrim(nothing_to_capture_reason) <> '')))
        OR
        (status = 'abandoned'
            AND ended_at IS NOT NULL
            AND summary IS NULL
            AND next_focus IS NULL
            AND cardinality(captured_knowledge_ids) = 0
            AND nothing_to_capture_reason IS NULL
            AND abandonment_reason IS NOT NULL
            AND btrim(abandonment_reason) <> ''
            AND end_expected_focus_revision IS NULL
            AND focus_outcome IS NULL
            AND focus_at_end IS NULL
            AND focus_revision_at_end IS NULL)
    )
);

CREATE INDEX idx_brain_sessions_project_status_started
    ON brain_sessions (project_key, status, started_at DESC);

CREATE TABLE brain_session_artifacts (
    knowledge_id UUID PRIMARY KEY,
    session_id UUID NOT NULL
        REFERENCES brain_sessions(id) ON DELETE CASCADE,
    knowledge_type VARCHAR(32) NOT NULL
        CHECK (knowledge_type IN (
            'decision', 'learning', 'snippet', 'runbook',
            'adr', 'indexed_plan', 'legacy'
        )),
    captured_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX idx_brain_session_artifacts_session_captured
    ON brain_session_artifacts (session_id, captured_at);
```

Several `open` sessions can coexist for the same project. The
`(project_key, client_key)` pair provides start idempotency; no uniqueness
constraint limits the number of open sessions. Targeted mutations also require
`expected_client_key` at the application level. The comparison against the stored `client_key`
guards against a wrong session UUID, but does not constitute authentication.

`brain_session_artifacts` carries the persistent provenance declared by the client. The
primary key `knowledge_id` attributes each UUID to exactly one session. The repository locks
the project, then verifies that each UUID exists in `decisions`, `learnings`, `snippets`,
`runbooks`, `adrs` or `indexed_plans`, belongs to the same project, and was created after
`started_at`. This attribution proves the persisted declaration, not the identity of the process
that created the artifact. `captured_knowledge_ids` stays empty during the session and receives
a copy of the ledger at the end as a terminal snapshot.

The model/API separately exposes `attributed_knowledge_ids`, a derived field that is not a
column. The repository rehydrates it from the ledger for start retries,
reads, lists, resumes, captures, heartbeats and abandonments. An abandoned session therefore
keeps a view and exclusive ownership of its attributions, even though its terminal snapshot
`captured_knowledge_ids` must stay empty.

`last_heartbeat_at` feeds the application's `is_stale` computation. An `open` session is stale
after 24 hours without a heartbeat; this status is derived, is not stored, and never closes the
session automatically. A heartbeat or a capture refreshes the timestamp.

Ending requires either a non-empty ledger or `nothing_to_capture_reason`, exclusively. It
persists the focus attempt in `end_expected_focus_revision`, then its result in
`focus_outcome`, `focus_at_end` and `focus_revision_at_end`. If the expected revision matches,
the focus is applied and the revision advances (`applied`). Otherwise, the shared focus stays unchanged,
but the session still ends (`conflict`). These fields make terminal replay stable.

During the 037 upgrade, `last_heartbeat_at` is taken from `updated_at`. Completed v3 sessions
receive `focus_outcome='applied'` and `focus_at_end=next_focus`; the requested revision and the
resulting revision stay `NULL`, since they were not persisted. Terminal v3 captures
are copied with `knowledge_type='legacy'`. The upgrade fails if the same UUID
appears across several sessions, so as not to invent a provenance; duplicates of the
same UUID within a single v3 session are deduplicated in the ledger.

The 037→036 downgrade refuses any attribution not already reflected in an `ended`
session's snapshot, as well as any `focus_outcome='conflict'`. These states are valid in
v4 but not representable in v3; the rollback must therefore be prepared offline rather than
silently dropping their provenance or their outcome.

### Table `indexed_plans` (migration 009 + extended by migration 014)

```sql
-- Base columns (migration 009)
CREATE TABLE indexed_plans (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    file_path VARCHAR(500) NOT NULL UNIQUE,
    title VARCHAR(200) NOT NULL,   -- VARCHAR(200) in DB (migration 009); tables.py declares String(500) — known drift
    plan_type VARCHAR(20) NOT NULL,          -- 'spec' | 'plan'
    project_key VARCHAR(50) NOT NULL,
    content_hash VARCHAR(64) NOT NULL,       -- skip unchanged files on reindex
    embedding vector(1536),
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Columns added by migration 014 (plan_chunks)
ALTER TABLE indexed_plans ADD COLUMN content TEXT NOT NULL DEFAULT '';
ALTER TABLE indexed_plans ADD COLUMN summary TEXT;
ALTER TABLE indexed_plans ADD COLUMN search_vector TSVECTOR;
ALTER TABLE indexed_plans ADD COLUMN tags VARCHAR[] NOT NULL DEFAULT '{}';
ALTER TABLE indexed_plans ADD COLUMN metadata JSONB NOT NULL DEFAULT '{}';
ALTER TABLE indexed_plans ADD COLUMN status VARCHAR(20) NOT NULL DEFAULT 'active'
    CHECK (status IN ('draft', 'active', 'archived'));
ALTER TABLE indexed_plans ADD COLUMN chunk_count INTEGER NOT NULL DEFAULT 0;
ALTER TABLE indexed_plans ADD COLUMN word_count INTEGER NOT NULL DEFAULT 0;
ALTER TABLE indexed_plans ADD COLUMN access_count INTEGER NOT NULL DEFAULT 0;
ALTER TABLE indexed_plans ADD COLUMN last_accessed_at TIMESTAMPTZ;
ALTER TABLE indexed_plans ADD COLUMN freshness_status VARCHAR(20) NOT NULL DEFAULT 'fresh'
    CHECK (freshness_status IN ('fresh', 'stale', 'archived'));
ALTER TABLE indexed_plans ADD COLUMN indexed_at TIMESTAMPTZ NOT NULL DEFAULT NOW();

-- Indexes added by migration 014
CREATE INDEX idx_indexed_plans_tags ON indexed_plans USING GIN(tags);
CREATE INDEX idx_indexed_plans_search_vector ON indexed_plans USING GIN(search_vector);
CREATE INDEX idx_indexed_plans_pk_status_fresh ON indexed_plans(project_key, status, freshness_status);

-- Index added by migration 027
CREATE INDEX idx_indexed_plans_updated_at ON indexed_plans (updated_at DESC);
-- Eliminates filesort on list_plans() ORDER BY updated_at DESC
```

### Table `indexed_plan_chunks` (migration 014)

Created by `014_plan_chunks.py` via raw SQL. Declared in `tables.py` for Alembic autogenerate support.

```sql
CREATE TABLE indexed_plan_chunks (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    plan_id UUID NOT NULL REFERENCES indexed_plans(id) ON DELETE CASCADE,
    section_title VARCHAR(500) NOT NULL,
    section_path VARCHAR(1000) NOT NULL,
    content TEXT NOT NULL,
    section_order INTEGER NOT NULL,
    word_count INTEGER NOT NULL DEFAULT 0,
    embedding VECTOR(1536) NOT NULL,         -- required (non-nullable)
    search_vector TSVECTOR,
    tags VARCHAR[] NOT NULL DEFAULT '{}',
    project_key VARCHAR(50) NOT NULL,
    plan_type VARCHAR(20) NOT NULL
        CHECK (plan_type IN ('spec', 'plan')),
    status VARCHAR(20) NOT NULL DEFAULT 'active'
        CHECK (status IN ('draft', 'active', 'archived')),
    access_count INTEGER NOT NULL DEFAULT 0,
    last_accessed_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX idx_plan_chunks_plan_id ON indexed_plan_chunks(plan_id);
CREATE INDEX idx_plan_chunks_embedding ON indexed_plan_chunks
    USING hnsw (embedding vector_cosine_ops) WITH (m = 16, ef_construction = 64);
CREATE INDEX idx_plan_chunks_tags ON indexed_plan_chunks USING GIN(tags);
CREATE INDEX idx_plan_chunks_search_vector ON indexed_plan_chunks USING GIN(search_vector);
CREATE INDEX idx_plan_chunks_pk_type ON indexed_plan_chunks(project_key, plan_type);
```

**Note**: `plan_id` is the FK to `indexed_plans`. The chunk's `id` is not exposed via MCP — `brain_get(entity_type="plan", entity_id=...)` takes the `plan_id` (UUID of the parent plan).

---

## Coordination family — Cross-project tickets (migrations 028–029 and 038)

> These 4 tables sit **outside** the memory family: no embedding, no `search_vector`, no decay columns (`freshness_status`, `last_accessed_at`, `access_count`), no Neo4j sync. Tickets are addressed transient data — only the `ticket_extraction_proposals` table forms the bridge to the memory family (via `scripts/ticket_extract.py`).

### Table `tickets` (migration 028)

```sql
CREATE TABLE tickets (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    kind VARCHAR(10) NOT NULL
        CONSTRAINT tickets_kind_valid CHECK (kind IN ('request', 'fyi')),
    title VARCHAR(200) NOT NULL,
    body TEXT NOT NULL,
    from_project VARCHAR(50) NOT NULL,       -- sender (canonical kebab-case)
    to_project VARCHAR(50) NOT NULL,         -- recipient
    status VARCHAR(15) NOT NULL DEFAULT 'open'
        CONSTRAINT tickets_status_valid
            CHECK (status IN ('open', 'in_progress', 'resolved', 'wontfix', 'closed', 'acked')),
    extraction_status VARCHAR(10)            -- NULL until closure, then 'pending'
        CONSTRAINT tickets_extraction_status_valid
            CHECK (extraction_status IS NULL
                   OR extraction_status IN ('pending', 'proposed', 'skipped', 'done')),
    resolved_at TIMESTAMPTZ,
    closed_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
    -- NO embedding, NO search_vector, NO decay columns
);

CREATE INDEX idx_tickets_to_project_status ON tickets (to_project, status);
CREATE INDEX idx_tickets_from_project_status ON tickets (from_project, status);
CREATE INDEX idx_tickets_extraction_pending ON tickets (extraction_status)
    WHERE extraction_status = 'pending';    -- partial index — only tickets pending extraction
```

### Table `ticket_messages` (migration 028)

```sql
CREATE TABLE ticket_messages (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    ticket_id UUID NOT NULL REFERENCES tickets(id) ON DELETE CASCADE,
    author_project VARCHAR(50) NOT NULL,    -- canonical kebab-case
    body TEXT NOT NULL,
    status_to VARCHAR(15),                  -- non-NULL if the message accompanies a transition
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
    -- NO updated_at: messages are immutable
);

CREATE INDEX idx_ticket_messages_ticket ON ticket_messages (ticket_id, created_at);
```

### Table `ticket_extraction_proposals` (migration 029)

Bridge between the coordination family and the memory family. Generated by `scripts/ticket_extract.py` (proposer-only pattern, human review before apply).

```sql
CREATE TABLE ticket_extraction_proposals (
    id BIGSERIAL PRIMARY KEY,               -- sequential (not UUID) — insertion order
    ticket_id UUID REFERENCES tickets(id) ON DELETE SET NULL,   -- nullable (ticket deleted)
    target_type VARCHAR(10) NOT NULL
        CONSTRAINT tep_target_type_valid CHECK (target_type IN ('learning', 'decision')),
    target_project VARCHAR(50) NOT NULL,    -- target project of the created entity
    payload JSONB NOT NULL,                 -- fields of the future entity (title, content, etc.)
    rationale TEXT,                         -- LLM explanation of the choice to extract
    status VARCHAR(10) NOT NULL DEFAULT 'proposed'
        CONSTRAINT tep_status_valid CHECK (status IN ('proposed', 'applied', 'rejected')),
    applied_entity_id UUID,                 -- UUID of the entity created after apply
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    applied_at TIMESTAMPTZ                  -- timestamp of the apply
);

CREATE INDEX idx_tep_status ON ticket_extraction_proposals (status);
CREATE INDEX idx_tep_ticket ON ticket_extraction_proposals (ticket_id);
```

### Table `ticket_extraction_attempts` (migration 038)

Terminal log of Dream EXTRACT attempts. An interrupted or deferred attempt stays
observable without creating a persistent lease state; the ticket can be resumed on the next run.

```sql
CREATE TABLE ticket_extraction_attempts (
    id BIGSERIAL PRIMARY KEY,
    ticket_id UUID NOT NULL REFERENCES tickets(id) ON DELETE CASCADE,
    run_date DATE NOT NULL,
    status VARCHAR(10) NOT NULL
        CONSTRAINT ticket_extraction_attempts_status_valid
            CHECK (status IN ('done', 'failed', 'timeout', 'deferred')),
    duration_s DOUBLE PRECISION NOT NULL,
    error_message TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX idx_ticket_extraction_attempts_ticket
    ON ticket_extraction_attempts (ticket_id, created_at);
CREATE INDEX idx_ticket_extraction_attempts_date
    ON ticket_extraction_attempts (run_date, status);
```

**Extraction cycle:**
1. Ticket reaches `closed` or `acked` → `extraction_status = 'pending'`
2. `ticket_extract.py` compares each draft, by canonical embedding, against the active learnings/decisions of the same project and the drafts already retained in the run. The corpus search is exact; cosine `>= 0.85` → draft dropped.
3. An active row of the project with a missing or non-comparable embedding (norm `<= 1e-6`), an invalid new vector, or a DB/embedding failure fails the entire gate: no persistence, no WET apply, and tickets left `pending`.
4. Under a `FOR UPDATE` lock, a still-`pending` ticket is claimed and the new drafts become `status = 'proposed'`; `tickets.extraction_status` moves to `'proposed'`. A concurrent runner that has become stale persists nothing.
5. Human review: apply via `python -m scripts.ticket_extract --apply-ids "<ids>"` → `status = 'applied'`, `applied_entity_id` filled in, `tickets.extraction_status = 'done'`. This operator override does not replay the automatic gate.
6. With no proposal retained after deduplication → `tickets.extraction_status = 'skipped'`.

The `ticket_extract_corpus_dedup_cosine=0.85` threshold is inventoried but not calibrated. EXTRACT stays in DRY until several nights of soak, human review of the duplicate rate, and measurement of the exact search's cost.

---

### Other tables

| Table | Migration | Role |
|-------|-----------|------|
| `features` | 005/009/030 | Roadmap tracking — planned/research/design/building/deployed/done/archived statuses |
| `feature_artifacts` | 005 | Feature ↔ artifact links (decision, learning, gitlab event, etc.) |
| `search_log` | 004 | 1 row per brain_search call — search quality (30d retention) |
| `process_metrics` | 004 | Snapshot of in-memory counters per MCP process (upsert 30s) |
| `access_log` | 006 | Reads recorded by AccessLogger → freshness decay |
| `consolidation_log` | 008 | Merge audit (brain_merge_entities) — 1 row per merge |
| `gitlab_events` | 009/019 | Raw GitLab webhook payloads (deduplicated on gitlab_event_id) |
| `dream_runs` | 013/015/022 | 1 row per Dream phase (SCAN/CLEAN/CONNECT/SYNTH/REORG/EXTRACT) |
| `dream_promotions` | 016/017/021 | Audit of learning → ADR/runbook promotions by Dream SYNTH |
| `metrics_timeseries` | 018 | 24h history for the red-monitor cockpit (bucket_ts + metric PK) |
| `tickets` | 028 | Requests and FYIs addressed between projects |
| `ticket_messages` | 028 | History of messages associated with tickets |
| `ticket_extraction_proposals` | 029 | Capitalization proposals derived from terminal tickets |
| `ticket_extraction_attempts` | 038 | Terminal log of Dream EXTRACT attempts |
| `roadmap_curation_proposals` | 030/031 | Roadmap curation proposals and JSONB application log |
| `brain_sessions` | 032/037 | Explicit, persistent and concurrent lifecycle of Brain sessions |
| `brain_session_artifacts` | 037 | Exclusive ledger of artifacts attributed to sessions |

Notable index added by migration 027 on `consolidation_log`:
```sql
CREATE INDEX idx_consolidation_log_entity_type ON consolidation_log (entity_type);
-- get_handled_pairs() WHERE entity_type = ? — avoids the seq-scan on this table with no index
```

## Automatic updated_at trigger

```sql
CREATE OR REPLACE FUNCTION update_updated_at()
RETURNS TRIGGER AS $$
BEGIN
    NEW.updated_at = NOW();
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;
-- Triggers on: decisions, learnings, snippets, runbooks, adrs,
-- project_contexts, features, indexed_plans
```

## Alembic Migrations

52 revisions (001 → 052), in `alembic/versions/`.

| Revision | Main content |
|----------|-------------------|
| 001 | Initial schema (6 knowledge tables + triggers) |
| 002 | Embedding 384 → 1536 dims |
| 003 | Fix to the source_type CHECK |
| 004 | search_log + process_metrics |
| 005 | features + feature_artifacts (roadmap) |
| 006 | access_log |
| 007 | Decay columns (last_accessed_at, access_count, freshness_status) |
| 008 | consolidation_log |
| 009 | roadmap_v2: CREATE indexed_plans + gitlab_events, features.pinned, project_contexts.plan_scan_paths/gitlab_project_path |
| 010 | superseded_by ON DELETE SET NULL |
| 011 | Schema consistency fixes |
| 012 | project_groups + project_key normalization |
| 013 | dream_runs |
| 014 | indexed_plan_chunks + indexed_plans extension (content, tags, search_vector, decay) + 3 indexes |
| 015 | dream_runs.error_message |
| 016 | dream_promotions |
| 017 | dream_promotions tombstone |
| 018 | metrics_timeseries |
| 019 | gitlab_events.feature_id ON DELETE SET NULL |
| 020 | Source type 'automated' |
| 021 | dream_promotions.skipped_reason (TEXT, widened) |
| 022 | dream_runs.phase_dry_run |
| 023 | merged_into ON DELETE SET NULL |
| 024 | codex_ro readonly view |
| 025 | process_metrics.agent_name |
| 026 | collapse PID PK (agent_name becomes the unique PK) |
| 027 | idx_consolidation_log_entity_type + idx_indexed_plans_updated_at |
| 028 | tickets + ticket_messages (cross-project coordination) |
| 029 | ticket_extraction_proposals |
| 030 | roadmap curation: archived status, features.merged_into, roadmap_curation_proposals |
| 031 | roadmap_curation_proposals.apply_log |
| 032 | project_contexts.focus_revision + CAS trigger + brain_sessions |
| 033 | projects + aliases + entity registry + canonical relations + graph_outbox + triggers/backfill |
| 034 | durable fencing of the Neo4j projector: singleton v2 + outbox claim generation/version |
| 035 | projection recovery interlock: resumable UUID, PG↔Neo4j phases, and last completed UUID |
| 036 | Red-Codex read contract: nine `codex_*` views, replacement of `codex_brain_entity_v1`, `codex_ro` constraints and grants |
| 037 | session lifecycle v4: identity guard, capture ledger, heartbeat/stale, and persisted focus outcome |
| 038 | terminal log of Dream EXTRACT attempts (`ticket_extraction_attempts`) |
| 039 | isolation of the `project_contexts` timestamp trigger to preserve signed CAS operations |
| 040 | `project_contexts.focus_updated_at`, written by application code, no backfill |
| 041 | corpus provenance: `access_log.actor`, `access_count_human` (6 tables), `content_updated_at` (5 tables) written by conditional trigger `WHEN … IS DISTINCT FROM`, no backfill |
| 042 | `dream_runs.project_key` (VARCHAR(64), nullable, no default or backfill) + index `(run_date DESC, project_key)`. `NULL` = written before 042; `'*'` = global phase, set by four writers. Nullable as a consequence: none of the **six** INSERT sites surfaces its failure — three swallow it in their own function, two are swallowed by the orchestrator, the sixth is dead code and never runs |
| 043 | `freshness_status_updated_at` (TIMESTAMPTZ, nullable, no default or backfill) + `freshness_source` (VARCHAR(16), CHECK `NULL OR IN (merge, judgment, score, revive)`) on the **six** tables tracked by decay. Written by a conditional trigger `BEFORE UPDATE OF freshness_status … WHEN (OLD IS DISTINCT FROM NEW)`, the template of 041 rather than 040: `freshness_status` has four writers, including one prompt going through the generic `brain_update` tool. The trigger resets `freshness_source` to `NULL` when the writer does not redeclare it — an absent provenance is visible, a false provenance is believed. Hard prerequisite for the purge: without it, `updated_at` restarts on every counter write and no archive-dwell clock is honest |
| 044 | `last_accessed_at_human` (TIMESTAMPTZ, nullable, no default or backfill) on the six tables tracked by decay. 041 had given `access_count_human`, which fixes `freq_factor`; it left `access_factor` driven by MACHINE reads — **1,522 learnings in this state as of 2026-08-22**, 2,060 across the six tables. BOTH WEIGHTS ARE PER TYPE: `freq_factor` is 0.2 for `decision`/`learning`/`adr` and 0.3 for the other three; `access_factor` is 0.3 everywhere except `adr` (0.2), and is **never dominated by age** (`w_access >= w_age` on all six) — the "heaviest after age" formula underestimated it. The `pg_access_log` aggregate was already grouped by actor: it gains a `max_accessed_human` in the loop that already exists. Consumed behind `decay_human_signal_enabled`, shipped CLOSED |
| 045 | `dream_runs.model` moves from `varchar(30)` to `varchar(120)`. Two of the five configured phase models did not fit within 30 char., including the WET fallback that was **already configured** (`nvidia/nemotron-3-super-120b-a12b`, 33 char.); an overflow raises `StringDataRightTruncation` in a best-effort `INSERT`, so it is the entire ROW that disappears, not the column. The `codex_dream_run_v1` view must drop and come back around the `ALTER` — Postgres refuses to retype a column a view projects — and its `GRANT SELECT` to `codex_ro` is reapplied, a `DROP VIEW` taking its privileges with it. No table added, no data touched. Fail-closed downgrade if any rows exceed 30 char. |
| 046 | `brain_sessions` gains five nullable columns — `started_by_actor` (64), `last_observed_at`, `intent` (500), `nature` (16, CHECK `agent`/`operator`), `connection_id` (64) — plus a **PARTIAL** UNIQUE index `uq_brain_sessions_connection` `WHERE status = 'open'`: a full unique would burn the connection for life at the first auto-closure. BOTH CHECKs move — `status_valid` (032) and `terminal_state_valid` (037) — to accommodate the fourth state `closed_inactive`, reserved by the CHECK for sessions with `nature = 'agent'`. No backfill: `NULL` means "before 046". |
| 047 | The `ended` branch of `brain_sessions_terminal_state_valid` loses the XOR "non-empty ledger XOR `nothing_to_capture_reason`": `captured_knowledge_ids` no longer carries any constraint there, as with `closed_inactive`. Only "non-blank reason IF present" survives. No column, no table, no backfill. The CHECK text is RE-READ from 046 rather than retyped (045's template), and the replacement is asserted at import time. **Fail-closed** downgrade: it counts and names the `ended` closures the restored XOR would forbid (a derived ledger with a reason, or neither). |
| 048 | `brain_session_artifacts.attribution_mode` VARCHAR(24) NULL, with no default and no backfill (`NULL` = written before 048). CHECK `..._attribution_mode_valid` on four modes: `explicit`, `derived_deposit`, `derived_connection`, `derived_window`. PARTIAL index `idx_brain_session_artifacts_derived_window` on the DERIVED mode only — undoing a guess must be a query, not a scan. **Fail-closed** downgrade: it counts AND NAMES the `derived_window` attributions, because dropping the column makes them indistinguishable from an explicit human capture, and undoing it becomes impossible; named opt-in `-x allow_attribution_mode_downgrade=yes`. |
| 049 | `dream_runs.closed_inactive_count` and `dream_runs.thinking_tokens`, INTEGER NULL with no default or backfill (`NULL` = pre-049 / not measured); the six `ck_*_freshness_source` CHECKs widened with `manual_update` and `plan_reindex`. **Fail-closed** downgrade with THREE independent, named refusals — it is the independence of the downgrades that makes the multi-object head legitimate (9d22bc6a, criterion (c)): `-x allow_sweep_series_downgrade=yes`, `-x allow_thinking_tokens_downgrade=yes`, `-x allow_freshness_vocabulary_downgrade=yes` (the latter resets to NULL the provenances that 043's vocabulary cannot carry, before restoring the old CHECK). |
| 050 | `project_focus_history` — append-only audit trail of project focus. `project_key` with NO foreign key (the knowledge tables' doctrine: dropping a context must not take the trail down with it), PK `(project_key, focus_revision)` as the only index, and `focus` NULLABLE because an erased focus IS the destructive overwrite the trail exists to record. An append-only trigger refuses UPDATE and DELETE; a DEFERRED constraint trigger on `project_contexts`, `UPDATE OF current_focus` only, requires the history row at COMMIT. It ships DISABLED — arming it is a named operator gesture after the MCP restart, and while it is off the `ops/recovery` attestation is RED by construction (its CTE requires `tgenabled = 'O'`). Seed of one row per context, `source='migration_seed'`. **Fail-closed** downgrade. |
| 051 | `brain_session_checkpoints` — append-only ledger of session checkpoints (M-C). `UNIQUE(session_id, seq)` + `ON CONFLICT DO NOTHING` buys idempotence by the KEY instead of a CAS, so an exact replay is absorbed while the same `seq` carrying different content is REFUSED. `BEFORE UPDATE OR DELETE` trigger refuses every rewrite; FK `ON DELETE RESTRICT`, which is what lets the guard stay total — with CASCADE it would have to tell a cascaded DELETE from a direct one. Consequence, written down because an operator would otherwise meet it at 3AM: a session carrying checkpoints becomes INDELIBLE. No index beyond the key, and none added to `brain_sessions`, whose index list is CLOSED by the v4 recovery assets. **Fail-closed** downgrade naming the sessions whose judgment it would destroy; opt-in `-x allow_checkpoint_downgrade=yes`. |
| 052 | `access_log_daily` — the durable access journal (ticket b93e32be). `(entity_type, entity_id, actor, day)` primary key plus `ix_access_log_daily_entity_day` `(entity_type, entity_id, day)`, because the PK's `actor` sits between the entity and the day and cannot serve a range on `day` alone. Written by `pg_access_log.aggregate_in_session` in the SAME transaction as the counters and BEFORE the queue's DELETE — a crash between the two would lose exactly what the table exists to keep. `count` ACCUMULATES (`existing + excluded`) and `last_accessed_at` takes `greatest(...)`, so a late flush of older events cannot walk it backwards. The day is `date(accessed_at AT TIME ZONE 'UTC')`, pinned as a literal so the SELECT and the GROUP BY render one identical expression. NOT NULL throughout, no sentinel invented (`access_log.actor` is itself NOT NULL, and `'unknown'` is a value production already writes), and no backfill — the source events of everything before this migration were deleted flushes ago. **Fail-closed** downgrade naming the rows and days it would destroy; opt-in `-x allow_access_log_daily_downgrade=yes`. |

## Typical queries

### Full-text search with ranking

```sql
SELECT *, ts_rank(search_vector, plainto_tsquery('english', $1)) AS rank
FROM decisions
WHERE search_vector @@ plainto_tsquery('english', $1)
  AND ($2::varchar IS NULL OR project_key = $2)
  AND ($3::varchar IS NULL OR status = $3)
ORDER BY rank DESC
LIMIT $4 OFFSET $5;
```

### Vector search (pgvector)

```sql
-- Semantic search: top-K by cosine similarity
-- op('<=>',return_type=sa.Float): ADR #8 — changes only the Python result-processor,
-- emits NO SQL CAST (the HNSW plan is preserved).
SELECT *, 1 - (embedding <=> $1::vector) AS similarity
FROM decisions
WHERE embedding IS NOT NULL
  AND ($2::varchar IS NULL OR project_key = $2)
ORDER BY embedding <=> $1::vector
LIMIT $3;
```

**Note**: `<=>` is the cosine distance (1 - cosine_similarity). `op('<=>',return_type=sa.Float)` in SQLAlchemy (ADR #8, `pg_base.py:528`) changes only the Python result processor — without it, pgvector returns `bytea` and the sort fails. No `CAST` is emitted in the generated SQL (which preserves the HNSW plan).

### Supersession chain (recursive CTE)

```sql
-- The full chain from a decision
WITH RECURSIVE chain AS (
    SELECT id, title, status, superseded_by, 1 AS depth
    FROM decisions WHERE id = $1
    UNION ALL
    SELECT d.id, d.title, d.status, d.superseded_by, c.depth + 1
    FROM decisions d
    JOIN chain c ON d.id = c.superseded_by
)
SELECT * FROM chain ORDER BY depth;
```

### Count for refresh_counts

```sql
UPDATE project_contexts SET
    decisions_count = (SELECT COUNT(*) FROM decisions WHERE project_key = $1),
    learnings_count = (SELECT COUNT(*) FROM learnings WHERE project_key = $1),
    snippets_count = (SELECT COUNT(*) FROM snippets WHERE project_key = $1),
    runbooks_count = (SELECT COUNT(*) FROM runbooks WHERE project_key = $1),
    adrs_count = (SELECT COUNT(*) FROM adrs WHERE project_key = $1)
WHERE project_key = $1
RETURNING *;
```

## Embedding — Text generation by entity type

| Entity | Fields concatenated for embedding |
|--------|----------------------------------|
| Decision | `{title} {description} {reasoning} {' '.join(alternatives)} {' '.join(tags)}` |
| Learning | `{topic} {insight} {source or ''} {' '.join(tags)}` |
| Snippet | `{title} {intention} {language} {' '.join(tags)}` |
| Runbook | `{title} {description} {trigger} {' '.join(tags)}` |
| ADR | `{title} {context} {decision} {consequences} {' '.join(tags)}` |
| PlanChunk | `{section_title} {content}` (truncated to 15,000 chars max in `plan_indexer.py`) |
| ProjectContext | No embedding |

## GPU Embedding Service

```python
class GPUEmbeddingService:
    """Embedding service using HTTP GPU inference (Qodo-Embed-1-1.5B)."""

    def __init__(
        self,
        base_url: str = "http://localhost:8003",  # constructor default
        timeout: float = 30.0,
        max_retries: int = 3,
    ) -> None: ...

    async def embed(self, text: str) -> list[float]: ...
    async def embed_batch(self, texts: list[str]) -> list[list[float]]: ...
```

**Note**: The `Settings.embedding_service_url` default and the constructor's default are both `http://localhost:8003`. The `deploy/dev-pc` path is a rollback reference that has been obsolete since the return to the local GPU service on 6 July 2026. The interface stays `embed()` / `embed_batch()` in async via httpx. The `DIMENSION = 1536` constant is not defined in `GPUEmbeddingService` — the dimension is configured via `Settings.embedding_dimension`.
