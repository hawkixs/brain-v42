"""Provenance du corpus — qui a touché quelle entité.

Le header ``X-Brain-Agent`` est déclaré par le client, donc falsifiable : c'est
un signal d'hygiène, pas une frontière de sécurité — même posture que le
``client_key`` de session, « déclarée, pas prouvée ».

Module feuille volontairement : aucune dépendance MCP ni base de données, pour
qu'il soit importable depuis la couche transport comme depuis les services.
"""

from __future__ import annotations

import os
import uuid
from contextvars import ContextVar, Token

UNKNOWN_ACTOR = "unknown"
UNEXPANDED_ACTOR = "_unexpanded"

# Largeur de la colonne access_log.actor (VARCHAR(64)) : un événement plus
# long que ça fait échouer l'executemany en lot (PG 22001) et l'échec fait
# perdre tout le batch, pas juste l'événement fautif.
MAX_ACTOR_LENGTH = 64

# Préfixes des acteurs système qui se déclarent. On reconnaît la FAMILLE
# `dream-`, jamais un rail nommé : les trois runners câblés émettent
# `dream-codex-{phase}`, `dream-claude-{phase}` et `dream-agy-{phase}`, et tout
# rail futur suivra le même gabarit. Énumérer un seul rail — ce que faisait
# `("dream-codex-",)` — laissait les deux autres compter leurs relectures
# nocturnes comme des lectures HUMAINES.
#
# La garantie ne vient pas de ce tuple mais de
# `test_every_dream_rail_header_is_machine`, qui relit les runners et exige que
# chaque en-tête émis soit classé machine : un quatrième rail qui s'écarterait du
# gabarit fait rougir la suite au lieu de passer au travers.
#
# Public : le prédicat SQL de `db/session_derived_capture.py` est le MIROIR de
# `is_human_actor` — il importe ces constantes au lieu de les redéclarer, sans
# quoi les deux classifications ne divergeraient qu'à la lecture, sur certains
# chemins seulement (le mode de panne maison).
SYSTEM_ACTOR_PREFIXES = ("dream-",)
_SYSTEM_ACTOR_PREFIXES = SYSTEM_ACTOR_PREFIXES

# Acteurs machine HORS famille `dream-`, recensés PAR SITE D'APPEL le
# 2026-08-29 (ticket 6878077f). Des noms EXACTS, jamais un préfixe : `red-`
# avalerait les basenames humains (`red-games` lancé interactivement).
#
# - `red-shrik` : bot actif (`systemctl is-active` → active), `brain_search`
#   en boucle, se déclare via `red-shrik/src/shrik/mcp_client.py:83` ;
# - `antigravity` : le même client, déploiement agy
#   (`deploy/agy/settings.mcp.example.json`) ;
# - `red-lab-factory` : l'acteur que red-lab DOIT poser (ticket a3fa6696) —
#   pré-classé ici pour que le correctif cross-repo, le jour où il arrive, ne
#   fasse pas basculer ce trafic d'`unknown` (machine) vers un nom compté
#   humain : fermer un trou n'a pas à en ouvrir un ;
# - `pc-dev-red` : client scripté du PC dev, mesuré sur `brain_ticket_list`.
#
# Coût assumé, dans le sens conservateur : une session interactive lancée
# DEPUIS le répertoire d'un de ces services déclare le même basename et compte
# machine. L'erreur coûte en couverture humaine (un plancher), jamais en
# fausse écriture — le sens que 6878077f bénit.
SYSTEM_ACTOR_NAMES = frozenset(
    {
        "red-shrik",
        "antigravity",
        "red-lab-factory",
        "pc-dev-red",
    }
)
_NON_HUMAN = frozenset({UNKNOWN_ACTOR, UNEXPANDED_ACTOR, ""})

_current_actor: ContextVar[str] = ContextVar(
    "brain_v42_current_actor",
    default=UNKNOWN_ACTOR,
)


def normalize_agent(value: str | None) -> str:
    """Réduire un ``X-Brain-Agent`` brut en nom d'acteur propre.

    Les sessions Claude Code interactives envoient ``${PWD}``, que Claude Code
    expanse en chemin absolu du projet : on garde le basename. Les libellés de
    service statiques passent tels quels. Une session démon (pas de ``PWD``
    dans l'environnement) laisse le gabarit non expansé, qu'on effondre sur un
    seul seau plutôt que d'inventer un acteur par littéral. Le résultat est
    tronqué à ``MAX_ACTOR_LENGTH`` : la colonne ``access_log.actor`` est
    ``VARCHAR(64)`` et une valeur plus longue ferait échouer l'insert en lot
    pour tout le batch. La troncature est déterministe et préserve la
    classification humain/système — un nom de projet légitime trop long reste
    compté comme humain plutôt que de basculer silencieusement sur
    ``unknown``.
    """
    value = (value or "").strip()
    if not value:
        return UNKNOWN_ACTOR
    if "${" in value:
        return UNEXPANDED_ACTOR
    if value.startswith("/"):
        value = os.path.basename(value.rstrip("/")) or UNKNOWN_ACTOR
    return value[:MAX_ACTOR_LENGTH]


_MAX_SESSION_CHARS = 36

_current_session: ContextVar[str | None] = ContextVar(
    "brain_v42_current_session",
    default=None,
)


def normalize_session(value: str | None) -> str | None:
    """Réduire un ``X-Brain-Session`` brut en UUID canonique, ou ``None``.

    Seule la forme canonique minuscule est acceptée : la valeur sert de clé de
    jointure, et deux graphies de la même session produiraient deux lignes.
    Tout ce qui n'est pas un UUID — gabarit non expansé, libellé, valeur
    surdimensionnée — vaut ``None``, c'est-à-dire « pas de session déclarée ».
    """
    value = (value or "").strip()
    if not value or len(value) > _MAX_SESSION_CHARS:
        return None
    try:
        parsed = uuid.UUID(value)
    except (ValueError, AttributeError):
        return None
    return value if str(parsed) == value else None


_TRANSPORT_CHARS = 32
_HEX_DIGITS = frozenset("0123456789abcdef")

_current_transport: ContextVar[str | None] = ContextVar(
    "brain_v42_current_transport",
    default=None,
)


def normalize_transport(value: str | None) -> str | None:
    """Réduire un ``Mcp-Session-Id`` brut en identifiant de transport, ou ``None``.

    Domaine de clés SÉPARÉ de ``normalize_session``, et c'est délibéré.
    ``Mcp-Session-Id`` est frappé par le SERVEUR (``uuid4().hex`` dans
    ``streamable_http_manager``) : 32 hexadécimaux minuscules, sans tirets. Il
    identifie une CONNEXION, pas une conversation d'agent, et n'a aucun pendant
    dans les flux OTLP — il ne peut donc rien joindre. ``normalize_session``,
    elle, garde l'espace des sessions d'agent, seul domicile légitime d'une
    vraie jointure le jour où un client sait la déclarer.

    Fail-closed strict sur la forme : la valeur reste bornée même si la garde de
    transport saute en amont. La longueur est vérifiée AVANT le contenu — cet
    en-tête est une entrée non maîtrisée et rien n'oblige un appelant à être
    raisonnable.
    """
    value = (value or "").strip()
    if len(value) != _TRANSPORT_CHARS:
        return None
    return value if _HEX_DIGITS.issuperset(value) else None


def set_current_transport(transport: str | None) -> None:
    """Poser l'identifiant de transport pour la durée du contexte courant."""
    _current_transport.set(transport)


def get_current_transport() -> str | None:
    """Lire le transport courant. ``None`` hors contexte ou en mode sans état."""
    return _current_transport.get()


def set_current_session(session: str | None) -> None:
    """Poser la session pour la durée du contexte courant."""
    _current_session.set(session)


def get_current_session() -> str | None:
    """Lire la session courante. ``None`` hors contexte ou sans déclaration."""
    return _current_session.get()


def set_current_actor(actor: str) -> None:
    """Poser l'acteur pour la durée du contexte courant."""
    _current_actor.set(actor or UNKNOWN_ACTOR)


def get_current_actor() -> str:
    """Lire l'acteur courant. ``unknown`` hors contexte de requête."""
    return _current_actor.get()


def is_human_actor(actor: str | None) -> bool:
    """Vrai si l'acteur est une session humaine.

    Fail-closed sur les sentinelles ET sur la famille système : un acteur
    inconnu, non expansé ou préfixé `dream-` n'est PAS humain, donc ne peut pas
    faire franchir à une entité le seuil de maturité de PROMOTE.

    PORTÉE EXACTE, à ne pas surestimer — c'est ce que `test_promote_prepare`
    rappelle déjà au juge PROMOTE : ceci n'est pas une liste blanche d'humains.
    Un acteur humain est le basename du projet appelant, arbitraire par
    construction, donc inénumérable ; exiger qu'un humain se déclare casserait
    le cas légitime. Le bot d'un AUTRE projet qui pose son propre
    `X-Brain-Agent` reste donc compté comme humain. La garde couvre la famille
    `dream-` ET les acteurs machine RECENSÉS (`SYSTEM_ACTOR_NAMES`, par site
    d'appel, datés dans leur commentaire) — pas toute machine concevable : le
    reste du monde est humain par défaut, c'est le prix d'un espace de noms
    inénumérable, et il se paie en plancher de couverture, jamais en fausse
    écriture.
    """
    value = (actor or "").strip()
    if value in _NON_HUMAN:
        return False
    if value in SYSTEM_ACTOR_NAMES:
        return False
    return not value.startswith(SYSTEM_ACTOR_PREFIXES)


_call_depth: ContextVar[int] = ContextVar("brain_v42_call_depth", default=0)


def enter_call() -> Token[int]:
    """Entrer d'un niveau dans la chaîne d'appels de tools."""
    return _call_depth.set(_call_depth.get() + 1)


def exit_call(token: Token[int]) -> None:
    """Ressortir du niveau ouvert par ``enter_call``."""
    _call_depth.reset(token)


def is_outermost_call() -> bool:
    """Vrai au seul premier niveau d'imbrication.

    En profil ``compact`` la passerelle ``brain_call_tool`` ré-entre dans la
    chaîne de middlewares (mesuré, commit 58329a84) : ``on_call_tool`` se
    déclenche deux fois par appel client. Compter à tous les niveaux
    gonflerait le compteur x2 en production et x1 en profil natif.
    """
    return _call_depth.get() == 1
