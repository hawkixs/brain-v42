"""Focused validation and error coverage for :mod:`roadmap_service`."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from brain_v42.services.roadmap_service import (
    ProjectFocusConflictError,
    ProjectFocusValidationError,
    RoadmapService,
)


def _update_factory(*rowcounts: int) -> tuple[MagicMock, AsyncMock]:
    session = AsyncMock()
    session.execute = AsyncMock(
        side_effect=[SimpleNamespace(rowcount=value) for value in rowcounts]
    )
    session.commit = AsyncMock()
    context = AsyncMock()
    context.__aenter__ = AsyncMock(return_value=session)
    context.__aexit__ = AsyncMock(return_value=False)
    factory = MagicMock(return_value=context)
    return factory, session


async def test_update_feature_statuses_rejects_mixed_invalid_batch_before_sql() -> None:
    factory, session = _update_factory(1, 1)
    service = RoadmapService(factory)

    with pytest.raises(
        ProjectFocusValidationError,
        match="invalid feature status: Invalid=in_progress",
    ):
        await service.update_feature_statuses(
            "brain-v42",
            {"Valid": "done", "Invalid": "in_progress"},
        )

    factory.assert_not_called()
    session.execute.assert_not_awaited()


@pytest.mark.parametrize("current_focus", ["", "   "])
async def test_update_project_focus_rejects_blank_focus_before_db(current_focus: str) -> None:
    factory = MagicMock()
    with pytest.raises(ProjectFocusValidationError, match="must not be blank"):
        await RoadmapService(factory).update_project_focus(
            "brain-v42", current_focus, expected_focus_revision=0
        )
    factory.assert_not_called()


@pytest.mark.parametrize("revision", [True, -1, "0"])
async def test_update_project_focus_rejects_invalid_revision_before_db(
    revision: object,
) -> None:
    factory = MagicMock()
    with pytest.raises(ProjectFocusValidationError, match="non-negative integer"):
        await RoadmapService(factory).update_project_focus(
            "brain-v42",
            "focus",
            expected_focus_revision=revision,  # type: ignore[arg-type]
        )
    factory.assert_not_called()


async def test_update_project_focus_rejects_invalid_status_before_db() -> None:
    factory = MagicMock()
    with pytest.raises(ProjectFocusValidationError, match="Feature=invalid"):
        await RoadmapService(factory).update_project_focus(
            "brain-v42",
            "focus",
            expected_focus_revision=0,
            feature_status={"Feature": "invalid"},
        )
    factory.assert_not_called()


async def test_update_project_focus_rejects_status_unpin_overlap_before_db() -> None:
    factory = MagicMock()
    with pytest.raises(ProjectFocusValidationError, match="status-updated and unpinned"):
        await RoadmapService(factory).update_project_focus(
            "brain-v42",
            "focus",
            expected_focus_revision=0,
            feature_status={"Feature": "done"},
            unpin=["Feature", "Feature"],
        )
    factory.assert_not_called()


def test_project_focus_conflict_exposes_current_state() -> None:
    error = ProjectFocusConflictError(current_focus="current", current_revision=4)
    assert error.current_focus == "current"
    assert error.current_revision == 4
    assert "current revision is 4" in str(error)


async def test_lock_requested_features_skips_sql_for_empty_names() -> None:
    session = AsyncMock()
    result = await RoadmapService(MagicMock())._lock_requested_features(
        session,
        project_key="brain-v42",
        requested_names=(),
    )
    assert result == []
    session.execute.assert_not_awaited()


def test_validate_requested_features_reports_missing_name() -> None:
    with pytest.raises(ProjectFocusValidationError, match="missing: Missing"):
        RoadmapService._validate_requested_features(
            requested_names=("Missing",),
            feature_rows=[],
            status_updates={"Missing": "done"},
        )


def test_validate_requested_features_reports_ambiguous_name() -> None:
    rows = [
        {"id": 1, "name": "Duplicate", "merged_into": None},
        {"id": 2, "name": "Duplicate", "merged_into": None},
    ]
    with pytest.raises(ProjectFocusValidationError, match="ambiguous: Duplicate"):
        RoadmapService._validate_requested_features(
            requested_names=("Duplicate",),
            feature_rows=rows,
            status_updates={"Duplicate": "done"},
        )


def test_validate_requested_features_rejects_merged_reactivation() -> None:
    rows = [{"id": 1, "name": "Merged", "merged_into": "canonical"}]
    with pytest.raises(ProjectFocusValidationError, match="cannot be reactivated: Merged"):
        RoadmapService._validate_requested_features(
            requested_names=("Merged",),
            feature_rows=rows,
            status_updates={"Merged": "building"},
        )


def test_validate_requested_features_allows_archiving_merged_feature() -> None:
    row = {"id": 1, "name": "Merged", "merged_into": "canonical"}
    result = RoadmapService._validate_requested_features(
        requested_names=("Merged",),
        feature_rows=[row],
        status_updates={"Merged": "archived"},
    )
    assert result == {"Merged": row}
