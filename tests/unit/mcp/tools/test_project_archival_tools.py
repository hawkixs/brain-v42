"""The archive verb reaches MCP, and it does not go through the PATCH that isn't one.

`brain_set_project_context` writes the whole row: a caller who omits a field
clears it. Archiving through it would silently drop focus, blockers, group and
metadata — the trap ticket 44ee7643 already paid for. These tools call the
repository's targeted UPDATE instead, and this file pins that they keep doing so.
"""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock

import pytest


class _Recorder:
    """Collects the tools a registrar registers, so they can be called directly."""

    def __init__(self) -> None:
        self.tools: dict[str, object] = {}

    def tool(self, *args, **kwargs):
        def _decorator(fn):
            self.tools[fn.__name__] = fn
            return fn

        return _decorator


def _register(service: MagicMock) -> _Recorder:
    from brain_v42.mcp.tools.project_context_tools import register_project_context_tools

    recorder = _Recorder()
    register_project_context_tools(recorder, service)
    return recorder


def _service() -> MagicMock:
    service = MagicMock()
    context = MagicMock()
    context.project_key = "red-quant"
    context.archived_at = datetime(2026, 9, 22, tzinfo=UTC)
    context.archived_reason = "no development since 2026-04-18"
    service.archive = AsyncMock(return_value=context)
    service.unarchive = AsyncMock(return_value=context)
    return service


class TestTheToolsExist:
    def test_both_verbs_are_registered(self) -> None:
        tools = _register(_service()).tools

        assert "brain_project_archive" in tools
        assert "brain_project_unarchive" in tools


class TestArchive:
    @pytest.mark.asyncio
    async def test_it_calls_the_targeted_update_not_the_context_upsert(self) -> None:
        service = _service()
        tools = _register(service).tools

        await tools["brain_project_archive"](
            project_key="red-quant", reason="no development since 2026-04-18"
        )

        service.archive.assert_awaited_once()
        service.get_or_create.assert_not_called()
        service.update_focus.assert_not_called()

    @pytest.mark.asyncio
    async def test_a_reason_is_required_by_the_signature(self) -> None:
        """An archive nobody can explain outlives the reason it was archived for."""
        import inspect

        tools = _register(_service()).tools
        signature = inspect.signature(tools["brain_project_archive"])

        assert signature.parameters["reason"].default is inspect.Parameter.empty

    @pytest.mark.asyncio
    async def test_an_unknown_project_is_reported_not_invented(self) -> None:
        service = _service()
        service.archive = AsyncMock(return_value=None)
        tools = _register(service).tools

        result = await tools["brain_project_archive"](project_key="no-such", reason="x")

        assert "no-such" in result
        service.get_or_create.assert_not_called()

    @pytest.mark.asyncio
    async def test_the_answer_says_no_knowledge_was_removed(self) -> None:
        """The operator rule, said where the operator reads it."""
        tools = _register(_service()).tools

        result = await tools["brain_project_archive"](project_key="red-quant", reason="dead")

        assert "red-quant" in result
        assert "deleted" in result.lower() or "supprim" in result.lower()


class TestUnarchive:
    @pytest.mark.asyncio
    async def test_it_calls_the_targeted_update(self) -> None:
        service = _service()
        tools = _register(service).tools

        await tools["brain_project_unarchive"](project_key="red-quant")

        service.unarchive.assert_awaited_once_with("red-quant")
        service.get_or_create.assert_not_called()
