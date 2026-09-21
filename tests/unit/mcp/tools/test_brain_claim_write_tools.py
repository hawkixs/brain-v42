"""MCP write-path contracts for declared learning and decision claims."""

from __future__ import annotations

import inspect
from contextlib import AbstractAsyncContextManager
from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock, MagicMock

from brain_v42.mcp.tools import brain_tools
from brain_v42.mcp.tools.brain_tools import register_tools
from brain_v42.models.decision import Decision
from brain_v42.models.learning import Learning
from tests.unit.mcp._tool_error_adapter import capture_tool_errors


class _Transaction(AbstractAsyncContextManager[None]):
    """Record that the claims path owns exactly one explicit transaction."""

    async def __aenter__(self) -> None:
        return None

    async def __aexit__(self, exc_type: object, exc_value: object, traceback: object) -> None:
        return None


class _Session(AbstractAsyncContextManager["_Session"]):
    """Minimal session double for the tool boundary; persistence is tested against PostgreSQL."""

    def __init__(self) -> None:
        self.transactions = 0

    async def __aenter__(self) -> _Session:
        return self

    async def __aexit__(self, exc_type: object, exc_value: object, traceback: object) -> None:
        return None

    def begin(self) -> _Transaction:
        self.transactions += 1
        return _Transaction()


class _SessionFactory:
    """A session factory that exposes an observable no-session regression boundary."""

    def __init__(self) -> None:
        self.calls = 0
        self.session = _Session()

    def __call__(self) -> _Session:
        self.calls += 1
        return self.session


def _learning() -> Learning:
    """Build a complete returned learning without a database or embedding service."""
    return Learning(
        id="12345678-1234-5678-1234-567812345678",
        topic="Atomic claims",
        insight="Claims and the learning share one transaction.",
        source=None,
        source_type="experience",
        confidence="medium",
        project_key="brain-v42",
        tags=[],
        metadata={},
        created_at=datetime(2026, 9, 21, tzinfo=UTC),
        updated_at=datetime(2026, 9, 21, tzinfo=UTC),
        validated_at=None,
        embedding=None,
    )


def _decision() -> Decision:
    """Build a complete returned decision without a database or embedding service."""
    return Decision(
        id="12345678-1234-5678-1234-567812345678",
        title="Atomic claims",
        description="Context: claims\n\nDecision: write atomically",
        reasoning="An entry without its declarations is incomplete.",
        project_key="brain-v42",
        created_at=datetime(2026, 9, 21, tzinfo=UTC),
        updated_at=datetime(2026, 9, 21, tzinfo=UTC),
    )


def _registered_tools(
    *,
    learning_svc: MagicMock | None = None,
    decision_svc: MagicMock | None = None,
    session_factory: _SessionFactory | None = None,
) -> dict[str, Any]:
    """Register the real closures while replacing only database persistence below them."""
    registered: dict[str, Any] = {}
    mcp = MagicMock()

    def tool_decorator(**kwargs: Any) -> Any:
        def decorator(function: Any) -> Any:
            registered[function.__name__] = capture_tool_errors(function)
            return function

        return decorator

    mcp.tool = tool_decorator
    register_tools(
        mcp,
        decision_svc=decision_svc or MagicMock(),
        learning_svc=learning_svc or MagicMock(),
        snippet_svc=MagicMock(),
        runbook_svc=MagicMock(),
        adr_svc=MagicMock(),
        project_context_svc=MagicMock(),
        brain_svc=MagicMock(),
        fact_registry=MagicMock(),
        session_factory=session_factory,
    )
    return registered


async def test_learning_with_claims_uses_one_transaction_then_enriches(
    monkeypatch: Any,
) -> None:
    """Removing the explicit post-commit enrichment would leave claimed learnings unsearchable."""
    learning_svc = MagicMock()
    learning_svc.create = AsyncMock(return_value=_learning())
    learning_svc.enrich_created = AsyncMock(return_value=_learning())
    session_factory = _SessionFactory()
    resolved = [MagicMock(), MagicMock()]
    persist = AsyncMock(return_value=[MagicMock(), MagicMock()])
    monkeypatch.setattr(
        brain_tools, "resolve_claim_inputs", AsyncMock(return_value=resolved), raising=False
    )
    monkeypatch.setattr(brain_tools, "persist_claims", persist, raising=False)
    tools = _registered_tools(learning_svc=learning_svc, session_factory=session_factory)

    response = await tools["brain_learn"](
        topic="Atomic claims",
        insight="Claims and the learning share one transaction.",
        project_key="brain-v42",
        claims=[{"fact_name": "graph_projection_lag"}],
    )

    assert session_factory.calls == 1
    assert session_factory.session.transactions == 1
    learning_svc.create.assert_awaited_once_with(
        learning_svc.create.call_args.args[0], session=session_factory.session
    )
    persist.assert_awaited_once()
    learning_svc.enrich_created.assert_awaited_once()
    assert "claims:2 recorded (declared; verification arrives with the verdict path)" in response


async def test_decision_with_claims_uses_one_transaction_then_enriches(
    monkeypatch: Any,
) -> None:
    """Removing the explicit post-commit enrichment would leave claimed decisions unsearchable."""
    decision_svc = MagicMock()
    decision_svc.create = AsyncMock(return_value=_decision())
    decision_svc.enrich_created = AsyncMock(return_value=_decision())
    session_factory = _SessionFactory()
    persist = AsyncMock(return_value=[MagicMock()])
    monkeypatch.setattr(
        brain_tools, "resolve_claim_inputs", AsyncMock(return_value=[MagicMock()]), raising=False
    )
    monkeypatch.setattr(brain_tools, "persist_claims", persist, raising=False)
    tools = _registered_tools(decision_svc=decision_svc, session_factory=session_factory)

    response = await tools["brain_log_decision"](
        title="Atomic claims",
        context="Claims must commit with the decision.",
        decision_made="Use a shared transaction.",
        reasoning="The declaration cannot outlive a failed entry write.",
        project_key="brain-v42",
        claims=[{"fact_name": "graph_projection_lag"}],
    )

    assert session_factory.calls == 1
    assert session_factory.session.transactions == 1
    decision_svc.create.assert_awaited_once_with(
        decision_svc.create.call_args.args[0], session=session_factory.session
    )
    persist.assert_awaited_once()
    decision_svc.enrich_created.assert_awaited_once()
    assert "claims:1 recorded (declared; verification arrives with the verdict path)" in response


async def test_claims_none_keeps_learning_and_decision_on_the_existing_path() -> None:
    """An absent claims field must not acquire a transaction or alter writer call arguments."""
    learning_svc = MagicMock()
    learning_svc.create = AsyncMock(return_value=_learning())
    decision_svc = MagicMock()
    decision_svc.create = AsyncMock(return_value=_decision())
    session_factory = _SessionFactory()
    tools = _registered_tools(
        learning_svc=learning_svc,
        decision_svc=decision_svc,
        session_factory=session_factory,
    )

    await tools["brain_learn"](
        topic="Atomic claims",
        insight="No declarations supplied.",
        project_key="brain-v42",
        claims=None,
    )
    await tools["brain_log_decision"](
        title="Atomic claims",
        context="No declarations supplied.",
        decision_made="Keep the existing path.",
        reasoning="No transaction is needed.",
        project_key="brain-v42",
        claims=None,
    )

    assert session_factory.calls == 0
    assert learning_svc.create.call_args.kwargs == {"related_to": None}
    assert decision_svc.create.call_args.kwargs == {"related_to": None}


async def test_other_writers_do_not_expose_claims_yet() -> None:
    """B2-T9b must deliberately widen the three writers that cannot yet commit atomically."""
    tools = _registered_tools()

    for name in ("brain_propose_adr", "brain_create_runbook", "brain_save_snippet"):
        assert "claims" not in inspect.signature(tools[name]).parameters
