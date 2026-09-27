"""brain_get's compact claim suffix (spec 2026-09-19, section 6.6).

The plan branch of brain_get is untouched: it resolves through
PgIndexedPlanRepo, not one of the domain services this suffix attaches to,
and the plan/spec require it preserved byte-for-byte.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest

from brain_v42.mcp.tools.claim_rendering import CLAIM_SUFFIX_UNAVAILABLE
from brain_v42.mcp.tools.crud_tools import register_crud_tools
from brain_v42.models.claim_read import ClaimRead, VerdictRead, evaluate_claim
from brain_v42.models.decision import Decision
from brain_v42.services.claim_read_service import ClaimReadError
from brain_v42.services.dream_project_scope import DreamProjectScope, bind_dream_project_scope


class MockMCP:
    def __init__(self) -> None:
        self.registered: dict[str, Any] = {}

    def tool(self, **_kwargs: Any) -> Any:
        def decorator(fn: Any) -> Any:
            self.registered[fn.__name__] = fn
            return fn

        return decorator


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


def _build_tools(*, claim_read_svc: Any = None) -> tuple[dict[str, Any], MagicMock]:
    decision_svc = MagicMock()
    decision_svc.get_by_id = AsyncMock()
    mcp = MockMCP()
    register_crud_tools(
        mcp,
        decision_svc=decision_svc,
        learning_svc=MagicMock(),
        snippet_svc=MagicMock(),
        runbook_svc=MagicMock(),
        adr_svc=MagicMock(),
        session_factory=MagicMock(),
        claim_read_svc=claim_read_svc,
    )
    return mcp.registered, decision_svc


def _state(entry_id: Any) -> Any:
    now = datetime(2026, 9, 26, tzinfo=UTC)
    verdict = VerdictRead(
        id=uuid4(),
        seq=1,
        verdict="holds",
        reason=None,
        emitted_at=now,
        recorded_at=now,
        observation_id=uuid4(),
        measurement={},
    )
    claim = ClaimRead(
        id=uuid4(),
        seq=1,
        entry_id=entry_id,
        entity_type="decision",
        project_key="test",
        claim_key="a" * 64,
        statement="Lag is bounded",
        fact_name="lag",
        definition_version=1,
        target="production",
        expected={"path": "/lag", "op": "lte", "value": 5},
        expected_resolved={"path": "/lag", "op": "lte", "value": 5},
        validity_seconds=60,
        provenance="declared",
        declared_by="tester",
        declared_at=now,
        recorded_at=now,
        retired_at=None,
        replaces_id=None,
        latest=verdict,
        conclusive=verdict,
    )
    return evaluate_claim(claim, now)


async def test_brain_get_appends_the_suffix_for_an_active_claim() -> None:
    decision = _make_decision()
    claim_svc = AsyncMock()
    claim_svc.batch_summaries.return_value = {("decision", decision.id): (_state(decision.id),)}
    tools, decision_svc = _build_tools(claim_read_svc=claim_svc)
    decision_svc.get_by_id.return_value = decision

    result = await tools["brain_get"]("decision", str(decision.id))

    assert "[claims : 1 tient]" in result
    claim_svc.batch_summaries.assert_awaited_once_with(
        [("decision", decision.id)], trusted_project_key=None
    )


async def test_brain_get_appends_nothing_for_no_active_claim() -> None:
    decision = _make_decision()
    claim_svc = AsyncMock()
    claim_svc.batch_summaries.return_value = {("decision", decision.id): ()}
    tools, decision_svc = _build_tools(claim_read_svc=claim_svc)
    decision_svc.get_by_id.return_value = decision

    with_claims = await tools["brain_get"]("decision", str(decision.id))

    tools_without, decision_svc_without = _build_tools(claim_read_svc=None)
    decision_svc_without.get_by_id.return_value = decision
    without_claims = await tools_without["brain_get"]("decision", str(decision.id))

    assert with_claims == without_claims


async def test_brain_get_shows_a_visible_marker_when_the_lookup_fails() -> None:
    decision = _make_decision()
    claim_svc = AsyncMock()
    claim_svc.batch_summaries.side_effect = ClaimReadError("read_unavailable")
    tools, decision_svc = _build_tools(claim_read_svc=claim_svc)
    decision_svc.get_by_id.return_value = decision

    result = await tools["brain_get"]("decision", str(decision.id))

    assert decision.title in result
    assert CLAIM_SUFFIX_UNAVAILABLE in result


async def test_brain_get_plan_branch_never_calls_the_claim_lookup() -> None:
    from fastmcp.exceptions import ToolError

    claim_svc = AsyncMock()
    tools, _decision_svc = _build_tools(claim_read_svc=claim_svc)

    with pytest.raises(ToolError, match="Invalid UUID"):
        await tools["brain_get"]("plan", "not-a-uuid")

    claim_svc.batch_summaries.assert_not_called()


async def test_brain_get_threads_the_dream_scope_as_the_trusted_project_key() -> None:
    decision = _make_decision()
    claim_svc = AsyncMock()
    claim_svc.batch_summaries.return_value = {}
    tools, decision_svc = _build_tools(claim_read_svc=claim_svc)
    decision_svc.get_by_id.return_value = decision
    scope = DreamProjectScope(
        project_key="test",
        resolver=MagicMock(),
        audit=MagicMock(),
        tool_name="brain_get",
    )

    with bind_dream_project_scope(scope):
        await tools["brain_get"]("decision", str(decision.id))

    claim_svc.batch_summaries.assert_awaited_once_with(
        [("decision", decision.id)], trusted_project_key="test"
    )


async def test_omitting_claim_read_svc_preserves_brain_get() -> None:
    """The optional dependency default keeps the pre-claims registration seam intact."""
    decision = _make_decision()
    tools, decision_svc = _build_tools()
    decision_svc.get_by_id.return_value = decision

    result = await tools["brain_get"]("decision", str(decision.id))

    assert decision.title in result
    assert "[claims" not in result
