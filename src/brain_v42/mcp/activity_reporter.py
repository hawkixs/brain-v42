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
import contextlib
import json

import httpx
import structlog

from brain_v42.config import get_settings
from brain_v42.metrics.client_observation import (
    MAX_CALLS_PER_OBSERVATION,
    MAX_OBSERVATION_BYTES,
    MAX_OBSERVATIONS,
)

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


_ObservationKey = tuple[str, str | None, str | None]

# Marge sous la borne du RÉCEPTEUR, pas une valeur choisie : l'enveloppe
# ``{"observations":[…]}``, les virgules, et l'observation immédiate qui
# s'ajoute au tampon au moment de l'émission doivent tenir dedans.
_BATCH_BYTE_BUDGET = MAX_OBSERVATION_BYTES - 1_024

# Le tampon garde une place libre : l'observation qui déclenche l'émission
# s'ajoute au lot, et le total doit rester sous la borne du décodeur.
_MAX_BUFFERED = MAX_OBSERVATIONS - 1


def _observation(key: _ObservationKey, calls: int) -> dict[str, object]:
    actor, session_id, transport = key
    observation: dict[str, object] = {"actor": actor, "calls": calls}
    # Clé ABSENTE plutôt que ``null`` : le décodeur distingue « non déclaré »
    # de « déclaré vide », et un ``null`` sur le fil ferait croire à une mesure
    # là où il n'y en a pas.
    if session_id is not None:
        observation["session"] = session_id
    if transport is not None:
        observation["transport"] = transport
    return observation


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
        # Le décompte ne peut pas non plus n'exister qu'à la fermeture :
        # `close_activity_reporter` est câblé dans `app_lifecycle` depuis le
        # lot d5e4bd73, mais un arrêt brutal (kill, OOM) ne le joue pas — un
        # résumé d'arrêt SEUL serait une mesure qui disparaît avec ses pires
        # scénarios.
        #
        # D'où l'escalade par décade : on parle à la 1re, la 10e, la 100e…
        # perte. Bornée en log10 — quinze lignes pour mille milliards — et
        # l'ordre de grandeur reste toujours lisible.
        self._warned_refusals: set[int] = set()
        # Tampon de coalescence. La borne en vol n'est pas relevée : elle
        # protège le sidecar. Ce qui change, c'est ce qu'on fait DERRIÈRE elle
        # — agréger au lieu de jeter. Le format de fil portait déjà les deux
        # leviers nécessaires et aucun n'était utilisé : un lot
        # (``MAX_OBSERVATIONS``) et un compteur (``MAX_CALLS_PER_OBSERVATION``),
        # alors que ``calls`` était en dur à 1.
        self._buffer: dict[_ObservationKey, int] = {}
        self._buffer_bytes = 0
        # Troisième compteur, distinct des deux autres : ``coalesced`` mesure ce
        # qui aurait été PERDU avant ce correctif. Le confondre avec ``dropped``
        # rendrait le correctif invisible dans ses propres métriques.
        self.coalesced = 0

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
        key: _ObservationKey = (actor, session_id, transport)
        # ``try`` TOTAL : tout ce qui est ajouté ici hérite de la promesse de
        # l'émetteur — ne jamais casser l'appel observé. Une panne du tampon
        # dégrade en perte COMPTÉE, jamais en exception remontée à l'appelant.
        try:
            if len(self._pending) < self._max_in_flight:
                self._emit(self._take_buffer() + [_observation(key, 1)])
                return
            if self._coalesce(key):
                self.coalesced += 1
                return
        except Exception:  # noqa: BLE001 - dégradation en perte comptée, jamais en panne
            logger.debug("activity_reporter.coalesce_failed")
        self._count_drop()

    def _count_drop(self) -> None:
        self.dropped += 1
        if _is_a_decade(self.dropped):
            logger.warning(
                "activity_reporter.dropped",
                dropped=self.dropped,
                max_in_flight=self._max_in_flight,
            )

    def _coalesce(self, key: _ObservationKey) -> bool:
        """Replier une observation dans le tampon. Faux si une borne est atteinte.

        Les deux bornes sont celles du DÉCODEUR, importées et non recopiées :
        franchir la borne d'octets ferait refuser le lot ENTIER en ``413``, donc
        échangerait la perte d'UNE observation contre celle de soixante-trois.
        """
        buffered = self._buffer.get(key)
        if buffered is not None:
            if buffered >= MAX_CALLS_PER_OBSERVATION:
                return False
            self._buffer[key] = buffered + 1
            return True
        cost = len(json.dumps(_observation(key, 1)).encode()) + 1
        if len(self._buffer) >= _MAX_BUFFERED:
            return False
        if self._buffer_bytes + cost > _BATCH_BYTE_BUDGET:
            return False
        self._buffer[key] = 1
        self._buffer_bytes += cost
        return True

    def _take_buffer(self) -> list[dict[str, object]]:
        if not self._buffer:
            return []
        batch = [_observation(key, calls) for key, calls in self._buffer.items()]
        self._buffer.clear()
        self._buffer_bytes = 0
        return batch

    def _emit(self, observations: list[dict[str, object]]) -> None:
        body = json.dumps({"observations": observations})
        # Référence retenue dans _pending : sans elle, le GC peut ramasser la
        # tâche avant son exécution (le loop ne détient qu'une weakref).
        task = asyncio.create_task(self._post(body))
        self._pending.add(task)
        task.add_done_callback(self._on_post_done)

    def _on_post_done(self, task: asyncio.Task[None]) -> None:
        """Rendre le créneau, puis vider le tampon s'il reste quelque chose.

        C'est ce qui rend la coalescence sûre sans minuterie : le tampon est
        drainé par l'événement qui libère une place, pas par une horloge. Sans
        ce rappel, une rafale suivie d'un silence laisserait ses observations
        dans le tampon jusqu'au prochain appel de tool — reportées, pas perdues,
        mais invisibles pendant un temps non borné.

        ``suppress`` : ce rappel tourne dans la boucle, hors de tout appelant.
        Une exception qui en sortirait irait au gestionnaire d'exceptions de la
        boucle, où personne ne la lit.
        """
        self._pending.discard(task)
        with contextlib.suppress(Exception):
            if self._buffer and len(self._pending) < self._max_in_flight:
                self._emit(self._take_buffer())

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
        while self._pending or self._buffer:
            if self._buffer and len(self._pending) < self._max_in_flight:
                self._emit(self._take_buffer())
            if not self._pending:
                break
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


async def close_activity_reporter() -> None:
    """Fermer l'émetteur s'il a été construit — câblé dans ``app_lifecycle``.

    Ticket ``d5e4bd73``, second trou : les POST en vol mouraient à l'arrêt sans
    être comptés. ``close()`` draine le tampon et attend les émissions en vol
    (bornées par le timeout httpx), donc chaque perte finit dans un compteur au
    lieu de disparaître avec le processus. L'oubli global est remis à ``None``
    AVANT la fermeture : un appel de tool tardif reconstruirait un émetteur
    frais plutôt que de poster sur un client clos. Ne lève jamais — un sidecar
    mort à l'arrêt ne doit pas faire échouer l'arrêt.
    """
    global _reporter
    reporter = _reporter
    _reporter = None
    if reporter is None:
        return
    with contextlib.suppress(Exception):
        await reporter.close()


def get_activity_reporter() -> ActivityReporter | None:
    """Renvoyer l'émetteur, construit à la première utilisation.

    Construction paresseuse plutôt que câblage dans le cycle de vie du serveur :
    `mcp` est bâti au niveau module (voir le commentaire de `mcp/server.py:170`),
    et la première émission a lieu dans une boucle déjà tournante. La fermeture,
    elle, est câblée : `close_activity_reporter` vit dans l'AsyncExitStack de
    `app_lifecycle`, pour que les émissions en vol à l'arrêt finissent comptées
    au lieu de mourir avec le processus (d5e4bd73, second trou).

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
