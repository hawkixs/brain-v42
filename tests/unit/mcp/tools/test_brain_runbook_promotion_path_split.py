"""The runbook promotion path is a TOOL, not two optional kwargs on the creation.

Ticket c07957ea, the twin of af3b58dd item 2. `brain_propose_adr` published three
Dream-only parameters and refused their meaningless combinations by name; lot A
turned that refusal into a schema by giving the promotion its own tool
(`brain_promote_adr`, decision e0278659).

`brain_create_runbook` carries the same Dream pair — `source_learning_id` and
`dream_run_id` — and carries NO guard at all. Its promotion branch is

    if source_learning_id is not None:      # runbook_tools.py:87

so a call naming `dream_run_id` alone falls into the standard path, which never
reads it, and returns a CONFIRMATION. The caller believes they attributed a
promotion to a run; nothing recorded anything. That is exactly the bug af3b58dd
named for the ADR tool, still open on its twin, and named by neither the ticket
nor the brief that opened it.

The server-side refusal does not cover it: `forbid_dream_run_id` in the scope
policy only bites for a SCOPED Dream principal, and the orchestrator path is
unscoped.

Same shape as A, for the same reasons: two paths publish two schemas, so the
request that means nothing can no longer be built (learning c34fb865 — a runtime
refusal proves the server says no, an absent parameter proves the request cannot
exist), and the refusal is asserted by its named cause rather than a return code
(learning e2e5a550).
"""

from __future__ import annotations

import inspect
from datetime import datetime
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest
from fastmcp import FastMCP

from brain_v42.mcp.tools.runbook_tools import register_runbook_tools
from brain_v42.models.runbook import Runbook, RunbookStep
from tests.unit.mcp._tool_error_adapter import capture_tool_errors

_SOURCE_ID = "11111111-1111-1111-1111-111111111111"

#: The parameters that belong to the promotion path and nowhere else.
_DREAM_ONLY_PARAMETERS = ("source_learning_id", "dream_run_id")


def _make_runbook(**overrides: object) -> Runbook:
    payload: dict = {
        "id": uuid4(),
        "title": "T",
        "description": "d",
        "project_key": "brain-v42",
        "trigger": "t",
        "steps": [RunbookStep(order=1, title="s")],
        "prerequisites": [],
        "rollback_steps": [],
        "estimated_duration": None,
        "tags": [],
        "metadata": {},
        "execution_count": 0,
        "last_executed_at": None,
        "last_execution_status": None,
        "embedding": None,
        "created_at": datetime(2026, 4, 18),
        "updated_at": datetime(2026, 4, 18),
    }
    payload.update(overrides)
    return Runbook.model_validate(payload)


def _make_mcp() -> tuple[FastMCP, MagicMock]:
    mcp = FastMCP("test-runbook")
    svc = MagicMock()
    svc.create = AsyncMock(return_value=_make_runbook())
    svc.create_with_promotion = AsyncMock(return_value=_make_runbook())
    register_runbook_tools(mcp, svc)
    return mcp, svc


async def _tool_fn(mcp: FastMCP, name: str):  # noqa: ANN202 - FastMCP tool callable
    tool = await mcp.get_tool(name)
    assert tool is not None, f"{name} must be registered"
    return capture_tool_errors(tool.fn)


async def _parameters(mcp: FastMCP, name: str) -> dict[str, inspect.Parameter]:
    tool = await mcp.get_tool(name)
    assert tool is not None, f"{name} must be registered"
    return dict(inspect.signature(tool.fn).parameters)


# ── the bug, stated as a test before it is fixed ─────────────────────────────


@pytest.mark.asyncio
async def test_a_lone_dream_run_id_is_not_swallowed() -> None:
    """TODAY this returns a confirmation and records nothing.

    The standard branch never reads `dream_run_id`, so the caller is told the
    runbook was created — which is true — while the attribution they asked for
    silently did not happen. After the split the parameter does not exist on
    this tool at all, so the same call fails loudly with an unknown-parameter
    error instead of succeeding on a lie.
    """
    mcp, svc = _make_mcp()

    published = await _parameters(mcp, "brain_create_runbook")

    assert [name for name in _DREAM_ONLY_PARAMETERS if name in published] == []


@pytest.mark.asyncio
async def test_promote_runbook_requires_its_source_learning() -> None:
    mcp, _ = _make_mcp()

    published = await _parameters(mcp, "brain_promote_runbook")

    assert published["source_learning_id"].default is inspect.Parameter.empty


@pytest.mark.asyncio
async def test_promote_runbook_still_accepts_an_optional_dream_run_id() -> None:
    """Kept optional: the orchestrator may name a run, the phase agent may not."""
    mcp, _ = _make_mcp()

    published = await _parameters(mcp, "brain_promote_runbook")

    assert published["dream_run_id"].default is None


@pytest.mark.asyncio
async def test_promote_runbook_routes_to_create_with_promotion() -> None:
    mcp, svc = _make_mcp()
    promote = await _tool_fn(mcp, "brain_promote_runbook")

    await promote(
        title="T",
        description="d",
        project_key="brain-v42",
        trigger="t",
        steps=[{"title": "s"}],
        source_learning_id=_SOURCE_ID,
    )

    svc.create_with_promotion.assert_awaited_once()
    kwargs = svc.create_with_promotion.await_args.kwargs
    assert str(kwargs["source_learning_id"]) == _SOURCE_ID
    assert kwargs["dream_run_id"] is None


@pytest.mark.asyncio
async def test_create_runbook_still_creates_an_ordinary_runbook() -> None:
    """Non-regression: the path most callers use is untouched by the split."""
    mcp, svc = _make_mcp()
    create = await _tool_fn(mcp, "brain_create_runbook")

    await create(
        title="T",
        description="d",
        project_key="brain-v42",
        trigger="t",
        steps=[{"title": "s"}],
    )

    svc.create.assert_awaited_once()
    svc.create_with_promotion.assert_not_awaited()
