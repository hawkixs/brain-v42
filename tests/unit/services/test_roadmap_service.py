"""Unit tests for RoadmapService — roadmap display + feature status updates.

All tests use AsyncMock/MagicMock — no real DB required.

Test classes:
1. TestImportability — module and class are importable
2. TestConstructor — session_factory stored on instance
3. TestGetRoadmap — fetches rows and pivots them into RoadmapProject list
4. TestPivotRowsBasic — groups flat rows into nested RoadmapProject -> RoadmapFeature
5. TestPivotRowsArtifactCounts — artifact_count populated from type_count
6. TestPivotRowsLastActivity — last_activity tracks max across artifact types
7. TestPivotRowsPinnedAndOrdering — pinned flag and feature ordering
8. TestPivotRowsMultipleProjects — multiple project_keys produce multiple RoadmapProject
9. TestPivotRowsEdgeCases — empty rows, null artifact_type, null type_count
10. TestUpdateFeatureStatuses — updates status+pinned via SQL, returns count
11. TestUpdateFeatureStatusesNotFound — logs warning when feature not found
12. TestUnpinFeatures — unpins features by name, returns rowcount
13. TestUnpinFeaturesEmptyList — returns 0 immediately for empty list
"""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from brain_v42.models.feature import RoadmapFeature, RoadmapProject
from brain_v42.services.roadmap_service import _ARTIFACT_TYPES, RoadmapService

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

NOW = datetime(2026, 3, 14, 12, 0, 0, tzinfo=UTC)
EARLIER = datetime(2026, 3, 13, 10, 0, 0, tzinfo=UTC)
LATER = datetime(2026, 3, 14, 18, 0, 0, tzinfo=UTC)


def make_row(
    *,
    project_key: str = "brain-v42",
    project_name: str = "Brain V42",
    current_phase: str | None = "M5",
    feature_id: int = 1,
    feature_name: str = "Feature A",
    status: str = "in_progress",
    status_updated_at: datetime = NOW,
    pinned: bool = False,
    artifact_type: str | None = None,
    type_count: int | None = None,
    type_last_activity: datetime | None = None,
) -> SimpleNamespace:
    """Build a fake row matching the _ROADMAP_SQL result columns."""
    return SimpleNamespace(
        project_key=project_key,
        project_name=project_name,
        current_phase=current_phase,
        feature_id=feature_id,
        feature_name=feature_name,
        status=status,
        status_updated_at=status_updated_at,
        pinned=pinned,
        artifact_type=artifact_type,
        type_count=type_count,
        type_last_activity=type_last_activity,
    )


def make_session_factory(rows: list | None = None) -> MagicMock:
    """Create a mock session_factory that returns a session as async context manager.

    The session.execute() returns a result proxy whose fetchall() returns `rows`.
    """
    mock_result = MagicMock()
    mock_result.fetchall.return_value = rows or []

    mock_session = AsyncMock()
    mock_session.execute = AsyncMock(return_value=mock_result)
    mock_session.commit = AsyncMock()

    # async context manager: async with session_factory() as session:
    mock_cm = AsyncMock()
    mock_cm.__aenter__ = AsyncMock(return_value=mock_session)
    mock_cm.__aexit__ = AsyncMock(return_value=False)

    mock_factory = MagicMock()
    mock_factory.return_value = mock_cm

    return mock_factory, mock_session


def make_update_result(rowcount: int) -> MagicMock:
    """Create a mock result object with a given rowcount."""
    result = MagicMock()
    result.rowcount = rowcount
    return result


# ---------------------------------------------------------------------------
# TestImportability
# ---------------------------------------------------------------------------


class TestImportability:
    def test_importable_from_module(self) -> None:
        """RoadmapService is importable from brain_v42.services.roadmap_service."""
        from brain_v42.services.roadmap_service import RoadmapService  # noqa: F811

        assert RoadmapService is not None

    def test_artifact_types_constant_is_tuple(self) -> None:
        """_ARTIFACT_TYPES is a tuple with expected artifact types."""
        assert isinstance(_ARTIFACT_TYPES, tuple)
        assert "learning" in _ARTIFACT_TYPES
        assert "decision" in _ARTIFACT_TYPES
        assert "snippet" in _ARTIFACT_TYPES
        assert "runbook" in _ARTIFACT_TYPES
        assert "adr" in _ARTIFACT_TYPES
        assert "plan" in _ARTIFACT_TYPES
        assert "gitlab_event" in _ARTIFACT_TYPES


# ---------------------------------------------------------------------------
# TestConstructor
# ---------------------------------------------------------------------------


class TestConstructor:
    def test_constructor_stores_session_factory(self) -> None:
        """Constructor accepts session_factory and stores it as self._sf."""
        mock_sf, _ = make_session_factory()
        svc = RoadmapService(session_factory=mock_sf)
        assert svc._sf is mock_sf


# ---------------------------------------------------------------------------
# TestGetRoadmap — async method that executes SQL and pivots
# ---------------------------------------------------------------------------


class TestGetRoadmap:
    async def test_get_roadmap_returns_list_of_roadmap_projects(self) -> None:
        """get_roadmap() returns a list of RoadmapProject objects."""
        rows = [
            make_row(feature_id=1, feature_name="Feature A", status="done"),
        ]
        mock_sf, mock_session = make_session_factory(rows)
        svc = RoadmapService(session_factory=mock_sf)

        result = await svc.get_roadmap()

        assert isinstance(result, list)
        assert len(result) == 1
        assert isinstance(result[0], RoadmapProject)

    async def test_get_roadmap_passes_project_key_to_sql(self) -> None:
        """get_roadmap(project_key='X') passes {'pk': 'X'} to execute."""
        mock_sf, mock_session = make_session_factory([])
        svc = RoadmapService(session_factory=mock_sf)

        await svc.get_roadmap(project_key="brain-v42")

        mock_session.execute.assert_awaited_once()
        call_args = mock_session.execute.call_args
        assert call_args[0][1] == {"pk": "brain-v42"}

    async def test_get_roadmap_passes_none_by_default(self) -> None:
        """get_roadmap() with no arg passes {'pk': None} to fetch all projects."""
        mock_sf, mock_session = make_session_factory([])
        svc = RoadmapService(session_factory=mock_sf)

        await svc.get_roadmap()

        call_args = mock_session.execute.call_args
        assert call_args[0][1] == {"pk": None}

    async def test_get_roadmap_empty_db_returns_empty_list(self) -> None:
        """get_roadmap() with no rows returns empty list."""
        mock_sf, _ = make_session_factory([])
        svc = RoadmapService(session_factory=mock_sf)

        result = await svc.get_roadmap()

        assert result == []

    async def test_get_roadmap_multiple_features(self) -> None:
        """get_roadmap() with multiple features returns all of them in one project."""
        rows = [
            make_row(feature_id=1, feature_name="Feature A", status="done"),
            make_row(feature_id=2, feature_name="Feature B", status="planned"),
        ]
        mock_sf, _ = make_session_factory(rows)
        svc = RoadmapService(session_factory=mock_sf)

        result = await svc.get_roadmap()

        assert len(result) == 1
        assert len(result[0].features) == 2
        names = [f.name for f in result[0].features]
        assert "Feature A" in names
        assert "Feature B" in names


# ---------------------------------------------------------------------------
# TestPivotRowsBasic — basic row pivoting
# ---------------------------------------------------------------------------


class TestPivotRowsBasic:
    def test_single_feature_no_artifacts(self) -> None:
        """A single row with no artifact_type produces one project with one feature."""
        svc = RoadmapService(session_factory=MagicMock())
        rows = [
            make_row(
                feature_id=1,
                feature_name="Auth System",
                status="planned",
                artifact_type=None,
                type_count=None,
            ),
        ]

        result = svc._pivot_rows(rows)

        assert len(result) == 1
        project = result[0]
        assert isinstance(project, RoadmapProject)
        assert project.project_key == "brain-v42"
        assert project.name == "Brain V42"
        assert project.current_phase == "M5"
        assert len(project.features) == 1

        feature = project.features[0]
        assert isinstance(feature, RoadmapFeature)
        assert feature.name == "Auth System"
        assert feature.status == "planned"
        assert feature.status_updated_at == NOW
        assert feature.pinned is False

    def test_artifact_count_initialized_to_zeros(self) -> None:
        """Feature with no artifacts has all artifact_count keys set to 0."""
        svc = RoadmapService(session_factory=MagicMock())
        rows = [make_row(artifact_type=None, type_count=None)]

        result = svc._pivot_rows(rows)

        feature = result[0].features[0]
        for art_type in _ARTIFACT_TYPES:
            assert feature.artifact_count[art_type] == 0

    def test_feature_status_preserved(self) -> None:
        """Feature status is preserved from the row."""
        svc = RoadmapService(session_factory=MagicMock())
        rows = [make_row(status="done")]

        result = svc._pivot_rows(rows)

        assert result[0].features[0].status == "done"

    def test_empty_rows_returns_empty_list(self) -> None:
        """Empty row list returns empty project list."""
        svc = RoadmapService(session_factory=MagicMock())

        result = svc._pivot_rows([])

        assert result == []


# ---------------------------------------------------------------------------
# TestPivotRowsArtifactCounts — artifact count population
# ---------------------------------------------------------------------------


class TestPivotRowsArtifactCounts:
    def test_single_artifact_type_counted(self) -> None:
        """A row with artifact_type='learning' and type_count=3 sets count correctly."""
        svc = RoadmapService(session_factory=MagicMock())
        rows = [
            make_row(
                feature_id=1,
                artifact_type="learning",
                type_count=3,
                type_last_activity=NOW,
            ),
        ]

        result = svc._pivot_rows(rows)

        feature = result[0].features[0]
        assert feature.artifact_count["learning"] == 3
        assert feature.artifact_count["decision"] == 0

    def test_multiple_artifact_types_for_same_feature(self) -> None:
        """Multiple rows for the same feature with different artifact types."""
        svc = RoadmapService(session_factory=MagicMock())
        rows = [
            make_row(
                feature_id=1,
                artifact_type="learning",
                type_count=3,
                type_last_activity=EARLIER,
            ),
            make_row(
                feature_id=1,
                artifact_type="decision",
                type_count=2,
                type_last_activity=NOW,
            ),
            make_row(
                feature_id=1,
                artifact_type="snippet",
                type_count=1,
                type_last_activity=EARLIER,
            ),
        ]

        result = svc._pivot_rows(rows)

        assert len(result) == 1
        assert len(result[0].features) == 1
        feature = result[0].features[0]
        assert feature.artifact_count["learning"] == 3
        assert feature.artifact_count["decision"] == 2
        assert feature.artifact_count["snippet"] == 1
        assert feature.artifact_count["runbook"] == 0
        assert feature.artifact_count["adr"] == 0
        assert feature.artifact_count["plan"] == 0
        assert feature.artifact_count["gitlab_event"] == 0

    def test_zero_type_count_not_set(self) -> None:
        """Row with type_count=0 (falsy) does not update artifact_count."""
        svc = RoadmapService(session_factory=MagicMock())
        rows = [
            make_row(
                feature_id=1,
                artifact_type="learning",
                type_count=0,
                type_last_activity=NOW,
            ),
        ]

        result = svc._pivot_rows(rows)

        feature = result[0].features[0]
        # type_count=0 is falsy, so the branch `if row.artifact_type and row.type_count`
        # should NOT execute — count remains 0
        assert feature.artifact_count["learning"] == 0

    def test_null_artifact_type_ignored(self) -> None:
        """Row with artifact_type=None does not affect artifact_count."""
        svc = RoadmapService(session_factory=MagicMock())
        rows = [
            make_row(
                feature_id=1,
                artifact_type=None,
                type_count=5,
                type_last_activity=NOW,
            ),
        ]

        result = svc._pivot_rows(rows)

        feature = result[0].features[0]
        for art_type in _ARTIFACT_TYPES:
            assert feature.artifact_count[art_type] == 0


# ---------------------------------------------------------------------------
# TestPivotRowsLastActivity — last_activity tracking
# ---------------------------------------------------------------------------


class TestPivotRowsLastActivity:
    def test_last_activity_defaults_to_status_updated_at(self) -> None:
        """Without artifacts, last_activity equals status_updated_at."""
        svc = RoadmapService(session_factory=MagicMock())
        rows = [make_row(status_updated_at=NOW, artifact_type=None)]

        result = svc._pivot_rows(rows)

        assert result[0].features[0].last_activity == NOW

    def test_last_activity_updated_by_newer_artifact(self) -> None:
        """last_activity updates when artifact type_last_activity is later."""
        svc = RoadmapService(session_factory=MagicMock())
        rows = [
            make_row(
                feature_id=1,
                status_updated_at=EARLIER,
                artifact_type="learning",
                type_count=1,
                type_last_activity=LATER,
            ),
        ]

        result = svc._pivot_rows(rows)

        assert result[0].features[0].last_activity == LATER

    def test_last_activity_not_overridden_by_older_artifact(self) -> None:
        """last_activity not overridden when artifact type_last_activity is older."""
        svc = RoadmapService(session_factory=MagicMock())
        rows = [
            make_row(
                feature_id=1,
                status_updated_at=NOW,
                artifact_type="decision",
                type_count=2,
                type_last_activity=LATER,
            ),
            make_row(
                feature_id=1,
                status_updated_at=NOW,
                artifact_type="learning",
                type_count=1,
                type_last_activity=EARLIER,
            ),
        ]

        result = svc._pivot_rows(rows)

        # LATER is the max
        assert result[0].features[0].last_activity == LATER

    def test_last_activity_with_none_type_last_activity(self) -> None:
        """type_last_activity=None does not override last_activity."""
        svc = RoadmapService(session_factory=MagicMock())
        rows = [
            make_row(
                feature_id=1,
                status_updated_at=NOW,
                artifact_type="snippet",
                type_count=1,
                type_last_activity=None,
            ),
        ]

        result = svc._pivot_rows(rows)

        # last_activity should still be NOW (from status_updated_at)
        assert result[0].features[0].last_activity == NOW

    def test_last_activity_picks_max_across_multiple_artifacts(self) -> None:
        """last_activity is the maximum across all artifact type_last_activity values."""
        svc = RoadmapService(session_factory=MagicMock())
        rows = [
            make_row(
                feature_id=1,
                status_updated_at=EARLIER,
                artifact_type="learning",
                type_count=2,
                type_last_activity=NOW,
            ),
            make_row(
                feature_id=1,
                status_updated_at=EARLIER,
                artifact_type="decision",
                type_count=1,
                type_last_activity=LATER,
            ),
            make_row(
                feature_id=1,
                status_updated_at=EARLIER,
                artifact_type="snippet",
                type_count=1,
                type_last_activity=EARLIER,
            ),
        ]

        result = svc._pivot_rows(rows)

        assert result[0].features[0].last_activity == LATER


# ---------------------------------------------------------------------------
# TestPivotRowsPinnedAndOrdering
# ---------------------------------------------------------------------------


class TestPivotRowsPinnedAndOrdering:
    def test_pinned_true_preserved(self) -> None:
        """Feature with pinned=True has pinned=True in result."""
        svc = RoadmapService(session_factory=MagicMock())
        rows = [make_row(pinned=True)]

        result = svc._pivot_rows(rows)

        assert result[0].features[0].pinned is True

    def test_pinned_none_defaults_to_false(self) -> None:
        """Feature with pinned=None (from DB NULL) defaults to False."""
        svc = RoadmapService(session_factory=MagicMock())
        rows = [make_row(pinned=None)]

        result = svc._pivot_rows(rows)

        assert result[0].features[0].pinned is False

    def test_pinned_false_preserved(self) -> None:
        """Feature with pinned=False stays False."""
        svc = RoadmapService(session_factory=MagicMock())
        rows = [make_row(pinned=False)]

        result = svc._pivot_rows(rows)

        assert result[0].features[0].pinned is False

    def test_feature_order_preserved_from_rows(self) -> None:
        """Features appear in the order they are first seen in rows."""
        svc = RoadmapService(session_factory=MagicMock())
        rows = [
            make_row(feature_id=10, feature_name="Z Feature"),
            make_row(feature_id=5, feature_name="A Feature"),
            make_row(feature_id=8, feature_name="M Feature"),
        ]

        result = svc._pivot_rows(rows)

        names = [f.name for f in result[0].features]
        assert names == ["Z Feature", "A Feature", "M Feature"]


# ---------------------------------------------------------------------------
# TestPivotRowsMultipleProjects
# ---------------------------------------------------------------------------


class TestPivotRowsMultipleProjects:
    def test_multiple_projects_from_mixed_rows(self) -> None:
        """Rows from different project_keys produce separate RoadmapProject objects."""
        svc = RoadmapService(session_factory=MagicMock())
        rows = [
            make_row(
                project_key="proj-a",
                project_name="Project A",
                current_phase="alpha",
                feature_id=1,
                feature_name="Feature A1",
            ),
            make_row(
                project_key="proj-b",
                project_name="Project B",
                current_phase="beta",
                feature_id=2,
                feature_name="Feature B1",
            ),
        ]

        result = svc._pivot_rows(rows)

        assert len(result) == 2
        keys = [p.project_key for p in result]
        assert "proj-a" in keys
        assert "proj-b" in keys

        proj_a = next(p for p in result if p.project_key == "proj-a")
        assert proj_a.name == "Project A"
        assert proj_a.current_phase == "alpha"
        assert len(proj_a.features) == 1
        assert proj_a.features[0].name == "Feature A1"

        proj_b = next(p for p in result if p.project_key == "proj-b")
        assert proj_b.name == "Project B"
        assert proj_b.current_phase == "beta"
        assert len(proj_b.features) == 1
        assert proj_b.features[0].name == "Feature B1"

    def test_features_grouped_under_correct_project(self) -> None:
        """Multiple features in the same project are grouped together, not split."""
        svc = RoadmapService(session_factory=MagicMock())
        rows = [
            make_row(project_key="proj-a", feature_id=1, feature_name="F1"),
            make_row(project_key="proj-a", feature_id=2, feature_name="F2"),
            make_row(project_key="proj-b", feature_id=3, feature_name="F3"),
        ]

        result = svc._pivot_rows(rows)

        proj_a = next(p for p in result if p.project_key == "proj-a")
        assert len(proj_a.features) == 2

        proj_b = next(p for p in result if p.project_key == "proj-b")
        assert len(proj_b.features) == 1

    def test_current_phase_none(self) -> None:
        """Project with current_phase=None is handled correctly."""
        svc = RoadmapService(session_factory=MagicMock())
        rows = [make_row(current_phase=None)]

        result = svc._pivot_rows(rows)

        assert result[0].current_phase is None


# ---------------------------------------------------------------------------
# TestPivotRowsEdgeCases
# ---------------------------------------------------------------------------


class TestPivotRowsEdgeCases:
    def test_same_feature_id_across_artifact_types_deduped(self) -> None:
        """Same feature_id appearing in multiple rows (one per artifact_type) produces one feature."""
        svc = RoadmapService(session_factory=MagicMock())
        rows = [
            make_row(feature_id=1, artifact_type="learning", type_count=2, type_last_activity=NOW),
            make_row(feature_id=1, artifact_type="decision", type_count=1, type_last_activity=NOW),
        ]

        result = svc._pivot_rows(rows)

        assert len(result[0].features) == 1
        feature = result[0].features[0]
        assert feature.artifact_count["learning"] == 2
        assert feature.artifact_count["decision"] == 1

    def test_large_type_count(self) -> None:
        """Large type_count values are stored correctly."""
        svc = RoadmapService(session_factory=MagicMock())
        rows = [
            make_row(
                feature_id=1, artifact_type="learning", type_count=9999, type_last_activity=NOW
            ),
        ]

        result = svc._pivot_rows(rows)

        assert result[0].features[0].artifact_count["learning"] == 9999

    def test_feature_with_all_artifact_types(self) -> None:
        """Feature with rows for every artifact type has all counts populated."""
        svc = RoadmapService(session_factory=MagicMock())
        rows = [
            make_row(feature_id=1, artifact_type=at, type_count=i + 1, type_last_activity=NOW)
            for i, at in enumerate(_ARTIFACT_TYPES)
        ]

        result = svc._pivot_rows(rows)

        feature = result[0].features[0]
        for i, at in enumerate(_ARTIFACT_TYPES):
            assert feature.artifact_count[at] == i + 1


# ---------------------------------------------------------------------------
# TestUpdateFeatureStatuses
# ---------------------------------------------------------------------------


class TestUpdateFeatureStatuses:
    async def test_updates_single_feature(self) -> None:
        """update_feature_statuses with one feature returns 1 when found."""
        mock_sf, mock_session = make_session_factory()
        mock_session.execute = AsyncMock(return_value=make_update_result(1))
        svc = RoadmapService(session_factory=mock_sf)

        count = await svc.update_feature_statuses(
            "brain-v42",
            {"Auth System": "done"},
        )

        assert count == 1
        mock_session.commit.assert_awaited_once()

    async def test_updates_multiple_features(self) -> None:
        """update_feature_statuses with multiple features returns total updated count."""
        mock_sf, mock_session = make_session_factory()
        mock_session.execute = AsyncMock(
            side_effect=[
                make_update_result(1),
                make_update_result(1),
                make_update_result(1),
            ]
        )
        svc = RoadmapService(session_factory=mock_sf)

        count = await svc.update_feature_statuses(
            "brain-v42",
            {"F1": "done", "F2": "building", "F3": "planned"},
        )

        assert count == 3
        mock_session.commit.assert_awaited_once()

    async def test_partial_update_some_not_found(self) -> None:
        """update_feature_statuses returns count of only found features."""
        mock_sf, mock_session = make_session_factory()
        mock_session.execute = AsyncMock(
            side_effect=[
                make_update_result(1),  # F1 found
                make_update_result(0),  # F2 not found
            ]
        )
        svc = RoadmapService(session_factory=mock_sf)

        count = await svc.update_feature_statuses(
            "brain-v42",
            {"F1": "done", "F2": "done"},
        )

        assert count == 1

    async def test_no_features_returns_zero(self) -> None:
        """update_feature_statuses with empty dict returns 0."""
        mock_sf, mock_session = make_session_factory()
        svc = RoadmapService(session_factory=mock_sf)

        count = await svc.update_feature_statuses("brain-v42", {})

        assert count == 0
        mock_session.commit.assert_awaited_once()

    async def test_execute_called_with_correct_params(self) -> None:
        """update_feature_statuses passes correct SQL params for each feature."""
        mock_sf, mock_session = make_session_factory()
        mock_session.execute = AsyncMock(return_value=make_update_result(1))
        svc = RoadmapService(session_factory=mock_sf)

        await svc.update_feature_statuses("proj-x", {"My Feature": "done"})

        # Check the params passed to execute
        call_args = mock_session.execute.call_args
        params = call_args[0][1]
        assert params["status"] == "done"
        assert params["pk"] == "proj-x"
        assert params["name"] == "My Feature"


# ---------------------------------------------------------------------------
# TestUpdateFeatureStatusesNotFound — warning on missing features
# ---------------------------------------------------------------------------


class TestUpdateFeatureStatusesNotFound:
    async def test_logs_warning_when_feature_not_found(self) -> None:
        """update_feature_statuses logs a warning when rowcount is 0."""
        mock_sf, mock_session = make_session_factory()
        mock_session.execute = AsyncMock(return_value=make_update_result(0))
        svc = RoadmapService(session_factory=mock_sf)

        with patch("brain_v42.services.roadmap_service.logger") as mock_logger:
            await svc.update_feature_statuses("brain-v42", {"NonExistent": "done"})

            mock_logger.warning.assert_called_once_with(
                "roadmap.feature_not_found",
                project_key="brain-v42",
                feature_name="NonExistent",
            )

    async def test_no_warning_when_feature_found(self) -> None:
        """update_feature_statuses does NOT log a warning when feature is found."""
        mock_sf, mock_session = make_session_factory()
        mock_session.execute = AsyncMock(return_value=make_update_result(1))
        svc = RoadmapService(session_factory=mock_sf)

        with patch("brain_v42.services.roadmap_service.logger") as mock_logger:
            await svc.update_feature_statuses("brain-v42", {"Found Feature": "done"})

            mock_logger.warning.assert_not_called()


# ---------------------------------------------------------------------------
# TestUnpinFeatures
# ---------------------------------------------------------------------------


class TestUnpinFeatures:
    async def test_unpins_features_returns_rowcount(self) -> None:
        """unpin_features returns the number of rows updated."""
        mock_sf, mock_session = make_session_factory()
        mock_session.execute = AsyncMock(return_value=make_update_result(2))
        svc = RoadmapService(session_factory=mock_sf)

        count = await svc.unpin_features("brain-v42", ["F1", "F2"])

        assert count == 2
        mock_session.commit.assert_awaited_once()

    async def test_unpin_passes_correct_params(self) -> None:
        """unpin_features passes project_key and feature names to SQL."""
        mock_sf, mock_session = make_session_factory()
        mock_session.execute = AsyncMock(return_value=make_update_result(1))
        svc = RoadmapService(session_factory=mock_sf)

        await svc.unpin_features("proj-x", ["Alpha", "Beta"])

        call_args = mock_session.execute.call_args
        params = call_args[0][1]
        assert params["pk"] == "proj-x"
        assert params["names"] == ["Alpha", "Beta"]

    async def test_unpin_no_matching_features(self) -> None:
        """unpin_features returns 0 when no features match."""
        mock_sf, mock_session = make_session_factory()
        mock_session.execute = AsyncMock(return_value=make_update_result(0))
        svc = RoadmapService(session_factory=mock_sf)

        count = await svc.unpin_features("brain-v42", ["Nonexistent"])

        assert count == 0


# ---------------------------------------------------------------------------
# TestUnpinFeaturesEmptyList — short-circuit on empty list
# ---------------------------------------------------------------------------


class TestUnpinFeaturesEmptyList:
    async def test_empty_list_returns_zero_immediately(self) -> None:
        """unpin_features with empty list returns 0 without touching the DB."""
        mock_sf, mock_session = make_session_factory()
        svc = RoadmapService(session_factory=mock_sf)

        count = await svc.unpin_features("brain-v42", [])

        assert count == 0
        # Should NOT have opened a session at all
        mock_sf.assert_not_called()

    async def test_empty_list_does_not_commit(self) -> None:
        """unpin_features with empty list does not call session.commit()."""
        mock_sf, mock_session = make_session_factory()
        svc = RoadmapService(session_factory=mock_sf)

        await svc.unpin_features("brain-v42", [])

        mock_session.commit.assert_not_awaited()


# ---------------------------------------------------------------------------
# TestRoadmapSQLFiltersArchived — archived / merged_into exclusion
# ---------------------------------------------------------------------------


class TestRoadmapSQLFiltersArchived:
    def test_roadmap_sql_excludes_archived_status(self) -> None:
        """_ROADMAP_SQL must filter out features with status = 'archived'.

        Without this, archived clusters surfaced first (recent status_updated_at
        from the purge run) and drowned the live roadmap in noise.
        """
        from brain_v42.services.roadmap_service import _ROADMAP_SQL

        assert "archived" in _ROADMAP_SQL

    def test_roadmap_sql_excludes_merged_into(self) -> None:
        """_ROADMAP_SQL must filter out features that have been merged into another.

        merged_into IS NULL ensures merged duplicates are hidden from the roadmap.
        """
        from brain_v42.services.roadmap_service import _ROADMAP_SQL

        assert "merged_into" in _ROADMAP_SQL
