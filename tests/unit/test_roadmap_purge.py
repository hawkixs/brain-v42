"""Unit tests for scripts.roadmap_purge — pure rules + mocked apply."""

from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest
from scripts.roadmap_purge import (
    TERMINAL_STATUSES,
    apply_archive,
    build_report,
    classify_feature,
)
from sqlalchemy.ext.asyncio import AsyncSession

_NOW = datetime(2026, 7, 4, tzinfo=UTC)
_KNOWN = {"brain-v42", "red-monitor", "red"}


def _feature(**kw) -> dict:
    defaults: dict = {
        "id": uuid4(),
        "project_key": "brain-v42",
        "name": "une feature",
        "status": "research",
        "pinned": False,
        "artifact_count": 3,
        "last_artifact_at": _NOW - timedelta(days=2),
    }
    defaults.update(kw)
    return defaults


class TestClassifyFeature:
    def test_pinned_never_touched(self):
        f = _feature(pinned=True, project_key="fantome", artifact_count=0)
        assert classify_feature(f, _KNOWN, _NOW) is None

    def test_r1_phantom_project_key(self):
        f = _feature(project_key="refondrre")
        assert classify_feature(f, _KNOWN, _NOW) == "R1"

    def test_r1_spares_key_in_red_group(self):
        # 'red' is in known_keys (via get_keys_by_group) → spared.
        f = _feature(project_key="red", artifact_count=5)
        assert classify_feature(f, _KNOWN, _NOW) is None

    def test_r2_zero_artifacts(self):
        f = _feature(artifact_count=0, last_artifact_at=None)
        assert classify_feature(f, _KNOWN, _NOW) == "R2"

    def test_r3_single_stale_artifact_non_terminal(self):
        f = _feature(artifact_count=1, last_artifact_at=_NOW - timedelta(days=61))
        assert classify_feature(f, _KNOWN, _NOW) == "R3"

    def test_r3_spares_terminal_status(self):
        for status in TERMINAL_STATUSES:
            f = _feature(
                status=status, artifact_count=1, last_artifact_at=_NOW - timedelta(days=61)
            )
            assert classify_feature(f, _KNOWN, _NOW) is None, status

    def test_r3_spares_recent_artifact(self):
        f = _feature(artifact_count=1, last_artifact_at=_NOW - timedelta(days=10))
        assert classify_feature(f, _KNOWN, _NOW) is None

    def test_r3_spares_multi_artifact(self):
        f = _feature(artifact_count=2, last_artifact_at=_NOW - timedelta(days=100))
        assert classify_feature(f, _KNOWN, _NOW) is None

    def test_alive_feature_untouched(self):
        assert classify_feature(_feature(), _KNOWN, _NOW) is None


class TestBuildReport:
    def test_report_groups_by_project_and_rule(self):
        rows = [
            (_feature(project_key="refondrre"), "R1"),
            (_feature(project_key="refondrre"), "R1"),
            (_feature(project_key="brain-v42", artifact_count=0), "R2"),
        ]
        report = build_report(rows)
        assert "refondrre" in report
        assert "R1: 2" in report
        assert "brain-v42" in report
        assert "R2: 1" in report
        assert "total à archiver: 3" in report


class TestApplyArchive:
    @pytest.mark.asyncio
    async def test_archives_ids_and_checks_postcondition(self):
        ids = [uuid4(), uuid4()]
        mock_session = MagicMock(spec=AsyncSession)
        update_result = MagicMock()
        count_result = MagicMock()
        count_result.scalar_one.return_value = len(ids)
        mock_session.execute = AsyncMock(side_effect=[update_result, count_result])
        begin_cm = MagicMock()
        begin_cm.__aenter__ = AsyncMock(return_value=None)
        begin_cm.__aexit__ = AsyncMock(return_value=False)
        mock_session.begin = MagicMock(return_value=begin_cm)

        @asynccontextmanager
        async def factory():
            yield mock_session

        archived = await apply_archive(factory, ids)
        assert archived == len(ids)

    @pytest.mark.asyncio
    async def test_postcondition_mismatch_raises(self):
        ids = [uuid4()]
        mock_session = MagicMock(spec=AsyncSession)
        update_result = MagicMock()
        count_result = MagicMock()
        count_result.scalar_one.return_value = 0  # nothing archived → post-cond KO
        mock_session.execute = AsyncMock(side_effect=[update_result, count_result])
        begin_cm = MagicMock()
        begin_cm.__aenter__ = AsyncMock(return_value=None)
        begin_cm.__aexit__ = AsyncMock(return_value=False)
        mock_session.begin = MagicMock(return_value=begin_cm)

        @asynccontextmanager
        async def factory():
            yield mock_session

        with pytest.raises(RuntimeError, match="post-condition"):
            await apply_archive(factory, ids)

    @pytest.mark.asyncio
    async def test_empty_ids_noop(self):
        factory = MagicMock()
        assert await apply_archive(factory, []) == 0
        factory.assert_not_called()
