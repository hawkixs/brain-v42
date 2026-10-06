"""Restrict sidecar registry access even when its metrics connection can write."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from brain_v42.repositories.pg_client_credentials import (
    CredentialRow,
    Disposition,
    PgClientCredentialRepo,
)


class ReadOnlyCredentialRegistry:
    """Use the existing repository under PostgreSQL's transaction write guard.

    Metrics persistence needs writes on the same engine. Set READ ONLY before
    each registry query instead of changing the engine's shared defaults.
    """

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory
        self._repository = PgClientCredentialRepo(session_factory)

    async def active_rows(self, now: datetime) -> list[CredentialRow]:
        async with self._session_factory() as session:
            await session.execute(text("SET TRANSACTION READ ONLY"))
            return await self._repository.active_rows(now, session=session)

    async def disposition_by_digest(
        self, token_sha256: bytes, now: datetime
    ) -> tuple[str | None, Disposition]:
        async with self._session_factory() as session:
            await session.execute(text("SET TRANSACTION READ ONLY"))
            return await self._repository.disposition_by_digest(token_sha256, now, session=session)
