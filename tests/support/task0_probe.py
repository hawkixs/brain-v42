"""Bounded asyncpg readiness and PostgreSQL version probe for Task 0."""

from __future__ import annotations

import asyncio
import os

import asyncpg


async def _probe() -> None:
    connection = await asyncio.wait_for(
        asyncpg.connect(os.environ["PG16_DSN"], timeout=1, command_timeout=1),
        timeout=1.5,
    )
    try:
        version = await asyncio.wait_for(
            connection.fetchval("SHOW server_version_num"),
            timeout=1,
        )
        if version != "160014":
            raise RuntimeError("unexpected_postgresql_version")
        print(version)
    finally:
        await asyncio.wait_for(connection.close(), timeout=1)


asyncio.run(_probe())
