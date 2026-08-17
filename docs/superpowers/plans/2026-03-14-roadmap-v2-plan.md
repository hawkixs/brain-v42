# Roadmap V2 — Event-Driven Feature Tracking — Implementation Plan

> **For agentic workers:** REQUIRED: Use superpowers:subagent-driven-development (if subagents available) or superpowers:executing-plans to implement this plan. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Transform roadmap tracking from manual seed+update to event-driven auto-creation, auto-linking, and auto-status via ClusterGuard, PlanIndexer, GitLabIngestor, and StatusEngine.

**Architecture:** Three signal sources (MCP artifacts, plan files, GitLab webhooks) feed through a shared ClusterGuard (cosine pre-filter + cross-encoder reranker) to auto-create/link/merge features, with a monotonic StatusEngine managing lifecycle. A lightweight cross-encoder CPU service (port 8004) handles disambiguation.

**Tech Stack:** Python 3.12+, FastMCP, SQLAlchemy 2.0 async, asyncpg, pgvector, httpx, aiohttp, sentence-transformers (CrossEncoder), Pydantic 2, structlog, pytest

**Spec:** `docs/superpowers/specs/2026-03-14-roadmap-v2-design.md`

---

## Chunk 1: Database + Models + Reranker Client

Foundation layer: migration 009, updated table definitions, updated Pydantic models, and the reranker HTTP client.

---

### Task 1: Alembic Migration 009

**Files:**
- Create: `alembic/versions/009_roadmap_v2.py`

- [ ] **Step 1: Write the migration file**

```python
"""009 — Roadmap V2: pinned features, plan indexing, gitlab events."""

from alembic import op

revision = "009"
down_revision = "008"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # 1. Features: add pinned flag
    op.execute("ALTER TABLE features ADD COLUMN pinned BOOLEAN DEFAULT FALSE")

    # 2. Project contexts: add config fields
    op.execute("ALTER TABLE project_contexts ADD COLUMN plan_scan_paths JSONB DEFAULT '[]'")
    op.execute("ALTER TABLE project_contexts ADD COLUMN gitlab_project_path VARCHAR(200)")

    # 3. New table: indexed_plans
    op.execute("""
        CREATE TABLE indexed_plans (
            id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            file_path VARCHAR(500) UNIQUE NOT NULL,
            title VARCHAR(200) NOT NULL,
            plan_type VARCHAR(20) NOT NULL CHECK (plan_type IN ('spec', 'plan')),
            project_key VARCHAR(50) NOT NULL,
            content_hash VARCHAR(64) NOT NULL,
            embedding VECTOR(1536),
            created_at TIMESTAMPTZ DEFAULT NOW(),
            updated_at TIMESTAMPTZ DEFAULT NOW()
        )
    """)
    op.execute("CREATE INDEX idx_indexed_plans_project ON indexed_plans(project_key)")
    op.execute("""
        CREATE TRIGGER set_indexed_plans_updated_at
            BEFORE UPDATE ON indexed_plans
            FOR EACH ROW EXECUTE FUNCTION update_updated_at()
    """)

    # 4. New table: gitlab_events (append-only)
    op.execute("""
        CREATE TABLE gitlab_events (
            id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            gitlab_event_id VARCHAR(100) UNIQUE,
            event_type VARCHAR(30) NOT NULL,
            project_key VARCHAR(50) NOT NULL,
            gitlab_project_id INTEGER,
            ref VARCHAR(200),
            title VARCHAR(500),
            embedding VECTOR(1536),
            feature_id UUID REFERENCES features(id),
            processed_at TIMESTAMPTZ DEFAULT NOW()
        )
    """)
    op.execute("CREATE INDEX idx_gitlab_events_project ON gitlab_events(project_key)")
    op.execute("CREATE INDEX idx_gitlab_events_feature ON gitlab_events(feature_id)")

    # 5. Feature artifacts: add new artifact types
    op.execute("ALTER TABLE feature_artifacts DROP CONSTRAINT IF EXISTS feature_artifacts_artifact_type_check")
    op.execute("""
        ALTER TABLE feature_artifacts ADD CONSTRAINT feature_artifacts_artifact_type_check
            CHECK (artifact_type IN ('learning', 'decision', 'snippet', 'runbook', 'adr', 'plan', 'gitlab_event'))
    """)


def downgrade() -> None:
    op.execute("ALTER TABLE feature_artifacts DROP CONSTRAINT IF EXISTS feature_artifacts_artifact_type_check")
    op.execute("""
        ALTER TABLE feature_artifacts ADD CONSTRAINT feature_artifacts_artifact_type_check
            CHECK (artifact_type IN ('learning', 'decision', 'snippet', 'runbook', 'adr'))
    """)
    op.execute("DROP TABLE IF EXISTS gitlab_events CASCADE")
    op.execute("DROP TABLE IF EXISTS indexed_plans CASCADE")
    op.execute("ALTER TABLE project_contexts DROP COLUMN IF EXISTS gitlab_project_path")
    op.execute("ALTER TABLE project_contexts DROP COLUMN IF EXISTS plan_scan_paths")
    op.execute("ALTER TABLE features DROP COLUMN IF EXISTS pinned")
```

- [ ] **Step 2: Verify migration applies cleanly**

Run: `cd /home/hawixs/hawkixs_infra/git_repo/brain_v42 && source .venv/bin/activate && alembic upgrade head`
Expected: Migration 009 applies without errors.

- [ ] **Step 3: Verify rollback works**

Run: `alembic downgrade -1 && alembic upgrade head`
Expected: Clean downgrade/upgrade cycle.

- [ ] **Step 4: Commit**

```bash
git add alembic/versions/009_roadmap_v2.py
git commit -m "feat(roadmap-v2): add migration 009 — pinned, indexed_plans, gitlab_events"
```

---

### Task 2: Update SQLAlchemy Table Definitions

**Files:**
- Modify: `src/brain_v42/db/tables.py`

- [ ] **Step 1: Add `pinned` column to `features` table definition**

In `tables.py`, add `Column("pinned", Boolean, server_default=sa.text("false"))` to the `features` table.

- [ ] **Step 2: Add `plan_scan_paths` and `gitlab_project_path` to `project_contexts` table**

Add `Column("plan_scan_paths", JSONB, server_default=sa.text("'[]'"))` and `Column("gitlab_project_path", String(200))` to `project_contexts`.

- [ ] **Step 3: Add `indexed_plans` table definition**

```python
indexed_plans = Table(
    "indexed_plans",
    METADATA,
    Column("id", UUID, primary_key=True, server_default=sa.text("gen_random_uuid()")),
    Column("file_path", String(500), unique=True, nullable=False),
    Column("title", String(200), nullable=False),
    Column("plan_type", String(20), nullable=False),
    Column("project_key", String(50), nullable=False),
    Column("content_hash", String(64), nullable=False),
    Column("embedding", Vector(_EMBEDDING_DIM)),
    Column("created_at", DateTime(timezone=True), server_default=sa.text("NOW()")),
    Column("updated_at", DateTime(timezone=True), server_default=sa.text("NOW()")),
)
```

- [ ] **Step 4: Add `gitlab_events` table definition**

```python
gitlab_events = Table(
    "gitlab_events",
    METADATA,
    Column("id", UUID, primary_key=True, server_default=sa.text("gen_random_uuid()")),
    Column("gitlab_event_id", String(100), unique=True),
    Column("event_type", String(30), nullable=False),
    Column("project_key", String(50), nullable=False),
    Column("gitlab_project_id", Integer),
    Column("ref", String(200)),
    Column("title", String(500)),
    Column("embedding", Vector(_EMBEDDING_DIM)),
    Column("feature_id", UUID, ForeignKey("features.id")),
    Column("processed_at", DateTime(timezone=True), server_default=sa.text("NOW()")),
)
```

- [ ] **Step 5: Commit**

```bash
git add src/brain_v42/db/tables.py
git commit -m "feat(roadmap-v2): add indexed_plans, gitlab_events, pinned to table defs"
```

---

### Task 3: Update Pydantic Models

**Files:**
- Modify: `src/brain_v42/models/feature.py`
- Modify: `src/brain_v42/models/project_context.py`
- Create: `src/brain_v42/models/indexed_plan.py`
- Create: `src/brain_v42/models/gitlab_event.py`

- [ ] **Step 1: Add `pinned` to Feature model + update RoadmapFeature**

In `models/feature.py`:
- Add `pinned: bool = False` to `Feature` class
- Add `pinned: bool` to `RoadmapFeature` class

- [ ] **Step 2: Add config fields to ProjectContext models**

In `models/project_context.py`:
- Add `plan_scan_paths: list[str] = Field(default_factory=list)` to `ProjectContextBase`
- Add `gitlab_project_path: str | None = None` to `ProjectContextBase`
- Add same fields to `ProjectContextUpdate` (as Optional)

- [ ] **Step 3: Create IndexedPlan model**

```python
"""Pydantic models for indexed plan files."""
from __future__ import annotations

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, Field


class IndexedPlanCreate(BaseModel):
    file_path: str
    title: str
    plan_type: str  # "spec" | "plan"
    project_key: str
    content_hash: str


class IndexedPlan(BaseModel):
    model_config = {"from_attributes": True}

    id: UUID
    file_path: str
    title: str
    plan_type: str
    project_key: str
    content_hash: str
    created_at: datetime
    updated_at: datetime
```

- [ ] **Step 4: Create GitLabEvent model**

```python
"""Pydantic models for GitLab webhook events."""
from __future__ import annotations

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel


class GitLabEventCreate(BaseModel):
    gitlab_event_id: str
    event_type: str  # "push", "mr_opened", "mr_merged", "pipeline_success", "pipeline_failure"
    project_key: str
    gitlab_project_id: int | None = None
    ref: str | None = None
    title: str | None = None


class GitLabEvent(BaseModel):
    model_config = {"from_attributes": True}

    id: UUID
    gitlab_event_id: str
    event_type: str
    project_key: str
    gitlab_project_id: int | None
    ref: str | None
    title: str | None
    feature_id: UUID | None
    processed_at: datetime
```

- [ ] **Step 5: Commit**

```bash
git add src/brain_v42/models/feature.py src/brain_v42/models/project_context.py \
        src/brain_v42/models/indexed_plan.py src/brain_v42/models/gitlab_event.py
git commit -m "feat(roadmap-v2): update Feature/ProjectContext models, add IndexedPlan/GitLabEvent"
```

---

### Task 4: Reranker HTTP Client

**Files:**
- Create: `src/brain_v42/services/reranker_client.py`
- Create: `tests/unit/test_reranker_client.py`

- [ ] **Step 1: Write failing tests for RerankerClient**

```python
"""Tests for RerankerClient."""
import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from brain_v42.services.reranker_client import RerankerClient


@pytest.fixture
def client():
    return RerankerClient(base_url="http://localhost:8004")


@pytest.mark.asyncio
async def test_rerank_returns_scores(client):
    mock_response = MagicMock()  # httpx.Response.json() is sync, not async
    mock_response.json.return_value = {"scores": [0.92, 0.15, 0.08]}
    mock_response.raise_for_status = MagicMock()

    with patch.object(client, "_get_client") as mock_get:
        mock_http = AsyncMock()
        mock_http.post.return_value = mock_response
        mock_get.return_value = mock_http

        scores = await client.rerank(
            query="Memory decay system",
            candidates=["Memory Decay", "Hybrid Search", "Knowledge Graph"],
        )
        assert scores == [0.92, 0.15, 0.08]


@pytest.mark.asyncio
async def test_rerank_returns_empty_on_no_candidates(client):
    scores = await client.rerank(query="test", candidates=[])
    assert scores == []


@pytest.mark.asyncio
async def test_is_available_returns_false_on_connection_error(client):
    with patch.object(client, "_get_client") as mock_get:
        mock_http = AsyncMock()
        import httpx
        mock_http.get.side_effect = httpx.ConnectError("refused")
        mock_get.return_value = mock_http

        result = await client.is_available()
        assert result is False
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/unit/test_reranker_client.py -v`
Expected: FAIL (module not found)

- [ ] **Step 3: Implement RerankerClient**

```python
"""HTTP client for cross-encoder reranker service (port 8004, CPU)."""
from __future__ import annotations

import httpx
import structlog

logger = structlog.get_logger(__name__)


class RerankerClient:
    """Lazy async HTTP client for the cross-encoder reranker service.

    Follows the same pattern as GPUEmbeddingService:
    lazy client creation, retry with backoff, graceful fallback.
    """

    def __init__(
        self,
        base_url: str = "http://localhost:8004",
        timeout: float = 10.0,
    ) -> None:
        self._base_url = base_url
        self._timeout = timeout
        self._client: httpx.AsyncClient | None = None

    def _get_client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(
                base_url=self._base_url,
                timeout=self._timeout,
            )
        return self._client

    async def rerank(
        self, query: str, candidates: list[str]
    ) -> list[float]:
        """Score candidates against query via cross-encoder.

        Returns list of scores in same order as candidates.
        Returns empty list if candidates is empty.
        Raises on HTTP/connection errors (caller handles fallback).
        """
        if not candidates:
            return []
        client = self._get_client()
        response = await client.post(
            "/rerank",
            json={"query": query, "candidates": candidates},
        )
        response.raise_for_status()
        return response.json()["scores"]

    async def is_available(self) -> bool:
        """Health check — returns False if service unreachable."""
        try:
            client = self._get_client()
            response = await client.get("/health")
            return response.status_code == 200
        except (httpx.ConnectError, httpx.TimeoutException):
            return False

    async def close(self) -> None:
        if self._client:
            await self._client.aclose()
            self._client = None
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/unit/test_reranker_client.py -v`
Expected: 3 PASSED

- [ ] **Step 5: Commit**

```bash
git add src/brain_v42/services/reranker_client.py tests/unit/test_reranker_client.py
git commit -m "feat(roadmap-v2): add RerankerClient with lazy httpx + health check"
```

---

### Task 5: Update Config (Settings)

**Files:**
- Modify: `src/brain_v42/config.py`

- [ ] **Step 1: Add reranker and GitLab webhook settings**

Add to `Settings` class:
```python
# Reranker
reranker_url: str = "http://localhost:8004"
reranker_timeout: float = 10.0

# GitLab webhooks
gitlab_webhook_secret: str = ""
```

- [ ] **Step 2: Commit**

```bash
git add src/brain_v42/config.py
git commit -m "feat(roadmap-v2): add reranker + gitlab webhook settings"
```

---

## Chunk 2: StatusEngine + ClusterGuard

Core logic: pure status heuristic and the anti-duplication resolver.

---

### Task 6: StatusEngine

**Files:**
- Create: `src/brain_v42/services/status_engine.py`
- Create: `tests/unit/test_status_engine.py`

- [ ] **Step 1: Write failing tests**

```python
"""Tests for StatusEngine — pure logic, no I/O."""
import pytest
from brain_v42.services.status_engine import StatusEngine

engine = StatusEngine()


@pytest.mark.parametrize(
    "current_status, signal_type, pinned, expected",
    [
        # Basic progression
        ("planned", "learning", False, "research"),
        ("planned", "decision", False, "research"),
        ("research", "plan", False, "design"),
        ("design", "mr_opened", False, "building"),
        ("building", "mr_merged", False, "deployed"),
        # Monotonic: never go backward
        ("deployed", "learning", False, "deployed"),
        ("building", "decision", False, "building"),
        ("design", "learning", False, "design"),
        # Pinned: no change
        ("research", "mr_merged", True, "research"),
        ("planned", "plan", True, "planned"),
        # No-op signals
        ("planned", "push", False, "planned"),
        ("building", "pipeline_failure", False, "building"),
        # Snippet and runbook/adr mapping
        ("planned", "snippet", False, "research"),
        ("planned", "runbook", False, "design"),
        ("planned", "adr", False, "design"),
        # Pipeline success
        ("building", "pipeline_success", False, "deployed"),
    ],
)
def test_compute_status(current_status, signal_type, pinned, expected):
    result = engine.compute_status(current_status, signal_type, pinned)
    assert result == expected


def test_status_order_complete():
    """All valid statuses must be in STATUS_ORDER."""
    assert StatusEngine.STATUS_ORDER == [
        "planned", "research", "design", "building", "deployed", "done"
    ]
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/unit/test_status_engine.py -v`
Expected: FAIL (module not found)

- [ ] **Step 3: Implement StatusEngine**

```python
"""StatusEngine — monotonic status heuristic for features."""
from __future__ import annotations

import structlog

logger = structlog.get_logger(__name__)


class StatusEngine:
    """Compute feature status from signal types. Pure logic, no I/O."""

    STATUS_ORDER: list[str] = [
        "planned", "research", "design", "building", "deployed", "done",
    ]

    SIGNAL_STATUS_MAP: dict[str, str | None] = {
        "learning": "research",
        "decision": "research",
        "snippet": "research",
        "runbook": "design",
        "adr": "design",
        "plan": "design",
        "mr_opened": "building",
        "push": None,
        "mr_merged": "deployed",
        "pipeline_success": "deployed",
        "pipeline_failure": None,
    }

    def compute_status(
        self, current_status: str, signal_type: str, pinned: bool
    ) -> str:
        """Return the new status. Never goes backward. Respects pinned."""
        if pinned:
            return current_status

        proposed = self.SIGNAL_STATUS_MAP.get(signal_type)
        if proposed is None:
            return current_status

        current_idx = self.STATUS_ORDER.index(current_status)
        proposed_idx = self.STATUS_ORDER.index(proposed)
        if proposed_idx > current_idx:
            return proposed
        return current_status
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/unit/test_status_engine.py -v`
Expected: 17 PASSED (all parametrized cases)

- [ ] **Step 5: Commit**

```bash
git add src/brain_v42/services/status_engine.py tests/unit/test_status_engine.py
git commit -m "feat(roadmap-v2): add StatusEngine — monotonic status heuristic"
```

---

### Task 7: ClusterGuard

**Files:**
- Create: `src/brain_v42/services/cluster_guard.py`
- Create: `tests/unit/test_cluster_guard.py`

- [ ] **Step 1: Write failing tests**

```python
"""Tests for ClusterGuard — mock DB + reranker."""
import uuid
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest
import sqlalchemy as sa

from brain_v42.services.cluster_guard import ClusterGuard


@pytest.fixture
def mock_deps():
    session = AsyncMock()
    factory = MagicMock()
    factory.return_value.__aenter__ = AsyncMock(return_value=session)
    factory.return_value.__aexit__ = AsyncMock(return_value=False)
    embedding_svc = AsyncMock()
    reranker = AsyncMock()
    status_engine = MagicMock()
    return factory, session, embedding_svc, reranker, status_engine


def _make_feature_row(name: str, score: float):
    """Helper to build a mock DB row for cosine query results."""
    return MagicMock(
        id=uuid.uuid4(),
        name=name,
        description=f"desc for {name}",
        status="planned",
        pinned=False,
        project_key="test",
        status_updated_at=datetime.now(timezone.utc),
        similarity=score,
    )


@pytest.mark.asyncio
async def test_resolve_links_when_high_cosine(mock_deps):
    """Cosine >= 0.70 → linked, skip reranker."""
    factory, session, embedding_svc, reranker, status_engine = mock_deps
    row = _make_feature_row("Memory Decay", 0.85)
    session.execute.return_value = MagicMock(fetchall=MagicMock(return_value=[row]))
    status_engine.compute_status.return_value = "research"

    guard = ClusterGuard(factory, embedding_svc, reranker, status_engine)
    feature, action = await guard.resolve(
        text="memory decay system",
        embedding=[0.1] * 1536,
        project_key="test",
        signal_type="learning",
    )
    assert action == "linked"
    assert feature.name == "Memory Decay"
    reranker.rerank.assert_not_called()


@pytest.mark.asyncio
async def test_resolve_creates_when_low_cosine(mock_deps):
    """Cosine < 0.50 → created, skip reranker."""
    factory, session, embedding_svc, reranker, status_engine = mock_deps
    session.execute.return_value = MagicMock(fetchall=MagicMock(return_value=[]))
    status_engine.compute_status.return_value = "research"
    # Mock the INSERT returning a new feature
    insert_result = MagicMock()
    insert_result.fetchone.return_value = MagicMock(
        id=uuid.uuid4(),
        name="New Feature",
        status="research",
        pinned=False,
    )
    session.execute.side_effect = [
        MagicMock(fetchall=MagicMock(return_value=[])),  # cosine query
        insert_result,  # INSERT
    ]

    guard = ClusterGuard(factory, embedding_svc, reranker, status_engine)
    feature, action = await guard.resolve(
        text="New Feature",
        embedding=[0.1] * 1536,
        project_key="test",
        signal_type="learning",
    )
    assert action == "created"
    reranker.rerank.assert_not_called()


@pytest.mark.asyncio
async def test_resolve_uses_reranker_in_grey_zone(mock_deps):
    """Cosine 0.50-0.70 → calls reranker to decide."""
    factory, session, embedding_svc, reranker, status_engine = mock_deps
    row = _make_feature_row("Decay System", 0.60)
    session.execute.return_value = MagicMock(fetchall=MagicMock(return_value=[row]))
    reranker.rerank.return_value = [0.80]  # high reranker score → linked
    reranker.is_available.return_value = True
    status_engine.compute_status.return_value = "research"

    guard = ClusterGuard(factory, embedding_svc, reranker, status_engine)
    feature, action = await guard.resolve(
        text="active forgetting",
        embedding=[0.1] * 1536,
        project_key="test",
        signal_type="learning",
    )
    assert action == "linked"
    reranker.rerank.assert_called_once()


@pytest.mark.asyncio
async def test_resolve_falls_back_when_reranker_down(mock_deps):
    """Reranker unavailable → cosine-only fallback."""
    factory, session, embedding_svc, reranker, status_engine = mock_deps
    row = _make_feature_row("Decay System", 0.65)
    session.execute.return_value = MagicMock(fetchall=MagicMock(return_value=[row]))
    reranker.is_available.return_value = False
    status_engine.compute_status.return_value = "research"

    guard = ClusterGuard(factory, embedding_svc, reranker, status_engine)
    feature, action = await guard.resolve(
        text="decay",
        embedding=[0.1] * 1536,
        project_key="test",
        signal_type="learning",
    )
    # 0.65 >= 0.65 fallback threshold → linked
    assert action == "linked"
    reranker.rerank.assert_not_called()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/unit/test_cluster_guard.py -v`
Expected: FAIL (module not found)

- [ ] **Step 3: Implement ClusterGuard**

```python
"""ClusterGuard — anti-duplication resolver for feature signals."""
from __future__ import annotations

from datetime import datetime, timezone
from typing import TYPE_CHECKING, Literal
from uuid import UUID

import sqlalchemy as sa
import structlog
from sqlalchemy.dialects.postgresql import insert as pg_insert

from sqlalchemy.ext.asyncio import AsyncSession

from brain_v42.db.tables import features

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import async_sessionmaker

    from brain_v42.services.reranker_client import RerankerClient
    from brain_v42.services.status_engine import StatusEngine

logger = structlog.get_logger(__name__)

# Thresholds
COSINE_LINK = 0.70
COSINE_GREY_LOW = 0.50
RERANKER_LINK = 0.75
RERANKER_MERGE = 0.50
FALLBACK_LINK = 0.65


class ClusterGuard:
    """Resolve a signal to a feature: link, merge, or create."""

    def __init__(
        self,
        session_factory: async_sessionmaker,
        embedding_svc: object,
        reranker: RerankerClient,
        status_engine: StatusEngine,
    ) -> None:
        self._sf = session_factory
        self._embedding_svc = embedding_svc
        self._reranker = reranker
        self._status_engine = status_engine

    async def resolve(
        self,
        text: str,
        embedding: list[float],
        project_key: str,
        signal_type: str,
    ) -> tuple[object, Literal["linked", "merged", "created"]]:
        """Resolve signal to a feature.

        Returns (feature_row, action) where action is one of:
        - "linked": matched existing feature
        - "merged": enriched existing feature description + re-embedded
        - "created": new feature created
        """
        async with self._sf() as session:
            # 1. Cosine top-5
            candidates = await self._cosine_top_n(session, embedding, project_key, n=5)

            if not candidates:
                return await self._create_feature(
                    session, text, embedding, project_key, signal_type
                ), "created"

            best = candidates[0]

            # 2. High cosine → direct link
            if best.similarity >= COSINE_LINK:
                await self._update_status(session, best, signal_type)
                return best, "linked"

            # 3. Low cosine → create directly
            if best.similarity < COSINE_GREY_LOW:
                return await self._create_feature(
                    session, text, embedding, project_key, signal_type
                ), "created"

            # 4. Grey zone → reranker
            try:
                reranker_available = await self._reranker.is_available()
            except Exception:
                reranker_available = False

            if not reranker_available:
                # Fallback: cosine only
                if best.similarity >= FALLBACK_LINK:
                    await self._update_status(session, best, signal_type)
                    return best, "linked"
                return await self._create_feature(
                    session, text, embedding, project_key, signal_type
                ), "created"

            # Call reranker
            candidate_texts = [f"{c.name}: {c.description}" for c in candidates]
            scores = await self._reranker.rerank(query=text, candidates=candidate_texts)
            best_idx = scores.index(max(scores))
            best_score = scores[best_idx]
            best_candidate = candidates[best_idx]

            if best_score >= RERANKER_LINK:
                await self._update_status(session, best_candidate, signal_type)
                return best_candidate, "linked"
            elif best_score >= RERANKER_MERGE:
                merged = await self._merge_feature(
                    session, best_candidate, text, signal_type
                )
                return merged, "merged"
            else:
                return await self._create_feature(
                    session, text, embedding, project_key, signal_type
                ), "created"

    async def _cosine_top_n(
        self, session: AsyncSession, embedding: list[float], project_key: str, n: int = 5
    ) -> list:
        """Find top-N features by cosine similarity using SQLAlchemy Core."""
        similarity = (1 - features.c.embedding.cosine_distance(embedding)).label("similarity")
        stmt = (
            sa.select(
                features.c.id,
                features.c.name,
                features.c.description,
                features.c.status,
                features.c.pinned,
                features.c.project_key,
                features.c.status_updated_at,
                similarity,
            )
            .where(features.c.project_key == project_key)
            .where(features.c.embedding.is_not(None))
            .order_by(features.c.embedding.cosine_distance(embedding))
            .limit(n)
        )
        result = await session.execute(stmt)
        rows = result.fetchall()
        return [r for r in rows if r.similarity >= COSINE_GREY_LOW]

    async def _create_feature(
        self, session: AsyncSession, text: str, embedding: list[float],
        project_key: str, signal_type: str,
    ) -> object:
        """Create a new feature from signal text."""
        name = text[:200]  # Truncate for name column
        status = self._status_engine.compute_status("planned", signal_type, False)
        now = datetime.now(timezone.utc)

        stmt = (
            pg_insert(features)
            .values(
                project_key=project_key,
                name=name,
                description=text,
                embedding=embedding,
                status=status,
                status_updated_at=now,
                pinned=False,
            )
            .returning(*features.c)
        )
        result = await session.execute(stmt)
        row = result.fetchone()
        await session.commit()
        logger.info("cluster_guard.feature_created", name=name, project_key=project_key)
        return row

    async def _merge_feature(
        self, session: AsyncSession, feature: object, new_text: str, signal_type: str,
    ) -> object:
        """Enrich feature description and re-embed."""
        updated_desc = f"{feature.description}\n---\n{new_text}"
        new_embedding = await self._embedding_svc.embed(updated_desc[:2000])
        new_status = self._status_engine.compute_status(
            feature.status, signal_type, feature.pinned
        )
        now = datetime.now(timezone.utc)

        update_values: dict = {
            "description": updated_desc[:5000],
            "embedding": new_embedding,
        }
        if new_status != feature.status:
            update_values["status"] = new_status
            update_values["status_updated_at"] = now

        await session.execute(
            sa.update(features)
            .where(features.c.id == feature.id)
            .values(**update_values)
        )
        await session.commit()
        logger.info("cluster_guard.feature_merged", name=feature.name)
        # Re-fetch to get updated row
        result = await session.execute(
            sa.select(features).where(features.c.id == feature.id)
        )
        return result.fetchone()

    async def _update_status(
        self, session: AsyncSession, feature: object, signal_type: str,
    ) -> None:
        """Update feature status if signal warrants progression."""
        new_status = self._status_engine.compute_status(
            feature.status, signal_type, feature.pinned
        )
        if new_status != feature.status:
            now = datetime.now(timezone.utc)
            await session.execute(
                sa.update(features)
                .where(features.c.id == feature.id)
                .values(status=new_status, status_updated_at=now)
            )
            await session.commit()
            logger.info(
                "cluster_guard.status_updated",
                feature=feature.name,
                old=feature.status,
                new=new_status,
            )
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/unit/test_cluster_guard.py -v`
Expected: 4 PASSED

- [ ] **Step 5: Run ruff**

Run: `ruff check src/brain_v42/services/cluster_guard.py src/brain_v42/services/status_engine.py && ruff format src/brain_v42/services/ tests/unit/`

- [ ] **Step 6: Commit**

```bash
git add src/brain_v42/services/cluster_guard.py tests/unit/test_cluster_guard.py
git commit -m "feat(roadmap-v2): add ClusterGuard — cosine + reranker anti-duplication"
```

---

## Chunk 3: FeatureLinker Enhancement + PlanIndexer

Wire ClusterGuard into the existing FeatureLinker and add the PlanIndexer.

---

### Task 8: Enhance FeatureLinker to Use ClusterGuard

**Files:**
- Modify: `src/brain_v42/services/feature_linker.py`
- Modify: `tests/unit/test_feature_linker.py`

- [ ] **Step 1: Write new failing tests**

Add to `tests/unit/test_feature_linker.py`:

```python
@pytest.mark.asyncio
async def test_link_artifact_delegates_to_cluster_guard(mock_session_factory):
    """When cluster_guard is provided, delegate to it instead of raw SQL."""
    factory, session = mock_session_factory
    cluster_guard = AsyncMock()
    mock_feature = MagicMock(id=uuid.uuid4(), name="Test Feature")
    cluster_guard.resolve.return_value = (mock_feature, "linked")

    linker = FeatureLinker(session_factory=factory, cluster_guard=cluster_guard)
    result = await linker.link_artifact(
        embedding=[0.1] * 10,
        artifact_type="learning",
        artifact_id=uuid.uuid4(),
        project_key="test",
        title="Some learning title",
    )
    cluster_guard.resolve.assert_called_once()
    assert result >= 0


@pytest.mark.asyncio
async def test_link_artifact_falls_back_to_raw_sql_without_cluster_guard(mock_session_factory):
    """Without cluster_guard, use the existing raw SQL path."""
    factory, session = mock_session_factory
    # Existing behavior
    linker = FeatureLinker(session_factory=factory)
    result = await linker.link_artifact(
        embedding=[0.1] * 10,
        artifact_type="learning",
        artifact_id=uuid.uuid4(),
        project_key="test",
    )
    assert result >= 0
```

- [ ] **Step 2: Run tests to verify new tests fail**

Run: `pytest tests/unit/test_feature_linker.py -v`
Expected: new tests FAIL (cluster_guard param not accepted)

- [ ] **Step 3: Update FeatureLinker to accept ClusterGuard**

Modify `feature_linker.py`:
- Add optional `cluster_guard` parameter to `__init__`
- Add optional `title` parameter to `link_artifact()`
- When `cluster_guard` is set, delegate to `cluster_guard.resolve()` then insert the link
- When not set, use existing raw SQL path (backward compat)

- [ ] **Step 4: Run all feature_linker tests**

Run: `pytest tests/unit/test_feature_linker.py -v`
Expected: ALL PASSED (old + new tests)

- [ ] **Step 5: Commit**

```bash
git add src/brain_v42/services/feature_linker.py tests/unit/test_feature_linker.py
git commit -m "feat(roadmap-v2): enhance FeatureLinker to delegate to ClusterGuard"
```

---

### Task 9: PlanIndexer Service

**Files:**
- Create: `src/brain_v42/services/plan_indexer.py`
- Create: `tests/unit/test_plan_indexer.py`

- [ ] **Step 1: Write failing tests**

```python
"""Tests for PlanIndexer — mock filesystem + embedding service."""
import hashlib
from unittest.mock import AsyncMock, MagicMock, patch, mock_open

import pytest

from brain_v42.services.plan_indexer import PlanIndexer


@pytest.fixture
def mock_deps():
    session = AsyncMock()
    factory = MagicMock()
    factory.return_value.__aenter__ = AsyncMock(return_value=session)
    factory.return_value.__aexit__ = AsyncMock(return_value=False)
    embedding_svc = AsyncMock()
    embedding_svc.embed.return_value = [0.1] * 1536
    cluster_guard = AsyncMock()
    return factory, session, embedding_svc, cluster_guard


@pytest.mark.asyncio
async def test_parse_plan_extracts_title_from_h1(mock_deps):
    factory, session, embedding_svc, cluster_guard = mock_deps
    indexer = PlanIndexer(factory, embedding_svc, cluster_guard)
    content = "# My Feature Design\n\nSome content here"
    title, plan_type = indexer.parse_plan("/docs/specs/2026-03-14-feature-design.md", content)
    assert title == "My Feature Design"
    assert plan_type == "spec"


@pytest.mark.asyncio
async def test_parse_plan_extracts_title_from_frontmatter(mock_deps):
    factory, session, embedding_svc, cluster_guard = mock_deps
    indexer = PlanIndexer(factory, embedding_svc, cluster_guard)
    content = "---\ntitle: Cool Feature\n---\n\n# Implementation\n\nContent"
    title, plan_type = indexer.parse_plan("/docs/plans/2026-03-14-cool-plan.md", content)
    assert title == "Cool Feature"
    assert plan_type == "plan"


@pytest.mark.asyncio
async def test_parse_plan_falls_back_to_filename(mock_deps):
    factory, session, embedding_svc, cluster_guard = mock_deps
    indexer = PlanIndexer(factory, embedding_svc, cluster_guard)
    content = "Just some content without headings"
    title, plan_type = indexer.parse_plan("/docs/specs/2026-03-14-my-thing-design.md", content)
    assert title == "my-thing"
    assert plan_type == "spec"


@pytest.mark.asyncio
async def test_index_skips_unchanged_files(mock_deps, tmp_path):
    """If content_hash matches DB, skip re-indexing."""
    factory, session, embedding_svc, cluster_guard = mock_deps
    content = "# Test\nBody"
    content_hash = hashlib.sha256(content.encode()).hexdigest()

    # Create real file in tmp_path
    spec_file = tmp_path / "test-design.md"
    spec_file.write_text(content)

    # DB returns existing plan with same hash
    existing_row = MagicMock(content_hash=content_hash)
    session.execute.return_value = MagicMock(fetchone=MagicMock(return_value=existing_row))

    indexer = PlanIndexer(factory, embedding_svc, cluster_guard)
    result = await indexer.index_path(str(tmp_path), "test_project")

    assert result["skipped"] == 1
    embedding_svc.embed.assert_not_called()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/unit/test_plan_indexer.py -v`
Expected: FAIL (module not found)

- [ ] **Step 3: Implement PlanIndexer**

```python
"""PlanIndexer — scan superpowers specs/plans, index, and link to features."""
from __future__ import annotations

import hashlib
import re
from pathlib import Path
from typing import TYPE_CHECKING

import sqlalchemy as sa
import structlog
from sqlalchemy.dialects.postgresql import insert as pg_insert

from brain_v42.db.tables import indexed_plans, feature_artifacts

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import async_sessionmaker

    from brain_v42.services.cluster_guard import ClusterGuard

logger = structlog.get_logger(__name__)

_GLOB_PATTERNS = ["**/*-design.md", "**/*-plan.md"]

_FRONTMATTER_RE = re.compile(r"^---\s*\n(.*?)\n---", re.DOTALL)
_TITLE_FM_RE = re.compile(r"^(?:title|name):\s*(.+)$", re.MULTILINE)
_H1_RE = re.compile(r"^#\s+(.+)$", re.MULTILINE)
_DATE_PREFIX_RE = re.compile(r"^\d{4}-\d{2}-\d{2}-")


class PlanIndexer:
    """Scan plan/spec files, embed, and link to features via ClusterGuard."""

    def __init__(
        self,
        session_factory: async_sessionmaker,
        embedding_svc: object,
        cluster_guard: ClusterGuard,
    ) -> None:
        self._sf = session_factory
        self._embedding_svc = embedding_svc
        self._cluster_guard = cluster_guard

    def parse_plan(self, file_path: str, content: str) -> tuple[str, str]:
        """Extract title and plan_type from file content and path.

        Returns (title, plan_type) where plan_type is 'spec' or 'plan'.
        """
        # Determine plan_type from filename
        plan_type = "spec" if "-design.md" in file_path else "plan"

        # Try frontmatter title
        fm_match = _FRONTMATTER_RE.match(content)
        if fm_match:
            title_match = _TITLE_FM_RE.search(fm_match.group(1))
            if title_match:
                return title_match.group(1).strip().strip("'\""), plan_type

        # Try H1 heading
        h1_match = _H1_RE.search(content)
        if h1_match:
            return h1_match.group(1).strip(), plan_type

        # Fallback: filename
        stem = Path(file_path).stem  # e.g. "2026-03-14-my-thing-design"
        stem = _DATE_PREFIX_RE.sub("", stem)  # "my-thing-design"
        stem = stem.removesuffix("-design").removesuffix("-plan")
        return stem, plan_type

    async def index_path(
        self, scan_path: str, project_key: str
    ) -> dict[str, int]:
        """Scan a directory for plan files, index new/changed ones.

        Returns {"indexed": N, "skipped": M, "linked": L}.
        """
        base = Path(scan_path)
        if not base.is_dir():
            logger.warning("plan_indexer.path_not_found", path=scan_path)
            return {"indexed": 0, "skipped": 0, "linked": 0}

        files: list[Path] = []
        for pattern in _GLOB_PATTERNS:
            files.extend(base.glob(pattern))

        indexed = 0
        skipped = 0
        linked = 0

        for file_path in files:
            try:
                content = file_path.read_text(encoding="utf-8")
            except OSError:
                logger.warning("plan_indexer.read_error", path=str(file_path))
                continue

            content_hash = hashlib.sha256(content.encode()).hexdigest()
            path_str = str(file_path.resolve())

            async with self._sf() as session:
                # Check if already indexed with same hash
                existing = await session.execute(
                    sa.select(indexed_plans.c.content_hash).where(
                        indexed_plans.c.file_path == path_str
                    )
                )
                row = existing.fetchone()
                if row and row.content_hash == content_hash:
                    skipped += 1
                    continue

                # Parse and embed
                title, plan_type = self.parse_plan(path_str, content)
                embed_text = f"{title}\n{content[:500]}"
                embedding = await self._embedding_svc.embed(embed_text)

                # Upsert indexed_plans
                stmt = (
                    pg_insert(indexed_plans)
                    .values(
                        file_path=path_str,
                        title=title,
                        plan_type=plan_type,
                        project_key=project_key,
                        content_hash=content_hash,
                        embedding=embedding,
                    )
                    .on_conflict_do_update(
                        index_elements=["file_path"],
                        set_={
                            "title": title,
                            "content_hash": content_hash,
                            "embedding": embedding,
                        },
                    )
                    .returning(indexed_plans.c.id)
                )
                result = await session.execute(stmt)
                plan_id = result.fetchone().id
                await session.commit()

            # Resolve via ClusterGuard (uses its own session internally)
            feature, action = await self._cluster_guard.resolve(
                text=title,
                embedding=embedding,
                project_key=project_key,
                signal_type="plan",
            )

            # Link plan to feature
            async with self._sf() as session:
                link_stmt = (
                    pg_insert(feature_artifacts)
                    .values(
                        feature_id=feature.id,
                        artifact_type="plan",
                        artifact_id=plan_id,
                        similarity_score=1.0,
                    )
                    .on_conflict_do_nothing()
                )
                await session.execute(link_stmt)
                await session.commit()

            indexed += 1
            linked += 1
            logger.info(
                "plan_indexer.indexed",
                file=path_str,
                title=title,
                feature=feature.name,
                action=action,
            )

        return {"indexed": indexed, "skipped": skipped, "linked": linked}

    async def index_project(self, project_key: str) -> dict[str, int] | None:
        """Index plans for a single project. Returns None if no paths configured."""
        from brain_v42.db.tables import project_contexts

        async with self._sf() as session:
            row_result = await session.execute(
                sa.select(project_contexts.c.plan_scan_paths).where(
                    project_contexts.c.project_key == project_key
                )
            )
            row = row_result.fetchone()
            if not row or not row.plan_scan_paths:
                return None

        totals = {"indexed": 0, "skipped": 0, "linked": 0}
        for path in row.plan_scan_paths:
            result = await self.index_path(path, project_key)
            for k in totals:
                totals[k] += result[k]
        return totals

    async def index_all_projects(self) -> dict[str, dict[str, int]]:
        """Scan all project_contexts with plan_scan_paths configured."""
        from brain_v42.db.tables import project_contexts

        results: dict[str, dict[str, int]] = {}
        async with self._sf() as session:
            rows = await session.execute(
                sa.select(
                    project_contexts.c.project_key,
                    project_contexts.c.plan_scan_paths,
                ).where(
                    project_contexts.c.plan_scan_paths != sa.text("'[]'::jsonb")
                )
            )
            projects = rows.fetchall()

        for row in projects:
            project_totals = {"indexed": 0, "skipped": 0, "linked": 0}
            for path in row.plan_scan_paths:
                result = await self.index_path(path, row.project_key)
                for k in project_totals:
                    project_totals[k] += result[k]
            results[row.project_key] = project_totals

        return results
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/unit/test_plan_indexer.py -v`
Expected: 4 PASSED

- [ ] **Step 5: Commit**

```bash
git add src/brain_v42/services/plan_indexer.py tests/unit/test_plan_indexer.py
git commit -m "feat(roadmap-v2): add PlanIndexer — scan specs/plans, embed, link to features"
```

---

## Chunk 4: GitLabIngestor + Sidecar Webhook Endpoint

GitLab webhook processing and sidecar extension.

---

### Task 10: GitLabIngestor Service

**Files:**
- Create: `src/brain_v42/services/gitlab_ingestor.py`
- Create: `tests/unit/test_gitlab_ingestor.py`

- [ ] **Step 1: Write failing tests**

```python
"""Tests for GitLabIngestor — mock webhook payloads."""
import pytest
from unittest.mock import AsyncMock, MagicMock

from brain_v42.services.gitlab_ingestor import GitLabIngestor


@pytest.fixture
def mock_deps():
    session = AsyncMock()
    factory = MagicMock()
    factory.return_value.__aenter__ = AsyncMock(return_value=session)
    factory.return_value.__aexit__ = AsyncMock(return_value=False)
    embedding_svc = AsyncMock()
    embedding_svc.embed.return_value = [0.1] * 1536
    cluster_guard = AsyncMock()
    mock_feature = MagicMock(id="feat-id", name="Test Feature")
    cluster_guard.resolve.return_value = (mock_feature, "linked")
    return factory, session, embedding_svc, cluster_guard


@pytest.mark.asyncio
async def test_process_mr_open_event(mock_deps):
    factory, session, embedding_svc, cluster_guard = mock_deps
    # Simulate no existing event with same ID
    session.execute.return_value = MagicMock(fetchone=MagicMock(return_value=None))

    ingestor = GitLabIngestor(factory, embedding_svc, cluster_guard)
    payload = {
        "object_kind": "merge_request",
        "object_attributes": {
            "action": "open",
            "title": "feat: add decay system",
            "description": "Implement memory decay",
            "source_branch": "feat/decay-system",
        },
        "project": {
            "id": 29,
            "path_with_namespace": "hawkixs_project/brain_v42",
        },
    }
    result = await ingestor.process_event(
        payload, event_uuid="uuid-123", project_key="brain_v42"
    )
    assert result["event_type"] == "mr_opened"
    assert result["signal_type"] == "mr_opened"
    cluster_guard.resolve.assert_called_once()


@pytest.mark.asyncio
async def test_process_push_event(mock_deps):
    factory, session, embedding_svc, cluster_guard = mock_deps
    session.execute.return_value = MagicMock(fetchone=MagicMock(return_value=None))

    ingestor = GitLabIngestor(factory, embedding_svc, cluster_guard)
    payload = {
        "object_kind": "push",
        "ref": "refs/heads/feat/hybrid-search",
        "commits": [
            {"message": "add BM25 scorer"},
            {"message": "integrate hybrid results"},
        ],
        "project": {
            "id": 29,
            "path_with_namespace": "hawkixs_project/brain_v42",
        },
    }
    result = await ingestor.process_event(
        payload, event_uuid="uuid-456", project_key="brain_v42"
    )
    assert result["event_type"] == "push"
    assert result["signal_type"] == "push"


@pytest.mark.asyncio
async def test_extract_text_from_mr():
    ingestor = GitLabIngestor.__new__(GitLabIngestor)
    text = ingestor._extract_text_mr({
        "object_attributes": {
            "title": "feat: add decay system",
            "description": "Implement memory decay with exponential curves",
            "source_branch": "feat/decay-system",
        }
    })
    assert "decay system" in text.lower()


@pytest.mark.asyncio
async def test_extract_feature_name_from_branch():
    ingestor = GitLabIngestor.__new__(GitLabIngestor)
    name = ingestor._branch_to_feature_name("refs/heads/feat/hybrid-search-v2")
    assert name == "Hybrid Search V2"


@pytest.mark.asyncio
async def test_skips_duplicate_event(mock_deps):
    """Duplicate gitlab_event_id → skip processing."""
    factory, session, embedding_svc, cluster_guard = mock_deps
    # Simulate existing event
    session.execute.return_value = MagicMock(
        fetchone=MagicMock(return_value=MagicMock(id="existing"))
    )

    ingestor = GitLabIngestor(factory, embedding_svc, cluster_guard)
    payload = {
        "object_kind": "push",
        "ref": "refs/heads/main",
        "commits": [{"message": "fix typo"}],
        "project": {"id": 29, "path_with_namespace": "hawkixs_project/brain_v42"},
    }
    result = await ingestor.process_event(
        payload, event_uuid="uuid-duplicate", project_key="brain_v42"
    )
    assert result["status"] == "skipped_duplicate"
    cluster_guard.resolve.assert_not_called()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/unit/test_gitlab_ingestor.py -v`
Expected: FAIL (module not found)

- [ ] **Step 3: Implement GitLabIngestor**

```python
"""GitLabIngestor — process GitLab webhooks into feature signals."""
from __future__ import annotations

import re
from typing import TYPE_CHECKING, Any

import sqlalchemy as sa
import structlog
from sqlalchemy.dialects.postgresql import insert as pg_insert

from brain_v42.db.tables import gitlab_events, feature_artifacts

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import async_sessionmaker

    from brain_v42.services.cluster_guard import ClusterGuard

logger = structlog.get_logger(__name__)

_PREFIX_RE = re.compile(r"^(feat|fix|chore|refactor|docs|test|ci|perf|style)[\s:/]+", re.IGNORECASE)
_BRANCH_RE = re.compile(r"^refs/heads/(?:feat|fix|feature|hotfix|bugfix)/(.+)$")


class GitLabIngestor:
    """Process GitLab webhook payloads: extract context, embed, link to features."""

    def __init__(
        self,
        session_factory: async_sessionmaker,
        embedding_svc: object,
        cluster_guard: ClusterGuard,
    ) -> None:
        self._sf = session_factory
        self._embedding_svc = embedding_svc
        self._cluster_guard = cluster_guard

    async def process_event(
        self,
        payload: dict[str, Any],
        event_uuid: str,
        project_key: str,
    ) -> dict[str, Any]:
        """Process a single GitLab webhook event.

        Returns a summary dict with event_type, signal_type, feature_name, status.
        """
        # Check for duplicate
        async with self._sf() as session:
            existing = await session.execute(
                sa.select(gitlab_events.c.id).where(
                    gitlab_events.c.gitlab_event_id == event_uuid
                )
            )
            if existing.fetchone():
                return {"status": "skipped_duplicate", "event_uuid": event_uuid}

        event_type, signal_type, text = self._parse_event(payload)
        if not text:
            return {"status": "ignored", "event_type": event_type}

        # Embed
        embedding = await self._embedding_svc.embed(text[:2000])

        # Resolve via ClusterGuard
        feature, action = await self._cluster_guard.resolve(
            text=text,
            embedding=embedding,
            project_key=project_key,
            signal_type=signal_type,
        )

        gitlab_project_id = payload.get("project", {}).get("id")
        ref = payload.get("ref") or payload.get("object_attributes", {}).get("source_branch")

        # Store event and link to feature
        async with self._sf() as session:
            stmt = (
                pg_insert(gitlab_events)
                .values(
                    gitlab_event_id=event_uuid,
                    event_type=event_type,
                    project_key=project_key,
                    gitlab_project_id=gitlab_project_id,
                    ref=ref,
                    title=text[:500],
                    embedding=embedding,
                    feature_id=feature.id,
                )
                .on_conflict_do_nothing()
                .returning(gitlab_events.c.id)
            )
            result = await session.execute(stmt)
            event_row = result.fetchone()

            # Link to feature using the gitlab_event's actual ID
            if event_row:
                link_stmt = (
                    pg_insert(feature_artifacts)
                    .values(
                        feature_id=feature.id,
                        artifact_type="gitlab_event",
                        artifact_id=event_row.id,
                        similarity_score=1.0,
                    )
                    .on_conflict_do_nothing()
                )
                await session.execute(link_stmt)
            await session.commit()

        logger.info(
            "gitlab_ingestor.processed",
            event_type=event_type,
            feature=feature.name,
            action=action,
        )
        return {
            "status": "processed",
            "event_type": event_type,
            "signal_type": signal_type,
            "feature_name": feature.name,
            "action": action,
        }

    def _parse_event(self, payload: dict) -> tuple[str, str, str]:
        """Parse webhook payload into (event_type, signal_type, text)."""
        kind = payload.get("object_kind", "")

        if kind == "merge_request":
            action = payload.get("object_attributes", {}).get("action", "")
            if action == "merge":
                return "mr_merged", "mr_merged", self._extract_text_mr(payload)
            if action == "close":
                return "mr_closed", "push", self._extract_text_mr(payload)  # linking only
            return "mr_opened", "mr_opened", self._extract_text_mr(payload)

        if kind == "push":
            return "push", "push", self._extract_text_push(payload)

        if kind == "pipeline":
            status = payload.get("object_attributes", {}).get("status", "")
            if status == "success":
                ref = payload.get("object_attributes", {}).get("ref", "")
                return "pipeline_success", "pipeline_success", f"Pipeline success: {ref}"
            return "pipeline_failure", "pipeline_failure", ""

        return kind, kind, ""

    def _extract_text_mr(self, payload: dict) -> str:
        """Extract searchable text from MR event."""
        attrs = payload.get("object_attributes", {})
        title = attrs.get("title", "")
        desc = attrs.get("description", "") or ""
        branch = attrs.get("source_branch", "")
        # Strip conventional commit prefix from title
        clean_title = _PREFIX_RE.sub("", title)
        return f"{clean_title}\n{desc[:500]}\nBranch: {branch}"

    def _extract_text_push(self, payload: dict) -> str:
        """Extract searchable text from push event."""
        ref = payload.get("ref", "")
        commits = payload.get("commits", [])
        messages = [c.get("message", "").split("\n")[0] for c in commits[:10]]
        branch_name = self._branch_to_feature_name(ref)
        return f"{branch_name}\n" + "\n".join(messages)

    def _branch_to_feature_name(self, ref: str) -> str:
        """Convert branch ref to feature name. 'refs/heads/feat/decay-system' → 'Decay System'."""
        match = _BRANCH_RE.match(ref)
        if match:
            slug = match.group(1)
        else:
            slug = ref.split("/")[-1]
        return slug.replace("-", " ").replace("_", " ").title()
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/unit/test_gitlab_ingestor.py -v`
Expected: 5 PASSED

- [ ] **Step 5: Commit**

```bash
git add src/brain_v42/services/gitlab_ingestor.py tests/unit/test_gitlab_ingestor.py
git commit -m "feat(roadmap-v2): add GitLabIngestor — webhook parsing + feature linking"
```

---

### Task 11: Add Webhook Endpoint to Metrics Sidecar

**Files:**
- Modify: `src/brain_v42/metrics/server.py`
- Modify: `src/brain_v42/metrics/__main__.py`
- Create: `tests/unit/test_metrics_webhook.py`

- [ ] **Step 1: Write failing tests for webhook handler**

```python
"""Tests for GitLab webhook endpoint in metrics sidecar."""
import json
from unittest.mock import AsyncMock, MagicMock

import pytest

from brain_v42.metrics.server import MetricsServer


@pytest.fixture
def mock_services():
    collector = MagicMock()
    embedding_svc = MagicMock()
    gitlab_ingestor = AsyncMock()
    gitlab_ingestor.process_event.return_value = {"status": "processed", "event_type": "push"}
    project_key_resolver = AsyncMock(return_value="brain_v42")
    return collector, embedding_svc, gitlab_ingestor, project_key_resolver


@pytest.mark.asyncio
async def test_webhook_rejects_without_token(mock_services):
    collector, embedding_svc, gitlab_ingestor, resolver = mock_services
    server = MetricsServer(
        collector=collector,
        embedding_svc=embedding_svc,
        gitlab_ingestor=gitlab_ingestor,
        project_key_resolver=resolver,
        webhook_secret="my-secret",
    )
    app = server._build_app()
    from aiohttp.test_utils import TestClient, TestServer
    async with TestClient(TestServer(app)) as client:
        resp = await client.post("/gitlab/webhook", json={"object_kind": "push"})
        assert resp.status == 401


@pytest.mark.asyncio
async def test_webhook_accepts_valid_token(mock_services):
    collector, embedding_svc, gitlab_ingestor, resolver = mock_services
    server = MetricsServer(
        collector=collector,
        embedding_svc=embedding_svc,
        gitlab_ingestor=gitlab_ingestor,
        project_key_resolver=resolver,
        webhook_secret="my-secret",
    )
    app = server._build_app()
    from aiohttp.test_utils import TestClient, TestServer
    async with TestClient(TestServer(app)) as client:
        resp = await client.post(
            "/gitlab/webhook",
            json={"object_kind": "push", "ref": "refs/heads/main", "commits": [], "project": {"id": 29, "path_with_namespace": "hawkixs_project/brain_v42"}},
            headers={"X-Gitlab-Token": "my-secret", "X-Gitlab-Event-UUID": "uuid-1"},
        )
        assert resp.status == 200
        gitlab_ingestor.process_event.assert_called_once()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/unit/test_metrics_webhook.py -v`
Expected: FAIL

- [ ] **Step 3: Update MetricsServer to add webhook route**

In `metrics/server.py`:
- Add `gitlab_ingestor`, `project_key_resolver`, `webhook_secret` to `__init__` with `None` defaults for backward compat
- Add `POST /gitlab/webhook` route in `_build_app()` (only if `gitlab_ingestor` is provided)
- Validate `X-Gitlab-Token` header
- Extract `X-Gitlab-Event-UUID` header
- Resolve `project_key` from `project.path_with_namespace`
- Delegate to `gitlab_ingestor.process_event()`

- [ ] **Step 4: Update `__main__.py` to wire new dependencies**

In `metrics/__main__.py`:
- Create `ClusterGuard`, `StatusEngine`, `GitLabIngestor` instances
- Create project_key resolver function (queries `project_contexts.gitlab_project_path`)
- Pass to `MetricsServer` constructor

- [ ] **Step 5: Run tests to verify they pass**

Run: `pytest tests/unit/test_metrics_webhook.py -v`
Expected: 2 PASSED

- [ ] **Step 6: Commit**

```bash
git add src/brain_v42/metrics/server.py src/brain_v42/metrics/__main__.py tests/unit/test_metrics_webhook.py
git commit -m "feat(roadmap-v2): add POST /gitlab/webhook to metrics sidecar"
```

---

## Chunk 5: MCP Tools + Server Wiring + RoadmapService Updates

Wire everything into the MCP server and update existing tools.

---

### Task 12: Update RoadmapService

**Files:**
- Modify: `src/brain_v42/services/roadmap_service.py`
- Modify: `tests/unit/mcp/tools/test_roadmap_tools.py`

- [ ] **Step 1: Update `_ARTIFACT_TYPES` and `_ROADMAP_SQL`**

Add `"plan"` and `"gitlab_event"` to `_ARTIFACT_TYPES` tuple. Update the pivot logic in `_pivot_rows` to initialize artifact_count with the new types.

- [ ] **Step 2: Add `pinned` to RoadmapFeature output**

Update `_pivot_rows` to include `pinned` from the features query.

- [ ] **Step 3: Update `_ROADMAP_SQL` to select `pinned`**

Add `f.pinned` to the SELECT list.

- [ ] **Step 4: Run existing roadmap tests**

Run: `pytest tests/unit/mcp/tools/test_roadmap_tools.py -v`
Expected: PASS (backward compatible)

- [ ] **Step 5: Commit**

```bash
git add src/brain_v42/services/roadmap_service.py tests/unit/mcp/tools/test_roadmap_tools.py
git commit -m "feat(roadmap-v2): extend RoadmapService with plan/gitlab_event types + pinned"
```

---

### Task 13: Update brain_set_project_context to Upsert

**Files:**
- Modify: `src/brain_v42/mcp/tools/project_context_tools.py`
- Modify: `src/brain_v42/services/project_context_service.py`
- Modify: `src/brain_v42/repositories/pg_project_context.py`

- [ ] **Step 1: Add `plan_scan_paths` and `gitlab_project_path` params to tool**

Add the two new parameters to `brain_set_project_context()` function.

- [ ] **Step 2: Change `get_or_create` to upsert in service/repo**

Update `ProjectContextService.get_or_create()` to use `INSERT ... ON CONFLICT (project_key) DO UPDATE` so that new fields are persisted even for existing projects.

- [ ] **Step 3: Add `unpin` param to brain_update_project_focus**

Add `unpin: list[str] | None = None` parameter. When provided, set `pinned=false` for matching feature names. When `feature_status` is provided, set `pinned=true` for those features.

- [ ] **Step 4: Run project_context tests**

Run: `pytest tests/unit/mcp/tools/test_project_context_tools.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/brain_v42/mcp/tools/project_context_tools.py \
        src/brain_v42/services/project_context_service.py \
        src/brain_v42/repositories/pg_project_context.py
git commit -m "feat(roadmap-v2): upsert project_context + plan_scan_paths/gitlab_project_path + pin/unpin"
```

---

### Task 14: Add brain_reindex_plans Tool

**Files:**
- Create: `src/brain_v42/mcp/tools/plan_tools.py`
- Create: `tests/unit/mcp/tools/test_plan_tools.py`

- [ ] **Step 1: Write failing test**

```python
"""Tests for brain_reindex_plans MCP tool."""
from unittest.mock import AsyncMock, MagicMock
import pytest


@pytest.mark.asyncio
async def test_reindex_plans_returns_summary():
    from brain_v42.mcp.tools.plan_tools import register_plan_tools

    mcp = MagicMock()
    plan_indexer = AsyncMock()
    plan_indexer.index_all_projects.return_value = {
        "brain_v42": {"indexed": 3, "skipped": 1, "linked": 3}
    }

    registered_tools = {}

    def capture_tool(**kwargs):
        def decorator(fn):
            registered_tools[fn.__name__] = fn
            return fn
        return decorator
    mcp.tool = capture_tool

    register_plan_tools(mcp, plan_indexer=plan_indexer)
    result = await registered_tools["brain_reindex_plans"](project_key=None)
    assert "brain_v42" in result
    assert "3" in result  # indexed count
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/unit/mcp/tools/test_plan_tools.py -v`

- [ ] **Step 3: Implement plan_tools.py**

```python
"""MCP tool: brain_reindex_plans — re-scan and index plan files."""
from __future__ import annotations

from typing import TYPE_CHECKING, Any

import structlog

from brain_v42.mcp.tools.formatters import format_confirmation

if TYPE_CHECKING:
    from brain_v42.services.plan_indexer import PlanIndexer

logger = structlog.get_logger(__name__)


def register_plan_tools(mcp: Any, plan_indexer: PlanIndexer) -> None:
    """Register brain_reindex_plans on mcp."""

    @mcp.tool(version="1.0")
    async def brain_reindex_plans(project_key: str | None = None) -> str:
        """Re-scan and index plan files for a project or all projects.

        Compares file content hashes to skip unchanged files.
        Links new/updated plans to features via ClusterGuard.

        Args:
            project_key: If provided, index only this project's paths. Otherwise index all.
        """
        logger.debug("mcp.brain_reindex_plans", project_key=project_key)

        if project_key:
            results = await plan_indexer.index_project(project_key)
            if not results:
                return f"No plan_scan_paths configured for project '{project_key}'"
            results = {project_key: results}
        else:
            results = await plan_indexer.index_all_projects()

        # Format output
        lines = ["## Plan Indexing Results\n"]
        for pk, stats in results.items():
            lines.append(f"**{pk}**: {stats['indexed']} indexed, {stats['skipped']} skipped, {stats['linked']} linked")
        return "\n".join(lines)
```

- [ ] **Step 4: Run test**

Run: `pytest tests/unit/mcp/tools/test_plan_tools.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/brain_v42/mcp/tools/plan_tools.py tests/unit/mcp/tools/test_plan_tools.py
git commit -m "feat(roadmap-v2): add brain_reindex_plans MCP tool"
```

---

### Task 15: Wire Everything in MCP Server

**Files:**
- Modify: `src/brain_v42/mcp/server.py`

- [ ] **Step 1: Create new services in `build_services()`**

Add to `build_services()`:
```python
# Reranker client
from brain_v42.services.reranker_client import RerankerClient
reranker = RerankerClient(base_url=settings.reranker_url, timeout=settings.reranker_timeout)

# StatusEngine
from brain_v42.services.status_engine import StatusEngine
status_engine = StatusEngine()

# ClusterGuard
from brain_v42.services.cluster_guard import ClusterGuard
cluster_guard = ClusterGuard(session_factory, embedding_svc, reranker, status_engine)

# PlanIndexer
from brain_v42.services.plan_indexer import PlanIndexer
plan_indexer = PlanIndexer(session_factory, embedding_svc, cluster_guard)

# Update FeatureLinker to use ClusterGuard
feature_linker = FeatureLinker(session_factory=session_factory, cluster_guard=cluster_guard)
```

- [ ] **Step 2: Register plan tools**

```python
from brain_v42.mcp.tools.plan_tools import register_plan_tools
register_plan_tools(mcp, plan_indexer=services["plan_indexer"])
```

- [ ] **Step 3: Add plan indexing at startup**

After server startup, run `plan_indexer.index_all_projects()` as a background task (like the decay flusher).

- [ ] **Step 4: Run full test suite**

Run: `pytest tests/unit -v --tb=short`
Expected: ALL PASS

- [ ] **Step 5: Commit**

```bash
git add src/brain_v42/mcp/server.py
git commit -m "feat(roadmap-v2): wire ClusterGuard, PlanIndexer, StatusEngine in server startup"
```

---

## Chunk 6: Reranker Service + Formatters + Final Polish

Deploy the cross-encoder service and update formatters.

---

### Task 16: Reranker Docker Service

**Files:**
- Create: `services/reranker/Dockerfile`
- Create: `services/reranker/server.py`
- Create: `services/reranker/requirements.txt`

- [ ] **Step 1: Create reranker service directory**

Run: `mkdir -p services/reranker`

- [ ] **Step 2: Write requirements.txt**

```
fastapi==0.115.0
uvicorn[standard]==0.34.0
sentence-transformers==3.4.1
torch==2.5.1+cpu
```

Note: `torch` CPU-only to avoid pulling GPU deps.

- [ ] **Step 3: Write server.py**

```python
"""Cross-encoder reranker service — CPU only, port 8004."""
from __future__ import annotations

from fastapi import FastAPI
from pydantic import BaseModel
from sentence_transformers import CrossEncoder

MODEL_NAME = "cross-encoder/ms-marco-MiniLM-L-6-v2"
model: CrossEncoder | None = None


class RerankRequest(BaseModel):
    query: str
    candidates: list[str]


class RerankResponse(BaseModel):
    scores: list[float]


from contextlib import asynccontextmanager

@asynccontextmanager
async def lifespan(app: FastAPI):
    global model
    model = CrossEncoder(MODEL_NAME)
    yield

app = FastAPI(title="brain-v42-reranker", lifespan=lifespan)


@app.get("/health")
def health() -> dict:
    return {"status": "ok", "model": MODEL_NAME}


@app.post("/rerank", response_model=RerankResponse)
def rerank(req: RerankRequest) -> RerankResponse:
    pairs = [[req.query, c] for c in req.candidates]
    scores = model.predict(pairs).tolist()
    return RerankResponse(scores=scores)
```

- [ ] **Step 4: Write Dockerfile**

```dockerfile
FROM python:3.12-slim

WORKDIR /app

# Install CPU-only torch first
RUN pip install torch==2.5.1+cpu -f https://download.pytorch.org/whl/cpu/torch_stable.html

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY server.py .

# Download model at build time
RUN python -c "from sentence_transformers import CrossEncoder; CrossEncoder('cross-encoder/ms-marco-MiniLM-L-6-v2')"

EXPOSE 8004

CMD ["uvicorn", "server:app", "--host", "0.0.0.0", "--port", "8004"]
```

- [ ] **Step 5: Build and test locally**

Run: `cd services/reranker && docker build -t brain-v42-reranker . && docker run -d --name brain-v42-reranker -p 8004:8004 brain-v42-reranker`
Verify: `curl -s http://localhost:8004/health` → `{"status": "ok", "model": "cross-encoder/ms-marco-MiniLM-L-6-v2"}`

- [ ] **Step 6: Test reranking endpoint**

Run: `curl -s -X POST http://localhost:8004/rerank -H "Content-Type: application/json" -d '{"query": "memory decay system", "candidates": ["Memory Decay / Active Forgetting", "Hybrid Search", "Knowledge Graph"]}'`
Expected: scores array with first score highest

- [ ] **Step 7: Commit**

```bash
git add services/reranker/
git commit -m "feat(roadmap-v2): add cross-encoder reranker service (CPU, port 8004)"
```

---

### Task 17: Update Formatters for Roadmap Pinned Display

**Files:**
- Modify: `src/brain_v42/mcp/tools/formatters.py`
- Modify: `tests/unit/mcp/tools/test_formatters.py`

- [ ] **Step 1: Update `format_roadmap` to show pinned indicator**

In `formatters.py`, update the roadmap formatter to include a pinned column/indicator when `pinned=true`.

- [ ] **Step 2: Update formatter tests**

Add test case for roadmap output with pinned features.

- [ ] **Step 3: Run formatter tests**

Run: `pytest tests/unit/mcp/tools/test_formatters.py -v`
Expected: PASS

- [ ] **Step 4: Commit**

```bash
git add src/brain_v42/mcp/tools/formatters.py tests/unit/mcp/tools/test_formatters.py
git commit -m "feat(roadmap-v2): update roadmap formatter with pinned indicator + plan/gitlab types"
```

---

### Task 18: Update docker-compose.yml

**Files:**
- Modify: `docker-compose.yml`

- [ ] **Step 1: Add reranker service**

```yaml
  reranker:
    build: ./services/reranker
    container_name: brain_v42_reranker
    restart: unless-stopped
    ports:
      - "8004:8004"
    healthcheck:
      test: ["CMD-SHELL", "curl -sf http://localhost:8004/health || exit 1"]
      interval: 30s
      timeout: 5s
      retries: 3
      start_period: 30s
```

- [ ] **Step 2: Commit**

```bash
git add docker-compose.yml
git commit -m "feat(roadmap-v2): add reranker service to docker-compose"
```

---

### Task 19: Update Domain Service Callers to Pass Title

**Files:**
- Modify: `src/brain_v42/services/decision_service.py`
- Modify: `src/brain_v42/services/learning_service.py`
- Modify: `src/brain_v42/services/snippet_service.py`
- Modify: `src/brain_v42/services/runbook_service.py`
- Modify: `src/brain_v42/services/adr_service.py`

- [ ] **Step 1: Update each service's create method to pass title to feature_linker**

In each service, find the `feature_linker.link_artifact()` call and add the `title` parameter from the artifact being created. For example in `DecisionService`:

```python
# Before
await self._feature_linker.link_artifact(
    embedding=embedding, artifact_type="decision",
    artifact_id=decision.id, project_key=data.project_key,
)
# After
await self._feature_linker.link_artifact(
    embedding=embedding, artifact_type="decision",
    artifact_id=decision.id, project_key=data.project_key,
    title=data.title,
)
```

Repeat for all 5 services: decision, learning, snippet, runbook, adr.

- [ ] **Step 2: Run existing service tests**

Run: `pytest tests/unit -k "service" -v --tb=short`
Expected: ALL PASS (title is optional, backward compat)

- [ ] **Step 3: Commit**

```bash
git add src/brain_v42/services/decision_service.py src/brain_v42/services/learning_service.py \
        src/brain_v42/services/snippet_service.py src/brain_v42/services/runbook_service.py \
        src/brain_v42/services/adr_service.py
git commit -m "feat(roadmap-v2): pass artifact title to FeatureLinker for dynamic feature naming"
```

---

### Task 20: Batch Feature Deduplication Job

**Files:**
- Create: `src/brain_v42/services/feature_dedup_job.py`
- Create: `tests/unit/test_feature_dedup_job.py`

- [ ] **Step 1: Write failing tests**

```python
"""Tests for FeatureDedupJob — periodic feature deduplication."""
import uuid
from unittest.mock import AsyncMock, MagicMock

import pytest

from brain_v42.services.feature_dedup_job import FeatureDedupJob


@pytest.fixture
def mock_deps():
    session = AsyncMock()
    factory = MagicMock()
    factory.return_value.__aenter__ = AsyncMock(return_value=session)
    factory.return_value.__aexit__ = AsyncMock(return_value=False)
    reranker = AsyncMock()
    embedding_svc = AsyncMock()
    return factory, session, reranker, embedding_svc


@pytest.mark.asyncio
async def test_find_candidates_uses_cosine_prefilter(mock_deps):
    factory, session, reranker, embedding_svc = mock_deps
    # No features → no candidates
    session.execute.return_value = MagicMock(fetchall=MagicMock(return_value=[]))
    job = FeatureDedupJob(factory, reranker, embedding_svc)
    candidates = await job.find_candidates("test_project")
    assert candidates == []


@pytest.mark.asyncio
async def test_merge_transfers_artifacts(mock_deps):
    factory, session, reranker, embedding_svc = mock_deps
    embedding_svc.embed.return_value = [0.1] * 1536
    job = FeatureDedupJob(factory, reranker, embedding_svc)
    source = MagicMock(id=uuid.uuid4(), name="Active Forgetting", description="desc1")
    target = MagicMock(id=uuid.uuid4(), name="Memory Decay", description="desc2")
    await job.merge_features(session, target, source)
    # Verify UPDATE and DELETE were called
    assert session.execute.call_count >= 2
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/unit/test_feature_dedup_job.py -v`
Expected: FAIL (module not found)

- [ ] **Step 3: Implement FeatureDedupJob**

```python
"""FeatureDedupJob — periodic feature deduplication via cross-encoder.

Reuses ConsolidationJob pattern: periodic scan, pre-filter, merge.
"""
from __future__ import annotations

from typing import TYPE_CHECKING

import sqlalchemy as sa
import structlog

from brain_v42.db.tables import features, feature_artifacts

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

    from brain_v42.services.reranker_client import RerankerClient

logger = structlog.get_logger(__name__)

COSINE_PREFILTER = 0.50
RERANKER_MERGE_THRESHOLD = 0.80


class FeatureDedupJob:
    """Find and merge duplicate features within same project."""

    def __init__(
        self,
        session_factory: async_sessionmaker,
        reranker: RerankerClient,
        embedding_svc: object,
    ) -> None:
        self._sf = session_factory
        self._reranker = reranker
        self._embedding_svc = embedding_svc

    async def find_candidates(self, project_key: str) -> list[tuple[object, object, float]]:
        """Find duplicate feature pairs using cosine pre-filter + cross-encoder."""
        async with self._sf() as session:
            # Get all features for project
            result = await session.execute(
                sa.select(features).where(
                    features.c.project_key == project_key,
                    features.c.embedding.is_not(None),
                )
            )
            all_features = result.fetchall()

        if len(all_features) < 2:
            return []

        candidates: list[tuple[object, object, float]] = []

        for i, feat_a in enumerate(all_features):
            # Pre-filter: top-3 neighbors via cosine
            async with self._sf() as session:
                similarity = (1 - features.c.embedding.cosine_distance(feat_a.embedding)).label("sim")
                result = await session.execute(
                    sa.select(features, similarity)
                    .where(features.c.project_key == project_key)
                    .where(features.c.id != feat_a.id)
                    .where(features.c.embedding.is_not(None))
                    .order_by(features.c.embedding.cosine_distance(feat_a.embedding))
                    .limit(3)
                )
                neighbors = [(r, r.sim) for r in result.fetchall() if r.sim >= COSINE_PREFILTER]

            if not neighbors:
                continue

            # Cross-encoder on pre-filtered pairs
            try:
                texts = [f"{n[0].name}: {n[0].description}" for n in neighbors]
                scores = await self._reranker.rerank(
                    query=f"{feat_a.name}: {feat_a.description}",
                    candidates=texts,
                )
                for (neighbor, _cosine), score in zip(neighbors, scores):
                    if score >= RERANKER_MERGE_THRESHOLD:
                        # Oldest absorbs newest
                        if feat_a.created_at <= neighbor.created_at:
                            candidates.append((feat_a, neighbor, score))
                        else:
                            candidates.append((neighbor, feat_a, score))
            except Exception:
                logger.exception("feature_dedup.reranker_error")

        return candidates

    async def merge_features(
        self, session: AsyncSession, target: object, source: object
    ) -> None:
        """Merge source into target: transfer artifacts, delete source."""
        # Transfer all feature_artifacts from source to target
        await session.execute(
            sa.update(feature_artifacts)
            .where(feature_artifacts.c.feature_id == source.id)
            .values(feature_id=target.id)
        )
        # Enrich target description
        new_desc = f"{target.description}\n---\n{source.description}"
        new_embedding = await self._embedding_svc.embed(new_desc[:2000])
        await session.execute(
            sa.update(features)
            .where(features.c.id == target.id)
            .values(description=new_desc[:5000], embedding=new_embedding)
        )
        # Delete source
        await session.execute(
            sa.delete(features).where(features.c.id == source.id)
        )
        logger.info("feature_dedup.merged", target=target.name, source=source.name)
```

- [ ] **Step 4: Run tests**

Run: `pytest tests/unit/test_feature_dedup_job.py -v`
Expected: 2 PASSED

- [ ] **Step 5: Commit**

```bash
git add src/brain_v42/services/feature_dedup_job.py tests/unit/test_feature_dedup_job.py
git commit -m "feat(roadmap-v2): add FeatureDedupJob — periodic cross-encoder deduplication"
```

---

### Task 21: Deprecate Seed Script

**Files:**
- Delete: `src/brain_v42/scripts/seed_features.py` (after verifying existing features are in DB)
- Delete: `src/brain_v42/scripts/retrolink_features.py` (replaced by ClusterGuard auto-linking)
- Delete: `scripts/backfill_feature_links.py` (replaced by ClusterGuard)

- [ ] **Step 1: Verify existing features are in production DB**

Run: `psql -h localhost -p 5433 -U brain brain -c "SELECT name, status FROM features ORDER BY project_key, name"`
Expected: All seeded features present

- [ ] **Step 2: Remove the three scripts**

```bash
rm src/brain_v42/scripts/seed_features.py
rm src/brain_v42/scripts/retrolink_features.py
rm scripts/backfill_feature_links.py
```

- [ ] **Step 3: Commit**

```bash
git add -u
git commit -m "chore(roadmap-v2): remove seed/retrolink/backfill scripts — features now dynamic"
```

---

### Task 22: Update tables.py __all__ and Model Barrel Imports

**Files:**
- Modify: `src/brain_v42/db/tables.py`
- Modify: `src/brain_v42/models/__init__.py` (if exists)

- [ ] **Step 1: Add new tables to `__all__` in tables.py**

Add `"indexed_plans"` and `"gitlab_events"` to the `__all__` list.

- [ ] **Step 2: Update models/__init__.py if it exists**

Add imports for `IndexedPlan`, `IndexedPlanCreate`, `GitLabEvent`, `GitLabEventCreate`.

- [ ] **Step 3: Commit**

```bash
git add src/brain_v42/db/tables.py src/brain_v42/models/
git commit -m "chore(roadmap-v2): update __all__ and barrel imports for new tables/models"
```

---

### Task 23: Full Test Suite + Ruff + Final Verification

**Files:** All modified/created files

- [ ] **Step 1: Run ruff check + format**

Run: `ruff check src/ tests/ && ruff format src/ tests/`

- [ ] **Step 2: Run full unit test suite**

Run: `pytest tests/unit -v --tb=short`
Expected: ALL PASS, no regressions

- [ ] **Step 3: Run coverage**

Run: `pytest tests/unit --cov=brain_v42 --cov-report=term-missing`
Expected: >= 60% coverage (CI threshold)

- [ ] **Step 4: Apply migration on dev DB and verify**

Run: `alembic upgrade head`
Verify: `psql -h localhost -p 5433 -U brain brain -c "\dt" | grep -E "indexed_plans|gitlab_events"`

- [ ] **Step 5: Start reranker and verify health**

Run: `docker compose up -d reranker && sleep 5 && curl -s http://localhost:8004/health`

- [ ] **Step 6: Restart metrics sidecar with new deps**

Run: `kill $(pgrep -f "brain_v42.metrics") && nohup .venv/bin/python -m brain_v42.metrics > /tmp/brain_v42_metrics.log 2>&1 &`

- [ ] **Step 7: Configure brain_v42 project scan paths**

Via MCP: `brain_set_project_context(project_key="brain_v42", plan_scan_paths=["/home/hawixs/hawkixs_infra/git_repo/brain_v42/docs"], gitlab_project_path="hawkixs_project/brain_v42")`

- [ ] **Step 8: Test plan indexing**

Via MCP: `brain_reindex_plans(project_key="brain_v42")`
Expected: specs and plans indexed, linked to features

- [ ] **Step 9: Verify roadmap output**

Via MCP: `brain_get_roadmap(project_key="brain_v42")`
Expected: features with plan artifact counts, pinned indicators

- [ ] **Step 10: Commit final state**

```bash
git add -A
git commit -m "feat(roadmap-v2): final polish — ruff, tests, verified deployment"
```
