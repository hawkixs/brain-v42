"""Câblage de l'ABSORPTION sur les commandes explicites de session.

La traçante recueille ; la session de l'utilisateur absorbe. Ce module épingle
QUAND elle absorbe — à chaque commande, une fois, et au plus tard à `end` — et
surtout ce qu'elle ne fait pas quand le drapeau est fermé : **zéro appel
supplémentaire au dépôt**, pas « un appel qui ne fait rien ». Un drapeau fermé
qui coûterait quand même un aller-retour par commande serait une régression que
personne ne verrait.
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
    # Miroir du Protocol : `start` relit le ledger après avoir absorbé. Un double
    # qui ne porte pas cette méthode ferait échouer les tests sur la FORME du
    # double, pas sur le comportement du service.
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

    # L'IDENTITÉ voyage avec la mutation : la garde vit dans l'absorption,
    # pas au site d'appel. Une absorption appelée sans elle serait un
    # déplacement de ledger sans contrôle de propriété.
    repo.absorb_derived_capture.assert_awaited_once_with(session_id, _CONNECTION, "task-a")


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


# ---------------------------------------------------------------------------
# L'ORDRE, sur les CINQ commandes — pas seulement sur celle qui l'avait déjà
# ---------------------------------------------------------------------------

#: `end` portait seul cette garantie. Les quatre autres matérialisaient leur
#: résultat AVANT l'absorption qu'elles déclenchent : le reçu n'était pas muet,
#: il était EN RETARD D'UN APPEL. Mesuré en production le 2026-08-25 — un
#: premier `heartbeat` a rendu `attributed_knowledge_ids: []` sur une session
#: portant 5 artefacts au ledger, le second a rendu les 5.
#: `start` est ABSENT de cette liste, et ce n'est pas une exemption de confort.
#: Sa cible n'existe pas avant qu'il ne matérialise : `absorb_derived_capture`
#: exige un `session_id`, et `start` est justement ce qui le résout. Exiger de
#: lui l'ordre « absorber d'abord » forcerait une conception fausse pour
#: satisfaire un test. Ce qu'on lui demande est la PROPRIÉTÉ, pas le mécanisme —
#: que son résultat reflète l'absorption — et c'est le test suivant qui l'épingle.
_ORDERED_COMMANDS = ["resume", "capture", "heartbeat", "end"]


@dataclass(frozen=True)
class _FakeSession:
    """Miroir minimal de `BrainSession` — le service n'en touche que ces deux champs."""

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
    """Dépôt qui NOTE l'ordre réel des appels, absorption comprise."""
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
    """Un résultat calculé avant l'absorption qu'il déclenche MENT d'un tour.

    C'est le défaut mesuré en production, et il n'était pas un angle mort de
    conception : `end` portait déjà cette garantie, testée nommément. Elle n'a
    simplement jamais été étendue aux quatre autres commandes.

    La garantie asserée ici est la seule qui compte pour l'appelant : quand le
    dépôt matérialise ce qu'il va rendre, l'absorption a DÉJÀ eu lieu.
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
    """La PROPRIÉTÉ pour `start`, puisque l'ordre lui est structurellement interdit.

    Sur la branche de REJEU — une session déjà ouverte que `start` retrouve —
    l'absorption peut déplacer des artefacts. Le résultat rendu doit les porter,
    sinon `start` ment exactement comme `heartbeat` mentait : d'un appel.

    On n'asserte donc pas « absorbe avant », qui serait impossible, mais « ce que
    tu me rends a vu l'absorption ».
    """
    session_id, moved = uuid4(), sorted([uuid4(), uuid4()], key=str)
    repo = _repo(session_id)
    # Un `MagicMock` répondrait `[]` à `list(...)` par la grâce de `__iter__`,
    # donc verdirait le jour où le service cesserait de réhydrater. Ce double-ci
    # implémente `model_copy` pour de vrai : il ne peut pas mentir par omission.
    repo.start = AsyncMock(return_value=_FakeStartResult(_FakeSession(session_id, [])))
    repo.attributed_knowledge_ids = AsyncMock(return_value=moved)

    result = await BrainSessionService(repo).start("brain-v42", "task-a")

    repo.attributed_knowledge_ids.assert_awaited_once_with(session_id)
    assert list(result.session.attributed_knowledge_ids) == moved
