"""Unit tests for brain_log_decision, brain_supersede_decision, brain_get_supersession_chain.

brain_search_decisions has been removed — use brain_search(types=["decision"]) instead.

Uses unittest.mock — no real DB, no real ONNX, no FastMCP server.
FastMCP is mocked as a simple decorator collector.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from pydantic import ValidationError

from brain_v42.mcp.tools.brain_tools import register_tools
from brain_v42.models.decision import Decision, DecisionCreate
from tests.unit.mcp._tool_error_adapter import capture_tool_errors

FIXED_UUID = uuid.UUID("12345678-1234-5678-1234-567812345678")
OLD_UUID = uuid.UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")
NOW = datetime.now(UTC)
FAKE_EMBEDDING = [0.1] * 1536


def _make_decision(id: uuid.UUID | None = None, title: str = "Use PostgreSQL") -> Decision:
    return Decision(
        id=id or FIXED_UUID,
        title=title,
        description="Context: PG\n\nDecision: use it",
        reasoning="pgvector support",
        project_key="brain-v42",
        created_at=NOW,
        updated_at=NOW,
    )


def _make_mcp_and_svc() -> tuple[dict[str, Any], MagicMock]:
    registered: dict[str, Any] = {}

    mock_mcp = MagicMock()

    def tool_decorator(**kwargs: Any) -> Any:
        def decorator(fn: Any) -> Any:
            registered[fn.__name__] = capture_tool_errors(fn)
            return fn

        return decorator

    mock_mcp.tool = tool_decorator

    decision_svc = MagicMock()
    register_tools(
        mock_mcp,
        decision_svc=decision_svc,
        learning_svc=MagicMock(),
        snippet_svc=MagicMock(),
        runbook_svc=MagicMock(),
        adr_svc=MagicMock(),
        project_context_svc=MagicMock(),
        brain_svc=MagicMock(),
    )
    return registered, decision_svc


class TestBrainLogDecision:
    async def test_creates_decision_with_correct_description(self) -> None:
        tools, svc = _make_mcp_and_svc()
        decision = _make_decision()
        svc.create = AsyncMock(return_value=decision)

        await tools["brain_log_decision"](
            title="Use PG",
            context="Need persistence",
            decision_made="PostgreSQL",
            reasoning="pgvector",
            project_key="brain-v42",
        )

        svc.create.assert_awaited_once()
        call_data: DecisionCreate = svc.create.call_args[0][0]
        assert call_data.title == "Use PG"
        assert "Need persistence" in call_data.description
        assert "PostgreSQL" in call_data.description
        assert call_data.reasoning == "pgvector"
        assert call_data.project_key == "brain-v42"

    async def test_returns_confirmation_string(self) -> None:
        tools, svc = _make_mcp_and_svc()
        decision = _make_decision()
        svc.create = AsyncMock(return_value=decision)

        result = await tools["brain_log_decision"](
            title="Use PG", context="ctx", decision_made="PG", reasoning="speed"
        )

        assert isinstance(result, str)
        assert "Decision logged" in result
        assert str(FIXED_UUID) in result

    async def test_defaults_alternatives_and_tags_to_empty_lists(self) -> None:
        tools, svc = _make_mcp_and_svc()
        svc.create = AsyncMock(return_value=_make_decision())

        await tools["brain_log_decision"](title="T", context="C", decision_made="D", reasoning="R")

        data: DecisionCreate = svc.create.call_args[0][0]
        assert data.alternatives == []
        assert data.tags == []


class TestBrainLearnValidation:
    async def test_rejects_invalid_source_type(self) -> None:
        tools, _ = _make_mcp_and_svc()

        with pytest.raises(ValidationError) as exc_info:
            await tools["brain_learn"](
                topic="Test topic",
                insight="Some insight",
                source_type="invalid_type",
            )

        error_text = str(exc_info.value)
        assert "source_type" in error_text
        assert "invalid_type" in error_text

    async def test_rejects_invalid_confidence(self) -> None:
        tools, _ = _make_mcp_and_svc()

        with pytest.raises(ValidationError) as exc_info:
            await tools["brain_learn"](
                topic="Test topic",
                insight="Some insight",
                confidence="very_high",
            )

        error_text = str(exc_info.value)
        assert "confidence" in error_text
        assert "very_high" in error_text

    async def test_accepts_valid_source_type_and_confidence(self) -> None:
        tools, svc = _make_mcp_and_svc()
        from brain_v42.models.learning import Learning

        learning = Learning(
            id=FIXED_UUID,
            topic="Test",
            insight="Insight",
            source_type="bug",
            confidence="high",
            created_at=NOW,
            updated_at=NOW,
        )
        # Re-register with a learning_svc we can control
        registered: dict[str, Any] = {}
        mock_mcp = MagicMock()

        def tool_decorator(**kwargs: Any) -> Any:
            def decorator(fn: Any) -> Any:
                registered[fn.__name__] = fn
                return fn

            return decorator

        mock_mcp.tool = tool_decorator
        learning_svc = MagicMock()
        learning_svc.create = AsyncMock(return_value=learning)

        register_tools(
            mock_mcp,
            decision_svc=MagicMock(),
            learning_svc=learning_svc,
            snippet_svc=MagicMock(),
            runbook_svc=MagicMock(),
            adr_svc=MagicMock(),
            project_context_svc=MagicMock(),
            brain_svc=MagicMock(),
        )

        result = await registered["brain_learn"](
            topic="Test",
            insight="Insight",
            source_type="bug",
            confidence="high",
        )

        assert "Learned" in result
        learning_svc.create.assert_awaited_once()


class TestBrainSupersedeDecision:
    async def test_supersedes_with_correct_data(self) -> None:
        tools, svc = _make_mcp_and_svc()
        new_decision = _make_decision(id=uuid.uuid4(), title="Use pgvector")
        svc.supersede = AsyncMock(return_value=new_decision)

        await tools["brain_supersede_decision"](
            old_decision_id=str(OLD_UUID),
            title="Use pgvector",
            context="Better search",
            decision_made="pgvector extension",
            reasoning="Native PG",
            project_key="brain-v42",
        )

        svc.supersede.assert_awaited_once()
        old_id_arg, data_arg = svc.supersede.call_args[0]
        assert old_id_arg == OLD_UUID
        assert data_arg.title == "Use pgvector"
        assert "Better search" in data_arg.description
        assert "pgvector extension" in data_arg.description
        assert data_arg.reasoning == "Native PG"

    async def test_returns_confirmation_string(self) -> None:
        tools, svc = _make_mcp_and_svc()
        new_id = uuid.uuid4()
        new_decision = _make_decision(id=new_id, title="New")
        svc.supersede = AsyncMock(return_value=new_decision)

        result = await tools["brain_supersede_decision"](
            old_decision_id=str(OLD_UUID),
            title="New",
            context="ctx",
            decision_made="new approach",
            reasoning="better",
        )

        assert isinstance(result, str)
        assert "Decision superseded" in result
        assert str(new_id) in result

    async def test_converts_old_decision_id_string_to_uuid(self) -> None:
        tools, svc = _make_mcp_and_svc()
        svc.supersede = AsyncMock(return_value=_make_decision())

        await tools["brain_supersede_decision"](
            old_decision_id=str(OLD_UUID),
            title="T",
            context="C",
            decision_made="D",
            reasoning="R",
        )

        old_id_arg = svc.supersede.call_args[0][0]
        assert isinstance(old_id_arg, uuid.UUID)
        assert old_id_arg == OLD_UUID


class TestBrainGetSupersessionChain:
    async def test_calls_service_get_supersession_chain(self) -> None:
        tools, svc = _make_mcp_and_svc()
        chain = [_make_decision(title="Old"), _make_decision(id=uuid.uuid4(), title="New")]
        svc.get_supersession_chain = AsyncMock(return_value=chain)

        await tools["brain_get_supersession_chain"](decision_id=str(FIXED_UUID))

        svc.get_supersession_chain.assert_awaited_once_with(FIXED_UUID)

    async def test_returns_formatted_chain_string(self) -> None:
        tools, svc = _make_mcp_and_svc()
        chain = [_make_decision(title="Old"), _make_decision(id=uuid.uuid4(), title="New")]
        svc.get_supersession_chain = AsyncMock(return_value=chain)

        result = await tools["brain_get_supersession_chain"](decision_id=str(FIXED_UUID))

        assert isinstance(result, str)
        assert "Supersession chain" in result
        assert "Old" in result
        assert "New" in result

    async def test_returns_empty_chain_message(self) -> None:
        tools, svc = _make_mcp_and_svc()
        svc.get_supersession_chain = AsyncMock(return_value=[])

        result = await tools["brain_get_supersession_chain"](decision_id=str(FIXED_UUID))

        assert isinstance(result, str)
        assert "No supersession chain found" in result

    async def test_converts_decision_id_to_uuid(self) -> None:
        tools, svc = _make_mcp_and_svc()
        svc.get_supersession_chain = AsyncMock(return_value=[])

        await tools["brain_get_supersession_chain"](decision_id=str(OLD_UUID))

        arg = svc.get_supersession_chain.call_args[0][0]
        assert isinstance(arg, uuid.UUID)
        assert arg == OLD_UUID
