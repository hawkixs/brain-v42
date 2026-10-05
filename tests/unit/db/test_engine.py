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
    engine_module._profile = "maintenance"
    yield
    # Cleanup after test - reset singletons
    engine_module._engine = None
    engine_module._session_factory = None
    engine_module._profile = "maintenance"


@pytest.fixture(autouse=True)
def mock_settings(monkeypatch):
    """Mock settings to avoid needing a real database URL."""
    mock = MagicMock()
    mock.postgres_url = "postgresql+asyncpg://brain:brain@localhost:5433/brain_test"
    mock.db_echo = False
    # Integers, not MagicMock attributes: a MagicMock would stringify into a
    # plausible-looking GUC and make the profile assertions meaningless.
    mock.pg_statement_timeout_ms = 120_000
    mock.pg_lock_timeout_ms = 30_000
    mock.pg_idle_in_transaction_session_timeout_ms = 300_000
    mock.pg_maintenance_statement_timeout_ms = 1_800_000
    mock.pg_maintenance_lock_timeout_ms = 300_000
    mock.pg_maintenance_idle_in_transaction_session_timeout_ms = 0
    mock.metrics_pg_statement_timeout_ms = 10_000
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


@pytest.fixture
def captured_engine_kwargs(monkeypatch):
    """Capture the kwargs get_engine() hands to create_async_engine."""
    import brain_v42.db.engine as engine_module

    captured: dict = {}

    def fake_create(url, **kwargs):
        captured.update(kwargs)
        return MagicMock(spec=AsyncEngine, url=MagicMock())

    monkeypatch.setattr(engine_module, "create_async_engine", fake_create)
    return captured


def test_default_profile_is_maintenance(captured_engine_kwargs):
    """Scripts and jobs that call get_engine() bare get the generous profile."""
    from brain_v42.db.engine import get_engine

    get_engine()

    assert captured_engine_kwargs["connect_args"]["server_settings"] == {
        "statement_timeout": "1800000",
        "lock_timeout": "300000",
        "idle_in_transaction_session_timeout": "0",
        "application_name": "brain-v42-maintenance",
    }


def test_interactive_profile_is_bounded(captured_engine_kwargs):
    from brain_v42.db.engine import get_engine, use_engine_profile

    use_engine_profile("interactive")
    get_engine()

    assert captured_engine_kwargs["connect_args"]["server_settings"] == {
        "statement_timeout": "120000",
        "lock_timeout": "30000",
        "idle_in_transaction_session_timeout": "300000",
        "application_name": "brain-v42-interactive",
    }


def test_metrics_profile_uses_its_own_statement_budget(mock_settings):
    from brain_v42.db.engine import pg_server_settings

    assert pg_server_settings(mock_settings, "metrics") == {
        "statement_timeout": "10000",
        "lock_timeout": "5000",
        "idle_in_transaction_session_timeout": "60000",
        "application_name": "brain-v42-metrics",
    }


def test_zero_disables(captured_engine_kwargs, mock_settings):
    """0 is PostgreSQL's own 'disabled': it is sent as such, never dropped."""
    mock_settings.pg_statement_timeout_ms = 0
    from brain_v42.db.engine import get_engine, use_engine_profile

    use_engine_profile("interactive")
    get_engine()

    assert captured_engine_kwargs["connect_args"]["server_settings"]["statement_timeout"] == "0"


def test_profile_after_engine_built_warns_and_keeps_engine(captured_engine_kwargs):
    """A late opt-in must not raise (a test may inject an engine first) nor swap it."""
    from structlog.testing import capture_logs

    import brain_v42.db.engine as engine_module

    first = engine_module.get_engine()
    with capture_logs() as logs:
        engine_module.use_engine_profile("interactive")

    assert engine_module.get_engine() is first
    assert engine_module._profile == "maintenance"
    assert [e["log_level"] for e in logs if e["event"] == "engine_profile_too_late"] == ["warning"]


@pytest.mark.asyncio
async def test_dispose_resets_profile(captured_engine_kwargs):
    import brain_v42.db.engine as engine_module
    from brain_v42.db.engine import dispose_engine, get_engine, use_engine_profile

    use_engine_profile("interactive")
    engine = get_engine()
    engine.dispose = _async_noop  # type: ignore[method-assign]

    await dispose_engine()

    assert engine_module._profile == "maintenance"


async def _async_noop() -> None:
    return None
