# MCP Tools Completeness Implementation Plan

> **For agentic workers:** REQUIRED: Use superpowers:subagent-driven-development (if subagents available) or superpowers:executing-plans to implement this plan. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add 8 missing MCP tools (generic CRUD + specific) and fix offset/docstrings to bring brain_v42 from 27 to 35 tools.

**Architecture:** Hybrid approach — generic tools (`brain_get`, `brain_delete`, `brain_update`, `brain_list`) in new `crud_tools.py` dispatching by `entity_type`, specific tools for ADR deprecation, project listing, runbook search, and supersession chain. Follows existing `decay_tools.py` generic dispatch pattern.

**Tech Stack:** Python 3.12+, FastMCP 3.1, SQLAlchemy 2.0 async, Pydantic 2, pytest, structlog

**Spec:** `docs/superpowers/specs/2026-03-15-mcp-tools-completeness-design.md`

---

## Batch 1: Service Layer Additions (parallel)

### Task 1: ADRService — add deprecate(), delete(), update()

**Files:**
- Modify: `src/brain_v42/services/adr_service.py`
- Test: `tests/unit/services/test_adr_service.py`

- [ ] **Step 1: Write failing tests for deprecate()**

```python
# In tests/unit/services/test_adr_service.py

class TestDeprecate:
    @pytest.mark.asyncio
    async def test_deprecate_sets_status_deprecated(
        self, service_with_embedding: ADRService, mock_repo: MagicMock
    ) -> None:
        adr = make_adr(status="accepted")
        mock_repo.update = AsyncMock(return_value=adr)
        mock_repo.get_by_id = AsyncMock(return_value=adr)
        result = await service_with_embedding.deprecate(adr.id, reason="Replaced by new approach")
        assert result is not None
        mock_repo.update.assert_called_once()

    @pytest.mark.asyncio
    async def test_deprecate_returns_none_if_not_found(
        self, service: ADRService, mock_repo: MagicMock
    ) -> None:
        mock_repo.get_by_id = AsyncMock(return_value=None)
        result = await service.deprecate(uuid.uuid4())
        assert result is None
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/unit/services/test_adr_service.py::TestDeprecate -v`
Expected: FAIL — `ADRService has no attribute 'deprecate'`

- [ ] **Step 3: Implement deprecate()**

```python
# In src/brain_v42/services/adr_service.py, add after accept():

    async def deprecate(self, adr_id: UUID, reason: str | None = None) -> ADR | None:
        """Deprecate an ADR. Appends reason to consequences field."""
        from brain_v42.models.adr import ADRUpdate  # noqa: PLC0415

        existing = await self._repo.get_by_id(adr_id)
        if existing is None:
            return None
        consequences = existing.consequences or ""
        if reason:
            consequences = f"{consequences}\n\nDeprecated: {reason}".strip()
        data = ADRUpdate(status="deprecated", consequences=consequences)
        result = await self._repo.update(adr_id, data)
        if result:
            logger.info("adr_service.deprecate", adr_id=str(adr_id), reason=reason)
        return result
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/unit/services/test_adr_service.py::TestDeprecate -v`
Expected: PASS

- [ ] **Step 5: Write failing tests for delete() and update()**

```python
class TestDelete:
    @pytest.mark.asyncio
    async def test_delete_delegates_to_repo(
        self, service: ADRService, mock_repo: MagicMock
    ) -> None:
        mock_repo.delete = AsyncMock(return_value=True)
        result = await service.delete(uuid.uuid4())
        assert result is True
        mock_repo.delete.assert_called_once()

    @pytest.mark.asyncio
    async def test_delete_returns_false_if_not_found(
        self, service: ADRService, mock_repo: MagicMock
    ) -> None:
        mock_repo.delete = AsyncMock(return_value=False)
        result = await service.delete(uuid.uuid4())
        assert result is False


class TestUpdate:
    @pytest.mark.asyncio
    async def test_update_delegates_to_repo(
        self, service: ADRService, mock_repo: MagicMock
    ) -> None:
        from brain_v42.models.adr import ADRUpdate
        adr = make_adr()
        mock_repo.update = AsyncMock(return_value=adr)
        data = ADRUpdate(title="Updated title")
        result = await service.update(adr.id, data)
        assert result is not None
        mock_repo.update.assert_called_once()

    @pytest.mark.asyncio
    async def test_update_re_embeds_on_text_change(
        self, service_with_embedding: ADRService, mock_repo: MagicMock, mock_embedding_svc: MagicMock
    ) -> None:
        from brain_v42.models.adr import ADRUpdate
        adr = make_adr()
        mock_repo.update = AsyncMock(return_value=adr)
        mock_repo.get_by_id = AsyncMock(return_value=adr)
        data = ADRUpdate(title="New title")
        await service_with_embedding.update(adr.id, data)
        mock_embedding_svc.embed.assert_called_once()

    @pytest.mark.asyncio
    async def test_update_returns_none_if_not_found(
        self, service: ADRService, mock_repo: MagicMock
    ) -> None:
        from brain_v42.models.adr import ADRUpdate
        mock_repo.update = AsyncMock(return_value=None)
        result = await service.update(uuid.uuid4(), ADRUpdate(title="x"))
        assert result is None
```

- [ ] **Step 6: Run tests to verify they fail**

Run: `pytest tests/unit/services/test_adr_service.py::TestDelete tests/unit/services/test_adr_service.py::TestUpdate -v`
Expected: FAIL — `ADRService has no attribute 'delete'`/`'update'`

- [ ] **Step 7: Implement delete() and update()**

```python
# In src/brain_v42/services/adr_service.py, add after deprecate():

    async def delete(self, adr_id: UUID) -> bool:
        """Hard delete an ADR by UUID."""
        result = await self._repo.delete(adr_id)
        if result:
            logger.info("adr_service.delete", adr_id=str(adr_id))
        return result

    async def update(self, adr_id: UUID, data: ADRUpdate) -> ADR | None:
        """Update ADR fields. Re-embeds if title/context/decision changed."""
        from brain_v42.models.adr import ADRUpdate as ADRUpdateModel  # noqa: PLC0415

        embedding: list[float] | None = None
        if self._embedding_svc and any(
            getattr(data, f) is not None for f in ("title", "context", "decision")
        ):
            existing = await self._repo.get_by_id(adr_id)
            if existing is None:
                return None
            title = data.title if data.title is not None else existing.title
            context = data.context if data.context is not None else existing.context
            decision = data.decision if data.decision is not None else existing.decision
            embedding = await self._maybe_embed(f"{title} {context} {decision}")
        result = await self._repo.update(adr_id, data, embedding=embedding)
        if result:
            logger.info("adr_service.update", adr_id=str(adr_id))
        return result
```

Also update import at top of file:
```python
from brain_v42.models.adr import ADR, ADRCreate, ADRUpdate
```

- [ ] **Step 8: Run tests to verify they pass**

Run: `pytest tests/unit/services/test_adr_service.py -v`
Expected: ALL PASS

- [ ] **Step 9: Commit**

```bash
git add src/brain_v42/services/adr_service.py tests/unit/services/test_adr_service.py
git commit -m "feat(adr-service): add deprecate, delete, update methods"
```

---

### Task 2: ProjectContextService — add list_all()

**Files:**
- Modify: `src/brain_v42/services/project_context_service.py`
- Test: `tests/unit/services/test_project_context_service.py`

- [ ] **Step 1: Write failing test**

```python
# In tests/unit/services/test_project_context_service.py, add:
# NOTE: This file uses inline setup, no fixtures. Follow existing pattern.

import pytest
from unittest.mock import AsyncMock, MagicMock
from brain_v42.services.project_context_service import ProjectContextService


class TestListAll:
    @pytest.mark.asyncio
    async def test_list_all_delegates_to_repo(self):
        repo = MagicMock()
        repo.list_all = AsyncMock(return_value=[])
        svc = ProjectContextService(pg_repo=repo)
        result = await svc.list_all()
        assert result == []
        repo.list_all.assert_called_once()

    @pytest.mark.asyncio
    async def test_list_all_returns_project_contexts(self):
        ctx = make_project_context()
        repo = MagicMock()
        repo.list_all = AsyncMock(return_value=[ctx])
        svc = ProjectContextService(pg_repo=repo)
        result = await svc.list_all()
        assert len(result) == 1
        assert result[0].project_key == ctx.project_key
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/unit/services/test_project_context_service.py::TestListAll -v`
Expected: FAIL — `ProjectContextService has no attribute 'list_all'`

- [ ] **Step 3: Implement list_all()**

```python
# In src/brain_v42/services/project_context_service.py, add after refresh_counts():

    async def list_all(self) -> list[ProjectContext]:
        """List all project contexts."""
        logger.debug("project_context_service.list_all")
        return await self.repo.list_all()
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/unit/services/test_project_context_service.py::TestListAll -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/brain_v42/services/project_context_service.py tests/unit/services/test_project_context_service.py
git commit -m "feat(project-context-service): add list_all method"
```

---

## Batch 2: Formatters (parallel with Batch 1)

### Task 3: New formatter functions

**Files:**
- Modify: `src/brain_v42/mcp/tools/formatters.py`
- Modify: `tests/unit/mcp/tools/test_formatters.py`

- [ ] **Step 1: Write failing tests for all 6 new formatters**

```python
# In tests/unit/mcp/tools/test_formatters.py, add:

from brain_v42.mcp.tools.formatters import (
    format_decision_detail,
    format_learning_detail,
    format_snippet_detail,
    format_adr_detail,
    format_projects_list,
    format_supersession_chain,
)


class TestFormatDecisionDetail:
    def test_includes_all_fields(self):
        d = _make_decision()
        result = format_decision_detail(d)
        assert "## Decision:" in result
        assert d.title in result
        assert d.description in result
        assert d.reasoning in result
        assert str(d.id)[:8] in result

    def test_shows_superseded_by(self):
        d = _make_decision(status="superseded", superseded_by=uuid4())
        result = format_decision_detail(d)
        assert "Superseded by" in result


class TestFormatLearningDetail:
    def test_includes_all_fields(self):
        lr = _make_learning()
        result = format_learning_detail(lr)
        assert "## Learning:" in result
        assert lr.topic in result
        assert lr.insight in result
        assert lr.confidence in result

    def test_shows_validated(self):
        lr = _make_learning(validated_at=datetime(2026, 3, 15, tzinfo=UTC))
        result = format_learning_detail(lr)
        assert "Validated" in result


class TestFormatSnippetDetail:
    def test_includes_full_code(self):
        s = _make_snippet()
        result = format_snippet_detail(s)
        assert "## Snippet:" in result
        assert s.code in result
        assert f"```{s.language}" in result

    def test_includes_gotchas(self):
        s = _make_snippet(gotchas="Watch out for X")
        result = format_snippet_detail(s)
        assert "Watch out for X" in result


class TestFormatADRDetail:
    def test_includes_all_fields(self):
        adr = _make_adr()
        result = format_adr_detail(adr)
        assert "## ADR #" in result
        assert adr.title in result
        assert adr.context in result
        assert adr.decision in result
        assert adr.consequences in result


class TestFormatProjectsList:
    def test_empty_list(self):
        result = format_projects_list([])
        assert "0 project" in result

    def test_lists_projects(self):
        ctx = _make_project_context()
        result = format_projects_list([ctx])
        assert ctx.project_key in result
        assert ctx.name in result


class TestFormatSupersessionChain:
    def test_single_decision(self):
        d = _make_decision()
        result = format_supersession_chain([d])
        assert d.title in result

    def test_chain_shows_arrow(self):
        d1 = _make_decision(title="Old decision")
        d2 = _make_decision(title="New decision")
        result = format_supersession_chain([d1, d2])
        assert "Old decision" in result
        assert "New decision" in result
```

Note: Use existing `_make_snippet`, `_make_adr`, `_make_project_context` helpers if they exist, or create them following the existing factory pattern.

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/unit/mcp/tools/test_formatters.py -k "Detail or ProjectsList or SupersessionChain" -v`
Expected: FAIL — `ImportError: cannot import name 'format_decision_detail'`

- [ ] **Step 3: Implement all 6 formatters**

```python
# In src/brain_v42/mcp/tools/formatters.py, add after format_project_context():

def format_decision_detail(d: Decision) -> str:
    """Format a single decision in full detail (for brain_get)."""
    lines = [f"## Decision: {d.title} (id:{short_id(d.id)})"]
    lines.append(f"**Status**: {d.status}")
    lines.append(f"**Created**: {short_date(d.created_at)}")
    if d.project_key:
        lines.append(f"**Project**: {d.project_key}")
    lines.append("")
    lines.append(f"**Description**: {d.description}")
    if d.reasoning:
        lines.append(f"**Reasoning**: {d.reasoning}")
    if d.alternatives:
        lines.append(f"**Alternatives**: {', '.join(d.alternatives)}")
    if d.consequences:
        lines.append(f"**Consequences**: {d.consequences}")
    if d.status == "superseded" and d.superseded_by:
        lines.append(f"**Superseded by**: id:{short_id(d.superseded_by)}")
    if d.tags:
        lines.append(f"**Tags**: {', '.join(d.tags)}")
    return "\n".join(lines)


def format_learning_detail(lr: Learning) -> str:
    """Format a single learning in full detail (for brain_get)."""
    badge = f" [{lr.confidence}]" if lr.confidence else ""
    lines = [f"## Learning: {lr.topic}{badge} (id:{short_id(lr.id)})"]
    lines.append(f"**Created**: {short_date(lr.created_at)}")
    if lr.project_key:
        lines.append(f"**Project**: {lr.project_key}")
    if lr.source:
        lines.append(f"**Source**: {lr.source} ({lr.source_type})")
    if lr.validated_at:
        lines.append(f"**Validated**: {short_date(lr.validated_at)}")
    lines.append("")
    lines.append(lr.insight)
    if lr.tags:
        lines.append(f"\n**Tags**: {', '.join(lr.tags)}")
    return "\n".join(lines)


def format_snippet_detail(s: Snippet) -> str:
    """Format a single snippet with full code (for brain_get)."""
    lines = [f"## Snippet: {s.title} [{s.language}] (id:{short_id(s.id)})"]
    lines.append(f"**Intention**: {s.intention}")
    if s.project_key:
        lines.append(f"**Project**: {s.project_key}")
    if s.dependencies:
        lines.append(f"**Dependencies**: {', '.join(s.dependencies)}")
    lines.append("")
    lines.append(f"```{s.language}")
    lines.append(s.code)
    lines.append("```")
    if s.usage_example:
        lines.append(f"\n**Example**: `{s.usage_example}`")
    if s.gotchas:
        lines.append(f"**Gotchas**: {s.gotchas}")
    if s.use_count and s.use_count > 0:
        last = f" (last: {short_date(s.last_used_at)})" if s.last_used_at else ""
        lines.append(f"**Used**: {s.use_count} times{last}")
    if s.tags:
        lines.append(f"**Tags**: {', '.join(s.tags)}")
    return "\n".join(lines)


def format_adr_detail(adr: ADR) -> str:
    """Format a single ADR in full detail (for brain_get)."""
    lines = [f"## ADR #{adr.number}: {adr.title} [{adr.status}] (id:{short_id(adr.id)})"]
    lines.append(f"**Project**: {adr.project_key}")
    lines.append(f"**Created**: {short_date(adr.created_at)}")
    if adr.decided_at:
        lines.append(f"**Decided**: {short_date(adr.decided_at)}")
    lines.append("")
    lines.append(f"**Context**: {adr.context}")
    lines.append(f"**Decision**: {adr.decision}")
    lines.append(f"**Consequences**: {adr.consequences}")
    if adr.alternatives_considered:
        lines.append("\n**Alternatives considered**:")
        for alt in adr.alternatives_considered:
            title = alt.title if hasattr(alt, 'title') else str(alt)
            lines.append(f"- {title}")
    if adr.tags:
        lines.append(f"\n**Tags**: {', '.join(adr.tags)}")
    return "\n".join(lines)


def format_projects_list(contexts: list[ProjectContext]) -> str:
    """Format project list summary (for brain_list_projects)."""
    n = len(contexts)
    s = "s" if n != 1 else ""
    header = f"## {n} project{s}"
    if n == 0:
        return header
    items: list[str] = []
    for ctx in contexts:
        focus = f" — {ctx.current_focus}" if ctx.current_focus else ""
        phase = f" [{ctx.current_phase}]" if ctx.current_phase else ""
        items.append(f"- **{ctx.project_key}** ({ctx.name}){phase}{focus}")
    return header + "\n\n" + "\n".join(items)


def format_supersession_chain(chain: list[Decision]) -> str:
    """Format a decision supersession chain as timeline."""
    if not chain:
        return "No supersession chain found."
    n = len(chain)
    header = f"## Supersession chain ({n} decision{'s' if n != 1 else ''})"
    items: list[str] = []
    for i, d in enumerate(chain):
        arrow = " -->" if i < n - 1 else " (current)"
        items.append(
            f"{i + 1}. **{d.title}** [{d.status}] "
            f"({short_date(d.created_at)}, id:{short_id(d.id)}){arrow}"
        )
    return header + "\n\n" + "\n".join(items)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/unit/mcp/tools/test_formatters.py -k "Detail or ProjectsList or SupersessionChain" -v`
Expected: ALL PASS

- [ ] **Step 5: Run full formatter tests to check no regressions**

Run: `pytest tests/unit/mcp/tools/test_formatters.py -v`
Expected: ALL PASS

- [ ] **Step 6: Commit**

```bash
git add src/brain_v42/mcp/tools/formatters.py tests/unit/mcp/tools/test_formatters.py
git commit -m "feat(formatters): add detail formatters for brain_get, list_projects, supersession chain"
```

---

## Batch 3: Generic CRUD Tools (depends on Batch 1 + 2)

### Task 4: crud_tools.py — brain_get

**Files:**
- Create: `src/brain_v42/mcp/tools/crud_tools.py`
- Create: `tests/unit/mcp/tools/test_crud_tools.py`

- [ ] **Step 1: Write failing test for brain_get**

```python
# tests/unit/mcp/tools/test_crud_tools.py

"""Unit tests for generic CRUD tools: brain_get, brain_delete, brain_update, brain_list."""
from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest


class MockMCP:
    """Collecting mock for FastMCP."""
    def __init__(self) -> None:
        self.registered: dict[str, Any] = {}

    def tool(self, **kwargs: Any) -> Any:
        def decorator(fn: Any) -> Any:
            self.registered[fn.__name__] = fn
            return fn
        return decorator


def _make_decision(**overrides):
    """Local factory for Decision model."""
    from datetime import UTC, datetime
    from brain_v42.models.decision import Decision
    defaults = {
        "id": uuid4(), "title": "Use PostgreSQL",
        "description": "Context: PG\n\nDecision: use it",
        "reasoning": "pgvector support", "project_key": "brain-v42",
        "created_at": datetime.now(UTC), "updated_at": datetime.now(UTC),
    }
    defaults.update(overrides)
    return Decision.model_validate(defaults)


def _make_services() -> dict[str, MagicMock]:
    """Create mock services dict for register_crud_tools."""
    services = {}
    for svc_name in ("decision_svc", "learning_svc", "snippet_svc", "runbook_svc", "adr_svc"):
        svc = MagicMock()
        svc.get_by_id = AsyncMock(return_value=None)
        svc.delete = AsyncMock(return_value=False)
        svc.update = AsyncMock(return_value=None)
        services[svc_name] = svc
    return services


@pytest.fixture
def services() -> dict[str, MagicMock]:
    return _make_services()


@pytest.fixture
def tools(services: dict[str, MagicMock]) -> dict[str, Any]:
    from brain_v42.mcp.tools.crud_tools import register_crud_tools
    mcp = MockMCP()
    register_crud_tools(mcp, **services)
    return mcp.registered


class TestBrainGet:
    @pytest.mark.asyncio
    async def test_invalid_entity_type_returns_error(self, tools):
        result = await tools["brain_get"]("invalid", str(uuid4()))
        assert "Unknown entity type" in result

    @pytest.mark.asyncio
    async def test_not_found_returns_error(self, tools, services):
        services["decision_svc"].get_by_id = AsyncMock(return_value=None)
        result = await tools["brain_get"]("decision", str(uuid4()))
        assert "\u2717" in result  # format_error marker

    @pytest.mark.asyncio
    async def test_found_returns_formatted(self, tools, services):
        from tests.unit.mcp.tools.test_crud_tools import _make_decision
        d = _make_decision()
        services["decision_svc"].get_by_id = AsyncMock(return_value=d)
        result = await tools["brain_get"]("decision", str(d.id))
        assert "## Decision:" in result
        assert d.title in result
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/unit/mcp/tools/test_crud_tools.py::TestBrainGet -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'brain_v42.mcp.tools.crud_tools'`

- [ ] **Step 3: Implement brain_get in crud_tools.py**

```python
# src/brain_v42/mcp/tools/crud_tools.py

"""Generic CRUD MCP tools: brain_get, brain_delete, brain_update, brain_list.

Follows the dispatch pattern from decay_tools.py (brain_refresh_entity, brain_merge_entities).
Dispatches by entity_type to the appropriate domain service.
"""
from __future__ import annotations

from typing import TYPE_CHECKING, Any
from uuid import UUID

import structlog

from brain_v42.mcp.tools.formatters import (
    format_adr_detail,
    format_confirmation,
    format_decision_detail,
    format_error,
    format_learning_detail,
    format_runbook,
    format_snippet_detail,
    short_id,
)

if TYPE_CHECKING:
    from brain_v42.services.adr_service import ADRService
    from brain_v42.services.decision_service import DecisionService
    from brain_v42.services.learning_service import LearningService
    from brain_v42.services.runbook_service import RunbookService
    from brain_v42.services.snippet_service import SnippetService

logger = structlog.get_logger(__name__)

_VALID_TYPES = ("decision", "learning", "snippet", "runbook", "adr")

# Maps entity_type -> formatter function for detail views
_DETAIL_FORMATTERS: dict[str, Any] = {
    "decision": format_decision_detail,
    "learning": format_learning_detail,
    "snippet": format_snippet_detail,
    "runbook": format_runbook,  # already exists as public
    "adr": format_adr_detail,
}


def register_crud_tools(
    mcp: Any,
    *,
    decision_svc: DecisionService,
    learning_svc: LearningService,
    snippet_svc: SnippetService,
    runbook_svc: RunbookService,
    adr_svc: ADRService,
) -> None:
    """Register generic CRUD tools on the FastMCP instance."""

    _services: dict[str, Any] = {
        "decision": decision_svc,
        "learning": learning_svc,
        "snippet": snippet_svc,
        "runbook": runbook_svc,
        "adr": adr_svc,
    }

    @mcp.tool(version="1.0")
    async def brain_get(entity_type: str, entity_id: str) -> str:
        """Get any entity by type and ID.

        Args:
            entity_type: One of: decision, learning, snippet, runbook, adr.
            entity_id: UUID of the entity.
        """
        if entity_type not in _VALID_TYPES:
            return format_error(
                f"Unknown entity type: {entity_type}. Use: {', '.join(_VALID_TYPES)}"
            )
        try:
            uid = UUID(entity_id)
        except ValueError:
            return format_error(f"Invalid UUID: {entity_id}")

        svc = _services[entity_type]
        entity = await svc.get_by_id(uid)
        if entity is None:
            return format_error(f"{entity_type} {short_id(entity_id)} not found")

        formatter = _DETAIL_FORMATTERS[entity_type]
        logger.info("mcp.brain_get", entity_type=entity_type, entity_id=entity_id)
        return formatter(entity)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/unit/mcp/tools/test_crud_tools.py::TestBrainGet -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/brain_v42/mcp/tools/crud_tools.py tests/unit/mcp/tools/test_crud_tools.py
git commit -m "feat(crud-tools): add brain_get generic tool"
```

---

### Task 5: crud_tools.py — brain_delete

**Files:**
- Modify: `src/brain_v42/mcp/tools/crud_tools.py`
- Modify: `tests/unit/mcp/tools/test_crud_tools.py`

- [ ] **Step 1: Write failing tests for brain_delete**

```python
class TestBrainDelete:
    @pytest.mark.asyncio
    async def test_invalid_entity_type_returns_error(self, tools):
        result = await tools["brain_delete"]("invalid", str(uuid4()))
        assert "Unknown entity type" in result

    @pytest.mark.asyncio
    async def test_not_found_returns_error(self, tools, services):
        services["decision_svc"].delete = AsyncMock(return_value=False)
        result = await tools["brain_delete"]("decision", str(uuid4()))
        assert "\u2717" in result

    @pytest.mark.asyncio
    async def test_success_returns_confirmation(self, tools, services):
        services["learning_svc"].delete = AsyncMock(return_value=True)
        uid = str(uuid4())
        result = await tools["brain_delete"]("learning", uid)
        assert "\u2713" in result
        assert "Deleted" in result
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/unit/mcp/tools/test_crud_tools.py::TestBrainDelete -v`
Expected: FAIL — `KeyError: 'brain_delete'`

- [ ] **Step 3: Implement brain_delete**

Add inside `register_crud_tools()`, after `brain_get`:

```python
    @mcp.tool(version="1.0")
    async def brain_delete(entity_type: str, entity_id: str) -> str:
        """Delete any entity by type and ID. Hard delete (not archive).

        Args:
            entity_type: One of: decision, learning, snippet, runbook, adr.
            entity_id: UUID of the entity to delete.
        """
        if entity_type not in _VALID_TYPES:
            return format_error(
                f"Unknown entity type: {entity_type}. Use: {', '.join(_VALID_TYPES)}"
            )
        try:
            uid = UUID(entity_id)
        except ValueError:
            return format_error(f"Invalid UUID: {entity_id}")

        svc = _services[entity_type]
        deleted = await svc.delete(uid)
        if not deleted:
            return format_error(f"{entity_type} {short_id(entity_id)} not found")

        logger.info("mcp.brain_delete", entity_type=entity_type, entity_id=entity_id)
        return format_confirmation("Deleted", f"{entity_type}/{short_id(entity_id)}")
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/unit/mcp/tools/test_crud_tools.py::TestBrainDelete -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/brain_v42/mcp/tools/crud_tools.py tests/unit/mcp/tools/test_crud_tools.py
git commit -m "feat(crud-tools): add brain_delete generic tool"
```

---

### Task 6: crud_tools.py — brain_update

**Files:**
- Modify: `src/brain_v42/mcp/tools/crud_tools.py`
- Modify: `tests/unit/mcp/tools/test_crud_tools.py`

- [ ] **Step 1: Write failing tests for brain_update**

```python
class TestBrainUpdate:
    @pytest.mark.asyncio
    async def test_invalid_entity_type_returns_error(self, tools):
        result = await tools["brain_update"]("invalid", str(uuid4()), {"title": "x"})
        assert "Unknown entity type" in result

    @pytest.mark.asyncio
    async def test_invalid_fields_returns_validation_error(self, tools, services):
        result = await tools["brain_update"]("decision", str(uuid4()), {"nonexistent_field": "x"})
        assert "\u2717" in result

    @pytest.mark.asyncio
    async def test_not_found_returns_error(self, tools, services):
        services["decision_svc"].update = AsyncMock(return_value=None)
        result = await tools["brain_update"]("decision", str(uuid4()), {"title": "New"})
        assert "\u2717" in result

    @pytest.mark.asyncio
    async def test_success_returns_confirmation(self, tools, services):
        from tests.unit.mcp.tools.test_crud_tools import _make_decision
        d = _make_decision(title="Updated")
        services["decision_svc"].update = AsyncMock(return_value=d)
        result = await tools["brain_update"]("decision", str(d.id), {"title": "Updated"})
        assert "\u2713" in result
        assert "Updated" in result
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/unit/mcp/tools/test_crud_tools.py::TestBrainUpdate -v`
Expected: FAIL — `KeyError: 'brain_update'`

- [ ] **Step 3: Implement brain_update**

Add inside `register_crud_tools()`, after `brain_delete`:

```python
    # Maps entity_type -> (UpdateModel, embed_fields)
    from brain_v42.models.adr import ADRUpdate  # noqa: PLC0415
    from brain_v42.models.decision import DecisionUpdate  # noqa: PLC0415
    from brain_v42.models.learning import LearningUpdate  # noqa: PLC0415
    from brain_v42.models.runbook import RunbookUpdate  # noqa: PLC0415
    from brain_v42.models.snippet import SnippetUpdate  # noqa: PLC0415

    _update_models: dict[str, type] = {
        "decision": DecisionUpdate,
        "learning": LearningUpdate,
        "snippet": SnippetUpdate,
        "runbook": RunbookUpdate,
        "adr": ADRUpdate,
    }

    @mcp.tool(version="1.0")
    async def brain_update(entity_type: str, entity_id: str, fields: dict) -> str:
        """Update any entity's fields by type and ID.

        Args:
            entity_type: One of: decision, learning, snippet, runbook, adr.
            entity_id: UUID of the entity to update.
            fields: Dict of field names to new values. Validated by Pydantic model.
        """
        if entity_type not in _VALID_TYPES:
            return format_error(
                f"Unknown entity type: {entity_type}. Use: {', '.join(_VALID_TYPES)}"
            )
        try:
            uid = UUID(entity_id)
        except ValueError:
            return format_error(f"Invalid UUID: {entity_id}")

        update_cls = _update_models[entity_type]
        try:
            update_data = update_cls(**fields)
        except Exception as e:
            return format_error(f"Validation error for {entity_type}: {e}")

        svc = _services[entity_type]
        result = await svc.update(uid, update_data)
        if result is None:
            return format_error(f"{entity_type} {short_id(entity_id)} not found")

        formatter = _DETAIL_FORMATTERS[entity_type]
        logger.info("mcp.brain_update", entity_type=entity_type, entity_id=entity_id)
        return format_confirmation(
            "Updated", f"{entity_type}/{short_id(entity_id)}"
        )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/unit/mcp/tools/test_crud_tools.py::TestBrainUpdate -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/brain_v42/mcp/tools/crud_tools.py tests/unit/mcp/tools/test_crud_tools.py
git commit -m "feat(crud-tools): add brain_update generic tool"
```

---

### Task 7: crud_tools.py — brain_list

**Files:**
- Modify: `src/brain_v42/mcp/tools/crud_tools.py`
- Modify: `tests/unit/mcp/tools/test_crud_tools.py`

- [ ] **Step 1: Write failing tests for brain_list**

```python
class TestBrainList:
    @pytest.mark.asyncio
    async def test_invalid_entity_type_returns_error(self, tools):
        result = await tools["brain_list"]("invalid")
        assert "Unknown entity type" in result

    @pytest.mark.asyncio
    async def test_decision_list_returns_formatted(self, tools, services):
        from tests.unit.mcp.tools.test_crud_tools import _make_decision
        d = _make_decision()
        services["decision_svc"].list_all = AsyncMock(return_value=[d])
        result = await tools["brain_list"]("decision")
        assert "1 decision" in result

    @pytest.mark.asyncio
    async def test_runbook_requires_project_key(self, tools):
        result = await tools["brain_list"]("runbook")
        assert "\u2717" in result
        assert "project_key" in result.lower()

    @pytest.mark.asyncio
    async def test_snippet_uses_list_snippets(self, tools, services):
        services["snippet_svc"].list_snippets = AsyncMock(return_value=[])
        result = await tools["brain_list"]("snippet", project_key="test")
        assert "0 snippet" in result
        services["snippet_svc"].list_snippets.assert_called_once()

    @pytest.mark.asyncio
    async def test_offset_is_passed(self, tools, services):
        services["decision_svc"].list_all = AsyncMock(return_value=[])
        await tools["brain_list"]("decision", offset=10)
        services["decision_svc"].list_all.assert_called_once()
        call_kwargs = services["decision_svc"].list_all.call_args[1]
        assert call_kwargs["offset"] == 10
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/unit/mcp/tools/test_crud_tools.py::TestBrainList -v`
Expected: FAIL — `KeyError: 'brain_list'`

- [ ] **Step 3: Implement brain_list**

Add imports at top of `crud_tools.py`:
```python
from brain_v42.mcp.tools.formatters import (
    # ... existing imports ...
    format_adrs,
    format_decisions,
    format_learnings,
    format_runbooks,
    format_snippets,
)
```

Add inside `register_crud_tools()`, after `brain_update`:

```python
    @mcp.tool(version="1.0")
    async def brain_list(
        entity_type: str,
        project_key: str | None = None,
        limit: int = 20,
        offset: int = 0,
        status: str | None = None,
        confidence: str | None = None,
        language: str | None = None,
        tags: list[str] | None = None,
    ) -> str:
        """List entities by type with optional filters and pagination.

        Args:
            entity_type: One of: decision, learning, snippet, runbook, adr.
            project_key: Filter by project (required for runbook).
            limit: Max results (default 20).
            offset: Pagination offset (default 0).
            status: Filter by status (decision, adr only).
            confidence: Filter by confidence (learning only).
            language: Filter by language (snippet only).
            tags: Filter by tags (decision, learning, adr).
        """
        if entity_type not in _VALID_TYPES:
            return format_error(
                f"Unknown entity type: {entity_type}. Use: {', '.join(_VALID_TYPES)}"
            )

        if entity_type == "decision":
            results = await decision_svc.list_all(
                project_key=project_key, status=status, tags=tags,
                limit=limit, offset=offset,
            )
            return format_decisions(results)
        elif entity_type == "learning":
            results = await learning_svc.list_all(
                project_key=project_key, confidence=confidence, tags=tags,
                limit=limit, offset=offset,
            )
            return format_learnings(results)
        elif entity_type == "snippet":
            results = await snippet_svc.list_snippets(
                project_key=project_key, language=language,
                limit=limit, offset=offset,
            )
            return format_snippets(results)
        elif entity_type == "runbook":
            if not project_key:
                return format_error("project_key is required for runbook listing")
            results = await runbook_svc.list_by_project(
                project_key=project_key, limit=limit, offset=offset,
            )
            return format_runbooks(results, project_key=project_key)
        else:  # adr
            results = await adr_svc.list_all(
                project_key=project_key, status=status,
                limit=limit, offset=offset,
            )
            return format_adrs(results, project_key=project_key)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/unit/mcp/tools/test_crud_tools.py::TestBrainList -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/brain_v42/mcp/tools/crud_tools.py tests/unit/mcp/tools/test_crud_tools.py
git commit -m "feat(crud-tools): add brain_list generic tool"
```

---

### Task 8: Register crud_tools in server.py

**Files:**
- Modify: `src/brain_v42/mcp/server.py`
- Modify: `src/brain_v42/mcp/tools/__init__.py`

- [ ] **Step 1: Add register_crud_tools to __init__.py**

```python
# Add import and export:
from brain_v42.mcp.tools.crud_tools import register_crud_tools

__all__ = [
    # ... existing ...
    "register_crud_tools",
]
```

- [ ] **Step 2: Wire up in server.py**

Add after the plan tools registration block (after line 287):

```python
    # CRUD tools (brain_get, brain_delete, brain_update, brain_list)
    from brain_v42.mcp.tools.crud_tools import register_crud_tools  # noqa: PLC0415

    register_crud_tools(
        mcp,
        decision_svc=services["decision_svc"],
        learning_svc=services["learning_svc"],
        snippet_svc=services["snippet_svc"],
        runbook_svc=services["runbook_svc"],
        adr_svc=services["adr_svc"],
    )
```

- [ ] **Step 3: Run all tests to verify no regressions**

Run: `pytest tests/unit -x -q`
Expected: ALL PASS

- [ ] **Step 4: Commit**

```bash
git add src/brain_v42/mcp/server.py src/brain_v42/mcp/tools/__init__.py
git commit -m "feat(server): wire register_crud_tools into MCP server"
```

---

## Batch 4: Specific Tools (parallel, depends on Batch 1 + 2)

### Task 9: brain_deprecate_adr

**Files:**
- Modify: `src/brain_v42/mcp/tools/brain_tools.py`
- Modify: `tests/unit/mcp/tools/test_brain_adr_tools.py`

- [ ] **Step 1: Write failing test**

NOTE: `test_brain_adr_tools.py` uses real FastMCP pattern with `_make_mcp_with_adr_tools()` + `_get_tool_fn()`. Follow that pattern:

```python
# In tests/unit/mcp/tools/test_brain_adr_tools.py, add:

class TestBrainDeprecateAdr:
    @pytest.mark.asyncio
    async def test_deprecate_success(self):
        mcp, mock_adr_svc = _make_mcp_with_adr_tools()
        adr = _make_adr(status="deprecated")
        mock_adr_svc.deprecate = AsyncMock(return_value=adr)
        fn = await _get_tool_fn(mcp, "brain_deprecate_adr")
        result = await fn(adr_id=str(adr.id), reason="Replaced")
        assert "\u2713" in result
        assert "deprecated" in result.lower()

    @pytest.mark.asyncio
    async def test_deprecate_not_found(self):
        mcp, mock_adr_svc = _make_mcp_with_adr_tools()
        mock_adr_svc.deprecate = AsyncMock(return_value=None)
        fn = await _get_tool_fn(mcp, "brain_deprecate_adr")
        result = await fn(adr_id=str(uuid4()))
        assert "\u2717" in result
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/unit/mcp/tools/test_brain_adr_tools.py::TestBrainDeprecateAdr -v`
Expected: FAIL — tool `brain_deprecate_adr` not registered (adr_svc.deprecate doesn't exist yet or tool not defined)

- [ ] **Step 3: Implement in brain_tools.py**

Add after `brain_list_adrs` in the ADR tools section:

```python
    @mcp.tool(version="1.0")
    async def brain_deprecate_adr(adr_id: str, reason: str | None = None) -> str:
        """Deprecate an ADR, setting status to 'deprecated'.

        Args:
            adr_id: UUID of the ADR to deprecate.
            reason: Optional reason for deprecation (appended to consequences).
        """
        adr = await adr_svc.deprecate(UUID(adr_id), reason=reason)
        if adr is None:
            return format_error(f"ADR '{short_id(adr_id)}' not found")
        logger.info("mcp.brain_deprecate_adr", adr_id=adr_id, reason=reason)
        return format_confirmation(
            f"ADR #{adr.number} deprecated",
            adr.title,
            id=str(adr.id),
        )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/unit/mcp/tools/test_brain_adr_tools.py::TestBrainDeprecateAdr -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/brain_v42/mcp/tools/brain_tools.py tests/unit/mcp/tools/test_brain_adr_tools.py
git commit -m "feat(tools): add brain_deprecate_adr MCP tool"
```

---

### Task 10: brain_list_projects

**Files:**
- Modify: `src/brain_v42/mcp/tools/project_context_tools.py`
- Modify: `tests/unit/mcp/tools/test_project_context_tools.py`

- [ ] **Step 1: Write failing test**

```python
class TestBrainListProjects:
    @pytest.mark.asyncio
    async def test_returns_formatted_list(self, tools, mock_svc):
        ctx = make_project_context()
        mock_svc.list_all = AsyncMock(return_value=[ctx])
        result = await tools["brain_list_projects"]()
        assert "1 project" in result
        assert ctx.project_key in result

    @pytest.mark.asyncio
    async def test_empty_list(self, tools, mock_svc):
        mock_svc.list_all = AsyncMock(return_value=[])
        result = await tools["brain_list_projects"]()
        assert "0 project" in result
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/unit/mcp/tools/test_project_context_tools.py::TestBrainListProjects -v`
Expected: FAIL

- [ ] **Step 3: Implement in project_context_tools.py**

Add import at top:
```python
from brain_v42.mcp.tools.formatters import (
    # ... existing ...
    format_projects_list,
)
```

Add inside `register_project_context_tools()`:

```python
    @mcp.tool(version="1.0")
    async def brain_list_projects() -> str:
        """List all known projects with their current focus and phase."""
        contexts = await project_context_svc.list_all()
        return format_projects_list(contexts)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/unit/mcp/tools/test_project_context_tools.py::TestBrainListProjects -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/brain_v42/mcp/tools/project_context_tools.py tests/unit/mcp/tools/test_project_context_tools.py
git commit -m "feat(tools): add brain_list_projects MCP tool"
```

---

### Task 11: brain_search_runbooks

**Files:**
- Modify: `src/brain_v42/mcp/tools/runbook_tools.py`
- Modify: `tests/unit/mcp/tools/test_runbook_tools.py`

- [ ] **Step 1: Write failing test**

```python
class TestBrainSearchRunbooks:
    @pytest.mark.asyncio
    async def test_returns_formatted_results(self, tools, mock_svc):
        rb = make_runbook()
        mock_svc.semantic_search = AsyncMock(return_value=[(rb, 0.85)])
        result = await tools["brain_search_runbooks"]("deploy procedure")
        assert "1 runbook" in result
        assert rb.title in result

    @pytest.mark.asyncio
    async def test_min_score_filters(self, tools, mock_svc):
        rb = make_runbook()
        mock_svc.semantic_search = AsyncMock(return_value=[(rb, 0.1)])
        result = await tools["brain_search_runbooks"]("deploy", min_score=0.5)
        assert "0 runbook" in result

    @pytest.mark.asyncio
    async def test_empty_results(self, tools, mock_svc):
        mock_svc.semantic_search = AsyncMock(return_value=[])
        result = await tools["brain_search_runbooks"]("nonexistent")
        assert "0 runbook" in result
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/unit/mcp/tools/test_runbook_tools.py::TestBrainSearchRunbooks -v`
Expected: FAIL

- [ ] **Step 3: Implement in runbook_tools.py**

Add `register_runbook_tools` signature to accept optional `hybrid_searcher` and `metrics_collector`:

```python
def register_runbook_tools(
    mcp: Any,
    runbook_svc: RunbookService,
    hybrid_searcher: Any | None = None,
    metrics_collector: Any | None = None,
) -> None:
```

Add the tool:

```python
    @mcp.tool(version="1.0")
    async def brain_search_runbooks(
        query: str,
        project_key: str | None = None,
        limit: int = 10,
        min_score: float = 0.2,
    ) -> str:
        """Search runbooks by semantic similarity.

        Args:
            query: Natural language search query.
            project_key: Optional project scope filter.
            limit: Maximum results (default 10).
            min_score: Minimum similarity threshold (default 0.2).
        """
        if hybrid_searcher:
            results = await hybrid_searcher.search(
                query=query,
                fts_search_fn=runbook_svc.search,
                vector_search_fn=runbook_svc.semantic_search,
                text_extractor=lambda rb: f"{rb.title} {rb.description} {rb.trigger}",
                limit=limit,
                project_key=project_key,
            )
        else:
            results = await runbook_svc.semantic_search(
                query=query, project_key=project_key, limit=limit,
            )
        filtered = [(rb, score) for rb, score in results if score >= min_score]
        runbooks_list = [rb for rb, _ in filtered]
        return format_runbooks(runbooks_list)
```

Also update `server.py` to pass `hybrid_searcher` to `register_runbook_tools`:
```python
    register_runbook_tools(mcp, runbook_svc, hybrid_searcher=hybrid_searcher)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/unit/mcp/tools/test_runbook_tools.py::TestBrainSearchRunbooks -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/brain_v42/mcp/tools/runbook_tools.py tests/unit/mcp/tools/test_runbook_tools.py src/brain_v42/mcp/server.py
git commit -m "feat(tools): add brain_search_runbooks MCP tool"
```

---

### Task 12: brain_get_supersession_chain

**Files:**
- Modify: `src/brain_v42/mcp/tools/brain_tools.py`
- Modify: `tests/unit/mcp/tools/test_brain_decision_tools.py`

- [ ] **Step 1: Write failing test**

NOTE: `test_brain_decision_tools.py` uses `_make_mcp_and_svc()` which returns `(registered_dict, mock_svc)`:

```python
# In tests/unit/mcp/tools/test_brain_decision_tools.py, add:

class TestBrainGetSupersessionChain:
    @pytest.mark.asyncio
    async def test_returns_chain(self):
        tools, svc = _make_mcp_and_svc()
        d1 = _make_decision(title="Old")
        d2 = _make_decision(title="New")
        svc.get_supersession_chain = AsyncMock(return_value=[d1, d2])
        result = await tools["brain_get_supersession_chain"](str(d1.id))
        assert "Old" in result
        assert "New" in result
        assert "2 decision" in result

    @pytest.mark.asyncio
    async def test_empty_chain(self):
        tools, svc = _make_mcp_and_svc()
        svc.get_supersession_chain = AsyncMock(return_value=[])
        result = await tools["brain_get_supersession_chain"](str(uuid.uuid4()))
        assert "No supersession chain" in result
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/unit/mcp/tools/test_brain_decision_tools.py::TestBrainGetSupersessionChain -v`
Expected: FAIL — `KeyError: 'brain_get_supersession_chain'`

- [ ] **Step 3: Implement in brain_tools.py**

Add import:
```python
from brain_v42.mcp.tools.formatters import (
    # ... existing ...
    format_supersession_chain,
)
```

Add after `brain_supersede_decision`:

```python
    @mcp.tool(version="1.0")
    async def brain_get_supersession_chain(decision_id: str) -> str:
        """Get the full supersession chain for a decision.

        Returns the chain from oldest to newest: old -> new -> newer.

        Args:
            decision_id: UUID of any decision in the chain.
        """
        chain = await decision_svc.get_supersession_chain(UUID(decision_id))
        logger.info("mcp.brain_get_supersession_chain", decision_id=decision_id, length=len(chain))
        return format_supersession_chain(chain)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/unit/mcp/tools/test_brain_decision_tools.py::TestBrainGetSupersessionChain -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/brain_v42/mcp/tools/brain_tools.py tests/unit/mcp/tools/test_brain_decision_tools.py
git commit -m "feat(tools): add brain_get_supersession_chain MCP tool"
```

---

### Task 13: Add offset to brain_search_decisions and brain_list_adrs

**Files:**
- Modify: `src/brain_v42/mcp/tools/brain_tools.py`
- Modify: `tests/unit/mcp/tools/test_brain_decision_tools.py`
- Modify: `tests/unit/mcp/tools/test_brain_adr_tools.py`

- [ ] **Step 1: Write failing tests**

```python
# In test_brain_decision_tools.py:
class TestBrainSearchDecisionsOffset:
    @pytest.mark.asyncio
    async def test_offset_passed_to_service(self, tools, mock_decision_svc):
        mock_decision_svc.list_all = AsyncMock(return_value=[])
        await tools["brain_search_decisions"](offset=5)
        call_kwargs = mock_decision_svc.list_all.call_args[1]
        assert call_kwargs["offset"] == 5

# In test_brain_adr_tools.py:
class TestBrainListAdrsOffset:
    @pytest.mark.asyncio
    async def test_offset_passed_to_service(self, tools, mock_adr_svc):
        mock_adr_svc.list_all = AsyncMock(return_value=[])
        await tools["brain_list_adrs"](offset=10)
        call_kwargs = mock_adr_svc.list_all.call_args[1]
        assert call_kwargs["offset"] == 10
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/unit/mcp/tools/test_brain_decision_tools.py::TestBrainSearchDecisionsOffset tests/unit/mcp/tools/test_brain_adr_tools.py::TestBrainListAdrsOffset -v`
Expected: FAIL — `unexpected keyword argument 'offset'`

- [ ] **Step 3: Add offset parameter to both tools AND fix existing test assertions**

In `brain_tools.py`, modify `brain_search_decisions`:
```python
    async def brain_search_decisions(
        query: str | None = None,
        project_key: str | None = None,
        status: str | None = None,
        tags: list[str] | None = None,
        limit: int = 10,
        offset: int = 0,  # NEW
    ) -> str:
```

Pass `offset=offset` to `decision_svc.search()` and `decision_svc.list_all()` calls.

Modify `brain_list_adrs`:
```python
    async def brain_list_adrs(
        project_key: str | None = None,
        status: str | None = None,
        limit: int = 20,
        offset: int = 0,  # NEW
    ) -> str:
```

Pass `offset=offset` to `adr_svc.list_all()`.

**IMPORTANT:** Also update existing test assertions that assert exact call signatures without `offset`:

In `test_brain_decision_tools.py`:
- `assert_awaited_once_with(..., limit=10)` → add `, offset=0`
- `assert_awaited_once_with(..., limit=5)` → add `, offset=0`

In `test_brain_adr_tools.py`:
- All `mock_svc.list_all.assert_called_once_with(...)` → add `offset=0` kwarg

Without this fix, ~4-6 existing tests will fail because the call now includes `offset=0`.

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/unit/mcp/tools/test_brain_decision_tools.py tests/unit/mcp/tools/test_brain_adr_tools.py -v`
Expected: ALL PASS (including existing tests)

- [ ] **Step 5: Commit**

```bash
git add src/brain_v42/mcp/tools/brain_tools.py tests/unit/mcp/tools/test_brain_decision_tools.py tests/unit/mcp/tools/test_brain_adr_tools.py
git commit -m "feat(tools): add offset param to brain_search_decisions and brain_list_adrs"
```

---

## Batch 5: Cleanup and Verification (depends on Batch 3 + 4)

### Task 14: Update docstrings and final verification

**Files:**
- Modify: `src/brain_v42/mcp/server.py`
- Modify: `src/brain_v42/mcp/tools/brain_tools.py`

- [ ] **Step 1: Update stale docstrings**

In `server.py`, update the module docstring:
```python
"""FastMCP stdio server for brain_v42.
...
6. register_tools(mcp, ...) — registers all 35 brain_* tools on the FastMCP instance
"""
```

In `brain_tools.py`, update the module docstring:
```python
"""brain_v42 MCP tools — 35 brain_* tool registrations.
...
```

- [ ] **Step 2: Run full test suite**

Run: `pytest tests/unit -v --tb=short`
Expected: ALL PASS

- [ ] **Step 3: Run linter**

Run: `ruff check src/brain_v42/mcp/ tests/unit/mcp/ && ruff format --check src/brain_v42/mcp/ tests/unit/mcp/`
Expected: CLEAN

- [ ] **Step 4: Commit**

```bash
git add src/brain_v42/mcp/server.py src/brain_v42/mcp/tools/brain_tools.py
git commit -m "docs: update tool count docstrings from 21 to 35"
```

- [ ] **Step 5: Final verification — count registered tools**

Run: `python -c "from brain_v42.mcp.server import build_services, mcp; print(f'Tools: {len(mcp._tools) if hasattr(mcp, \"_tools\") else \"check manually\"}')"` or verify via grep:

Run: `grep -c '@mcp.tool' src/brain_v42/mcp/tools/*.py`
Expected: total should be 35
