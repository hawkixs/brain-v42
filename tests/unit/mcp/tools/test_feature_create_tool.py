"""Behavior tests for explicit roadmap feature creation."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest

from brain_v42.mcp.tools.roadmap_tools import register_roadmap_tools
from brain_v42.models.feature import Feature
from brain_v42.services.feature_creation_service import (
    FeatureAlreadyExistsError,
    FeatureEmbeddingError,
    FeatureProjectNotFoundError,
)
from tests.unit.mcp._tool_error_adapter import capture_tool_errors


class _MockMCP:
    def __init__(self) -> None:
        self.registered: dict[str, Any] = {}

    def tool(self, **_kwargs: Any) -> Any:
        def decorator(function: Any) -> Any:
            self.registered[function.__name__] = capture_tool_errors(function)
            return function

        return decorator


def test_brain_feature_create_is_registered() -> None:
    mcp = _MockMCP()

    register_roadmap_tools(
        mcp,
        roadmap_svc=MagicMock(),
        feature_svc=MagicMock(),
        feature_creation_svc=None,
    )

    assert "brain_feature_create" in mcp.registered


def _create_handler(feature_creation_svc: Any | None = None) -> Any:
    mcp = _MockMCP()
    register_roadmap_tools(
        mcp,
        roadmap_svc=MagicMock(),
        feature_svc=MagicMock(),
        feature_creation_svc=feature_creation_svc,
    )
    return mcp.registered["brain_feature_create"]


@pytest.mark.asyncio
async def test_create_fails_closed_when_service_is_not_injected() -> None:
    result = await _create_handler()(
        name="Roadmap feature",
        description="Must not be written.",
        project_key="brain-v42",
    )

    assert "service unavailable" in result
    assert "no feature created" in result


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("overrides", "field"),
    [
        ({"name": "   "}, "name"),
        ({"status": "archived"}, "status"),
        ({"project_key": "Bad Project"}, "project_key"),
    ],
)
async def test_create_rejects_invalid_payload_without_calling_service(
    overrides: dict[str, Any],
    field: str,
) -> None:
    creator = MagicMock()
    creator.create = AsyncMock()
    arguments: dict[str, Any] = {
        "name": "Roadmap feature",
        "description": "Useful description",
        "project_key": "brain-v42",
        "status": "planned",
        "pinned": True,
        **overrides,
    }

    result = await _create_handler(creator)(**arguments)

    assert "Invalid feature" in result
    assert field in result
    creator.create.assert_not_awaited()


@pytest.mark.asyncio
async def test_create_forwards_validated_payload_and_reports_created_feature() -> None:
    created = Feature(
        id=uuid4(),
        project_key="brain-v42",
        name="Roadmap feature",
        description="Useful description",
        status="design",
        status_updated_at=datetime.now(UTC),
        pinned=False,
        created_at=datetime.now(UTC),
        updated_at=datetime.now(UTC),
    )
    creator = MagicMock()
    creator.create = AsyncMock(return_value=created)

    result = await _create_handler(creator)(
        name="  Roadmap feature  ",
        description="  Useful description  ",
        project_key="brain_v42",
        status="design",
        pinned=False,
    )

    payload = creator.create.await_args.args[0]
    assert payload.project_key == "brain-v42"
    assert payload.name == "Roadmap feature"
    assert payload.description == "Useful description"
    assert payload.status == "design"
    assert payload.pinned is False
    assert "Roadmap feature" in result
    assert "created" in result
    assert "design" in result
    assert "unpinned" in result


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "error",
    [
        FeatureProjectNotFoundError("project 'missing' does not exist; no feature created"),
        FeatureAlreadyExistsError("feature 'Duplicate' already exists; no feature created"),
        FeatureEmbeddingError("feature embedding unavailable; no feature created"),
    ],
)
async def test_create_formats_domain_failures_without_claiming_success(error: Exception) -> None:
    creator = MagicMock()
    creator.create = AsyncMock(side_effect=error)

    result = await _create_handler(creator)(
        name="Roadmap feature",
        description="Useful description",
        project_key="brain-v42",
    )

    assert str(error) in result
    assert "created →" not in result
