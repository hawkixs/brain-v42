"""Auto-ouverture et observation contre une vraie base — l'index PARTIEL au travail.

Ce que le harnais unitaire ne peut PAS prouver, et qui est tout l'enjeu :

- l'``ON CONFLICT`` s'infère bien sur ``uq_brain_sessions_connection``, et le
  ``WHERE status = 'open'`` du prédicat d'index est ce qui rend la RÉOUVERTURE
  possible après une fermeture nocturne. Un index PLEIN brûlerait la connexion à
  vie dès la première auto-fermeture — le piège hérité de `SPEC-M-G` §5, qui ne
  se voit qu'ici ;
- ``client_key`` reçoit un UUID neuf parce que ``uq_brain_sessions_project_client``
  est PLEINE : réutiliser une clé stable par connexion ferait échouer cette même
  réouverture, le piège déplacé d'une colonne ;
- et les DEUX horloges bougent réellement, ce qu'aucune assertion sur du SQL
  compilé ne peut établir.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
import pytest_asyncio
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from brain_v42.db.tables import brain_sessions, project_contexts
from brain_v42.repositories.pg_brain_session import PgBrainSessionRepo

pytestmark = pytest.mark.integration


@dataclass(frozen=True)
class _Identity:
    project_key: str
    connection_id: str
    started_by_actor: str = "integ-actor"
    nature: str = "agent"
    intent: str | None = None


@pytest_asyncio.fixture
async def autoopen_project(
    session_factory: async_sessionmaker[AsyncSession],
) -> AsyncIterator[str]:
    project_key = f"integ-autoopen-{uuid4().hex[:10]}"
    async with session_factory.begin() as session:
        await session.execute(
            project_contexts.insert().values(
                project_key=project_key,
                name="Auto-open integration",
                description="Isolated auto-open fixture",
                current_focus="focus de la traçante",
            )
        )
    try:
        yield project_key
    finally:
        async with session_factory.begin() as session:
            await session.execute(
                brain_sessions.delete().where(brain_sessions.c.project_key == project_key)
            )
            await session.execute(
                project_contexts.delete().where(project_contexts.c.project_key == project_key)
            )


async def _row(session_factory: async_sessionmaker[AsyncSession], session_id):
    async with session_factory() as session:
        return (
            (
                await session.execute(
                    sa.select(brain_sessions).where(brain_sessions.c.id == session_id)
                )
            )
            .mappings()
            .one()
        )


async def test_the_first_call_writes_the_five_046_columns(
    session_factory: async_sessionmaker[AsyncSession],
    autoopen_project: str,
) -> None:
    """La 046 avait posé cinq colonnes et zéro écrivain. Voici les quatre écrites."""
    identity = _Identity(autoopen_project, uuid4().hex)
    now = datetime(2026, 8, 22, 9, 0, tzinfo=UTC)

    opened = await PgBrainSessionRepo(session_factory).auto_open(identity, now=now)

    assert opened is not None
    row = await _row(session_factory, opened)
    assert row["nature"] == "agent"
    assert row["connection_id"] == identity.connection_id
    assert row["started_by_actor"] == "integ-actor"
    assert row["last_observed_at"] == now
    # `intent` est le seul champ de JUGEMENT des cinq : NULL veut dire
    # « pas mesuré », et le serveur n'en fabrique pas.
    assert row["intent"] is None
    assert row["status"] == "open"


async def test_a_second_call_on_the_same_connection_redates_the_same_row(
    session_factory: async_sessionmaker[AsyncSession],
    autoopen_project: str,
) -> None:
    """`DO UPDATE`, pas `DO NOTHING` : un conflit EST une observation.

    Avec son TÉMOIN NÉGATIF — l'horloge doit avoir BOUGÉ. Sans lui, un
    `DO NOTHING` suivi d'un `SELECT` rendrait le même id et laisserait ce test
    vert, alors que `last_observed_at` resterait figé à l'heure d'ouverture et
    que la règle des 4 h finirait par prendre une connexion active.
    """
    repo = PgBrainSessionRepo(session_factory)
    identity = _Identity(autoopen_project, uuid4().hex)
    opened_at = datetime(2026, 8, 22, 9, 0, tzinfo=UTC)
    again_at = opened_at + timedelta(hours=3)

    first = await repo.auto_open(identity, now=opened_at)
    second = await repo.auto_open(identity, now=again_at)

    assert second == first
    row = await _row(session_factory, first)
    assert row["last_observed_at"] == again_at
    assert row["last_heartbeat_at"] == again_at

    async with session_factory() as session:
        total = (
            await session.execute(
                sa.select(sa.func.count())
                .select_from(brain_sessions)
                .where(brain_sessions.c.project_key == autoopen_project)
            )
        ).scalar_one()
    assert total == 1, "un conflit ne doit jamais produire une seconde session"


async def test_a_closed_session_does_not_burn_its_connection(
    session_factory: async_sessionmaker[AsyncSession],
    autoopen_project: str,
) -> None:
    """LE test que l'index partiel existe pour rendre vrai.

    Après une fermeture nocturne, la MÊME connexion doit pouvoir rouvrir. Un
    index UNIQUE plein — sur le modèle de `uq_brain_sessions_project_client` —
    rendrait cet insert impossible pour toujours, et `closed_inactive` fait
    précisément sortir des lignes de `status = 'open'` en masse chaque nuit.
    """
    repo = PgBrainSessionRepo(session_factory)
    identity = _Identity(autoopen_project, uuid4().hex)
    first = await repo.auto_open(identity, now=datetime(2026, 8, 22, 3, 0, tzinfo=UTC))

    swept = await repo.sweep_open_sessions(
        older_than=timedelta(days=365),
        close_inactive_after=timedelta(hours=4),
        dry_run=False,
        now=datetime(2026, 8, 22, 9, 0, tzinfo=UTC),
    )
    assert swept.closed_inactive_count == 1

    second = await repo.auto_open(identity, now=datetime(2026, 8, 22, 9, 5, tzinfo=UTC))

    assert second is not None
    assert second != first
    assert (await _row(session_factory, first))["status"] == "closed_inactive"
    assert (await _row(session_factory, second))["status"] == "open"
    # `client_key` doit différer : `uq_brain_sessions_project_client` est PLEINE,
    # donc une clé stable par connexion aurait fait échouer cette réouverture.
    assert (await _row(session_factory, second))["client_key"] != (
        await _row(session_factory, first)
    )["client_key"]


async def test_observe_reports_whether_the_session_is_still_open(
    session_factory: async_sessionmaker[AsyncSession],
    autoopen_project: str,
) -> None:
    """Le booléen qui fait jeter la mémo — les trois cas, dans un seul test.

    Ouverte `agent` ⇒ `True` et l'horloge bouge. Fermée ⇒ `False`, et c'est un
    FAIT, pas une panne. Inexistante ⇒ `False` aussi : un UUID inconnu ne doit
    pas lever sur un chemin dont tout le contrat est de ne jamais lever.
    """
    repo = PgBrainSessionRepo(session_factory)
    identity = _Identity(autoopen_project, uuid4().hex)
    opened = await repo.auto_open(identity, now=datetime(2026, 8, 22, 3, 0, tzinfo=UTC))
    later = datetime(2026, 8, 22, 6, 0, tzinfo=UTC)

    assert await repo.observe(opened, now=later) is True
    assert (await _row(session_factory, opened))["last_observed_at"] == later
    assert await repo.observe(uuid4(), now=later) is False

    await repo.sweep_open_sessions(
        older_than=timedelta(days=365),
        close_inactive_after=timedelta(hours=1),
        dry_run=False,
        now=datetime(2026, 8, 22, 12, 0, tzinfo=UTC),
    )

    assert await repo.observe(opened, now=datetime(2026, 8, 22, 13, 0, tzinfo=UTC)) is False


async def test_observe_never_touches_an_operator_session(
    session_factory: async_sessionmaker[AsyncSession],
    autoopen_project: str,
) -> None:
    """Garde DURE : une mémo empoisonnée ne doit rien pouvoir dater ailleurs.

    L'auto-ouverture ne produit que des traçantes, donc ce cas ne devrait pas
    exister. C'est précisément pourquoi il est gardé en base plutôt qu'en
    commentaire : les invariants qu'on croit inatteignables sont ceux dont la
    violation passe inaperçue.
    """
    repo = PgBrainSessionRepo(session_factory)
    stamped = datetime(2026, 8, 22, 4, 0, tzinfo=UTC)
    async with session_factory.begin() as session:
        operator = (
            await session.execute(
                brain_sessions.insert()
                .values(
                    project_key=autoopen_project,
                    client_key=f"operator-{uuid4().hex[:8]}",
                    started_focus="focus opérateur",
                    started_focus_revision=1,
                    nature="operator",
                    last_observed_at=stamped,
                )
                .returning(brain_sessions.c.id)
            )
        ).scalar_one()

    assert await repo.observe(operator, now=datetime(2026, 8, 22, 10, 0, tzinfo=UTC)) is False
    assert (await _row(session_factory, operator))["last_observed_at"] == stamped


async def test_a_fresh_tracer_never_dates_its_heartbeat_before_its_start(
    session_factory: async_sessionmaker[AsyncSession],
    autoopen_project: str,
) -> None:
    """`last_heartbeat_at >= started_at` — l'invariant que le contrat DR compte.

    Mesuré en production le 2026-08-22 : la traçante ouvrait avec un heartbeat
    daté **1,5 ms AVANT** son propre démarrage, et
    `brain_runtime_032_036_037.focus_revision_violations` comptait chaque ligne.
    Le reçu passait de 29/29 à 28/29 sur les DEUX variantes de l'actif.

    La cause est un désaccord d'horloges, pas une erreur de signe : `reference`
    est lu par l'application AVANT l'ouverture de la transaction, tandis que
    `started_at` tombe sur le `DEFAULT now()` de la base — l'estampille de
    DÉBUT DE TRANSACTION, donc postérieure. `start()` échappe au piège en ne
    posant aucune des deux colonnes : ses deux horloges viennent du même
    défaut. `auto_open` en posait une seule, et c'est l'asymétrie qui coûte.

    Ce test tourne SANS `now=` injecté — c'est la forme de production, et la
    seule où l'écart des deux horloges est celui que la prod a réellement
    porté. Un `now=` injecté rendrait l'écart si grand qu'il masquerait le fait
    que le défaut se mesure en millisecondes.

    Le défaut est TRANSITOIRE et c'est ce qui le rend coûteux : la première
    `observe()` venue repousse `last_heartbeat_at` et efface la violation. Le
    contrôle DR clignote donc rouge/vert selon qu'une traçante fraîche a déjà
    rappelé, et un reçu vert ne prouve rien.
    """
    identity = _Identity(autoopen_project, uuid4().hex)

    opened = await PgBrainSessionRepo(session_factory).auto_open(identity)

    assert opened is not None
    row = await _row(session_factory, opened)
    assert row["last_heartbeat_at"] >= row["started_at"], (
        "une traçante fraîche date sa présence avant son propre démarrage : "
        f"heartbeat={row['last_heartbeat_at']} < started={row['started_at']}"
    )
    # UNE seule lecture d'horloge, pas trois qui se suivent. C'est le témoin
    # qui distingue le correctif de son contrefaçon : borner l'écart à « moins
    # d'une seconde » laisserait passer deux lectures distinctes, donc le bug.
    assert row["started_at"] == row["last_heartbeat_at"] == row["last_observed_at"]


async def test_reobserving_a_tracer_moves_both_clocks_but_never_its_start(
    session_factory: async_sessionmaker[AsyncSession],
    autoopen_project: str,
) -> None:
    """Le TÉMOIN NÉGATIF du correctif : `started_at` est une date d'OUVERTURE.

    Le correctif le plus court — glisser `started_at` dans
    `_observation_columns()` — verdit le test précédent et ment : chaque
    réobservation réécrirait la date d'ouverture, la traçante aurait
    éternellement zéro seconde d'âge, et le balayage des 7 j ne prendrait plus
    jamais rien. Une session qui rajeunit à chaque appel d'outil est un pire
    défaut que celui qu'on répare.

    Ce test échoue donc sur ce faux correctif, là où le précédent passerait.
    Les deux ensemble bornent la seule forme correcte : la branche INSERT pose
    les trois horloges d'une même lecture, la branche `DO UPDATE` n'en déplace
    que deux.
    """
    repo = PgBrainSessionRepo(session_factory)
    identity = _Identity(autoopen_project, uuid4().hex)
    opened_at = datetime(2026, 8, 22, 9, 0, tzinfo=UTC)
    again_at = opened_at + timedelta(hours=3)

    first = await repo.auto_open(identity, now=opened_at)
    assert first is not None
    assert (await _row(session_factory, first))["started_at"] == opened_at

    assert await repo.auto_open(identity, now=again_at) == first
    row = await _row(session_factory, first)
    assert row["last_observed_at"] == again_at
    assert row["last_heartbeat_at"] == again_at
    assert row["started_at"] == opened_at, "réobserver n'est pas rouvrir"
