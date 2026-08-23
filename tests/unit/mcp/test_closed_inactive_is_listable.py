"""Le 4ᵉ état de session est LISTABLE — sinon la nuit ferme sans témoin.

La 046 a livré `closed_inactive` dans les DEUX `CHECK` de `brain_sessions`, et
le balayage nocturne sait le poser. Mais `SessionStatusFilter`, le seul filtre
publié au catalogue MCP, ne le nommait pas (`24ca3b73`) : un opérateur ne
pouvait donc pas lister ce que la nuit avait fermé automatiquement.

**Le service et le dépôt le supportaient déjà.** `_normalize_status` accepte
tout membre de `BrainSessionStatus`, et `list_sessions` filtre sur
`brain_sessions.c.status == status` sans énumérer. Le trou vivait dans le seul
littéral publié — un état atteignable en base, écrivable par le serveur, et
indemandable par un client. Encore un schéma posé sans lecteur.

**Pourquoi c'est urgent maintenant** : le drapeau du balayage d'inactivité est
armé sur la mauvaise unité systemd, donc `inactive_cutoff=off` et zéro
fermeture. Le jour où l'opérateur pose le drop-in sur `brain-v42-dream`, les
sessions inobservées seront fermées dès la première nuit — et sans ce filtre,
personne ne pourra dire lesquelles.

Le témoin d'anti-dérive est `test_the_filter_covers_every_persisted_status` : il
dérive ses attentes de l'énumération elle-même, donc un 5ᵉ état ajouté à
`BrainSessionStatus` sans être publié au filtre **rougit ici**, au lieu d'être
découvert par un opérateur qui ne trouve pas ses sessions.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastmcp import FastMCP
from pydantic import ValidationError

from brain_v42.models.brain_session import BrainSessionStatus

#: Les deux filtres DÉRIVÉS, qui ne sont pas des statuts persistés : `stale` est
#: calculé sur `last_heartbeat_at`, `all` est l'absence de filtre. Les distinguer
#: importe — les confondre avec les statuts ferait passer un test qui compte
#: seulement des entrées, sans vérifier lesquelles.
DERIVED_FILTERS = frozenset({"stale", "all"})


def _server(service: Any) -> FastMCP:
    from brain_v42.mcp.tools.session_lifecycle_tools import register_session_lifecycle_tools

    server = FastMCP("closed-inactive-listable")
    register_session_lifecycle_tools(server, service, AsyncMock(return_value=""))
    return server


def _service() -> Any:
    service = MagicMock()
    service.list = AsyncMock(return_value=MagicMock())
    return service


async def _status_enum() -> list[str]:
    server = _server(_service())
    tool = await server.get_tool("brain_session_list")
    assert tool is not None
    return list(tool.parameters["properties"]["status"]["enum"])


async def test_closed_inactive_is_an_accepted_list_filter() -> None:
    """Le filtre publié nomme `closed_inactive`.

    L'assertion porte sur le schéma PUBLIÉ : c'est ce qu'un client peut
    demander, et le seul niveau où l'omission était observable.
    """
    assert BrainSessionStatus.CLOSED_INACTIVE.value in await _status_enum()


async def test_the_filter_covers_every_persisted_status() -> None:
    """Anti-dérive : le filtre couvre TOUS les statuts persistés, dérivé de l'énum.

    Rien n'est écrit en dur ici. Un 5ᵉ état ajouté à `BrainSessionStatus` et
    oublié au filtre rougit sur cette ligne — c'est le témoin qui manquait
    quand la 046 a ajouté le 4ᵉ.
    """
    published = set(await _status_enum())
    persisted = {status.value for status in BrainSessionStatus}

    assert persisted <= published, f"statuts persistés absents du filtre : {persisted - published}"
    # Témoin négatif : le filtre ne publie RIEN d'autre que les statuts
    # persistés et les deux filtres dérivés. Sans cette moitié, publier
    # n'importe quoi (un état inexistant, une faute de frappe) passerait.
    assert published == persisted | DERIVED_FILTERS, (
        f"le filtre publie des valeurs inattendues : {published - persisted - DERIVED_FILTERS}"
    )


async def test_listing_closed_inactive_reaches_the_service() -> None:
    """Le chemin est EMPRUNTÉ, pas seulement déclaré.

    Un `enum` qui contient la valeur ne prouve pas qu'un appel la traverse : la
    validation pourrait la refuser plus bas, ou le tool la réécrire. Ce test
    APPELLE le tool et lit ce que le service a réellement reçu.
    """
    service = _service()
    tool = await _server(service).get_tool("brain_session_list")
    assert tool is not None

    await tool.run({"project_key": "brain-v42", "status": "closed_inactive"})

    service.list.assert_called_once()
    assert service.list.call_args.kwargs["status"] == "closed_inactive"


async def test_an_unknown_status_is_still_refused() -> None:
    """Élargir le filtre n'est pas l'ouvrir. Témoin négatif de l'élargissement.

    Sans lui, remplacer le `Literal` par un `str` nu rendrait les trois tests
    précédents verts tout en supprimant la validation — exactement le
    contre-sens que ce lot doit éviter.
    """
    tool = await _server(_service()).get_tool("brain_session_list")
    assert tool is not None

    with pytest.raises(ValidationError):
        await tool.run({"project_key": "brain-v42", "status": "closed-inactive"})
