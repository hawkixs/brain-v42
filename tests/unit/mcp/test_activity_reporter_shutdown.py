"""L'arrêt du serveur ferme l'émetteur d'activité — la perte entre au compteur.

Ticket `d5e4bd73`, second trou : `close()` existait et n'était câblé NULLE
PART — les POST en vol mouraient à l'arrêt sans être comptés (~2,2/jour,
négligeable en volume ; ce qui compte est que la perte n'était pas dans le
compteur de pertes, exactement le défaut du trou principal). Le câblage passe
par `close_activity_reporter()`, enregistré dans l'AsyncExitStack de
`app_lifecycle` — le propriétaire unique du cycle de vie des deux transports.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from brain_v42.mcp import activity_reporter as reporter_module
from brain_v42.mcp.activity_reporter import (
    close_activity_reporter,
    set_activity_reporter,
)

SERVER_SOURCE = (Path(__file__).parents[3] / "src" / "brain_v42" / "mcp" / "server.py").read_text(
    encoding="utf-8"
)


@pytest.fixture(autouse=True)
def _reset_reporter() -> None:
    set_activity_reporter(None)
    yield
    set_activity_reporter(None)


@pytest.mark.asyncio
async def test_close_drains_then_forgets_the_reporter() -> None:
    fake = AsyncMock()
    set_activity_reporter(fake)

    await close_activity_reporter()

    fake.close.assert_awaited_once()
    # L'émetteur fermé ne doit jamais resservir : un POST après aclose()
    # lèverait dans le chemin chaud d'un appel de tool.
    assert reporter_module._reporter is None


@pytest.mark.asyncio
async def test_close_without_a_reporter_is_a_quiet_no_op() -> None:
    await close_activity_reporter()


@pytest.mark.asyncio
async def test_a_failing_close_never_breaks_the_shutdown() -> None:
    """Même promesse que l'émetteur : l'observation n'est jamais la panne —
    ici, un sidecar mort à l'arrêt ne doit pas faire échouer l'arrêt."""
    fake = AsyncMock()
    fake.close.side_effect = RuntimeError("sidecar déjà mort")
    set_activity_reporter(fake)

    await close_activity_reporter()

    assert reporter_module._reporter is None


def test_the_lifecycle_registers_the_close() -> None:
    """Le câblage vit dans `app_lifecycle`, pas dans une convention : un
    transport ajouté demain hérite de la fermeture sans y penser."""
    assert "cleanup.push_async_callback(close_activity_reporter)" in SERVER_SOURCE


def test_no_docstring_still_claims_close_is_unwired() -> None:
    """Les deux commentaires qui justifiaient l'absence de câblage doivent
    tomber avec elle — un texte qui décrit l'ancien monde ferait reprendre la
    même décision pour de mauvaises raisons."""
    reporter_source = (
        Path(__file__).parents[3] / "src" / "brain_v42" / "mcp" / "activity_reporter.py"
    ).read_text(encoding="utf-8")

    assert "câblé NULLE PART" not in reporter_source
    assert "Aucune fermeture n'est câblée" not in reporter_source
