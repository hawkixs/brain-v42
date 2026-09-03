"""An abstention must be READABLE by the client, not only by the log reader.

Ticket dfaed283. `brain_session_capture` and `brain_session_heartbeat` both
absorb, and both used to answer with a ledger that says nothing about why it is
empty.

**Why these two tools and not `end`, which the ticket named.** The eight session
tools share a frozen output-schema budget with 35 bytes left
(`tests/unit/mcp/test_session_lifecycle_tool_discovery.py`). Measured on
2026-09-03: the field costs 417 bytes on `BrainSessionEndResult` and 38 even as
a bare 4-character string — it does not fit, on any of the three tools that
DERIVE a schema. `capture` and `heartbeat` are declared `output_schema=None`, so
the field reaches the client through `structuredContent` at ZERO schema cost.
The last test in this module is what keeps that decision from rotting.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest

from brain_v42.models.brain_session import (
    BrainSessionEndResult,
    BrainSessionResumeResult,
    BrainSessionStartResult,
)
from brain_v42.provenance import set_current_transport
from brain_v42.services.brain_session_service import BrainSessionService

_CONNECTION = "aabbccdd11223344556677889900aabb"


@pytest.fixture(autouse=True)
def _connection() -> None:
    set_current_transport(_CONNECTION)
    yield
    set_current_transport(None)


@pytest.fixture
def _open_flag(monkeypatch: pytest.MonkeyPatch) -> None:
    import brain_v42.services.brain_session_service as module

    monkeypatch.setattr(
        module,
        "get_settings",
        lambda: MagicMock(brain_session_derived_capture_enabled=True),
    )


def _repo(outcome: object) -> MagicMock:
    """A repository double whose absorption returns the OUTCOME, not a total."""
    from brain_v42.models.brain_session import BrainSession

    session = MagicMock(spec=BrainSession)
    result = MagicMock()
    result.session = session
    result.absorption = None
    result.model_copy = MagicMock(side_effect=lambda update: _Copied(**update))

    repo = MagicMock()
    for method in ("start", "resume", "capture", "heartbeat", "end"):
        setattr(repo, method, AsyncMock(return_value=result))
    repo.absorb_derived_capture_outcome = AsyncMock(return_value=outcome)
    repo.attributed_knowledge_ids = AsyncMock(return_value=[])
    return repo


class _Copied:
    """What `model_copy(update=...)` handed back, so the field can be read."""

    def __init__(self, **fields: object) -> None:
        self.__dict__.update(fields)


def _outcome(**kwargs: object) -> object:
    from brain_v42.db.session_derived_capture import AbsorptionOutcome

    return AbsorptionOutcome(**kwargs)  # type: ignore[arg-type]


class TestCaptureAndHeartbeatCarryIt:
    async def test_capture_names_the_rival_when_the_window_abstains(self, _open_flag: None) -> None:
        """The ticket's criterion: an empty ledger that SAYS why it is empty."""
        rival, session_id = uuid4(), uuid4()
        repo = _repo(
            _outcome(
                reason="ambiguous",
                rival_artifacts=1,
                rival_sessions=(rival,),
                held_by_tracers=1,
            )
        )

        result = await BrainSessionService(repo).capture(session_id, "task-a", [uuid4()])

        assert result.absorption.outcome == "abstained"
        assert result.absorption.reason == "ambiguous"
        assert result.absorption.rivals == [rival]
        assert result.absorption.held_by_tracers == 1

    async def test_capture_says_absorbed_when_nothing_contested_it(self, _open_flag: None) -> None:
        session_id, moved = uuid4(), uuid4()
        repo = _repo(
            _outcome(
                reason="absorbed",
                moved_by_window=1,
                moved_ids=(moved,),
                held_by_tracers=1,
            )
        )

        result = await BrainSessionService(repo).capture(session_id, "task-a", [uuid4()])

        assert result.absorption.outcome == "absorbed"
        assert result.absorption.rivals == []
        assert result.absorption.held_by_tracers == 1

    async def test_heartbeat_carries_the_very_same_field(self, _open_flag: None) -> None:
        """`heartbeat` absorbs too — a long session reads its state THERE."""
        rival, session_id = uuid4(), uuid4()
        repo = _repo(
            _outcome(
                reason="ambiguous", rival_artifacts=2, rival_sessions=(rival,), held_by_tracers=2
            )
        )

        result = await BrainSessionService(repo).heartbeat(session_id, "task-a")

        assert result.absorption.outcome == "abstained"
        assert result.absorption.rivals == [rival]

    async def test_a_closed_flag_leaves_the_field_absent_rather_than_lying(self) -> None:
        """No absorption attempted ⇒ no verdict. `None`, never a fake `nothing`."""
        session_id = uuid4()
        repo = _repo(_outcome(reason="absorbed"))

        result = await BrainSessionService(repo).heartbeat(session_id, "task-a")

        assert result.absorption is None


class TestTheBudgetDecisionStaysPinned:
    """`end`, `start` and `resume` DERIVE an output schema. The field must not
    land there: measured 2026-09-03, it costs 417 bytes against 35 available.

    This test is the memory of that arbitration. Without it, the next person to
    read the ticket adds the field to `end`, and the budget test goes red with no
    explanation of why the obvious move was refused.
    """

    @pytest.mark.parametrize(
        "model",
        [BrainSessionEndResult, BrainSessionStartResult, BrainSessionResumeResult],
    )
    def test_the_schema_deriving_results_do_not_carry_absorption(self, model: type) -> None:
        assert "absorption" not in model.model_fields
