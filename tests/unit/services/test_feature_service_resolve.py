"""Tests for FeatureService.resolve_feature / update_status (mocked sessions)."""

from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from brain_v42.models.feature import VALID_FEATURE_STATUSES as MODEL_FEATURE_STATUSES
from brain_v42.services.feature_service import (
    VALID_FEATURE_STATUSES,
    FeatureService,
)

_NOW = datetime(2026, 7, 4, tzinfo=UTC)


def _row(**kw) -> dict:
    defaults: dict = {
        "id": uuid4(),
        "project_key": "brain-v42",
        "name": "Recherche hybride",
        "description": "d",
        "status": "building",
        "status_updated_at": _NOW,
        "pinned": False,
        "created_at": _NOW,
        "updated_at": _NOW,
    }
    defaults.update(kw)
    return defaults


def _factory(side_effects: list) -> tuple:
    mock_session = MagicMock(spec=AsyncSession)
    mock_session.execute = AsyncMock(side_effect=side_effects)
    begin_cm = MagicMock()
    begin_cm.__aenter__ = AsyncMock(return_value=None)
    begin_cm.__aexit__ = AsyncMock(return_value=False)
    mock_session.begin = MagicMock(return_value=begin_cm)

    @asynccontextmanager
    async def factory():
        yield mock_session

    return factory, mock_session


def _mappings_all(rows: list[dict]) -> MagicMock:
    r = MagicMock()
    r.mappings.return_value.all.return_value = rows
    return r


def _mappings_one_or_none(row: dict | None) -> MagicMock:
    r = MagicMock()
    r.mappings.return_value.one_or_none.return_value = row
    return r


def _scalars_all(values: list) -> MagicMock:
    r = MagicMock()
    r.scalars.return_value.all.return_value = values
    return r


class TestStatuses:
    def test_valid_statuses_include_archived(self):
        assert VALID_FEATURE_STATUSES is MODEL_FEATURE_STATUSES
        assert VALID_FEATURE_STATUSES == (
            "planned",
            "research",
            "design",
            "building",
            "deployed",
            "done",
            "archived",
        )


class TestResolveFeature:
    @pytest.mark.asyncio
    async def test_exact_name_hit(self):
        row = _row()
        factory, _ = _factory([_mappings_all([row])])
        svc = FeatureService(factory)
        resolved = await svc.resolve_feature("brain-v42", "Recherche hybride")
        assert not isinstance(resolved, str)
        assert resolved.id == row["id"]

    @pytest.mark.asyncio
    async def test_exact_name_ambiguous_lists_candidates(self):
        r1, r2 = _row(), _row(name="Recherche hybride")
        factory, _ = _factory([_mappings_all([r1, r2])])
        svc = FeatureService(factory)
        resolved = await svc.resolve_feature("brain-v42", "Recherche hybride")
        assert isinstance(resolved, str)
        assert str(r1["id"])[:8] in resolved and str(r2["id"])[:8] in resolved

    @pytest.mark.asyncio
    async def test_id_prefix_unique_hit(self):
        row = _row()
        prefix = str(row["id"]).replace("-", "")[:12]
        factory, _ = _factory(
            [
                _mappings_all([]),  # exact name: miss
                _scalars_all([row["id"]]),  # resolve_id_prefix: 1 match
                _mappings_one_or_none(row),  # SELECT by id
            ]
        )
        svc = FeatureService(factory)
        resolved = await svc.resolve_feature("brain-v42", prefix)
        assert not isinstance(resolved, str)
        assert resolved.id == row["id"]

    @pytest.mark.asyncio
    async def test_id_prefix_wrong_project_rejected(self):
        row = _row(project_key="red-monitor")
        prefix = str(row["id"]).replace("-", "")[:12]
        factory, _ = _factory(
            [
                _mappings_all([]),
                _scalars_all([row["id"]]),
                _mappings_one_or_none(row),
            ]
        )
        svc = FeatureService(factory)
        resolved = await svc.resolve_feature("brain-v42", prefix)
        assert isinstance(resolved, str)
        assert "red-monitor" in resolved

    @pytest.mark.asyncio
    async def test_ilike_unique_hit(self):
        row = _row()
        factory, _ = _factory(
            [
                _mappings_all([]),  # exact: miss (not hex → no id branch)
                _mappings_all([row]),  # ILIKE: 1 match
            ]
        )
        svc = FeatureService(factory)
        resolved = await svc.resolve_feature("brain-v42", "hybride")
        assert not isinstance(resolved, str)
        assert resolved.id == row["id"]

    @pytest.mark.asyncio
    async def test_ilike_ambiguous_lists_id_and_name(self):
        r1, r2 = _row(name="Recherche hybride"), _row(name="Recherche hybride v2")
        factory, _ = _factory([_mappings_all([]), _mappings_all([r1, r2])])
        svc = FeatureService(factory)
        resolved = await svc.resolve_feature("brain-v42", "hybride")
        assert isinstance(resolved, str)
        assert "Recherche hybride v2" in resolved
        assert str(r1["id"])[:8] in resolved

    @pytest.mark.asyncio
    async def test_no_match_explicit_error(self):
        factory, _ = _factory([_mappings_all([]), _mappings_all([])])
        svc = FeatureService(factory)
        resolved = await svc.resolve_feature("brain-v42", "inexistante")
        assert isinstance(resolved, str)
        assert "inexistante" in resolved and "brain-v42" in resolved


class TestUpdateStatus:
    @pytest.mark.asyncio
    async def test_update_pins_and_returns_feature(self):
        row = _row(status="deployed", pinned=True)
        result = MagicMock()
        result.mappings.return_value.one_or_none.return_value = row
        factory, session = _factory([result])
        svc = FeatureService(factory)
        updated = await svc.update_status(row["id"], "deployed")
        assert updated is not None
        assert updated.status == "deployed"
        assert updated.pinned is True
        # The UPDATE statement does carry status + pinned + status_updated_at.
        stmt = session.execute.call_args[0][0]
        compiled = str(stmt)
        assert "status" in compiled and "pinned" in compiled

    @pytest.mark.asyncio
    async def test_update_unknown_id_returns_none(self):
        result = MagicMock()
        result.mappings.return_value.one_or_none.return_value = None
        factory, _ = _factory([result])
        svc = FeatureService(factory)
        assert await svc.update_status(uuid4(), "done") is None

    @pytest.mark.asyncio
    async def test_update_archived_does_not_set_pinned(self):
        """update_status('archived') must NOT include pinned=True in the statement.

        Archiving a feature should not pin it — pinning would keep it visible
        in stale_pinned briefings forever, defeating the archive.
        """
        row = _row(status="archived", pinned=False)
        result = MagicMock()
        result.mappings.return_value.one_or_none.return_value = row
        factory, session = _factory([result])
        svc = FeatureService(factory)
        await svc.update_status(row["id"], "archived")
        stmt = session.execute.call_args[0][0]
        # Inspect the statement params dict: 'pinned' must not be a bound key
        # in the UPDATE's SET clause.  We compile and inspect _update_values
        # (the dict passed to .values()) via the statement's clause element.
        compiled = stmt.compile(compile_kwargs={"literal_binds": True})
        # The SET portion looks like "SET status=..., status_updated_at=...".
        # Extract everything between SET and WHERE to isolate the VALUES clause.
        sql_str = str(compiled)
        set_start = sql_str.find("SET ")
        where_start = sql_str.find(" WHERE ")
        set_clause = sql_str[set_start:where_start] if where_start != -1 else sql_str[set_start:]
        # 'pinned' must not appear in the SET clause (it may appear in RETURNING).
        assert "pinned" not in set_clause
