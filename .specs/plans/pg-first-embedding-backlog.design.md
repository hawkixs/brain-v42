---
title: "PG-first embedding backlog"
status: accepted
feature: "Commit-before-async: universal state safety pattern across all projects"
---

# PG-first embedding backlog

## Goal and invariants

Decision, Learning, Snippet, Runbook, and ADR creation must survive a slow or unavailable
embedding service. PostgreSQL remains the source of truth: each service first inserts the
entity with `embedding=NULL`, commits it, and only then attempts derived work. The tool must
return the durable entity when embedding, feature linking, automatic linking, or Neo4j is
unavailable. A client retry must never be required to recover an entity that PostgreSQL has
already accepted.

For these five entity types, `embedding IS NULL` is the durable backlog. The schema already
allows null vectors, semantic queries already exclude them, and full-text search remains
available. This design adds no queue service and no migration solely to duplicate state that
PostgreSQL already records. Historical null vectors join the same backlog; the production
baseline on 12 July contained 14 Decisions and 29 Learnings without embeddings.

Creation preserves existing authoritative transactions, including ADR and Runbook promotion.
Explicit graph relations and node upserts may run after the PostgreSQL commit because they do
not require a vector. Feature linking and semantic auto-linking run only after an embedding
has been stored. Failures are logged with entity type and ID, never with entity content.

## Components and data flow

`EmbeddingEnrichmentService` owns the post-commit operation. It receives an entity type, ID,
canonical embedding text, and the row's `updated_at`. It applies a short request-path budget
around the existing embedding client. On success, a dedicated repository writes the vector
only when the row still exists, its embedding is null, and `updated_at` still matches the
captured value. This compare-and-set prevents a stale vector from overwriting a concurrent
content update. The service returns a typed outcome: `stored`, `stale`, `missing`, or
`unavailable`.

Each create service follows one sequence: insert null, perform graph write-through, call the
enricher, then run vector-dependent linkers only for `stored`. The returned model is refreshed
after a successful store; otherwise it remains the original durable row. Embedding exceptions,
timeouts, cancellations owned by the enrichment budget, and linker failures do not turn the
creation into an error. Process cancellation outside that bounded operation still propagates.

`EmbeddingBackfillJob` uses the same canonical text builders and compare-and-set repository.
It scans bounded batches of null rows, embeds them with the existing batch API, stores vectors,
and invokes vector-dependent linking. One PostgreSQL advisory lock prevents concurrent runs;
the compare-and-set remains the final idempotency guard. A CLI entry point supports dry-run,
entity-type filters, and bounded batch sizes. Scheduling stays a separate deployment step.

## Errors and observability

The request path records one structured outcome per entity without logging text or vectors.
Expected embedding outages use warning level and preserve the returned entity. Invalid vector
shape, repository errors, and stale compare-and-set outcomes remain visible but cannot erase
the PostgreSQL row. The worker continues across independent entities, reports partial failure,
and exits non-zero when any attempted enrichment fails.

Metrics expose backlog count and oldest age by entity type, attempted and stored totals, stale
skips, missing rows, timeouts, and failures split by embedding-unavailable kind. The backlog is
read directly from PostgreSQL, so a process restart cannot reset its truth. Existing graph
reconciliation remains the repair path for PG-to-Neo4j drift; the embedding worker does not
reimplement graph reconciliation.

The first delivery slice proves the safety boundary across all five create services. The
second adds the shared compare-and-set repository and post-commit enrichment. The third adds
the bounded worker, metrics, and failure injection. The final gate disables the embedding
endpoint during integration tests, creates each entity exactly once, restores the endpoint,
runs the worker twice, and proves one stored vector plus idempotent links per entity.

## Test strategy

TDD starts with service-level failures: embedding raises or exceeds its budget, yet the repo
has already created one null-vector entity and the caller receives it. Ordering assertions
prove `create` precedes `embed`. Existing happy-path tests are updated only after these RED
tests fail for the expected pre-commit behavior.

Repository integration tests exercise compare-and-set against a disposable PostgreSQL:
success, concurrent content update, deletion, already-enriched row, and duplicate worker run.
Worker tests use real repository behavior and a local fake HTTP embedding endpoint rather than
asserting mock calls. Failure injection covers service unavailable, malformed vector, timeout,
partial batch failure, cancellation, and linker outage. Full-text search must find a pending
entity while semantic search omits it; after backfill, semantic search includes it.

Acceptance requires targeted unit and integration suites, Ruff, mypy, GitNexus change
detection, and an isolated runtime drill. No live table mutation, service installation, or
production scheduler change belongs to this branch.
