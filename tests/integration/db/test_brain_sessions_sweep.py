"""Le balayage serveur contre une vraie base : frontière et invariants.

Seuil de 365 jours partout : le balayage est GLOBAL par conception, et la base
d'intégration est partagée. Antidater les lignes de la fixture et viser un an
rend structurellement impossible d'emporter la session d'un test voisin, qui
est forcément créée « maintenant ».
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
import pytest_asyncio
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from brain_v42.db.tables import (
    brain_session_artifacts,
    brain_sessions,
    decisions,
    project_contexts,
)
from brain_v42.repositories.pg_brain_session import PgBrainSessionRepo

pytestmark = pytest.mark.integration

THRESHOLD = timedelta(days=365)


@pytest_asyncio.fixture
async def sweep_project(
    session_factory: async_sessionmaker[AsyncSession],
) -> AsyncIterator[str]:
    project_key = f"integ-sweep-{uuid4().hex[:10]}"
    async with session_factory.begin() as session:
        await session.execute(
            project_contexts.insert().values(
                project_key=project_key,
                name="Session sweep integration",
                description="Isolated sweep fixture",
                current_focus="focus avant balayage",
            )
        )
    try:
        yield project_key
    finally:
        async with session_factory.begin() as session:
            project_sessions = sa.select(brain_sessions.c.id).where(
                brain_sessions.c.project_key == project_key
            )
            await session.execute(
                brain_session_artifacts.delete().where(
                    brain_session_artifacts.c.session_id.in_(project_sessions)
                )
            )
            await session.execute(
                brain_sessions.delete().where(brain_sessions.c.project_key == project_key)
            )
            await session.execute(decisions.delete().where(decisions.c.project_key == project_key))
            await session.execute(
                project_contexts.delete().where(project_contexts.c.project_key == project_key)
            )


async def _insert_open_session(
    session_factory: async_sessionmaker[AsyncSession],
    project_key: str,
    client_key: str,
    heartbeat: datetime,
    *,
    nature: str | None = None,
    observed: datetime | None = None,
):
    """Insérer une session OUVERTE, avec ou sans les colonnes de la 046.

    ``nature=None`` et ``observed=None`` par défaut : c'est l'état d'AVANT la
    046, celui de toutes les lignes existantes, et il doit rester le cas par
    défaut de la fixture pour que les tests antérieurs prouvent encore ce qu'ils
    prouvaient.
    """
    async with session_factory.begin() as session:
        row = (
            await session.execute(
                brain_sessions.insert()
                .values(
                    project_key=project_key,
                    client_key=client_key,
                    started_focus="focus avant balayage",
                    started_focus_revision=1,
                    started_at=heartbeat,
                    last_heartbeat_at=heartbeat,
                    nature=nature,
                    last_observed_at=observed,
                )
                .returning(brain_sessions.c.id)
            )
        ).scalar_one()
    return row


async def _read(session_factory: async_sessionmaker[AsyncSession], session_id):
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


async def test_predicate_boundary_spares_n_minus_one_and_takes_n_plus_one(
    session_factory: async_sessionmaker[AsyncSession],
    sweep_project: str,
) -> None:
    now = datetime.now(UTC)
    inside = await _insert_open_session(
        session_factory, sweep_project, "inside", now - THRESHOLD + timedelta(days=1)
    )
    outside = await _insert_open_session(
        session_factory, sweep_project, "outside", now - THRESHOLD - timedelta(days=1)
    )

    result = await PgBrainSessionRepo(session_factory).sweep_open_sessions(
        older_than=THRESHOLD, dry_run=False, now=now
    )

    swept = {candidate.id for candidate in result.candidates}
    assert outside in swept
    assert inside not in swept
    assert (await _read(session_factory, inside))["status"] == "open"
    abandoned = await _read(session_factory, outside)
    assert abandoned["status"] == "abandoned"
    assert abandoned["abandonment_reason"] == "auto_stale_7d"
    assert abandoned["ended_at"] is not None
    assert abandoned["summary"] is None
    assert abandoned["next_focus"] is None
    assert abandoned["focus_outcome"] is None


async def test_dry_run_writes_nothing(
    session_factory: async_sessionmaker[AsyncSession],
    sweep_project: str,
) -> None:
    now = datetime.now(UTC)
    ghost = await _insert_open_session(
        session_factory, sweep_project, "ghost", now - THRESHOLD - timedelta(days=1)
    )
    before = await _read(session_factory, ghost)

    result = await PgBrainSessionRepo(session_factory).sweep_open_sessions(
        older_than=THRESHOLD, dry_run=True, now=now
    )

    assert [candidate.id for candidate in result.candidates] == [ghost]
    assert result.abandoned_count == 0
    assert dict(await _read(session_factory, ghost)) == dict(before)


async def test_sweep_preserves_focus_revision_and_attributions(
    session_factory: async_sessionmaker[AsyncSession],
    sweep_project: str,
) -> None:
    now = datetime.now(UTC)
    ghost = await _insert_open_session(
        session_factory, sweep_project, "ghost-with-capture", now - THRESHOLD - timedelta(days=2)
    )
    knowledge_id = uuid4()
    async with session_factory.begin() as session:
        await session.execute(
            decisions.insert().values(
                id=knowledge_id,
                project_key=sweep_project,
                title="décision capturée avant le balayage",
                description="corps",
                reasoning="corps",
            )
        )
        await session.execute(
            brain_session_artifacts.insert().values(
                session_id=ghost,
                knowledge_id=knowledge_id,
                knowledge_type="decision",
            )
        )
    async with session_factory() as session:
        focus_before = (
            (
                await session.execute(
                    sa.select(
                        project_contexts.c.current_focus,
                        project_contexts.c.focus_revision,
                        project_contexts.c.focus_updated_at,
                    ).where(project_contexts.c.project_key == sweep_project)
                )
            )
            .mappings()
            .one()
        )

    await PgBrainSessionRepo(session_factory).sweep_open_sessions(
        older_than=THRESHOLD, dry_run=False, now=now
    )

    async with session_factory() as session:
        focus_after = (
            (
                await session.execute(
                    sa.select(
                        project_contexts.c.current_focus,
                        project_contexts.c.focus_revision,
                        project_contexts.c.focus_updated_at,
                    ).where(project_contexts.c.project_key == sweep_project)
                )
            )
            .mappings()
            .one()
        )
        attributions = (
            (
                await session.execute(
                    sa.select(brain_session_artifacts.c.knowledge_id).where(
                        brain_session_artifacts.c.session_id == ghost
                    )
                )
            )
            .scalars()
            .all()
        )

    assert dict(focus_after) == dict(focus_before)
    assert list(attributions) == [knowledge_id]
    swept = await _read(session_factory, ghost)
    assert swept["status"] == "abandoned"
    # Le snapshot terminal reste vide : c'est la contrainte CHECK
    # brain_sessions_terminal_state_valid pour 'abandoned'. Le ledger, lui,
    # vit dans brain_session_artifacts et survit.
    assert list(swept["captured_knowledge_ids"]) == []


async def test_manual_abandonment_reason_is_never_overwritten(
    session_factory: async_sessionmaker[AsyncSession],
    sweep_project: str,
) -> None:
    now = datetime.now(UTC)
    manual = await _insert_open_session(
        session_factory, sweep_project, "manual", now - THRESHOLD - timedelta(days=3)
    )
    async with session_factory.begin() as session:
        await session.execute(
            brain_sessions.update()
            .where(brain_sessions.c.id == manual)
            .values(
                status="abandoned",
                abandonment_reason="abandon manuel de l'opérateur",
                ended_at=now,
            )
        )
    # Positive control: a co-resident stale *open* session in the same sweep
    # call. Without it, this test only asserts a row nobody touched stayed
    # untouched — which a no-op implementation also satisfies. This ghost
    # must actually be swept, or the negative assertions below are vacuous.
    ghost = await _insert_open_session(
        session_factory, sweep_project, "ghost", now - THRESHOLD - timedelta(days=1)
    )

    result = await PgBrainSessionRepo(session_factory).sweep_open_sessions(
        older_than=THRESHOLD, dry_run=False, now=now
    )

    assert ghost in {candidate.id for candidate in result.candidates}
    assert (await _read(session_factory, ghost))["status"] == "abandoned"
    assert manual not in {candidate.id for candidate in result.candidates}
    row = await _read(session_factory, manual)
    assert row["abandonment_reason"] == "abandon manuel de l'opérateur"


# ─── M-G : la règle des 4 h contre une vraie base ────────────────────────────
#
# Le harnais unitaire prouve la FORME du prédicat ; il ne peut rien dire du
# CHECK `brain_sessions_terminal_state_valid` de la 046, qui n'existe que dans
# PostgreSQL. Une ligne `closed_inactive` mal formée — une raison d'abandon
# laissée en place, un `ended_at` oublié — ne se voit QUE ici, et elle ferait
# tomber la nuit entière sur une violation de contrainte.

INACTIVE = timedelta(hours=4)


async def test_an_agent_tracer_inactive_past_the_threshold_is_closed_not_abandoned(
    session_factory: async_sessionmaker[AsyncSession],
    sweep_project: str,
) -> None:
    """Le fait central de M-G, contre la base : le 4ᵉ état est ATTEIGNABLE.

    Et il l'est sous le CHECK de la 046, qui interdit sur cette branche tout ce
    qu'un abandon exige. Que la ligne existe après le statement est la preuve
    que le `CASE` produit une forme que la base accepte.
    """
    now = datetime.now(UTC)
    tracer = await _insert_open_session(
        session_factory,
        sweep_project,
        "agent-inactive",
        now - timedelta(hours=6),
        nature="agent",
        observed=now - timedelta(hours=6),
    )

    result = await PgBrainSessionRepo(session_factory).sweep_open_sessions(
        older_than=THRESHOLD, close_inactive_after=INACTIVE, dry_run=False, now=now
    )

    assert result.closed_inactive_count == 1
    assert result.abandoned_count == 0
    closed = await _read(session_factory, tracer)
    assert closed["status"] == "closed_inactive"
    assert closed["ended_at"] is not None
    # Les quatre interdits de la branche, relus un par un : c'est ce que le
    # CHECK exige, et c'est ce qui distingue cet état d'un abandon.
    assert closed["abandonment_reason"] is None
    assert closed["summary"] is None
    assert closed["next_focus"] is None
    assert closed["focus_outcome"] is None
    assert closed["nothing_to_capture_reason"] is None


async def test_a_never_observed_session_survives_the_four_hour_rule(
    session_factory: async_sessionmaker[AsyncSession],
    sweep_project: str,
) -> None:
    """S3, tranché — avec son TÉMOIN NÉGATIF dans le même test.

    `last_observed_at IS NULL` veut dire « jamais observée », pas « observée il
    y a longtemps ». La paire est indispensable : la survivante seule resterait
    verte si la règle ne prenait JAMAIS personne, et la fermée seule ne dirait
    rien du sort de `NULL`. Les deux sessions sont identiques à une colonne
    près, et cette colonne est exactement celle que S3 tranche.
    """
    now = datetime.now(UTC)
    never_observed = await _insert_open_session(
        session_factory,
        sweep_project,
        "agent-never-observed",
        now - timedelta(hours=5),
        nature="agent",
        observed=None,
    )
    observed_long_ago = await _insert_open_session(
        session_factory,
        sweep_project,
        "agent-observed-5h",
        now - timedelta(hours=5),
        nature="agent",
        observed=now - timedelta(hours=5),
    )

    result = await PgBrainSessionRepo(session_factory).sweep_open_sessions(
        older_than=THRESHOLD, close_inactive_after=INACTIVE, dry_run=False, now=now
    )

    swept = {candidate.id for candidate in result.candidates}
    assert never_observed not in swept
    assert observed_long_ago in swept
    assert (await _read(session_factory, never_observed))["status"] == "open"
    assert (await _read(session_factory, observed_long_ago))["status"] == "closed_inactive"


async def test_an_operator_session_is_never_closed_by_inactivity(
    session_factory: async_sessionmaker[AsyncSession],
    sweep_project: str,
) -> None:
    """La garantie principale n'est pas le seuil, c'est la NATURE (§0bis.3).

    Une session `claimed` — donc `operator` — n'est jamais fermée par
    inactivité, quel que soit son âge. Le CHECK de la 046 le rend d'ailleurs
    impossible en base : la branche `closed_inactive` exige `nature = 'agent'`.
    Ce test prouve que le PRÉDICAT ne l'essaie même pas, donc que la nuit ne
    tombe pas sur la contrainte.
    """
    now = datetime.now(UTC)
    operator = await _insert_open_session(
        session_factory,
        sweep_project,
        "operator-idle",
        now - timedelta(hours=48),
        nature="operator",
        observed=now - timedelta(hours=48),
    )

    result = await PgBrainSessionRepo(session_factory).sweep_open_sessions(
        older_than=THRESHOLD, close_inactive_after=INACTIVE, dry_run=False, now=now
    )

    assert operator not in {candidate.id for candidate in result.candidates}
    assert (await _read(session_factory, operator))["status"] == "open"


async def test_a_recently_observed_tracer_is_not_taken(
    session_factory: async_sessionmaker[AsyncSession],
    sweep_project: str,
) -> None:
    """Le critère de sortie que la SPEC-M-G §7 dit explicitement manquant ailleurs.

    « Ne prouve rien sur les sessions actives : il faut aussi montrer qu'une
    session ayant observé un appel dans les 4 h n'est PAS prise. »
    """
    now = datetime.now(UTC)
    active = await _insert_open_session(
        session_factory,
        sweep_project,
        "agent-active",
        now - timedelta(hours=30),
        nature="agent",
        observed=now - timedelta(minutes=3),
    )

    result = await PgBrainSessionRepo(session_factory).sweep_open_sessions(
        older_than=THRESHOLD, close_inactive_after=INACTIVE, dry_run=False, now=now
    )

    assert active not in {candidate.id for candidate in result.candidates}
    assert (await _read(session_factory, active))["status"] == "open"


async def test_seven_days_wins_over_four_hours_on_a_session_matching_both(
    session_factory: async_sessionmaker[AsyncSession],
    sweep_project: str,
) -> None:
    """PRÉSÉANCE, prouvée sur la ligne écrite — pas sur le SQL compilé.

    Une traçante inactive depuis plus que le seuil de PRÉSENCE matche les deux
    prédicats. Elle doit partir en `abandoned` AVEC sa raison, jamais en
    `closed_inactive` muet : perdre la raison, c'est perdre la seule trace de
    POURQUOI la session est terminée.
    """
    now = datetime.now(UTC)
    both = await _insert_open_session(
        session_factory,
        sweep_project,
        "agent-both-rules",
        now - THRESHOLD - timedelta(days=1),
        nature="agent",
        observed=now - THRESHOLD - timedelta(days=1),
    )

    result = await PgBrainSessionRepo(session_factory).sweep_open_sessions(
        older_than=THRESHOLD, close_inactive_after=INACTIVE, dry_run=False, now=now
    )

    assert result.abandoned_count == 1
    assert result.closed_inactive_count == 0
    row = await _read(session_factory, both)
    assert row["status"] == "abandoned"
    assert row["abandonment_reason"] == "auto_stale_7d"


async def test_a_closed_rule_leaves_an_inactive_tracer_open(
    session_factory: async_sessionmaker[AsyncSession],
    sweep_project: str,
) -> None:
    """Livré FERMÉ, prouvé contre la base et pas seulement contre le défaut.

    C'est la mutation qui compte le plus ici : le balayage tourne WET toutes les
    nuits. Si `close_inactive_after=None` ne fermait pas complètement la règle,
    merger ce lot fermerait des sessions dès la nuit suivante.
    """
    now = datetime.now(UTC)
    tracer = await _insert_open_session(
        session_factory,
        sweep_project,
        "agent-rule-closed",
        now - timedelta(hours=9),
        nature="agent",
        observed=now - timedelta(hours=9),
    )

    result = await PgBrainSessionRepo(session_factory).sweep_open_sessions(
        older_than=THRESHOLD, close_inactive_after=None, dry_run=False, now=now
    )

    assert result.inactive_cutoff is None
    assert tracer not in {candidate.id for candidate in result.candidates}
    assert (await _read(session_factory, tracer))["status"] == "open"


async def test_closing_a_tracer_preserves_its_ledger_and_the_project_focus(
    session_factory: async_sessionmaker[AsyncSession],
    sweep_project: str,
) -> None:
    """La raison d'être de la 046, et le bruit qu'elle refuse de fabriquer.

    `abandoned` FORCE `cardinality(captured_knowledge_ids) = 0` : une traçante
    qui a fait son travail y déclarerait un ledger vide. Sur `closed_inactive`
    la colonne n'a AUCUNE contrainte — c'est le point entier de la migration.

    Et `focus_revision` doit être INCHANGÉE : une fermeture groupée qui
    tenterait un CAS par ligne produirait N−1 `conflict` fabriqués sur les
    sessions opérateur concurrentes (`SPEC-M-G` §3.2).
    """
    now = datetime.now(UTC)
    tracer = await _insert_open_session(
        session_factory,
        sweep_project,
        "agent-with-ledger",
        now - timedelta(hours=7),
        nature="agent",
        observed=now - timedelta(hours=7),
    )
    knowledge_id = uuid4()
    async with session_factory.begin() as session:
        await session.execute(
            decisions.insert().values(
                id=knowledge_id,
                project_key=sweep_project,
                title="décision attribuée à une traçante",
                description="corps",
                reasoning="corps",
            )
        )
        await session.execute(
            brain_session_artifacts.insert().values(
                session_id=tracer,
                knowledge_id=knowledge_id,
                knowledge_type="decision",
            )
        )

    async with session_factory() as session:
        revision_before = (
            await session.execute(
                sa.select(project_contexts.c.focus_revision).where(
                    project_contexts.c.project_key == sweep_project
                )
            )
        ).scalar_one()

    await PgBrainSessionRepo(session_factory).sweep_open_sessions(
        older_than=THRESHOLD, close_inactive_after=INACTIVE, dry_run=False, now=now
    )

    async with session_factory() as session:
        surviving = (
            await session.execute(
                sa.select(sa.func.count())
                .select_from(brain_session_artifacts)
                .where(brain_session_artifacts.c.session_id == tracer)
            )
        ).scalar_one()
        revision_after = (
            await session.execute(
                sa.select(project_contexts.c.focus_revision).where(
                    project_contexts.c.project_key == sweep_project
                )
            )
        ).scalar_one()

    assert (await _read(session_factory, tracer))["status"] == "closed_inactive"
    assert surviving == 1, "le ledger d'une traçante fermée survit — c'est tout M-G"
    assert revision_after == revision_before, "aucun CAS tenté, donc aucun conflit fabriqué"


async def test_a_closed_inactive_row_survives_the_pydantic_rail(
    session_factory: async_sessionmaker[AsyncSession],
    sweep_project: str,
) -> None:
    """Piège (a) : le rail ne doit pas refuser ce que la base accepte.

    Le dispatcher de `BrainSession` n'a plus de branche fourre-tout, il LÈVE.
    Avant la 046 il finissait par `else: _validate_abandoned_state()`, qui exige
    `abandonment_reason` — le champ que la branche `closed_inactive` INTERDIT.
    Une ligne parfaitement valide en base aurait donc fait exploser toute
    lecture qui la croise : `brain_session_list`, le briefing, la reprise.

    C'est le seul test qui fasse aller-retour la ligne RÉELLE — écrite par le
    balayage, relue par le modèle. Les tests unitaires construisent le modèle à
    la main et ne peuvent pas voir un écart entre le CHECK et le rail.
    """
    now = datetime.now(UTC)
    tracer = await _insert_open_session(
        session_factory,
        sweep_project,
        "agent-roundtrip",
        now - timedelta(hours=6),
        nature="agent",
        observed=now - timedelta(hours=6),
    )
    repo = PgBrainSessionRepo(session_factory)
    await repo.sweep_open_sessions(
        older_than=THRESHOLD, close_inactive_after=INACTIVE, dry_run=False, now=now
    )

    loaded = await repo.get_by_id(tracer)

    assert loaded is not None
    assert loaded.status.value == "closed_inactive"
    assert loaded.nature == "agent"
    # `is_stale` n'est vrai que sur `open` : une session terminale qui le
    # porterait ferait lever le rail, et le balayage en produit toutes les nuits.
    assert loaded.is_stale is False

    listed = await repo.list(project_key=sweep_project, status="all")
    assert tracer in {session.id for session in listed.sessions}
