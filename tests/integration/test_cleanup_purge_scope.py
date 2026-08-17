"""Le teardown d'intégration ne purge que ce qui porte le préfixe ``integ-``.

Ticket adfc24eb. Mesuré le 2026-08-10 sur ``brain_test`` : 159 learnings sous
``project_key='brain-v42'``, toutes portant ``topic='sujet'`` — donc 100 % issues de
``tests/integration/dream/test_promote_prepare_provenance.py``, qui semait sous une clé
de PRODUCTION que le prédicat de purge ne matche pas. Sous ``integ%`` : zéro ligne. La
purge fait exactement ce qu'elle vise ; elle ne vise pas assez large.

CE QUE LE TICKET SE TROMPE, et il faut le dire : il désigne
``test_real_content_edit_readmits_the_candidate`` comme victime. C'était vrai le
2026-08-06. Le commit 508439d2 du 08-08 a basculé l'``ORDER BY`` de ``promote_prepare``
de ``access_count`` vers ``access_count_human``, ce qui inverse le classement. Avec le
code d'aujourd'hui c'est ``test_human_reads_mature_a_learning`` (acch=4) qui sera évincé,
et le premier (acch=9) est rang 1 et ne tombera jamais. Écrire la reproduction depuis la
prose du ticket viserait le seul test qui ne peut pas casser.

CE QU'ON NE FERA JAMAIS : ajouter ``DELETE FROM learnings WHERE project_key='brain-v42'``
au teardown. Le garde-fou du conftest ne refuse que le NOM de base ``brain`` ; un
``BRAIN_V42_TEST_DB_URL`` pointé sur une restauration effacerait les learnings réels du
projet. La correction est en amont — des clés uniques — pas en aval.
"""

from __future__ import annotations

import uuid

import pytest
import sqlalchemy as sa

from tests.integration.conftest import purge_integration_rows
from tests.integration.dream.test_promote_prepare_provenance import make_promote_project_key

pytestmark = pytest.mark.integration


async def _seed(session, project_key: str, *, access_count_human: int, age_days: int) -> uuid.UUID:
    learning_id = uuid.uuid4()
    await session.execute(
        sa.text(
            "INSERT INTO learnings (id, topic, insight, project_key, access_count, "
            "access_count_human, confidence, created_at, updated_at) "
            "VALUES (:id, 'sujet', 'corps', :pk, :ac, :acch, 'high', "
            "NOW() - make_interval(days => :age), NOW() - make_interval(days => :age))"
        ),
        {
            "id": learning_id,
            "pk": project_key,
            "ac": access_count_human,
            "acch": access_count_human,
            "age": age_days,
        },
    )
    return learning_id


@pytest.mark.asyncio
async def test_each_promote_test_gets_its_own_project_key() -> None:
    """Le témoin DIRECT de l'hermétisme, sans base.

    Il retombe si quelqu'un revient à une constante partagée, même en gardant le
    préfixe ``integ-`` : le préfixe règle la purge, l'unicité règle le couplage
    entre tests d'un même run.
    """
    assert make_promote_project_key() != make_promote_project_key()


@pytest.mark.asyncio
async def test_the_promote_fixture_key_is_actually_purged(engine, db_session) -> None:
    key = make_promote_project_key()
    await _seed(db_session, key, access_count_human=9, age_days=30)
    await db_session.commit()

    async with engine.begin() as conn:
        await purge_integration_rows(conn)

    remaining = (
        await db_session.execute(
            sa.text("SELECT count(*) FROM learnings WHERE project_key = :pk"), {"pk": key}
        )
    ).scalar_one()
    assert remaining == 0, (
        "la clé semée par les tests PROMOTE survit au teardown : elle s'accumulera "
        "d'un run à l'autre jusqu'à évincer un vrai candidat"
    )


@pytest.mark.asyncio
async def test_the_purge_leaves_a_non_integration_key_alone(engine, db_session) -> None:
    """La sonde NÉGATIVE de la purge, et le garde-fou du correctif dangereux.

    Elle est verte dès l'écriture — ce n'est pas un RED. Sa raison d'être est
    d'échouer le jour où quelqu'un élargit le prédicat pour « nettoyer aussi
    brain-v42 » et efface des données réelles.
    """
    key = f"notinteg-{uuid.uuid4().hex[:8]}"
    learning_id = await _seed(db_session, key, access_count_human=1, age_days=1)
    await db_session.commit()
    try:
        async with engine.begin() as conn:
            await purge_integration_rows(conn)

        remaining = (
            await db_session.execute(
                sa.text("SELECT count(*) FROM learnings WHERE project_key = :pk"), {"pk": key}
            )
        ).scalar_one()
        assert remaining == 1, (
            "le teardown a effacé une clé hors de son périmètre — c'est le mode de "
            "panne qui détruirait des données réelles sur une base restaurée"
        )
    finally:
        await db_session.execute(
            sa.text("DELETE FROM learnings WHERE id = :id"), {"id": learning_id}
        )
        await db_session.commit()


@pytest.mark.asyncio
async def test_accumulated_survivors_do_not_evict_a_freshly_matured_row(
    engine, db_session, session_factory
) -> None:
    """La reproduction du VRAI mode de panne — le seul test qui prouve l'utilité.

    Dix survivants acch=9 laissés par dix runs antérieurs, puis une ligne acch=4
    fraîchement mûrie. Avec la clé partagée, ``ORDER BY access_count_human DESC``
    et ``LIMIT 10`` rendent les dix vieux et la nouvelle est au rang onze : le test
    qui la cherche échoue, sans que rien n'ait changé dans le code de production.
    """
    from scripts.dream.promote_prepare import fetch_candidates

    old_key = make_promote_project_key()
    for _ in range(10):
        await _seed(db_session, old_key, access_count_human=9, age_days=30)

    new_key = make_promote_project_key()
    # 8 jours, pas 1 : le filtre exige (NOW() - created_at) >= 7 jours. « Fraîche »
    # veut dire fraîchement MÛRIE — elle vient de franchir le seuil de lectures
    # humaines — pas fraîchement créée.
    await _seed(db_session, new_key, access_count_human=4, age_days=8)
    await db_session.commit()

    try:
        candidates = await fetch_candidates(session_factory, new_key, limit=10)
        assert len(candidates) == 1, (
            "les survivants d'anciens runs ont évincé la ligne fraîchement mûrie : "
            f"{len(candidates)} candidats rendus"
        )
    finally:
        async with engine.begin() as conn:
            await purge_integration_rows(conn)
