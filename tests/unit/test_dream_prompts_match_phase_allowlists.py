"""Aucun prompt n'appelle un outil que sa phase n'a pas le droit d'utiliser.

Le pare-feu de capacités par phase (SEC1a) est resté INERTE de sa livraison du
2026-07-16 jusqu'à l'armement du 2026-08-10. Pendant treize mois-agent, la
liste blanche d'une phase et ce que son prompt lui demandait d'appeler ont pu
diverger sans qu'aucune nuit ne s'en aperçoive : rien ne refusait un appel.

Armé, un appel hors liste lève `DreamProjectAuthorizationError`. C'est
fail-closed et bruyant — la phase échoue, l'unité rougit — donc pas un mode de
panne silencieux. Mais c'est une nuit perdue pour un écart que ce test attrape
en 30 ms.

LA RÈGLE DE LECTURE, et pourquoi elle sépare vraiment. Un SITE D'APPEL porte une
parenthèse ouvrante : `brain_list(entity_type=...)`. Une INTERDICTION s'écrit
sans : « Do NOT call brain_learn. » Les quatre prompts qui citent `brain_learn`
hors de leur liste le citent tous pour l'interdire, et `phase_promote.md` liste
`brain_update`, `brain_accept_adr` et `brain_delete` dans la même intention.
Mesuré le 2026-08-10 : la règle laisse zéro faux positif sur les six prompts.

Ce que l'armement change vraiment, et qui vaut d'être nommé : ces interdictions
étaient de la PROSE, que le modèle pouvait ignorer. Elles sont maintenant
adossées à un refus côté serveur. Le prompt et la liste blanche disent la même
chose — ce test est ce qui les garde d'accord.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from brain_v42.mcp.dream_capabilities import DREAM_PHASE_TOOL_ALLOWLISTS

REPO_ROOT = Path(__file__).resolve().parents[2]
PROMPT_DIR = REPO_ROOT / "scripts" / "dream"

# Site d'appel = nom d'outil suivi d'une parenthèse ouvrante.
_CALL_SITE = re.compile(r"\b(brain_[a-z0-9_]+)\s*\(")
# Toute mention, appel ou non. Sert à la garde de non-régression plus bas.
_ANY_MENTION = re.compile(r"\b(brain_[a-z][a-z0-9_]*)")
_PROHIBITION = re.compile(r"\bNOT\b|\bnever\b|\bNEVER\b|[Ff]orbidden|\bdo not\b")


def _prompt(phase: str) -> str:
    return (PROMPT_DIR / f"phase_{phase}.md").read_text(encoding="utf-8")


@pytest.mark.parametrize("phase", sorted(DREAM_PHASE_TOOL_ALLOWLISTS))
def test_every_call_site_is_inside_the_phase_allowlist(phase: str) -> None:
    called = set(_CALL_SITE.findall(_prompt(phase)))
    allowed = set(DREAM_PHASE_TOOL_ALLOWLISTS[phase])

    assert called <= allowed, (
        f"phase {phase} : le prompt appelle {sorted(called - allowed)}, "
        "qui n'est pas dans sa liste blanche. Armé, le serveur refusera l'appel "
        "et la phase échouera — bruyamment, mais la nuit sera perdue."
    )


@pytest.mark.parametrize("phase", sorted(DREAM_PHASE_TOOL_ALLOWLISTS))
def test_every_prompt_actually_calls_something(phase: str) -> None:
    """Garde du test lui-même.

    Un prompt réécrit dans une autre syntaxe — sans parenthèses — rendrait
    l'assertion ci-dessus vraie sur l'ensemble vide, donc verte sur du vide.
    C'est la forme de faux témoin que ce dépôt a déjà rencontrée trois fois.
    """
    assert _CALL_SITE.findall(_prompt(phase)), (
        f"phase {phase} : aucun site d'appel détecté. Soit le prompt a changé de "
        "syntaxe et la règle de lecture est à revoir, soit il n'appelle plus rien."
    )


@pytest.mark.parametrize("phase", sorted(DREAM_PHASE_TOOL_ALLOWLISTS))
def test_tools_mentioned_outside_the_allowlist_are_only_prohibitions(phase: str) -> None:
    """Un outil hors liste ne peut apparaître que pour être interdit.

    Sans cette assertion, un prompt pourrait demander un outil hors liste dans
    une phrase en prose — « use brain_delete when… » — sans parenthèse, et le
    test principal ne le verrait pas.
    """
    text = _prompt(phase)
    allowed = set(DREAM_PHASE_TOOL_ALLOWLISTS[phase])
    known_tools = {tool for tools in DREAM_PHASE_TOOL_ALLOWLISTS.values() for tool in tools}
    lines = text.splitlines()

    for tool in sorted(set(_ANY_MENTION.findall(text)) & known_tools - allowed):
        for index, line in enumerate(lines):
            if tool not in line:
                continue
            # Fenêtre, pas ligne unique : l'interdiction s'écrit aussi comme un
            # EN-TÊTE DE SECTION suivi de la liste — `## Forbidden tools` puis
            # les noms sur la ligne d'après. Un test sur la ligne seule aurait
            # exigé de réécrire le prompt pour satisfaire le test, ce qui est
            # la mauvaise direction.
            window = "\n".join(lines[max(0, index - 2) : index + 1])
            assert _PROHIBITION.search(window), (
                f"phase {phase} : `{tool}` est hors liste blanche et rien dans les "
                f"trois lignes qui le portent ne l'interdit :\n  {line.strip()}"
            )
