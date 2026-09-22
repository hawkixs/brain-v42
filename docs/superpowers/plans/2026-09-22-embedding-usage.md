# Embedding usage preservation implementation plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development. Steps use checkbox syntax for tracking.

**Goal:** Satisfy ticket `5710a366`: preserve reported embedding tokens through the existing metrics pipeline, separating reads from writes.

**Architecture:** Add optional usage parsing to the wire without changing the public vector return types. A request-scoped capture carries reported usage across the embedding service into its metrics wrapper; the collector persists and aggregates separate read/write counters through the existing JSONB process metrics. Missing usage is unknown, represented by zero reported requests, never inferred from text length.

**Tech Stack:** Python 3.12, ContextVar, httpx, existing SQLAlchemy JSONB metrics, pytest.

## Global Constraints

- Preserve unrelated work; the Claude session owns plan indexing. Work only in this isolated worktree.
- Keep `embed() -> list[float]`, `embed_query() -> list[float]`, and `embed_texts() -> list[list[float]]` unchanged, including retry, empty-batch and degraded-service behavior.
- Count only provider-reported nonnegative integer `usage.total_tokens`; reject bool, floats, strings, negative integers, nulls and malformed containers silently. Missing usage is normal for both wires; ShimWire always reports none.
- Classify `embed_query` as `read`; `embed` and `embed_texts` as `write`. Count a batch's usage once, not once per vector.
- Never store/log request text, response bodies, URLs, API keys or provider-supplied labels in the new metrics. Labels are the fixed `read` and `write` vocabulary.
- Do not add a migration, pricing assumptions, new retention policy, invoice computation, a global last-response value, or a production rollout. These are counters over the existing process-metrics lifecycle, not a durable billing ledger.
- TDD: observe a meaningful failing assertion before implementation. Full unit tests, ruff check, ruff format --check and mypy must pass before commit. Use a dummy BRAIN_POSTGRES_URL for unit tests; do not copy the production .env or access a shared test database.
- GitNexus canonical index was refreshed at 2026-09-22T10:37:50.397Z and matches the worktree base. Invoke upstream impact before changing existing symbols and detect_changes before commit with the explicit worktree path, then inspect the actual git diff.
- Commit locally with an English conventional message; do not push, merge, change tickets/sessions, or restart any service.

### Task 1: Carry optional usage from HTTP to the sidecar

**Files:**
- Create `src/brain_v42/services/embedding_usage.py` for the bounded task-local capture.
- Modify `src/brain_v42/services/embedding_wire.py` and `gpu_embedding_service.py`.
- Modify `src/brain_v42/metrics/instrument.py`, `collector.py`, `collector_db.py`, `server.py`.
- Extend `tests/unit/services/test_embedding_wire.py`, `tests/unit/test_metrics_instrument.py`, `tests/unit/test_metrics_collector.py`, `tests/unit/test_metrics_server.py`; add a focused cross-process usage test under `tests/unit/metrics/` following `test_process_metrics_retention_window.py`.
- Extend the relevant existing English metrics documentation with the new fields and lifecycle; do not restructure unrelated docs.

**Interfaces:**
- `EmbeddingWire.parse_usage(payload: Any) -> int | None`; OpenAIWire validates `usage.total_tokens`, ShimWire returns None. Existing parse_single/parse_batch stay vector-only.
- `EmbeddingUsage` holds `total_tokens: int = 0` and `reported_requests: int = 0`.
- `capture_embedding_usage()` is a context manager yielding a fresh EmbeddingUsage and resetting its ContextVar token in finally; `record_embedding_usage(total_tokens: int | None)` adds only a known value to the active capture and otherwise does nothing.
- GPUEmbeddingService parses a successful response JSON once, records its optional usage before vector validation, then preserves the existing `_parsed` conversion of malformed vector/JSON responses. Thus valid reported tokens survive a vector-validation error, while an HTTP error or absent usage invents no tokens.
- InstrumentedEmbeddingService wraps each call in a capture and records it in its existing finally block. Captures cannot leak between concurrent requests, sequential calls, exceptions or cancellation.
- `MetricsCollector.record_embedding_usage(intent: Literal['read', 'write'], total_tokens: int, reported_requests: int) -> None` updates only the new usage counters. Preserve existing request/latency/error counters unchanged.
- `embedding_service.usage` and `_process.embedding.usage` have the same shape:

```json
{"read":{"total_tokens":0,"reported_requests":0},"write":{"total_tokens":0,"reported_requests":0}}
```

- `get_metrics`, `get_flush_data`, `collect_process_metrics` and the sidecar override carry that shape. Aggregate only `_process` rows, never per-agent duplicates. Legacy JSONB rows without `usage` contribute zero reported requests. A valid total of zero increments reported_requests. Return independent snapshots, not a mutable reference to collector state.

- [ ] **Step 1: Add failing behavior tests.** A minimal parser case is:

```python
def test_openai_usage_preserves_provider_total() -> None:
    wire = OpenAIWire(model="fixture-model")
    assert wire.parse_usage({"usage": {"total_tokens": 15}}) == 15
```

Add real httpx MockTransport tests through GPUEmbeddingService + InstrumentedEmbeddingService + MetricsCollector: query total 3, single document total 5, two-document batch total 7 yield read `(3,1)` and write `(12,2)`; returned vectors remain unchanged. Cover absent/invalid usage, zero, empty batch, concurrent read/write responses completing out of order, a successful report followed by absent usage, and valid usage accompanying malformed vectors. Verify snapshots and the cross-process sidecar response using the existing fake database boundary; no network or provider calls.

- [ ] **Step 2: Run the new tests and retain the RED command/output in the task report.** Initial missing-method failures establish the new wire interface; the end-to-end assertions must demonstrate the actual lost-counter behavior before its implementation.

- [ ] **Step 3: Implement the small capture, parsing and metrics propagation described above.** Keep request fields, credentials, prefixes, retry policy, reranker and vector shapes unchanged. If an existing fake needs the new usage parser, use an explicit implementation in the test fixture rather than runtime mock detection.

- [ ] **Step 4: Run focused tests, then required repository gates.** Run from this worktree:

```bash
BRAIN_POSTGRES_URL=postgresql+asyncpg://unit:unit@127.0.0.1:1/brain_unit .venv/bin/pytest -q tests/unit
.venv/bin/ruff check src/ tests/
.venv/bin/ruff format --check src/ tests/
.venv/bin/mypy src/
```

Document exact commands, exit codes and summaries. Investigate any failure; do not weaken tests or repair unrelated code silently. The initial focused baseline had 167 passing tests and one missing-settings failure because the isolated worktree lacks `.env`; rerun with the dummy variable above to settle that environment issue.

- [ ] **Step 5: Self-review, detect_changes, and commit.** Inspect all changed files and exports for accidental payload/secret retention and double counting. Commit as `fix(metrics): preserve embedding token usage by intent`. Report the commit, test evidence, remaining concerns and retention limits in the designated task report.

### Validation update (2026-09-22)

- RED is retained in `.superpowers/sdd/2026-09-22-embedding-usage/red.txt`:
  missing wire parsing and collector usage produced 10 expected failures.
- The focused embedding/metrics suite is green, including empty-batch HTTP avoidance
  and same-task cancellation capture reset.
- For the complete unit gate, use an ignored worktree `.env` containing only a fake
  `POSTGRES_URL`, with inherited `BRAIN_POSTGRES_URL`, `POSTGRES_URL`, and
  `BRAIN_V42_TEST_DB_URL` unset. This prevents a required dummy environment variable
  from defeating tests that intentionally control their own DSN. Run under `umask 022`;
  deployment preflight fixtures reject release paths created under `umask 002`.
- The parent owns the final full gate and commit; no production rollout is part of this
  task.
