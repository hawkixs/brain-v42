"""Câblage de l'ABSORPTION sur les commandes explicites de session.

La traçante recueille ; la session de l'utilisateur absorbe. Ce module épingle
QUAND elle absorbe — à chaque commande, une fois, et au plus tard à `end` — et
surtout ce qu'elle ne fait pas quand le drapeau est fermé : **zéro appel
supplémentaire au dépôt**, pas « un appel qui ne fait rien ». Un drapeau fermé
qui coûterait quand même un aller-retour par commande serait une régression que
personne ne verrait.
"""

from __future__ import annotations

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

    repo.absorb_derived_capture.assert_awaited_once_with(session_id, _CONNECTION)


@pytest.mark.parametrize("command", _COMMANDS)
async def test_a_closed_flag_costs_no_extra_round_trip(command: str, _closed_flag: None) -> None:
    session_id = uuid4()
    repo = _repo(session_id)

    await _run(BrainSessionService(repo), command, session_id)

    repo.absorb_derived_capture.assert_not_awaited()


@pytest.mark.parametrize("command", _COMMANDS)
async def test_no_connection_absorbs_nothing(command: str, _open_flag: None) -> None:
    """stdio et mode sans état : la clé (projet, connexion) n'existe pas."""
    set_current_transport(None)
    session_id = uuid4()
    repo = _repo(session_id)

    await _run(BrainSessionService(repo), command, session_id)

    repo.absorb_derived_capture.assert_not_awaited()


async def test_start_absorbs_on_the_replay_branch_too(_open_flag: None) -> None:
    """Rejeu = même session rendue. C'est la branche qui a quelque chose à absorber.

    La branche NEUVE n'absorbe presque jamais rien — `started_at` vient d'être
    posé, donc la fenêtre est vide. Si seule la branche neuve était câblée, le
    câblage aurait l'air fait et ne servirait à rien.
    """
    session_id = uuid4()
    repo = _repo(session_id)
    service = BrainSessionService(repo)

    await service.start("brain-v42", "task-a")
    await service.start("brain-v42", "task-a")

    assert repo.absorb_derived_capture.await_count == 2


async def test_end_absorbs_before_it_persists(_open_flag: None) -> None:
    """L'ordre est le point : `end` lit le ledger pour décider de fermer.

    Absorber APRÈS la fermeture rendrait le ledger visible trop tard — la
    session serait close en ayant conclu qu'elle n'avait rien produit.
    """
    session_id = uuid4()
    repo = _repo(session_id)
    order: list[str] = []
    repo.absorb_derived_capture.side_effect = lambda *a, **k: order.append("absorb") or 0
    repo.end.side_effect = lambda *a, **k: order.append("end") or MagicMock()

    await BrainSessionService(repo).end(session_id, "task-a", "summary", "next", 3)

    assert order == ["absorb", "end"]
