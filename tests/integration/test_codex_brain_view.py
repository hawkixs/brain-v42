"""Integration tests: codex_brain_entity_v1 view + codex_ro role (migration 024).

The view is the cross-repo READ contract consumed by red-codex (S2a). It must:
  * normalize the 6 knowledge entity types into ONE stable 10-column shape,
  * bake in the project_group='red' filter (red-codex only ever sees red data),
  * resolve an ADR's superseded_by (internal per-project integer `number`) into
    a stable UUID (codex never sees the number),
and the codex_ro role must have SELECT on the view ONLY — never the base tables.

These run against the live brain PG (:5433) with the schema migrated to head.
All test rows use the 'integ-' project_key prefix so the conftest cleanup
fixture removes them.
"""

from __future__ import annotations

import os
import uuid

import pytest
import pytest_asyncio
import sqlalchemy as sa
from sqlalchemy.engine import make_url
from sqlalchemy.exc import ProgrammingError
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine
from sqlalchemy.pool import NullPool

from tests.integration.conftest import INTEGRATION_DB_URL

pytestmark = pytest.mark.integration

VIEW = "codex_brain_entity_v1"
CONTRACT_COLUMNS = {
    "id",
    "type",
    "title",
    "status",
    "freshness_status",
    "content",
    "project_key",
    "updated_at",
    "superseded_by",
    "merged_into",
}
VALID_TYPES = {"decision", "learning", "snippet", "runbook", "adr", "plan"}

CODEX_RO_PASSWORD = os.getenv("CODEX_RO_PASSWORD")


def _key(prefix: str) -> str:
    # 'integ-' prefix is required by the conftest cleanup fixture.
    return f"integ-{prefix}-{uuid.uuid4().hex[:8]}"


async def _tag_red(engine: AsyncEngine, project_key: str) -> None:
    """Register a project_contexts row putting project_key in the 'red' group."""
    async with engine.begin() as conn:
        await conn.execute(
            sa.text(
                "INSERT INTO project_contexts (project_key, name, description, "
                "project_group) VALUES (:k, :k, 'integ test', 'red')"
            ),
            {"k": project_key},
        )


async def _insert_learning(
    engine: AsyncEngine, project_key: str, topic: str, insight: str
) -> uuid.UUID:
    async with engine.begin() as conn:
        row = await conn.execute(
            sa.text(
                "INSERT INTO learnings (topic, insight, project_key) "
                "VALUES (:t, :i, :k) RETURNING id"
            ),
            {"t": topic, "i": insight, "k": project_key},
        )
        return row.scalar_one()


async def _insert_adr(
    engine: AsyncEngine,
    project_key: str,
    number: int,
    *,
    superseded_by: int | None = None,
) -> uuid.UUID:
    async with engine.begin() as conn:
        row = await conn.execute(
            sa.text(
                "INSERT INTO adrs (number, title, context, decision, consequences, "
                "project_key, status, superseded_by) "
                "VALUES (:n, :title, 'ctx', 'dec', 'cons', :k, :status, :sup) "
                "RETURNING id"
            ),
            {
                "n": number,
                "title": f"ADR {number}",
                "k": project_key,
                "status": "superseded" if superseded_by is not None else "accepted",
                "sup": superseded_by,
            },
        )
        return row.scalar_one()


class TestViewShape:
    async def test_view_exposes_exactly_the_contract_columns(self, engine: AsyncEngine) -> None:
        async with engine.connect() as conn:
            rows = (
                await conn.execute(
                    sa.text(
                        "SELECT column_name, data_type FROM information_schema.columns "
                        "WHERE table_name = :v"
                    ),
                    {"v": VIEW},
                )
            ).all()
        cols = dict(rows)
        assert set(cols) == CONTRACT_COLUMNS, f"unexpected column set: {set(cols)}"
        assert cols["id"] == "uuid"
        assert cols["superseded_by"] == "uuid"
        assert cols["merged_into"] == "uuid"
        assert cols["updated_at"].startswith("timestamp")

    async def test_type_column_only_holds_the_six_types(self, engine: AsyncEngine) -> None:
        async with engine.connect() as conn:
            present = {
                r[0]
                for r in (
                    await conn.execute(sa.text(f"SELECT DISTINCT type FROM {VIEW}"))  # noqa: S608
                ).all()
            }
        assert present <= VALID_TYPES, f"view leaked non-contract types: {present}"


class TestRedScoping:
    async def test_only_red_group_entities_are_visible(self, engine: AsyncEngine) -> None:
        red_key = _key("red")
        nonred_key = _key("nonred")
        await _tag_red(engine, red_key)
        red_id = await _insert_learning(engine, red_key, "red topic", "red insight")
        nonred_id = await _insert_learning(engine, nonred_key, "nonred topic", "nonred insight")

        async with engine.connect() as conn:
            visible = {
                r[0]
                for r in (
                    await conn.execute(
                        sa.text(
                            f"SELECT id FROM {VIEW} WHERE id = ANY(:ids)"  # noqa: S608
                        ),
                        {"ids": [red_id, nonred_id]},
                    )
                ).all()
            }
        assert red_id in visible, "red-group entity must be visible"
        assert nonred_id not in visible, "non-red entity must be filtered out"

    async def test_learning_maps_topic_to_title_and_insight_to_content(
        self, engine: AsyncEngine
    ) -> None:
        red_key = _key("red")
        await _tag_red(engine, red_key)
        lid = await _insert_learning(engine, red_key, "the topic", "the insight body")
        async with engine.connect() as conn:
            row = (
                (
                    await conn.execute(
                        sa.text(
                            f"SELECT type, title, content, status FROM {VIEW} "  # noqa: S608
                            "WHERE id = :id"
                        ),
                        {"id": lid},
                    )
                )
                .mappings()
                .one()
            )
        assert row["type"] == "learning"
        assert row["title"] == "the topic"
        assert "the insight body" in row["content"]
        assert row["status"] is None  # learnings have no status column


class TestAdrSupersededByResolution:
    async def test_adr_superseded_by_is_resolved_to_uuid(self, engine: AsyncEngine) -> None:
        red_key = _key("red")
        await _tag_red(engine, red_key)
        successor_id = await _insert_adr(engine, red_key, number=2)
        old_id = await _insert_adr(engine, red_key, number=1, superseded_by=2)

        async with engine.connect() as conn:
            old_row = (
                (
                    await conn.execute(
                        sa.text(
                            f"SELECT superseded_by FROM {VIEW} WHERE id = :id"  # noqa: S608
                        ),
                        {"id": old_id},
                    )
                )
                .mappings()
                .one()
            )
            successor_row = (
                (
                    await conn.execute(
                        sa.text(
                            f"SELECT superseded_by FROM {VIEW} WHERE id = :id"  # noqa: S608
                        ),
                        {"id": successor_id},
                    )
                )
                .mappings()
                .one()
            )
        # The integer number 1->2 must surface as the successor's stable UUID.
        assert old_row["superseded_by"] == successor_id
        # A non-superseded ADR exposes NULL, never a dangling number.
        assert successor_row["superseded_by"] is None


class TestCodexRoLeastPrivilege:
    @pytest_asyncio.fixture
    async def codex_ro_engine(self) -> AsyncEngine:  # type: ignore[misc]
        if not CODEX_RO_PASSWORD:
            pytest.skip("CODEX_RO_PASSWORD is required for codex_ro login tests")
        dsn = make_url(INTEGRATION_DB_URL).set(
            username="codex_ro",
            password=CODEX_RO_PASSWORD,
        )
        eng = create_async_engine(dsn, poolclass=NullPool, echo=False)
        yield eng  # type: ignore[misc]
        await eng.dispose()

    async def test_codex_ro_can_select_the_view(self, codex_ro_engine: AsyncEngine) -> None:
        async with codex_ro_engine.connect() as conn:
            # Must not raise — SELECT on the view is the only thing codex_ro can do.
            await conn.execute(sa.text(f"SELECT count(*) FROM {VIEW}"))  # noqa: S608

    @pytest.mark.parametrize("table", ["learnings", "decisions", "snippets", "runbooks", "adrs"])
    async def test_codex_ro_cannot_read_base_tables(
        self, codex_ro_engine: AsyncEngine, table: str
    ) -> None:
        with pytest.raises(ProgrammingError) as exc:
            async with codex_ro_engine.connect() as conn:
                await conn.execute(sa.text(f"SELECT * FROM {table} LIMIT 1"))  # noqa: S608
        assert "permission denied" in str(exc.value).lower()
