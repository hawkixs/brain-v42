"""Unit tests for CRUD MCP tools: brain_get, brain_delete, brain_update, brain_list."""

from __future__ import annotations

import inspect
from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest

from brain_v42.models.adr import ADR
from brain_v42.models.decision import Decision
from brain_v42.models.indexed_plan import IndexedPlan
from brain_v42.models.learning import Learning
from brain_v42.models.runbook import Runbook, RunbookStep
from brain_v42.models.snippet import Snippet
from tests.unit.mcp._tool_error_adapter import capture_tool_errors

# ---------------------------------------------------------------------------
# MockMCP (same as decay_tools tests)
# ---------------------------------------------------------------------------


class MockMCP:
    """Collecting mock for FastMCP."""

    def __init__(self) -> None:
        self.registered: dict[str, Any] = {}

    def tool(self, **kwargs: Any) -> Any:
        def decorator(fn: Any) -> Any:
            self.registered[fn.__name__] = capture_tool_errors(fn)
            return fn

        return decorator


# ---------------------------------------------------------------------------
# Model factories (local, no cross-test imports)
# ---------------------------------------------------------------------------


def _make_decision(**overrides: Any) -> Decision:
    defaults: dict[str, Any] = {
        "id": uuid4(),
        "title": "Test Decision",
        "description": "desc",
        "reasoning": "reason",
        "project_key": "test",
        "status": "active",
        "created_at": datetime.now(UTC),
        "updated_at": datetime.now(UTC),
    }
    defaults.update(overrides)
    return Decision.model_validate(defaults)


def _make_learning(**overrides: Any) -> Learning:
    defaults: dict[str, Any] = {
        "id": uuid4(),
        "topic": "Test Topic",
        "insight": "Test insight text",
        "source": "test",
        "source_type": "experience",
        "confidence": "medium",
        "project_key": "test",
        "created_at": datetime.now(UTC),
        "updated_at": datetime.now(UTC),
    }
    defaults.update(overrides)
    return Learning.model_validate(defaults)


def _make_snippet(**overrides: Any) -> Snippet:
    defaults: dict[str, Any] = {
        "id": uuid4(),
        "title": "Test Snippet",
        "intention": "Test intention",
        "code": "print('hello')",
        "language": "python",
        "project_key": "test",
        "created_at": datetime.now(UTC),
        "updated_at": datetime.now(UTC),
    }
    defaults.update(overrides)
    return Snippet.model_validate(defaults)


def _make_runbook(**overrides: Any) -> Runbook:
    defaults: dict[str, Any] = {
        "id": uuid4(),
        "title": "Test Runbook",
        "description": "desc",
        "project_key": "test",
        "trigger": "manual",
        "steps": [RunbookStep(order=1, title="Step 1", command="echo hi")],
        "created_at": datetime.now(UTC),
        "updated_at": datetime.now(UTC),
    }
    defaults.update(overrides)
    return Runbook.model_validate(defaults)


def _make_adr(**overrides: Any) -> ADR:
    defaults: dict[str, Any] = {
        "id": uuid4(),
        "title": "Test ADR",
        "context": "context",
        "decision": "decision",
        "consequences": "consequences",
        "project_key": "test",
        "number": 1,
        "status": "proposed",
        "created_at": datetime.now(UTC),
        "updated_at": datetime.now(UTC),
    }
    defaults.update(overrides)
    return ADR.model_validate(defaults)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _make_services() -> dict[str, Any]:
    services: dict[str, Any] = {}
    for name in ("decision_svc", "learning_svc", "snippet_svc", "runbook_svc", "adr_svc"):
        svc = MagicMock()
        svc.get_by_id = AsyncMock(return_value=None)
        svc.delete = AsyncMock(return_value=False)
        svc.update = AsyncMock(return_value=None)
        services[name] = svc
    # List methods vary by service
    services["decision_svc"].list_all = AsyncMock(return_value=[])
    services["learning_svc"].list_all = AsyncMock(return_value=[])
    services["snippet_svc"].list_snippets = AsyncMock(return_value=[])
    services["runbook_svc"].list_by_project = AsyncMock(return_value=[])
    services["adr_svc"].list_all = AsyncMock(return_value=[])
    return services


@pytest.fixture
def services() -> dict[str, Any]:
    return _make_services()


@pytest.fixture
def tools(services: dict[str, Any]) -> dict[str, Any]:
    from unittest.mock import MagicMock

    from brain_v42.mcp.tools.crud_tools import register_crud_tools

    mcp = MockMCP()
    # session_factory is required for plan dispatch; use a mock for unit tests
    mock_session_factory = MagicMock()
    register_crud_tools(mcp, **services, session_factory=mock_session_factory)
    return mcp.registered


@pytest.fixture
def access_logged_tools(
    services: dict[str, Any],
) -> tuple[dict[str, Any], MagicMock, MagicMock]:
    """Register CRUD tools with the optional internal access logger."""
    from brain_v42.mcp.tools.crud_tools import register_crud_tools

    mcp = MockMCP()
    session = AsyncMock()
    context = AsyncMock()
    context.__aenter__ = AsyncMock(return_value=session)
    context.__aexit__ = AsyncMock(return_value=False)
    session_factory = MagicMock(return_value=context)
    access_logger = MagicMock()
    optional_logger = (
        {"access_logger": access_logger}
        if "access_logger" in inspect.signature(register_crud_tools).parameters
        else {}
    )
    register_crud_tools(
        mcp,
        **services,
        session_factory=session_factory,
        **optional_logger,
    )
    return mcp.registered, access_logger, session_factory


# ===========================================================================
# brain_get
# ===========================================================================


class TestBrainGet:
    @pytest.mark.asyncio
    async def test_successful_entity_get_logs_uuid_after_lookup(
        self,
        access_logged_tools: tuple[dict[str, Any], MagicMock, MagicMock],
        services: dict[str, Any],
    ) -> None:
        """Generic knowledge reads produce one canonical access signal."""
        tools, access_logger, _session_factory = access_logged_tools
        decision = _make_decision()
        services["decision_svc"].get_by_id.return_value = decision

        await tools["brain_get"]("decision", str(decision.id))

        access_logger.log_access.assert_called_once_with("decision", decision.id, "get_by_id")

    @pytest.mark.asyncio
    async def test_missing_or_invalid_entity_get_logs_nothing(
        self,
        access_logged_tools: tuple[dict[str, Any], MagicMock, MagicMock],
    ) -> None:
        """Rejected and missing reads are not counted as usage."""
        tools, access_logger, _session_factory = access_logged_tools

        await tools["brain_get"]("decision", str(uuid4()))
        await tools["brain_get"]("decision", "not-a-uuid")

        access_logger.log_access.assert_not_called()

    @pytest.mark.asyncio
    async def test_successful_plan_get_logs_parent_uuid(
        self,
        access_logged_tools: tuple[dict[str, Any], MagicMock, MagicMock],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Generic plan detail logs the indexed plan parent itself."""
        tools, access_logger, _session_factory = access_logged_tools
        plan_id = uuid4()
        now = datetime.now(UTC)
        plan = IndexedPlan(
            id=plan_id,
            file_path="docs/plans/cor1.md",
            title="COR1",
            plan_type="plan",
            project_key="brain-v42",
            content_hash="a" * 64,
            content="# COR1",
            status="active",
            chunk_count=0,
            word_count=1,
            freshness_status="fresh",
            indexed_at=now,
            created_at=now,
            updated_at=now,
        )
        repo = MagicMock()
        repo.get_with_chunks = AsyncMock(return_value=(plan, []))
        monkeypatch.setattr(
            "brain_v42.repositories.pg_indexed_plan_repo.PgIndexedPlanRepo",
            MagicMock(return_value=repo),
        )

        await tools["brain_get"]("plan", str(plan_id))

        access_logger.log_access.assert_called_once_with("plan", plan_id, "get_by_id")

    @pytest.mark.asyncio
    async def test_tool_registered(self, tools: dict[str, Any]) -> None:
        """brain_get is registered as an MCP tool."""
        assert "brain_get" in tools

    @pytest.mark.asyncio
    async def test_get_decision(self, tools: dict[str, Any], services: dict[str, Any]) -> None:
        """brain_get returns formatted decision detail."""
        decision = _make_decision()
        services["decision_svc"].get_by_id.return_value = decision

        result = await tools["brain_get"](entity_type="decision", entity_id=str(decision.id))

        assert isinstance(result, str)
        assert "## Decision:" in result
        assert decision.title in result
        services["decision_svc"].get_by_id.assert_called_once_with(decision.id)

    @pytest.mark.asyncio
    async def test_get_learning(self, tools: dict[str, Any], services: dict[str, Any]) -> None:
        """brain_get returns formatted learning detail."""
        learning = _make_learning()
        services["learning_svc"].get_by_id.return_value = learning

        result = await tools["brain_get"](entity_type="learning", entity_id=str(learning.id))

        assert isinstance(result, str)
        assert "## Learning:" in result
        assert learning.topic in result

    @pytest.mark.asyncio
    async def test_get_snippet(self, tools: dict[str, Any], services: dict[str, Any]) -> None:
        """brain_get returns formatted snippet detail."""
        snippet = _make_snippet()
        services["snippet_svc"].get_by_id.return_value = snippet

        result = await tools["brain_get"](entity_type="snippet", entity_id=str(snippet.id))

        assert isinstance(result, str)
        assert "## Snippet:" in result
        assert snippet.title in result

    @pytest.mark.asyncio
    async def test_get_runbook(self, tools: dict[str, Any], services: dict[str, Any]) -> None:
        """brain_get returns formatted runbook detail."""
        runbook = _make_runbook()
        services["runbook_svc"].get_by_id.return_value = runbook

        result = await tools["brain_get"](entity_type="runbook", entity_id=str(runbook.id))

        assert isinstance(result, str)
        assert "## Runbook:" in result
        assert runbook.title in result

    @pytest.mark.asyncio
    async def test_get_adr(self, tools: dict[str, Any], services: dict[str, Any]) -> None:
        """brain_get returns formatted ADR detail."""
        adr = _make_adr()
        services["adr_svc"].get_by_id.return_value = adr

        result = await tools["brain_get"](entity_type="adr", entity_id=str(adr.id))

        assert isinstance(result, str)
        assert "## ADR #" in result
        assert adr.title in result

    @pytest.mark.asyncio
    async def test_get_not_found(self, tools: dict[str, Any], services: dict[str, Any]) -> None:
        """brain_get returns error when entity not found."""
        services["decision_svc"].get_by_id.return_value = None

        result = await tools["brain_get"](entity_type="decision", entity_id=str(uuid4()))

        assert isinstance(result, str)
        assert "not found" in result

    @pytest.mark.asyncio
    async def test_get_unknown_type(self, tools: dict[str, Any]) -> None:
        """brain_get rejects unknown entity type."""
        result = await tools["brain_get"](entity_type="unknown", entity_id=str(uuid4()))

        assert isinstance(result, str)
        assert "Unknown entity type" in result

    @pytest.mark.asyncio
    async def test_get_invalid_uuid(self, tools: dict[str, Any]) -> None:
        """brain_get rejects invalid UUID."""
        result = await tools["brain_get"](entity_type="decision", entity_id="not-a-uuid")

        assert isinstance(result, str)
        assert "Invalid UUID" in result


# ===========================================================================
# brain_delete
# ===========================================================================


class TestBrainDelete:
    @pytest.mark.asyncio
    async def test_tool_registered(self, tools: dict[str, Any]) -> None:
        """brain_delete is registered as an MCP tool."""
        assert "brain_delete" in tools

    @pytest.mark.asyncio
    async def test_delete_success(self, tools: dict[str, Any], services: dict[str, Any]) -> None:
        """brain_delete returns confirmation on success."""
        services["decision_svc"].delete.return_value = True
        entity_id = str(uuid4())

        result = await tools["brain_delete"](entity_type="decision", entity_id=entity_id)

        assert isinstance(result, str)
        assert result.startswith("ok Deleted")
        assert "type:decision" in result

    @pytest.mark.asyncio
    async def test_delete_not_found(self, tools: dict[str, Any], services: dict[str, Any]) -> None:
        """brain_delete returns error when entity not found."""
        services["learning_svc"].delete.return_value = False

        result = await tools["brain_delete"](entity_type="learning", entity_id=str(uuid4()))

        assert isinstance(result, str)
        assert "not found" in result

    @pytest.mark.asyncio
    async def test_delete_unknown_type(self, tools: dict[str, Any]) -> None:
        """brain_delete rejects unknown entity type."""
        result = await tools["brain_delete"](entity_type="unknown", entity_id=str(uuid4()))

        assert isinstance(result, str)
        assert "Unknown entity type" in result

    @pytest.mark.asyncio
    async def test_delete_invalid_uuid(self, tools: dict[str, Any]) -> None:
        """brain_delete rejects invalid UUID."""
        result = await tools["brain_delete"](entity_type="decision", entity_id="not-a-uuid")

        assert isinstance(result, str)
        assert "Invalid UUID" in result

    @pytest.mark.asyncio
    async def test_delete_all_types(self, tools: dict[str, Any], services: dict[str, Any]) -> None:
        """brain_delete dispatches correctly for all entity types."""
        for entity_type, svc_key in [
            ("decision", "decision_svc"),
            ("learning", "learning_svc"),
            ("snippet", "snippet_svc"),
            ("runbook", "runbook_svc"),
            ("adr", "adr_svc"),
        ]:
            services[svc_key].delete.return_value = True
            result = await tools["brain_delete"](entity_type=entity_type, entity_id=str(uuid4()))
            assert result.startswith("ok Deleted"), f"Failed for {entity_type}"


# ===========================================================================
# brain_update
# ===========================================================================


class TestBrainUpdate:
    @pytest.mark.asyncio
    async def test_tool_registered(self, tools: dict[str, Any]) -> None:
        """brain_update is registered as an MCP tool."""
        assert "brain_update" in tools

    @pytest.mark.asyncio
    async def test_update_decision(self, tools: dict[str, Any], services: dict[str, Any]) -> None:
        """brain_update returns confirmation on success."""
        updated = _make_decision(title="Updated Title")
        services["decision_svc"].update.return_value = updated
        entity_id = str(uuid4())

        result = await tools["brain_update"](
            entity_type="decision",
            entity_id=entity_id,
            fields={"title": "Updated Title"},
        )

        assert isinstance(result, str)
        assert result.startswith("ok Updated")
        assert "type:decision" in result

    @pytest.mark.asyncio
    async def test_update_not_found(self, tools: dict[str, Any], services: dict[str, Any]) -> None:
        """brain_update returns error when entity not found (service returns None)."""
        services["learning_svc"].update.return_value = None

        result = await tools["brain_update"](
            entity_type="learning",
            entity_id=str(uuid4()),
            fields={"topic": "New Topic"},
        )

        assert isinstance(result, str)
        assert "not found" in result

    @pytest.mark.asyncio
    async def test_update_unknown_type(self, tools: dict[str, Any]) -> None:
        """brain_update rejects unknown entity type."""
        result = await tools["brain_update"](
            entity_type="unknown",
            entity_id=str(uuid4()),
            fields={"title": "x"},
        )

        assert isinstance(result, str)
        assert "Unknown entity type" in result

    @pytest.mark.asyncio
    async def test_update_invalid_uuid(self, tools: dict[str, Any]) -> None:
        """brain_update rejects invalid UUID."""
        result = await tools["brain_update"](
            entity_type="decision",
            entity_id="not-a-uuid",
            fields={"title": "x"},
        )

        assert isinstance(result, str)
        assert "Invalid UUID" in result

    @pytest.mark.asyncio
    async def test_update_invalid_fields(self, tools: dict[str, Any]) -> None:
        """brain_update returns error on invalid Pydantic fields."""
        result = await tools["brain_update"](
            entity_type="decision",
            entity_id=str(uuid4()),
            fields={"status": "INVALID_STATUS_VALUE"},
        )

        assert isinstance(result, str)
        assert "Invalid fields" in result

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("entity_type", "svc_key", "make", "unknown_field"),
        [
            ("decision", "decision_svc", _make_decision, "decision_made"),
            ("learning", "learning_svc", _make_learning, "lesson"),
            ("snippet", "snippet_svc", _make_snippet, "snippet_code"),
            ("runbook", "runbook_svc", _make_runbook, "procedure"),
            ("adr", "adr_svc", _make_adr, "rationale"),
        ],
    )
    async def test_update_rejects_unknown_field(
        self,
        tools: dict[str, Any],
        services: dict[str, Any],
        entity_type: str,
        svc_key: str,
        make: Any,
        unknown_field: str,
    ) -> None:
        """Unknown keys fail loudly instead of being silently dropped.

        Footgun (learning 0cd1109a): a decision body lives in the single
        ``description`` column (``Context: \u2026 Decision: \u2026`` merged), so
        passing ``decision_made`` matched no DTO field and was silently
        ignored \u2014 yet brain_update still returned ``ok Updated``. The
        ``*Update`` DTOs now forbid extra keys, so the caller gets an
        error and the update service is never reached.
        """
        services[svc_key].update.return_value = make()

        result = await tools["brain_update"](
            entity_type=entity_type,
            entity_id=str(uuid4()),
            fields={unknown_field: "x"},
        )

        assert isinstance(result, str)
        assert unknown_field in result, f"{entity_type}: error should name the bad key"
        services[svc_key].update.assert_not_called()

    @pytest.mark.asyncio
    async def test_update_unknown_field_error_lists_valid_fields(
        self, tools: dict[str, Any], services: dict[str, Any]
    ) -> None:
        """The error surfaces valid field names so the LLM can self-correct.

        A decision body is edited via ``description`` (Context + Decision
        merged); naming the valid fields turns the silent footgun
        (learning 0cd1109a) into a self-correcting error.
        """
        services["decision_svc"].update.return_value = _make_decision()

        result = await tools["brain_update"](
            entity_type="decision",
            entity_id=str(uuid4()),
            fields={"decision_made": "PostgreSQL over MySQL"},
        )

        assert "description" in result, f"valid field 'description' must be listed, got {result!r}"

    @pytest.mark.asyncio
    async def test_update_archive_learning_via_freshness_status(
        self,
        tools: dict[str, Any],
        services: dict[str, Any],
    ) -> None:
        """brain_update(fields={'freshness_status':'archived'}) archives a learning.

        Archive is the reversible alternative to delete — entity stays in
        DB but is filtered from default listings/searches. Consumed by the
        Dream REORG phase to clean up test pollution and operational
        snapshots (2026-04-22 blocker-3 resolution).
        """
        archived = _make_learning(freshness_status="archived")
        services["learning_svc"].update.return_value = archived

        result = await tools["brain_update"](
            entity_type="learning",
            entity_id=str(uuid4()),
            fields={"freshness_status": "archived"},
        )

        assert isinstance(result, str)
        assert result.startswith("ok Updated"), f"expected ok result, got: {result!r}"

        call_args = services["learning_svc"].update.call_args
        assert call_args is not None
        update_data = call_args.args[1] if len(call_args.args) > 1 else call_args.kwargs["data"]
        assert update_data.freshness_status == "archived", (
            f"freshness_status must propagate through LearningUpdate; got {update_data!r}"
        )

    @pytest.mark.asyncio
    async def test_update_archive_decision_via_freshness_status(
        self,
        tools: dict[str, Any],
        services: dict[str, Any],
    ) -> None:
        """Same archive path available for decision entities."""
        updated = _make_decision()
        services["decision_svc"].update.return_value = updated

        result = await tools["brain_update"](
            entity_type="decision",
            entity_id=str(uuid4()),
            fields={"freshness_status": "archived"},
        )

        assert isinstance(result, str)
        assert result.startswith("ok Updated"), f"expected ok result, got: {result!r}"

        call_args = services["decision_svc"].update.call_args
        assert call_args is not None
        update_data = call_args.args[1] if len(call_args.args) > 1 else call_args.kwargs["data"]
        assert update_data.freshness_status == "archived"

    @pytest.mark.asyncio
    async def test_update_snippet(self, tools: dict[str, Any], services: dict[str, Any]) -> None:
        """brain_update works for snippet type."""
        updated = _make_snippet(code="new code")
        services["snippet_svc"].update.return_value = updated

        result = await tools["brain_update"](
            entity_type="snippet",
            entity_id=str(uuid4()),
            fields={"code": "new code"},
        )

        assert result.startswith("ok Updated")

    @pytest.mark.asyncio
    async def test_update_runbook(self, tools: dict[str, Any], services: dict[str, Any]) -> None:
        """brain_update works for runbook type."""
        updated = _make_runbook(trigger="new trigger")
        services["runbook_svc"].update.return_value = updated

        result = await tools["brain_update"](
            entity_type="runbook",
            entity_id=str(uuid4()),
            fields={"trigger": "new trigger"},
        )

        assert result.startswith("ok Updated")

    @pytest.mark.asyncio
    async def test_update_adr(self, tools: dict[str, Any], services: dict[str, Any]) -> None:
        """brain_update works for ADR type."""
        updated = _make_adr(context="new context")
        services["adr_svc"].update.return_value = updated

        result = await tools["brain_update"](
            entity_type="adr",
            entity_id=str(uuid4()),
            fields={"context": "new context"},
        )

        assert result.startswith("ok Updated")


# ===========================================================================
# brain_list
# ===========================================================================


class TestBrainList:
    @pytest.mark.asyncio
    async def test_tool_registered(self, tools: dict[str, Any]) -> None:
        """brain_list is registered as an MCP tool."""
        assert "brain_list" in tools

    @pytest.mark.asyncio
    async def test_list_decisions(self, tools: dict[str, Any], services: dict[str, Any]) -> None:
        """brain_list returns formatted decisions."""
        decisions = [_make_decision(), _make_decision(title="Second")]
        services["decision_svc"].list_all.return_value = decisions

        result = await tools["brain_list"](entity_type="decision")

        assert isinstance(result, str)
        assert "2 decisions found" in result
        services["decision_svc"].list_all.assert_called_once_with(
            project_key=None, status=None, tags=None, limit=20, offset=0, include_archived=False
        )

    @pytest.mark.asyncio
    async def test_list_decisions_with_filters(
        self, tools: dict[str, Any], services: dict[str, Any]
    ) -> None:
        """brain_list passes filters for decisions."""
        services["decision_svc"].list_all.return_value = []

        await tools["brain_list"](
            entity_type="decision",
            project_key="myproject",
            status="active",
            tags=["tag1"],
            limit=10,
            offset=5,
        )

        services["decision_svc"].list_all.assert_called_once_with(
            project_key="myproject",
            status="active",
            tags=["tag1"],
            limit=10,
            offset=5,
            include_archived=False,
        )

    @pytest.mark.asyncio
    async def test_list_learnings(self, tools: dict[str, Any], services: dict[str, Any]) -> None:
        """brain_list returns formatted learnings."""
        learnings = [_make_learning()]
        services["learning_svc"].list_all.return_value = learnings

        result = await tools["brain_list"](entity_type="learning", confidence="high")

        assert isinstance(result, str)
        assert "1 learning found" in result
        services["learning_svc"].list_all.assert_called_once_with(
            project_key=None,
            confidence="high",
            tags=None,
            limit=20,
            offset=0,
            include_archived=False,
        )

    @pytest.mark.asyncio
    async def test_list_snippets(self, tools: dict[str, Any], services: dict[str, Any]) -> None:
        """brain_list calls list_snippets (not list_all) for snippets."""
        snippets = [_make_snippet()]
        services["snippet_svc"].list_snippets.return_value = snippets

        result = await tools["brain_list"](entity_type="snippet", language="python")

        assert isinstance(result, str)
        assert "1 snippet" in result
        services["snippet_svc"].list_snippets.assert_called_once_with(
            project_key=None, language="python", limit=20, offset=0, include_archived=False
        )

    @pytest.mark.asyncio
    async def test_list_runbooks_requires_project_key(
        self, tools: dict[str, Any], services: dict[str, Any]
    ) -> None:
        """brain_list returns error for runbooks without project_key."""
        result = await tools["brain_list"](entity_type="runbook")

        assert isinstance(result, str)
        assert "project_key" in result.lower()

    @pytest.mark.asyncio
    async def test_list_runbooks_with_project_key(
        self, tools: dict[str, Any], services: dict[str, Any]
    ) -> None:
        """brain_list returns formatted runbooks when project_key is given."""
        runbooks = [_make_runbook()]
        services["runbook_svc"].list_by_project.return_value = runbooks

        result = await tools["brain_list"](entity_type="runbook", project_key="test")

        assert isinstance(result, str)
        assert "1 runbook" in result
        services["runbook_svc"].list_by_project.assert_called_once_with(
            project_key="test", limit=20, offset=0
        )

    @pytest.mark.asyncio
    async def test_list_adrs(self, tools: dict[str, Any], services: dict[str, Any]) -> None:
        """brain_list returns formatted ADRs."""
        adrs = [_make_adr()]
        services["adr_svc"].list_all.return_value = adrs

        result = await tools["brain_list"](entity_type="adr", status="proposed")

        assert isinstance(result, str)
        assert "1 ADR" in result
        services["adr_svc"].list_all.assert_called_once_with(
            project_key=None, status="proposed", limit=20, offset=0, include_archived=False
        )

    @pytest.mark.asyncio
    async def test_list_unknown_type(self, tools: dict[str, Any]) -> None:
        """brain_list rejects unknown entity type."""
        result = await tools["brain_list"](entity_type="unknown")

        assert isinstance(result, str)
        assert "Unknown entity type" in result

    @pytest.mark.asyncio
    async def test_list_learnings_summary_only_drops_body(
        self, tools: dict[str, Any], services: dict[str, Any]
    ) -> None:
        """summary_only=True omits insight body \u2014 REORG Part 1 unblock."""
        lr = _make_learning(insight="UNIQUE_BODY_TOKEN_X")
        services["learning_svc"].list_all.return_value = [lr]

        result = await tools["brain_list"](entity_type="learning", summary_only=True)

        assert isinstance(result, str)
        assert "UNIQUE_BODY_TOKEN_X" not in result
        assert "access:" in result

    @pytest.mark.asyncio
    async def test_list_decisions_summary_only_drops_description(
        self, tools: dict[str, Any], services: dict[str, Any]
    ) -> None:
        """summary_only=True omits decision description/reasoning."""
        d = _make_decision(description="UNIQUE_DESC_TOKEN_X")
        services["decision_svc"].list_all.return_value = [d]

        result = await tools["brain_list"](entity_type="decision", summary_only=True)

        assert isinstance(result, str)
        assert "UNIQUE_DESC_TOKEN_X" not in result
        assert "access:" in result

    @pytest.mark.asyncio
    async def test_list_empty_result(self, tools: dict[str, Any], services: dict[str, Any]) -> None:
        """brain_list returns header even with no results."""
        services["decision_svc"].list_all.return_value = []

        result = await tools["brain_list"](entity_type="decision")

        assert isinstance(result, str)
        assert "0 decisions found" in result

    @pytest.mark.asyncio
    async def test_list_learnings_excludes_archived_by_default(
        self, tools: dict[str, Any], services: dict[str, Any]
    ) -> None:
        """brain_list excludes archived/merged learnings by default."""
        services["learning_svc"].list_all.return_value = []
        await tools["brain_list"](entity_type="learning")
        call_kwargs = services["learning_svc"].list_all.call_args
        assert (
            call_kwargs.kwargs.get("include_archived") is False
            or call_kwargs[1].get("include_archived") is False
        )

    @pytest.mark.asyncio
    async def test_list_learnings_includes_archived_when_requested(
        self, tools: dict[str, Any], services: dict[str, Any]
    ) -> None:
        """brain_list includes archived learnings when include_archived=True."""
        services["learning_svc"].list_all.return_value = []
        await tools["brain_list"](entity_type="learning", include_archived=True)
        call_kwargs = services["learning_svc"].list_all.call_args
        assert (
            call_kwargs.kwargs.get("include_archived") is True
            or call_kwargs[1].get("include_archived") is True
        )

    @pytest.mark.asyncio
    async def test_list_decisions_excludes_archived_by_default(
        self, tools: dict[str, Any], services: dict[str, Any]
    ) -> None:
        """brain_list excludes archived decisions by default."""
        services["decision_svc"].list_all.return_value = []
        await tools["brain_list"](entity_type="decision")
        call_kwargs = services["decision_svc"].list_all.call_args
        assert (
            call_kwargs.kwargs.get("include_archived") is False
            or call_kwargs[1].get("include_archived") is False
        )

    @pytest.mark.asyncio
    async def test_list_snippets_excludes_archived_by_default(
        self, tools: dict[str, Any], services: dict[str, Any]
    ) -> None:
        """brain_list excludes archived snippets by default."""
        services["snippet_svc"].list_snippets.return_value = []
        await tools["brain_list"](entity_type="snippet")
        call_kwargs = services["snippet_svc"].list_snippets.call_args
        assert (
            call_kwargs.kwargs.get("include_archived") is False
            or call_kwargs[1].get("include_archived") is False
        )

    @pytest.mark.asyncio
    async def test_list_adrs_excludes_archived_by_default(
        self, tools: dict[str, Any], services: dict[str, Any]
    ) -> None:
        """brain_list excludes archived ADRs by default."""
        services["adr_svc"].list_all.return_value = []
        await tools["brain_list"](entity_type="adr")
        call_kwargs = services["adr_svc"].list_all.call_args
        assert (
            call_kwargs.kwargs.get("include_archived") is False
            or call_kwargs[1].get("include_archived") is False
        )


# ===========================================================================
# brain_get — git-style id prefix resolution
# ===========================================================================


class TestBrainGetIdPrefix:
    @pytest.mark.asyncio
    async def test_resolves_prefix_through_entity_service(
        self, tools: dict[str, Any], services: dict[str, Any]
    ) -> None:
        """A unique prefix resolves to the full UUID before the get_by_id call."""
        uid = uuid4()
        services["decision_svc"].resolve_id_prefix = AsyncMock(return_value=[uid])
        # get_by_id default (None) → the not-found message must carry the FULL
        # uuid, proving resolution happened before lookup.
        result = await tools["brain_get"](entity_type="decision", entity_id=uid.hex[:8])

        services["decision_svc"].resolve_id_prefix.assert_awaited_once_with(uid.hex[:8])
        services["decision_svc"].get_by_id.assert_awaited_once_with(uid)
        assert "not found" in result

    @pytest.mark.asyncio
    async def test_ambiguous_prefix_lists_matches(
        self, tools: dict[str, Any], services: dict[str, Any]
    ) -> None:
        """Ambiguity reports every candidate UUID and does no lookup."""
        a, b = uuid4(), uuid4()
        services["learning_svc"].resolve_id_prefix = AsyncMock(return_value=[a, b])

        result = await tools["brain_get"](entity_type="learning", entity_id="61b0fa47")

        assert "Ambiguous" in result
        assert str(a) in result
        assert str(b) in result
        services["learning_svc"].get_by_id.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_plan_prefix_not_supported_v1(self, tools: dict[str, Any]) -> None:
        """plan is served by PgIndexedPlanRepo (no BasePgRepository) — prefix
        resolution is deliberately not wired for it in v1."""
        result = await tools["brain_get"](entity_type="plan", entity_id="61b0fa47")

        assert "Invalid UUID" in result
