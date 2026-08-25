"""Capture DÉRIVÉE : déposer l'artefact dans la traçante de sa connexion.

La forme retenue est l'**ABSORPTION**, pas l'adoption. Le serveur ne promeut
jamais une traçante en session d'utilisateur : il dépose l'artefact dans la
traçante `agent` de la connexion à la création, et la session de l'utilisateur
absorbe ce ledger à sa commande suivante (``absorb_tracer_ledger`` plus bas).

**Pourquoi l'adoption est interdite.** ``auto_open`` re-date une ligne qui
entre en conflit sur (projet, connexion). Une ligne `operator` qui porterait une
connexion serait donc re-datée à chaque appel d'outil ; et comme l'éligibilité
7 jours du balayage lit ``last_heartbeat_at`` **sans filtre de nature**, la seule
exception ÉCRITE au covenant deviendrait inatteignable — un fantôme immortel. On
ne pose jamais ``connection_id`` sur une ligne `operator`.

**Module FEUILLE, et c'est structurel.** ``pg_brain_session`` importe
``pg_base``; ``pg_base`` appelle ce module. Lui faire importer
``pg_brain_session`` — ne serait-ce que pour ``CAPTURE_TABLES`` — fermerait le
cycle. Les deux listes vivent donc séparément, et
``test_the_table_map_agrees_with_the_repository_capture_tables`` est ce qui les
empêche de diverger en silence.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Final
from uuid import UUID

import sqlalchemy as sa
import structlog
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from brain_v42.config import get_settings
from brain_v42.db.tables import (
    adrs,
    brain_session_artifacts,
    brain_sessions,
    decisions,
    indexed_plans,
    learnings,
    runbooks,
    snippets,
)

logger = structlog.get_logger(__name__)


def _capture_cap() -> int:
    """Le plafond de la capture explicite, importé TARD et pour une raison.

    ``pg_base`` appelle ce module et porte une règle écrite : « Never import
    from brain_v42.models here — stay at the DB Core dict layer. » Un import de
    module la contournerait par la bande, en tirant la couche modèle dans le
    graphe d'import du cœur DB. Le différer garde la règle vraie sans dupliquer
    la constante, ce qui la ferait dériver.
    """
    from brain_v42.models.brain_session import (  # noqa: PLC0415
        MAX_CAPTURED_KNOWLEDGE_IDS,
    )

    return MAX_CAPTURED_KNOWLEDGE_IDS


#: Les tables dont une création peut être attribuée. Miroir volontaire de
#: ``pg_brain_session.CAPTURE_TABLES`` : voir le cycle d'import ci-dessus.
_CAPTURE_TABLES: Final[tuple[tuple[sa.Table, str], ...]] = (
    (decisions, "decision"),
    (learnings, "learning"),
    (snippets, "snippet"),
    (runbooks, "runbook"),
    (adrs, "adr"),
    (indexed_plans, "indexed_plan"),
)

#: Par NOM de table — la seule chose que ``BasePgRepository`` connaisse de la
#: sienne.
CAPTURE_TABLES: Final[Mapping[str, str]] = {
    table.name: knowledge_type for table, knowledge_type in _CAPTURE_TABLES
}


#: Les quatre modes d'attribution, miroir du CHECK de la 048 et de `tables.py`.
#: Les nommer ici plutôt que de les écrire à la volée est ce qui fait échouer un
#: mode inventé à l'INSERT plutôt qu'en production, sur une contrainte.
EXPLICIT: Final = "explicit"
DEPOSIT: Final = "derived_deposit"
BY_CONNECTION: Final = "derived_connection"
BY_WINDOW: Final = "derived_window"

#: Statuts d'une traçante dont le ledger reste PRENABLE. `closed_inactive` en
#: fait partie et ce n'est pas de la générosité : le balayage 4 h existe pour
#: sortir une traçante de `open` EN GARDANT son ledger. S'en tenir à `open`
#: marcherait aujourd'hui — le balayage est inerte par accident de placement de
#: drop-in — et redeviendrait muet le jour où quelqu'un corrigera ce placement,
#: sans qu'aucun test n'échoue.
_DONOR_STATUSES: Final = ("open", "closed_inactive")

#: Acteurs dont une traçante n'est JAMAIS absorbée par l'étage fenêtre. Miroir
#: SQL de `provenance.is_human_actor` — le dream n'est pas un « créateur
#: inconnu », il est identifié, et le laisser dans le pot commun rendrait le
#: mode de panne quotidien (le `promote` de 03:00 tombe dans la fenêtre de
#: n'importe quelle session ouverte la nuit) au lieu de marginal.
_SYSTEM_ACTOR_PREFIX: Final = "dream-"
_NON_HUMAN_ACTORS: Final = ("unknown", "_unexpanded", "")


@dataclass(frozen=True)
class AbsorptionOutcome:
    """Ce que l'absorption a fait, ET par quelle clé — jamais un total nu.

    Trois retours `0` étaient jusqu'ici indiscernables : drapeau fermé, aucune
    connexion, et « rien à absorber ». Un quatrième vient s'y ajouter avec
    l'étage fenêtre — le REFUS pour ambiguïté — et c'est le seul qui veuille dire
    « la règle a fonctionné et a dit non ». Les confondre, c'est reproduire
    exactement le mode de panne de ce chantier : une capacité armée, verte, et
    muette là où elle échoue.

    `total` est ce que le dépôt rend à l'appelant ; le reste est ce qu'on lit au
    journal quand on cherche pourquoi rien n'a bougé.
    """

    reason: str
    moved_by_connection: int = 0
    moved_by_window: int = 0
    #: Artefacts que l'étage fenêtre a refusés parce qu'une AUTRE session
    #: non-`agent` couvrait leur instant de création. Sans ce compte, un refus
    #: systématique est indistinguable d'un chemin mort.
    rivals: int = 0
    moved_ids: tuple[UUID, ...] = field(default_factory=tuple)
    donors: tuple[UUID, ...] = field(default_factory=tuple)

    @property
    def total(self) -> int:
        return self.moved_by_connection + self.moved_by_window


def _enabled() -> bool:
    """Drapeau fermé par défaut. Une résolution qui rate vaut « fermé »."""
    try:
        return bool(get_settings().brain_session_derived_capture_enabled)
    except Exception:
        return False


def _current_connection_id() -> str:
    from brain_v42.provenance import get_current_transport  # noqa: PLC0415

    return (get_current_transport() or "").strip()


def _tracer_query(project_key: str, connection_id: str) -> sa.Select[Any]:
    """La traçante de cette connexion, et rien d'autre.

    Quatre bornes, aucune décorative. ``nature = 'agent'`` en particulier : sans
    elle, la dérivation pourrait déposer dans la session d'un humain sans qu'il
    l'ait demandé, ce qui est précisément ce que l'absorption doit rester seule
    à faire — et sur commande explicite.
    """
    return sa.select(brain_sessions.c.id).where(
        brain_sessions.c.project_key == project_key,
        brain_sessions.c.connection_id == connection_id,
        brain_sessions.c.status == "open",
        brain_sessions.c.nature == "agent",
    )


async def derive_capture(
    session: AsyncSession,
    table_name: str,
    row: Mapping[str, Any],
) -> UUID | None:
    """Déposer l'artefact tout juste créé dans la traçante de sa connexion.

    Rend l'identifiant déposé, ou ``None`` — et ``None`` n'est jamais une
    erreur : pas de drapeau, pas de connexion, pas de traçante, table hors
    périmètre, artefact déjà attribué, ledger plein. Ce sont des refus, pas des
    pannes, et aucun ne doit se voir depuis l'appel qui crée.

    **Elle ne vole jamais** : ``ON CONFLICT DO NOTHING`` porte sur
    ``knowledge_id``, qui EST la clé primaire du ledger. Un artefact déjà
    attribué reste où il est, que ce soit à une session explicite ou à une autre
    traçante.

    **Elle ne casse pas la création qu'elle observe** : tout vit dans un
    ``begin_nested()`` et toute ``Exception`` est avalée. « Pas » et non
    « jamais », parce que ``except Exception`` n'attrape pas ``BaseException`` :
    une ``CancelledError`` reçue pendant le ``ROLLBACK TO SAVEPOINT`` du
    ``__aexit__`` peut laisser la transaction appelante dans un état
    indéterminé. La fenêtre est étroite, et l'écrire est moins cher que de
    laisser croire à une garantie totale.

    La résolution de la connexion vit DANS le ``try``, et pas au-dessus : son
    import est différé, donc un ``ImportError`` de ``brain_v42.provenance``
    remonterait dans l'appel observé — exactement ce que ces gardes prétendent
    empêcher.
    """
    knowledge_type = CAPTURE_TABLES.get(table_name)
    if knowledge_type is None or not _enabled():
        return None

    try:
        connection_id = _current_connection_id()
        knowledge_id = row.get("id")
        project_key = row.get("project_key")
        if not connection_id or knowledge_id is None or not project_key:
            return None

        async with session.begin_nested():
            tracer = (
                await session.execute(_tracer_query(str(project_key), connection_id))
            ).scalar_one_or_none()
            if tracer is None:
                return None

            occupied = await session.execute(
                sa.select(sa.func.count())
                .select_from(brain_session_artifacts)
                .where(brain_session_artifacts.c.session_id == tracer)
            )
            if int(occupied.scalar_one() or 0) >= _capture_cap():
                return None

            inserted = (
                await session.execute(
                    pg_insert(brain_session_artifacts)
                    .values(
                        knowledge_id=knowledge_id,
                        session_id=tracer,
                        knowledge_type=knowledge_type,
                        attribution_mode=DEPOSIT,
                    )
                    .on_conflict_do_nothing(index_elements=[brain_session_artifacts.c.knowledge_id])
                    .returning(brain_session_artifacts.c.knowledge_id)
                )
            ).scalar_one_or_none()
    except Exception:
        # ``except`` TOTAL et étroitement scopé, même posture que
        # l'auto-ouverture : ce chemin accompagne CHAQUE création de
        # connaissance. Le savepoint a déjà rendu la transaction de création
        # saine ; ce qui reste à faire est de ne pas propager.
        logger.warning("session_derived_capture.failed", table=table_name, exc_info=True)
        return None

    return UUID(str(inserted)) if inserted is not None else None


def _eligible_ids(project_key: str, started_at: datetime, limit: int) -> sa.CompoundSelect[Any]:
    """Ce qu'une capture EXPLICITE aurait accepté, et rien de plus.

    ``_validate_captures`` borne une capture demandée à « même projet ET
    ``created_at >= started_at`` », sur ces six tables. L'absorption porte les
    MÊMES bornes, et c'est l'invariant du chantier : sans lui, la dérivation
    attribuerait des artefacts que l'utilisateur n'aurait pas pu capturer
    lui-même, donc deviendrait un chemin plus permissif que la commande qu'elle
    remplace. Un passe-droit, pas une commodité.
    """
    branches = [
        sa.select(table.c.id).where(
            table.c.project_key == project_key,
            table.c.created_at >= started_at,
        )
        for table, _knowledge_type in _CAPTURE_TABLES
    ]
    return sa.union_all(*branches).limit(limit)


def _eligible_rows(project_key: str, started_at: datetime, limit: int) -> sa.CompoundSelect[Any]:
    """Les mêmes bornes que ``_eligible_ids``, mais l'INSTANT voyage avec l'id.

    L'étage fenêtre ne juge pas un artefact dans l'absolu : il juge qui couvrait
    l'instant de sa création. Cet instant doit donc être corrélable ligne à
    ligne, ce qu'une liste d'identifiants nus ne permet pas.
    """
    branches = [
        sa.select(table.c.id.label("id"), table.c.created_at.label("created_at")).where(
            table.c.project_key == project_key,
            table.c.created_at >= started_at,
        )
        for table, _knowledge_type in _CAPTURE_TABLES
    ]
    return sa.union_all(*branches).limit(limit)


def _window_donors(project_key: str) -> sa.Select[Any]:
    """Les traçantes du projet dont le ledger est prenable par la fenêtre.

    Trois bornes, aucune décorative. ``nature='agent'`` : on ne prend jamais à
    un humain. Le statut inclut ``closed_inactive``, sinon le correctif
    redeviendrait muet le jour où le balayage 4 h cessera d'être inerte. Et
    l'acteur doit être humain : une traçante ouverte par le dream est identifiée
    comme telle, la laisser dans le pot commun ferait tomber le `promote` de
    03:00 dans la fenêtre de toute session ouverte cette nuit-là.
    """
    actor = brain_sessions.c.started_by_actor
    return sa.select(brain_sessions.c.id).where(
        brain_sessions.c.project_key == project_key,
        brain_sessions.c.nature == "agent",
        brain_sessions.c.status.in_(_DONOR_STATUSES),
        actor.is_not(None),
        actor.not_in(_NON_HUMAN_ACTORS),
        sa.not_(actor.startswith(_SYSTEM_ACTOR_PREFIX)),
    )


def _covered_by_a_rival(
    project_key: str, target_id: Any, created_at: Any
) -> sa.ColumnElement[bool]:
    """Une AUTRE session non-`agent` couvrait-elle cet instant ?

    La rivalité est SYMÉTRIQUE : aucune clause de fraîcheur, aucune clause de
    fratrie. Deux prétendantes valent une abstention — jamais un tirage au sort,
    et jamais « la plus récente gagne », qui rendrait l'attribution dépendante
    de l'ordre de fermeture.

    La couverture se juge à l'INSTANT, pas au moment de la commande :
    ``started_at <= t <= coalesce(ended_at, now())``. Une session close APRÈS
    l'instant l'a bel et bien couvert et reste donc une rivale ; une session
    close AVANT ne couvre rien. Juger « ouverte MAINTENANT » ferait rafler la
    mise par la dernière à fermer.
    """
    rival = brain_sessions.alias("rival")
    return sa.exists().where(
        sa.and_(
            rival.c.project_key == project_key,
            rival.c.id != target_id,
            sa.or_(rival.c.nature.is_(None), rival.c.nature != "agent"),
            rival.c.started_at <= created_at,
            sa.func.coalesce(rival.c.ended_at, sa.func.now()) >= created_at,
        )
    )


async def absorb_tracer_ledger(
    session: AsyncSession,
    target: Any,
    connection_id: str,
) -> AbsorptionOutcome:
    """Rendre à ``target`` ce que les traçantes ont recueilli pour elle. DEUX étages.

    C'est l'ABSORPTION : la session de l'utilisateur prend ce qu'une traçante a
    recueilli, sans que la traçante soit jamais promue.

    **Étage 1 — la connexion courante.** L'appariement EXACT, évalué en premier
    et inchangé. Quand il répond, il n'y a rien à déduire.

    **Étage 2 — l'exclusivité temporelle.** Il n'existe que parce que l'étage 1
    est structurellement insuffisant : ``connection_id`` est le
    ``Mcp-Session-Id``, un identifiant de TRANSPORT que l'idle timeout de 900 s
    tue bien avant que l'utilisateur ne ferme sa session — mesuré ~26 fois par
    jour, contre 3 redémarrages en trois jours. Une session de 16 h face à des
    transports dont la durée de vie médiane est sous 2 minutes ne peut pas être
    appariée par la connexion de son seul appel de fermeture.

    L'étage 2 est une DÉDUCTION, pas une preuve, et le code doit le dire : il
    n'attribue que si ``target`` était, à l'instant de création, la SEULE session
    non-`agent` du projet qui couvrait cet instant. Sous ambiguïté il refuse —
    l'artefact reste chez la traçante, visible et non perdu.

    Le donneur reste `agent` UNIQUEMENT, aux deux étages. Absorber une session
    `operator` déplacerait le ledger d'un humain vers un autre humain, ce que
    l'exclusivité du ledger existe pour empêcher.

    Le plafond de 100 de la capture explicite est respecté et DÉCRÉMENTÉ entre
    les étages : le franchir rendrait ``brain_session_capture`` refusable pour
    une raison que l'utilisateur n'a pas provoquée.
    """
    if not _enabled():
        return AbsorptionOutcome(reason="disabled")
    if not connection_id:
        # `stdio` et le mode sans état n'ont pas de couple (projet, connexion).
        # Ce n'est pas le même « rien » qu'un drapeau fermé, et les confondre
        # est exactement ce qui a rendu cette panne muette pendant dix jours.
        return AbsorptionOutcome(reason="no_connection")

    moved_connection: list[UUID] = []
    moved_window: list[UUID] = []
    donors: list[UUID] = []
    rivals = 0

    try:
        async with session.begin_nested():
            tracer = (
                await session.execute(_tracer_query(target.project_key, connection_id))
            ).scalar_one_or_none()

            occupied = int(
                (
                    await session.execute(
                        sa.select(sa.func.count())
                        .select_from(brain_session_artifacts)
                        .where(brain_session_artifacts.c.session_id == target.id)
                    )
                ).scalar_one()
                or 0
            )
            remaining = _capture_cap() - occupied
            if remaining <= 0:
                return AbsorptionOutcome(reason="ledger_full")

            if tracer is not None and tracer != target.id:
                donors.append(UUID(str(tracer)))
                moved_connection = list(
                    (
                        await session.execute(
                            brain_session_artifacts.update()
                            .where(
                                brain_session_artifacts.c.session_id == tracer,
                                brain_session_artifacts.c.knowledge_id.in_(
                                    _eligible_ids(target.project_key, target.started_at, remaining)
                                ),
                            )
                            .values(session_id=target.id, attribution_mode=BY_CONNECTION)
                            .returning(brain_session_artifacts.c.knowledge_id)
                        )
                    )
                    .scalars()
                    .all()
                )
                remaining -= len(moved_connection)

            if remaining > 0:
                eligible = _eligible_rows(
                    target.project_key, target.started_at, remaining
                ).subquery()
                contested = _covered_by_a_rival(
                    target.project_key, target.id, eligible.c.created_at
                )
                parked = sa.select(brain_session_artifacts.c.knowledge_id).where(
                    brain_session_artifacts.c.session_id.in_(_window_donors(target.project_key))
                )
                moved_window = list(
                    (
                        await session.execute(
                            brain_session_artifacts.update()
                            .where(
                                brain_session_artifacts.c.session_id.in_(
                                    _window_donors(target.project_key)
                                ),
                                brain_session_artifacts.c.knowledge_id.in_(
                                    sa.select(eligible.c.id).where(sa.not_(contested))
                                ),
                            )
                            .values(session_id=target.id, attribution_mode=BY_WINDOW)
                            .returning(brain_session_artifacts.c.knowledge_id)
                        )
                    )
                    .scalars()
                    .all()
                )

                if not moved_connection and not moved_window:
                    # Ce compte est la différence entre « la règle a dit non » et
                    # « ce chemin est mort ». Sans lui, un refus systématique
                    # ressemble à du code jamais atteint.
                    rivals = int(
                        (
                            await session.execute(
                                sa.select(sa.func.count())
                                .select_from(eligible)
                                .where(eligible.c.id.in_(parked), contested)
                            )
                        ).scalar_one()
                        or 0
                    )
    except Exception:
        logger.warning("session_derived_capture.absorb_failed", exc_info=True)
        return AbsorptionOutcome(reason="failed")

    moved_ids = tuple(UUID(str(item)) for item in (*moved_connection, *moved_window))
    if moved_ids:
        reason = "absorbed"
    elif rivals:
        reason = "ambiguous"
    else:
        reason = "nothing_to_absorb"

    outcome = AbsorptionOutcome(
        reason=reason,
        moved_by_connection=len(moved_connection),
        moved_by_window=len(moved_window),
        rivals=rivals,
        moved_ids=moved_ids,
        donors=tuple(donors),
    )
    _log_absorption(target, connection_id, outcome)
    return outcome


def _log_absorption(target: Any, connection_id: str, outcome: AbsorptionOutcome) -> None:
    """L'observable de production : PAR QUELLE CLÉ, et sur QUELS artefacts.

    Sans les UUID, une mauvaise attribution n'est pas défaisable — on saurait
    qu'il y en a eu une, jamais laquelle. Sans le compte des rivales, un refus
    systématique est indistinguable d'un chemin mort, ce qui est le mode de
    panne que ce lot répare.
    """
    if outcome.reason in {"disabled", "no_connection"}:
        return
    logger.info(
        "session_derived_capture.absorbed",
        reason=outcome.reason,
        session_id=str(target.id),
        project_key=target.project_key,
        connection_id=connection_id,
        moved_by_connection=outcome.moved_by_connection,
        moved_by_window=outcome.moved_by_window,
        rivals_blocked=outcome.rivals,
        moved_ids=[str(item) for item in outcome.moved_ids],
        donors=[str(item) for item in outcome.donors],
    )
