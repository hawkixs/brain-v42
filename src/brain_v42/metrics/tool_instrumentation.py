"""Instrumente les tools enregistrés, sans muter ``mcp.tool`` (ticket c352eaaa).

Le câblage historique remplaçait ``FastMCP.tool`` par un wrapper au moment de
l'enregistrement (``brain_tools.register_tools``). Trois défauts, tous cités par
la spec §1 : couplage à ``metrics_collector`` passé à UNE fonction
d'enregistrement particulière, dépendance à l'ordre de déclaration (un tool
enregistré avant le patch n'était pas compté), et mutation d'une méthode d'objet
tiers.

Envelopper les tools APRÈS enregistrement supprime les trois. Même mécanisme que
``brain_v42.mcp.business_errors`` : ``FunctionTool.run`` lit ``self.fn`` à chaque
appel, donc remplacer ``fn`` par une enveloppe transparente à la signature
(``functools.wraps``) instrumente le tool sans toucher au schéma publié.

Pourquoi pas un middleware ``on_call_tool``
-------------------------------------------
Le ticket proposait un middleware, la re-mesure Q2 ayant montré qu'il voit aussi
le nom du tool réel derrière la passerelle compact. C'est vrai, mais insuffisant,
et MESURÉ : ``FastMCP.call_tool`` applique les middlewares PUIS ré-entre dans
``call_tool(run_middleware=False)``, qui porte le ``try/except`` de masquage. Un
middleware est donc au-dessus du masquage et ne reçoit que le ``ToolError``
générique — la contrainte 2 du ticket (« préserver EXACTEMENT la capture
d'AuthorizationError ») serait tenue par chance (``AuthorizationError`` est un
``FastMCPError``, relancé tel quel) mais le journal ``exception_type`` dégénérerait
en « ToolError » pour toute autre panne, perdant le diagnostic qu'il existe pour
donner. Envelopper la fonction voit l'exception AVANT masquage.

La contrainte 1 (« ignorer les noms de passerelle, sinon double comptage ») est
satisfaite PAR CONSTRUCTION, sans liste noire à maintenir : ``_list_tools()`` rend
le registre brut, où ``brain_call_tool`` et ``brain_find_tool`` n'existent pas —
ils ne vivent que dans la vue transformée de ``list_tools()``. Un renommage futur
des passerelles ne peut donc pas rouvrir le double comptage.
"""

from __future__ import annotations

from typing import Any

from fastmcp import FastMCP
from fastmcp.tools.function_tool import FunctionTool

from brain_v42.metrics.collector import MetricsCollector
from brain_v42.metrics.instrument import instrument_tool

_INSTRUMENTED_MARKER = "__brain_metrics_instrumented__"


def _wrap(fn: Any, collector: MetricsCollector, tool_name: str) -> Any:
    """Appliquer ``instrument_tool`` et marquer la fonction comme déjà traitée.

    Seul le POINT D'APPLICATION change : le décorateur lui-même est réutilisé tel
    quel, ce qui satisfait littéralement la contrainte 2 du ticket (« préserver
    EXACTEMENT la capture d'AuthorizationError et la mesure de latence ») plutôt
    que de la ré-implémenter à l'identique et d'espérer.
    """
    instrumented = instrument_tool(collector, tool_name)(fn)
    setattr(instrumented, _INSTRUMENTED_MARKER, True)
    return instrumented


async def instrument_registered_tools(
    mcp: FastMCP,
    collector: MetricsCollector,
) -> tuple[str, ...]:
    """Instrumenter tout tool enregistré et rendre les noms effectivement traités.

    Idempotent : un tool déjà instrumenté est laissé tel quel, si bien qu'un
    double appel ne superpose pas les enveloppes et ne double pas les compteurs.
    """
    instrumented: list[str] = []
    for tool in await mcp._list_tools():
        if not isinstance(tool, FunctionTool) or getattr(tool.fn, _INSTRUMENTED_MARKER, False):
            continue
        tool.fn = _wrap(tool.fn, collector, tool.name)
        instrumented.append(tool.name)
    return tuple(instrumented)
