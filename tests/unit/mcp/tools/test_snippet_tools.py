"""Unit tests for MCP snippet tools: brain_save_snippet, brain_use_snippet.

brain_find_snippet has been removed — use brain_search(types=["snippet"]) instead.
All service calls are mocked with AsyncMock — no real DB or ONNX needed.
"""

from __future__ import annotations

import inspect
import uuid
from datetime import datetime
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from brain_v42.models.snippet import Snippet
from tests.unit.mcp._tool_error_adapter import capture_tool_errors

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def make_snippet(**kwargs: Any) -> Snippet:
    """Build a Snippet with sensible defaults."""
    defaults: dict[str, Any] = {
        "id": uuid.uuid4(),
        "title": "Parse JSON",
        "intention": "Parse JSON from a string",
        "code": "import json; data = json.loads(s)",
        "language": "python",
        "dependencies": [],
        "usage_example": None,
        "gotchas": None,
        "project_key": "brain-v42",
        "tags": [],
        "metadata": {},
        "use_count": 0,
        "last_used_at": None,
        "embedding": None,
        "created_at": datetime(2024, 1, 1),
        "updated_at": datetime(2024, 1, 1),
    }
    defaults.update(kwargs)
    return Snippet.model_validate(defaults)


class MockMCP:
    """Collecting mock for FastMCP — stores registered tools by function name."""

    def __init__(self) -> None:
        self.registered: dict[str, Any] = {}

    def tool(self, **kwargs: Any) -> Any:
        def decorator(fn: Any) -> Any:
            self.registered[fn.__name__] = capture_tool_errors(fn)
            return fn

        return decorator


@pytest.fixture
def mock_snippet_svc() -> AsyncMock:
    """Mock SnippetService with all async methods."""
    svc = AsyncMock()
    return svc


@pytest.fixture
def tools(mock_snippet_svc: AsyncMock) -> tuple[dict[str, Any], AsyncMock]:
    """Register snippet tools and return (registered_tools_dict, mock_svc)."""
    from brain_v42.mcp.tools.snippet_tools import register_snippet_tools

    mcp = MockMCP()
    register_snippet_tools(mcp, mock_snippet_svc)
    return mcp.registered, mock_snippet_svc


@pytest.fixture
def tools_with_access_logger(
    mock_snippet_svc: AsyncMock,
) -> tuple[dict[str, Any], AsyncMock, MagicMock]:
    from brain_v42.mcp.tools.snippet_tools import register_snippet_tools

    mcp = MockMCP()
    access_logger = MagicMock()
    optional_logger = (
        {"access_logger": access_logger}
        if "access_logger" in inspect.signature(register_snippet_tools).parameters
        else {}
    )
    register_snippet_tools(mcp, mock_snippet_svc, **optional_logger)
    return mcp.registered, mock_snippet_svc, access_logger


# ---------------------------------------------------------------------------
# brain_save_snippet
# ---------------------------------------------------------------------------


class TestBrainSaveSnippet:
    async def test_returns_confirmation_string(
        self, tools: tuple[dict[str, Any], AsyncMock]
    ) -> None:
        """brain_save_snippet returns a confirmation string with title and id."""
        registered, svc = tools
        snippet = make_snippet(title="Async fetch")
        svc.create.return_value = snippet

        result = await registered["brain_save_snippet"](
            title="Async fetch",
            intention="Fetch data asynchronously with aiohttp",
            code="async with aiohttp.ClientSession() as s: ...",
            language="python",
        )

        assert isinstance(result, str)
        assert "Snippet saved" in result

    async def test_calls_service_create_with_snippet_create(
        self, tools: tuple[dict[str, Any], AsyncMock]
    ) -> None:
        """brain_save_snippet calls snippet_svc.create() with correct SnippetCreate."""
        registered, svc = tools
        snippet = make_snippet()
        svc.create.return_value = snippet

        await registered["brain_save_snippet"](
            title="Parse JSON",
            intention="Parse JSON from a string",
            code="json.loads(s)",
            language="python",
            project_key="brain-v42",
            tags=["json", "parsing"],
        )

        svc.create.assert_called_once()
        call_arg = svc.create.call_args[0][0]
        assert call_arg.title == "Parse JSON"
        assert call_arg.intention == "Parse JSON from a string"
        assert call_arg.code == "json.loads(s)"
        assert call_arg.language == "python"
        assert call_arg.project_key == "brain-v42"
        assert call_arg.tags == ["json", "parsing"]

    async def test_defaults_dependencies_to_empty_list_when_none(
        self, tools: tuple[dict[str, Any], AsyncMock]
    ) -> None:
        """brain_save_snippet defaults dependencies=[] when caller passes None."""
        registered, svc = tools
        snippet = make_snippet()
        svc.create.return_value = snippet

        await registered["brain_save_snippet"](
            title="Test",
            intention="test intention",
            code="x = 1",
            language="python",
            dependencies=None,
        )

        call_arg = svc.create.call_args[0][0]
        assert call_arg.dependencies == []

    async def test_defaults_tags_to_empty_list_when_none(
        self, tools: tuple[dict[str, Any], AsyncMock]
    ) -> None:
        """brain_save_snippet defaults tags=[] when caller passes None."""
        registered, svc = tools
        snippet = make_snippet()
        svc.create.return_value = snippet

        await registered["brain_save_snippet"](
            title="Test",
            intention="test intention",
            code="x = 1",
            language="python",
            tags=None,
        )

        call_arg = svc.create.call_args[0][0]
        assert call_arg.tags == []

    async def test_passes_optional_fields(self, tools: tuple[dict[str, Any], AsyncMock]) -> None:
        """brain_save_snippet passes usage_example, gotchas to SnippetCreate."""
        registered, svc = tools
        snippet = make_snippet(usage_example="result = parse(data)", gotchas="Only works for UTF-8")
        svc.create.return_value = snippet

        await registered["brain_save_snippet"](
            title="Parse",
            intention="parse data",
            code="parse(data)",
            language="python",
            dependencies=["parser-lib"],
            usage_example="result = parse(data)",
            gotchas="Only works for UTF-8",
        )

        call_arg = svc.create.call_args[0][0]
        assert call_arg.dependencies == ["parser-lib"]
        assert call_arg.usage_example == "result = parse(data)"
        assert call_arg.gotchas == "Only works for UTF-8"

    async def test_confirmation_includes_language(
        self, tools: tuple[dict[str, Any], AsyncMock]
    ) -> None:
        """brain_save_snippet confirmation includes language info."""
        registered, svc = tools
        snippet = make_snippet(title="Fetch", language="typescript")
        svc.create.return_value = snippet

        result = await registered["brain_save_snippet"](
            title="Fetch",
            intention="fetch data",
            code="fetch(url)",
            language="typescript",
        )

        assert "lang:typescript" in result


# ---------------------------------------------------------------------------
# brain_use_snippet
# ---------------------------------------------------------------------------


class TestBrainUseSnippet:
    async def test_success_logs_use_after_increment(
        self,
        tools_with_access_logger: tuple[dict[str, Any], AsyncMock, MagicMock],
    ) -> None:
        """A durable snippet use produces one UUID usage signal."""
        registered, svc, access_logger = tools_with_access_logger
        snippet_id = uuid.uuid4()
        svc.increment_use.return_value = make_snippet(id=snippet_id, use_count=1)

        await registered["brain_use_snippet"](snippet_id=str(snippet_id))

        access_logger.log_access.assert_called_once_with("snippet", snippet_id, "use")

    async def test_rejected_or_missing_use_logs_nothing(
        self,
        tools_with_access_logger: tuple[dict[str, Any], AsyncMock, MagicMock],
    ) -> None:
        """Invalid and missing snippets are not counted as usage."""
        registered, svc, access_logger = tools_with_access_logger
        svc.increment_use.return_value = None

        await registered["brain_use_snippet"](snippet_id=str(uuid.uuid4()))
        await registered["brain_use_snippet"](snippet_id="not-a-valid-uuid")

        access_logger.log_access.assert_not_called()

    async def test_returns_confirmation_string(
        self, tools: tuple[dict[str, Any], AsyncMock]
    ) -> None:
        """brain_use_snippet returns confirmation string when snippet found."""
        registered, svc = tools
        snippet_id = uuid.uuid4()
        snippet = make_snippet(id=snippet_id, title="Parse JSON", use_count=5)
        svc.increment_use.return_value = snippet

        result = await registered["brain_use_snippet"](snippet_id=str(snippet_id))

        assert isinstance(result, str)
        assert "Snippet used" in result
        assert "use_count:5" in result

    async def test_calls_increment_use_with_uuid(
        self, tools: tuple[dict[str, Any], AsyncMock]
    ) -> None:
        """brain_use_snippet calls snippet_svc.increment_use(UUID(snippet_id))."""
        registered, svc = tools
        snippet_id = uuid.uuid4()
        snippet = make_snippet(id=snippet_id)
        svc.increment_use.return_value = snippet

        await registered["brain_use_snippet"](snippet_id=str(snippet_id))

        svc.increment_use.assert_called_once_with(snippet_id)

    async def test_returns_error_when_not_found(
        self, tools: tuple[dict[str, Any], AsyncMock]
    ) -> None:
        """brain_use_snippet returns error string when service returns None."""
        registered, svc = tools
        snippet_id = str(uuid.uuid4())
        svc.increment_use.return_value = None

        result = await registered["brain_use_snippet"](snippet_id=snippet_id)

        assert isinstance(result, str)
        assert result and result[0].isalnum()
        assert "not found" in result

    async def test_returns_error_for_invalid_uuid(
        self, tools: tuple[dict[str, Any], AsyncMock]
    ) -> None:
        """brain_use_snippet returns an unprefixed error string for an invalid UUID."""
        registered, svc = tools

        result = await registered["brain_use_snippet"](snippet_id="not-a-valid-uuid")
        assert isinstance(result, str)
        assert result and result[0].isalnum(), f"Expected an unprefixed error, got: {result!r}"
        svc.increment_use.assert_not_called()
