"""Unit tests for the T6 extension of brain_propose_adr (Dream v3 Spec A).

Adds two optional kwargs — source_learning_id + auto_accept + dream_run_id —
that graduate a mature learning directly into an accepted ADR via
ADRService.create_with_promotion. Kwarg-pair validation prevents foot-guns
for non-Dream callers; IntegrityError on duplicate source is translated to
a typed error string.
"""

from __future__ import annotations

from datetime import datetime
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest
from fastmcp import FastMCP
from sqlalchemy.exc import IntegrityError

from brain_v42.mcp.tools.brain_tools import register_tools
from brain_v42.models.adr import ADR
from tests.unit.mcp._tool_error_adapter import capture_tool_errors


def _make_adr(**kwargs) -> ADR:
    defaults = {
        "id": uuid4(),
        "number": 1,
        "title": "T",
        "context": "c",
        "decision": "d",
        "consequences": "q",
        "alternatives_considered": [],
        "project_key": "brain-v42",
        "tags": [],
        "status": "accepted",
        "decided_at": datetime(2026, 4, 18),
        "superseded_by": None,
        "embedding": None,
        "metadata": {},
        "created_at": datetime(2026, 4, 18),
        "updated_at": datetime(2026, 4, 18),
    }
    defaults.update(kwargs)
    return ADR.model_validate(defaults)


def _make_mcp_with_adr_tools() -> tuple[FastMCP, MagicMock]:
    mcp = FastMCP("test-brain")
    mock_adr_svc = MagicMock()
    register_tools(
        mcp,
        decision_svc=MagicMock(),
        learning_svc=MagicMock(),
        snippet_svc=MagicMock(),
        runbook_svc=MagicMock(),
        adr_svc=mock_adr_svc,
        project_context_svc=MagicMock(),
        brain_svc=MagicMock(),
    )
    return mcp, mock_adr_svc


async def _get_tool_fn(mcp: FastMCP, name: str):
    tool = await mcp.get_tool(name)
    return capture_tool_errors(tool.fn)


_SOURCE_ID = "11111111-1111-1111-1111-111111111111"


@pytest.mark.asyncio
async def test_propose_adr_rejects_source_without_auto_accept() -> None:
    """source_learning_id set but auto_accept=False → validation error (ADR path)."""
    mcp, mock_svc = _make_mcp_with_adr_tools()
    fn = await _get_tool_fn(mcp, "brain_propose_adr")

    reply = await fn(
        title="T",
        context="c",
        decision="d",
        consequences="q",
        project_key="brain-v42",
        source_learning_id=_SOURCE_ID,
        auto_accept=False,
    )
    assert "source_learning_id requires auto_accept=True" in reply
    mock_svc.create.assert_not_called()


@pytest.mark.asyncio
async def test_propose_adr_rejects_auto_accept_without_source() -> None:
    """auto_accept=True without source_learning_id → validation error (Dream-only path)."""
    mcp, mock_svc = _make_mcp_with_adr_tools()
    fn = await _get_tool_fn(mcp, "brain_propose_adr")

    reply = await fn(
        title="T",
        context="c",
        decision="d",
        consequences="q",
        project_key="brain-v42",
        auto_accept=True,
    )
    assert "auto_accept=True requires source_learning_id" in reply
    mock_svc.create.assert_not_called()


@pytest.mark.asyncio
async def test_propose_adr_backcompat_no_new_kwargs() -> None:
    """Calls without the new kwargs still invoke the legacy create path."""
    mcp, mock_svc = _make_mcp_with_adr_tools()
    mock_svc.create = AsyncMock(return_value=_make_adr(status="proposed"))
    fn = await _get_tool_fn(mcp, "brain_propose_adr")

    reply = await fn(
        title="T",
        context="c",
        decision="d",
        consequences="q",
        project_key="brain-v42",
    )
    assert "proposed" in reply.lower()
    mock_svc.create.assert_awaited_once()


@pytest.mark.asyncio
async def test_propose_adr_happy_path_calls_create_with_promotion() -> None:
    """Both kwargs set → routes to ADRService.create_with_promotion with auto_accept=True."""
    mcp, mock_svc = _make_mcp_with_adr_tools()
    adr = _make_adr(number=5, status="accepted")
    mock_svc.create_with_promotion = AsyncMock(return_value=adr)
    fn = await _get_tool_fn(mcp, "brain_propose_adr")

    reply = await fn(
        title="T",
        context="c",
        decision="d",
        consequences="q",
        project_key="brain-v42",
        source_learning_id=_SOURCE_ID,
        auto_accept=True,
        dream_run_id=None,
    )
    mock_svc.create_with_promotion.assert_awaited_once()
    kwargs = mock_svc.create_with_promotion.await_args.kwargs
    assert str(kwargs["source_learning_id"]) == _SOURCE_ID
    assert kwargs["auto_accept"] is True
    assert "accepted" in reply.lower()


@pytest.mark.asyncio
async def test_propose_adr_translates_duplicate_source_integrity_error() -> None:
    """IntegrityError from repo (dup source) → typed error, not unhandled exception."""
    mcp, mock_svc = _make_mcp_with_adr_tools()
    mock_svc.create_with_promotion = AsyncMock(
        side_effect=IntegrityError("stmt", {}, Exception("dup"))
    )
    fn = await _get_tool_fn(mcp, "brain_propose_adr")

    reply = await fn(
        title="T",
        context="c",
        decision="d",
        consequences="q",
        project_key="brain-v42",
        source_learning_id=_SOURCE_ID,
        auto_accept=True,
    )
    assert "already" in reply.lower() and "materialized" in reply.lower()


@pytest.mark.asyncio
async def test_propose_adr_rejects_a_lone_dream_run_id_instead_of_swallowing_it() -> None:
    """Le troisième membre du trio était AVALÉ en silence (ticket af3b58dd, item 2).

    Mesuré le 2026-08-29 : `dream_run_id` sans la paire tombait dans le chemin
    standard qui ne le lit jamais — l'appelant croyait tracer une promotion que
    rien n'enregistrait. L'invariant du trio vit désormais en UN endroit
    (`_dream_promotion_invariant`), pas en gardes dispersées : un paramètre
    dream-only orphelin est un refus nommé, jamais un silence.
    """
    mcp, mock_svc = _make_mcp_with_adr_tools()
    fn = await _get_tool_fn(mcp, "brain_propose_adr")

    reply = await fn(
        title="T",
        context="c",
        decision="d",
        consequences="q",
        project_key="brain-v42",
        dream_run_id=42,
    )

    assert "dream_run_id" in reply
    assert "Dream-only" in reply
    mock_svc.create.assert_not_called()
