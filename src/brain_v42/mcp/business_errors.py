"""Fait traverser les erreurs métier attendues à travers ``mask_error_details``.

Le serveur est construit avec ``mask_error_details=True`` : toute exception qui
s'échappe d'un tool est aplatie en ``Error calling tool 'X'``. C'est le bon
défaut pour les pannes internes (DSN, chemins, traces), mais il détruit aussi le
texte des gardes *fail-closed*, qui sont précisément rédigées pour être
actionnables. Mesuré en production le 2026-08-06 (ticket brain 40ab2ced) :

    brain_learn(project_key="projet-qui-nexiste-pas", ...)
    serveur : Unknown project 'projet-qui-nexiste-pas' — create it first
              (brain_set_project_context) or check the key (brain_list_projects)
    client  : Error calling tool 'brain_learn'

L'appelant n'apprend ni que c'est la clé le problème, ni laquelle il a passée,
ni quoi faire — il réessaie donc à l'identique, ou abandonne la capitalisation.

``format_error`` (``mcp.tools.formatters``) est le canal déjà sanctionné, mais il
ne couvre que les échecs qu'un tool attrape *explicitement*. Les gardes comme
``require_known_project`` lèvent depuis le fond d'un service et s'échappent sans
être converties ; le lifecycle de session fait de même. Ce module ferme ce trou
en un seul point, pour tous les tools enregistrés.

Pourquoi pas un middleware
--------------------------
MESURÉ, pas supposé : ``FastMCP.call_tool`` applique la chaîne de middlewares
puis ré-entre dans ``call_tool(run_middleware=False)``, et c'est cette passe
interne qui porte le ``try/except`` de masquage. Un ``on_call_tool`` est donc
structurellement AU-DESSUS du masquage : il ne voit jamais l'exception brute,
seulement le ``ToolError`` générique déjà produit. L'interception doit avoir lieu
à l'intérieur de ``tool._run``, donc dans la fonction du tool elle-même.

``FunctionTool.run`` lit ``self.fn`` à chaque appel et en dérive son type adapter,
si bien qu'envelopper ``fn`` avec ``functools.wraps`` préserve le schéma d'entrée
publié (garanti par ``test_wrapping_preserves_tool_input_schema``).

Choix explicite : LISTE BLANCHE, pas passe-plat
------------------------------------------------
Remonter un message d'exception arbitraire divulguerait des chemins, des
identifiants de connexion ou de la structure interne. Seules traversent les
familles ci-dessous, retenues parce que leur message est (a) rédigé pour
l'appelant et (b) dépourvu d'interne. Tout le reste reste masqué — le défaut est
le refus.

Exclusions délibérées :

- ``PlanScanPathError`` — décrit un refus de scan de système de fichiers.
- Les erreurs d'ops/admin (``LegacyGraphImportBlocked``, ``RepairSafetyError``,
  ``AutomationCleanupError``, ``OwnershipLostError``) — jamais sur un chemin de
  tool LLM, et leur texte décrit l'interne.

Hors périmètre car FastMCP les traite déjà : toute sous-classe de ``FastMCPError``
traverse intacte par construction (branche ``except FastMCPError: raise``). C'est
le cas de ``DreamProjectAuthorizationError``, qui dérive de ``AuthorizationError``
et porte volontairement un message générique, la raison précise restant en
attribut — un refus d'autorisation détaillé serait un oracle d'énumération.
"""

from __future__ import annotations

import functools
from typing import Any

from fastmcp import FastMCP
from fastmcp.exceptions import ToolError
from fastmcp.tools.function_tool import FunctionTool

from brain_v42.models.brain_session import BrainSessionError
from brain_v42.services.consolidation import ConsolidationEntityNotFoundError
from brain_v42.services.entity_maintenance_service import UnknownEntityTypeError
from brain_v42.services.feature_creation_service import FeatureCreationError
from brain_v42.services.feature_service import FeatureStateConflictError
from brain_v42.services.proposal_service import ProposalServiceError
from brain_v42.services.roadmap_service import ProjectFocusError
from brain_v42.services.ticket_service import TicketError

#: Familles d'exceptions dont le message est destiné à l'appelant.
#: Ajouter une entrée est une décision de divulgation : vérifier que le texte
#: ne peut contenir ni chemin, ni DSN, ni détail de schéma.
SURFACED_BUSINESS_ERRORS: tuple[type[Exception], ...] = (
    TicketError,
    BrainSessionError,
    FeatureCreationError,
    FeatureStateConflictError,
    ProposalServiceError,
    ProjectFocusError,
    UnknownEntityTypeError,
    ConsolidationEntityNotFoundError,
)

_SURFACED_MARKER = "__brain_business_errors_surfaced__"


def _wrap(fn: Any) -> Any:
    """Convertir les familles de la liste blanche en ``ToolError`` lisible."""

    @functools.wraps(fn)
    async def surfaced(*args: Any, **kwargs: Any) -> Any:
        try:
            return await fn(*args, **kwargs)
        except SURFACED_BUSINESS_ERRORS as exc:
            raise ToolError(str(exc)) from exc

    setattr(surfaced, _SURFACED_MARKER, True)
    return surfaced


async def surface_business_errors(mcp: FastMCP) -> tuple[str, ...]:
    """Envelopper tout tool enregistré et rendre les noms effectivement traités.

    Idempotent : un tool déjà enveloppé est laissé tel quel, si bien qu'un
    double appel (ou un réenregistrement) ne superpose pas les couches.

    ``_list_tools()`` est utilisé plutôt que ``list_tools()`` parce qu'il rend
    les tools bruts sans passer par les transforms de catalogue — MESURÉ : sous
    le profil ``compact``, ``list_tools()`` n'exposerait que les passerelles et
    laisserait la quasi-totalité des tools réels sans conversion.

    Les tools qui ne sont pas des ``FunctionTool`` n'exposent pas de fonction
    Python à envelopper et sont ignorés.
    """
    wrapped: list[str] = []
    for tool in await mcp._list_tools():
        if not isinstance(tool, FunctionTool) or getattr(tool.fn, _SURFACED_MARKER, False):
            continue
        tool.fn = _wrap(tool.fn)
        wrapped.append(tool.name)
    return tuple(wrapped)
