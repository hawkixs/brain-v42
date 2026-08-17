"""RED contract tests for the explicit Brain session domain models."""

from __future__ import annotations

import importlib
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

import pytest
from pydantic import ValidationError


def _symbol(name: str) -> Any:
    """Resolve lazily so missing symbols are test failures, not collection errors."""
    module = importlib.import_module("brain_v42.models.brain_session")
    value = getattr(module, name, None)
    assert value is not None, f"brain_v42.models.brain_session must define {name}"
    return value


def _session(*, status: str = "open") -> Any:
    now = datetime(2026, 7, 13, 10, 0, tzinfo=UTC)
    data: dict[str, Any] = {
        "id": uuid4(),
        "project_key": "brain-v42",
        "client_key": "codex-task-42",
        "status": status,
        "started_focus": "B3 recovery evidence",
        "started_focus_revision": 7,
        "captured_knowledge_ids": [],
        "started_at": now,
        "updated_at": now,
    }
    if status == "ended":
        data.update(
            summary="Session lifecycle specified",
            next_focus="Implement MCP tools",
            captured_knowledge_ids=[uuid4()],
            end_expected_focus_revision=7,
            focus_outcome="applied",
            focus_at_end="Implement MCP tools",
            focus_revision_at_end=8,
            ended_at=now,
        )
    elif status == "abandoned":
        data.update(abandonment_reason="Superseded task", ended_at=now)
    return _symbol("BrainSession").model_validate(data)


@pytest.mark.parametrize(
    "name",
    [
        "BrainSessionStatus",
        "BrainSession",
        "BrainSessionStartResult",
        "BrainSessionResumeResult",
        "BrainSessionEndResult",
        "BrainSessionCaptureResult",
        "BrainSessionHeartbeatResult",
        "BrainSessionAbandonResult",
        "BrainSessionListResult",
    ],
)
def test_public_model_symbol_exists(name: str) -> None:
    assert _symbol(name) is not None


def test_session_models_are_exported_from_models_package() -> None:
    import brain_v42.models as models

    for name in (
        "BrainSessionStatus",
        "BrainSession",
        "BrainSessionStartResult",
        "BrainSessionResumeResult",
        "BrainSessionEndResult",
        "BrainSessionCaptureResult",
        "BrainSessionHeartbeatResult",
        "BrainSessionAbandonResult",
        "BrainSessionListResult",
    ):
        assert getattr(models, name) is _symbol(name)
        assert name in models.__all__


@pytest.mark.parametrize("status", ["open", "ended", "abandoned"])
def test_brain_session_accepts_only_lifecycle_statuses(status: str) -> None:
    session = _session(status=status)
    assert getattr(session.status, "value", session.status) == status


def test_brain_session_rejects_unknown_status() -> None:
    with pytest.raises(ValidationError):
        _session(status="paused")


def test_brain_session_preserves_persistent_identity_and_revision() -> None:
    session = _session()
    assert isinstance(session.id, UUID)
    assert session.started_focus_revision == 7
    assert session.started_at.tzinfo is UTC
    assert session.captured_knowledge_ids == []


def test_start_result_reports_replay_open_count_and_default_briefing() -> None:
    session = _session()
    result = _symbol("BrainSessionStartResult")(
        session=session,
        replayed=False,
        open_session_count=3,
    )
    assert result.session is session
    assert result.replayed is False
    assert result.open_session_count == 3
    assert result.briefing == ""


def test_resume_result_carries_current_focus_snapshot() -> None:
    session = _session()
    result = _symbol("BrainSessionResumeResult")(
        session=session,
        open_session_count=2,
        current_focus="Implement lifecycle",
        current_focus_revision=8,
    )
    assert result.session is session
    assert result.open_session_count == 2
    assert result.current_focus == "Implement lifecycle"
    assert result.current_focus_revision == 8
    assert result.briefing == ""


def test_end_result_reports_atomic_focus_outcome() -> None:
    session = _session(status="ended")
    result = _symbol("BrainSessionEndResult")(
        session=session,
        replayed=False,
        remaining_open_session_count=1,
        current_focus="Implement MCP lifecycle tools",
        current_focus_revision=8,
        focus_outcome="applied",
        focus_at_end="Implement MCP lifecycle tools",
        focus_revision_at_end=8,
    )
    assert result.session is session
    assert result.replayed is False
    assert result.remaining_open_session_count == 1
    assert result.current_focus_revision == 8


def test_end_result_allows_focus_cleared_after_an_idempotent_replay() -> None:
    session = _session(status="ended")

    result = _symbol("BrainSessionEndResult")(
        session=session,
        replayed=True,
        remaining_open_session_count=0,
        current_focus=None,
        current_focus_revision=9,
        focus_outcome="applied",
        focus_at_end="Implement MCP tools",
        focus_revision_at_end=8,
    )

    assert result.current_focus is None


def test_ended_session_rejects_blank_reason_even_when_captures_exist() -> None:
    session = _session(status="ended")
    payload = session.model_dump()
    payload["nothing_to_capture_reason"] = "   "

    with pytest.raises(ValidationError):
        _symbol("BrainSession").model_validate(payload)


def test_ended_session_rejects_more_than_one_hundred_capture_ids() -> None:
    session = _session(status="ended")
    payload = session.model_dump()
    payload["captured_knowledge_ids"] = [uuid4() for _ in range(101)]

    with pytest.raises(ValidationError):
        _symbol("BrainSession").model_validate(payload)


def test_abandon_result_reports_remaining_open_sessions() -> None:
    session = _session(status="abandoned")
    result = _symbol("BrainSessionAbandonResult")(
        session=session,
        replayed=True,
        remaining_open_session_count=0,
    )
    assert result.session is session
    assert result.replayed is True
    assert result.remaining_open_session_count == 0


def test_list_result_is_paginated_and_structured() -> None:
    session = _session()
    result = _symbol("BrainSessionListResult")(sessions=[session], total=1, limit=20, offset=0)
    assert result.sessions == [session]
    assert result.total == 1
    assert result.limit == 20
    assert result.offset == 0


def test_focus_conflict_exposes_current_focus_and_revision() -> None:
    error_type = _symbol("BrainSessionFocusConflictError")

    error = error_type(current_focus="newer focus", current_revision=9)

    assert error.current_focus == "newer focus"
    assert error.current_revision == 9
    assert "9" in str(error)
