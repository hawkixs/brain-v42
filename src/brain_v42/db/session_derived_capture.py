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
from typing import Any, Final
from uuid import UUID

import sqlalchemy as sa
import structlog
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from brain_v42.config import get_settings
from brain_v42.db.tables import brain_session_artifacts, brain_sessions
from brain_v42.models.brain_session import MAX_CAPTURED_KNOWLEDGE_IDS

logger = structlog.get_logger(__name__)

#: Les tables dont une création peut être attribuée, par NOM de table — la seule
#: chose que ``BasePgRepository`` connaisse de la sienne. Miroir volontaire de
#: ``pg_brain_session.CAPTURE_TABLES`` : voir le cycle d'import ci-dessus.
CAPTURE_TABLES: Final[Mapping[str, str]] = {
    "decisions": "decision",
    "learnings": "learning",
    "snippets": "snippet",
    "runbooks": "runbook",
    "adrs": "adr",
    "indexed_plans": "indexed_plan",
}


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

    **Elle ne casse jamais la création qu'elle observe** : tout vit dans un
    ``begin_nested()`` et toute exception est avalée. Une capture dérivée qui
    ferait échouer un ``brain_learn`` serait pire que pas de capture du tout.
    """
    knowledge_type = CAPTURE_TABLES.get(table_name)
    if knowledge_type is None or not _enabled():
        return None

    connection_id = _current_connection_id()
    knowledge_id = row.get("id")
    project_key = row.get("project_key")
    if not connection_id or knowledge_id is None or not project_key:
        return None

    try:
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
            if int(occupied.scalar_one() or 0) >= MAX_CAPTURED_KNOWLEDGE_IDS:
                return None

            inserted = (
                await session.execute(
                    pg_insert(brain_session_artifacts)
                    .values(
                        knowledge_id=knowledge_id,
                        session_id=tracer,
                        knowledge_type=knowledge_type,
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
