"""PostgreSQL proofs for atomic, CAS-aware project focus updates."""

from __future__ import annotations

import asyncio
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from brain_v42.db.tables import features, project_contexts
from brain_v42.services import roadmap_service

pytestmark = pytest.mark.integration


@pytest_asyncio.fixture
async def focus_project(
    session_factory: async_sessionmaker[AsyncSession],
) -> tuple[str, dict[str, UUID]]:
    project_key = f"integ-focus-cas-{uuid4().hex[:10]}"
    feature_ids = {name: uuid4() for name in ("alpha", "beta", "gamma", "merged")}
    async with session_factory.begin() as session:
        await session.execute(
            project_contexts.insert().values(
                project_key=project_key,
                name="Focus CAS integration",
                description="Atomic focus and roadmap fixture",
                current_focus="initial focus",
                blockers=[],
            )
        )
        await session.execute(
            features.insert(),
            [
                {
                    "id": feature_ids["alpha"],
                    "project_key": project_key,
                    "name": "Alpha",
                    "description": "alpha",
                    "status": "planned",
                    "pinned": False,
                    "merged_into": None,
                },
                {
                    "id": feature_ids["beta"],
                    "project_key": project_key,
                    "name": "Beta",
                    "description": "beta",
                    "status": "building",
                    "pinned": True,
                    "merged_into": None,
                },
                {
                    "id": feature_ids["gamma"],
                    "project_key": project_key,
                    "name": "Gamma",
                    "description": "gamma",
                    "status": "planned",
                    "pinned": True,
                    "merged_into": None,
                },
                {
                    "id": feature_ids["merged"],
                    "project_key": project_key,
                    "name": "Merged",
                    "description": "merged",
                    "status": "archived",
                    "pinned": False,
                    "merged_into": feature_ids["alpha"],
                },
            ],
        )
    return project_key, feature_ids


async def _state(
    session_factory: async_sessionmaker[AsyncSession],
    project_key: str,
) -> tuple[dict, dict[str, dict]]:
    async with session_factory() as session:
        context = dict(
            (
                await session.execute(
                    sa.select(
                        project_contexts.c.current_focus,
                        project_contexts.c.focus_revision,
                        project_contexts.c.blockers,
                    ).where(project_contexts.c.project_key == project_key)
                )
            )
            .mappings()
            .one()
        )
        rows = (
            (
                await session.execute(
                    sa.select(
                        features.c.name,
                        features.c.status,
                        features.c.pinned,
                    ).where(features.c.project_key == project_key)
                )
            )
            .mappings()
            .all()
        )
    return context, {row["name"]: dict(row) for row in rows}


async def test_valid_focus_and_feature_batch_commits_atomically(
    session_factory: async_sessionmaker[AsyncSession],
    focus_project: tuple[str, dict[str, UUID]],
) -> None:
    project_key, _ = focus_project
    service = roadmap_service.RoadmapService(session_factory)

    result = await service.update_project_focus(
        project_key,
        "stabilisation",
        expected_focus_revision=0,
        blockers=["none"],
        feature_status={"Alpha": "done", "Beta": "archived"},
        unpin=["Gamma"],
    )

    context, feature_rows = await _state(session_factory, project_key)
    assert result.focus_revision == 1
    assert result.features_updated == ("Alpha", "Beta")
    assert result.features_unpinned == ("Gamma",)
    assert context == {
        "current_focus": "stabilisation",
        "focus_revision": 1,
        "blockers": ["none"],
    }
    assert feature_rows["Alpha"]["status"] == "done"
    assert feature_rows["Alpha"]["pinned"] is True
    assert feature_rows["Beta"]["status"] == "archived"
    assert feature_rows["Beta"]["pinned"] is False
    assert feature_rows["Gamma"]["pinned"] is False


@pytest.mark.parametrize("invalid_status", ["in_progress", "unknown"])
async def test_invalid_status_rolls_back_focus_and_all_features(
    session_factory: async_sessionmaker[AsyncSession],
    focus_project: tuple[str, dict[str, UUID]],
    invalid_status: str,
) -> None:
    project_key, _ = focus_project
    before = await _state(session_factory, project_key)

    with pytest.raises(roadmap_service.ProjectFocusValidationError):
        await roadmap_service.RoadmapService(session_factory).update_project_focus(
            project_key,
            "must not persist",
            expected_focus_revision=0,
            feature_status={"Alpha": "done", "Beta": invalid_status},
        )

    assert await _state(session_factory, project_key) == before


async def test_missing_feature_rolls_back_known_feature_and_focus(
    session_factory: async_sessionmaker[AsyncSession],
    focus_project: tuple[str, dict[str, UUID]],
) -> None:
    project_key, _ = focus_project
    before = await _state(session_factory, project_key)

    with pytest.raises(roadmap_service.ProjectFocusValidationError) as error:
        await roadmap_service.RoadmapService(session_factory).update_project_focus(
            project_key,
            "must not persist",
            expected_focus_revision=0,
            feature_status={"Alpha": "done", "Missing": "planned"},
        )

    assert "Missing" in str(error.value)
    assert await _state(session_factory, project_key) == before


async def test_merged_feature_cannot_be_reactivated(
    session_factory: async_sessionmaker[AsyncSession],
    focus_project: tuple[str, dict[str, UUID]],
) -> None:
    project_key, _ = focus_project
    before = await _state(session_factory, project_key)

    with pytest.raises(roadmap_service.ProjectFocusValidationError):
        await roadmap_service.RoadmapService(session_factory).update_project_focus(
            project_key,
            "must not persist",
            expected_focus_revision=0,
            feature_status={"Merged": "building"},
        )

    assert await _state(session_factory, project_key) == before


async def test_concurrent_same_revision_has_one_winner_and_no_silent_overwrite(
    session_factory: async_sessionmaker[AsyncSession],
    focus_project: tuple[str, dict[str, UUID]],
) -> None:
    project_key, _ = focus_project
    first = roadmap_service.RoadmapService(session_factory)
    second = roadmap_service.RoadmapService(session_factory)

    outcomes = await asyncio.gather(
        first.update_project_focus(project_key, "focus A", expected_focus_revision=0),
        second.update_project_focus(project_key, "focus B", expected_focus_revision=0),
        return_exceptions=True,
    )

    successes = [value for value in outcomes if not isinstance(value, Exception)]
    conflicts = [
        value for value in outcomes if isinstance(value, roadmap_service.ProjectFocusConflictError)
    ]
    assert len(successes) == 1
    assert len(conflicts) == 1
    context, _ = await _state(session_factory, project_key)
    assert context["current_focus"] in {"focus A", "focus B"}
    assert context["focus_revision"] == 1
    assert conflicts[0].current_revision == 1


async def test_same_focus_still_consumes_revision_for_composite_mutation(
    session_factory: async_sessionmaker[AsyncSession],
    focus_project: tuple[str, dict[str, UUID]],
) -> None:
    project_key, _ = focus_project
    first = roadmap_service.RoadmapService(session_factory)
    second = roadmap_service.RoadmapService(session_factory)

    outcomes = await asyncio.gather(
        first.update_project_focus(
            project_key,
            "initial focus",
            expected_focus_revision=0,
            blockers=["blocker A"],
        ),
        second.update_project_focus(
            project_key,
            "initial focus",
            expected_focus_revision=0,
            blockers=["blocker B"],
        ),
        return_exceptions=True,
    )

    successes = [value for value in outcomes if not isinstance(value, Exception)]
    conflicts = [
        value for value in outcomes if isinstance(value, roadmap_service.ProjectFocusConflictError)
    ]
    assert len(successes) == 1
    assert len(conflicts) == 1
    context, _ = await _state(session_factory, project_key)
    assert context["current_focus"] == "initial focus"
    assert context["blockers"] in (["blocker A"], ["blocker B"])
    assert context["focus_revision"] == 1
    assert conflicts[0].current_revision == 1
