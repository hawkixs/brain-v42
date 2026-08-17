"""Alembic environment for brain_v42.

Uses async SQLAlchemy (asyncpg) with NullPool for migrations.
Imports target_metadata from brain_v42.db.tables.
Requires an explicit, validated POSTGRES_URL for every migration.
"""

from __future__ import annotations

import asyncio
import os
from logging.config import fileConfig

from alembic import context
from sqlalchemy import MetaData, pool
from sqlalchemy.engine import URL, Connection, make_url
from sqlalchemy.exc import ArgumentError
from sqlalchemy.ext.asyncio import async_engine_from_config

# Alembic Config object — provides access to values within the .ini file
config = context.config

# Set up logging from alembic.ini config
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# Import target_metadata from brain_v42.db.tables. Manual migrations remain available
# when the package is not installed, while autogenerate requires this metadata.
target_metadata: MetaData | None
try:
    from brain_v42.db.tables import METADATA
except ImportError:
    target_metadata = None
else:
    target_metadata = METADATA


def _resolve_sqlalchemy_url() -> str:
    """Return the explicit migration URL after fail-closed safety checks."""
    raw_url = os.environ.get("POSTGRES_URL")
    if not raw_url:
        raise RuntimeError("POSTGRES_URL is required for Alembic migrations") from None

    parsed_url: URL | None = None
    try:
        parsed_url = make_url(raw_url)
        _ = parsed_url.port
    except (ArgumentError, ValueError):
        parsed_url = None

    if parsed_url is None:
        raise RuntimeError("POSTGRES_URL is invalid") from None
    if parsed_url.query:
        raise RuntimeError("POSTGRES_URL must not include query parameters") from None

    if parsed_url.drivername != "postgresql+asyncpg":
        raise RuntimeError("POSTGRES_URL must use the postgresql+asyncpg driver") from None
    if not parsed_url.database:
        raise RuntimeError("POSTGRES_URL must include a database name") from None
    if not parsed_url.host:
        raise RuntimeError("POSTGRES_URL must include a host") from None
    if parsed_url.port is None or not 1 <= parsed_url.port <= 65535:
        raise RuntimeError("POSTGRES_URL must include a port between 1 and 65535") from None
    if not parsed_url.username:
        raise RuntimeError("POSTGRES_URL must include a username") from None
    if not parsed_url.password:
        raise RuntimeError("POSTGRES_URL must include a password") from None

    allowed_prod_values = {"1", "true", "yes"}
    prod_opt_in = os.environ.get("BRAIN_ALEMBIC_ALLOW_PROD", "").casefold()
    if parsed_url.database == "brain" and prod_opt_in not in allowed_prod_values:
        raise RuntimeError(
            "Refusing the brain database without BRAIN_ALEMBIC_ALLOW_PROD=1|true|yes"
        ) from None

    return raw_url


# ConfigParser treats percent signs as interpolation markers. Escape them only at the
# Alembic boundary while keeping the resolver's return value identical to POSTGRES_URL.
_resolved_url = _resolve_sqlalchemy_url()
config.set_main_option("sqlalchemy.url", _resolved_url.replace("%", "%%"))


def run_migrations_offline() -> None:
    """Run migrations in 'offline' mode.

    Configures the context with just a URL and not an Engine.
    Calls to context.execute() emit the given string to script output.
    """
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )

    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection: Connection) -> None:
    """Synchronous callback to run migrations inside an async context."""
    context.configure(connection=connection, target_metadata=target_metadata)
    with context.begin_transaction():
        context.run_migrations()


async def run_async_migrations() -> None:
    """Run migrations in async mode using asyncpg + NullPool.

    NullPool is critical: prevents the asyncio event loop from hanging
    when the migration completes (avoids connection pool cleanup issues).
    """
    connectable = async_engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )

    async with connectable.connect() as connection:
        await connection.run_sync(do_run_migrations)

    await connectable.dispose()


def run_migrations_online() -> None:
    """Run migrations in 'online' mode using asyncio.run()."""
    asyncio.run(run_async_migrations())


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
