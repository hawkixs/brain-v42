"""PROMOTE's pool stops re-admitting a verdict that is still valid."""

from __future__ import annotations

import uuid

import pytest
import sqlalchemy as sa

pytestmark = pytest.mark.integration

from scripts.dream.promote_prepare import fetch_candidates  # noqa: E402


@pytest.fixture
def project_key() -> str:
    """A fresh key per test — isolation is the fix, not the cleanup."""
    return make_promote_project_key()


def make_promote_project_key() -> str:
    """A UNIQUE project key for a PROMOTE test.

    Two properties, and both are needed. The ``integ-`` prefix makes it visible to
    the teardown's purge predicate, which did not see the production key used until
    now — 159 accumulated rows, measured on 2026-08-10. Uniqueness, for its part,
    decouples the tests of the SAME run: they shared a key, so their rows competed
    for ``fetch_candidates``'s ten slots.
    """
    return f"integ-promote-{uuid.uuid4().hex[:8]}"


class TestTerminalCache:
    async def test_uncertain_verdict_survives_a_counter_write(
        self, db_session, project_key, session_factory
    ) -> None:
        """The production defect: a verdict returned, then a read, and the candidate
        came back the following night."""
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
        # A read later than the verdict — what broke the cache.
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
        """A proof across two distinct transactions, committed separately.

        The test above (`test_uncertain_verdict_survives_a_counter_write`) writes the
        verdict and the counter update in the SAME uncommitted transaction: Postgres
        freezes NOW() at the transaction's start, so dream_promotions.created_at and
        the learnings.updated_at re-stamped by the `update_updated_at` trigger end up
        identical by coincidence — the old predicate `u.created_at >= l.updated_at`
        passed already, without proving the fix. Here, the counter update is a
        separate transaction, committed afterwards: its NOW() is strictly later than
        the verdict's created_at, so the old predicate would fail (re-admission) while
        the new one, based on COALESCE(l.content_updated_at, l.created_at), holds.
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

        # A separate transaction, committed independently — the update_updated_at()
        # trigger re-stamps updated_at with THIS transaction's NOW(), later than the
        # verdict's created_at above.
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
        """A high access_count but purely machine: not a candidate."""
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
