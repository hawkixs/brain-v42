"""Marche 1 de `55a21fb8` : REORG cesse d'écrire une transition muette.

`brain_update` écrit `freshness_status` **sans jamais nommer la colonne** — elle
arrive par le modèle Pydantic de mise à jour. Aucun `grep` sur le nom de colonne
ne peut le voir, et c'est ainsi qu'il a échappé à un premier recensement. Le
trigger de la 043 remet alors `freshness_source` à `NULL` : la transition est
datée, mais orpheline.

Mesuré en production le 2026-08-22, sur la fenêtre réelle de douze jours :
**3 transitions muettes sur 44**, toutes sur `learnings`, toutes vers
`archived`, à 68 ms d'intervalle, dans un projet dont la nuit portait un run
`reorg` WET. `brain_update` est la SEULE écriture que l'allowlist serveur
accorde à REORG.

La valeur employée est `judgment` : le `CHECK` de la 043 l'autorise déjà et
**aucun code ne l'écrivait** — réservée, inutilisée, et elle décrit exactement
ce que fait REORG. Aucune migration, donc rien dans le couloir signé. Exercée
pour la PREMIÈRE fois le 2026-08-22 contre `brain_test`, transaction annulée :
acceptée, avec la date posée ; et une valeur hors vocabulaire refusée par la
même contrainte.

**Ce que cette marche ne fait PAS**, et c'est délibéré : elle ne tarit que la
source CONNUE. Une écriture humaine reste muette — donc toujours VUE par le
compteur de la marche 0. Tarir tout d'un coup aurait supprimé le signal en même
temps que le bruit, et la prochaine source non recensée serait passée sans que
rien ne bouge.
"""

from __future__ import annotations

import inspect
import re
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest

from brain_v42.mcp.dream_project_authorization import (
    DreamProjectAudit,
    DreamProjectScope,
    bind_dream_project_scope,
)
from tests.unit.mcp._tool_error_adapter import capture_tool_errors

PROJECT_KEY = "reorg-owned"

#: Le vocabulaire fermé du `CHECK` de la 043. `judgment` y était déjà.
FRESHNESS_SOURCES = ("merge", "judgment", "score", "revive")


class MockMCP:
    def __init__(self) -> None:
        self.registered: dict[str, Any] = {}

    def tool(self, **_kwargs: Any) -> Any:
        def decorator(fn: Any) -> Any:
            self.registered[fn.__name__] = capture_tool_errors(fn)
            return fn

        return decorator


class UnusedResolver:
    async def references_belong_to_project(self, *_args: Any) -> bool:
        raise AssertionError("point-of-use scoping must not rerun middleware resolution")


def _scope() -> DreamProjectScope:
    return DreamProjectScope(
        project_key=PROJECT_KEY,
        resolver=UnusedResolver(),
        audit=DreamProjectAudit(principal="dream-codex-reorg", phase="reorg"),
        tool_name="brain_update",
    )


def _registered_tools() -> tuple[dict[str, Any], dict[str, Any]]:
    from brain_v42.mcp.tools.crud_tools import register_crud_tools

    mcp = MockMCP()
    services: dict[str, Any] = {}
    for name in ("decision_svc", "learning_svc", "snippet_svc", "runbook_svc", "adr_svc"):
        svc = MagicMock()
        svc.update = AsyncMock(return_value=None)
        svc.get_by_id = AsyncMock(return_value=None)
        svc.resolve_id_prefix = AsyncMock(return_value=[])
        services[name] = svc
    session = AsyncMock()
    context = AsyncMock()
    context.__aenter__ = AsyncMock(return_value=session)
    context.__aexit__ = AsyncMock(return_value=False)
    optional = (
        {"access_logger": MagicMock()}
        if "access_logger" in inspect.signature(register_crud_tools).parameters
        else {}
    )
    register_crud_tools(
        mcp, **services, session_factory=MagicMock(return_value=context), **optional
    )
    return mcp.registered, services


def _sent_model(services: dict[str, Any], entity: str = "learning") -> Any:
    call = services[f"{entity}_svc"].update.await_args
    assert call is not None, "le service doit avoir été appelé"
    return call.args[1]


@pytest.mark.asyncio
async def test_a_scoped_freshness_write_declares_judgment() -> None:
    """Le témoin positif : REORG déclare, donc la transition cesse d'être muette."""
    tools, services = _registered_tools()

    with bind_dream_project_scope(_scope()):
        await tools["brain_update"]("learning", str(uuid4()), {"freshness_status": "archived"})

    assert _sent_model(services).freshness_source == "judgment"


@pytest.mark.asyncio
async def test_an_unrelated_scoped_write_declares_nothing() -> None:
    """Une provenance FAUSSE est pire qu'une absente — le trigger le dit lui-même.

    Estampiller toutes les écritures scopées ferait décrire une transition qui
    n'a pas eu lieu. La marque ne suit que `freshness_status`.
    """
    tools, services = _registered_tools()

    with bind_dream_project_scope(_scope()):
        await tools["brain_update"]("learning", str(uuid4()), {"topic": "renommé"})

    assert _sent_model(services).freshness_source is None


@pytest.mark.asyncio
async def test_a_human_write_stays_mute_and_therefore_visible() -> None:
    """LE SECOND TÉMOIN, celui sans lequel on tarit la source et perd le signal.

    Hors scope dream, rien n'est estampillé : la transition reste muette, donc
    comptée par la marche 0. C'est ce qui permet de voir la PROCHAINE source non
    recensée au lieu de la confondre avec REORG.
    """
    tools, services = _registered_tools()

    await tools["brain_update"]("learning", str(uuid4()), {"freshness_status": "archived"})

    assert _sent_model(services).freshness_source is None


@pytest.mark.asyncio
@pytest.mark.parametrize("forged", ["judgment", "score", "revive", "merge"])
async def test_a_caller_may_never_forge_its_own_provenance(forged: str) -> None:
    """La provenance est posée par le SERVEUR ou pas du tout.

    Un appelant qui pourrait l'écrire pourrait signer une transition du nom d'un
    autre : « une provenance fausse, qui se croit, au lieu d'une provenance
    absente, qui se voit » — les mots de la 043.
    """
    tools, services = _registered_tools()

    with bind_dream_project_scope(_scope()):
        result = await tools["brain_update"](
            "learning",
            str(uuid4()),
            {"freshness_status": "archived", "freshness_source": forged},
        )

    assert "freshness_source" in result
    services["learning_svc"].update.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("entity", ["decision", "learning", "snippet", "runbook", "adr"])
async def test_every_mutable_type_declares(entity: str) -> None:
    """Les cinq types que `brain_update` peut écrire, pas seulement `learning`.

    Les trois transitions mesurées étaient sur `learnings` ; rien ne garantit
    que la prochaine le sera.
    """
    tools, services = _registered_tools()

    with bind_dream_project_scope(_scope()):
        await tools["brain_update"](entity, str(uuid4()), {"freshness_status": "archived"})

    assert _sent_model(services, entity).freshness_source == "judgment"


def test_the_stamped_value_belongs_to_the_043_vocabulary() -> None:
    """Sans ça, la marche 1 échouerait sur une contrainte, la nuit entière avec.

    Vérifié aussi contre la base réelle : `judgment` accepté, `reorg` refusé.
    """
    from brain_v42.mcp.tools import crud_tools

    assert crud_tools.DREAM_FRESHNESS_SOURCE in FRESHNESS_SOURCES

    migration = (
        Path(__file__).resolve().parents[4]
        / "alembic"
        / "versions"
        / "043_freshness_status_clock.py"
    ).read_text(encoding="utf-8")
    declared = set(re.findall(r"_SOURCES[^)]*?\)", migration, flags=re.S))
    assert declared, "la migration 043 doit déclarer son vocabulaire"
    assert crud_tools.DREAM_FRESHNESS_SOURCE in " ".join(declared), (
        "la valeur estampillée doit venir du vocabulaire de la 043, pas d'un littéral parallèle"
    )
