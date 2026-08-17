"""Émetteur d'observations d'activité vers le sidecar métriques.

Feu-et-oubli borné : le registre vit dans un autre processus, et un sidecar
lent ou arrêté ne doit jamais ralentir ni casser un appel de tool. Sous
saturation LOCALE — tous les créneaux en vol pris — on préfère perdre une
observation que faire attendre l'appelant ; cette perte-là, et elle seule, est
comptée dans ``dropped``.

Ce n'est ni le seul mode de perte ni le dominant. Le récepteur peut refuser
l'observation (404 quand la route n'est pas enregistrée, 403, 413, 415, 400,
503…), et ``httpx`` ne lève pas sur un statut d'erreur : ces refus reviennent
par le chemin nominal, pas par le ``except``. Ils sont donc lus explicitement,
comptés à part dans ``refused`` et journalisés avec leur seul statut — jamais
le corps de la réponse, qui peut renvoyer l'observation en écho.

Aucun rejeu, aucun backoff : le contrat reste feu-et-oubli. Un refus est
rendu OBSERVABLE, pas rattrapé.
"""

from __future__ import annotations

import asyncio
import json

import httpx
import structlog

from brain_v42.config import get_settings

logger = structlog.get_logger(__name__)

_reporter: ActivityReporter | None = None


def _is_a_decade(count: int) -> bool:
    """Vrai à la 1re, la 10e, la 100e… occurrence, jamais entre les deux.

    Sur un compteur strictement croissant d'un en un, la garde ne peut pas
    sauter une décade.

    Écrite en entiers plutôt qu'avec ``log10``. MESURÉ, contre la tentation
    de justifier ce choix trop fort : la variante flottante ne rate AUCUNE
    puissance de 10 jusqu'à 10^24 — elle produit des faux positifs sur leurs
    voisins (999999999999999 déclaré décade) à partir de 10^15. Sur un
    compteur incrémenté d'un par appel de tool, cet écart est hors d'atteinte,
    et la suite ne l'attrape pas : substituer ``log10`` ici laisse les tests
    verts. Le choix de l'entier tient donc à ce qu'il est exact et sans
    import, pas à un bug qu'on aurait constaté.
    """
    if count < 1:
        return False
    while count % 10 == 0:
        count //= 10
    return count == 1


class ActivityReporter:
    def __init__(
        self,
        url: str,
        timeout: float = 1.0,
        max_in_flight: int = 8,
    ) -> None:
        self._url = url
        self._client = httpx.AsyncClient(timeout=timeout)
        self._max_in_flight = max_in_flight
        self._pending: set[asyncio.Task[None]] = set()
        # Deux modes de perte, deux compteurs : les confondre rendrait « aucun
        # appel » et « toutes les observations refusées » indiscernables.
        self.dropped = 0  # contre-pression locale : créneaux en vol saturés
        self.refused = 0  # le récepteur a répondu autre chose qu'un 2xx
        # Ces deux compteurs vivent dans le processus MCP ; le panneau lit le
        # registre du SIDECAR, un autre processus. Ils n'atteignaient donc
        # aucun humain. Et ils ne PEUVENT pas voyager par le POST qu'ils
        # mesurent : sous 404 permanent — le scénario même qui les rend
        # utiles — ce POST est précisément celui qui est refusé. Le journal
        # est le seul canal honnête.
        #
        # Une ligne par perte est exclue : c'est le chemin chaud de TOUT appel
        # de tool, et le refus mesuré est PERMANENT, pas transitoire.
        #
        # Mais « une seule ligne, jamais plus » laisserait l'AMPLEUR invisible
        # — un opérateur ne saurait pas distinguer trois pertes d'un million.
        # Le décompte ne peut pas non plus attendre la fermeture : `close()`
        # n'est câblé NULLE PART en production (voir get_activity_reporter,
        # « le client meurt avec le processus »), un résumé à l'arrêt serait
        # du code mort déguisé en mesure.
        #
        # D'où l'escalade par décade : on parle à la 1re, la 10e, la 100e…
        # perte. Bornée en log10 — quinze lignes pour mille milliards — et
        # l'ordre de grandeur reste toujours lisible.
        self._warned_refusals: set[int] = set()

    def report(
        self,
        actor: str,
        session_id: str | None,
        transport: str | None = None,
    ) -> None:
        """Signaler un appel client. Ne bloque jamais, ne lève jamais.

        ``transport`` par défaut à ``None`` pour que les appelants à deux
        arguments restent valides : le mode sans état ne frappe aucun
        identifiant de connexion, et cette absence doit rester dicible.
        """
        if len(self._pending) >= self._max_in_flight:
            self.dropped += 1
            if _is_a_decade(self.dropped):
                logger.warning(
                    "activity_reporter.dropped",
                    dropped=self.dropped,
                    max_in_flight=self._max_in_flight,
                )
            return
        observation: dict[str, object] = {"actor": actor, "calls": 1}
        if session_id is not None:
            observation["session"] = session_id
        # Clé ABSENTE plutôt que ``null`` : le décodeur distingue « non
        # déclaré » de « déclaré vide », et un ``null`` sur le fil ferait
        # croire à une mesure là où il n'y en a pas.
        if transport is not None:
            observation["transport"] = transport
        body = json.dumps({"observations": [observation]})
        # Référence retenue dans _pending : sans elle, le GC peut ramasser la
        # tâche avant son exécution (le loop ne détient qu'une weakref).
        task = asyncio.create_task(self._post(body))
        self._pending.add(task)
        task.add_done_callback(self._pending.discard)

    async def _post(self, body: str) -> None:
        try:
            response = await self._client.post(
                self._url,
                content=body,
                headers={"Content-Type": "application/json"},
            )
            if not response.is_success:
                # ``httpx`` ne lève pas sur 4xx/5xx : sans cette lecture, un
                # refus revient par le chemin nominal et disparaît sans trace.
                # Le cas mesuré est permanent, pas transitoire : un bind non
                # loopback du sidecar n'enregistre pas ``/v1/client-activity``,
                # donc chaque POST reçoit 404, la moitié « brain » du panneau
                # reste vide pour toujours et toute la chaîne se déclare saine.
                # Statut seul, jamais ``response.text`` : le corps d'un refus
                # est une entrée non maîtrisée, et beaucoup de récepteurs y
                # renvoient la requête en écho — UUID de session compris.
                self.refused += 1
                # Une signature INÉDITE parle toujours : un 503 après mille
                # 404 est un fait neuf, et attendre la décade suivante le
                # noierait. Sinon, même escalade que la contre-pression.
                first_of_its_kind = response.status_code not in self._warned_refusals
                self._warned_refusals.add(response.status_code)
                if first_of_its_kind or _is_a_decade(self.refused):
                    logger.warning(
                        "activity_reporter.refused",
                        status=response.status_code,
                        refused=self.refused,
                    )
        except Exception as exc:
            # Type seul, jamais ``exc_info``. Le rendu d'exception de la chaîne
            # de production (rich, via ConsoleRenderer) affiche les variables
            # locales de chaque cadre : ``body`` y recopie l'observation, UUID
            # de session compris, et le chemin des cadres nomme le projet. Un
            # seul POST raté écrivait ainsi 456 lignes — sur le chemin chaud de
            # TOUT appel de tool, et pour un émetteur qui promet de ne jamais
            # ralentir l'appelant. Le type suffit à diagnostiquer un sidecar mort.
            logger.debug("activity_reporter.post_failed", error=type(exc).__name__)

    async def drain(self) -> None:
        """Attendre les émissions en vol. Réservé aux tests et à l'arrêt.

        Retire elle-même les tâches attendues de ``_pending`` plutôt que de
        compter sur leur ``done_callback`` (``self._pending.discard``) pour
        le faire. Sur Python 3.12+, ``asyncio.gather()`` traite les futures
        déjà ``done()`` de façon eager : attendre une tâche déjà terminée ne
        cède jamais la main à la boucle événementielle, donc si le callback
        n'a pas encore eu son tour (il demande un second tour de boucle),
        `while self._pending: await asyncio.gather(...)` boucle indéfiniment
        sans jamais laisser ce callback s'exécuter — livelock à 100% CPU qui
        ne rend jamais la main, même à un `asyncio.wait_for` englobant.

        La boucle continue tant que `_pending` n'est pas vide pour couvrir
        les émissions lancées pendant l'attente elle-même.
        """
        while self._pending:
            batch = tuple(self._pending)
            await asyncio.gather(*batch, return_exceptions=True)
            self._pending.difference_update(batch)

    async def close(self) -> None:
        await self.drain()
        await self._client.aclose()


def set_activity_reporter(reporter: ActivityReporter | None) -> None:
    """Poser un émetteur explicite. Réservé aux tests."""
    global _reporter
    _reporter = reporter


def get_activity_reporter() -> ActivityReporter | None:
    """Renvoyer l'émetteur, construit à la première utilisation.

    Construction paresseuse plutôt que câblage dans le cycle de vie du serveur :
    `mcp` est bâti au niveau module (voir le commentaire de `mcp/server.py:170`),
    et la première émission a lieu dans une boucle déjà tournante. Aucune
    fermeture n'est câblée — le client meurt avec le processus, et un
    `aclose()` à l'arrêt n'apporterait rien à un émetteur feu-et-oubli.

    Ne lève jamais : l'appelant est le middleware de provenance, sur le
    chemin de TOUT appel de tool. Une résolution de settings ou une
    construction de client qui échoue est traitée comme une indisponibilité
    (renvoie ``None``) plutôt que de casser l'appel en cours.
    """
    global _reporter
    if _reporter is None:
        try:
            settings = get_settings()
            if not settings.client_activity_reporting_enabled:
                return None
            _reporter = ActivityReporter(url=settings.client_activity_url)
        except Exception as exc:
            # Type seul, même raison qu'en ``_post`` : les cadres traversés ici
            # sont ceux de la construction des settings, dont les variables
            # locales portent la configuration (DSN compris). Et l'échec se
            # répète à chaque appel de tool tant que la résolution échoue,
            # puisque ``_reporter`` reste ``None``.
            logger.debug("activity_reporter.unavailable", error=type(exc).__name__)
            return None
    return _reporter
