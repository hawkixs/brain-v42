"""SQLAlchemy async engine and session factory for brain_v42.

Pool configuration:
- pool_size=20     : Up to 20 connections maintained (single long-lived server)
- max_overflow=10  : Up to 10 additional connections under load (hard cap 30)
- pool_timeout=10  : Wait max 10s for a connection before raising
- pool_recycle=1800: Recycle connections older than 30 min (long-lived server)
- pool_pre_ping=True: Detect stale connections before use
"""

from __future__ import annotations

from typing import Any, Literal

import structlog
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from brain_v42.config import Settings, get_settings

logger = structlog.get_logger(__name__)

EngineProfile = Literal["interactive", "maintenance", "metrics"]

# The metrics scrape path runs about fifteen cheap queries per poll: a lock wait
# or an idle transaction there is a bug, so these two are constants, not knobs.
_METRICS_LOCK_TIMEOUT_MS = 5_000
_METRICS_IDLE_IN_TRANSACTION_TIMEOUT_MS = 60_000

# Module-level singletons
_engine: AsyncEngine | None = None
_session_factory: async_sessionmaker[AsyncSession] | None = None
# Maintenance is the default so that a script, a job or a test calling get_engine()
# bare is never cut short; only a long-lived interactive process opts into the
# tight profile, in its own entry point (never in a library function).
_profile: EngineProfile = "maintenance"


def pg_server_settings(settings: Settings, profile: EngineProfile) -> dict[str, str]:
    """The PostgreSQL session settings of one profile, as asyncpg ``server_settings``.

    Sent at connection time, so they hold for the whole session and win over any
    ``ALTER ROLE``/``ALTER DATABASE SET`` default. Values are strings of milliseconds;
    "0" is PostgreSQL's own "disabled" and is sent as such.
    """
    if profile == "interactive":
        statement = settings.pg_statement_timeout_ms
        lock = settings.pg_lock_timeout_ms
        idle = settings.pg_idle_in_transaction_session_timeout_ms
    elif profile == "maintenance":
        statement = settings.pg_maintenance_statement_timeout_ms
        lock = settings.pg_maintenance_lock_timeout_ms
        idle = settings.pg_maintenance_idle_in_transaction_session_timeout_ms
    else:
        statement = settings.metrics_pg_statement_timeout_ms
        lock = _METRICS_LOCK_TIMEOUT_MS
        idle = _METRICS_IDLE_IN_TRANSACTION_TIMEOUT_MS
    return {
        "statement_timeout": str(statement),
        "lock_timeout": str(lock),
        "idle_in_transaction_session_timeout": str(idle),
        "application_name": f"brain-v42-{profile}",
    }


def pg_connect_args(settings: Settings, profile: EngineProfile) -> dict[str, Any]:
    """``connect_args`` for ``create_async_engine`` carrying one profile's budgets."""
    return {"server_settings": pg_server_settings(settings, profile)}


def use_engine_profile(profile: EngineProfile) -> None:
    """Select the profile of the shared engine, before it is first built.

    Called once, from a process entry point. A call that arrives after the engine
    exists cannot change its connections: it warns and keeps the engine instead of
    raising, because a test may legitimately have injected one first.
    """
    global _profile
    if _engine is not None:
        if profile != _profile:
            logger.warning("engine_profile_too_late", requested=profile, active=_profile)
        return
    _profile = profile


def get_engine() -> AsyncEngine:
    """Return the shared AsyncEngine singleton.

    Creates the engine on first call using settings.postgres_url.
    Subsequent calls return the cached engine.
    """
    global _engine
    if _engine is None:
        settings = get_settings()
        _engine = create_async_engine(
            settings.postgres_url,
            pool_size=20,
            max_overflow=10,
            pool_timeout=10,
            pool_recycle=1800,
            pool_pre_ping=True,
            echo=False,
            connect_args=pg_connect_args(settings, _profile),
        )
        logger.info(
            "SQLAlchemy async engine created",
            url=_engine.url.render_as_string(hide_password=True),
            profile=_profile,
        )
    return _engine


def get_session_factory() -> async_sessionmaker[AsyncSession]:
    """Return the shared async_sessionmaker singleton.

    The factory is configured with:
    - expire_on_commit=False: objects remain usable after commit (important for async)
    - class_=AsyncSession: explicit async session class
    """
    global _session_factory
    if _session_factory is None:
        engine = get_engine()
        _session_factory = async_sessionmaker(
            engine,
            class_=AsyncSession,
            expire_on_commit=False,
        )
        logger.info("SQLAlchemy async session factory created")
    return _session_factory


async def dispose_engine() -> None:
    """Dispose the engine and clear singletons.

    Call during application shutdown to close all connections gracefully.
    Note: dispose_engine must be async because AsyncEngine.dispose() is a coroutine.
    """
    global _engine, _session_factory, _profile
    if _engine is not None:
        await _engine.dispose()
        logger.info("SQLAlchemy engine disposed")
    _engine = None
    _session_factory = None
    _profile = "maintenance"
