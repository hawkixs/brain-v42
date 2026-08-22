"""Chaque appel dream porte DEUX identités ; une seule était vérifiée.

- Le **JETON** : le serveur n'accepte un principal scopé que si
  `client_id == "dream-codex-{phase}"`, avec la claim `agent` identique, les
  scopes exacts et le jeu de claims exact. Cette borne est serrée, et elle tient.
- L'**ACTEUR** : l'en-tête `X-Brain-Agent`, posé en variable de contexte par
  `ProvenanceMiddleware`, **jamais confronté au jeton**.

Ce n'est PAS une faille de périmètre — la borne de capacité porte sur le jeton.
C'est un défaut d'ATTRIBUTION, et il est STRUCTUREL, pas accidentel : le drop-in
vivant déclare `BRAIN_DREAM_AGENT_PROVIDERS=codex,agy,claude`, et les trois
runners posent trois en-têtes distincts — `dream-codex-{phase}`,
`dream-agy-{phase}`, `dream-claude-{phase}` — pour un registre de jetons qui ne
frappe que des profils codex. **Deux rails sur trois divergent par
construction, à chaque appel.**

Mesuré le 2026-08-22 sur `dream_runs`, la seule source qui survive : le rail
codex a produit 672 runs depuis le 04/08, le rail agy 235 entre le 11 et le
17/08. Le repli n'est pas théorique, il a tourné une semaine.

POURQUOI JOURNALISER PLUTÔT QUE REFUSER. Refuser casserait le rail de repli tant
qu'il annonce `agy` avec un jeton codex. Dériver l'acteur du jeton ferait perdre
l'information « quel rail a réellement tourné », précisément ce qui a permis de
mesurer le ratio. Journaliser ne peut rien casser et produit le dénominateur
dont les deux autres formes ont besoin — et il n'existe AUCUNE autre source :
`access_log.actor` est drainé toutes les 300 s (mesuré à 0 ligne), rien dans
journald ne porte l'acteur par appel, `process_metrics.agent_name` est le nom du
PROCESSUS, les compteurs par agent du collecteur vivent en mémoire et meurent au
restart, et `dream_runs` n'a pas de colonne d'acteur.
"""

from __future__ import annotations

import importlib
from typing import Any
from unittest.mock import AsyncMock

import pytest

from brain_v42.provenance import set_current_actor
from tests.unit.mcp.test_dream_capabilities import (
    _call_context,
    _capability_middleware,
    _scoped_access_token,
)


def _divergences(events: list[tuple[str, dict]]) -> list[dict]:
    return [fields for event, fields in events if event == "dream_identity_divergence"]


@pytest.mark.asyncio
async def test_an_aligned_actor_is_silent() -> None:
    """Le rail codex nominal ne doit rien produire — sinon le signal est du bruit."""
    capabilities = importlib.import_module("brain_v42.mcp.dream_capabilities")
    middleware = _capability_middleware(capabilities)
    call_next = AsyncMock(return_value="allowed-result")
    seen: list[tuple[str, dict]] = []

    class _Recorder:
        def info(self, event: str, **fields: Any) -> None:
            seen.append((event, fields))

        def __getattr__(self, _name: str) -> Any:
            return lambda *a, **k: None

    with pytest.MonkeyPatch.context() as monkeypatch:
        monkeypatch.setattr(
            capabilities,
            "get_access_token",
            lambda: _scoped_access_token(phase="scan"),
            raising=False,
        )
        monkeypatch.setattr(capabilities, "logger", _Recorder(), raising=False)
        set_current_actor("dream-codex-scan")
        try:
            result = await middleware.on_call_tool(_call_context("brain_search"), call_next)
        finally:
            set_current_actor("unknown")

    assert result == "allowed-result"
    assert _divergences(seen) == []


@pytest.mark.asyncio
@pytest.mark.parametrize("actor", ["dream-agy-scan", "dream-claude-scan", "unknown"])
async def test_a_diverging_actor_is_logged_AND_NOT_REFUSED(actor: str) -> None:
    """LE TÉMOIN QUI COMPTE : l'appel PASSE, et il est compté.

    Refuser casserait le rail de repli, qui est exactement ce que le repli
    existe pour éviter. Une observation qui bloque la nuit qu'elle observe est
    pire que l'angle mort qu'elle corrige.
    """
    capabilities = importlib.import_module("brain_v42.mcp.dream_capabilities")
    middleware = _capability_middleware(capabilities)
    call_next = AsyncMock(return_value="allowed-result")
    seen: list[tuple[str, dict]] = []

    class _Recorder:
        def info(self, event: str, **fields: Any) -> None:
            seen.append((event, fields))

        def __getattr__(self, _name: str) -> Any:
            return lambda *a, **k: None

    with pytest.MonkeyPatch.context() as monkeypatch:
        monkeypatch.setattr(
            capabilities,
            "get_access_token",
            lambda: _scoped_access_token(phase="scan"),
            raising=False,
        )
        monkeypatch.setattr(capabilities, "logger", _Recorder(), raising=False)
        set_current_actor(actor)
        try:
            result = await middleware.on_call_tool(_call_context("brain_search"), call_next)
        finally:
            set_current_actor("unknown")

    assert result == "allowed-result", "la divergence ne refuse RIEN"
    call_next.assert_awaited_once()

    logged = _divergences(seen)
    assert len(logged) == 1
    fields = logged[0]
    assert fields["actor"] == actor
    assert fields["token_client_id"] == "dream-codex-scan"
    assert fields["phase"] == "scan"


@pytest.mark.asyncio
async def test_the_observation_can_never_break_the_call() -> None:
    """Un canal d'OBSERVATION ne peut pas être un point de défaillance.

    Même règle que `ProvenanceMiddleware._report` : au pire on perd une ligne.
    Sans ce témoin, une divergence journalisée pourrait tuer la nuit qu'elle
    documente.
    """
    capabilities = importlib.import_module("brain_v42.mcp.dream_capabilities")
    middleware = _capability_middleware(capabilities)
    call_next = AsyncMock(return_value="allowed-result")

    class _Exploding:
        def __getattr__(self, _name: str) -> Any:
            def _boom(*_a: Any, **_k: Any) -> None:
                raise RuntimeError("journal cassé")

            return _boom

    with pytest.MonkeyPatch.context() as monkeypatch:
        monkeypatch.setattr(
            capabilities,
            "get_access_token",
            lambda: _scoped_access_token(phase="scan"),
            raising=False,
        )
        monkeypatch.setattr(capabilities, "logger", _Exploding(), raising=False)
        set_current_actor("dream-agy-scan")
        try:
            result = await middleware.on_call_tool(_call_context("brain_search"), call_next)
        finally:
            set_current_actor("unknown")

    assert result == "allowed-result"
