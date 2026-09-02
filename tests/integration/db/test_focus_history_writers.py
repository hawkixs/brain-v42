"""One canary per focus writer — the exit criterion M-D is actually judged on.

The plan is explicit about what does NOT count. The `focus_revision` bump is not
a criterion: it has held since 032 and testing it here would be testing Postgres.
The `focus_revision` ↔ history join is not one either — it is 100% green on a
database that never received a write after M-D, which is a criterion satisfied by
doing nothing.

What counts is the deferred constraint trigger (proved in
`test_migration_050_focus_history.py`) plus a canary on EACH writer. The two
INSERT paths matter most: `create` and `get_or_create`'s INSERT branch escape the
trigger entirely (route (b)), so they are the only place where these tests
measure the application path ALONE, with nothing behind it.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

import pytest
import pytest_asyncio
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from brain_v42.models.project_context import ProjectContextCreate, ProjectContextUpdate
from brain_v42.repositories.pg_project_context import PgProjectContextRepo
from brain_v42.services.roadmap_service import ProjectFocusConflictError, RoadmapService

pytestmark = pytest.mark.integration

_KEY = "w44-writer-canary"


async def _history(session_factory: async_sessionmaker[AsyncSession]) -> list[dict[str, Any]]:
    async with session_factory() as session:
        rows = (
            (
                await session.execute(
                    sa.text(
                        "SELECT focus_revision, focus, actor, source FROM project_focus_history "
                        "WHERE project_key = :k ORDER BY focus_revision"
                    ),
                    {"k": _KEY},
                )
            )
            .mappings()
            .all()
        )
    return [dict(row) for row in rows]


@pytest_asyncio.fixture(autouse=True)
async def clean(engine: AsyncEngine) -> AsyncIterator[None]:
    """No context, no trail — before and after. The trail must be lifted to be cleared."""

    async def purge() -> None:
        async with engine.begin() as connection:
            # The trigger is NAMED, never `USER`. `DISABLE TRIGGER USER` is not the
            # inverse of `ENABLE TRIGGER USER`: the first switches off whatever is
            # on, the second switches on EVERYTHING — including a trigger some
            # other migration deliberately ships disabled, which is exactly the
            # shape 050 uses for `project_contexts_focus_history_required`. On this
            # table the wildcard happens to cover one trigger today, so the bug is
            # latent rather than live; naming it removes the latency instead of
            # relying on the table never gaining a second trigger.
            await connection.execute(
                sa.text(
                    "ALTER TABLE project_focus_history "
                    "DISABLE TRIGGER project_focus_history_append_only_trigger"
                )
            )
            await connection.execute(
                sa.text("DELETE FROM project_focus_history WHERE project_key = :k"), {"k": _KEY}
            )
            await connection.execute(
                sa.text(
                    "ALTER TABLE project_focus_history "
                    "ENABLE TRIGGER project_focus_history_append_only_trigger"
                )
            )
            await connection.execute(
                sa.text("DELETE FROM project_contexts WHERE project_key = :k"), {"k": _KEY}
            )

    await purge()
    yield
    await purge()


def _create(**overrides: Any) -> ProjectContextCreate:
    fields: dict[str, Any] = {
        "project_key": _KEY,
        "name": "canary",
        "description": "canary",
        "current_focus": "focus at birth",
    }
    fields.update(overrides)
    return ProjectContextCreate(**fields)


# ── The two INSERT paths: no database guard stands behind them ───────────────


@pytest.mark.asyncio
async def test_create_historicises_revision_zero(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Route (b) makes this canary the ONLY proof for a context's first focus."""
    await PgProjectContextRepo(session_factory).create(_create())

    assert await _history(session_factory) == [
        {"focus_revision": 0, "focus": "focus at birth", "actor": None, "source": "context_upsert"}
    ]


@pytest.mark.asyncio
async def test_a_context_born_without_a_focus_records_that_too(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """NULL at birth is a fact of the trail, not an absence of one.

    Skipping it would leave revision 0 unbacked — and the day the constraint
    trigger is armed, the first UPDATE of that context would abort at COMMIT for
    a row nobody thought to write.
    """
    await PgProjectContextRepo(session_factory).create(_create(current_focus=None))

    assert await _history(session_factory) == [
        {"focus_revision": 0, "focus": None, "actor": None, "source": "context_upsert"}
    ]


@pytest.mark.asyncio
async def test_get_or_create_historicises_both_of_its_branches(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """One upsert, two branches, and the conflict branch is the B6 channel itself."""
    repo = PgProjectContextRepo(session_factory)
    await repo.get_or_create(_create())
    await repo.get_or_create(_create(current_focus="focus after the upsert"))

    assert await _history(session_factory) == [
        {"focus_revision": 0, "focus": "focus at birth", "actor": None, "source": "context_upsert"},
        {
            "focus_revision": 1,
            "focus": "focus after the upsert",
            "actor": None,
            "source": "context_upsert",
        },
    ]


@pytest.mark.asyncio
async def test_the_upsert_records_an_explicit_erasure(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """The exact shape of B6: a focus overwritten to NULL, now recoverable."""
    repo = PgProjectContextRepo(session_factory)
    await repo.get_or_create(_create(current_focus="prose worth keeping"))
    await repo.get_or_create(_create(current_focus=None))

    trail = await _history(session_factory)

    assert [row["focus"] for row in trail] == ["prose worth keeping", None]


# ── The four UPDATE paths ────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_update_focus_historicises(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    repo = PgProjectContextRepo(session_factory)
    await repo.create(_create())

    await repo.update_focus(_KEY, "focus from the tool")

    assert (await _history(session_factory))[-1] == {
        "focus_revision": 1,
        "focus": "focus from the tool",
        "actor": None,
        "source": "focus_tool",
    }


@pytest.mark.asyncio
async def test_a_generic_update_that_moves_the_focus_historicises(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    repo = PgProjectContextRepo(session_factory)
    created = await repo.create(_create())

    await repo.update(created.id, ProjectContextUpdate(current_focus="focus from a partial update"))

    assert (await _history(session_factory))[-1] == {
        "focus_revision": 1,
        "focus": "focus from a partial update",
        "actor": None,
        "source": "generic_update",
    }


@pytest.mark.asyncio
async def test_a_generic_update_that_never_touches_the_focus_writes_no_row(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Renaming a project is not authoring prose — the same rule `focus_stamp` follows.

    Without this the trail would fill with rows at an unchanged revision, and the
    PK would start refusing ordinary writes.
    """
    repo = PgProjectContextRepo(session_factory)
    created = await repo.create(_create())

    await repo.update(created.id, ProjectContextUpdate(name="renamed"))

    assert len(await _history(session_factory)) == 1


@pytest.mark.asyncio
async def test_the_roadmap_cas_historicises_and_a_conflict_writes_nothing(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Two halves of one contract: the applied CAS records, the refused one does not."""
    repo = PgProjectContextRepo(session_factory)
    await repo.create(_create())
    service = RoadmapService(session_factory)

    await service.update_project_focus(
        project_key=_KEY, current_focus="focus from the roadmap", expected_focus_revision=0
    )

    assert (await _history(session_factory))[-1] == {
        "focus_revision": 1,
        "focus": "focus from the roadmap",
        "actor": None,
        "source": "focus_tool",
    }

    with pytest.raises(ProjectFocusConflictError):
        await service.update_project_focus(
            project_key=_KEY, current_focus="never applied", expected_focus_revision=0
        )

    assert len(await _history(session_factory)) == 2, "a refused CAS leaves no trace of its intent"


@pytest.mark.asyncio
async def test_a_copy_forward_still_records_at_its_new_revision(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """The normal regime of a session close, reproduced on the CAS that shares its shape.

    Re-posting identical prose consumes the CAS token and bumps the revision
    without the text moving. The row is a content duplicate at a new revision —
    expected, not a collision, and the reading tool marks it rather than hiding it.
    """
    repo = PgProjectContextRepo(session_factory)
    await repo.create(_create())
    service = RoadmapService(session_factory)

    await service.update_project_focus(
        project_key=_KEY, current_focus="focus at birth", expected_focus_revision=0
    )

    trail = await _history(session_factory)

    assert [(row["focus_revision"], row["focus"]) for row in trail] == [
        (0, "focus at birth"),
        (1, "focus at birth"),
    ]
