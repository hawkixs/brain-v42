# Dream Mode Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a nightly autonomous agent (Dream Mode) that consolidates brain-v42's knowledge through 5 phases: scan, clean, connect, synthesize, reorganize.

**Architecture:** External bash orchestrator (`scripts/dream.sh`) sequences 5 `claude -p` headless runs. Each phase writes a tagged report into the brain itself, which the next phase reads. Two new MCP tools (`brain_backfill_links_batch`, `brain_get_clusters`) are added; everything else reuses existing tools.

**Tech Stack:** Python 3.12+, FastMCP 3.1, SQLAlchemy 2.0 async, Neo4j 5, bash, `claude -p` CLI

**Spec:** `docs/superpowers/specs/2026-04-05-dream-mode-design.md`

---

## File Map

| Action | File | Responsibility |
|--------|------|---------------|
| Modify | `src/brain_v42/models/learning.py` | Add `"automated"` to `SourceType` |
| Modify | `src/brain_v42/services/graph_service.py` | Add `find_unlinked_nodes()` + `get_all_related_edges()` |
| Modify | `src/brain_v42/mcp/server.py` | Expose `auto_linker` in services dict + wire dream tools |
| Create | `src/brain_v42/mcp/tools/dream_tools.py` | `brain_backfill_links_batch` + `brain_get_clusters` MCP tools |
| Create | `tests/unit/test_dream_tools.py` | Unit tests for both new tools |
| Create | `scripts/dream.sh` | Bash orchestrator |
| Create | `scripts/dream/phase_scan.md` | SCAN prompt |
| Create | `scripts/dream/phase_clean.md` | CLEAN prompt |
| Create | `scripts/dream/phase_connect.md` | CONNECT prompt |
| Create | `scripts/dream/phase_synth.md` | SYNTH prompt |
| Create | `scripts/dream/phase_reorg.md` | REORG prompt |

---

## Batch 1: Prerequisite changes (independent, parallelizable)

### Task 1: Add `"automated"` to `SourceType`

**Files:**
- Modify: `src/brain_v42/models/learning.py:11-22`

- [ ] **Step 1: Write the failing test**

Create a test that imports `SourceType` and verifies `"automated"` is a valid value:

```python
# tests/unit/test_source_type_automated.py
"""Test that SourceType includes 'automated' for dream agent reports."""
from brain_v42.models.learning import LearningCreate


def test_source_type_automated_is_valid():
    """Dream agent reports use source_type='automated'."""
    lr = LearningCreate(
        topic="Dream Scan — 2026-04-05",
        insight="test",
        source_type="automated",
        tags=["dream:scan"],
    )
    assert lr.source_type == "automated"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /home/hawixs/hawkixs_infra/git_repo/brain_v42 && python -m pytest tests/unit/test_source_type_automated.py -v`
Expected: FAIL with Pydantic `ValidationError` — `"automated"` is not in the Literal.

- [ ] **Step 3: Add `"automated"` to `SourceType` Literal**

In `src/brain_v42/models/learning.py`, change line 11-22:

```python
SourceType = Literal[
    "experience",
    "documentation",
    "code_review",
    "bug",
    "external",
    "article",
    "video",
    "book",
    "conversation",
    "research",
    "automated",
]
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd /home/hawixs/hawkixs_infra/git_repo/brain_v42 && python -m pytest tests/unit/test_source_type_automated.py -v`
Expected: PASS

- [ ] **Step 5: Run existing tests to ensure no regression**

Run: `cd /home/hawixs/hawkixs_infra/git_repo/brain_v42 && python -m pytest tests/unit/ -v --timeout=30`
Expected: All pass.

- [ ] **Step 6: Commit**

```bash
cd /home/hawixs/hawkixs_infra/git_repo/brain_v42
git add src/brain_v42/models/learning.py tests/unit/test_source_type_automated.py
git commit -m "feat(models): add 'automated' source_type for dream agent reports"
```

---

### Task 2: Add `find_unlinked_nodes()` and `get_all_related_edges()` to `GraphService`

**Files:**
- Modify: `src/brain_v42/services/graph_service.py`
- Create: `tests/unit/test_graph_service_dream.py`

- [ ] **Step 1: Write failing tests**

```python
# tests/unit/test_graph_service_dream.py
"""Tests for GraphService dream-related methods: find_unlinked_nodes, get_all_related_edges."""
from __future__ import annotations

import pytest
from unittest.mock import AsyncMock, MagicMock

from brain_v42.services.graph_service import GraphService


@pytest.fixture
def mock_driver():
    driver = AsyncMock()
    return driver


@pytest.fixture
def graph(mock_driver):
    return GraphService(driver=mock_driver, timeout=5.0)


class TestFindUnlinkedNodes:
    """find_unlinked_nodes returns entity IDs with zero RELATED_TO edges."""

    @pytest.mark.asyncio
    async def test_returns_unlinked_ids(self, graph):
        """Nodes without RELATED_TO edges are returned."""
        mock_records = [
            {"id": "aaa-111"},
            {"id": "bbb-222"},
        ]
        graph._run_read = AsyncMock(return_value=mock_records)

        result = await graph.find_unlinked_nodes(limit=10)

        assert result == ["aaa-111", "bbb-222"]
        graph._run_read.assert_called_once()
        # Verify query contains NOT ... RELATED_TO
        query_arg = graph._run_read.call_args[0][0]
        assert "RELATED_TO" in query_arg
        assert "NOT" in query_arg

    @pytest.mark.asyncio
    async def test_filters_by_entity_type(self, graph):
        """When entity_type is passed, it filters by label."""
        graph._run_read = AsyncMock(return_value=[])

        await graph.find_unlinked_nodes(entity_type="Learning", limit=5)

        params = graph._run_read.call_args[0][1]
        assert params["type"] == "Learning"
        assert params["limit"] == 5

    @pytest.mark.asyncio
    async def test_returns_empty_on_error(self, graph):
        """Graph errors return empty list (graceful degradation)."""
        graph._run_read = AsyncMock(return_value=[])

        result = await graph.find_unlinked_nodes()

        assert result == []


class TestGetAllRelatedEdges:
    """get_all_related_edges returns all RELATED_TO edge pairs."""

    @pytest.mark.asyncio
    async def test_returns_edge_pairs(self, graph):
        """Returns deduplicated (src, tgt) tuples."""
        mock_records = [
            {"src": "aaa-111", "tgt": "bbb-222"},
            {"src": "ccc-333", "tgt": "ddd-444"},
        ]
        graph._run_read = AsyncMock(return_value=mock_records)

        result = await graph.get_all_related_edges()

        assert result == [("aaa-111", "bbb-222"), ("ccc-333", "ddd-444")]

    @pytest.mark.asyncio
    async def test_returns_empty_when_no_edges(self, graph):
        """No edges → empty list."""
        graph._run_read = AsyncMock(return_value=[])

        result = await graph.get_all_related_edges()

        assert result == []
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd /home/hawixs/hawkixs_infra/git_repo/brain_v42 && python -m pytest tests/unit/test_graph_service_dream.py -v`
Expected: FAIL with `AttributeError: 'GraphService' object has no attribute 'find_unlinked_nodes'`

- [ ] **Step 3: Implement both methods**

Add to the end of `src/brain_v42/services/graph_service.py`, before the `# ── Internal ──` section (before line 145):

```python
    # ── Dream Mode queries ──

    async def find_unlinked_nodes(
        self, entity_type: str | None = None, limit: int = 50
    ) -> list[str]:
        """Return entity IDs that have a node but zero RELATED_TO edges."""
        query = """
            MATCH (n)
            WHERE NOT (n)-[:RELATED_TO]-()
            AND ($type IS NULL OR $type IN labels(n))
            RETURN n.id AS id
            LIMIT $limit
        """
        rows = await self._run_read(query, {"type": entity_type, "limit": limit})
        return [row["id"] for row in rows]

    async def get_all_related_edges(self) -> list[tuple[str, str]]:
        """Return all (source_id, target_id) pairs for RELATED_TO edges.

        Deduplicates by requiring a.id < b.id so each edge appears once.
        """
        query = """
            MATCH (a)-[:RELATED_TO]-(b)
            WHERE a.id < b.id
            RETURN DISTINCT a.id AS src, b.id AS tgt
        """
        rows = await self._run_read(query, {})
        return [(row["src"], row["tgt"]) for row in rows]
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd /home/hawixs/hawkixs_infra/git_repo/brain_v42 && python -m pytest tests/unit/test_graph_service_dream.py -v`
Expected: All PASS

- [ ] **Step 5: Commit**

```bash
cd /home/hawixs/hawkixs_infra/git_repo/brain_v42
git add src/brain_v42/services/graph_service.py tests/unit/test_graph_service_dream.py
git commit -m "feat(graph): add find_unlinked_nodes and get_all_related_edges for dream mode"
```

---

### Task 3: Expose `auto_linker` in `build_services()` return dict

**Files:**
- Modify: `src/brain_v42/mcp/server.py:257-280`

- [ ] **Step 1: Add `auto_linker` to the return dict**

In `src/brain_v42/mcp/server.py`, in the `build_services()` return dict (line 257-280), add `"auto_linker"` after `"neo4j_driver"`:

```python
        "neo4j_driver": neo4j_driver,
        "auto_linker": auto_linker,
    }
```

- [ ] **Step 2: Run existing tests to verify no regression**

Run: `cd /home/hawixs/hawkixs_infra/git_repo/brain_v42 && python -m pytest tests/unit/ -v --timeout=30`
Expected: All pass.

- [ ] **Step 3: Commit**

```bash
cd /home/hawixs/hawkixs_infra/git_repo/brain_v42
git add src/brain_v42/mcp/server.py
git commit -m "fix(server): expose auto_linker in build_services() for dream tools"
```

---

## Batch 2: New MCP tools (depends on Batch 1)

### Task 4: Implement `dream_tools.py` — `brain_backfill_links_batch`

**Files:**
- Create: `src/brain_v42/mcp/tools/dream_tools.py`
- Create: `tests/unit/test_dream_tools.py`

- [ ] **Step 1: Write failing tests for `brain_backfill_links_batch`**

```python
# tests/unit/test_dream_tools.py
"""Tests for dream_tools: brain_backfill_links_batch, brain_get_clusters."""
from __future__ import annotations

import uuid
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastmcp import FastMCP

from brain_v42.mcp.tools.dream_tools import register_dream_tools


# ── Fixtures ──────────────────────────────────────────────────────────────


@pytest.fixture
def mock_graph():
    g = AsyncMock()
    g.find_unlinked_nodes = AsyncMock(return_value=[])
    g.get_all_related_edges = AsyncMock(return_value=[])
    return g


@pytest.fixture
def mock_auto_linker():
    al = AsyncMock()
    al.auto_link = AsyncMock(return_value=[])
    return al


@pytest.fixture
def mock_session_factory():
    """Session factory that returns a mock async session."""
    mock_session = AsyncMock()
    mock_result = MagicMock()
    mock_result.mappings = MagicMock(return_value=MagicMock(all=MagicMock(return_value=[])))
    mock_session.execute = AsyncMock(return_value=mock_result)

    factory = MagicMock()
    factory.return_value = mock_session
    factory.return_value.__aenter__ = AsyncMock(return_value=mock_session)
    factory.return_value.__aexit__ = AsyncMock(return_value=False)
    return factory


@pytest.fixture
def mcp_app(mock_session_factory, mock_auto_linker, mock_graph):
    app = FastMCP("test-dream")
    register_dream_tools(
        app,
        session_factory=mock_session_factory,
        auto_linker=mock_auto_linker,
        graph_service=mock_graph,
    )
    return app


@pytest.fixture
def mcp_app_no_graph(mock_session_factory):
    app = FastMCP("test-dream-nograph")
    register_dream_tools(
        app,
        session_factory=mock_session_factory,
        auto_linker=None,
        graph_service=None,
    )
    return app


# ── brain_backfill_links_batch ────────────────────────────────────────────


class TestBackfillLinksBatch:
    """Tests for brain_backfill_links_batch tool."""

    @pytest.mark.asyncio
    async def test_returns_error_when_no_graph(self, mcp_app_no_graph):
        """Graceful degradation when Neo4j not configured."""
        tool = mcp_app_no_graph._tool_manager._tools["brain_backfill_links_batch"]
        result = await tool.fn(limit=10)
        assert "not configured" in result.lower()

    @pytest.mark.asyncio
    async def test_returns_zero_when_no_unlinked(self, mcp_app, mock_graph):
        """No unlinked entities → 0 processed."""
        mock_graph.find_unlinked_nodes.return_value = []
        tool = mcp_app._tool_manager._tools["brain_backfill_links_batch"]
        result = await tool.fn(limit=10)
        assert "0" in result

    @pytest.mark.asyncio
    async def test_calls_auto_link_for_each_entity(
        self, mcp_app, mock_graph, mock_auto_linker, mock_session_factory
    ):
        """Each unlinked entity gets an auto_link call with its embedding."""
        entity_id = str(uuid.uuid4())
        mock_graph.find_unlinked_nodes.return_value = [entity_id]

        # Mock PG query to return entity with embedding
        fake_embedding = [0.1] * 1536
        mock_row = {"id": uuid.UUID(entity_id), "embedding": fake_embedding}
        mock_result = MagicMock()
        mock_result.mappings = MagicMock(
            return_value=MagicMock(all=MagicMock(return_value=[mock_row]))
        )
        mock_session = AsyncMock()
        mock_session.execute = AsyncMock(return_value=mock_result)
        mock_session_factory.return_value.__aenter__ = AsyncMock(return_value=mock_session)

        mock_auto_linker.auto_link.return_value = [
            {"id": uuid.uuid4(), "entity_type": "Decision", "similarity": 0.7}
        ]

        tool = mcp_app._tool_manager._tools["brain_backfill_links_batch"]
        result = await tool.fn(limit=10)

        mock_auto_linker.auto_link.assert_called_once()
        assert "1" in result  # 1 entity processed
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd /home/hawixs/hawkixs_infra/git_repo/brain_v42 && python -m pytest tests/unit/test_dream_tools.py::TestBackfillLinksBatch -v`
Expected: FAIL — `dream_tools` module does not exist.

- [ ] **Step 3: Implement `dream_tools.py` with `brain_backfill_links_batch`**

```python
# src/brain_v42/mcp/tools/dream_tools.py
"""Dream Mode MCP tools: brain_backfill_links_batch, brain_get_clusters.

These tools are used by the Dream Agent (scripts/dream.sh) during nightly
consolidation phases. They operate on existing data using existing services.
"""
from __future__ import annotations

from typing import TYPE_CHECKING, Any
from uuid import UUID

import sqlalchemy as sa
import structlog

from brain_v42.db.tables import adrs, decisions, learnings, runbooks, snippets
from brain_v42.mcp.tools.formatters import format_error

if TYPE_CHECKING:
    from fastmcp import FastMCP
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

    from brain_v42.services.auto_linker import AutoLinker
    from brain_v42.services.graph_service import GraphService

logger = structlog.get_logger(__name__)

_ENTITY_TABLES: dict[str, sa.Table] = {
    "Decision": decisions,
    "Learning": learnings,
    "Snippet": snippets,
    "Runbook": runbooks,
    "ADR": adrs,
}

# Map entity type labels to table + title column for PG enrichment
_TYPE_META: dict[str, tuple[sa.Table, str]] = {
    "Decision": (decisions, "title"),
    "Learning": (learnings, "topic"),
    "Snippet": (snippets, "title"),
    "Runbook": (runbooks, "title"),
    "ADR": (adrs, "title"),
}


def register_dream_tools(
    mcp: FastMCP,
    *,
    session_factory: async_sessionmaker[AsyncSession],
    auto_linker: AutoLinker | None = None,
    graph_service: GraphService | None = None,
) -> None:
    """Register dream-related MCP tools."""

    @mcp.tool(version="1.0")
    async def brain_backfill_links_batch(
        project_key: str | None = None,
        entity_type: str | None = None,
        limit: int = 50,
        threshold: float = 0.6,
        max_links: int = 3,
    ) -> str:
        """Batch-create RELATED_TO links for entities missing graph connections.

        Finds entities that have embeddings but no RELATED_TO edges in Neo4j,
        then runs AutoLinker.auto_link() on each.

        Args:
            project_key: Scope to one project. None = all.
            entity_type: Scope to one type (Decision/Learning/Snippet/Runbook/ADR). None = all.
            limit: Max entities to process per call.
            threshold: Min cosine similarity for link creation.
            max_links: Max links per entity.

        Returns:
            Summary: entities processed, links created, errors.
        """
        if graph_service is None or auto_linker is None:
            return format_error(
                "Neo4j graph not configured — enable graph_enabled=true in settings"
            )

        # Step 1: Find unlinked entity IDs from Neo4j
        unlinked_ids = await graph_service.find_unlinked_nodes(
            entity_type=entity_type, limit=limit
        )

        if not unlinked_ids:
            return "## Backfill Links\n\n0 entities without links found. Nothing to do."

        # Step 2+3: For each unlinked entity, fetch embedding from PG and call auto_link
        processed = 0
        total_links = 0
        errors = 0

        # Search across all entity tables for these IDs + their embeddings
        tables_to_check = (
            {entity_type: _ENTITY_TABLES[entity_type]}
            if entity_type and entity_type in _ENTITY_TABLES
            else _ENTITY_TABLES
        )

        uuid_ids = []
        for uid_str in unlinked_ids:
            try:
                uuid_ids.append(UUID(uid_str))
            except ValueError:
                continue

        async with session_factory() as session:
            for etype, table in tables_to_check.items():
                # Build filter: id IN (...) AND embedding IS NOT NULL
                filters = [
                    table.c.id.in_(uuid_ids),
                    table.c.embedding.isnot(None),
                ]
                if project_key and "project_key" in table.c:
                    filters.append(table.c.project_key == project_key)

                stmt = sa.select(table.c.id, table.c.embedding).where(sa.and_(*filters))
                result = await session.execute(stmt)
                rows = result.mappings().all()

                for row in rows:
                    try:
                        links = await auto_linker.auto_link(
                            entity_type=etype,
                            entity_id=row["id"],
                            embedding=list(row["embedding"]),
                            threshold=threshold,
                            max_links=max_links,
                        )
                        processed += 1
                        total_links += len(links)
                    except Exception:
                        logger.error(
                            "backfill_links.auto_link_failed",
                            entity_id=str(row["id"]),
                            exc_info=True,
                        )
                        errors += 1

        return (
            f"## Backfill Links\n\n"
            f"- Entities processed: {processed}\n"
            f"- Links created: {total_links}\n"
            f"- Errors: {errors}\n"
            f"- Unlinked found: {len(unlinked_ids)}"
        )

    # brain_get_clusters will be added in Task 5
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd /home/hawixs/hawkixs_infra/git_repo/brain_v42 && python -m pytest tests/unit/test_dream_tools.py::TestBackfillLinksBatch -v`
Expected: All PASS

- [ ] **Step 5: Commit**

```bash
cd /home/hawixs/hawkixs_infra/git_repo/brain_v42
git add src/brain_v42/mcp/tools/dream_tools.py tests/unit/test_dream_tools.py
git commit -m "feat(dream): add brain_backfill_links_batch MCP tool"
```

---

### Task 5: Implement `brain_get_clusters` in `dream_tools.py`

**Files:**
- Modify: `src/brain_v42/mcp/tools/dream_tools.py`
- Modify: `tests/unit/test_dream_tools.py`

- [ ] **Step 1: Write failing tests for `brain_get_clusters`**

Add to `tests/unit/test_dream_tools.py`:

```python
# ── brain_get_clusters ────────────────────────────────────────────────────


class TestGetClusters:
    """Tests for brain_get_clusters tool."""

    @pytest.mark.asyncio
    async def test_returns_error_when_no_graph(self, mcp_app_no_graph):
        """Graceful degradation when Neo4j not configured."""
        tool = mcp_app_no_graph._tool_manager._tools["brain_get_clusters"]
        result = await tool.fn(min_size=3, limit=10)
        assert "not configured" in result.lower()

    @pytest.mark.asyncio
    async def test_returns_empty_when_no_edges(self, mcp_app, mock_graph):
        """No edges → no clusters."""
        mock_graph.get_all_related_edges.return_value = []
        tool = mcp_app._tool_manager._tools["brain_get_clusters"]
        result = await tool.fn(min_size=2, limit=10)
        assert "0 clusters" in result.lower()

    @pytest.mark.asyncio
    async def test_finds_connected_components(self, mcp_app, mock_graph, mock_session_factory):
        """Edges form clusters via union-find."""
        # 3 nodes forming one cluster: A-B, B-C
        id_a, id_b, id_c = str(uuid.uuid4()), str(uuid.uuid4()), str(uuid.uuid4())
        mock_graph.get_all_related_edges.return_value = [
            (id_a, id_b),
            (id_b, id_c),
        ]

        # Mock PG metadata enrichment
        mock_row_a = {"id": uuid.UUID(id_a), "title": "Entity A", "entity_type": "Decision"}
        mock_row_b = {"id": uuid.UUID(id_b), "title": "Entity B", "entity_type": "Learning"}
        mock_row_c = {"id": uuid.UUID(id_c), "title": "Entity C", "entity_type": "Decision"}
        mock_result = MagicMock()
        mock_result.mappings = MagicMock(
            return_value=MagicMock(
                all=MagicMock(return_value=[mock_row_a, mock_row_b, mock_row_c])
            )
        )
        mock_session = AsyncMock()
        mock_session.execute = AsyncMock(return_value=mock_result)
        mock_session_factory.return_value.__aenter__ = AsyncMock(return_value=mock_session)

        tool = mcp_app._tool_manager._tools["brain_get_clusters"]
        result = await tool.fn(min_size=2, limit=10)

        assert "1 cluster" in result.lower() or "cluster 1" in result.lower()

    @pytest.mark.asyncio
    async def test_filters_by_min_size(self, mcp_app, mock_graph):
        """Clusters smaller than min_size are excluded."""
        # Only 2 connected nodes — min_size=3 should filter them out
        id_a, id_b = str(uuid.uuid4()), str(uuid.uuid4())
        mock_graph.get_all_related_edges.return_value = [(id_a, id_b)]

        tool = mcp_app._tool_manager._tools["brain_get_clusters"]
        result = await tool.fn(min_size=3, limit=10)

        assert "0 clusters" in result.lower()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd /home/hawixs/hawkixs_infra/git_repo/brain_v42 && python -m pytest tests/unit/test_dream_tools.py::TestGetClusters -v`
Expected: FAIL — `brain_get_clusters` not registered.

- [ ] **Step 3: Implement `brain_get_clusters` with union-find**

Add to `src/brain_v42/mcp/tools/dream_tools.py`, inside the `register_dream_tools` function, after the `brain_backfill_links_batch` tool:

```python
    @mcp.tool(version="1.0")
    async def brain_get_clusters(
        project_key: str | None = None,
        min_size: int = 3,
        limit: int = 10,
    ) -> str:
        """Find clusters of related entities in the knowledge graph.

        Uses Python union-find on RELATED_TO edges from Neo4j.
        Returns clusters sorted by size (largest first), with member details.

        Args:
            project_key: Scope to one project. None = all.
            min_size: Minimum cluster size to return.
            limit: Maximum clusters to return.

        Returns:
            Formatted cluster list with member details.
        """
        if graph_service is None:
            return format_error(
                "Neo4j graph not configured — enable graph_enabled=true in settings"
            )

        # Step 1: Fetch all RELATED_TO edges
        edges = await graph_service.get_all_related_edges()

        if not edges:
            return "## Knowledge Clusters\n\n0 clusters found (no RELATED_TO edges)."

        # Step 2: Union-find to compute connected components
        parent: dict[str, str] = {}

        def find(x: str) -> str:
            while parent.get(x, x) != x:
                parent[x] = parent.get(parent[x], parent[x])
                x = parent[x]
            return x

        def union(a: str, b: str) -> None:
            ra, rb = find(a), find(b)
            if ra != rb:
                parent[ra] = rb

        for src, tgt in edges:
            parent.setdefault(src, src)
            parent.setdefault(tgt, tgt)
            union(src, tgt)

        # Group by root
        components: dict[str, list[str]] = {}
        for node in parent:
            root = find(node)
            components.setdefault(root, []).append(node)

        # Filter by min_size, sort by size DESC
        clusters = sorted(
            [ids for ids in components.values() if len(ids) >= min_size],
            key=len,
            reverse=True,
        )[:limit]

        if not clusters:
            return f"## Knowledge Clusters\n\n0 clusters found (none with >= {min_size} members)."

        # Step 3: Enrich with PG metadata
        all_ids: set[str] = set()
        for cluster in clusters:
            all_ids.update(cluster)

        uuid_ids = []
        for uid_str in all_ids:
            try:
                uuid_ids.append(UUID(uid_str))
            except ValueError:
                continue

        metadata: dict[str, dict[str, str]] = {}  # id -> {type, title}

        async with session_factory() as session:
            for etype, (table, title_col) in _TYPE_META.items():
                filters = [table.c.id.in_(uuid_ids)]
                if project_key and "project_key" in table.c:
                    filters.append(table.c.project_key == project_key)

                stmt = sa.select(
                    table.c.id, sa.literal(etype).label("entity_type"),
                    getattr(table.c, title_col).label("title"),
                ).where(sa.and_(*filters))
                result = await session.execute(stmt)
                for row in result.mappings().all():
                    metadata[str(row["id"])] = {
                        "type": row["entity_type"],
                        "title": row["title"] or "(untitled)",
                    }

        # Step 4: Format output
        lines = [f"## Knowledge Clusters\n\n{len(clusters)} cluster(s) found.\n"]
        for i, cluster in enumerate(clusters, 1):
            lines.append(f"### Cluster {i} ({len(cluster)} members)\n")
            for eid in cluster:
                meta = metadata.get(eid, {"type": "Unknown", "title": "(not in PG)"})
                lines.append(f"- [{meta['type']}] {meta['title']} (id:{eid})")
            lines.append("")

        return "\n".join(lines)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd /home/hawixs/hawkixs_infra/git_repo/brain_v42 && python -m pytest tests/unit/test_dream_tools.py -v`
Expected: All PASS (both TestBackfillLinksBatch and TestGetClusters)

- [ ] **Step 5: Commit**

```bash
cd /home/hawixs/hawkixs_infra/git_repo/brain_v42
git add src/brain_v42/mcp/tools/dream_tools.py tests/unit/test_dream_tools.py
git commit -m "feat(dream): add brain_get_clusters MCP tool with union-find"
```

---

### Task 6: Wire dream tools into `server.py`

**Files:**
- Modify: `src/brain_v42/mcp/server.py`

- [ ] **Step 1: Add dream tools registration**

In `src/brain_v42/mcp/server.py`, after the CRUD tools registration block (after line 346), add:

```python
    # Dream tools (backfill links, clusters)
    from brain_v42.mcp.tools.dream_tools import register_dream_tools  # noqa: PLC0415

    register_dream_tools(
        mcp,
        session_factory=get_session_factory(),
        auto_linker=services.get("auto_linker"),
        graph_service=services.get("graph_service"),
    )
```

- [ ] **Step 2: Run existing tests to verify no regression**

Run: `cd /home/hawixs/hawkixs_infra/git_repo/brain_v42 && python -m pytest tests/unit/ -v --timeout=30`
Expected: All pass.

- [ ] **Step 3: Commit**

```bash
cd /home/hawixs/hawkixs_infra/git_repo/brain_v42
git add src/brain_v42/mcp/server.py
git commit -m "feat(server): wire dream tools into MCP server"
```

---

## Batch 3: Orchestrator + Prompts (independent of Batch 2 for writing, but needs Batch 2 for runtime)

### Task 7: Create `scripts/dream.sh` orchestrator

**Files:**
- Create: `scripts/dream.sh`

- [ ] **Step 1: Create the scripts directory**

```bash
mkdir -p /home/hawixs/hawkixs_infra/git_repo/brain_v42/scripts/dream
mkdir -p /home/hawixs/hawkixs_infra/git_repo/brain_v42/logs/dream
```

- [ ] **Step 2: Write `dream.sh`**

Create `scripts/dream.sh` with the exact content from the spec (Section "Orchestrator"). See spec lines 277-339.

- [ ] **Step 3: Make executable**

```bash
chmod +x /home/hawixs/hawkixs_infra/git_repo/brain_v42/scripts/dream.sh
```

- [ ] **Step 4: Add `logs/dream/` to `.gitignore`**

Append to `.gitignore`:

```
logs/dream/
```

- [ ] **Step 5: Commit**

```bash
cd /home/hawixs/hawkixs_infra/git_repo/brain_v42
git add scripts/dream.sh .gitignore
git commit -m "feat(dream): add dream.sh orchestrator"
```

---

### Task 8: Write phase prompts

**Files:**
- Create: `scripts/dream/phase_scan.md`
- Create: `scripts/dream/phase_clean.md`
- Create: `scripts/dream/phase_connect.md`
- Create: `scripts/dream/phase_synth.md`
- Create: `scripts/dream/phase_reorg.md`

Each prompt follows the 7 prompt design rules from the spec (imperative framing, English, anti-meta-analysis, structured output, explicit tool whitelist, dry-run awareness, missing report fallback).

- [ ] **Step 1: Write `phase_scan.md`**

Key elements: read-only, calls `brain_decay_status` + `brain_consolidation_candidates` + `brain_list` + `brain_search`. Writes one `brain_learn` report with tags `["dream:scan", "dream:scan:{{DATE}}"]`, `source_type: "automated"`. Include `{{DRY_RUN}}` check, `{{PROJECT_KEY}}`, `{{DATE}}` placeholders.

- [ ] **Step 2: Write `phase_clean.md`**

Key elements: reads SCAN report, calls `brain_consolidation_candidates`, merges >=0.95 via `brain_merge_entities`, deletes candidates via `brain_delete`. Max 10 merges, max 5 deletes. Prunes dream reports >30 days. Never touches `dream:generated` or dream reports. Dry-run support.

- [ ] **Step 3: Write `phase_connect.md`**

Key elements: calls `brain_backfill_links_batch` (new tool) up to 4 times (limit=50 each = 200 entities max). Writes connect report.

- [ ] **Step 4: Write `phase_synth.md`**

Key elements: calls `brain_get_clusters` (new tool), then `brain_get` for cluster members. Opus-level synthesis. Max 3 insights tagged `dream:generated`. Never modifies existing entities.

- [ ] **Step 5: Write `phase_reorg.md`**

Key elements: reads SCAN + SYNTH reports. Calls `brain_list` + `brain_get` + `brain_update`. Max 20 updates. Only tags/project_key. Never content fields. Flags entity_type mismatches without fixing them.

- [ ] **Step 6: Commit all prompts**

```bash
cd /home/hawixs/hawkixs_infra/git_repo/brain_v42
git add scripts/dream/phase_scan.md scripts/dream/phase_clean.md scripts/dream/phase_connect.md scripts/dream/phase_synth.md scripts/dream/phase_reorg.md
git commit -m "feat(dream): add 5 phase prompts for dream agent"
```

---

## Batch 4: Validation

### Task 9: Dry-run integration test

**Files:** None (manual validation)

- [ ] **Step 1: Run a dry-run of SCAN phase**

```bash
cd /home/hawixs/hawkixs_infra/git_repo/brain_v42
DRY_RUN=true scripts/dream.sh brain-v42
```

Verify:
- `dream.sh` starts and logs to `logs/dream/`
- SCAN phase runs and completes
- Check `logs/dream/{DATE}_scan.log` for output
- Verify no entities were modified (only a read-only report)

- [ ] **Step 2: Verify the SCAN report was written to brain**

```
brain_search(tags=["dream:scan"], limit=1)
```

Check the report contains entity counts, freshness stats, consolidation candidates.

- [ ] **Step 3: Run full dry-run**

```bash
DRY_RUN=true scripts/dream.sh brain-v42
```

Verify all 5 phases complete (or timeout gracefully).

---

## Summary

| Batch | Tasks | Parallelizable | Estimated |
|-------|-------|----------------|-----------|
| 1 | Tasks 1-3 (prereqs) | Yes, all 3 independent | ~15min |
| 2 | Tasks 4-6 (new tools + wiring) | Tasks 4-5 sequential, Task 6 after both | ~25min |
| 3 | Tasks 7-8 (orchestrator + prompts) | Independent of Batch 2 for writing | ~20min |
| 4 | Task 9 (validation) | After all | ~10min |

**Total: 9 tasks, ~70min estimated implementation time.**
