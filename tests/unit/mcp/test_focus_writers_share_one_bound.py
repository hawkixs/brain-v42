"""Les écrivains de `project_contexts.current_focus` portent TOUS la même borne.

Le défaut (`bfb4cf93`) : `brain_session_end` plafonne `next_focus` à
``NEXT_FOCUS_MAX_LENGTH`` caractères, et cette valeur REMPLACE `current_focus`
quand le compare-and-swap réussit. Les autres écrivains de la MÊME colonne
prenaient un ``str`` nu — aucune borne, ni dans l'argument, ni dans le modèle,
ni dans le service, ni dans la colonne (``text``). Le plafond MCP était la SEULE
borne du chemin d'écriture, et elle ne couvrait qu'un écrivain sur trois.

Conséquence, et c'est le fond : **l'écrivain non borné met le projet dans un
état que l'écrivain borné ne sait pas représenter.** Une session honnête, qui a
lu un focus de 12 000 caractères, ne peut plus le rendre à la fermeture — elle
est refusée par une validation dont elle n'est pas responsable.

Rejoué le 2026-08-23 sur les instantanés `brain_sessions.started_focus` : la
révision 217 a porté **12 157 caractères** — 2 157 de plus que ce que
`brain_session_end` sait écrire — du 2026-08-21 16:14:45 au 2026-08-22 08:27:43,
soit **seize heures, vue par sept sessions**.

**TROIS écrivains, pas deux.** Le ticket et son mandat n'en nommaient que deux.
Le recensement par plusieurs motifs en trouve un troisième,
`brain_set_project_context`, dont le `current_focus` est optionnel et donc
invisible à qui cherche un argument obligatoire. C'est le même angle mort de
motif que le focus met en garde : compter par PLUSIEURS motifs.

**L'unité est le CARACTÈRE, et ce test porte le cas qui les distingue.** Ce
n'est pas une précaution de style : rejoué le 2026-08-23, **trois** révisions du
focus de `brain-v42` tenaient sous le plafond en caractères tout en le dépassant
en octets — 192 (9 996 / 10 277), **194 (9 984 / 10 287)** et 219 (9 977 /
10 285). La 194 est la plus tranchante : **seize caractères sous la borne, 287
octets au-dessus**. Une borne qui compterait des octets aurait donc refusé trois
focus parfaitement légaux.

Le témoin de distinction est un cas non-ASCII de longueur exactement égale au
plafond, donc deux fois plus lourd en octets : il doit être ACCEPTÉ, et le test
rougit le jour où quelqu'un réécrit la borne en octets.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest
from fastmcp import FastMCP
from pydantic import ValidationError

from brain_v42.mcp.tools.session_lifecycle_tools import NEXT_FOCUS_MAX_LENGTH

#: Chaque entrée est (nom du tool MCP, nom de l'argument qui écrit le focus).
#: `next_focus` et `current_focus` portent des noms différents mais écrivent la
#: MÊME colonne : `next_focus` DEVIENT `current_focus` quand le CAS réussit.
#: C'est ce qui rend leur borne commune, et non deux contrats voisins.
FOCUS_WRITERS: tuple[tuple[str, str], ...] = (
    ("brain_session_end", "next_focus"),
    ("brain_update_project_focus", "current_focus"),
    ("brain_set_project_context", "current_focus"),
)

#: Un caractère non-ASCII de 2 octets en UTF-8. `len()` en compte 1, `encode()`
#: en compte 2 : c'est tout l'objet du témoin de distinction.
_TWO_BYTE_CHAR = "é"


def _register_lifecycle(server: FastMCP, service: Any) -> None:
    from brain_v42.mcp.tools.session_lifecycle_tools import register_session_lifecycle_tools

    register_session_lifecycle_tools(server, service, AsyncMock(return_value=""))


def _register_project_context(server: FastMCP, context_svc: Any, roadmap_svc: Any) -> None:
    from brain_v42.mcp.tools.project_context_tools import register_project_context_tools

    register_project_context_tools(server, context_svc, roadmap_svc)


def _lifecycle_server() -> FastMCP:
    server = FastMCP("focus-bound-lifecycle")
    _register_lifecycle(server, MagicMock())
    return server


def _project_context_server() -> FastMCP:
    server = FastMCP("focus-bound-project-context")
    _register_project_context(server, AsyncMock(), AsyncMock())
    return server


def _invocation(tool_name: str, argument: str) -> tuple[FastMCP, Any, Any]:
    """Rendre (serveur, mock du service écrivain, fabrique d'arguments).

    Le mock rendu est celui que le tool appelle POUR ÉCRIRE — pas le service
    entier : c'est sur lui que porte le « jamais appelé » du fail-closed.
    """
    if tool_name == "brain_session_end":
        service = MagicMock()
        service.end = AsyncMock(
            return_value=SimpleNamespace(
                session=SimpleNamespace(id=uuid4()), briefing="", focus_outcome="applied"
            )
        )
        server = FastMCP("focus-bound-lifecycle")
        _register_lifecycle(server, service)
        return (
            server,
            service.end,
            lambda focus: {
                "session_id": str(uuid4()),
                "expected_client_key": "task-a",
                "summary": "done",
                argument: focus,
                "expected_focus_revision": 1,
                "nothing_to_capture_reason": "nothing durable",
            },
        )

    context_svc, roadmap_svc = AsyncMock(), AsyncMock()
    roadmap_svc.update_project_focus = AsyncMock(
        side_effect=lambda _key, focus, **_kw: SimpleNamespace(
            current_focus=focus, focus_revision=4, features_updated=(), features_unpinned=()
        )
    )
    server = FastMCP("focus-bound-project-context")
    _register_project_context(server, context_svc, roadmap_svc)

    if tool_name == "brain_update_project_focus":
        return (
            server,
            roadmap_svc.update_project_focus,
            lambda focus: {
                "project_key": "brain-v42",
                argument: focus,
                "expected_focus_revision": 1,
            },
        )
    return (
        server,
        context_svc.get_or_create,
        lambda focus: {
            "project_key": "brain-v42",
            "name": "Brain V42",
            "description": "Second Cerveau MCP server",
            argument: focus,
        },
    )


async def _focus_property(tool_name: str, argument: str) -> dict[str, Any]:
    server = _lifecycle_server() if tool_name == "brain_session_end" else _project_context_server()
    tool = await server.get_tool(tool_name)
    assert tool is not None, f"missing MCP tool {tool_name}"
    schema = tool.parameters["properties"][argument]
    # `brain_set_project_context` déclare son focus optionnel : la borne vit
    # alors dans la branche `string` de l'`anyOf`, pas à la racine. La chercher
    # au seul niveau racine rendrait ce test VERT sur un argument non borné.
    if "anyOf" in schema:
        schema = next(v for v in schema["anyOf"] if v.get("type") == "string")
    return schema


@pytest.mark.parametrize(("tool_name", "argument"), FOCUS_WRITERS)
async def test_every_focus_writer_publishes_the_same_bound(tool_name: str, argument: str) -> None:
    """Les trois écrivains annoncent la MÊME borne dans leur schéma publié.

    L'assertion porte sur le schéma PUBLIÉ, pas sur une constante importée :
    c'est ce que voit un client, et c'est le seul niveau où une divergence est
    observable de l'extérieur.
    """
    schema = await _focus_property(tool_name, argument)

    assert schema.get("maxLength") == NEXT_FOCUS_MAX_LENGTH, (
        f"{tool_name}.{argument} publie maxLength={schema.get('maxLength')!r} "
        f"au lieu de {NEXT_FOCUS_MAX_LENGTH}"
    )


@pytest.mark.parametrize(("tool_name", "argument"), FOCUS_WRITERS)
async def test_every_focus_writer_refuses_one_character_too_many(
    tool_name: str, argument: str
) -> None:
    """Un caractère de trop est REFUSÉ **avant tout appel de service**, jamais tronqué.

    C'est la preuve fail-closed, et elle se joue en APPELANT le tool, pas en
    relisant son schéma : un schéma qui annonce une borne ne prouve pas qu'elle
    s'applique. Deux assertions, indissociables :

    - à ``CAP + 1`` : ``ValidationError``, **et le service n'est jamais appelé**
      — donc rien n'est écrit, et rien n'est tronqué en silence ;
    - à ``CAP`` exactement (témoin négatif) : **le service EST appelé**. Sans
      lui, un argument devenu obligatoire-mais-toujours-refusé, ou une borne
      tombée à zéro, rendrait la première moitié verte.
    """
    server, service, call = _invocation(tool_name, argument)
    tool = await server.get_tool(tool_name)
    assert tool is not None

    with pytest.raises(ValidationError):
        await tool.run(call("x" * (NEXT_FOCUS_MAX_LENGTH + 1)))
    service.assert_not_called()

    # Témoin négatif : à la longueur exacte, la validation laisse passer et le
    # service reçoit l'écriture.
    await tool.run(call("x" * NEXT_FOCUS_MAX_LENGTH))
    service.assert_called_once()


@pytest.mark.parametrize(("tool_name", "argument"), FOCUS_WRITERS)
async def test_the_bound_counts_characters_not_bytes(tool_name: str, argument: str) -> None:
    """Le témoin de distinction : plafond en CARACTÈRES, pas en octets.

    Un focus non-ASCII de longueur exactement égale au plafond pèse DEUX FOIS
    le plafond en octets. Il doit être accepté. Ce test rougit le jour où
    quelqu'un réécrit la borne en octets — le cas que le focus réel de
    `brain-v42` rencontrait déjà (9 977 caractères pour 10 285 octets).
    """
    schema = await _focus_property(tool_name, argument)

    non_ascii = _TWO_BYTE_CHAR * NEXT_FOCUS_MAX_LENGTH
    assert len(non_ascii) == NEXT_FOCUS_MAX_LENGTH
    assert len(non_ascii.encode("utf-8")) == 2 * NEXT_FOCUS_MAX_LENGTH

    # Les deux comptes DIFFÈRENT sur cette entrée : c'est ce qui en fait un
    # témoin. Une borne en octets la refuserait ; une borne en caractères non.
    assert len(non_ascii) <= schema["maxLength"] < len(non_ascii.encode("utf-8"))


async def test_the_three_writers_are_not_parallel_literals() -> None:
    """Une borne partagée, pas trois littéraux qui dérivent séparément.

    Trois `10_000` écrits à trois endroits seraient verts aujourd'hui et
    divergeraient au premier changement — exactement le défaut que ce lot
    répare, reproduit une couche plus haut. Le test compare les bornes entre
    elles, sans citer aucun nombre : il rougit si l'une bouge seule.
    """
    bounds = {}
    for tool_name, argument in FOCUS_WRITERS:
        schema = await _focus_property(tool_name, argument)
        bounds[f"{tool_name}.{argument}"] = schema.get("maxLength")

    assert len(set(bounds.values())) == 1, f"bornes divergentes : {bounds}"


async def test_no_focus_writer_escapes_the_census() -> None:
    """Le recensement des écrivains est CLOS, et se compte par plusieurs motifs.

    `bfb4cf93` et son mandat n'en nommaient que DEUX ; il y en a trois. Ce test
    épingle la liste pour qu'un quatrième écrivain ajouté plus tard casse ici
    plutôt que d'être découvert par un focus irrécupérable. Le motif de
    recherche est le nom de la COLONNE dans la signature publiée, pas le nom de
    l'argument — c'est ce qui rattrape un `current_focus` optionnel.
    """
    from brain_v42.mcp.tools.project_context_tools import register_project_context_tools
    from brain_v42.mcp.tools.session_lifecycle_tools import register_session_lifecycle_tools

    found: set[tuple[str, str]] = set()
    for registrar, args in (
        (register_session_lifecycle_tools, (MagicMock(), AsyncMock())),
        (register_project_context_tools, (AsyncMock(), AsyncMock())),
    ):
        server = FastMCP("focus-writer-census")
        registrar(server, *args)
        for tool in await server.list_tools():
            for name, schema in tool.parameters.get("properties", {}).items():
                if name in ("next_focus", "current_focus"):
                    del schema
                    found.add((tool.name, name))

    assert found == set(FOCUS_WRITERS), (
        f"le recensement des écrivains de focus a changé : {sorted(found)}"
    )
