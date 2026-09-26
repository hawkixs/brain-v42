"""MCP write-path contracts for declared learning and decision claims."""

from __future__ import annotations

import inspect
from contextlib import AbstractAsyncContextManager
from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock, MagicMock
from uuid import UUID

import pytest

from brain_v42.mcp.tools import brain_tools, runbook_tools, snippet_tools
from brain_v42.mcp.tools.brain_tools import register_tools
from brain_v42.mcp.tools.claim_writes import ClaimWriteOutcome
from brain_v42.models.decision import Decision
from brain_v42.models.learning import Learning
from tests.unit.mcp._tool_error_adapter import capture_tool_errors

#: Occurrence ids the fake persistence returns: a writer must hand them back in
#: full, because `brain_claim_verify` names a claim by its canonical UUID alone.
CLAIM_A = UUID("7d0b1f53-4c55-4c2e-9e57-a5b1b8d0c001")
CLAIM_B = UUID("7d0b1f53-4c55-4c2e-9e57-a5b1b8d0c002")


def _declared(*ids: UUID) -> list[ClaimWriteOutcome]:
    """The pre-measurement outcome shape: `measure` was never requested for these claims."""
    return [ClaimWriteOutcome(claim_id=cid, provenance="declared", detail=None) for cid in ids]


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


def _adr() -> Any:
    """Build the ADR fields consumed by the real confirmation formatter."""
    return MagicMock(
        id=UUID("12345678-1234-5678-1234-567812345678"),
        number=7,
    )


def _runbook() -> Any:
    """Build the runbook fields consumed by the real confirmation formatter."""
    return MagicMock(
        id=UUID("12345678-1234-5678-1234-567812345678"),
        title="Atomic runbook",
        steps=[MagicMock()],
    )


def _snippet() -> Any:
    """Build the snippet fields consumed by the real confirmation formatter."""
    return MagicMock(
        id=UUID("12345678-1234-5678-1234-567812345678"),
        title="Atomic snippet",
        language="python",
    )


def _registered_tools(
    *,
    learning_svc: MagicMock | None = None,
    decision_svc: MagicMock | None = None,
    adr_svc: MagicMock | None = None,
    runbook_svc: MagicMock | None = None,
    snippet_svc: MagicMock | None = None,
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
        snippet_svc=snippet_svc or MagicMock(),
        runbook_svc=runbook_svc or MagicMock(),
        adr_svc=adr_svc or MagicMock(),
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
    persist = AsyncMock(return_value=_declared(CLAIM_A, CLAIM_B))
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
    assert (
        f"claims:2 recorded as declared [{CLAIM_A} {CLAIM_B}]; brain_claim_verify measures one"
        in response
    )


async def test_decision_with_claims_uses_one_transaction_then_enriches(
    monkeypatch: Any,
) -> None:
    """Removing the explicit post-commit enrichment would leave claimed decisions unsearchable."""
    decision_svc = MagicMock()
    decision_svc.create = AsyncMock(return_value=_decision())
    decision_svc.enrich_created = AsyncMock(return_value=_decision())
    session_factory = _SessionFactory()
    persist = AsyncMock(return_value=_declared(CLAIM_A))
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
    assert f"claims:1 recorded as declared [{CLAIM_A}]; brain_claim_verify measures one" in response


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


@pytest.mark.parametrize(
    "name",
    (
        "brain_log_decision",
        "brain_learn",
        "brain_propose_adr",
        "brain_create_runbook",
        "brain_save_snippet",
    ),
)
async def test_all_five_writers_expose_claims(name: str) -> None:
    """Removing claims from any writer would make declared facts silently inconsistent."""
    tools = _registered_tools()

    assert "claims" in inspect.signature(tools[name]).parameters


async def test_claims_none_keeps_adr_on_the_existing_path() -> None:
    """Adding declarations must not make normal ADR writes open a transaction."""
    adr_svc = MagicMock()
    adr_svc.create = AsyncMock(return_value=_adr())
    adr_svc.enrich_created = AsyncMock(return_value=_adr())
    session_factory = _SessionFactory()
    tools = _registered_tools(adr_svc=adr_svc, session_factory=session_factory)

    await tools["brain_propose_adr"](
        title="Atomic claims",
        context="No declarations supplied.",
        decision="Keep the existing path.",
        consequences="No transaction is needed.",
        project_key="brain-v42",
        claims=None,
    )

    assert session_factory.calls == 0
    adr_svc.create.assert_awaited_once()
    assert adr_svc.create.call_args.kwargs == {}
    adr_svc.enrich_created.assert_not_awaited()


async def test_claims_none_keeps_runbook_on_the_existing_path() -> None:
    """Adding declarations must not make normal runbook writes open a transaction."""
    runbook_svc = MagicMock()
    runbook_svc.create = AsyncMock(return_value=_runbook())
    runbook_svc.enrich_created = AsyncMock(return_value=_runbook())
    session_factory = _SessionFactory()
    tools = _registered_tools(runbook_svc=runbook_svc, session_factory=session_factory)

    await tools["brain_create_runbook"](
        title="Atomic claims",
        description="No declarations supplied.",
        project_key="brain-v42",
        trigger="A write occurs.",
        steps=[{"title": "Observe"}],
        claims=None,
    )

    assert session_factory.calls == 0
    runbook_svc.create.assert_awaited_once()
    assert runbook_svc.create.call_args.kwargs == {}
    runbook_svc.enrich_created.assert_not_awaited()


async def test_claims_none_keeps_snippet_on_the_existing_path() -> None:
    """Adding declarations must not make normal snippet writes open a transaction."""
    snippet_svc = MagicMock()
    snippet_svc.create = AsyncMock(return_value=_snippet())
    snippet_svc.enrich_created = AsyncMock(return_value=_snippet())
    session_factory = _SessionFactory()
    tools = _registered_tools(snippet_svc=snippet_svc, session_factory=session_factory)

    await tools["brain_save_snippet"](
        title="Atomic claims",
        intention="No declarations supplied.",
        code="pass",
        language="python",
        project_key="brain-v42",
        claims=None,
    )

    assert session_factory.calls == 0
    snippet_svc.create.assert_awaited_once()
    assert snippet_svc.create.call_args.kwargs == {"related_to": None}
    snippet_svc.enrich_created.assert_not_awaited()


async def test_adr_with_claims_uses_one_transaction_then_enriches(
    monkeypatch: Any,
) -> None:
    """Skipping ADR enrichment after commit would leave declared ADRs unsearchable."""
    adr_svc = MagicMock()
    adr_svc.create = AsyncMock(return_value=_adr())
    adr_svc.enrich_created = AsyncMock(return_value=_adr())
    session_factory = _SessionFactory()
    persist = AsyncMock(return_value=_declared(CLAIM_A))
    monkeypatch.setattr(
        brain_tools, "resolve_claim_inputs", AsyncMock(return_value=[MagicMock()]), raising=False
    )
    monkeypatch.setattr(brain_tools, "persist_claims", persist, raising=False)
    tools = _registered_tools(adr_svc=adr_svc, session_factory=session_factory)

    response = await tools["brain_propose_adr"](
        title="Atomic claims",
        context="Claims commit with the ADR.",
        decision="Use one transaction.",
        consequences="No partial declarations.",
        project_key="brain-v42",
        claims=[{"fact_name": "graph_projection_lag"}],
    )

    assert session_factory.calls == 1
    assert session_factory.session.transactions == 1
    adr_svc.create.assert_awaited_once_with(
        adr_svc.create.call_args.args[0], session=session_factory.session
    )
    persist.assert_awaited_once()
    adr_svc.enrich_created.assert_awaited_once()
    assert f"claims:1 recorded as declared [{CLAIM_A}]; brain_claim_verify measures one" in response


async def test_runbook_with_claims_uses_one_transaction_then_enriches(
    monkeypatch: Any,
) -> None:
    """Skipping runbook enrichment after commit would leave declared runbooks unsearchable."""
    runbook_svc = MagicMock()
    runbook_svc.create = AsyncMock(return_value=_runbook())
    runbook_svc.enrich_created = AsyncMock(return_value=_runbook())
    session_factory = _SessionFactory()
    persist = AsyncMock(return_value=_declared(CLAIM_A, CLAIM_B))
    monkeypatch.setattr(
        runbook_tools,
        "resolve_claim_inputs",
        AsyncMock(return_value=[MagicMock(), MagicMock()]),
        raising=False,
    )
    monkeypatch.setattr(runbook_tools, "persist_claims", persist, raising=False)
    tools = _registered_tools(runbook_svc=runbook_svc, session_factory=session_factory)

    response = await tools["brain_create_runbook"](
        title="Atomic claims",
        description="Claims commit with the runbook.",
        project_key="brain-v42",
        trigger="A write occurs.",
        steps=[{"title": "Observe"}],
        claims=[{"fact_name": "graph_projection_lag"}],
    )

    assert session_factory.calls == 1
    assert session_factory.session.transactions == 1
    runbook_svc.create.assert_awaited_once_with(
        runbook_svc.create.call_args.args[0], session=session_factory.session
    )
    persist.assert_awaited_once()
    runbook_svc.enrich_created.assert_awaited_once()
    assert (
        f"claims:2 recorded as declared [{CLAIM_A} {CLAIM_B}]; brain_claim_verify measures one"
        in response
    )


async def test_snippet_with_claims_uses_one_transaction_then_enriches(
    monkeypatch: Any,
) -> None:
    """Skipping snippet enrichment after commit would leave declared snippets unsearchable."""
    snippet_svc = MagicMock()
    snippet_svc.create = AsyncMock(return_value=_snippet())
    snippet_svc.enrich_created = AsyncMock(return_value=_snippet())
    session_factory = _SessionFactory()
    persist = AsyncMock(return_value=_declared(CLAIM_A))
    monkeypatch.setattr(
        snippet_tools, "resolve_claim_inputs", AsyncMock(return_value=[MagicMock()]), raising=False
    )
    monkeypatch.setattr(snippet_tools, "persist_claims", persist, raising=False)
    tools = _registered_tools(snippet_svc=snippet_svc, session_factory=session_factory)

    response = await tools["brain_save_snippet"](
        title="Atomic claims",
        intention="Claims commit with the snippet.",
        code="pass",
        language="python",
        project_key="brain-v42",
        claims=[{"fact_name": "graph_projection_lag"}],
    )

    assert session_factory.calls == 1
    assert session_factory.session.transactions == 1
    snippet_svc.create.assert_awaited_once_with(
        snippet_svc.create.call_args.args[0], session=session_factory.session
    )
    persist.assert_awaited_once()
    snippet_svc.enrich_created.assert_awaited_once()
    assert f"claims:1 recorded as declared [{CLAIM_A}]; brain_claim_verify measures one" in response
