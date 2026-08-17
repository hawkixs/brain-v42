"""SQLAlchemy async engine and session factory for brain_v42.

Pool configuration:
- pool_size=20     : Up to 20 connections maintained (single long-lived server)
- max_overflow=10  : Up to 10 additional connections under load (hard cap 30)
- pool_timeout=10  : Wait max 10s for a connection before raising
- pool_recycle=1800: Recycle connections older than 30 min (long-lived server)
- pool_pre_ping=True: Detect stale connections before use
"""

from __future__ import annotations

import structlog
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from brain_v42.config import get_settings

logger = structlog.get_logger(__name__)

# Module-level singletons
_engine: AsyncEngine | None = None
_session_factory: async_sessionmaker[AsyncSession] | None = None


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
        )
        logger.info(
            "SQLAlchemy async engine created",
            url=_engine.url.render_as_string(hide_password=True),
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
    global _engine, _session_factory
    if _engine is not None:
        await _engine.dispose()
        logger.info("SQLAlchemy engine disposed")
    _engine = None
    _session_factory = None
