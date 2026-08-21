"""Middleware de provenance — pose l'acteur courant pour tout appel de tool.

Installé INCONDITIONNELLEMENT. La provenance ne doit pas dépendre de
l'activation des métriques, qui ne sont posées que si un collecteur existe, et
une provenance silencieusement muette est pire que pas de provenance.

Ne porte pas les métriques. Depuis le ticket c352eaaa elles ne passent plus par
un monkey-patch de `mcp.tool` : `brain_v42.metrics.tool_instrumentation`
enveloppe les tools déjà enregistrés depuis `_run_mcp`. Ce point d'application a
été retenu CONTRE le middleware que proposait le ticket, pour deux raisons
mesurées — un middleware est au-dessus du masquage de `call_tool` et ne verrait
que le `ToolError` générique (le journal `exception_type` dégénérerait), et
`_list_tools()` exclut les passerelles par construction, là où un middleware
aurait exigé une liste noire de noms. Ne pas rouvrir « déplacer les métriques
ici » : la question a été tranchée, pas reportée.

Re-mesure Q2 du 2026-08-06, qui reste vraie pour CE middleware : en profil
`compact`, la passerelle `brain_call_tool` ré-entre dans la chaîne
(`FastMCP.call_tool`, `run_middleware=True` par défaut), donc `on_call_tool` voit
AUSSI le nom du tool réel — il se déclenche deux fois par appel compact (mesuré,
commit 58329a84). C'était inoffensif tant que le middleware ne faisait que poser
l'acteur, le même deux fois de suite. Ça ne l'est plus depuis que `_report`
existe : compter à tous les niveaux donnerait x2 en `compact`, qui est le profil
de production, et x1 en profil natif — deux chiffres incomparables. D'où la garde
de profondeur (`enter_call`/`exit_call`/`is_outermost_call`), qui réserve
`_report` au seul niveau extérieur.
"""

from __future__ import annotations

from typing import Any

import structlog
from fastmcp.server.dependencies import get_http_headers, get_http_request
from fastmcp.server.middleware import Middleware

from brain_v42.mcp.activity_reporter import get_activity_reporter
from brain_v42.mcp.session_autoopen import get_session_autoopener
from brain_v42.provenance import (
    UNKNOWN_ACTOR,
    enter_call,
    exit_call,
    is_outermost_call,
    normalize_agent,
    normalize_session,
    normalize_transport,
    set_current_actor,
    set_current_session,
    set_current_transport,
)

# ``get_http_headers()`` retire ``mcp-session-id`` de ce qu'il rend : l'en-tête
# figure en dur dans son ``exclude_headers``. Il faut le redemander nommément,
# sinon la lecture renvoie ``None`` en silence et le panneau reste anonyme sans
# qu'aucun test ne s'en aperçoive.
_TRANSPORT_HEADER = "mcp-session-id"

#: Couples (pair, User-Agent) déjà dénoncés. Un client anonyme appelle ici une
#: fois par minute depuis des semaines : la réponse tient dans la PREMIÈRE
#: ligne, et journaliser la répétition ne ferait qu'enterrer la découverte.
_seen_unidentified: set[tuple[str, str]] = set()
#: Plafond dur sur ce mémo. Le pair est déclaré par le réseau et le User-Agent
#: par le client : sans borne, un appelant qui varie son User-Agent ferait
#: croître ce set sans fin dans un processus long-vivant.
_MAX_UNIDENTIFIED_TRACKED = 32
_UNKNOWN_PEER = "?"


logger = structlog.get_logger(__name__)


class ProvenanceMiddleware(Middleware):
    """Pose l'acteur et la session déclarés, et signale l'appel une seule fois."""

    async def on_call_tool(self, context: Any, call_next: Any) -> Any:
        headers = get_http_headers(include={_TRANSPORT_HEADER}) or {}
        actor = normalize_agent(headers.get("x-brain-agent"))
        session = normalize_session(headers.get("x-brain-session"))
        # NE PAS remplacer par ``Context.session_id`` : en mode sans état il
        # forge un ``uuid4()`` PAR REQUÊTE (mesuré : trois valeurs pour trois
        # appels d'un même client), et cette forme-là passe ``normalize_session``.
        # On obtiendrait une ligne de panneau par appel de tool, toutes
        # présentées comme des sessions distinctes.
        transport = normalize_transport(headers.get(_TRANSPORT_HEADER))
        set_current_actor(actor)
        set_current_session(session)
        set_current_transport(transport)

        token = enter_call()
        try:
            if is_outermost_call():
                await self._auto_open_session()
                self._report(actor, session, transport)
                if actor == UNKNOWN_ACTOR:
                    self._name_unidentified_client(context)
            return await call_next(context)
        finally:
            exit_call(token)

    async def _auto_open_session(self) -> None:
        """Ouvrir la session traçante de cette connexion, AVANT l'outil.

        Trois choses tiennent dans ces cinq lignes, et aucune n'est
        interchangeable :

        - **Avant ``call_next``**, pas après, et pas en tâche détachée. La
          capture borne les artefacts par ``created_at >= started_at`` : une
          session ouverte après l'outil n'attribuerait rien de ce que l'outil
          vient de créer. C'est ce qui distingue ce chemin de ``_report``, qui
          n'observe rien qu'il doive précéder.
        - **Sous ``is_outermost_call()``**, donc une fois par appel client et
          non deux en profil ``compact`` (mesuré, commit 58329a84). La mémo de
          l'ouvreur ne suffirait pas : elle masquerait le second tir au lieu de
          l'empêcher, et le compteur ``memoized`` mentirait d'un facteur deux.
        - **``ensure_open`` ne lève jamais**, par contrat. Aucun ``try`` ici
          serait donc redondant — mais l'absence de garde locale est un choix
          qui repose entièrement sur ce contrat, tenu par un test de panne
          injectée avec témoin négatif.
        """
        autoopener = get_session_autoopener()
        if autoopener is None:
            return
        await autoopener.ensure_open()

    def _name_unidentified_client(self, context: Any) -> None:
        """Dénoncer une fois un appelant qui ne se déclare pas.

        C'est la seule mesure que le serveur puisse faire LUI-MÊME. Les
        instruments extérieurs ont tous échoué sur ce trafic (mesuré le
        2026-08-12) : `ss` échantillonné rate structurellement un appel de
        4,4 ms, `ss -E` voit l'événement mais plus le processus qui l'a ouvert,
        et `access_log` était vide. Ici l'information est présente par
        construction, au moment où la requête arrive.

        Le ``except`` est TOTAL et étroitement scopé, pour la même raison que
        ``_report`` : ce chemin s'exécute sur chaque appel anonyme d'un process
        partagé, et une sonde ne peut pas faire tomber ce qu'elle observe.
        """
        try:
            headers = get_http_headers(include={"user-agent"}) or {}
            user_agent = (headers.get("user-agent") or "").strip()[:120]
            client = getattr(get_http_request(), "client", None)
            peer = getattr(client, "host", None) or _UNKNOWN_PEER
            key = (peer, user_agent)
            if key in _seen_unidentified:
                return
            if len(_seen_unidentified) >= _MAX_UNIDENTIFIED_TRACKED:
                return
            _seen_unidentified.add(key)
            logger.warning(
                "provenance.unidentified_client",
                peer=peer,
                port=getattr(client, "port", None),
                user_agent=user_agent,
                tool=getattr(getattr(context, "message", None), "name", None),
            )
        except Exception:
            logger.debug("provenance.unidentified_probe_failed", exc_info=True)

    def _report(self, actor: str, session: str | None, transport: str | None = None) -> None:
        """Signaler l'appel au sidecar métriques, en feu-et-oubli borné.

        Le ``except`` est délibérément TOTAL, et il est étroitement scopé à l'émetteur :
        ce chemin s'exécute à chaque appel de tool, dans un process partagé et
        long-vivant, et il est armé en production. Une exception qui en sortait
        remontait jusqu'à ``on_call_tool`` — qui n'a qu'un ``finally`` — et tuait
        l'appel. Un canal d'OBSERVATION ne peut pas être un point de défaillance de
        l'opération observée : au pire on perd une ligne de panneau.

        Le journal est en ``warning`` et non ``debug`` : contrairement au refus du
        récepteur, qui peut se répéter à chaque appel, une exception ici signale un
        défaut de l'émetteur lui-même, pas un état du réseau.
        """
        reporter = get_activity_reporter()
        if reporter is None:
            return
        try:
            reporter.report(actor, session, transport)
        except Exception:
            logger.warning("activity_reporter.report_failed", exc_info=True)
