# Memory Decay Implementation Plan

> **For agentic workers:** REQUIRED: Use superpowers:subagent-driven-development (if subagents available) or superpowers:executing-plans to implement this plan. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add time-aware search scoring, lifecycle management, and duplicate consolidation to brain_v42's persistent memory system.

**Architecture:** Composite decay score (age + access recency + frequency + validation) computed at search time using aggregated access statistics. A DecayFlusher background task aggregates access logs and updates freshness status. A ConsolidationJob detects near-duplicate entities via embedding similarity.

**Tech Stack:** Python 3.12+, SQLAlchemy 2.0 async, asyncpg, Alembic, pgvector, Pydantic 2, structlog, asyncio

**Spec:** `docs/superpowers/specs/2026-03-13-memory-decay-design.md`

---

## File Structure

### New Files

| File | Responsibility |
|------|----------------|
| `src/brain_v42/services/decay.py` | DecayCalculator (pure math), DecayProfiles config |
| `src/brain_v42/services/decay_flusher.py` | Background task: aggregate access_log, update freshness |
| `src/brain_v42/services/consolidation.py` | ConsolidationJob: detect near-duplicates |
| `src/brain_v42/services/access_logger.py` | Bounded queue + batch consumer for access events |
| `src/brain_v42/repositories/pg_access_log.py` | AccessLog CRUD + aggregation queries |
| `src/brain_v42/repositories/pg_consolidation_log.py` | ConsolidationLog CRUD |
| `src/brain_v42/mcp/tools/decay_tools.py` | brain_decay_status, brain_refresh_entity, brain_consolidation_candidates, brain_merge_entities |
| `alembic/versions/006_access_log.py` | Create access_log table |
| `alembic/versions/007_decay_columns.py` | Add decay columns to 5 entity tables |
| `alembic/versions/008_consolidation_log.py` | Create consolidation_log table |
| `tests/unit/services/test_decay.py` | DecayCalculator unit tests |
| `tests/unit/services/test_access_logger.py` | AccessLogger unit tests |
| `tests/unit/services/test_decay_flusher.py` | DecayFlusher unit tests |
| `tests/unit/services/test_consolidation.py` | ConsolidationJob unit tests |
| `tests/unit/repositories/test_pg_access_log.py` | AccessLog repo unit tests |
| `tests/unit/repositories/test_pg_consolidation_log.py` | ConsolidationLog repo unit tests |
| `tests/unit/mcp/tools/test_decay_tools.py` | Decay MCP tools unit tests |

### Modified Files

| File | Changes |
|------|---------|
| `src/brain_v42/db/tables.py` | Add access_log table, consolidation_log table, decay columns on 5 entity tables |
| `src/brain_v42/config.py` | Add decay config fields |
| `src/brain_v42/services/search/hybrid.py` | Apply decay multiplier for re-ranking |
| `src/brain_v42/services/brain_service.py` | Inject DecayCalculator, log access on search results |
| `src/brain_v42/mcp/server.py` | Wire DecayFlusher, AccessLogger, ConsolidationJob, decay tools |
| `src/brain_v42/mcp/tools/__init__.py` | Export register_decay_tools |
| `src/brain_v42/metrics/collector.py` | Add decay metrics (stale_count, archived_count, access_log_size) |

---

## Batch 1: Foundation (sequential — no deps)

### Task 1: Alembic migration — access_log table

**Files:**
- Create: `alembic/versions/006_access_log.py`
- Modify: `src/brain_v42/db/tables.py`

- [ ] **Step 1: Define access_log table in tables.py**

Add after the existing `features` table definition:

```python
access_log = Table(
    "access_log",
    METADATA,
    Column("id", sa.BigInteger, primary_key=True, autoincrement=True),
    Column("entity_type", String(20), nullable=False),
    Column("entity_id", UUID(as_uuid=True), nullable=False),
    Column("access_type", String(20), nullable=False),
    Column(
        "accessed_at",
        DateTime(timezone=True),
        nullable=False,
        server_default=sa.text("NOW()"),
    ),
)
```

- [ ] **Step 2: Write Alembic migration 006**

```python
"""006 — Create access_log table."""

revision = "006"
down_revision = "005"

from alembic import op


def upgrade() -> None:
    op.execute("""
        CREATE TABLE access_log (
            id          BIGSERIAL PRIMARY KEY,
            entity_type VARCHAR(20) NOT NULL,
            entity_id   UUID NOT NULL,
            access_type VARCHAR(20) NOT NULL,
            accessed_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        );
        CREATE INDEX idx_access_log_entity ON access_log(entity_type, entity_id);
        CREATE INDEX idx_access_log_time ON access_log(accessed_at);
    """)


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS access_log;")
```

- [ ] **Step 3: Run ruff check and format**

Run: `uv run ruff check src/brain_v42/db/tables.py alembic/versions/006_access_log.py && uv run ruff format src/brain_v42/db/tables.py alembic/versions/006_access_log.py`

- [ ] **Step 4: Commit**

```bash
git add src/brain_v42/db/tables.py alembic/versions/006_access_log.py
git commit -m "feat(decay): add access_log table schema + migration 006"
```

---

### Task 2: Alembic migration — decay columns on 5 entity tables

**Files:**
- Create: `alembic/versions/007_decay_columns.py`
- Modify: `src/brain_v42/db/tables.py`

- [ ] **Step 1: Add decay columns to tables.py**

Add these columns to each of the 5 tables (decisions, learnings, snippets, runbooks, adrs):

```python
sa.Column("last_accessed_at", sa.DateTime(timezone=True), nullable=True),
sa.Column("access_count", sa.Integer, server_default=sa.text("0")),
sa.Column("freshness_status", sa.String(10), server_default=sa.text("'fresh'")),
Column("merged_into", UUID(as_uuid=True), nullable=True),
```

- [ ] **Step 2: Write Alembic migration 007**

```python
"""007 — Add decay columns to entity tables."""

revision = "007"
down_revision = "006"

from alembic import op

_TABLES = ("decisions", "learnings", "snippets", "runbooks", "adrs")


def upgrade() -> None:
    for table in _TABLES:
        op.execute(f"""
            ALTER TABLE {table}
            ADD COLUMN last_accessed_at TIMESTAMPTZ,
            ADD COLUMN access_count INTEGER DEFAULT 0,
            ADD COLUMN freshness_status VARCHAR(10) DEFAULT 'fresh',
            ADD COLUMN merged_into UUID;
        """)


def downgrade() -> None:
    for table in _TABLES:
        op.execute(f"""
            ALTER TABLE {table}
            DROP COLUMN IF EXISTS last_accessed_at,
            DROP COLUMN IF EXISTS access_count,
            DROP COLUMN IF EXISTS freshness_status,
            DROP COLUMN IF EXISTS merged_into;
        """)
```

- [ ] **Step 3: Run ruff check and format**

Run: `uv run ruff check src/brain_v42/db/tables.py alembic/versions/007_decay_columns.py && uv run ruff format src/brain_v42/db/tables.py alembic/versions/007_decay_columns.py`

- [ ] **Step 4: Commit**

```bash
git add src/brain_v42/db/tables.py alembic/versions/007_decay_columns.py
git commit -m "feat(decay): add decay columns to 5 entity tables — migration 007"
```

---

### Task 3: Alembic migration — consolidation_log table

**Files:**
- Create: `alembic/versions/008_consolidation_log.py`
- Modify: `src/brain_v42/db/tables.py`

- [ ] **Step 1: Define consolidation_log table in tables.py**

```python
consolidation_log = Table(
    "consolidation_log",
    METADATA,
    Column("id", sa.BigInteger, primary_key=True, autoincrement=True),
    Column("source_id", UUID(as_uuid=True), nullable=False),
    Column("target_id", UUID(as_uuid=True), nullable=False),
    Column("entity_type", String(20), nullable=False),
    Column("similarity", sa.Float, nullable=False),
    Column("action", String(20), nullable=False),
    Column(
        "created_at",
        DateTime(timezone=True),
        server_default=sa.text("NOW()"),
    ),
)
```

- [ ] **Step 2: Write Alembic migration 008**

```python
"""008 — Create consolidation_log table."""

revision = "008"
down_revision = "007"

from alembic import op


def upgrade() -> None:
    op.execute("""
        CREATE TABLE consolidation_log (
            id            BIGSERIAL PRIMARY KEY,
            source_id     UUID NOT NULL,
            target_id     UUID NOT NULL,
            entity_type   VARCHAR(20) NOT NULL,
            similarity    FLOAT NOT NULL,
            action        VARCHAR(20) NOT NULL,
            created_at    TIMESTAMPTZ DEFAULT NOW()
        );
    """)


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS consolidation_log;")
```

- [ ] **Step 3: Run ruff check and format**

Run: `uv run ruff check src/brain_v42/db/tables.py alembic/versions/008_consolidation_log.py && uv run ruff format src/brain_v42/db/tables.py alembic/versions/008_consolidation_log.py`

- [ ] **Step 4: Commit**

```bash
git add src/brain_v42/db/tables.py alembic/versions/008_consolidation_log.py
git commit -m "feat(decay): add consolidation_log table — migration 008"
```

---

### Task 4: Config — add decay settings

**Files:**
- Modify: `src/brain_v42/config.py`
- Test: `tests/unit/test_config.py`

- [ ] **Step 1: Write failing test**

```python
# In tests/unit/test_config.py — add new test class
from brain_v42.config import Settings

_PG_URL = "postgresql+asyncpg://brain:brain@localhost:5433/brain"


class TestDecayConfig:
    def test_decay_defaults(self) -> None:
        """Decay config has sensible defaults."""
        s = Settings(postgres_url=_PG_URL)
        assert s.decay_enabled is True
        assert s.decay_floor == 0.3
        assert s.stale_threshold == 0.5
        assert s.archive_threshold == 0.2

    def test_decay_flush_interval(self) -> None:
        """Decay flush interval defaults to 300 seconds."""
        s = Settings(postgres_url=_PG_URL)
        assert s.decay_flush_interval_seconds == 300

    def test_consolidation_defaults(self) -> None:
        """Consolidation config has sensible defaults."""
        s = Settings(postgres_url=_PG_URL)
        assert s.consolidation_interval_seconds == 21600
        assert s.consolidation_similarity_threshold == 0.92
        assert s.forgetting_archive_days == 180
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/unit/test_config.py::TestDecayConfig -v`
Expected: FAIL — attributes not found

- [ ] **Step 3: Add decay fields to Settings**

```python
# In config.py Settings class, after metrics_host
# --- Decay ---
decay_enabled: bool = True
decay_floor: float = 0.3
decay_flush_interval_seconds: int = 300
stale_threshold: float = 0.5
archive_threshold: float = 0.2
forgetting_archive_days: int = 180

# --- Consolidation ---
consolidation_interval_seconds: int = 21600
consolidation_similarity_threshold: float = 0.92
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/unit/test_config.py::TestDecayConfig -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/brain_v42/config.py tests/unit/test_config.py
git commit -m "feat(decay): add decay + consolidation config fields"
```

---

## Batch 2: DecayCalculator + Models (parallelizable)

### Task 5: DecayCalculator — pure math

**Files:**
- Create: `src/brain_v42/services/decay.py`
- Create: `tests/unit/services/test_decay.py`

- [ ] **Step 1: Write failing tests for DecayCalculator**

```python
"""Unit tests for DecayCalculator — pure math, no DB."""

from __future__ import annotations

import math
from datetime import datetime, timedelta, timezone

import pytest

from brain_v42.services.decay import DecayCalculator, DecayProfile


@pytest.fixture
def calculator() -> DecayCalculator:
    return DecayCalculator()


class TestDecayProfile:
    def test_default_profiles_exist(self, calculator: DecayCalculator) -> None:
        """All 5 entity types have profiles."""
        for t in ("decision", "learning", "snippet", "runbook", "adr"):
            assert t in calculator.profiles

    def test_profile_weights_sum_to_one(self, calculator: DecayCalculator) -> None:
        """Weights must sum to 1.0 for each profile."""
        for profile in calculator.profiles.values():
            total = profile.w_age + profile.w_access + profile.w_freq + profile.w_valid
            assert abs(total - 1.0) < 0.001


class TestComputeMultiplier:
    def test_brand_new_entity_is_fresh(self, calculator: DecayCalculator) -> None:
        """An entity created now with no accesses should have high multiplier."""
        now = datetime.now(tz=timezone.utc)
        result = calculator.compute_multiplier(
            entity_type="decision",
            created_at=now,
            last_accessed_at=None,
            access_count=0,
            is_validated=False,
        )
        assert result > 0.5

    def test_old_never_accessed_entity_decays(self, calculator: DecayCalculator) -> None:
        """An entity created 1 year ago with no accesses should decay."""
        old = datetime.now(tz=timezone.utc) - timedelta(days=365)
        result = calculator.compute_multiplier(
            entity_type="learning",
            created_at=old,
            last_accessed_at=None,
            access_count=0,
            is_validated=False,
        )
        assert result < 0.3

    def test_old_but_recently_accessed_stays_fresher(self, calculator: DecayCalculator) -> None:
        """An old entity accessed recently should score higher than one never accessed."""
        now = datetime.now(tz=timezone.utc)
        old = now - timedelta(days=365)

        never_accessed = calculator.compute_multiplier(
            entity_type="learning",
            created_at=old,
            last_accessed_at=None,
            access_count=0,
            is_validated=False,
        )
        recently_accessed = calculator.compute_multiplier(
            entity_type="learning",
            created_at=old,
            last_accessed_at=now - timedelta(hours=1),
            access_count=50,
            is_validated=False,
        )
        assert recently_accessed > never_accessed

    def test_validated_entity_gets_boost(self, calculator: DecayCalculator) -> None:
        """Validated entities score higher than non-validated."""
        now = datetime.now(tz=timezone.utc)
        old = now - timedelta(days=60)

        not_validated = calculator.compute_multiplier(
            entity_type="learning",
            created_at=old,
            last_accessed_at=None,
            access_count=0,
            is_validated=False,
        )
        validated = calculator.compute_multiplier(
            entity_type="learning",
            created_at=old,
            last_accessed_at=None,
            access_count=0,
            is_validated=True,
        )
        assert validated > not_validated

    def test_multiplier_between_zero_and_one(self, calculator: DecayCalculator) -> None:
        """Multiplier is always in [0.0, 1.0]."""
        now = datetime.now(tz=timezone.utc)
        for entity_type in ("decision", "learning", "snippet", "runbook", "adr"):
            for days_old in (0, 30, 180, 365, 1000):
                result = calculator.compute_multiplier(
                    entity_type=entity_type,
                    created_at=now - timedelta(days=days_old),
                    last_accessed_at=None,
                    access_count=0,
                    is_validated=False,
                )
                assert 0.0 <= result <= 1.0

    def test_adr_decays_slower_than_snippet(self, calculator: DecayCalculator) -> None:
        """ADRs should decay slower than snippets (longer half-life)."""
        now = datetime.now(tz=timezone.utc)
        old = now - timedelta(days=180)

        adr_score = calculator.compute_multiplier(
            entity_type="adr",
            created_at=old,
            last_accessed_at=None,
            access_count=0,
            is_validated=False,
        )
        snippet_score = calculator.compute_multiplier(
            entity_type="snippet",
            created_at=old,
            last_accessed_at=None,
            access_count=0,
            is_validated=False,
        )
        assert adr_score > snippet_score


class TestFreshnessStatus:
    def test_fresh_above_threshold(self, calculator: DecayCalculator) -> None:
        """Multiplier >= 0.5 → fresh."""
        assert calculator.freshness_status(0.8) == "fresh"
        assert calculator.freshness_status(0.5) == "fresh"

    def test_stale_between_thresholds(self, calculator: DecayCalculator) -> None:
        """0.2 <= multiplier < 0.5 → stale."""
        assert calculator.freshness_status(0.3) == "stale"
        assert calculator.freshness_status(0.2) == "stale"

    def test_archived_below_threshold(self, calculator: DecayCalculator) -> None:
        """Multiplier < 0.2 → archived."""
        assert calculator.freshness_status(0.1) == "archived"
        assert calculator.freshness_status(0.0) == "archived"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/unit/services/test_decay.py -v`
Expected: FAIL — module not found

- [ ] **Step 3: Implement DecayCalculator**

```python
"""Decay score calculator — pure math, no I/O."""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime, timezone


@dataclass(frozen=True)
class DecayProfile:
    """Decay parameters for one entity type."""

    age_half_life_days: float
    access_half_life_days: float
    w_age: float
    w_access: float
    w_freq: float
    w_valid: float
    freq_baseline: int


_DEFAULT_PROFILES: dict[str, DecayProfile] = {
    "decision": DecayProfile(180, 90, 0.3, 0.3, 0.2, 0.2, 10),
    "learning": DecayProfile(90, 60, 0.3, 0.3, 0.2, 0.2, 10),
    "snippet": DecayProfile(60, 30, 0.2, 0.3, 0.3, 0.2, 20),
    "runbook": DecayProfile(365, 180, 0.2, 0.3, 0.3, 0.2, 5),
    "adr": DecayProfile(730, 365, 0.1, 0.2, 0.2, 0.5, 3),
}


def _exp_decay(half_life_days: float, days_elapsed: float) -> float:
    """Exponential decay: returns value in [0.0, 1.0]."""
    if half_life_days <= 0:
        return 0.0
    lam = math.log(2) / half_life_days
    return math.exp(-lam * max(days_elapsed, 0.0))


@dataclass
class DecayCalculator:
    """Compute composite decay multiplier for entities."""

    profiles: dict[str, DecayProfile] = field(default_factory=lambda: dict(_DEFAULT_PROFILES))
    stale_threshold: float = 0.5
    archive_threshold: float = 0.2

    def compute_multiplier(
        self,
        entity_type: str,
        created_at: datetime,
        last_accessed_at: datetime | None,
        access_count: int,
        is_validated: bool,
    ) -> float:
        """Compute decay multiplier in [0.0, 1.0]."""
        profile = self.profiles.get(entity_type)
        if profile is None:
            return 1.0  # unknown type → no decay

        now = datetime.now(tz=timezone.utc)
        days_since_created = (now - created_at).total_seconds() / 86400

        # Fallback: last_accessed_at = created_at when NULL
        effective_access = last_accessed_at if last_accessed_at is not None else created_at
        days_since_access = (now - effective_access).total_seconds() / 86400

        age_factor = _exp_decay(profile.age_half_life_days, days_since_created)
        access_factor = _exp_decay(profile.access_half_life_days, days_since_access)
        frequency_factor = min(access_count / profile.freq_baseline, 1.0) if profile.freq_baseline > 0 else 0.0
        validation_factor = 1.0 if is_validated else 0.7

        multiplier = (
            profile.w_age * age_factor
            + profile.w_access * access_factor
            + profile.w_freq * frequency_factor
            + profile.w_valid * validation_factor
        )
        return max(0.0, min(1.0, multiplier))

    def freshness_status(self, multiplier: float) -> str:
        """Map decay multiplier to freshness status."""
        if multiplier >= self.stale_threshold:
            return "fresh"
        if multiplier >= self.archive_threshold:
            return "stale"
        return "archived"
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/unit/services/test_decay.py -v`
Expected: ALL PASS

- [ ] **Step 5: Run ruff**

Run: `uv run ruff check src/brain_v42/services/decay.py tests/unit/services/test_decay.py && uv run ruff format src/brain_v42/services/decay.py tests/unit/services/test_decay.py`

- [ ] **Step 6: Commit**

```bash
git add src/brain_v42/services/decay.py tests/unit/services/test_decay.py
git commit -m "feat(decay): add DecayCalculator with exponential decay + profiles"
```

---

### Task 6: AccessLogger — bounded queue + batch consumer

**Files:**
- Create: `src/brain_v42/services/access_logger.py`
- Create: `tests/unit/services/test_access_logger.py`

- [ ] **Step 1: Write failing tests**

```python
"""Unit tests for AccessLogger — bounded queue + batch consumer."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock
import uuid

import pytest


class TestAccessLogger:
    @pytest.mark.asyncio
    async def test_log_access_enqueues_event(self) -> None:
        """log_access puts event on the queue."""
        from brain_v42.services.access_logger import AccessLogger

        logger = AccessLogger(session_factory=MagicMock())
        logger.log_access("decision", uuid.uuid4(), "search_hit")
        assert logger._queue.qsize() == 1

    @pytest.mark.asyncio
    async def test_log_access_drops_on_full_queue(self) -> None:
        """log_access silently drops when queue is full."""
        from brain_v42.services.access_logger import AccessLogger

        logger = AccessLogger(session_factory=MagicMock(), max_queue_size=2)
        for _ in range(5):
            logger.log_access("decision", uuid.uuid4(), "search_hit")
        assert logger._queue.qsize() == 2

    @pytest.mark.asyncio
    async def test_flush_batch_inserts_to_db(self) -> None:
        """_flush_batch inserts queued events into access_log."""
        from brain_v42.services.access_logger import AccessLogger

        session = AsyncMock()
        session_factory = MagicMock()
        session_factory.return_value.__aenter__ = AsyncMock(return_value=session)
        session_factory.return_value.__aexit__ = AsyncMock(return_value=False)

        logger = AccessLogger(session_factory=session_factory)
        entity_id = uuid.uuid4()
        logger.log_access("learning", entity_id, "get_by_id")

        await logger._flush_batch()

        session.execute.assert_called_once()
        assert logger._queue.qsize() == 0
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/unit/services/test_access_logger.py -v`
Expected: FAIL — module not found

- [ ] **Step 3: Implement AccessLogger**

```python
"""Bounded-queue access logger — fire-and-forget, batch consumer."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any
from uuid import UUID

import sqlalchemy as sa
import structlog

from brain_v42.db.tables import access_log

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import async_sessionmaker, AsyncSession

logger = structlog.get_logger(__name__)

_BATCH_SIZE = 50
_FLUSH_INTERVAL = 5.0  # seconds


class AccessLogger:
    """Enqueues access events and flushes them in batches."""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        max_queue_size: int = 1000,
    ) -> None:
        self._session_factory = session_factory
        self._queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=max_queue_size)
        self._task: asyncio.Task[None] | None = None

    def log_access(self, entity_type: str, entity_id: UUID, access_type: str) -> None:
        """Enqueue an access event. Drops silently if queue is full."""
        try:
            self._queue.put_nowait({
                "entity_type": entity_type,
                "entity_id": entity_id,
                "access_type": access_type,
            })
        except asyncio.QueueFull:
            pass  # silent drop — best-effort logging

    async def start(self) -> None:
        """Start the background consumer loop."""
        self._task = asyncio.create_task(self._run_loop())

    async def stop(self) -> None:
        """Stop the consumer, flush remaining events."""
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        # Flush remaining
        if not self._queue.empty():
            await self._flush_batch()

    async def _run_loop(self) -> None:
        """Consumer loop: flush every N seconds or when batch is full."""
        try:
            while True:
                await asyncio.sleep(_FLUSH_INTERVAL)
                if not self._queue.empty():
                    await self._flush_batch()
        except asyncio.CancelledError:
            raise

    async def _flush_batch(self) -> None:
        """Drain queue and batch-insert into access_log."""
        events: list[dict[str, Any]] = []
        while not self._queue.empty() and len(events) < _BATCH_SIZE:
            try:
                events.append(self._queue.get_nowait())
            except asyncio.QueueEmpty:
                break

        if not events:
            return

        try:
            async with self._session_factory() as session:
                await session.execute(sa.insert(access_log), events)
                await session.commit()
            logger.debug("access_logger.flushed", count=len(events))
        except Exception:
            logger.warning("access_logger.flush_failed", count=len(events), exc_info=True)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/unit/services/test_access_logger.py -v`
Expected: ALL PASS

- [ ] **Step 5: Run ruff**

Run: `uv run ruff check src/brain_v42/services/access_logger.py tests/unit/services/test_access_logger.py && uv run ruff format src/brain_v42/services/access_logger.py tests/unit/services/test_access_logger.py`

- [ ] **Step 6: Commit**

```bash
git add src/brain_v42/services/access_logger.py tests/unit/services/test_access_logger.py
git commit -m "feat(decay): add AccessLogger with bounded queue + batch consumer"
```

---

### Task 7: AccessLog repository

**Files:**
- Create: `src/brain_v42/repositories/pg_access_log.py`
- Create: `tests/unit/repositories/test_pg_access_log.py`

- [ ] **Step 1: Write failing tests**

Tests should cover:
- `aggregate_and_flush()` — returns dict of `{(entity_type, entity_id): {"max_accessed_at": datetime, "count": int}}`
- `purge_old(days=30)` — deletes entries older than N days
- Both methods use mock session

- [ ] **Step 2: Run tests — verify fail**

Run: `uv run pytest tests/unit/repositories/test_pg_access_log.py -v`

- [ ] **Step 3: Implement PgAccessLogRepo**

Follow BasePgRepository pattern. Two methods:
- `aggregate_and_flush()`: SELECT entity_type, entity_id, MAX(accessed_at), COUNT(*) GROUP BY ..., then DELETE the aggregated rows
- `purge_old(days)`: DELETE FROM access_log WHERE accessed_at < NOW() - interval

- [ ] **Step 4: Run tests — verify pass**

Run: `uv run pytest tests/unit/repositories/test_pg_access_log.py -v`

- [ ] **Step 5: Commit**

```bash
git add src/brain_v42/repositories/pg_access_log.py tests/unit/repositories/test_pg_access_log.py
git commit -m "feat(decay): add PgAccessLogRepo with aggregate + purge"
```

---

## Batch 3: DecayFlusher + Search Integration (sequential — depends on Batch 2)

### Task 8: DecayFlusher — background aggregation + freshness updates

**Files:**
- Create: `src/brain_v42/services/decay_flusher.py`
- Create: `tests/unit/services/test_decay_flusher.py`

- [ ] **Step 1: Write failing tests**

Tests should cover:
- `_flush()` calls `access_log_repo.aggregate_and_flush()`
- `_flush()` updates `freshness_status` on entities when multiplier crosses thresholds
- `_flush()` calls `access_log_repo.purge_old(30)`
- Freshness transitions are logged

- [ ] **Step 2: Run tests — verify fail**

Run: `uv run pytest tests/unit/services/test_decay_flusher.py -v`

- [ ] **Step 3: Implement DecayFlusher**

Follow MetricsFlusher pattern:
- `__init__(session_factory, access_log_repo, decay_calculator, interval_seconds)`
- `start()` / `stop()` / `_run_loop()` / `_flush()`
- `_flush()`:
  1. Call `access_log_repo.aggregate_and_flush()` → get aggregated stats
  2. For each entity touched: update `last_accessed_at`, `access_count += count`
  3. Compute new `decay_multiplier` via `DecayCalculator`
  4. If `freshness_status` changed, UPDATE and log transition
  5. Call `access_log_repo.purge_old(30)`

- [ ] **Step 4: Run tests — verify pass**

Run: `uv run pytest tests/unit/services/test_decay_flusher.py -v`

- [ ] **Step 5: Commit**

```bash
git add src/brain_v42/services/decay_flusher.py tests/unit/services/test_decay_flusher.py
git commit -m "feat(decay): add DecayFlusher background task"
```

---

### Task 9: HybridSearcher — decay re-ranking integration

**Files:**
- Modify: `src/brain_v42/services/search/hybrid.py`
- Modify: `src/brain_v42/services/brain_service.py`
- Test: existing + new tests

- [ ] **Step 1: Write failing tests**

Add tests to the existing HybridSearcher test file:
- `test_search_applies_decay_reranking` — with a mock DecayCalculator, verify results are re-ranked by effective_score
- `test_search_skips_decay_when_disabled` — when decay_calculator is None, ranking is unchanged
- `test_search_excludes_archived_by_default` — entities with freshness_status='archived' are excluded
- `test_search_includes_archived_when_flag_set` — include_archived=True returns everything

- [ ] **Step 2: Run tests — verify fail**

Run: `uv run pytest tests/unit/services/test_hybrid_search.py -v` (or wherever hybrid tests live)

- [ ] **Step 3: Modify HybridSearcher**

In the search method, after RRF fusion + optional reranking:
1. If `decay_calculator` is not None and `decay_enabled`:
   - For each result, compute `decay_multiplier` from entity metadata (created_at, last_accessed_at, access_count, validated_at)
   - Compute `effective_score = score * (decay_floor + (1 - decay_floor) * multiplier)`
   - Sort by `effective_score`
   - Add `decay_multiplier` and `freshness_status` to result item metadata
   - Filter out `archived` unless `include_archived=True`
2. `SearchResult.score` stays as raw semantic score

- [ ] **Step 4: Run tests — verify pass**

Run: `uv run pytest tests/unit/services/ -v -k "hybrid or decay"`

- [ ] **Step 5: Modify BrainService to inject DecayCalculator and log accesses**

In `brain_service.py`:
- Add `decay_calculator: DecayCalculator | None = None` and `access_logger: AccessLogger | None = None` to `__init__`
- After search results are built, call `access_logger.log_access()` for each result
- Pass `decay_calculator` to HybridSearcher

- [ ] **Step 6: Add `include_archived` parameter to existing MCP tools**

In `src/brain_v42/mcp/tools/brain_tools.py`:
- Add `include_archived: bool = False` parameter to `brain_search` and `brain_what_do_i_know_about` tool registrations
- Pass it through BrainService → HybridSearcher

- [ ] **Step 7: Filter out `merged_into IS NOT NULL` entities in search**

In HybridSearcher or at repo level:
- Entities with `merged_into IS NOT NULL` are treated as archived and excluded from results (even if `freshness_status != 'archived'`)
- Apply this filter alongside the `freshness_status` filter

- [ ] **Step 8: Commit**

```bash
git add src/brain_v42/services/search/hybrid.py src/brain_v42/services/brain_service.py src/brain_v42/mcp/tools/brain_tools.py tests/
git commit -m "feat(decay): integrate decay re-ranking in HybridSearcher + BrainService"
```

---

## Batch 4: MCP Tools (parallelizable — depends on Batch 3)

### Task 10: Decay MCP tools — brain_decay_status + brain_refresh_entity

**Files:**
- Create: `src/brain_v42/mcp/tools/decay_tools.py`
- Create: `tests/unit/mcp/tools/test_decay_tools.py`
- Modify: `src/brain_v42/mcp/tools/__init__.py`

- [ ] **Step 1: Write failing tests**

Tests for:
- `brain_decay_status` — returns `{"fresh": N, "stale": N, "archived": N}` per entity type
- `brain_refresh_entity` — sets freshness_status='fresh', updates last_accessed_at=NOW
- Error handling: unknown entity_type, entity not found

- [ ] **Step 2: Run tests — verify fail**

Run: `uv run pytest tests/unit/mcp/tools/test_decay_tools.py -v`

- [ ] **Step 3: Implement decay_tools.py**

Follow project_context_tools pattern:
- `register_decay_tools(mcp, session_factory, decay_calculator)`
- `brain_decay_status()` — queries COUNT(*) GROUP BY freshness_status for each entity type. Also flags **deletion candidates**: entities archived for 180+ days with `access_count = 0` since archival.
- `brain_refresh_entity(entity_type, entity_id)` — UPDATE SET freshness_status='fresh', last_accessed_at=NOW()

- [ ] **Step 4: Run tests — verify pass**

Run: `uv run pytest tests/unit/mcp/tools/test_decay_tools.py -v`

- [ ] **Step 5: Update __init__.py barrel export**

Add `register_decay_tools` to the barrel file.

- [ ] **Step 6: Commit**

```bash
git add src/brain_v42/mcp/tools/decay_tools.py src/brain_v42/mcp/tools/__init__.py tests/unit/mcp/tools/test_decay_tools.py
git commit -m "feat(decay): add brain_decay_status + brain_refresh_entity MCP tools"
```

---

### Task 11: Consolidation — ConsolidationJob + MCP tools

**Files:**
- Create: `src/brain_v42/services/consolidation.py`
- Create: `src/brain_v42/repositories/pg_consolidation_log.py`
- Create: `tests/unit/services/test_consolidation.py`
- Create: `tests/unit/repositories/test_pg_consolidation_log.py`
- Modify: `src/brain_v42/mcp/tools/decay_tools.py` (add consolidation tools)
- Modify: `tests/unit/mcp/tools/test_decay_tools.py` (add consolidation tool tests)

- [ ] **Step 1: Write failing tests for PgConsolidationLogRepo**

Tests for:
- `get_handled_pairs(entity_type)` — returns set of (source_id, target_id) tuples
- `log_action(source_id, target_id, entity_type, similarity, action)` — inserts record

- [ ] **Step 2: Run tests — verify fail**

- [ ] **Step 3: Implement PgConsolidationLogRepo**

- [ ] **Step 4: Run tests — verify pass**

- [ ] **Step 5: Write failing tests for ConsolidationJob**

Tests for:
- `find_candidates(entity_type)` — returns list of `(id_a, id_b, similarity)` tuples
- Excludes already-handled pairs from consolidation_log
- Uses SQL self-join with cosine similarity > threshold

- [ ] **Step 6: Run tests — verify fail**

- [ ] **Step 7: Implement ConsolidationJob**

```python
class ConsolidationJob:
    def __init__(self, session_factory, consolidation_log_repo, threshold=0.92):
        ...

    async def find_candidates(self, entity_type: str) -> list[tuple[UUID, UUID, float]]:
        """Find near-duplicate pairs using pgvector self-join."""
        ...
```

SQL pattern:
```sql
SELECT a.id, b.id, 1 - (a.embedding <=> b.embedding) AS similarity
FROM {table} a, {table} b
WHERE a.id < b.id
  AND a.project_key IS NOT DISTINCT FROM b.project_key
  AND a.freshness_status != 'archived'
  AND b.freshness_status != 'archived'
  AND a.merged_into IS NULL
  AND b.merged_into IS NULL
  AND 1 - (a.embedding <=> b.embedding) > :threshold
```

Use async session directly (project pattern). The job scheduling wrapper (`_run_loop`) uses `asyncio.sleep` between runs — no `asyncio.to_thread` needed since all DB ops are already async.

- [ ] **Step 8: Run tests — verify pass**

- [ ] **Step 9: Add brain_consolidation_candidates + brain_merge_entities to decay_tools.py**

- `brain_consolidation_candidates(entity_type=None, limit=20)` — calls ConsolidationJob.find_candidates, returns preview
- `brain_merge_entities(entity_type, source_id, target_id)` — merges source into target, marks source archived with merged_into

- [ ] **Step 10: Write tests for new MCP tools — verify pass**

- [ ] **Step 11: Commit**

```bash
git add src/brain_v42/services/consolidation.py src/brain_v42/repositories/pg_consolidation_log.py src/brain_v42/mcp/tools/decay_tools.py tests/
git commit -m "feat(decay): add ConsolidationJob + brain_consolidation_candidates + brain_merge_entities"
```

---

## Batch 5: Wiring + Metrics (sequential — depends on all above)

### Task 12: Sidecar metrics — decay gauges

**Files:**
- Modify: `src/brain_v42/metrics/collector.py`
- Modify: `tests/unit/test_metrics_instrument.py` (or test_metrics_collector.py)

- [ ] **Step 1: Write failing tests**

Tests for:
- `record_decay_stats(stale_count, archived_count, access_log_size)` stores values
- `get_flush_data()` includes decay stats

- [ ] **Step 2: Run tests — verify fail**

- [ ] **Step 3: Add decay stats to MetricsCollector**

Add `_decay_stats` dict with `stale_count`, `archived_count`, `access_log_size` gauges.
Include in `get_flush_data()` output.

- [ ] **Step 4: Run tests — verify pass**

- [ ] **Step 5: Commit**

```bash
git add src/brain_v42/metrics/collector.py tests/
git commit -m "feat(decay): add decay gauges to MetricsCollector"
```

---

### Task 13: Server wiring — connect everything

**Files:**
- Modify: `src/brain_v42/mcp/server.py`

- [ ] **Step 1: Wire in server.py startup**

In `create_server()` or equivalent:
1. Create `DecayCalculator(stale_threshold=settings.stale_threshold, archive_threshold=settings.archive_threshold)`
2. Create `AccessLogger(session_factory)` → `await access_logger.start()`
3. Create `PgAccessLogRepo(session_factory)`
4. Create `DecayFlusher(session_factory, access_log_repo, decay_calculator, interval=settings.decay_flush_interval_seconds)`
5. If `settings.decay_enabled`: `await decay_flusher.start()`
6. Create `ConsolidationJob(session_factory, consolidation_log_repo, threshold=settings.consolidation_similarity_threshold)`
7. Pass `decay_calculator` and `access_logger` to `BrainService`
8. Call `register_decay_tools(mcp, session_factory, decay_calculator, consolidation_job)`
9. On shutdown: `await access_logger.stop()`, `await decay_flusher.stop()`

- [ ] **Step 2: Run full unit test suite**

Run: `uv run pytest tests/unit -q`
Expected: ALL PASS (no regressions)

- [ ] **Step 3: Run ruff on all modified files**

Run: `uv run ruff check src/ tests/ && uv run ruff format src/ tests/`

- [ ] **Step 4: Commit**

```bash
git add src/brain_v42/mcp/server.py
git commit -m "feat(decay): wire DecayFlusher, AccessLogger, ConsolidationJob in server startup"
```

---

## Batch 6: Migrations + Integration Test

### Task 14: Apply migrations + smoke test

- [ ] **Step 1: Apply migrations on dev DB**

```bash
uv run alembic upgrade head
```

- [ ] **Step 2: Run full test suite**

```bash
uv run pytest tests/unit -q
uv run pytest tests/integration -q
```

- [ ] **Step 3: Run ruff + format on entire codebase**

```bash
uv run ruff check src/ tests/ && uv run ruff format --check src/ tests/
```

- [ ] **Step 4: Final commit + push**

```bash
git add -A
git commit -m "feat(decay): memory decay, lifecycle management, and consolidation — complete"
git push origin main
```

---

## Dependency Graph

```
Batch 1 (Tasks 1-4): Foundation — migrations + config
    ↓
Batch 2 (Tasks 5-7): DecayCalculator + AccessLogger + AccessLogRepo [parallelizable]
    ↓
Batch 3 (Tasks 8-9): DecayFlusher + Search Integration [sequential]
    ↓
Batch 4 (Tasks 10-11): MCP Tools + Consolidation [parallelizable]
    ↓
Batch 5 (Tasks 12-13): Metrics + Server Wiring [sequential]
    ↓
Batch 6 (Task 14): Migrations + Integration
```
