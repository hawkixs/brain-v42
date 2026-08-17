# AV1 Real Linker Integration Proof Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Close the final AV1 acceptance gap by proving that a real PostgreSQL-backed `FeatureLinker` creates exactly one feature link for each of the five recovered entity types and that an identical second backfill run creates no extra work or links.

**Architecture:** Extend the existing isolated PostgreSQL integration drill only. Insert one real vectorized feature for the drill's random project key, use the production `FeatureLinker` raw-SQL path with the real session factory, and assert persisted `feature_artifacts` rows before and after the second run. Do not modify production services or use Neo4j; the missing roadmap proof is the real vector-dependent feature linker that the current test omits.

**Tech Stack:** Python 3.12+, pytest/pytest-asyncio, SQLAlchemy 2.x async sessions, PostgreSQL with pgvector, aiohttp fake embedding endpoint, existing Brain-v42 repositories and `FeatureLinker`.

## Global Constraints

- Run DB-backed verification only with `BRAIN_V42_TEST_DB_URL` pointing to a dedicated database whose name is not `brain`; never fall back to production.
- Keep the fake HTTP embedding endpoint, but use real PostgreSQL repositories and the real `FeatureLinker`; do not assert mock calls.
- Preserve the existing five-entity outage, FTS/vector, metrics, CAS, timestamp, and zero-second-batch-call assertions.
- The production mutation that must make this proof fail is removal or bypass of `link_artifact_if_enabled()` after a successful embedding store.
- The first run must persist exactly five links to one seeded feature: `decision`, `learning`, `snippet`, `runbook`, and `adr`, with the exact created artifact IDs.
- The second run must attempt zero rows, issue zero additional embedding batch calls, leave entity timestamps unchanged, and leave exactly the same five persisted links.
- No production source file, migration, scheduler, live database, service, or Neo4j instance may change.
- Before commit, run GitNexus change detection against this worktree and review every affected flow.

---

### Task 1: Prove Real Feature Linking and Idempotent Replay

**Files:**
- Modify: `tests/integration/test_embedding_backlog_recovery.py`
- Test: `tests/integration/test_embedding_backlog_recovery.py`

**Interfaces:**
- Consumes: `FeatureLinker(session_factory, threshold=0.70)` and `EmbeddingBackfillJob(..., feature_linker=feature_linker)`.
- Consumes: SQLAlchemy tables `features` and `feature_artifacts` from `brain_v42.db.tables`.
- Produces: A regression proof that reads persisted links from PostgreSQL; no new production interface.

- [ ] **Step 1: Add the failing persisted-link assertions**

Add these imports:

```python
import sqlalchemy as sa

from brain_v42.db.tables import feature_artifacts, features
from brain_v42.services.feature_linker import FeatureLinker
```

Inside `test_five_types_survive_outage_and_backfill_is_idempotent`, after `created` is built, seed one real feature with the same vector and random project key:

```python
feature_id = uuid.uuid4()
async with session_factory() as session:
    await session.execute(
        features.insert().values(
            id=feature_id,
            project_key=project_key,
            name=f"Feature {unique_term}",
            description="AV1 real linker integration target",
            status="building",
            embedding=VECTOR,
        )
    )
    await session.commit()

expected_links = {
    (entity_type, entity.id) for entity_type, entity in created.items()
}
```

After the first backfill, query rows for `feature_id` and assert the hand-derived cardinality and exact identities:

```python
async with session_factory() as session:
    first_link_rows = (
        await session.execute(
            sa.select(
                feature_artifacts.c.artifact_type,
                feature_artifacts.c.artifact_id,
            ).where(feature_artifacts.c.feature_id == feature_id)
        )
    ).all()
assert len(first_link_rows) == 5
assert {(row.artifact_type, row.artifact_id) for row in first_link_rows} == expected_links
```

After the second backfill and existing timestamp checks, repeat the query and assert `len(second_link_rows) == 5` and the same exact set equals `expected_links`.

Do not yet pass a linker to `EmbeddingBackfillJob`.

- [ ] **Step 2: Run the test and verify RED**

Run with an explicitly isolated PostgreSQL URL:

```bash
uv run pytest tests/integration/test_embedding_backlog_recovery.py::test_five_types_survive_outage_and_backfill_is_idempotent -q
```

Expected: FAIL at the first persisted-link assertion because `first_link_rows` is empty. A skip caused by missing `BRAIN_V42_TEST_DB_URL`, a connection error, or any failure before that assertion is not an acceptable RED result.

- [ ] **Step 3: Inject the minimal real linker**

Construct the existing production linker immediately before the job and pass it through the existing interface:

```python
feature_linker = FeatureLinker(session_factory=session_factory)
job = EmbeddingBackfillJob(
    session_factory=session_factory,
    repos=repos,
    embedding_svc=embedding_svc,
    feature_linker=feature_linker,
)
```

Make no production-code change.

- [ ] **Step 4: Run targeted GREEN verification**

Run:

```bash
uv run pytest tests/integration/test_embedding_backlog_recovery.py -q
uv run ruff check tests/integration/test_embedding_backlog_recovery.py
uv run ruff format --check tests/integration/test_embedding_backlog_recovery.py
git diff --check
```

Expected: both integration tests pass against the isolated database; Ruff, formatting, and diff checks exit zero with no warnings attributable to this change.

- [ ] **Step 5: Run the mutation check**

Temporarily remove only `feature_linker=feature_linker` from the test's job construction, rerun the targeted first test, and verify it fails at the persisted-link assertion. Restore the argument and rerun the targeted first test to green. Do not commit the temporary mutation.

- [ ] **Step 6: Commit the proof**

```bash
git add docs/superpowers/plans/2026-07-27-av1-linker-proof.md tests/integration/test_embedding_backlog_recovery.py
git commit -m "test(embedding): prove AV1 feature linking recovery"
```

Record the exact test commands, pass/fail counts, commit SHA, and any remaining concern in the SDD report.
