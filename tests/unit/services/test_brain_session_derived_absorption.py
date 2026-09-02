"""Wiring of ABSORPTION onto the explicit session commands.

The tracer collects; the user's session absorbs. This module pins WHEN it
absorbs — on every command, once, and at the latest at `end` — and above all what
it does not do when the flag is closed: **zero extra repository call**, not "a
call that does nothing". A closed flag that still cost one round trip per command
would be a regression nobody would see.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from unittest.mock import AsyncMock, MagicMock
from uuid import UUID, uuid4

import pytest

from brain_v42.provenance import set_current_transport
from brain_v42.services.brain_session_service import BrainSessionService

_CONNECTION = "5544332211ffeeddccbbaa9988776655"


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


@pytest.fixture
def _closed_flag(monkeypatch: pytest.MonkeyPatch) -> None:
    import brain_v42.services.brain_session_service as module

    monkeypatch.setattr(
        module,
        "get_settings",
        lambda: MagicMock(brain_session_derived_capture_enabled=False),
    )


def _repo(session_id: UUID) -> MagicMock:
    result = MagicMock()
    result.session = MagicMock(id=session_id)
    repo = MagicMock()
    for method in ("start", "resume", "capture", "heartbeat", "end"):
        setattr(repo, method, AsyncMock(return_value=result))
    repo.absorb_derived_capture = AsyncMock(return_value=0)
    # Mirror of the Protocol: `start` re-reads the ledger after absorbing. A
    # double without this method would make the tests fail on the double's SHAPE,
    # not on the service's behaviour.
    repo.attributed_knowledge_ids = AsyncMock(return_value=[])
    return repo


async def _run(service: BrainSessionService, command: str, session_id: UUID) -> None:
    if command == "start":
        await service.start("brain-v42", "task-a")
    elif command == "resume":
        await service.resume(session_id, "task-a")
    elif command == "capture":
        await service.capture(session_id, "task-a", [uuid4()])
    elif command == "heartbeat":
        await service.heartbeat(session_id, "task-a")
    else:
        await service.end(session_id, "task-a", "summary", "next", 3)


_COMMANDS = ["start", "resume", "capture", "heartbeat", "end"]


@pytest.mark.parametrize("command", _COMMANDS)
async def test_every_command_absorbs_exactly_once(command: str, _open_flag: None) -> None:
    session_id = uuid4()
    repo = _repo(session_id)

    await _run(BrainSessionService(repo), command, session_id)

    # IDENTITY travels with the mutation: the guard lives inside the absorption,
    # not at the call site. An absorption called without it would be a ledger
    # move with no ownership check.
    repo.absorb_derived_capture.assert_awaited_once_with(session_id, _CONNECTION, "task-a")


@pytest.mark.parametrize("command", _COMMANDS)
async def test_a_closed_flag_costs_no_extra_round_trip(command: str, _closed_flag: None) -> None:
    session_id = uuid4()
    repo = _repo(session_id)

    await _run(BrainSessionService(repo), command, session_id)

    repo.absorb_derived_capture.assert_not_awaited()


@pytest.mark.parametrize("command", _COMMANDS)
async def test_no_connection_absorbs_nothing(command: str, _open_flag: None) -> None:
    """stdio and stateless mode: the (project, connection) key does not exist."""
    set_current_transport(None)
    session_id = uuid4()
    repo = _repo(session_id)

    await _run(BrainSessionService(repo), command, session_id)

    repo.absorb_derived_capture.assert_not_awaited()


async def test_start_absorbs_on_the_replay_branch_too(_open_flag: None) -> None:
    """Replay = the same session returned. This is the branch with something to absorb.

    The FRESH branch almost never absorbs anything — `started_at` was just set,
    so the window is empty. If only the fresh branch were wired, the wiring would
    look done and serve nothing.
    """
    session_id = uuid4()
    repo = _repo(session_id)
    service = BrainSessionService(repo)

    await service.start("brain-v42", "task-a")
    await service.start("brain-v42", "task-a")

    assert repo.absorb_derived_capture.await_count == 2


async def test_end_absorbs_before_it_persists(_open_flag: None) -> None:
    """The order is the point: `end` reads the ledger to decide how to close.

    Absorbing AFTER the closure would make the ledger visible too late — the
    session would be closed having concluded it produced nothing.
    """
    session_id = uuid4()
    repo = _repo(session_id)
    order: list[str] = []
    repo.absorb_derived_capture.side_effect = lambda *a, **k: order.append("absorb") or 0
    repo.end.side_effect = lambda *a, **k: order.append("end") or MagicMock()

    await BrainSessionService(repo).end(session_id, "task-a", "summary", "next", 3)

    assert order == ["absorb", "end"]


# ---------------------------------------------------------------------------
# THE ORDER, across all FIVE commands — not only the one that already had it
# ---------------------------------------------------------------------------

#: `end` alone carried this guarantee. The other four materialized their result
#: BEFORE the absorption they trigger: the receipt was not mute, it was ONE CALL
#: BEHIND. Measured in production on 2026-08-25 — a first `heartbeat` returned
#: `attributed_knowledge_ids: []` on a session carrying 5 artifacts in the
#: ledger, the second returned all 5.
#: `start` is ABSENT from this list, and that is not an exemption of convenience.
#: Its target does not exist before it materializes: `absorb_derived_capture`
#: requires a `session_id`, and `start` is precisely what resolves it. Demanding
#: an "absorb first" order from it would force a wrong design to satisfy a test.
#: What is asked of it is the PROPERTY, not the mechanism — that its result
#: reflect the absorption — and the next test is what pins that.
_ORDERED_COMMANDS = ["resume", "capture", "heartbeat", "end"]


@dataclass(frozen=True)
class _FakeSession:
    """Minimal mirror of `BrainSession` — the service touches only these two fields."""

    id: UUID
    attributed_knowledge_ids: list[UUID]

    def model_copy(self, *, update: dict[str, object]) -> _FakeSession:
        return replace(self, **update)  # type: ignore[arg-type]


@dataclass(frozen=True)
class _FakeStartResult:
    session: _FakeSession

    def model_copy(self, *, update: dict[str, object]) -> _FakeStartResult:
        return replace(self, **update)  # type: ignore[arg-type]


def _ordering_repo(session_id: UUID, order: list[str]) -> MagicMock:
    """A repository that RECORDS the real call order, absorption included."""
    repo = _repo(session_id)

    def _record(name: str, value: object) -> object:
        order.append(name)
        return value

    result = MagicMock()
    result.session = MagicMock(id=session_id)
    for method in ("start", "resume", "capture", "heartbeat", "end"):
        setattr(
            repo,
            method,
            AsyncMock(side_effect=lambda *a, _n=method, **k: _record(_n, result)),
        )
    repo.absorb_derived_capture = AsyncMock(side_effect=lambda *a, **k: _record("absorb", 0))
    return repo


@pytest.mark.parametrize("command", _ORDERED_COMMANDS)
async def test_every_command_absorbs_before_it_materializes(command: str, _open_flag: None) -> None:
    """A result computed before the absorption it triggers LIES by one round.

    That is the defect measured in production, and it was not a design blind
    spot: `end` already carried this guarantee, tested by name. It simply was
    never extended to the other four commands.

    The guarantee asserted here is the only one that matters to the caller: when
    the repository materializes what it will return, the absorption has ALREADY
    happened.
    """
    session_id = uuid4()
    order: list[str] = []
    repo = _ordering_repo(session_id, order)

    await _run(BrainSessionService(repo), command, session_id)

    assert order.index("absorb") < order.index(command), (
        f"`{command}` matérialise son résultat AVANT d'absorber : il rendra le "
        "ledger d'avant, donc un reçu en retard d'un appel"
    )


async def test_start_result_reflects_the_absorption_it_triggered(_open_flag: None) -> None:
    """The PROPERTY for `start`, since the order is structurally forbidden to it.

    On the REPLAY branch — an already open session that `start` finds — the
    absorption can move artifacts. The returned result must carry them, otherwise
    `start` lies exactly as `heartbeat` lied: by one call.

    So we do not assert "absorbs first", which would be impossible, but "what you
    hand me has seen the absorption".
    """
    session_id, moved = uuid4(), sorted([uuid4(), uuid4()], key=str)
    repo = _repo(session_id)
    # A `MagicMock` would answer `[]` to `list(...)` by grace of `__iter__`, so
    # it would go green the day the service stopped rehydrating. This double
    # implements `model_copy` for real: it cannot lie by omission.
    repo.start = AsyncMock(return_value=_FakeStartResult(_FakeSession(session_id, [])))
    repo.attributed_knowledge_ids = AsyncMock(return_value=moved)

    result = await BrainSessionService(repo).start("brain-v42", "task-a")

    repo.attributed_knowledge_ids.assert_awaited_once_with(session_id)
    assert list(result.session.attributed_knowledge_ids) == moved
