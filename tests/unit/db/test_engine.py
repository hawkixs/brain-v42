"""Unit tests for SQLAlchemy async engine + session factory."""

from unittest.mock import MagicMock

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker


@pytest.fixture(autouse=True)
def reset_engine_singletons():
    """Reset engine singletons before each test to ensure isolation."""
    import brain_v42.db.engine as engine_module

    engine_module._engine = None
    engine_module._session_factory = None
    yield
    # Cleanup after test - reset singletons
    engine_module._engine = None
    engine_module._session_factory = None


@pytest.fixture(autouse=True)
def mock_settings(monkeypatch):
    """Mock settings to avoid needing a real database URL."""
    mock = MagicMock()
    mock.postgres_url = "postgresql+asyncpg://brain:brain@localhost:5433/brain_test"
    mock.db_echo = False
    monkeypatch.setattr("brain_v42.db.engine.get_settings", lambda: mock)
    return mock


def test_get_engine_returns_async_engine():
    """get_engine() returns a SQLAlchemy AsyncEngine."""
    from brain_v42.db.engine import get_engine

    engine = get_engine()
    assert isinstance(engine, AsyncEngine)


def test_get_engine_singleton():
    """get_engine() returns the same engine on multiple calls (singleton)."""
    from brain_v42.db.engine import get_engine

    engine1 = get_engine()
    engine2 = get_engine()
    assert engine1 is engine2


def test_get_session_factory_returns_sessionmaker():
    """get_session_factory() returns an async_sessionmaker."""
    from brain_v42.db.engine import get_session_factory

    factory = get_session_factory()
    assert callable(factory)


def test_get_session_factory_is_async_sessionmaker():
    """get_session_factory() returns an async_sessionmaker instance."""
    from brain_v42.db.engine import get_session_factory

    factory = get_session_factory()
    assert isinstance(factory, async_sessionmaker)


def test_engine_pool_configuration():
    """Engine pool is configured with size=20, max_overflow=10, pool_timeout=10, pool_recycle=1800."""
    from brain_v42.db.engine import get_engine

    engine = get_engine()
    pool = engine.pool
    assert pool.size() == 20
    assert pool._max_overflow == 10
    assert pool._timeout == 10
    assert pool._recycle == 1800


def test_engine_pool_pre_ping():
    """Engine has pool_pre_ping=True for connection health checks."""
    from brain_v42.db.engine import get_engine

    engine = get_engine()
    # pool_pre_ping is stored in engine dialect
    assert engine.pool._pre_ping is True


@pytest.mark.asyncio
async def test_session_factory_produces_async_session():
    """Session factory produces AsyncSession instances."""
    from brain_v42.db.engine import get_session_factory

    factory = get_session_factory()
    async with factory() as session:
        assert isinstance(session, AsyncSession)


def test_session_factory_expire_on_commit_false():
    """Session factory is configured with expire_on_commit=False."""
    from brain_v42.db.engine import get_session_factory

    factory = get_session_factory()
    # Check kw_args stored in the factory
    assert factory.kw.get("expire_on_commit") is False


def test_session_factory_singleton():
    """get_session_factory() returns the same factory on multiple calls."""
    from brain_v42.db.engine import get_session_factory

    factory1 = get_session_factory()
    factory2 = get_session_factory()
    assert factory1 is factory2


@pytest.mark.asyncio
async def test_dispose_engine_clears_singleton():
    """dispose_engine() clears the singleton so next get_engine() creates fresh engine."""
    import brain_v42.db.engine as engine_module
    from brain_v42.db.engine import dispose_engine, get_engine

    # Get an engine to create the singleton
    get_engine()
    assert engine_module._engine is not None
    # Dispose
    await dispose_engine()
    # Singletons should be cleared
    assert engine_module._engine is None
    assert engine_module._session_factory is None
    # New call should create a new engine
    engine2 = get_engine()
    assert isinstance(engine2, AsyncEngine)


@pytest.mark.asyncio
async def test_dispose_engine_is_async():
    """dispose_engine() is an async function (coroutine)."""
    import inspect

    from brain_v42.db.engine import dispose_engine

    assert inspect.iscoroutinefunction(dispose_engine)


def test_db_module_re_exports():
    """brain_v42.db.__init__ re-exports get_engine, get_session_factory, dispose_engine."""
    from brain_v42.db import dispose_engine, get_engine, get_session_factory

    assert callable(get_engine)
    assert callable(get_session_factory)
    assert callable(dispose_engine)


def test_engine_uses_settings_postgres_url(mock_settings):
    """Engine is created from settings.postgres_url."""
    mock_settings.postgres_url = "postgresql+asyncpg://user:pass@custom:5433/testdb"
    from brain_v42.db.engine import get_engine

    engine = get_engine()
    # The engine URL should reflect the configured URL
    assert "custom" in str(engine.url) or "testdb" in str(engine.url)


def test_engine_log_masks_password(mock_settings):
    """The logged DSN must not contain the password (finding 2026-07-04)."""
    from structlog.testing import capture_logs

    mock_settings.postgres_url = "postgresql+asyncpg://brain:s3cret@localhost:5433/brain"
    from brain_v42.db.engine import get_engine

    with capture_logs() as logs:
        get_engine()

    created = [e for e in logs if e["event"] == "SQLAlchemy async engine created"]
    assert len(created) == 1
    assert "s3cret" not in created[0]["url"]
    assert "***" in created[0]["url"]
