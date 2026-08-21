"""Auto-ouverture d'une session traçante `agent`, une par connexion.

Forme **signée** (`ae0d0475`, ADR §0ter) et ses quatre propriétés, dans l'ordre
où elles ont été arbitrées :

1. **Synchrone et AVANT l'outil.** Le feu-et-oubli n'attribue rien : la capture
   borne les artefacts par ``created_at >= started_at``, donc la session doit
   exister au moment où l'appel crée les siens. C'est la différence avec
   l'émetteur d'activité client (`1c40c36a`), qui n'observe rien qu'il doive
   précéder.
2. **Fail-open : l'échec n'est JAMAIS propagé.** Fail-open n'est pas
   asynchrone — on attend l'ouverture, et on laisse passer l'appel si elle
   rate. Le prix est écrit une fois pour toutes dans `SPEC-M-G` §6 : les
   artefacts créés avant une ouverture ratée tombent hors de la fenêtre de
   capture, et B5 redevient mordante ponctuellement.
3. **Mémoïsée par connexion.** La mémo est un CHEMIN RAPIDE, jamais l'autorité :
   celle-ci est l'index UNIQUE PARTIEL ``WHERE status = 'open'`` de la 046, qui
   rend la réouverture naturelle après une fermeture. Un cache qui trancherait
   « déjà fait » sans la base mentirait dès la première auto-fermeture.
4. **Idempotence par la garde de profondeur.** En profil `compact`,
   ``on_call_tool`` tire deux fois par appel client (mesuré, commit 58329a84) :
   c'est ``is_outermost_call()``, côté middleware, qui réserve l'ouverture au
   niveau extérieur. La mémo ne peut pas jouer ce rôle — elle masquerait le
   second tir au lieu de l'empêcher.

**Rien du tout en stdio** (§0ter.2, signé). L'auto-ouverture n'existe qu'en
HTTP, sur la clé ``(projet, connexion)``, parce que ``Mcp-Session-Id`` est le
seul des trois identifiants que le client ne déclare pas : il est frappé côté
serveur. Le repli sur ``(projet, acteur)`` a été explicitement ÉCARTÉ — il
attribuerait sur du déclaratif, là où toute la valeur du modèle est d'attribuer
sur le seul signal non falsifiable. Ici, l'absence d'identifiant de connexion
n'est donc pas un cas dégradé à rattraper : c'est le contrat.

Précondition dure, héritée et non re-signée : ``mcp_http_stateless=False``. En
mode sans état il n'y a pas d'identifiant de connexion, et cette clé tombe.
"""

from __future__ import annotations

from collections import OrderedDict, defaultdict
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Literal
from uuid import UUID

import structlog

from brain_v42.config import get_settings
from brain_v42.models.project_key import canonicalize_project_key
from brain_v42.provenance import (
    MAX_ACTOR_LENGTH,
    UNEXPANDED_ACTOR,
    UNKNOWN_ACTOR,
    get_current_actor,
    get_current_transport,
)

logger = structlog.get_logger(__name__)

#: Largeur de ``brain_sessions.connection_id`` (046). Tronquer plutôt que
#: laisser PG lever un 22001 : l'identifiant est frappé par le serveur en
#: ``uuid4().hex`` (32 caractères), donc la troncature est hors d'atteinte en
#: nominal — elle borne un transport hostile, pas le chemin nominal.
MAX_CONNECTION_ID_LENGTH = 64

#: Plafond de la mémo. Une connexion = une entrée ; sans borne, un processus
#: long-vivant qui voit défiler des connexions ferait croître ce dict sans fin.
#: Même raisonnement que ``_MAX_UNIDENTIFIED_TRACKED`` dans le middleware.
DEFAULT_MAX_MEMOIZED_CONNECTIONS = 512

_autoopener: SessionAutoOpener | None = None


@dataclass(frozen=True, slots=True)
class AutoOpenIdentity:
    """Ce qu'une session auto-ouverte porte, et rien de plus.

    Quatre des cinq colonnes de la 046 voyagent ici et **pas** dans
    ``BrainSession`` : FastMCP dérive le schéma de sortie des tools de ce
    modèle-là, et les y faire entrer coûterait le budget de schéma que
    ``test_discovery_contract_keeps_tool_identity_inputs_and_schema_budget``
    garantit. Seule ``nature`` est au contrat public.

    ``intent`` reste ``None`` : c'est le champ humain de triage des fantômes, et
    ``NULL`` y veut dire « pas mesuré », jamais « vide ». Le serveur ne fabrique
    pas de jugement (objection C9).
    """

    project_key: str
    connection_id: str
    started_by_actor: str
    nature: Literal["agent"] = "agent"
    intent: str | None = None


#: Un ouvreur reçoit l'identité résolue et rend l'UUID de la session ouverte
#: (neuve ou déjà ouverte pour cette connexion), ou ``None`` s'il n'y a rien à
#: ouvrir — par exemple quand le projet n'a pas de contexte.
SessionOpener = Callable[[AutoOpenIdentity], Awaitable[UUID | None]]


def resolve_auto_open_identity() -> tuple[AutoOpenIdentity | None, str]:
    """Résoudre l'identité de la connexion courante, ou dire pourquoi non.

    Rend ``(identité, "")`` ou ``(None, raison)``. Les trois raisons sont
    disjointes et comptées séparément : les confondre rendrait « stdio » et
    « client anonyme » indiscernables dans le seul instrument qu'on aura.
    """
    connection = (get_current_transport() or "").strip()
    if not connection:
        return None, "no_connection"

    actor = get_current_actor()
    if actor in (UNKNOWN_ACTOR, UNEXPANDED_ACTOR) or not actor.strip():
        return None, "no_actor"

    try:
        project_key = canonicalize_project_key(actor)
    except (TypeError, ValueError):
        # `strict=True` VOULU : le chemin d'écriture. `strict=False` laisserait
        # passer une clé malformée, qui créerait un projet fantôme invisible du
        # briefing scopé (learning 7bc821a1).
        return None, "no_project"

    return (
        AutoOpenIdentity(
            project_key=project_key,
            connection_id=connection[:MAX_CONNECTION_ID_LENGTH],
            started_by_actor=actor[:MAX_ACTOR_LENGTH],
        ),
        "",
    )


class SessionAutoOpener:
    """Garde une session `agent` ouverte par connexion, sans jamais lever."""

    def __init__(
        self,
        opener: SessionOpener,
        *,
        max_connections: int = DEFAULT_MAX_MEMOIZED_CONNECTIONS,
    ) -> None:
        self._opener = opener
        self._max_connections = max_connections
        self._memo: OrderedDict[str, UUID] = OrderedDict()
        self.opened = 0
        self.memoized = 0
        self.failed = 0
        self.skipped: defaultdict[str, int] = defaultdict(int)

    async def ensure_open(self) -> UUID | None:
        """Ouvrir si besoin. **Ne lève jamais** — c'est tout le contrat."""
        identity, reason = resolve_auto_open_identity()
        if identity is None:
            self.skipped[reason] += 1
            return None

        memoized = self._memo.get(identity.connection_id)
        if memoized is not None:
            self._memo.move_to_end(identity.connection_id)
            self.memoized += 1
            return memoized

        try:
            session_id = await self._opener(identity)
        except Exception:
            # ``except`` TOTAL et étroitement scopé, même posture que
            # ``_report`` : ce chemin s'exécute sur CHAQUE appel de tool
            # extérieur d'un processus partagé. Un hoquet de base ne peut pas
            # faire tomber l'appel qu'il accompagne.
            #
            # ``warning`` et non ``debug`` : contrairement à un refus de
            # récepteur, qui peut se répéter à chaque appel, un échec ici
            # signale un défaut de l'ouvreur lui-même.
            self.failed += 1
            logger.warning(
                "session_autoopen.failed",
                project_key=identity.project_key,
                connection_id=identity.connection_id,
                exc_info=True,
            )
            return None

        if session_id is None:
            # Pas d'ouverture possible (contexte de projet absent) : ce n'est
            # PAS un échec, et surtout pas une mémo — le contexte peut naître
            # plus tard, et la connexion doit pouvoir en profiter.
            self.skipped["no_session"] += 1
            return None

        self._remember(identity.connection_id, session_id)
        self.opened += 1
        return session_id

    def _remember(self, connection_id: str, session_id: UUID) -> None:
        self._memo[connection_id] = session_id
        self._memo.move_to_end(connection_id)
        while len(self._memo) > self._max_connections:
            # LRU plutôt que refus d'insérer : une entrée évincée coûte un
            # aller-retour en base, jamais une session perdue — l'index partiel
            # rend ce chemin idempotent.
            self._memo.popitem(last=False)


def get_session_autoopener() -> SessionAutoOpener | None:
    """Renvoyer l'ouvreur, ou ``None`` tant que le drapeau est fermé.

    Ne lève jamais : l'appelant est le middleware de provenance, sur le chemin
    de TOUT appel de tool. Une résolution de settings qui échoue est traitée
    comme une indisponibilité, pas comme une erreur d'appel.
    """
    global _autoopener
    if _autoopener is None:
        try:
            settings = get_settings()
            if not settings.brain_session_auto_open_enabled:
                return None
            _autoopener = SessionAutoOpener(_build_default_opener())
        except Exception as exc:
            # Type seul : les cadres traversés portent la configuration, DSN
            # compris.
            logger.debug("session_autoopen.unavailable", error=type(exc).__name__)
            return None
    return _autoopener


def reset_session_autoopener() -> None:
    """Oublier l'ouvreur mémoïsé — point d'entrée des tests, jamais de la prod."""
    global _autoopener
    _autoopener = None


def _build_default_opener() -> SessionOpener:
    """Câbler l'ouvreur de production sur le dépôt de sessions."""
    from brain_v42.db.engine import get_session_factory  # noqa: PLC0415
    from brain_v42.repositories.pg_brain_session import PgBrainSessionRepo  # noqa: PLC0415

    repo = PgBrainSessionRepo(get_session_factory())

    async def _open(identity: AutoOpenIdentity) -> UUID | None:
        return await repo.auto_open(identity)

    return _open
