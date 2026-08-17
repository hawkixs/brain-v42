"""Unit tests for RoadmapService."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock

import pytest

from brain_v42.services.roadmap_service import RoadmapService


@pytest.fixture
def mock_session_factory():
    session = AsyncMock()
    factory = MagicMock()
    factory.return_value.__aenter__ = AsyncMock(return_value=session)
    factory.return_value.__aexit__ = AsyncMock(return_value=False)
    return factory, session


@pytest.mark.asyncio
async def test_get_roadmap_empty(mock_session_factory):
    factory, session = mock_session_factory
    session.execute = AsyncMock(return_value=MagicMock(fetchall=MagicMock(return_value=[])))
    svc = RoadmapService(session_factory=factory)
    result = await svc.get_roadmap()
    assert result == []


@pytest.mark.asyncio
async def test_get_roadmap_groups_by_project(mock_session_factory):
    factory, session = mock_session_factory
    fid = uuid.uuid4()
    row = MagicMock()
    row.project_key = "red"
    row.project_name = "red-monitor"
    row.current_phase = "production"
    row.feature_id = fid
    row.feature_name = "Core Monitoring"
    row.status = "deployed"
    row.status_updated_at = datetime(2026, 3, 12, tzinfo=UTC)
    row.artifact_type = "learning"
    row.type_count = 5
    row.type_last_activity = datetime(2026, 3, 12, tzinfo=UTC)

    session.execute = AsyncMock(return_value=MagicMock(fetchall=MagicMock(return_value=[row])))
    svc = RoadmapService(session_factory=factory)
    result = await svc.get_roadmap()

    assert len(result) == 1
    assert result[0].project_key == "red"
    assert result[0].name == "red-monitor"
    assert len(result[0].features) == 1
    assert result[0].features[0].artifact_count["learning"] == 5
    assert result[0].features[0].status == "deployed"


@pytest.mark.asyncio
async def test_update_feature_statuses_sets_pinned_true(mock_session_factory):
    """update_feature_statuses SQL should set pinned=true alongside status."""
    factory, session = mock_session_factory
    mock_result = MagicMock()
    mock_result.rowcount = 1
    session.execute = AsyncMock(return_value=mock_result)
    session.commit = AsyncMock()

    svc = RoadmapService(session_factory=factory)
    count = await svc.update_feature_statuses("brain-v42", {"Core Monitoring": "deployed"})

    assert count == 1
    # Verify the SQL text includes pinned = true
    call_args = session.execute.call_args
    sql_text = str(call_args[0][0].text)
    assert "pinned" in sql_text


@pytest.mark.asyncio
async def test_unpin_features(mock_session_factory):
    """unpin_features sets pinned=false for named features."""
    factory, session = mock_session_factory
    mock_result = MagicMock()
    mock_result.rowcount = 2
    session.execute = AsyncMock(return_value=mock_result)
    session.commit = AsyncMock()

    svc = RoadmapService(session_factory=factory)
    count = await svc.unpin_features("brain-v42", ["Old Feature", "Stale Feature"])

    assert count == 2
    session.execute.assert_called_once()
    session.commit.assert_called_once()


@pytest.mark.asyncio
async def test_unpin_features_empty_list(mock_session_factory):
    """unpin_features returns 0 for empty list without hitting DB."""
    factory, session = mock_session_factory
    svc = RoadmapService(session_factory=factory)
    count = await svc.unpin_features("brain-v42", [])
    assert count == 0
    session.execute.assert_not_called()


# ---------------------------------------------------------------------------
# brain_get_roadmap tool handler (register_roadmap_tools)
# ---------------------------------------------------------------------------


class _MockMCP:
    """Collecting mock for FastMCP — stores registered tools by function name."""

    def __init__(self) -> None:
        self.registered: dict = {}

    def tool(self, **kwargs):
        def decorator(fn):
            self.registered[fn.__name__] = fn
            return fn

        return decorator


def _register_roadmap_handler():
    from brain_v42.mcp.tools.roadmap_tools import register_roadmap_tools

    mcp = _MockMCP()
    svc = AsyncMock()
    register_roadmap_tools(
        mcp,
        svc,
        feature_svc=MagicMock(),
        feature_creation_svc=None,
    )
    return mcp.registered["brain_get_roadmap"], svc


@pytest.mark.asyncio
async def test_brain_get_roadmap_handler_empty_returns_string():
    """Handler returns a formatted string and forwards project_key=None."""
    handler, svc = _register_roadmap_handler()
    svc.get_roadmap.return_value = []

    result = await handler()

    assert isinstance(result, str)
    svc.get_roadmap.assert_awaited_once_with(project_key=None)


@pytest.mark.asyncio
async def test_brain_get_roadmap_handler_forwards_project_key_and_formats():
    """Handler passes project_key through and serializes RoadmapProject models."""
    from brain_v42.models.feature import RoadmapFeature, RoadmapProject

    handler, svc = _register_roadmap_handler()
    project = RoadmapProject(
        project_key="brain-v42",
        name="brain-v42",
        current_phase="M5",
        features=[
            RoadmapFeature(
                name="session start",
                status="done",
                status_updated_at=datetime(2026, 5, 1, tzinfo=UTC),
                pinned=False,
                artifact_count={"decision": 2},
                last_activity=datetime(2026, 5, 1, tzinfo=UTC),
            )
        ],
    )
    svc.get_roadmap.return_value = [project]

    result = await handler(project_key="brain-v42")

    assert isinstance(result, str)
    assert "brain-v42" in result
    svc.get_roadmap.assert_awaited_once_with(project_key="brain-v42")
