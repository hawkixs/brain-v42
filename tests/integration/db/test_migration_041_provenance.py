"""Migration 041 — the content date only moves on a change of value."""

from __future__ import annotations

import uuid

import pytest
import sqlalchemy as sa

pytestmark = pytest.mark.integration


class TestContentUpdatedAtTrigger:
    async def test_tag_change_does_not_stamp_content(self, db_session) -> None:
        """REORG normalise un tag : content_updated_at doit rester NULL."""
        lid = uuid.uuid4()
        await db_session.execute(
            sa.text(
                "INSERT INTO learnings (id, topic, insight, project_key, tags) "
                "VALUES (:id, 'sujet', 'corps', 'brain-v42', ARRAY['a'])"
            ),
            {"id": lid},
        )
        await db_session.execute(
            sa.text("UPDATE learnings SET tags = ARRAY['b'] WHERE id = :id"), {"id": lid}
        )
        value = (
            await db_session.execute(
                sa.text("SELECT content_updated_at FROM learnings WHERE id = :id"), {"id": lid}
            )
        ).scalar_one()
        assert value is None

    async def test_counter_write_does_not_stamp_content(self, db_session) -> None:
        """The case that produced the 23-night loop."""
        lid = uuid.uuid4()
        await db_session.execute(
            sa.text(
                "INSERT INTO learnings (id, topic, insight, project_key) "
                "VALUES (:id, 'sujet', 'corps', 'brain-v42')"
            ),
            {"id": lid},
        )
        await db_session.execute(
            sa.text("UPDATE learnings SET access_count = access_count + 1 WHERE id = :id"),
            {"id": lid},
        )
        value = (
            await db_session.execute(
                sa.text("SELECT content_updated_at FROM learnings WHERE id = :id"), {"id": lid}
            )
        ).scalar_one()
        assert value is None

    async def test_real_content_change_stamps(self, db_session) -> None:
        lid = uuid.uuid4()
        await db_session.execute(
            sa.text(
                "INSERT INTO learnings (id, topic, insight, project_key) "
                "VALUES (:id, 'sujet', 'corps', 'brain-v42')"
            ),
            {"id": lid},
        )
        await db_session.execute(
            sa.text("UPDATE learnings SET insight = 'corps révisé' WHERE id = :id"), {"id": lid}
        )
        value = (
            await db_session.execute(
                sa.text("SELECT content_updated_at FROM learnings WHERE id = :id"), {"id": lid}
            )
        ).scalar_one()
        assert value is not None

    async def test_rewriting_identical_content_does_not_stamp(self, db_session) -> None:
        """Value semantics: copying the same text back rejuvenates nothing."""
        lid = uuid.uuid4()
        await db_session.execute(
            sa.text(
                "INSERT INTO learnings (id, topic, insight, project_key) "
                "VALUES (:id, 'sujet', 'corps', 'brain-v42')"
            ),
            {"id": lid},
        )
        await db_session.execute(
            sa.text("UPDATE learnings SET insight = 'corps' WHERE id = :id"), {"id": lid}
        )
        value = (
            await db_session.execute(
                sa.text("SELECT content_updated_at FROM learnings WHERE id = :id"), {"id": lid}
            )
        ).scalar_one()
        assert value is None


class TestMigrationShape:
    async def test_update_updated_at_is_untouched(self, db_session) -> None:
        """039 pins this function by SHA256: 041 must not touch it."""
        digest = (
            await db_session.execute(
                sa.text(
                    "SELECT encode(sha256(convert_to(p.prosrc, 'UTF8')), 'hex') "
                    "FROM pg_proc p WHERE p.proname = 'update_updated_at'"
                )
            )
        ).scalar_one()
        assert digest == ("83ca0f7a3230405dae8b4f4e692b4983869b58e4225b6e60bbf96db3f6ae9a59")

    async def test_access_log_has_actor(self, db_session) -> None:
        value = (
            await db_session.execute(
                sa.text(
                    "SELECT column_default FROM information_schema.columns "
                    "WHERE table_name = 'access_log' AND column_name = 'actor'"
                )
            )
        ).scalar_one()
        assert "unknown" in value

    @pytest.mark.parametrize(
        "table",
        ["learnings", "decisions", "snippets", "runbooks", "adrs", "indexed_plans"],
    )
    async def test_access_count_human_exists(self, db_session, table: str) -> None:
        found = (
            await db_session.execute(
                sa.text(
                    "SELECT count(*) FROM information_schema.columns "
                    "WHERE table_name = :t AND column_name = 'access_count_human'"
                ),
                {"t": table},
            )
        ).scalar_one()
        assert found == 1
