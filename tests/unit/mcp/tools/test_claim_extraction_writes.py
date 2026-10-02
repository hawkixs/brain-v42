"""Write-path contracts for deterministic, server-owned claim extraction.

The five ordinary knowledge writers must, when extraction is armed and a
project key is present, create their entry and its automatic claim in ONE
caller-owned transaction; when extraction is off they must stay byte-for-byte
on their pre-extraction path. These are the PR 3 gates for tasks 1 and 2.
"""

from __future__ import annotations

from contextlib import AbstractAsyncContextManager
from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock, MagicMock
from uuid import UUID

import pytest

from brain_v42.facts.claim_extractor import ClaimCandidate, ExtractionResult
from brain_v42.mcp.tools import brain_tools, claim_writes, runbook_tools, snippet_tools
from brain_v42.mcp.tools.brain_tools import register_tools
from brain_v42.mcp.tools.claim_writes import ClaimWriteOutcome
from brain_v42.mcp.tools.crud_tools import register_crud_tools
from brain_v42.models.decision import Decision
from brain_v42.models.learning import Learning
from tests.unit.mcp._tool_error_adapter import capture_tool_errors

EXTRACTED_ID = UUID("7d0b1f53-4c55-4c2e-9e57-a5b1b8d0c003")

_CANDIDATE = ClaimCandidate(
    statement="The production Alembic head is 058.",
    fact_name="alembic_head",
    expected={"path": "/revision", "op": "eq", "value": "058"},
)


class _Transaction(AbstractAsyncContextManager[None]):
    async def __aenter__(self) -> None:
        return None

    async def __aexit__(self, exc_type: object, exc_value: object, traceback: object) -> None:
        return None


class _Session(AbstractAsyncContextManager["_Session"]):
    """Minimal session double; persistence is tested against PostgreSQL."""

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
    """A session factory exposing an observable no-session regression boundary."""

    def __init__(self) -> None:
        self.calls = 0
        self.session = _Session()

    def __call__(self) -> _Session:
        self.calls += 1
        return self.session


def _learning() -> Learning:
    return Learning(
        id="12345678-1234-5678-1234-567812345678",
        topic="Extracted claims",
        insight="The production Alembic head is 058.",
        source=None,
        source_type="experience",
        confidence="medium",
        project_key="brain-v42",
        tags=[],
        metadata={},
        created_at=datetime(2026, 9, 30, tzinfo=UTC),
        updated_at=datetime(2026, 9, 30, tzinfo=UTC),
        validated_at=None,
        embedding=None,
    )


def _decision() -> Decision:
    return Decision(
        id="12345678-1234-5678-1234-567812345678",
        title="Extracted claims",
        description="Context: claims\n\nDecision: write atomically",
        reasoning="The production Alembic head is 058.",
        project_key="brain-v42",
        created_at=datetime(2026, 9, 30, tzinfo=UTC),
        updated_at=datetime(2026, 9, 30, tzinfo=UTC),
    )


def _adr() -> Any:
    return MagicMock(id=UUID("12345678-1234-5678-1234-567812345678"), number=7)


def _runbook() -> Any:
    return MagicMock(
        id=UUID("12345678-1234-5678-1234-567812345678"),
        title="Extracted claims",
        steps=[MagicMock()],
    )


def _snippet() -> Any:
    return MagicMock(
        id=UUID("12345678-1234-5678-1234-567812345678"),
        title="Extracted claims",
        language="python",
    )


def _register(*, extraction_enabled: bool, session_factory: Any, **services: Any) -> dict[str, Any]:
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
        decision_svc=services.get("decision_svc", MagicMock()),
        learning_svc=services.get("learning_svc", MagicMock()),
        snippet_svc=services.get("snippet_svc", MagicMock()),
        runbook_svc=services.get("runbook_svc", MagicMock()),
        adr_svc=services.get("adr_svc", MagicMock()),
        project_context_svc=MagicMock(),
        brain_svc=MagicMock(),
        fact_registry=MagicMock(),
        session_factory=session_factory,
        extraction_enabled=extraction_enabled,
    )
    register_crud_tools(
        mcp,
        decision_svc=services.get("decision_svc", MagicMock()),
        learning_svc=services.get("learning_svc", MagicMock()),
        snippet_svc=services.get("snippet_svc", MagicMock()),
        runbook_svc=services.get("runbook_svc", MagicMock()),
        adr_svc=services.get("adr_svc", MagicMock()),
        session_factory=session_factory,
        fact_registry=MagicMock(),
    )
    return registered


def _mock_services() -> dict[str, Any]:
    """Five services whose create/enrich round-trips return a typed entity."""

    def svc(entity: Any) -> MagicMock:
        mock = MagicMock()
        mock.create = AsyncMock(return_value=entity)
        mock.enrich_created = AsyncMock(return_value=entity)
        mock.update = AsyncMock(return_value=entity)
        return mock

    decision_svc = svc(_decision())
    decision_svc.supersede = AsyncMock(return_value=_decision())
    adr_svc = svc(_adr())
    adr_svc.create_with_promotion = AsyncMock(return_value=_adr())

    return {
        "learning_svc": svc(_learning()),
        "decision_svc": decision_svc,
        "adr_svc": adr_svc,
        "snippet_svc": svc(_snippet()),
        "runbook_svc": svc(_runbook()),
    }


async def test_extraction_disabled_preserves_write_paths(monkeypatch: pytest.MonkeyPatch) -> None:
    """With the flag off, no writer calls the extractor and no-claims writes open no session."""
    extract_calls: list[str] = []

    def fake_extract(*args: object, **kwargs: object) -> ExtractionResult:
        extract_calls.append(str(kwargs.get("entity", "")))
        return ExtractionResult((), frozenset())

    monkeypatch.setattr(claim_writes, "extract_candidates", fake_extract)
    monkeypatch.setattr(brain_tools, "persist_extracted_claims", AsyncMock(), raising=False)
    monkeypatch.setattr(snippet_tools, "persist_extracted_claims", AsyncMock(), raising=False)
    monkeypatch.setattr(runbook_tools, "persist_extracted_claims", AsyncMock(), raising=False)

    services = _mock_services()
    session_factory = _SessionFactory()
    tools = _register(extraction_enabled=False, session_factory=session_factory, **services)

    await tools["brain_learn"](
        topic="Extracted claims",
        insight="The production Alembic head is 058.",
        project_key="brain-v42",
    )
    await tools["brain_log_decision"](
        title="Extracted claims",
        context="Claims.",
        decision_made="Write atomically.",
        reasoning="The production Alembic head is 058.",
        project_key="brain-v42",
    )
    await tools["brain_propose_adr"](
        title="Extracted claims",
        context="Claims.",
        decision="Write atomically.",
        consequences="None.",
        project_key="brain-v42",
    )
    await tools["brain_save_snippet"](
        title="Extracted claims",
        intention="The production Alembic head is 058.",
        code="pass",
        language="python",
        project_key="brain-v42",
    )
    await tools["brain_create_runbook"](
        title="Extracted claims",
        description="The production Alembic head is 058.",
        project_key="brain-v42",
        trigger="A write occurs.",
        steps=[{"title": "Observe"}],
    )
    await tools["brain_supersede_decision"](
        old_decision_id="12345678-1234-5678-1234-567812345678",
        title="Extracted claims",
        context="Claims.",
        decision_made="Write atomically.",
        reasoning="The production Alembic head is 058.",
    )
    await tools["brain_promote_adr"](
        title="Extracted claims",
        context="Claims.",
        decision="Write atomically.",
        consequences="None.",
        project_key="brain-v42",
        source_learning_id="12345678-1234-5678-1234-567812345678",
    )
    await tools["brain_update"](
        entity_type="learning",
        entity_id="12345678-1234-5678-1234-567812345678",
        fields={"insight": "No extraction while off."},
    )

    assert extract_calls == []
    assert session_factory.calls == 0
    # The two special creates keep their own private transactions while off.
    assert services["decision_svc"].supersede.call_args.kwargs == {}
    assert "session" not in services["adr_svc"].create_with_promotion.call_args.kwargs


async def test_five_ordinary_writers_extract_without_claims_argument(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Each of the five ordinary writers extracts from selected prose and commits atomically.

    The entry and its automatic claim share one caller-owned transaction, the
    claim persist runs after the entry create and before enrichment, and the
    extractor only ever reads assertion-bearing fields. An unscoped write
    extracts nothing.
    """
    extract_calls: list[tuple[str, str | None, dict[str, str | None]]] = []

    def fake_extract(
        *, entity: str, project_key: str | None, fields: dict[str, str | None]
    ) -> ExtractionResult:
        extract_calls.append((entity, project_key, fields))
        if project_key is None:
            return ExtractionResult((), frozenset({"no_project_key"}))
        return ExtractionResult((_CANDIDATE,), frozenset())

    monkeypatch.setattr(claim_writes, "extract_candidates", fake_extract)

    persist_calls: list[tuple[str, object, str | None, UUID]] = []

    async def fake_persist(
        session: object,
        registry: object,
        *,
        entry_id: UUID,
        entity_type: str,
        project_key: str,
        candidates: object,
    ) -> list[ClaimWriteOutcome]:
        persist_calls.append((entity_type, candidates, project_key, entry_id))
        return [ClaimWriteOutcome(claim_id=EXTRACTED_ID, provenance="extracted", detail=None)]

    monkeypatch.setattr(brain_tools, "persist_extracted_claims", fake_persist, raising=False)
    monkeypatch.setattr(snippet_tools, "persist_extracted_claims", fake_persist, raising=False)
    monkeypatch.setattr(runbook_tools, "persist_extracted_claims", fake_persist, raising=False)

    services = _mock_services()
    session_factory = _SessionFactory()
    tools = _register(extraction_enabled=True, session_factory=session_factory, **services)

    await tools["brain_learn"](
        topic="Extracted claims",
        insight="The production Alembic head is 058.",
        project_key="brain-v42",
    )
    await tools["brain_log_decision"](
        title="Extracted claims",
        context="Claims.",
        decision_made="Write atomically.",
        reasoning="The production Alembic head is 058.",
        project_key="brain-v42",
    )
    await tools["brain_propose_adr"](
        title="Extracted claims",
        context="The production Alembic head is 058.",
        decision="Write atomically.",
        consequences="None.",
        project_key="brain-v42",
    )
    await tools["brain_save_snippet"](
        title="Extracted claims",
        intention="The production Alembic head is 058.",
        code="def production_schema(): return '058'",
        language="python",
        usage_example="The production Alembic head is 999.",
        gotchas="Use it.",
        project_key="brain-v42",
    )
    await tools["brain_create_runbook"](
        title="Extracted claims",
        description="The production Alembic head is 058.",
        project_key="brain-v42",
        trigger="A write occurs.",
        steps=[{"title": "Observe", "command": "echo 058"}],
    )

    assert session_factory.calls == 5
    assert {entity for entity, _, _ in extract_calls} == {
        "learning",
        "decision",
        "adr",
        "snippet",
        "runbook",
    }
    assert {entity for entity, _, _, _ in persist_calls} == {
        "learning",
        "decision",
        "adr",
        "snippet",
        "runbook",
    }
    for _entity, candidates, project_key, entry_id in persist_calls:
        assert candidates == (_CANDIDATE,)
        assert project_key == "brain-v42"
        assert entry_id == UUID("12345678-1234-5678-1234-567812345678")

    # Excluded fields never reach the extractor: snippet contributes only its
    # two assertion-bearing fields, code/usage/title/command never do.
    snippet_fields = next(fields for entity, _, fields in extract_calls if entity == "snippet")
    assert set(snippet_fields) == {"intention", "gotchas"}
    runbook_fields = next(fields for entity, _, fields in extract_calls if entity == "runbook")
    assert set(runbook_fields) == {"description", "trigger"}

    for name in ("learning_svc", "decision_svc", "adr_svc", "snippet_svc", "runbook_svc"):
        mock = services[name]
        mock.create.assert_awaited_once()
        assert mock.create.call_args.kwargs.get("session") is session_factory.session
        mock.enrich_created.assert_awaited_once()

    # An unscoped write extracts nothing and persists no automatic claim.
    before = len(persist_calls)
    await tools["brain_learn"](
        topic="Extracted claims",
        insight="The production Alembic head is 058.",
        project_key=None,
    )
    assert extract_calls[-1][1] is None
    assert len(persist_calls) == before
