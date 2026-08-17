"""RED domain contracts for Brain session lifecycle v4."""

from __future__ import annotations

import importlib
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import uuid4

import pytest
from pydantic import ValidationError


def _symbol(name: str) -> Any:
    module = importlib.import_module("brain_v42.models.brain_session")
    value = getattr(module, name, None)
    assert value is not None, f"brain_session models must define {name}"
    return value


def _open_session(**overrides: Any) -> Any:
    now = datetime.now(UTC)
    payload = {
        "id": uuid4(),
        "project_key": "brain-v42",
        "client_key": "task-a",
        "status": "open",
        "started_focus": "old",
        "started_focus_revision": 3,
        "captured_knowledge_ids": [],
        "started_at": now - timedelta(hours=2),
        "last_heartbeat_at": now - timedelta(minutes=2),
        "updated_at": now - timedelta(minutes=2),
        "is_stale": False,
    }
    payload.update(overrides)
    return _symbol("BrainSession").model_validate(payload)


@pytest.mark.parametrize(
    "name",
    [
        "BrainSessionIdentityConflictError",
        "BrainSessionCaptureConflictError",
        "BrainSessionFocusOutcome",
        "BrainSessionCaptureResult",
        "BrainSessionHeartbeatResult",
    ],
)
def test_v4_public_symbols_exist(name: str) -> None:
    assert _symbol(name) is not None


def test_open_session_exposes_heartbeat_and_derived_stale_marker() -> None:
    session = _open_session(is_stale=True)

    assert session.status.value == "open"
    assert session.is_stale is True
    assert session.last_heartbeat_at.tzinfo is not None


def test_terminal_session_cannot_be_marked_stale() -> None:
    now = datetime.now(UTC)

    with pytest.raises(ValidationError, match="stale"):
        _open_session(
            status="ended",
            summary="done",
            next_focus="next",
            captured_knowledge_ids=[uuid4()],
            end_expected_focus_revision=3,
            focus_outcome="applied",
            focus_at_end="next",
            focus_revision_at_end=4,
            ended_at=now,
            is_stale=True,
        )


@pytest.mark.parametrize("outcome", ["applied", "conflict"])
def test_ended_session_persists_focus_outcome(outcome: str) -> None:
    now = datetime.now(UTC)
    session = _open_session(
        status="ended",
        summary="done",
        next_focus="proposal",
        captured_knowledge_ids=[uuid4()],
        end_expected_focus_revision=3,
        focus_outcome=outcome,
        focus_at_end="proposal" if outcome == "applied" else "shared focus",
        focus_revision_at_end=4,
        ended_at=now,
    )

    assert session.focus_outcome.value == outcome
    assert session.end_expected_focus_revision == 3
    assert session.focus_revision_at_end == 4


def test_capture_result_distinguishes_new_and_replayed_attributions() -> None:
    first, second = uuid4(), uuid4()
    result = _symbol("BrainSessionCaptureResult")(
        session=_open_session(),
        captured_knowledge_ids=[first, second],
        newly_captured_knowledge_ids=[second],
        replayed_knowledge_ids=[first],
        replayed=False,
    )

    assert result.newly_captured_knowledge_ids == [second]
    assert result.replayed_knowledge_ids == [first]


def test_heartbeat_result_keeps_session_open_and_fresh() -> None:
    result = _symbol("BrainSessionHeartbeatResult")(session=_open_session())

    assert result.session.status.value == "open"
    assert result.session.is_stale is False


def test_attributed_knowledge_remains_observable_before_and_after_abandon() -> None:
    knowledge_id = uuid4()
    opened = _open_session(attributed_knowledge_ids=[knowledge_id])
    abandoned = _open_session(
        status="abandoned",
        abandonment_reason="work stopped explicitly",
        ended_at=datetime.now(UTC),
        attributed_knowledge_ids=[knowledge_id],
    )

    assert opened.attributed_knowledge_ids == [knowledge_id]
    assert abandoned.attributed_knowledge_ids == [knowledge_id]
    assert abandoned.captured_knowledge_ids == []


@pytest.mark.parametrize(
    ("focus_outcome", "focus_at_end", "focus_revision_at_end"),
    [
        ("applied", "wrong focus", 4),
        ("applied", "proposal", 99),
        ("conflict", "shared focus", 3),
    ],
)
def test_v4_end_rejects_incoherent_persisted_focus_snapshot(
    focus_outcome: str,
    focus_at_end: str,
    focus_revision_at_end: int,
) -> None:
    with pytest.raises(ValidationError, match="focus"):
        _open_session(
            status="ended",
            summary="done",
            next_focus="proposal",
            captured_knowledge_ids=[uuid4()],
            end_expected_focus_revision=3,
            focus_outcome=focus_outcome,
            focus_at_end=focus_at_end,
            focus_revision_at_end=focus_revision_at_end,
            ended_at=datetime.now(UTC),
        )


def test_legacy_end_accepts_only_the_backfilled_applied_shape() -> None:
    with pytest.raises(ValidationError, match="legacy"):
        _open_session(
            status="ended",
            summary="done",
            next_focus="proposal",
            captured_knowledge_ids=[uuid4()],
            end_expected_focus_revision=None,
            focus_outcome="conflict",
            focus_at_end="shared focus",
            focus_revision_at_end=None,
            ended_at=datetime.now(UTC),
        )
