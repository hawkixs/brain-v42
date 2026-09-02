"""The checkpoint is a JUDGMENT object, and the tests say so before the code does.

`SPEC-checkpoint.md` §0: the checkpoint changed nature between its proposal and its
delivery. It was a LIVENESS mechanism refreshing `last_heartbeat_at`; under ADR
§0bis.4 an `agent` session's liveness comes from `last_observed_at`, which moves on
every tool call, so the checkpoint stops being special and goes back to its only job
— semantic freshness (B7).

The consequence is the sharpest line in the spec, and the easiest to undo by accident:
the checkpoint **neither writes nor touches** `last_heartbeat_at`, on a real
checkpoint or on a replay. ADR D4 still says the opposite in prose (§0 flags the
contradiction and asks for an in-place amendment), so an implementer reading D4 first
would wire the effect the spec forbids. That is why the absence of effect is TESTED
here (§5, "two tests of absence of effect"), not merely written down.

Validation lives in the service and is fail-closed everywhere (§2.2): a judgment
truncated at 2000 characters produces a sentence that LOOKS complete and is not, so
overflow raises rather than truncating — deliberately unlike `parse_and_validate`,
which forgivingly clips a `topic` because there a model is producing, not judging.
"""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from brain_v42.models.brain_session import (
    MAX_CHECKPOINT_TEXT,
    MAX_CHECKPOINTS_PER_SESSION,
    BrainSessionCheckpointResult,
    BrainSessionInputError,
)
from brain_v42.services.brain_session_service import BrainSessionService

_SESSION = uuid4()
_KEY = "task-key"


def _result(seq: int = 1, replayed: bool = False, count: int = 1) -> BrainSessionCheckpointResult:
    return BrainSessionCheckpointResult(
        session_id=_SESSION,
        seq=seq,
        created_at=datetime.now(UTC),
        replayed=replayed,
        checkpoint_count=count,
    )


def _service() -> tuple[BrainSessionService, AsyncMock]:
    repo = AsyncMock()
    repo.checkpoint = AsyncMock(return_value=_result())
    return BrainSessionService(repo), repo


async def _checkpoint(service: BrainSessionService, **over: object) -> object:
    payload: dict[str, object] = {
        "session_id": _SESSION,
        "expected_client_key": _KEY,
        "seq": 1,
        "progress": "read the spec",
        "next_step": "write the table",
        "blocker": None,
    }
    payload.update(over)
    return await service.checkpoint(**payload)  # type: ignore[arg-type]


class TestTheJudgmentReachesTheRepositoryIntact:
    @pytest.mark.asyncio
    async def test_the_three_texts_and_the_seq_are_forwarded(self) -> None:
        service, repo = _service()

        await _checkpoint(service, blocker="the shim refuses the bearer")

        kwargs = repo.checkpoint.await_args.kwargs
        assert kwargs["seq"] == 1
        assert kwargs["progress"] == "read the spec"
        assert kwargs["next_step"] == "write the table"
        assert kwargs["blocker"] == "the shim refuses the bearer"

    @pytest.mark.asyncio
    async def test_surrounding_whitespace_is_trimmed_not_rejected(self) -> None:
        """Trimming is not truncation: nothing a reader would have read is lost."""
        service, repo = _service()

        await _checkpoint(service, progress="  done  ", next_step="\tnext\n")

        kwargs = repo.checkpoint.await_args.kwargs
        assert kwargs["progress"] == "done"
        assert kwargs["next_step"] == "next"

    @pytest.mark.asyncio
    async def test_a_blank_blocker_becomes_none_rather_than_an_empty_judgment(self) -> None:
        """`blocker` is optional, and "   " is not a blocker — it is the absence of one."""
        service, repo = _service()

        await _checkpoint(service, blocker="   ")

        assert repo.checkpoint.await_args.kwargs["blocker"] is None


class TestTheBoundsAreFailClosed:
    """§2.2 — every bound raises. None of them truncates."""

    @pytest.mark.asyncio
    @pytest.mark.parametrize("field", ["progress", "next_step"])
    async def test_a_blank_required_text_is_refused(self, field: str) -> None:
        service, repo = _service()

        with pytest.raises(BrainSessionInputError, match=field):
            await _checkpoint(service, **{field: "   "})

        repo.checkpoint.assert_not_awaited()

    @pytest.mark.asyncio
    @pytest.mark.parametrize("field", ["progress", "next_step", "blocker"])
    async def test_an_overlong_text_raises_instead_of_being_truncated(self, field: str) -> None:
        """A judgment clipped at 2000 characters reads as complete and is not."""
        service, repo = _service()

        with pytest.raises(BrainSessionInputError, match=field):
            await _checkpoint(service, **{field: "x" * (MAX_CHECKPOINT_TEXT + 1)})

        repo.checkpoint.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_the_exact_bound_is_accepted(self) -> None:
        """Off-by-one witness: 2000 is allowed, 2001 is not."""
        service, repo = _service()

        await _checkpoint(service, progress="x" * MAX_CHECKPOINT_TEXT)

        repo.checkpoint.assert_awaited_once()

    @pytest.mark.asyncio
    @pytest.mark.parametrize("seq", [0, -1])
    async def test_a_non_positive_seq_is_refused(self, seq: int) -> None:
        service, repo = _service()

        with pytest.raises(BrainSessionInputError, match="seq"):
            await _checkpoint(service, seq=seq)

        repo.checkpoint.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_a_blank_expected_client_key_is_refused(self) -> None:
        service, repo = _service()

        with pytest.raises(BrainSessionInputError):
            await _checkpoint(service, expected_client_key="   ")

        repo.checkpoint.assert_not_awaited()


class TestTheCheckpointIsNotALifecycleCommand:
    """§2.3 — what the tool does NOT do, asserted where it can be asserted cheaply."""

    @pytest.mark.asyncio
    async def test_it_never_asks_the_repository_for_a_heartbeat(self) -> None:
        """ADR D4 still says a real checkpoint refreshes `last_heartbeat_at`; §0bis.4,
        which is later, dissolves that effect. This test is the reason a reader of D4
        cannot quietly wire it back."""
        service, repo = _service()

        await _checkpoint(service)

        repo.heartbeat.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_it_opens_closes_and_abandons_nothing(self) -> None:
        """The covenant holds: the checkpoint is not a lifecycle boundary."""
        service, repo = _service()

        await _checkpoint(service)

        repo.start.assert_not_awaited()
        repo.end.assert_not_awaited()
        repo.abandon.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_it_attributes_no_artifact(self) -> None:
        """The capture ledger stays `brain_session_capture` (§2.3)."""
        service, repo = _service()

        await _checkpoint(service)

        repo.capture.assert_not_awaited()


class TestTheResultCarriesWhatTheCallerNeeds:
    @pytest.mark.asyncio
    async def test_a_replay_is_reported_as_replayed(self) -> None:
        """§1.1 — an exact retry is idempotent by construction, and says so."""
        service, repo = _service()
        repo.checkpoint.return_value = _result(replayed=True, count=3)

        result = await _checkpoint(service)

        assert result.replayed is True
        assert result.checkpoint_count == 3

    def test_the_count_ceiling_is_the_spec_value(self) -> None:
        """200 per SESSION, not per night: under automatic opening a tracer lives at
        most until the sweep, and 200 judgment notes inside one session is already a
        signal in itself (§2.2)."""
        assert MAX_CHECKPOINTS_PER_SESSION == 200
        assert MAX_CHECKPOINT_TEXT == 2000
