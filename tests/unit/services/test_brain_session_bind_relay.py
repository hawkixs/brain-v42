from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest

from brain_v42.config import Settings
from brain_v42.models.brain_session import BrainSessionInputError
from brain_v42.models.focus_slot import FocusSlotError
from brain_v42.services.brain_session_service import BrainSessionService


async def test_bind_normalises_the_identity_and_forwards() -> None:
    repo = MagicMock()
    repo.bind = AsyncMock(return_value="bound")
    session_id, slot_id = uuid4(), uuid4()
    assert await BrainSessionService(repo).bind(session_id, " key ", slot_id) == "bound"
    repo.bind.assert_awaited_once_with(session_id, "key", slot_id)


async def test_bind_refuses_a_blank_identity_before_the_repository() -> None:
    repo = MagicMock()
    repo.bind = AsyncMock()
    with pytest.raises(BrainSessionInputError, match="expected_client_key"):
        await BrainSessionService(repo).bind(uuid4(), "  ", uuid4())
    repo.bind.assert_not_awaited()


_DSN = "postgresql+asyncpg://brain:x@localhost:5433/brain_test"


def _relay_repo() -> MagicMock:
    repo = MagicMock()
    repo.relay = AsyncMock(return_value="relayed")
    repo.absorb_derived_capture_outcome = AsyncMock()
    return repo


def _flag(monkeypatch: pytest.MonkeyPatch, enabled: bool) -> None:
    monkeypatch.setattr(
        "brain_v42.services.brain_session_service.get_settings",
        lambda: SimpleNamespace(
            brain_session_relay_guard_mod_enabled=enabled,
            brain_session_derived_capture_enabled=False,
        ),
    )


def _relay(service: BrainSessionService, **overrides):
    values = {
        "summary": " done ",
        "handover": " next ",
        "expected_slot_revision": 3,
        "new_client_key": " successor ",
        "initiator": "operator",
    }
    values.update(overrides)
    return service.relay(uuid4(), " old ", **values)


def test_the_flag_ships_closed_and_reads_its_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    assert Settings.model_fields["brain_session_relay_guard_mod_enabled"].default is False
    monkeypatch.setenv("BRAIN_SESSION_RELAY_GUARD_MOD_ENABLED", "true")
    assert Settings(postgres_url=_DSN).brain_session_relay_guard_mod_enabled is True


async def test_guard_mod_is_refused_while_the_flag_is_off_s10(monkeypatch) -> None:
    _flag(monkeypatch, False)
    repo = _relay_repo()
    with pytest.raises(FocusSlotError, match="^relay_guard_mod_disabled: "):
        await _relay(BrainSessionService(repo), initiator="guard_mod")
    repo.relay.assert_not_awaited()
    repo.absorb_derived_capture_outcome.assert_not_awaited()


async def test_operator_is_accepted_while_the_flag_is_off_and_inputs_are_normalised(
    monkeypatch,
) -> None:
    _flag(monkeypatch, False)
    repo = _relay_repo()
    assert await _relay(BrainSessionService(repo)) == "relayed"
    kwargs = repo.relay.await_args.kwargs
    assert repo.relay.await_args.args[1] == "old"
    assert (kwargs["summary"], kwargs["handover"], kwargs["new_client_key"]) == (
        "done",
        "next",
        "successor",
    )
    assert (kwargs["expected_slot_revision"], kwargs["initiator"], kwargs["knowledge_ids"]) == (
        3,
        "operator",
        [],
    )


async def test_guard_mod_is_accepted_once_the_flag_is_on(monkeypatch) -> None:
    _flag(monkeypatch, True)
    repo = _relay_repo()
    await _relay(BrainSessionService(repo), initiator="guard_mod")
    repo.relay.assert_awaited_once()


async def test_a_settings_failure_refuses_guard_mod(monkeypatch) -> None:
    def broken():
        raise RuntimeError("no settings")

    monkeypatch.setattr("brain_v42.services.brain_session_service.get_settings", broken)
    with pytest.raises(FocusSlotError, match="^relay_guard_mod_disabled: "):
        await _relay(BrainSessionService(_relay_repo()), initiator="guard_mod")


async def test_the_successor_needs_a_new_client_key(monkeypatch) -> None:
    _flag(monkeypatch, False)
    with pytest.raises(FocusSlotError, match="^relay_same_client_key: "):
        await _relay(BrainSessionService(_relay_repo()), new_client_key="old ")


async def test_a_handover_over_4000_characters_is_slot_body_too_long(monkeypatch) -> None:
    _flag(monkeypatch, False)
    repo = _relay_repo()
    with pytest.raises(FocusSlotError, match="^slot_body_too_long: "):
        await _relay(BrainSessionService(repo), handover="é" * 4001)
    await _relay(BrainSessionService(repo), handover="é" * 4000)
    repo.relay.assert_awaited_once()


@pytest.mark.parametrize("initiator", ["hook", "", "OPERATOR"])
async def test_an_unknown_initiator_is_an_input_error(monkeypatch, initiator: str) -> None:
    _flag(monkeypatch, True)
    with pytest.raises(BrainSessionInputError, match="initiator"):
        await _relay(BrainSessionService(_relay_repo()), initiator=initiator)


@pytest.mark.parametrize("revision", [-1, True, "3"])
async def test_a_bad_slot_revision_is_refused_before_the_repository(monkeypatch, revision) -> None:
    _flag(monkeypatch, False)
    repo = _relay_repo()
    with pytest.raises(BrainSessionInputError, match="expected_slot_revision"):
        await _relay(BrainSessionService(repo), expected_slot_revision=revision)
    repo.relay.assert_not_awaited()
