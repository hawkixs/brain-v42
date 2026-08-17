"""Unit tests for MCP runbook tools: brain_create_runbook, brain_get_runbook, brain_execute_runbook.

brain_search_runbooks has been removed — use brain_search(types=["runbook"]) instead.
All service calls are mocked with AsyncMock — no real DB or ONNX needed.
"""

from __future__ import annotations

import inspect
import uuid
from datetime import datetime
from typing import Any
from unittest.mock import AsyncMock, MagicMock, call

import pytest
from fastmcp import Client, FastMCP
from fastmcp.exceptions import ToolError

from brain_v42.mcp.tools.runbook_tools import register_runbook_tools
from brain_v42.models.runbook import Runbook, RunbookCreate, RunbookStep
from tests.unit.mcp._tool_error_adapter import capture_tool_errors

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def make_runbook(**kwargs: Any) -> Runbook:
    """Build a Runbook with sensible defaults."""
    defaults: dict[str, Any] = {
        "id": uuid.uuid4(),
        "title": "Deploy app",
        "description": "Deploy the application to production",
        "project_key": "brain-v42",
        "trigger": "on release",
        "prerequisites": [],
        "steps": [
            RunbookStep(order=1, title="Build", command="make build"),
        ],
        "rollback_steps": [],
        "estimated_duration": "10m",
        "tags": [],
        "metadata": {},
        "execution_count": 0,
        "last_executed_at": None,
        "last_execution_status": None,
        "embedding": None,
        "created_at": datetime(2024, 1, 1),
        "updated_at": datetime(2024, 1, 1),
    }
    defaults.update(kwargs)
    return Runbook.model_validate(defaults)


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
def mock_runbook_svc() -> MagicMock:
    """Mock RunbookService with all async methods."""
    svc = MagicMock()
    svc.create = AsyncMock(return_value=make_runbook())
    svc.get_by_id = AsyncMock(return_value=make_runbook())
    svc.get_by_title = AsyncMock(return_value=make_runbook())
    svc.list_by_project = AsyncMock(return_value=[make_runbook()])
    svc.record_execution = AsyncMock(return_value=make_runbook())
    return svc


@pytest.fixture
def tools(mock_runbook_svc: MagicMock) -> tuple[dict[str, Any], MagicMock]:
    """Register runbook tools and return (registered_tools_dict, mock_svc)."""
    from brain_v42.mcp.tools.runbook_tools import register_runbook_tools

    mcp = MockMCP()
    register_runbook_tools(mcp, mock_runbook_svc)
    return mcp.registered, mock_runbook_svc


@pytest.fixture
def tools_with_access_logger(
    mock_runbook_svc: MagicMock,
) -> tuple[dict[str, Any], MagicMock, MagicMock]:
    from brain_v42.mcp.tools.runbook_tools import register_runbook_tools

    mcp = MockMCP()
    access_logger = MagicMock()
    optional_logger = (
        {"access_logger": access_logger}
        if "access_logger" in inspect.signature(register_runbook_tools).parameters
        else {}
    )
    register_runbook_tools(mcp, mock_runbook_svc, **optional_logger)
    return mcp.registered, mock_runbook_svc, access_logger


# ---------------------------------------------------------------------------
# brain_create_runbook
# ---------------------------------------------------------------------------


class TestBrainCreateRunbook:
    async def test_returns_confirmation_string(
        self, tools: tuple[dict[str, Any], MagicMock]
    ) -> None:
        """brain_create_runbook returns confirmation string with title and step count."""
        registered, svc = tools
        runbook = make_runbook(title="Deploy app")
        svc.create.return_value = runbook

        result = await registered["brain_create_runbook"](
            title="Deploy app",
            description="Deploy the application",
            project_key="brain-v42",
            trigger="on release",
            steps=[{"order": 1, "title": "Build", "command": "make build"}],
        )

        assert isinstance(result, str)
        assert "Runbook created" in result

    async def test_calls_service_create_with_runbook_create(
        self, tools: tuple[dict[str, Any], MagicMock]
    ) -> None:
        """brain_create_runbook calls runbook_svc.create() with correct RunbookCreate."""
        registered, svc = tools

        await registered["brain_create_runbook"](
            title="Deploy app",
            description="Deploy the application",
            project_key="brain-v42",
            trigger="on release",
            steps=[{"order": 1, "title": "Build", "command": "make build"}],
        )

        svc.create.assert_called_once()
        call_arg = svc.create.call_args[0][0]
        assert isinstance(call_arg, RunbookCreate)
        assert call_arg.title == "Deploy app"
        assert call_arg.description == "Deploy the application"
        assert call_arg.project_key == "brain-v42"
        assert call_arg.trigger == "on release"

    async def test_converts_steps_dicts_to_runbook_step_objects(
        self, tools: tuple[dict[str, Any], MagicMock]
    ) -> None:
        """brain_create_runbook converts list[dict] steps to list[RunbookStep]."""
        registered, svc = tools

        await registered["brain_create_runbook"](
            title="Deploy",
            description="Deploy desc",
            project_key="proj",
            trigger="manual",
            steps=[
                {"order": 1, "title": "Step 1", "command": "cmd1"},
                {"order": 2, "title": "Step 2", "command": "cmd2"},
            ],
        )

        call_arg = svc.create.call_args[0][0]
        assert len(call_arg.steps) == 2
        assert all(isinstance(s, RunbookStep) for s in call_arg.steps)
        assert call_arg.steps[0].order == 1
        assert call_arg.steps[0].title == "Step 1"
        assert call_arg.steps[0].command == "cmd1"
        assert call_arg.steps[1].order == 2

    async def test_converts_rollback_steps_dicts_to_runbook_step_objects(
        self, tools: tuple[dict[str, Any], MagicMock]
    ) -> None:
        """brain_create_runbook converts rollback_steps dicts to RunbookStep objects."""
        registered, svc = tools

        await registered["brain_create_runbook"](
            title="Deploy",
            description="Deploy desc",
            project_key="proj",
            trigger="manual",
            steps=[{"order": 1, "title": "Step 1"}],
            rollback_steps=[{"order": 1, "title": "Rollback 1", "command": "rollback"}],
        )

        call_arg = svc.create.call_args[0][0]
        assert len(call_arg.rollback_steps) == 1
        assert isinstance(call_arg.rollback_steps[0], RunbookStep)
        assert call_arg.rollback_steps[0].title == "Rollback 1"

    async def test_defaults_prerequisites_to_empty_list(
        self, tools: tuple[dict[str, Any], MagicMock]
    ) -> None:
        """brain_create_runbook defaults prerequisites=[] when not provided."""
        registered, svc = tools

        await registered["brain_create_runbook"](
            title="Deploy",
            description="Deploy desc",
            project_key="proj",
            trigger="manual",
            steps=[{"order": 1, "title": "Step 1"}],
        )

        call_arg = svc.create.call_args[0][0]
        assert call_arg.prerequisites == []

    async def test_defaults_tags_to_empty_list(
        self, tools: tuple[dict[str, Any], MagicMock]
    ) -> None:
        """brain_create_runbook defaults tags=[] when not provided."""
        registered, svc = tools

        await registered["brain_create_runbook"](
            title="Deploy",
            description="Deploy desc",
            project_key="proj",
            trigger="manual",
            steps=[{"order": 1, "title": "Step 1"}],
            tags=None,
        )

        call_arg = svc.create.call_args[0][0]
        assert call_arg.tags == []

    async def test_passes_optional_fields(self, tools: tuple[dict[str, Any], MagicMock]) -> None:
        """brain_create_runbook passes optional fields to RunbookCreate."""
        registered, svc = tools

        await registered["brain_create_runbook"](
            title="Deploy",
            description="Deploy desc",
            project_key="proj",
            trigger="manual",
            steps=[{"order": 1, "title": "Step 1"}],
            prerequisites=["Docker running"],
            estimated_duration="15m",
            tags=["deploy", "production"],
        )

        call_arg = svc.create.call_args[0][0]
        assert call_arg.prerequisites == ["Docker running"]
        assert call_arg.estimated_duration == "15m"
        assert call_arg.tags == ["deploy", "production"]

    async def test_confirmation_includes_step_count(
        self, tools: tuple[dict[str, Any], MagicMock]
    ) -> None:
        """brain_create_runbook confirmation includes steps count."""
        registered, svc = tools
        runbook = make_runbook(
            steps=[
                RunbookStep(order=1, title="Step 1"),
                RunbookStep(order=2, title="Step 2"),
            ]
        )
        svc.create.return_value = runbook

        result = await registered["brain_create_runbook"](
            title="Deploy",
            description="Deploy desc",
            project_key="proj",
            trigger="manual",
            steps=[{"order": 1, "title": "Step 1"}, {"order": 2, "title": "Step 2"}],
        )

        assert "steps:2" in result


# ---------------------------------------------------------------------------
# brain_get_runbook
# ---------------------------------------------------------------------------


class TestBrainGetRunbook:
    async def test_detail_reads_log_uuid_but_lists_do_not(
        self,
        tools_with_access_logger: tuple[dict[str, Any], MagicMock, MagicMock],
    ) -> None:
        """ID/title details refresh runbooks; project lists do not."""
        registered, svc, access_logger = tools_with_access_logger
        by_id = make_runbook()
        by_title = make_runbook()
        svc.get_by_id.return_value = by_id
        svc.get_by_title.return_value = by_title

        await registered["brain_get_runbook"](runbook_id=str(by_id.id))
        await registered["brain_get_runbook"](
            title=by_title.title,
            project_key=by_title.project_key,
        )
        await registered["brain_get_runbook"](project_key="brain-v42")

        assert access_logger.log_access.call_args_list == [
            call("runbook", by_id.id, "get_by_id"),
            call("runbook", by_title.id, "get_by_id"),
        ]

    async def test_missing_detail_logs_nothing(
        self,
        tools_with_access_logger: tuple[dict[str, Any], MagicMock, MagicMock],
    ) -> None:
        """A failed detail read is not usage evidence."""
        registered, svc, access_logger = tools_with_access_logger
        svc.get_by_id.return_value = None

        await registered["brain_get_runbook"](runbook_id=str(uuid.uuid4()))

        access_logger.log_access.assert_not_called()

    async def test_dispatches_to_get_by_id_when_runbook_id_provided(
        self, tools: tuple[dict[str, Any], MagicMock]
    ) -> None:
        """brain_get_runbook calls get_by_id when runbook_id is provided."""
        registered, svc = tools
        runbook_id = uuid.uuid4()
        svc.get_by_id.return_value = make_runbook(id=runbook_id, title="Deploy app")

        result = await registered["brain_get_runbook"](runbook_id=str(runbook_id))

        svc.get_by_id.assert_called_once_with(runbook_id)
        assert isinstance(result, str)
        assert "Deploy app" in result

    async def test_dispatches_to_get_by_title_when_title_and_project_key_provided(
        self, tools: tuple[dict[str, Any], MagicMock]
    ) -> None:
        """brain_get_runbook calls get_by_title when title+project_key are provided."""
        registered, svc = tools
        svc.get_by_title.return_value = make_runbook(title="Deploy app")

        result = await registered["brain_get_runbook"](
            title="Deploy app",
            project_key="brain-v42",
        )

        svc.get_by_title.assert_called_once_with("Deploy app", "brain-v42")
        assert isinstance(result, str)
        assert "Deploy app" in result

    async def test_dispatches_to_list_by_project_when_only_project_key_provided(
        self, tools: tuple[dict[str, Any], MagicMock]
    ) -> None:
        """brain_get_runbook calls list_by_project when only project_key provided."""
        registered, svc = tools
        runbooks = [make_runbook(title="R1"), make_runbook(title="R2")]
        svc.list_by_project.return_value = runbooks

        result = await registered["brain_get_runbook"](project_key="brain-v42")

        # list_by_project is called with clamped+1 (default limit=10 → 11) to
        # detect pagination overflow without a separate COUNT query.
        svc.list_by_project.assert_called_once_with("brain-v42", limit=11)
        assert isinstance(result, str)
        assert "2 runbook" in result

    async def test_returns_error_when_no_args_provided(
        self, tools: tuple[dict[str, Any], MagicMock]
    ) -> None:
        """brain_get_runbook returns error string when no valid args provided."""
        registered, svc = tools

        result = await registered["brain_get_runbook"]()

        assert isinstance(result, str)
        assert result and result[0].isalnum()
        svc.get_by_id.assert_not_called()
        svc.get_by_title.assert_not_called()
        svc.list_by_project.assert_not_called()

    async def test_returns_error_when_get_by_id_not_found(
        self, tools: tuple[dict[str, Any], MagicMock]
    ) -> None:
        """brain_get_runbook returns error string when get_by_id returns None."""
        registered, svc = tools
        svc.get_by_id.return_value = None

        result = await registered["brain_get_runbook"](runbook_id=str(uuid.uuid4()))

        assert isinstance(result, str)
        assert result and result[0].isalnum()
        assert "not found" in result

    async def test_runbook_id_takes_priority_over_title(
        self, tools: tuple[dict[str, Any], MagicMock]
    ) -> None:
        """brain_get_runbook dispatches to get_by_id when runbook_id is set (even with title)."""
        registered, svc = tools
        runbook_id = uuid.uuid4()
        svc.get_by_id.return_value = make_runbook(id=runbook_id)

        await registered["brain_get_runbook"](
            runbook_id=str(runbook_id),
            title="Deploy app",
            project_key="brain-v42",
        )

        svc.get_by_id.assert_called_once_with(runbook_id)
        svc.get_by_title.assert_not_called()


# ---------------------------------------------------------------------------
# brain_execute_runbook
# ---------------------------------------------------------------------------


class TestBrainExecuteRunbook:
    async def test_successful_execution_logs_uuid_after_recording(
        self,
        tools_with_access_logger: tuple[dict[str, Any], MagicMock, MagicMock],
    ) -> None:
        """Only a persisted execution emits an execute signal."""
        registered, svc, access_logger = tools_with_access_logger
        runbook = make_runbook(execution_count=1)
        svc.record_execution.return_value = runbook

        await registered["brain_execute_runbook"](runbook_id=str(runbook.id))

        access_logger.log_access.assert_called_once_with("runbook", runbook.id, "execute")

    async def test_failed_execution_logs_nothing(
        self,
        tools_with_access_logger: tuple[dict[str, Any], MagicMock, MagicMock],
    ) -> None:
        """Missing and invalid executions never refresh a runbook."""
        registered, svc, access_logger = tools_with_access_logger
        svc.record_execution.return_value = None

        await registered["brain_execute_runbook"](runbook_id=str(uuid.uuid4()))
        await registered["brain_execute_runbook"](runbook_id="not-a-valid-uuid")

        access_logger.log_access.assert_not_called()

    async def test_returns_confirmation_string_on_success(
        self, tools: tuple[dict[str, Any], MagicMock]
    ) -> None:
        """brain_execute_runbook returns confirmation string on success."""
        registered, svc = tools
        runbook_id = uuid.uuid4()
        runbook = make_runbook(id=runbook_id, title="Deploy app", execution_count=1)
        svc.record_execution.return_value = runbook

        result = await registered["brain_execute_runbook"](runbook_id=str(runbook_id))

        assert isinstance(result, str)
        assert "Runbook executed" in result

    async def test_calls_record_execution_with_uuid_and_status(
        self, tools: tuple[dict[str, Any], MagicMock]
    ) -> None:
        """brain_execute_runbook calls record_execution(UUID, status)."""
        registered, svc = tools
        runbook_id = uuid.uuid4()
        svc.record_execution.return_value = make_runbook(id=runbook_id)

        await registered["brain_execute_runbook"](
            runbook_id=str(runbook_id),
            status="success",
        )

        svc.record_execution.assert_called_once_with(runbook_id, "success")

    async def test_default_status_is_success(self, tools: tuple[dict[str, Any], MagicMock]) -> None:
        """brain_execute_runbook uses 'success' as default status."""
        registered, svc = tools
        runbook_id = uuid.uuid4()
        svc.record_execution.return_value = make_runbook(id=runbook_id)

        await registered["brain_execute_runbook"](runbook_id=str(runbook_id))

        call_args = svc.record_execution.call_args[0]
        assert call_args[1] == "success"

    async def test_returns_error_when_runbook_not_found(
        self, tools: tuple[dict[str, Any], MagicMock]
    ) -> None:
        """brain_execute_runbook returns error string when service returns None."""
        registered, svc = tools
        runbook_id = str(uuid.uuid4())
        svc.record_execution.return_value = None

        result = await registered["brain_execute_runbook"](runbook_id=runbook_id)

        assert isinstance(result, str)
        assert result and result[0].isalnum()
        assert "not found" in result

    async def test_passes_failed_status_to_service(
        self, tools: tuple[dict[str, Any], MagicMock]
    ) -> None:
        """brain_execute_runbook passes 'failed' status to record_execution."""
        registered, svc = tools
        runbook_id = uuid.uuid4()
        svc.record_execution.return_value = make_runbook(
            id=runbook_id, last_execution_status="failed"
        )

        result = await registered["brain_execute_runbook"](
            runbook_id=str(runbook_id),
            status="failed",
        )

        call_args = svc.record_execution.call_args[0]
        assert call_args[1] == "failed"
        assert "status:failed" in result

    async def test_confirmation_includes_count(
        self, tools: tuple[dict[str, Any], MagicMock]
    ) -> None:
        """brain_execute_runbook confirmation includes execution count."""
        registered, svc = tools
        runbook_id = uuid.uuid4()
        svc.record_execution.return_value = make_runbook(id=runbook_id, execution_count=3)

        result = await registered["brain_execute_runbook"](runbook_id=str(runbook_id))

        assert "count:3" in result

    async def test_rejects_invalid_status(self, mock_runbook_svc: MagicMock) -> None:
        """The MCP schema rejects an invalid execution status."""
        runbook_id = str(uuid.uuid4())
        mcp = FastMCP("test-runbook-status")
        register_runbook_tools(mcp, mock_runbook_svc)

        async with Client(mcp) as client:
            with pytest.raises(ToolError) as exc_info:
                await client.call_tool(
                    "brain_execute_runbook",
                    {"runbook_id": runbook_id, "status": "invalid_status"},
                )

        assert "invalid_status" in str(exc_info.value)
        mock_runbook_svc.record_execution.assert_not_called()

    async def test_returns_error_for_invalid_uuid(
        self, tools: tuple[dict[str, Any], MagicMock]
    ) -> None:
        """brain_execute_runbook returns an unprefixed error string for an invalid UUID."""
        registered, svc = tools

        result = await registered["brain_execute_runbook"](runbook_id="not-a-valid-uuid")
        assert isinstance(result, str)
        assert result and result[0].isalnum(), f"Expected an unprefixed error, got: {result!r}"
        svc.record_execution.assert_not_called()


# ---------------------------------------------------------------------------
# Git-style id prefix resolution (brain_get_runbook / brain_execute_runbook)
# ---------------------------------------------------------------------------


class TestIdPrefixResolution:
    async def test_get_runbook_resolves_unique_prefix(
        self, tools: tuple[dict[str, Any], MagicMock]
    ) -> None:
        """An 8-char hex prefix with a unique match resolves like a full UUID."""
        registered, svc = tools
        uid = uuid.uuid4()
        svc.resolve_id_prefix = AsyncMock(return_value=[uid])
        svc.get_by_id = AsyncMock(return_value=make_runbook(id=uid, title="Morning check"))

        result = await registered["brain_get_runbook"](runbook_id=uid.hex[:8])

        svc.resolve_id_prefix.assert_awaited_once_with(uid.hex[:8])
        svc.get_by_id.assert_awaited_once_with(uid)
        assert "Morning check" in result

    async def test_get_runbook_ambiguous_prefix_lists_matches(
        self, tools: tuple[dict[str, Any], MagicMock]
    ) -> None:
        """Two matches → error listing the full UUIDs, no lookup attempted."""
        registered, svc = tools
        a, b = uuid.uuid4(), uuid.uuid4()
        svc.resolve_id_prefix = AsyncMock(return_value=[a, b])
        svc.get_by_id = AsyncMock()

        result = await registered["brain_get_runbook"](runbook_id="61b0fa47")

        assert "Ambiguous" in result
        assert str(a) in result
        assert str(b) in result
        svc.get_by_id.assert_not_awaited()

    async def test_get_runbook_prefix_without_match_reports_it(
        self, tools: tuple[dict[str, Any], MagicMock]
    ) -> None:
        """Zero matches → explicit no-match error naming the prefix."""
        registered, svc = tools
        svc.resolve_id_prefix = AsyncMock(return_value=[])

        result = await registered["brain_get_runbook"](runbook_id="61b0fa47")

        assert "No runbook found for id prefix '61b0fa47'" in result

    async def test_get_runbook_garbage_id_stays_invalid_uuid(
        self, tools: tuple[dict[str, Any], MagicMock]
    ) -> None:
        """Non-hex garbage keeps the historical Invalid UUID error, no DB hit."""
        registered, svc = tools
        svc.resolve_id_prefix = AsyncMock()

        result = await registered["brain_get_runbook"](runbook_id="not-a-uuid")

        assert "Invalid UUID" in result
        svc.resolve_id_prefix.assert_not_awaited()

    async def test_execute_runbook_resolves_unique_prefix(
        self, tools: tuple[dict[str, Any], MagicMock]
    ) -> None:
        """brain_execute_runbook accepts a unique prefix (morning-check flow)."""
        registered, svc = tools
        uid = uuid.uuid4()
        svc.resolve_id_prefix = AsyncMock(return_value=[uid])
        svc.record_execution = AsyncMock(return_value=make_runbook(id=uid))

        result = await registered["brain_execute_runbook"](runbook_id=uid.hex[:8])

        svc.record_execution.assert_awaited_once_with(uid, "success")
        assert "Runbook executed" in result
