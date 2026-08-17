"""Tests for Neo4j async driver factory (TDD Red phase).

Tests for:
- create_neo4j_driver: factory returning None or AsyncDriver
- close_neo4j_driver: safe close (no-op if None)
- neo4j_healthcheck: True/False/None-driver cases
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest


class TestCreateNeo4jDriver:
    """Tests for create_neo4j_driver factory function."""

    def test_returns_none_when_url_is_none(self) -> None:
        """create_neo4j_driver returns None when url is None."""
        from brain_v42.db.neo4j import create_neo4j_driver

        result = create_neo4j_driver(url=None)
        assert result is None

    def test_returns_none_when_disabled(self) -> None:
        """create_neo4j_driver returns None when enabled=False."""
        from brain_v42.db.neo4j import create_neo4j_driver

        result = create_neo4j_driver(url="bolt://localhost:7687", enabled=False)
        assert result is None

    def test_returns_none_when_url_empty_string(self) -> None:
        """create_neo4j_driver returns None when url is empty string."""
        from brain_v42.db.neo4j import create_neo4j_driver

        result = create_neo4j_driver(url="")
        assert result is None

    def test_creates_driver_when_enabled(self) -> None:
        """create_neo4j_driver returns AsyncDriver when url provided and enabled."""
        from brain_v42.db.neo4j import create_neo4j_driver

        mock_driver = MagicMock()
        with patch(
            "brain_v42.db.neo4j.AsyncGraphDatabase.driver",
            return_value=mock_driver,
        ) as mock_factory:
            result = create_neo4j_driver(
                url="bolt://localhost:7687",
                user="neo4j",
                password="test",
                enabled=True,
            )
            mock_factory.assert_called_once_with(
                "bolt://localhost:7687", auth=("neo4j", "test"), max_connection_pool_size=20
            )
            assert result is mock_driver

    def test_creates_driver_with_default_credentials(self) -> None:
        """create_neo4j_driver uses default user='neo4j' and password='' when not given."""
        from brain_v42.db.neo4j import create_neo4j_driver

        mock_driver = MagicMock()
        with patch(
            "brain_v42.db.neo4j.AsyncGraphDatabase.driver",
            return_value=mock_driver,
        ) as mock_factory:
            create_neo4j_driver(url="bolt://localhost:7687")
            mock_factory.assert_called_once_with(
                "bolt://localhost:7687",
                auth=("neo4j", ""),
                max_connection_pool_size=20,
            )

    def test_creates_driver_passes_max_connection_pool_size(self) -> None:
        """create_neo4j_driver passes max_connection_pool_size=20 to the driver factory."""
        from brain_v42.db.neo4j import create_neo4j_driver

        captured: dict = {}

        def capturing_stub(url, **kwargs):
            captured["url"] = url
            captured["kwargs"] = kwargs
            return MagicMock()

        with patch(
            "brain_v42.db.neo4j.AsyncGraphDatabase.driver",
            side_effect=capturing_stub,
        ):
            create_neo4j_driver(url="bolt://localhost:7687", user="neo4j", password="pw")

        assert captured["kwargs"].get("max_connection_pool_size") == 20


class TestCloseNeo4jDriver:
    """Tests for close_neo4j_driver."""

    @pytest.mark.asyncio
    async def test_close_none_driver_is_noop(self) -> None:
        """close_neo4j_driver does nothing when driver is None."""
        from brain_v42.db.neo4j import close_neo4j_driver

        # Should not raise
        await close_neo4j_driver(None)

    @pytest.mark.asyncio
    async def test_close_calls_driver_close(self) -> None:
        """close_neo4j_driver calls driver.close() when driver is provided."""
        from brain_v42.db.neo4j import close_neo4j_driver

        mock_driver = AsyncMock()
        await close_neo4j_driver(mock_driver)
        mock_driver.close.assert_called_once()


class TestNeo4jHealthcheck:
    """Tests for neo4j_healthcheck."""

    @pytest.mark.asyncio
    async def test_healthcheck_none_driver_returns_false(self) -> None:
        """neo4j_healthcheck returns False when driver is None."""
        from brain_v42.db.neo4j import neo4j_healthcheck

        result = await neo4j_healthcheck(None)
        assert result is False

    @pytest.mark.asyncio
    async def test_healthcheck_success(self) -> None:
        """neo4j_healthcheck returns True when session.run succeeds."""
        from brain_v42.db.neo4j import neo4j_healthcheck

        mock_session = AsyncMock()
        mock_driver = MagicMock()
        mock_driver.session.return_value.__aenter__ = AsyncMock(return_value=mock_session)
        mock_driver.session.return_value.__aexit__ = AsyncMock(return_value=False)

        result = await neo4j_healthcheck(mock_driver)
        assert result is True
        mock_session.run.assert_called_once_with("RETURN 1")

    @pytest.mark.asyncio
    async def test_healthcheck_failure(self) -> None:
        """neo4j_healthcheck returns False when session.run raises an exception."""
        from brain_v42.db.neo4j import neo4j_healthcheck

        mock_session = AsyncMock()
        mock_session.run.side_effect = Exception("Connection refused")
        mock_driver = MagicMock()
        mock_driver.session.return_value.__aenter__ = AsyncMock(return_value=mock_session)
        mock_driver.session.return_value.__aexit__ = AsyncMock(return_value=False)

        result = await neo4j_healthcheck(mock_driver)
        assert result is False

    @pytest.mark.asyncio
    async def test_healthcheck_connection_error(self) -> None:
        """neo4j_healthcheck returns False on connection error (no re-raise)."""
        from brain_v42.db.neo4j import neo4j_healthcheck

        mock_driver = MagicMock()
        mock_driver.session.return_value.__aenter__ = AsyncMock(
            side_effect=ConnectionError("timeout")
        )
        mock_driver.session.return_value.__aexit__ = AsyncMock(return_value=False)

        result = await neo4j_healthcheck(mock_driver)
        assert result is False
