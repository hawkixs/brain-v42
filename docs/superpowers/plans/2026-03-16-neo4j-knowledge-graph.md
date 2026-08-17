# Neo4j Knowledge Graph Integration — Implementation Plan

> **For agentic workers:** REQUIRED: Use superpowers:subagent-driven-development (if subagents available) or superpowers:executing-plans to implement this plan. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add Neo4j 5 Community as an invisible relationship index alongside PostgreSQL, consolidate MCP tools from 35 to 29, and enrich search results with graph traversals.

**Architecture:** PG remains source of truth. Neo4j stores lightweight nodes (UUID + type + label) and 10 relation types. Write-through sync with graceful degradation. Graph is invisible to LLM — no new MCP tools. Feature-flagged via `BRAIN_GRAPH_ENABLED`.

**Tech Stack:** Python 3.12+, neo4j[async] driver, Neo4j 5 Community, FastMCP, SQLAlchemy async, pgvector, Pydantic 2

**Spec:** `docs/superpowers/specs/2026-03-16-neo4j-knowledge-graph-design.md`

---

## File Structure

### New Files
| File | Responsibility |
|------|----------------|
| `src/brain_v42/models/relation.py` | `RelationInput` Pydantic model for `related_to` param validation |
| `src/brain_v42/services/graph_service.py` | Neo4j async client — upsert nodes, create/delete relations, traversals |
| `src/brain_v42/db/neo4j.py` | Neo4j driver factory (async driver init, close, health) |
| `scripts/init_graph.py` | Populate Neo4j from existing PG data |
| `scripts/reconcile_graph.py` | PG↔Neo4j consistency check |
| `config/project_hierarchy.yml` | Project CONTAINS/DEPENDS_ON definitions |
| `tests/unit/services/test_graph_service.py` | GraphService unit tests (mocked driver) |
| `tests/unit/db/test_neo4j.py` | Neo4j driver factory tests |
| `tests/integration/test_graph_integration.py` | Real Neo4j integration tests |

### Modified Files
| File | Changes |
|------|---------|
| `src/brain_v42/config.py` | Add Neo4j settings (url, user, password, timeout, graph_enabled) |
| `docker-compose.yml` | Add neo4j service |
| `src/brain_v42/mcp/server.py` | Wire GraphService, pass to all entity services |
| `src/brain_v42/services/decision_service.py` | Add graph write-through on create/delete/supersede |
| `src/brain_v42/services/learning_service.py` | Add graph write-through on create/delete |
| `src/brain_v42/services/snippet_service.py` | Add graph write-through on create/delete |
| `src/brain_v42/services/runbook_service.py` | Add graph write-through on create/delete |
| `src/brain_v42/services/adr_service.py` | Add graph write-through on create/delete |
| `src/brain_v42/services/project_context_service.py` | Add graph write-through on create/delete |
| `src/brain_v42/services/brain_service.py` | Add graph enrichment to search/what_do_i_know |
| `src/brain_v42/services/consolidation.py` | Add MERGED_INTO relation on merge |
| `src/brain_v42/mcp/tools/brain_tools.py` | Merge search tools, add related_to, add group_by_type |
| `src/brain_v42/mcp/tools/snippet_tools.py` | Add related_to param to brain_save_snippet |
| `src/brain_v42/mcp/tools/session_tools.py` | Absorb brain_get_project_context |
| `src/brain_v42/mcp/tools/project_context_tools.py` | Remove brain_get_project_context |
| `src/brain_v42/mcp/tools/formatters.py` | Add "Related" section to search result formatters |
| `src/brain_v42/metrics/instrument.py` | Add InstrumentedGraphService |
| `src/brain_v42/metrics/collector.py` | Add record_graph_query, collect_graph_stats |
| `src/brain_v42/metrics/server.py` | Add graph section to /metrics response |
| `src/brain_v42/services/__init__.py` | Export GraphService |
| `src/brain_v42/models/__init__.py` | Export RelationInput |

---

## Chunk 1: Foundation — Config, Docker, Neo4j Driver, GraphService

### Task 1: Config + Docker + Neo4j Driver

**Files:**
- Modify: `src/brain_v42/config.py:38-78` (Settings class)
- Modify: `docker-compose.yml`
- Create: `src/brain_v42/db/neo4j.py`
- Create: `tests/unit/db/test_neo4j.py`

- [ ] **Step 1: Write test for Neo4j driver factory**

```python
# tests/unit/db/test_neo4j.py
"""Tests for Neo4j async driver factory."""
import pytest
from unittest.mock import AsyncMock, patch, MagicMock

from brain_v42.db.neo4j import create_neo4j_driver, close_neo4j_driver, neo4j_healthcheck


class TestCreateNeo4jDriver:
    def test_returns_none_when_url_is_none(self):
        driver = create_neo4j_driver(url=None)
        assert driver is None

    def test_returns_none_when_disabled(self):
        driver = create_neo4j_driver(url="bolt://localhost:7687", enabled=False)
        assert driver is None

    @patch("brain_v42.db.neo4j.AsyncGraphDatabase")
    def test_creates_driver_when_enabled(self, mock_gdb):
        mock_driver = MagicMock()
        mock_gdb.driver.return_value = mock_driver
        driver = create_neo4j_driver(
            url="bolt://localhost:7687",
            user="neo4j",
            password="test",
            enabled=True,
        )
        assert driver is mock_driver
        mock_gdb.driver.assert_called_once_with(
            "bolt://localhost:7687", auth=("neo4j", "test")
        )


class TestNeo4jHealthcheck:
    @pytest.mark.asyncio
    async def test_healthcheck_success(self):
        mock_session = AsyncMock()
        mock_session.run = AsyncMock()
        mock_driver = AsyncMock()
        mock_driver.session.return_value.__aenter__ = AsyncMock(return_value=mock_session)
        mock_driver.session.return_value.__aexit__ = AsyncMock(return_value=False)
        result = await neo4j_healthcheck(mock_driver)
        assert result is True

    @pytest.mark.asyncio
    async def test_healthcheck_failure(self):
        mock_driver = AsyncMock()
        mock_driver.session.side_effect = Exception("connection refused")
        result = await neo4j_healthcheck(mock_driver)
        assert result is False

    @pytest.mark.asyncio
    async def test_healthcheck_none_driver(self):
        result = await neo4j_healthcheck(None)
        assert result is False
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/unit/db/test_neo4j.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'brain_v42.db.neo4j'`

- [ ] **Step 3: Implement neo4j driver factory**

```python
# src/brain_v42/db/neo4j.py
"""Neo4j async driver factory.

Provides create/close/healthcheck for the Neo4j async driver.
Returns None when Neo4j is disabled or URL is not configured.
"""
from __future__ import annotations

import structlog
from neo4j import AsyncGraphDatabase

logger = structlog.get_logger()


def create_neo4j_driver(
    url: str | None,
    user: str = "neo4j",
    password: str = "",
    enabled: bool = True,
):
    """Create Neo4j async driver. Returns None if disabled or no URL."""
    if not url or not enabled:
        logger.info("neo4j_disabled", url=url, enabled=enabled)
        return None
    driver = AsyncGraphDatabase.driver(url, auth=(user, password))
    logger.info("neo4j_driver_created", url=url)
    return driver


async def close_neo4j_driver(driver) -> None:
    """Close Neo4j driver if not None."""
    if driver:
        await driver.close()
        logger.info("neo4j_driver_closed")


async def neo4j_healthcheck(driver) -> bool:
    """Check Neo4j connectivity. Returns False if driver is None or connection fails."""
    if driver is None:
        return False
    try:
        async with driver.session() as session:
            await session.run("RETURN 1")
        return True
    except Exception:
        logger.warning("neo4j_healthcheck_failed", exc_info=True)
        return False
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/unit/db/test_neo4j.py -v`
Expected: PASS (all 5 tests)

- [ ] **Step 5: Add Neo4j settings to config.py**

Add to Settings class in `src/brain_v42/config.py` after `reranker_timeout`:

```python
    # Neo4j (optional — disabled by default)
    neo4j_url: str | None = None
    neo4j_user: str = "neo4j"
    neo4j_password: str = ""
    neo4j_timeout: float = 5.0
    graph_enabled: bool = False
```

Update the docstring at top of config.py — remove "No Redis, Neo4j" statement.

- [ ] **Step 6: Add neo4j service to docker-compose.yml**

Append to `docker-compose.yml` services:

```yaml
  neo4j:
    image: neo4j:5-community
    container_name: brain_v42_neo4j
    ports:
      - "7687:7687"
      - "7474:7474"
    environment:
      NEO4J_AUTH: ${NEO4J_USER:-neo4j}/${NEO4J_PASSWORD:-brain_v42_graph}
      NEO4J_PLUGINS: '[]'
    volumes:
      - ./data/neo4j:/data
    healthcheck:
      test: ["CMD", "cypher-shell", "RETURN 1"]
      interval: 10s
      retries: 5
```

- [ ] **Step 7: Add neo4j[async] dependency to pyproject.toml**

Add `"neo4j[async]>=5.0"` to the `dependencies` list and `"pyyaml>=6.0"` (for project_hierarchy.yml).

- [ ] **Step 8: Export from `src/brain_v42/db/__init__.py`**

Add `from brain_v42.db.neo4j import create_neo4j_driver, close_neo4j_driver, neo4j_healthcheck`.

- [ ] **Step 9: Run all existing tests to verify no regression**

Run: `pytest tests/unit -x -q`
Expected: All existing tests PASS

- [ ] **Step 10: Commit**

```bash
git add src/brain_v42/db/neo4j.py src/brain_v42/db/__init__.py src/brain_v42/config.py \
  docker-compose.yml tests/unit/db/test_neo4j.py pyproject.toml
git commit -m "feat(graph): add Neo4j driver factory, config, docker-compose"
```

---

### Task 2: RelationInput Model

**Files:**
- Create: `src/brain_v42/models/relation.py`
- Modify: `src/brain_v42/models/__init__.py`

- [ ] **Step 1: Write test for RelationInput model**

```python
# Add to tests/unit/models/test_models.py or create tests/unit/models/test_relation.py
import pytest
from pydantic import ValidationError
from brain_v42.models.relation import RelationInput


class TestRelationInput:
    def test_valid_relation(self):
        r = RelationInput(id="550e8400-e29b-41d4-a716-446655440000", type="MOTIVATED_BY")
        assert r.type == "MOTIVATED_BY"

    def test_all_valid_types(self):
        for t in ["MOTIVATED_BY", "IMPLEMENTS", "DOCUMENTS", "USES", "RELATED_TO"]:
            r = RelationInput(id="550e8400-e29b-41d4-a716-446655440000", type=t)
            assert r.type == t

    def test_invalid_type_rejected(self):
        with pytest.raises(ValidationError):
            RelationInput(id="550e8400-e29b-41d4-a716-446655440000", type="INVALID")

    def test_invalid_uuid_rejected(self):
        with pytest.raises(ValidationError):
            RelationInput(id="not-a-uuid", type="MOTIVATED_BY")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/unit/models/test_relation.py -v` (or wherever placed)
Expected: FAIL — `ModuleNotFoundError`

- [ ] **Step 3: Implement RelationInput model**

```python
# src/brain_v42/models/relation.py
"""Pydantic model for entity relation input validation."""
from __future__ import annotations

from typing import Literal
from uuid import UUID

from pydantic import BaseModel, field_validator


class RelationInput(BaseModel):
    """Validated relation input for the related_to parameter on write tools."""

    id: str
    type: Literal["MOTIVATED_BY", "IMPLEMENTS", "DOCUMENTS", "USES", "RELATED_TO"]

    @field_validator("id")
    @classmethod
    def validate_uuid(cls, v: str) -> str:
        UUID(v)  # raises ValueError if invalid
        return v
```

- [ ] **Step 4: Run test to verify it passes + export from `__init__.py`**

Add `from brain_v42.models.relation import RelationInput` to `models/__init__.py`.

Run: `pytest tests/unit/models/ -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/brain_v42/models/relation.py src/brain_v42/models/__init__.py tests/unit/models/
git commit -m "feat(graph): add RelationInput Pydantic model"
```

---

### Task 3: GraphService Core

**Files:**
- Create: `src/brain_v42/services/graph_service.py`
- Create: `tests/unit/services/test_graph_service.py`
- Modify: `src/brain_v42/services/__init__.py`

- [ ] **Step 1: Write tests for GraphService**

Test file: `tests/unit/services/test_graph_service.py`

Test the following methods with mocked Neo4j AsyncDriver:

1. `upsert_node(entity_type, id, props)` — verifies MERGE Cypher with correct label and properties
2. `delete_node(entity_type, id)` — verifies DETACH DELETE Cypher
3. `create_relation(source_id, target_id, rel_type)` — verifies MATCH+MERGE Cypher with correct rel type
4. `delete_relation(source_id, target_id, rel_type)` — verifies MATCH+DELETE Cypher
5. `link_to_project(entity_id, project_key)` — verifies MATCH entity + MATCH Project by project_key + MERGE BELONGS_TO
6. `get_neighbors(id, depth=1)` — verifies traversal Cypher returns list of dicts with id, type, rel, title
7. `get_supersession_chain(decision_id)` — verifies bidirectional chain Cypher returns ordered list of UUIDs
8. `get_project_tree(project_key)` — verifies CONTAINS* traversal returns list of project_keys
9. `get_related_ids(ids)` — verifies batch neighbors query returns dict[UUID, list[dict]]
10. `healthcheck()` — verifies RETURN 1 query
11. Error handling: each method returns gracefully on Neo4j exception (log, don't raise)

Mock pattern for Neo4j async driver:

```python
@pytest.fixture
def mock_driver():
    driver = AsyncMock()
    session = AsyncMock()
    result = AsyncMock()
    result.data = AsyncMock(return_value=[])
    session.run = AsyncMock(return_value=result)
    driver.session.return_value.__aenter__ = AsyncMock(return_value=session)
    driver.session.return_value.__aexit__ = AsyncMock(return_value=False)
    return driver, session
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/unit/services/test_graph_service.py -v`
Expected: FAIL — `ModuleNotFoundError`

- [ ] **Step 3: Implement GraphService**

```python
# src/brain_v42/services/graph_service.py
"""Neo4j async client — lightweight nodes + explicit relations.

Stores identity-only nodes (UUID, type, label) and relationships.
PG remains source of truth. All methods are fault-tolerant: Neo4j
errors are logged but never raised to callers.
"""
from __future__ import annotations

from uuid import UUID

import structlog

logger = structlog.get_logger()


class GraphService:
    def __init__(self, driver, timeout: float = 5.0):
        self._driver = driver
        self._timeout = timeout

    async def upsert_node(self, entity_type: str, id: UUID, props: dict) -> None:
        """Create or update a lightweight node. Label = entity_type."""
        query = f"MERGE (n:{entity_type} {{id: $id}}) SET n += $props"
        await self._run(query, {"id": str(id), "props": props})

    async def delete_node(self, entity_type: str, id: UUID) -> None:
        """Delete node and all its relations."""
        query = f"MATCH (n:{entity_type} {{id: $id}}) DETACH DELETE n"
        await self._run(query, {"id": str(id)})

    async def create_relation(
        self, source_id: UUID, target_id: UUID, rel_type: str, props: dict | None = None
    ) -> None:
        """Create a relation between two entity nodes (matched by id)."""
        query = (
            "MATCH (a {id: $source_id}) "
            "MATCH (b {id: $target_id}) "
            f"MERGE (a)-[r:{rel_type}]->(b)"
        )
        if props:
            query += " SET r += $props"
        params = {"source_id": str(source_id), "target_id": str(target_id)}
        if props:
            params["props"] = props
        await self._run(query, params)

    async def delete_relation(
        self, source_id: UUID, target_id: UUID, rel_type: str
    ) -> None:
        query = (
            "MATCH (a {id: $source_id})-[r:" + rel_type + "]->(b {id: $target_id}) "
            "DELETE r"
        )
        await self._run(query, {"source_id": str(source_id), "target_id": str(target_id)})

    async def link_to_project(self, entity_id: UUID, project_key: str) -> None:
        """Link an entity node to a Project node via BELONGS_TO."""
        query = (
            "MATCH (e {id: $entity_id}) "
            "MATCH (p:Project {project_key: $project_key}) "
            "MERGE (e)-[:BELONGS_TO]->(p)"
        )
        await self._run(query, {"entity_id": str(entity_id), "project_key": project_key})

    async def get_neighbors(
        self, id: UUID, rel_types: list[str] | None = None, depth: int = 1
    ) -> list[dict]:
        """Get neighboring nodes up to given depth."""
        rel_filter = ":" + "|".join(rel_types) if rel_types else ""
        query = (
            f"MATCH (start {{id: $id}})-[r{rel_filter}*1..{depth}]-(neighbor) "
            "RETURN DISTINCT neighbor.id AS id, labels(neighbor)[0] AS type, "
            "type(r[0]) AS rel, neighbor.title AS title, "
            "coalesce(neighbor.topic, neighbor.title) AS label "
            "LIMIT 20"
        )
        return await self._run_read(query, {"id": str(id)})

    async def get_supersession_chain(self, decision_id: UUID) -> list[str]:
        """Get full supersession chain (newest to oldest).
        SUPERSEDES points from new to old: (new)-[:SUPERSEDES]->(old).
        """
        query = """
            MATCH (start:Decision {id: $decision_id})
            OPTIONAL MATCH path_back = (newest:Decision)-[:SUPERSEDES*]->(start)
            WHERE NOT ()-[:SUPERSEDES]->(newest)
            WITH coalesce(newest, start) AS root
            MATCH chain = (root)-[:SUPERSEDES*0..]->(leaf)
            RETURN [n IN nodes(chain) | n.id] AS chain_ids
            ORDER BY length(chain) DESC LIMIT 1
        """
        rows = await self._run_read(query, {"decision_id": str(decision_id)})
        if rows:
            return rows[0]["chain_ids"]
        return [str(decision_id)]

    async def get_project_tree(self, project_key: str) -> list[str]:
        """Get all sub-project keys (recursive CONTAINS traversal)."""
        query = """
            MATCH (root:Project {project_key: $project_key})
            OPTIONAL MATCH (root)-[:CONTAINS*]->(sub:Project)
            RETURN collect(DISTINCT sub.project_key) AS sub_keys
        """
        rows = await self._run_read(query, {"project_key": project_key})
        if rows:
            return rows[0]["sub_keys"]
        return []

    async def get_related_ids(self, ids: list[UUID]) -> dict[str, list[dict]]:
        """Batch: get neighbors for multiple entity IDs.
        Returns {entity_id_str: [{id, type, rel, title}, ...]}.
        Max 5 neighbors per entity.
        """
        if not ids:
            return {}
        query = """
            UNWIND $ids AS eid
            MATCH (e {id: eid})-[r]-(neighbor)
            WHERE neighbor.id <> eid
            WITH eid, neighbor, type(r) AS rel_type, labels(neighbor)[0] AS ntype
            RETURN eid,
                   collect({id: neighbor.id, type: ntype, rel: rel_type,
                            title: coalesce(neighbor.title, neighbor.topic)})[..5] AS neighbors
        """
        rows = await self._run_read(query, {"ids": [str(i) for i in ids]})
        return {row["eid"]: row["neighbors"] for row in rows}

    async def healthcheck(self) -> bool:
        try:
            async with self._driver.session() as session:
                await session.run("RETURN 1")
            return True
        except Exception:
            return False

    # ── Internal ──────────────────────────────────────────────

    async def _run(self, query: str, params: dict) -> None:
        """Execute a write query. Logs errors, never raises."""
        try:
            async with self._driver.session() as session:
                await session.run(query, params, timeout=self._timeout)
        except Exception:
            logger.error("neo4j_write_failed", query=query[:100], exc_info=True)

    async def _run_read(self, query: str, params: dict) -> list[dict]:
        """Execute a read query. Returns list of record dicts. Logs errors, returns [] on failure."""
        try:
            async with self._driver.session() as session:
                result = await session.run(query, params, timeout=self._timeout)
                return [dict(record) async for record in result]
        except Exception:
            logger.error("neo4j_read_failed", query=query[:100], exc_info=True)
            return []
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/unit/services/test_graph_service.py -v`
Expected: PASS

- [ ] **Step 5: Export from services/__init__.py**

Add `from brain_v42.services.graph_service import GraphService` to `services/__init__.py`.

- [ ] **Step 6: Run all unit tests**

Run: `pytest tests/unit -x -q`
Expected: All PASS

- [ ] **Step 7: Commit**

```bash
git add src/brain_v42/services/graph_service.py src/brain_v42/services/__init__.py \
  tests/unit/services/test_graph_service.py
git commit -m "feat(graph): add GraphService with Neo4j traversals"
```

---

## Chunk 2: Write-Through Integration

### Task 4: Wire GraphService in server.py

**Files:**
- Modify: `src/brain_v42/mcp/server.py` (lines ~77-150)

- [ ] **Step 1: Write test verifying graph wiring**

Add to `tests/unit/mcp/test_server.py`: test that when `settings.graph_enabled=True` and `settings.neo4j_url` is set, GraphService is passed to entity services. When disabled, `graph=None`.

- [ ] **Step 2: Add GraphService instantiation to server.py**

After the embedding service creation (~line 77), add:

```python
from brain_v42.db.neo4j import create_neo4j_driver
from brain_v42.services.graph_service import GraphService

# Neo4j graph (optional)
neo4j_driver = create_neo4j_driver(
    url=settings.neo4j_url,
    user=settings.neo4j_user,
    password=settings.neo4j_password,
    enabled=settings.graph_enabled,
)
graph_service = GraphService(neo4j_driver, timeout=settings.neo4j_timeout) if neo4j_driver else None
```

Pass `graph=graph_service` to all entity service constructors (DecisionService, LearningService, etc.).

Add cleanup in the shutdown handler:

```python
from brain_v42.db.neo4j import close_neo4j_driver

# In shutdown/lifespan:
await close_neo4j_driver(neo4j_driver)
```

- [ ] **Step 3: Run server tests to verify they pass**

Run: `pytest tests/unit/mcp/test_server.py -v`
Expected: PASS

- [ ] **Step 4: Commit**

```bash
git add src/brain_v42/mcp/server.py
git commit -m "feat(graph): wire GraphService in MCP server"
```

---

### Task 5: Write-through in DecisionService

**Files:**
- Modify: `src/brain_v42/services/decision_service.py`
- Modify: `tests/unit/services/test_decision_service.py`

- [ ] **Step 1: Write test for graph write-through on create**

```python
# In test_decision_service.py
class TestDecisionServiceGraphWriteThrough:
    @pytest.mark.asyncio
    async def test_create_calls_graph_upsert(self, decision_service_with_graph):
        """Creating a decision upserts a node and links to project in Neo4j."""
        svc = decision_service_with_graph
        data = DecisionCreate(title="Test", description="d", reasoning="r", project_key="test_proj")
        await svc.create(data)
        svc.graph.upsert_node.assert_called_once()
        svc.graph.link_to_project.assert_called_once()

    @pytest.mark.asyncio
    async def test_create_with_related_to(self, decision_service_with_graph):
        """related_to creates graph relations."""
        svc = decision_service_with_graph
        data = DecisionCreate(title="Test", description="d", reasoning="r")
        related = [{"id": "550e8400-e29b-41d4-a716-446655440000", "type": "MOTIVATED_BY"}]
        await svc.create(data, related_to=related)
        svc.graph.create_relation.assert_called_once()

    @pytest.mark.asyncio
    async def test_create_graph_failure_does_not_raise(self, decision_service_with_graph):
        """Graph failure is logged, not raised — PG write still succeeds."""
        svc = decision_service_with_graph
        svc.graph.upsert_node.side_effect = Exception("neo4j down")
        data = DecisionCreate(title="Test", description="d", reasoning="r")
        result = await svc.create(data)
        assert result is not None  # PG write succeeded

    @pytest.mark.asyncio
    async def test_delete_removes_graph_node(self, decision_service_with_graph):
        svc = decision_service_with_graph
        await svc.delete(some_uuid)
        svc.graph.delete_node.assert_called_once()

    @pytest.mark.asyncio
    async def test_supersede_creates_supersedes_relation(self, decision_service_with_graph):
        svc = decision_service_with_graph
        await svc.supersede(old_id, new_data)
        # Should create new node + SUPERSEDES relation
        svc.graph.upsert_node.assert_called()
        svc.graph.create_relation.assert_called()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/unit/services/test_decision_service.py::TestDecisionServiceGraphWriteThrough -v`
Expected: FAIL

- [ ] **Step 3: Add graph parameter and write-through to DecisionService**

Modify `decision_service.py`:
- Add `graph: GraphService | None = None` to `__init__`
- In `create()`: after PG write, call graph.upsert_node + link_to_project + related_to relations (wrapped in try/except)
- In `delete()`: after PG delete, call graph.delete_node (wrapped in try/except)
- In `supersede()`: after PG supersede, call graph.upsert_node for new + graph.create_relation SUPERSEDES (wrapped in try/except)

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/unit/services/test_decision_service.py -v`
Expected: All PASS (new + existing)

- [ ] **Step 5: Commit**

```bash
git add src/brain_v42/services/decision_service.py tests/unit/services/test_decision_service.py
git commit -m "feat(graph): write-through in DecisionService"
```

---

### Task 6a: Write-through in Learning, Snippet, Runbook services

**Files:**
- Modify: `src/brain_v42/services/learning_service.py`
- Modify: `src/brain_v42/services/snippet_service.py`
- Modify: `src/brain_v42/services/runbook_service.py`
- Modify: `tests/unit/services/test_learning_service.py`
- Modify: `tests/unit/services/test_snippet_service.py`
- Modify: `tests/unit/services/test_runbook_service.py`

Same pattern as Task 5 (DecisionService) — TDD order:

- [ ] **Step 1: Write tests for graph write-through on all 3 services**

For each service, test: create calls graph.upsert_node + link_to_project, delete calls graph.delete_node, graph failure does not raise. Same test pattern as Task 5.

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/unit/services/test_learning_service.py tests/unit/services/test_snippet_service.py tests/unit/services/test_runbook_service.py -v -k graph`
Expected: FAIL

- [ ] **Step 3: Add `graph: GraphService | None = None` to each service __init__**
- [ ] **Step 4: Add graph.upsert_node + link_to_project in create() — try/except**
- [ ] **Step 5: Add graph.delete_node in delete() — try/except**
- [ ] **Step 6: Run tests to verify they pass**

Run: `pytest tests/unit/services/test_learning_service.py tests/unit/services/test_snippet_service.py tests/unit/services/test_runbook_service.py -v`
Expected: All PASS

- [ ] **Step 7: Commit**

```bash
git add src/brain_v42/services/learning_service.py src/brain_v42/services/snippet_service.py \
  src/brain_v42/services/runbook_service.py tests/unit/services/
git commit -m "feat(graph): write-through in Learning, Snippet, Runbook services"
```

---

### Task 6b: Write-through in ADR, ProjectContext, Consolidation

**Files:**
- Modify: `src/brain_v42/services/adr_service.py`
- Modify: `src/brain_v42/services/project_context_service.py`
- Modify: `src/brain_v42/services/consolidation.py`
- Modify: `tests/unit/services/test_adr_service.py`
- Modify: `tests/unit/services/test_project_context_service.py`

These have special behaviors:
- ProjectContextService creates `Project` nodes (not entity nodes)
- Consolidation adds `MERGED_INTO` relation

- [ ] **Step 1: Write tests for graph write-through**

For ADR: same pattern as Task 5. For ProjectContext: test that create() calls `graph.upsert_node("Project", ...)`. For Consolidation: test that merge() calls `graph.create_relation(source, target, "MERGED_INTO")`.

- [ ] **Step 2: Run tests to verify they fail**
- [ ] **Step 3: Add `graph: GraphService | None = None` to each service __init__**
- [ ] **Step 4: Implement graph calls in create/delete/merge — try/except**
- [ ] **Step 5: Run tests to verify they pass**

Run: `pytest tests/unit/services/ -v`
Expected: All PASS

- [ ] **Step 6: Commit**

```bash
git add src/brain_v42/services/adr_service.py src/brain_v42/services/project_context_service.py \
  src/brain_v42/services/consolidation.py tests/unit/services/
git commit -m "feat(graph): write-through in ADR, ProjectContext, Consolidation"
```

---

## Chunk 3: Traversals + Search Enrichment

### Task 7: Supersession chain via Neo4j

**Files:**
- Modify: `src/brain_v42/services/decision_service.py`
- Modify: `src/brain_v42/mcp/tools/brain_tools.py` (brain_get_supersession_chain tool)
- Modify: `tests/unit/services/test_decision_service.py`

- [ ] **Step 1: Write test for graph-based supersession chain**

```python
class TestSupersessionChainGraph:
    @pytest.mark.asyncio
    async def test_chain_delegates_to_graph_when_available(self, decision_service_with_graph):
        svc = decision_service_with_graph
        svc.graph.get_supersession_chain.return_value = ["uuid-new", "uuid-old"]
        result = await svc.get_supersession_chain(some_uuid)
        svc.graph.get_supersession_chain.assert_called_once()

    @pytest.mark.asyncio
    async def test_chain_falls_back_to_pg_when_no_graph(self, decision_service_no_graph):
        svc = decision_service_no_graph
        result = await svc.get_supersession_chain(some_uuid)
        # Should use PG repo method (existing behavior)
        svc.repo.get_supersession_chain.assert_called_once()
```

- [ ] **Step 2: Implement: if self.graph, delegate to graph.get_supersession_chain, else fall back to repo CTE**
- [ ] **Step 3: Run tests**
- [ ] **Step 4: Commit**

```bash
git commit -m "feat(graph): supersession chain delegates to Neo4j"
```

---

### Task 8: Enrich brain_search with graph neighbors

**Files:**
- Modify: `src/brain_v42/services/brain_service.py`
- Modify: `src/brain_v42/mcp/tools/formatters.py`
- Modify: `tests/unit/services/test_brain_service.py`

- [ ] **Step 1: Write test for graph-enriched search**

```python
class TestBrainServiceGraphEnrichment:
    @pytest.mark.asyncio
    async def test_search_includes_related_when_graph_available(self, brain_service_with_graph):
        svc = brain_service_with_graph
        svc.graph.get_related_ids.return_value = {
            "some-uuid": [{"id": "other-uuid", "type": "Learning", "rel": "MOTIVATED_BY", "title": "test"}]
        }
        results = await svc.search("test query")
        svc.graph.get_related_ids.assert_called_once()

    @pytest.mark.asyncio
    async def test_search_works_without_graph(self, brain_service_no_graph):
        results = await brain_service_no_graph.search("test query")
        # Should work identically to current behavior
        assert results is not None
```

- [ ] **Step 2: Implement graph enrichment in BrainService.search()**

After the fan-out search completes and results are collected:

```python
# In brain_service.py search() method, after collecting results:
if self.graph and results:
    try:
        result_ids = [UUID(r.id) for r in results if hasattr(r, 'id')]
        related = await self.graph.get_related_ids(result_ids)
        # Attach related info to results (add as metadata or separate field)
    except Exception:
        logger.error("graph_enrichment_failed", exc_info=True)
```

- [ ] **Step 3: Add "Related" section to formatters.py**

Add a `format_related_section(related: list[dict]) -> str` function that formats graph neighbors:

```
### Related
- MOTIVATED_BY: "Learning title" (Learning, id:abc123)
- IMPLEMENTS: "Decision title" (Decision, id:def456)
```

- [ ] **Step 4: Run tests**
- [ ] **Step 5: Commit**

```bash
git commit -m "feat(graph): enrich brain_search with graph neighbors"
```

---

### Task 9: Project tree traversal in search

**Files:**
- Modify: `src/brain_v42/services/brain_service.py`

- [ ] **Step 1: Write test**

When `brain_search(project_key="red")` is called and graph is available, it should also search sub-projects returned by `graph.get_project_tree("red")`.

- [ ] **Step 2: Implement in BrainService.search()**

Before the fan-out, if graph is available and project_key is set:

```python
project_keys = [project_key]
if self.graph and project_key:
    try:
        sub_keys = await self.graph.get_project_tree(project_key)
        project_keys.extend(sub_keys)
    except Exception:
        pass
# Fan out search across all project_keys
```

- [ ] **Step 3: Run tests**
- [ ] **Step 4: Commit**

```bash
git commit -m "feat(graph): search across project tree"
```

---

## Chunk 4: Tool Consolidation

### Task 10: Merge search tools into brain_search

**Files:**
- Modify: `src/brain_v42/mcp/tools/brain_tools.py`
- Modify: `src/brain_v42/mcp/tools/snippet_tools.py`
- Modify: `src/brain_v42/mcp/tools/project_context_tools.py`
- Modify: `tests/unit/mcp/tools/test_brain_decision_tools.py`
- Modify: `tests/unit/mcp/tools/test_brain_learn_tools.py`

Remove these tools (their functionality is covered by `brain_search(types=[...])` which already exists):
- `brain_search_decisions` → `brain_search(types=["decision"])`
- `brain_recall` → `brain_search(types=["learning"])`
- `brain_find_snippet` → `brain_search(types=["snippet"])`
- `brain_search_runbooks` → `brain_search(types=["runbook"])`
- `brain_what_do_i_know_about` → `brain_search(group_by_type=true)`

- [ ] **Step 1: Add `group_by_type: bool = False` param to brain_search tool**

When `group_by_type=True`, the formatter groups results by entity type (reproducing what_do_i_know_about behavior).

- [ ] **Step 2: Write test verifying brain_search with group_by_type=True**
- [ ] **Step 3: Remove 3 merged tool functions from `src/brain_v42/mcp/tools/brain_tools.py`**: `brain_search_decisions`, `brain_recall`, `brain_what_do_i_know_about`
- [ ] **Step 4: Remove `brain_find_snippet` from `src/brain_v42/mcp/tools/snippet_tools.py`**
- [ ] **Step 5: Remove `brain_search_runbooks` from `src/brain_v42/mcp/tools/runbook_tools.py`**
- [ ] **Step 6: Update existing tests — replace calls to removed tools with brain_search equivalents**
- [ ] **Step 7: Run all tool tests**

Run: `pytest tests/unit/mcp/tools/ -v`
Expected: All PASS

- [ ] **Step 8: Commit**

```bash
git commit -m "refactor(tools): merge 5 search tools into brain_search"
```

---

### Task 11: Merge brain_get_project_context into brain_session_start

**Files:**
- Modify: `src/brain_v42/mcp/tools/session_tools.py`
- Modify: `src/brain_v42/mcp/tools/project_context_tools.py`

- [ ] **Step 1: Remove brain_get_project_context tool from project_context_tools.py**

The `brain_session_start` tool already returns project context. Remove the duplicate.

- [ ] **Step 2: Update tests**
- [ ] **Step 3: Run tests**
- [ ] **Step 4: Commit**

```bash
git commit -m "refactor(tools): merge brain_get_project_context into session_start"
```

---

### Task 12: Add related_to param to write tools

**Files:**
- Modify: `src/brain_v42/mcp/tools/brain_tools.py` (brain_log_decision, brain_learn)
- Modify: `src/brain_v42/mcp/tools/snippet_tools.py` (brain_save_snippet)
- Modify: `src/brain_v42/mcp/tools/crud_tools.py` (brain_update)

- [ ] **Step 1: Add `related_to: list[dict] | None = None` param to each write tool**

In each tool function, validate via RelationInput and pass to service:

```python
from brain_v42.models.relation import RelationInput

async def brain_log_decision(..., related_to: list[dict] | None = None):
    validated_relations = None
    if related_to:
        validated_relations = [RelationInput(**r).model_dump() for r in related_to]
    # Pass validated_relations to service.create(..., related_to=validated_relations)
```

- [ ] **Step 2: Write tests for related_to validation (valid + invalid)**
- [ ] **Step 3: Run tests**
- [ ] **Step 4: Commit**

```bash
git commit -m "feat(tools): add related_to param to write tools"
```

---

## Chunk 5: Metrics Update

### Task 13: InstrumentedGraphService + Metrics

**Files:**
- Modify: `src/brain_v42/metrics/instrument.py`
- Modify: `src/brain_v42/metrics/collector.py`
- Modify: `src/brain_v42/metrics/server.py`
- Modify: `src/brain_v42/mcp/server.py`

- [ ] **Step 1: Write test for InstrumentedGraphService**

```python
class TestInstrumentedGraphService:
    @pytest.mark.asyncio
    async def test_upsert_records_graph_query(self):
        inner = AsyncMock()
        collector = MagicMock()
        svc = InstrumentedGraphService(inner, collector)
        await svc.upsert_node("Decision", some_uuid, {"title": "test"})
        collector.record_graph_query.assert_called_once()
        inner.upsert_node.assert_called_once()
```

- [ ] **Step 2: Implement InstrumentedGraphService in instrument.py**

Same pattern as InstrumentedEmbeddingService/InstrumentedReranker: wrap inner, record latency/errors.

- [ ] **Step 3: Add `record_graph_query(latency_ms, error)` to MetricsCollector**
- [ ] **Step 4: Add `collect_graph_stats()` to MetricsCollector — queries Neo4j for node/relation counts**
- [ ] **Step 5: Add `graph` section to MetricsServer._handle_metrics response**
- [ ] **Step 6: Wire InstrumentedGraphService in server.py (wrap graph_service if metrics_enabled)**
- [ ] **Step 7: Run all metrics tests + server tests**
- [ ] **Step 8: Commit**

```bash
git commit -m "feat(metrics): add graph monitoring to sidecar"
```

---

## Chunk 6: Migration Scripts

### Task 14: init_graph.py + project_hierarchy.yml

**Files:**
- Create: `scripts/init_graph.py`
- Create: `config/project_hierarchy.yml`

- [ ] **Step 1: Create project_hierarchy.yml**

```yaml
project_hierarchy:
  red:
    contains: [red-monitor, red-orchestrator, auto-discord, brain_v42, lyriks-v3]
    depends_on: []
  red-monitor:
    depends_on: [brain_v42]
```

- [ ] **Step 2: Implement init_graph.py**

Script that:
1. Connects to PG (reads all entities) and Neo4j
2. **Creates uniqueness constraints first** (required before MERGE for performance):
   ```cypher
   CREATE CONSTRAINT decision_id IF NOT EXISTS FOR (d:Decision) REQUIRE d.id IS UNIQUE;
   CREATE CONSTRAINT learning_id IF NOT EXISTS FOR (l:Learning) REQUIRE l.id IS UNIQUE;
   CREATE CONSTRAINT snippet_id  IF NOT EXISTS FOR (s:Snippet)  REQUIRE s.id IS UNIQUE;
   CREATE CONSTRAINT runbook_id  IF NOT EXISTS FOR (r:Runbook)  REQUIRE r.id IS UNIQUE;
   CREATE CONSTRAINT adr_id      IF NOT EXISTS FOR (a:ADR)      REQUIRE a.id IS UNIQUE;
   CREATE CONSTRAINT project_pk  IF NOT EXISTS FOR (p:Project)  REQUIRE p.project_key IS UNIQUE;
   ```
3. Creates Project nodes from project_contexts
4. Creates entity nodes (Decision, Learning, Snippet, Runbook, ADR) — lightweight
5. Creates BELONGS_TO relations
6. Creates SUPERSEDES from decisions.superseded_by
7. Creates MERGED_INTO from merged_into fields
8. Creates CONTAINS/DEPENDS_ON from project_hierarchy.yml
9. Uses MERGE (idempotent)
10. Reports counts created

- [ ] **Step 3: Test locally against dev containers**

```bash
docker compose up -d postgres neo4j
python scripts/init_graph.py
```
Expected: nodes and relations created, verify in Neo4j browser at localhost:7474

- [ ] **Step 4: Commit**

```bash
git add scripts/init_graph.py config/project_hierarchy.yml
git commit -m "feat(graph): add init_graph.py migration script"
```

---

### Task 15: reconcile_graph.py

**Files:**
- Create: `scripts/reconcile_graph.py`

- [ ] **Step 1: Implement reconcile_graph.py**

Script that:
1. Reads all entity IDs from PG
2. Reads all node IDs from Neo4j
3. Reports: missing nodes (in PG but not Neo4j), orphan nodes (in Neo4j but not PG)
4. Creates missing nodes, deletes orphans (with --fix flag)
5. Checks relation integrity (superseded_by, merged_into)

- [ ] **Step 2: Test locally**
- [ ] **Step 3: Commit**

```bash
git add scripts/reconcile_graph.py
git commit -m "feat(graph): add reconcile_graph.py consistency check"
```

---

## Chunk 7: Integration & E2E Tests

### Task 16: Integration tests with real Neo4j

**Files:**
- Create: `tests/integration/test_graph_integration.py`
- Modify: `tests/integration/conftest.py`

- [ ] **Step 1: Add Neo4j fixture to conftest.py**

```python
@pytest.fixture(scope="session")
async def neo4j_driver():
    driver = AsyncGraphDatabase.driver("bolt://localhost:7687", auth=("neo4j", "brain_v42_graph"))
    yield driver
    # Cleanup: delete all test nodes
    async with driver.session() as s:
        await s.run("MATCH (n) WHERE n.id STARTS WITH 'test-' DETACH DELETE n")
    await driver.close()

@pytest.fixture
async def graph_service(neo4j_driver):
    return GraphService(neo4j_driver)
```

- [ ] **Step 2: Write integration tests**

1. Upsert node + verify exists
2. Create relation + get_neighbors
3. Supersession chain (create A←B←C, query from C, verify chain A,B,C)
4. Project tree (create P1→CONTAINS→P2→CONTAINS→P3, verify tree)
5. link_to_project + BELONGS_TO traversal
6. get_related_ids bulk query
7. delete_node removes node + relations
8. Graceful degradation: GraphService with broken driver → methods return empty, don't raise

- [ ] **Step 3: Run integration tests**

```bash
docker compose up -d postgres neo4j
pytest tests/integration/test_graph_integration.py -v
```

- [ ] **Step 4: Commit**

```bash
git add tests/integration/test_graph_integration.py tests/integration/conftest.py
git commit -m "test(graph): integration tests with real Neo4j"
```

---

### Task 17: E2E test updates

**Files:**
- Modify existing E2E test file or create `tests/integration/test_graph_e2e.py`

- [ ] **Step 1: Write E2E tests**

1. brain_log_decision with related_to → verify graph relation created
2. brain_search returns "Related" section when graph is available
3. brain_supersede_decision → verify supersession chain works bidirectionally
4. brain_search(project_key="red") → returns results from sub-projects
5. Consolidated tools: brain_search(types=["learning"]) works (replaces brain_recall)

- [ ] **Step 2: Run full E2E suite**
- [ ] **Step 3: Commit**

```bash
git commit -m "test(graph): E2E tests for graph-enriched tools"
```

---

### Task 18: Final verification + lint

- [ ] **Step 1: Run full test suite**

```bash
pytest tests/unit -v --tb=short
pytest tests/integration -v --tb=short
```

- [ ] **Step 2: Lint**

```bash
ruff check src/ tests/
ruff format src/ tests/
```

- [ ] **Step 3: Verify tool count**

Count registered MCP tools in server.py — should be 29.

- [ ] **Step 4: Final commit**

```bash
git commit -m "chore: lint + verify 29 tools registered"
```
