"""Tests for SchemaStateService — the measured schema revision (ticket 87ac8b7a).

The briefing must never restate a revision copied forward from prose. This
service reads the one authority: alembic_version in the brain's own database.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from brain_v42.services.schema_state_service import SchemaStateService


def _session_factory(scalar_value: object) -> MagicMock:
    """Build an async-context-manager session factory returning scalar_value."""
    session = MagicMock()
    session.execute = AsyncMock(return_value=MagicMock(scalar=MagicMock(return_value=scalar_value)))
    ctx = MagicMock()
    ctx.__aenter__ = AsyncMock(return_value=session)
    ctx.__aexit__ = AsyncMock(return_value=False)
    return MagicMock(return_value=ctx)


class TestCurrentRevision:
    @pytest.mark.asyncio
    async def test_returns_the_revision_in_force(self) -> None:
        svc = SchemaStateService(_session_factory("039"))
        assert await svc.current_revision() == "039"

    @pytest.mark.asyncio
    async def test_returns_none_when_the_table_is_empty(self) -> None:
        """An unstamped database has no revision — that is not "039"."""
        svc = SchemaStateService(_session_factory(None))
        assert await svc.current_revision() is None

    @pytest.mark.asyncio
    async def test_coerces_to_str(self) -> None:
        """The caller renders the value; it must never leak a non-str type."""
        svc = SchemaStateService(_session_factory(39))
        assert await svc.current_revision() == "39"

    @pytest.mark.asyncio
    async def test_does_not_swallow_a_failing_read(self) -> None:
        """Failures propagate: the briefing decides to render "indisponible".

        Returning None here would make an unreachable database indistinguishable
        from an unstamped one, and the briefing would silently show nothing.
        """
        session = MagicMock()
        session.execute = AsyncMock(side_effect=RuntimeError("connection refused"))
        ctx = MagicMock()
        ctx.__aenter__ = AsyncMock(return_value=session)
        ctx.__aexit__ = AsyncMock(return_value=False)
        svc = SchemaStateService(MagicMock(return_value=ctx))
        with pytest.raises(RuntimeError):
            await svc.current_revision()
