"""Le pool de PROMOTE cesse de réadmettre un verdict encore valide."""

from __future__ import annotations

import uuid

import pytest
import sqlalchemy as sa

pytestmark = pytest.mark.integration

from scripts.dream.promote_prepare import fetch_candidates  # noqa: E402


@pytest.fixture
def project_key() -> str:
    """Une clé neuve par test — l'isolation est la correction, pas le nettoyage."""
    return make_promote_project_key()


def make_promote_project_key() -> str:
    """Clé de projet UNIQUE pour un test PROMOTE.

    Deux propriétés, et il faut les deux. Le préfixe ``integ-`` la rend visible du
    prédicat de purge du teardown, qui ne voyait pas la clé de production employée
    jusqu'ici — 159 lignes accumulées, mesurées le 2026-08-10. L'unicité, elle,
    découple les tests d'un MÊME run : ils partageaient une clé, donc leurs lignes
    se disputaient les dix places de ``fetch_candidates``.
    """
    return f"integ-promote-{uuid.uuid4().hex[:8]}"


class TestTerminalCache:
    async def test_uncertain_verdict_survives_a_counter_write(
        self, db_session, project_key, session_factory
    ) -> None:
        """Le défaut de production : un verdict rendu, puis une lecture, et le
        candidat revenait la nuit suivante."""
        lid = uuid.uuid4()
        await db_session.execute(
            sa.text(
                "INSERT INTO learnings "
                "(id, topic, insight, project_key, access_count, access_count_human, "
                " confidence, created_at) "
                "VALUES (:id, 'sujet', 'corps', :pk, 9, 9, 'high', "
                "        NOW() - INTERVAL '30 days')"
            ),
            {"id": lid, "pk": project_key},
        )
        await db_session.execute(
            sa.text(
                "INSERT INTO dream_promotions "
                "(source_learning_id, target_type, created_at) "
                "VALUES (:id, 'classification_uncertain', NOW())"
            ),
            {"id": lid},
        )
        # Une lecture postérieure au verdict — ce qui cassait le cache.
        await db_session.execute(
            sa.text("UPDATE learnings SET access_count = access_count + 1 WHERE id = :id"),
            {"id": lid},
        )
        await db_session.commit()

        candidates = await fetch_candidates(session_factory, project_key, limit=10)
        assert str(lid) not in {c["id"] for c in candidates}

    async def test_verdict_survives_counter_write_in_later_transaction(
        self, project_key, session_factory
    ) -> None:
        """Preuve à deux transactions distinctes, committées séparément.

        Le test ci-dessus (`test_uncertain_verdict_survives_a_counter_write`)
        écrit le verdict et la mise à jour du compteur dans la MÊME
        transaction non commitée : Postgres fige NOW() au début de la
        transaction, donc dream_promotions.created_at et le
        learnings.updated_at restampé par le trigger `update_updated_at`
        finissent identiques par coïncidence — l'ancien prédicat
        `u.created_at >= l.updated_at` passait déjà, sans prouver la
        correction. Ici, la mise à jour du compteur est une transaction
        séparée, committée après coup : son NOW() est strictement postérieur
        au created_at du verdict, donc l'ancien prédicat échouerait
        (réadmission) tandis que le nouveau, fondé sur
        COALESCE(l.content_updated_at, l.created_at), tient.
        """
        lid = uuid.uuid4()
        async with session_factory() as session:
            await session.execute(
                sa.text(
                    "INSERT INTO learnings "
                    "(id, topic, insight, project_key, access_count, access_count_human, "
                    " confidence, created_at) "
                    "VALUES (:id, 'sujet', 'corps', :pk, 9, 9, 'high', "
                    "        NOW() - INTERVAL '30 days')"
                ),
                {"id": lid, "pk": project_key},
            )
            await session.execute(
                sa.text(
                    "INSERT INTO dream_promotions "
                    "(source_learning_id, target_type, created_at) "
                    "VALUES (:id, 'classification_uncertain', NOW())"
                ),
                {"id": lid},
            )
            await session.commit()

        # Transaction séparée, committée indépendamment — le trigger
        # update_updated_at() restampe updated_at avec le NOW() de CETTE
        # transaction, postérieur au created_at du verdict ci-dessus.
        async with session_factory() as session:
            await session.execute(
                sa.text("UPDATE learnings SET access_count = access_count + 1 WHERE id = :id"),
                {"id": lid},
            )
            await session.commit()

        candidates = await fetch_candidates(session_factory, project_key, limit=10)
        assert str(lid) not in {c["id"] for c in candidates}

    async def test_real_content_edit_readmits_the_candidate(
        self, db_session, project_key, session_factory
    ) -> None:
        lid = uuid.uuid4()
        await db_session.execute(
            sa.text(
                "INSERT INTO learnings "
                "(id, topic, insight, project_key, access_count, access_count_human, "
                " confidence, created_at) "
                "VALUES (:id, 'sujet', 'corps', :pk, 9, 9, 'high', "
                "        NOW() - INTERVAL '30 days')"
            ),
            {"id": lid, "pk": project_key},
        )
        await db_session.execute(
            sa.text(
                "INSERT INTO dream_promotions "
                "(source_learning_id, target_type, created_at) "
                "VALUES (:id, 'classification_uncertain', NOW() - INTERVAL '1 day')"
            ),
            {"id": lid},
        )
        await db_session.execute(
            sa.text("UPDATE learnings SET insight = 'corps révisé' WHERE id = :id"),
            {"id": lid},
        )
        await db_session.commit()

        candidates = await fetch_candidates(session_factory, project_key, limit=10)
        assert str(lid) in {c["id"] for c in candidates}


class TestMaturityGate:
    async def test_dream_reads_alone_do_not_mature_a_learning(
        self, db_session, project_key, session_factory
    ) -> None:
        """access_count élevé mais purement machine : pas candidat."""
        lid = uuid.uuid4()
        await db_session.execute(
            sa.text(
                "INSERT INTO learnings "
                "(id, topic, insight, project_key, access_count, access_count_human, "
                " confidence, created_at) "
                "VALUES (:id, 'sujet', 'corps', :pk, 40, 0, 'high', "
                "        NOW() - INTERVAL '30 days')"
            ),
            {"id": lid, "pk": project_key},
        )
        await db_session.commit()

        candidates = await fetch_candidates(session_factory, project_key, limit=10)
        assert str(lid) not in {c["id"] for c in candidates}

    async def test_human_reads_mature_a_learning(
        self, db_session, project_key, session_factory
    ) -> None:
        lid = uuid.uuid4()
        await db_session.execute(
            sa.text(
                "INSERT INTO learnings "
                "(id, topic, insight, project_key, access_count, access_count_human, "
                " confidence, created_at) "
                "VALUES (:id, 'sujet', 'corps', :pk, 40, 4, 'high', "
                "        NOW() - INTERVAL '30 days')"
            ),
            {"id": lid, "pk": project_key},
        )
        await db_session.commit()

        candidates = await fetch_candidates(session_factory, project_key, limit=10)
        assert str(lid) in {c["id"] for c in candidates}
