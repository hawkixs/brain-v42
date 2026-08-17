"""Tests for brain_feature_update MCP tool (surface 42)."""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest
from fastmcp import FastMCP

from brain_v42.mcp.tools.roadmap_tools import register_roadmap_tools
from brain_v42.models.feature import Feature
from tests.unit.mcp._tool_error_adapter import capture_tool_errors

_NOW = datetime(2026, 7, 4, tzinfo=UTC)


def _feature(**kw) -> Feature:
    defaults: dict = {
        "id": uuid4(),
        "project_key": "brain-v42",
        "name": "Recherche hybride",
        "description": "d",
        "status": "deployed",
        "status_updated_at": _NOW,
        "pinned": True,
        "created_at": _NOW,
        "updated_at": _NOW,
    }
    defaults.update(kw)
    return Feature(**defaults)


def _mcp_with_mocks(feature_svc=None):
    mcp = FastMCP("test")
    register_roadmap_tools(
        mcp,
        roadmap_svc=MagicMock(),
        feature_svc=feature_svc or MagicMock(),
        feature_creation_svc=None,
    )
    return mcp


class TestBrainFeatureUpdate:
    @pytest.mark.asyncio
    async def test_tool_registered(self):
        mcp = _mcp_with_mocks()
        tool = await mcp.get_tool("brain_feature_update")
        assert tool is not None

    @pytest.mark.asyncio
    async def test_invalid_status_rejected_without_service_call(self):
        svc = MagicMock()
        svc.resolve_feature = AsyncMock()
        mcp = _mcp_with_mocks(svc)
        tool = await mcp.get_tool("brain_feature_update")
        result = await capture_tool_errors(tool.fn)(
            feature="x", status="shipped", project_key="brain-v42"
        )
        assert "Invalid status" in result
        svc.resolve_feature.assert_not_called()

    @pytest.mark.asyncio
    async def test_resolution_error_passthrough(self):
        svc = MagicMock()
        svc.resolve_feature = AsyncMock(
            return_value="No feature matching 'x' in project 'brain-v42'"
        )
        mcp = _mcp_with_mocks(svc)
        tool = await mcp.get_tool("brain_feature_update")
        result = await capture_tool_errors(tool.fn)(
            feature="x", status="done", project_key="brain-v42"
        )
        assert "No feature matching" in result

    @pytest.mark.asyncio
    async def test_happy_path_updates_and_reports(self):
        feat = _feature()
        svc = MagicMock()
        svc.resolve_feature = AsyncMock(return_value=feat)
        svc.update_status = AsyncMock(return_value=feat)
        mcp = _mcp_with_mocks(svc)
        tool = await mcp.get_tool("brain_feature_update")
        result = await tool.fn(
            feature="Recherche hybride", status="deployed", project_key="brain-v42"
        )
        svc.update_status.assert_called_once_with(feat.id, "deployed")
        assert "Recherche hybride" in result
        assert "deployed" in result
        assert "pinned" in result

    @pytest.mark.asyncio
    async def test_archived_is_a_valid_status(self):
        feat = _feature(status="archived")
        svc = MagicMock()
        svc.resolve_feature = AsyncMock(return_value=feat)
        svc.update_status = AsyncMock(return_value=feat)
        mcp = _mcp_with_mocks(svc)
        tool = await mcp.get_tool("brain_feature_update")
        result = await tool.fn(feature="x", status="archived", project_key="brain-v42")
        assert "archived" in result
