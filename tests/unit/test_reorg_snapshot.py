"""Snapshot of the tags BEFORE the REORG phase — the only "before" that is observed.

The validator cannot prove an entity moved if it only knows its after-state.
`updated_at` seemed to hold that role and did not: the `DecayFlusher` emits a bulk
`UPDATE` on `learnings` and `decisions` every 300 s, and migration 001's
`update_updated_at()` trigger is UNCONDITIONAL — it has no `WHEN` clause. Worse,
it is REORG's own reads (`brain_get` before each normalisation) that feed the
access_log the flusher uses: the phase therefore manufactured its own evidence.

`content_updated_at` (migration 041) cannot replace it either: its triggers are
declared `BEFORE UPDATE OF topic, insight` on `learnings` and
`BEFORE UPDATE OF title, description, reasoning, consequences` on `decisions`.
`tags` is not among them, and REORG mutates ONLY tags — so the column stays `NULL`
on exactly the entities we are trying to verify.

That leaves the snapshot: read the project corpus's tags just before the phase,
and compare afterwards. It is the only form where the "before" is measured and not
deduced.
"""

from __future__ import annotations

import asyncio
import json
import uuid

import pytest
import pytest_asyncio
import sqlalchemy as sa
from scripts.dream.reorg_snapshot import snapshot_tags
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import NullPool

from tests.conftest import require_test_db_url
from tests.unit.keys import make_unit_project_key


@pytest_asyncio.fixture(scope="module")
async def _engine() -> AsyncEngine:  # type: ignore[misc]
    eng = create_async_engine(require_test_db_url(), poolclass=NullPool, echo=False)
    try:
        async with eng.connect() as conn:
            await conn.execute(sa.text("SELECT 1"))
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"PostgreSQL not reachable: {exc}")
    yield eng  # type: ignore[misc]
    await eng.dispose()


@pytest_asyncio.fixture
async def session_factory(_engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(_engine, class_=AsyncSession, expire_on_commit=False)


async def _seed_learning(
    session_factory: async_sessionmaker[AsyncSession],
    project_key: str,
    tags: list[str],
) -> uuid.UUID:
    from brain_v42.db.tables import learnings

    async with session_factory() as session:
        async with session.begin():
            return (
                await session.execute(
                    learnings.insert()
                    .values(
                        topic=f"t-{uuid.uuid4().hex[:6]}",
                        insight="i",
                        project_key=project_key,
                        source_type="experience",
                        confidence="high",
                        tags=tags,
                    )
                    .returning(learnings.c.id)
                )
            ).scalar_one()


async def _seed_decision(
    session_factory: async_sessionmaker[AsyncSession],
    project_key: str,
    tags: list[str],
) -> uuid.UUID:
    from brain_v42.db.tables import decisions

    async with session_factory() as session:
        async with session.begin():
            return (
                await session.execute(
                    decisions.insert()
                    .values(
                        title=f"D-{uuid.uuid4().hex[:6]}",
                        description="d",
                        reasoning="r",
                        alternatives=[],
                        project_key=project_key,
                        tags=tags,
                    )
                    .returning(decisions.c.id)
                )
            ).scalar_one()


@pytest.mark.asyncio
async def test_the_snapshot_covers_both_tables_reorg_can_mutate(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """REORG touches only `learnings` and `decisions` — so does the snapshot.

    Missing one of the two tables would give the worst possible result: an "absent
    from the snapshot" on perfectly legitimate entities, hence a red night for an
    invented reason.
    """
    pk = make_unit_project_key("rs")
    lid = await _seed_learning(session_factory, pk, ["alpha"])
    did = await _seed_decision(session_factory, pk, ["beta", "gamma"])

    taken = await snapshot_tags(session_factory, pk)

    assert taken[str(lid)] == ["alpha"]
    assert taken[str(did)] == ["beta", "gamma"]


@pytest.mark.asyncio
async def test_the_snapshot_stops_at_the_project_boundary(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """The snapshot's scope is the run's, not the whole corpus.

    A global snapshot would make "present in the snapshot" true for an entity of
    another project, and the boundary-crossing check would lose its simplest
    witness.
    """
    pk = make_unit_project_key("rs")
    foreign_pk = make_unit_project_key("rs-foreign")
    mine = await _seed_learning(session_factory, pk, ["alpha"])
    theirs = await _seed_learning(session_factory, foreign_pk, ["alpha"])

    taken = await snapshot_tags(session_factory, pk)

    assert str(mine) in taken
    assert str(theirs) not in taken


@pytest.mark.asyncio
async def test_an_entity_without_tags_is_present_with_an_empty_list(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """`[]` and "absent" must stay two distinct facts.

    Confusing them would pass off as "created during the phase" an entity that
    simply had no tag — and that is the most frequent case in the corpus REORG is
    charged with normalising.
    """
    pk = make_unit_project_key("rs")
    lid = await _seed_learning(session_factory, pk, [])

    taken = await snapshot_tags(session_factory, pk)

    assert str(lid) in taken
    assert taken[str(lid)] == []


def test_the_cli_writes_json_the_validator_can_read(
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The transport contract: a JSON object id → list of tags, on stdout.

    A SYNCHRONOUS test by design: `main` opens its own loop with `asyncio.run`,
    which refuses to nest inside pytest-asyncio's. An `async` test here would fail
    on the harness and would say nothing about the contract.
    """
    from scripts.dream import reorg_snapshot

    url = require_test_db_url()
    pk = make_unit_project_key("rs")

    async def _seed() -> uuid.UUID:
        engine = create_async_engine(url, poolclass=NullPool)
        try:
            factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
            return await _seed_learning(factory, pk, ["alpha"])
        finally:
            await engine.dispose()

    lid = asyncio.run(_seed())
    monkeypatch.setattr(
        reorg_snapshot,
        "Settings",
        lambda: type("S", (), {"postgres_url": url})(),
    )

    assert reorg_snapshot.main(["--project-key", pk]) == 0

    payload = json.loads(capsys.readouterr().out)
    assert payload[str(lid)] == ["alpha"]


def test_the_cli_requires_a_perimeter() -> None:
    """Same guard as the three validators: no snapshot without a scope.

    A snapshot mute about its project would produce this batch's most expensive
    failure: a "before" taken on the wrong corpus, hence false comparisons that
    nothing would report.
    """
    from scripts.dream import reorg_snapshot

    with pytest.raises(SystemExit) as excinfo:
        reorg_snapshot.main([])

    assert excinfo.value.code == 2
