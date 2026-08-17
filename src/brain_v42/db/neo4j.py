"""Neo4j async driver factory.

Provides factory and lifecycle helpers for the optional Neo4j relationship index.
All functions are no-ops when neo4j is disabled (url=None or enabled=False).
"""

from __future__ import annotations

import structlog
from neo4j import AsyncDriver, AsyncGraphDatabase

log = structlog.get_logger(__name__)


def create_neo4j_driver(
    url: str | None,
    user: str = "neo4j",
    password: str = "",
    enabled: bool = True,
) -> AsyncDriver | None:
    """Create and return an async Neo4j driver, or None if disabled.

    Args:
        url: Bolt URL for Neo4j (e.g. bolt://localhost:7687). Returns None if falsy.
        user: Neo4j username (default: "neo4j").
        password: Neo4j password (default: "").
        enabled: Set to False to disable Neo4j even when url is provided.

    Returns:
        AsyncDriver instance, or None if url is falsy or enabled is False.
    """
    if not url or not enabled:
        return None
    log.info("neo4j.driver.creating", url=url, user=user)
    return AsyncGraphDatabase.driver(url, auth=(user, password), max_connection_pool_size=20)


async def close_neo4j_driver(driver: AsyncDriver | None) -> None:
    """Close the Neo4j driver if it is not None.

    Args:
        driver: AsyncDriver instance to close, or None (no-op).
    """
    if driver:
        await driver.close()
        log.info("neo4j.driver.closed")


async def neo4j_healthcheck(driver: AsyncDriver | None) -> bool:
    """Check Neo4j connectivity by running RETURN 1.

    Args:
        driver: AsyncDriver to use for the check, or None.

    Returns:
        True if the driver is reachable, False otherwise (including None driver).
    """
    if not driver:
        return False
    try:
        async with driver.session() as session:
            await session.run("RETURN 1")
        return True
    except Exception as exc:
        log.warning("neo4j.healthcheck.failed", error=str(exc))
        return False
