"""Ancrage Phase 0 — la phrase-covenant vit dans les docstrings, et rien ne la gardait.

Le covenant d'explicitation n'est pas seulement une règle de documentation : il est
écrit dans le CONTRAT de chaque tool, et c'est ce que lit un agent avant d'appeler.
Sa forme courante est `_COVENANT` ci-dessous ; celle d'avant la 046 est
`_RETIRED_COVENANT`, et l'encadré « RÉÉCRIT PAR NATURE » dit pourquoi elle a changé.

Or **aucun test ne vérifiait sa présence** (recensé le 2026-08-19 : zéro occurrence de
cette phrase dans tests/). Elle pouvait disparaître d'un ou des sept tools sans qu'une
seule suite ne rougisse — et le covenant serait devenu une intention de prose.

Ce test est ÉCRIT POUR ÊTRE ÉTENDU. La refonte livre un huitième tool (`checkpoint`,
migration M-C) : le jour où il arrive, `_EXPECTED_TOOL_COUNT` passe à 8 et le mot de la
docstring d'enregistrement passe à « eight », DANS LE MÊME COMMIT que le tool. C'est
volontairement un point de friction : il force la mise à jour du contrat au moment où
la surface change, au lieu de la laisser dériver.

**RÉÉCRIT PAR NATURE (ADR §0ter (d), résolution ratifiée).** La 046 fait naître des
sessions `agent` que le SERVEUR ouvre et que le balayage nocturne ferme : la phrase
« No hook or auto-close may invoke this lifecycle boundary. » était donc devenue fausse
telle quelle. Elle n'est pas SUPPRIMÉE — le contrat doit rester lisible là où l'agent le
lit — elle NOMME maintenant son exception. Ce test a rougi sur les sept tools avant
d'être mis à jour ; c'était le geste Red qui ouvrait la livraison, pas un dégât.

`_RETIRED_COVENANT` est un TÉMOIN NÉGATIF, pas une redondance : sans lui, réintroduire
l'ancienne phrase À CÔTÉ de la neuve laisserait les deux vraies dans le même contrat,
dont une qui ment. Un critère périmé se retourne en test d'absence.
"""

from __future__ import annotations

import ast
import pathlib

import pytest

_MODULE = (
    pathlib.Path(__file__).resolve().parents[4]
    / "src/brain_v42/mcp/tools/session_lifecycle_tools.py"
)

#: Comparée sur les blancs NORMALISÉS : la phrase tient sur trois lignes de docstring,
#: et un test sensible au retour à la ligne épinglerait le remplissage, pas le contrat.
_COVENANT = (
    "An agent tracer is the only session the server opens or closes on its own; "
    "no hook and no auto-close may invoke this lifecycle boundary."
)

#: La forme d'AVANT la 046, qui ne connaissait pas les traçantes. Doit avoir disparu.
_RETIRED_COVENANT = "No hook or auto-close may invoke this lifecycle boundary."

#: Sept aujourd'hui. Le checkpoint (M-C) portera ce nombre à huit — voir le module.
_EXPECTED_TOOL_COUNT = 7

#: Le mot que la docstring d'enregistrement doit employer pour ce nombre.
_COUNT_WORD = {7: "seven", 8: "eight", 9: "nine"}


def _tool_functions() -> dict[str, ast.AsyncFunctionDef]:
    """Rendre les fonctions `brain_session_*`, où qu'elles soient imbriquées."""
    tree = ast.parse(_MODULE.read_text(encoding="utf-8"))
    return {
        node.name: node
        for node in ast.walk(tree)
        if isinstance(node, ast.AsyncFunctionDef) and node.name.startswith("brain_session_")
    }


def test_the_lifecycle_surface_is_exactly_the_expected_size() -> None:
    tools = _tool_functions()
    assert sorted(tools) == sorted(
        [
            "brain_session_start",
            "brain_session_list",
            "brain_session_resume",
            "brain_session_capture",
            "brain_session_heartbeat",
            "brain_session_end",
            "brain_session_abandon",
        ]
    )
    assert len(tools) == _EXPECTED_TOOL_COUNT


def _normalized_docstring(name: str) -> str:
    doc = ast.get_docstring(_tool_functions()[name])
    assert doc is not None, f"{name} n'a pas de docstring"
    return " ".join(doc.split())


@pytest.mark.parametrize("name", sorted(_tool_functions()))
def test_every_lifecycle_tool_states_the_covenant_in_its_docstring(name: str) -> None:
    assert _COVENANT in _normalized_docstring(name), (
        f"{name} ne porte plus la phrase-covenant. Le covenant d'explicitation est "
        f"écrit dans le contrat du tool, pas seulement dans CLAUDE.md : le retirer "
        f"change ce qu'un agent lit avant d'appeler."
    )


@pytest.mark.parametrize("name", sorted(_tool_functions()))
def test_no_tool_still_carries_the_pre_046_covenant(name: str) -> None:
    """Témoin négatif : l'ancienne phrase ne doit survivre nulle part.

    Elle affirmait qu'AUCUNE auto-fermeture ne franchit cette frontière. Depuis la
    046 c'est faux pour les traçantes `agent`, et deux phrases contradictoires dans
    le même contrat sont pires qu'une seule périmée.
    """
    assert _RETIRED_COVENANT not in _normalized_docstring(name), (
        f"{name} porte encore la phrase d'avant la 046. Elle promet qu'aucune "
        f"auto-fermeture ne franchit cette frontière — le balayage nocturne la "
        f"franchit désormais sur les sessions `agent`."
    )


def test_the_registration_docstring_counts_the_tools_it_registers() -> None:
    """Le nombre écrit en toutes lettres doit suivre la surface réelle.

    Sans ce test, « seven » survivrait à l'arrivée du huitième tool et la docstring
    mentirait sur ce qu'elle enregistre.
    """
    tree = ast.parse(_MODULE.read_text(encoding="utf-8"))
    register = next(
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef)
        and node.name == "register_session_lifecycle_tools"
    )
    doc = ast.get_docstring(register) or ""
    expected_word = _COUNT_WORD[_EXPECTED_TOOL_COUNT]
    assert expected_word in doc, (
        f"la docstring de register_session_lifecycle_tools() doit dire « {expected_word} » "
        f"pour {_EXPECTED_TOOL_COUNT} tools ; elle dit : {doc!r}"
    )
