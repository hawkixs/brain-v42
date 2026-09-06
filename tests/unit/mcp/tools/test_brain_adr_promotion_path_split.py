"""The ADR promotion path is a TOOL, not three optional kwargs on the proposal.

Ticket af3b58dd item 2, structural half. `brain_propose_adr` published three
Dream-only parameters — `source_learning_id`, `auto_accept`, `dream_run_id` —
to every caller of a tool that most callers use to propose an ordinary ADR.
The combinations that mean nothing were refused at RUNTIME by
`_dream_promotion_invariant`, which is a good guard and a poor schema: the
invalid request could still be typed, sent, and named back at the caller.

What changes here is the SHAPE, not the check. Two paths, two tools, each
publishing only its own parameters:

    brain_propose_adr(...)  -> a proposed ADR. No Dream parameters exist.
    brain_promote_adr(..., source_learning_id=<required>) -> an accepted ADR.

`auto_accept` disappears entirely rather than moving: it carried no
information a dedicated tool does not already carry, and a boolean whose only
legal value is True is a trap for the next reader. `source_learning_id` stops
being optional, which is what makes the XOR structural — the three refusals
the invariant used to name are now unrepresentable, so the tests that pinned
those refusals are replaced by ONE test that the parameters are absent.

WHAT THIS DOES NOT COVER, measured on HEAD c32a0da and deliberately left:
`brain_create_runbook` carries the same Dream pair (`source_learning_id`,
`dream_run_id`) with no guard at all — a lone `dream_run_id` there still falls
into the standard path that never reads it, and is swallowed in silence. That
is the exact bug af3b58dd named for the ADR tool, still open on its twin. It
is out of this lot's surface, not out of the codebase.
"""

from __future__ import annotations

import inspect
from datetime import datetime
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest
from fastmcp import FastMCP
from sqlalchemy.exc import IntegrityError

from brain_v42.mcp.tools.brain_tools import register_tools
from brain_v42.models.adr import ADR
from tests.unit.mcp._tool_error_adapter import capture_tool_errors

_SOURCE_ID = "11111111-1111-1111-1111-111111111111"

#: The parameters that belong to the promotion path and nowhere else.
_DREAM_ONLY_PARAMETERS = ("source_learning_id", "auto_accept", "dream_run_id")


def _make_adr(**kwargs: object) -> ADR:
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


async def _tool_fn(mcp: FastMCP, name: str):  # noqa: ANN202 - FastMCP tool callable
    tool = await mcp.get_tool(name)
    assert tool is not None, f"{name} must be registered"
    return capture_tool_errors(tool.fn)


async def _parameters(mcp: FastMCP, name: str) -> dict[str, inspect.Parameter]:
    tool = await mcp.get_tool(name)
    assert tool is not None, f"{name} must be registered"
    return dict(inspect.signature(tool.fn).parameters)


# ── the structural claim ──────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_propose_adr_publishes_no_dream_only_parameter() -> None:
    """The proposal tool no longer carries the promotion path's parameters.

    This single assertion replaces the three runtime refusals that
    `_dream_promotion_invariant` used to name. It is a stronger statement: a
    refusal proves the server says no, an absent parameter proves the request
    cannot be built.
    """
    mcp, _ = _make_mcp_with_adr_tools()

    published = await _parameters(mcp, "brain_propose_adr")

    assert [name for name in _DREAM_ONLY_PARAMETERS if name in published] == []


@pytest.mark.asyncio
async def test_promote_adr_requires_its_source_learning() -> None:
    """`source_learning_id` is REQUIRED — that is what makes the XOR structural."""
    mcp, _ = _make_mcp_with_adr_tools()

    published = await _parameters(mcp, "brain_promote_adr")

    assert published["source_learning_id"].default is inspect.Parameter.empty


@pytest.mark.asyncio
async def test_promote_adr_does_not_publish_auto_accept() -> None:
    """A boolean whose only legal value is True is not a parameter."""
    mcp, _ = _make_mcp_with_adr_tools()

    published = await _parameters(mcp, "brain_promote_adr")

    assert "auto_accept" not in published


@pytest.mark.asyncio
async def test_promote_adr_still_accepts_an_optional_dream_run_id() -> None:
    """Kept optional: the orchestrator may name a run, the phase agent may not.

    The scope policy refuses the argument for scoped Dream principals
    (`forbid_dream_run_id`); the parameter exists for the unscoped orchestrator
    path. Removing it here would silently drop that attribution.
    """
    mcp, _ = _make_mcp_with_adr_tools()

    published = await _parameters(mcp, "brain_promote_adr")

    assert published["dream_run_id"].default is None


# ── the behaviour, unchanged, on its new home ────────────────────────────────


@pytest.mark.asyncio
async def test_promote_adr_routes_to_create_with_promotion() -> None:
    mcp, adr_svc = _make_mcp_with_adr_tools()
    adr_svc.create_with_promotion = AsyncMock(return_value=_make_adr())
    promote = await _tool_fn(mcp, "brain_promote_adr")

    reply = await promote(
        title="T",
        context="c",
        decision="d",
        consequences="q",
        project_key="brain-v42",
        source_learning_id=_SOURCE_ID,
    )

    adr_svc.create_with_promotion.assert_awaited_once()
    kwargs = adr_svc.create_with_promotion.await_args.kwargs
    assert str(kwargs["source_learning_id"]) == _SOURCE_ID
    assert kwargs["auto_accept"] is True
    assert kwargs["dream_run_id"] is None
    assert "auto-graduated" in reply


@pytest.mark.asyncio
async def test_promote_adr_rejects_a_malformed_source_learning_id() -> None:
    mcp, adr_svc = _make_mcp_with_adr_tools()
    adr_svc.create_with_promotion = AsyncMock()
    promote = await _tool_fn(mcp, "brain_promote_adr")

    reply = await promote(
        title="T",
        context="c",
        decision="d",
        consequences="q",
        project_key="brain-v42",
        source_learning_id="not-a-uuid",
    )

    assert "Invalid UUID" in reply
    adr_svc.create_with_promotion.assert_not_awaited()


@pytest.mark.asyncio
async def test_promote_adr_translates_duplicate_source_integrity_error() -> None:
    mcp, adr_svc = _make_mcp_with_adr_tools()
    adr_svc.create_with_promotion = AsyncMock(
        side_effect=IntegrityError("dup", None, Exception("dup"))
    )
    promote = await _tool_fn(mcp, "brain_promote_adr")

    reply = await promote(
        title="T",
        context="c",
        decision="d",
        consequences="q",
        project_key="brain-v42",
        source_learning_id=_SOURCE_ID,
    )

    assert "already" in reply


@pytest.mark.asyncio
async def test_propose_adr_still_creates_a_proposed_adr() -> None:
    """Non-regression: the ordinary path is untouched by the split."""
    mcp, adr_svc = _make_mcp_with_adr_tools()
    adr_svc.create = AsyncMock(return_value=_make_adr(status="proposed"))
    propose = await _tool_fn(mcp, "brain_propose_adr")

    reply = await propose(
        title="T",
        context="c",
        decision="d",
        consequences="q",
        project_key="brain-v42",
    )

    adr_svc.create.assert_awaited_once()
    assert "proposed" in reply
