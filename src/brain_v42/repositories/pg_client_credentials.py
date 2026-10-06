"""Client credential registry and admin elevations (migration 063).

Brain stores the SHA-256 digest of a bearer token and never the token. Two walls keep
``admin`` out of a credential: this module refuses it before any SQL, and the table's
CHECK refuses it again. Administrative power exists only as an elevation, granted to
the connections of one OPERATOR session and bounded to four hours.

Rows are revoked, never deleted: there is no delete path here. Every method accepts an
optional ``session`` so a caller can compose it into a larger transaction, as the other
``pg_*`` repositories do; without one, each call opens its own.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from uuid import UUID

import sqlalchemy as sa
from sqlalchemy import RowMapping
from sqlalchemy.ext.asyncio import AsyncSession

from brain_v42.db.tables import brain_admin_elevations, brain_client_credentials, brain_sessions
from brain_v42.repositories.pg_base import BasePgRepository

#: A credential seen again within this delay is not rewritten: the registry's NOTIFY
#: trigger fires on every UPDATE, and a busy client would otherwise reload it constantly.
LAST_USED_RESOLUTION = timedelta(minutes=1)

#: The longest elevation. The table's CHECK states the same bound.
MAX_ELEVATION = timedelta(hours=4)


class ClientCredentialError(ValueError):
    """A refused registry operation, carrying a stable ``code`` for callers to branch on."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True, slots=True)
class CredentialRow:
    """One registry row. The digest is stored but kept out of ``repr`` and logs."""

    id: UUID
    client_id: str
    token_sha256: bytes = field(repr=False)
    families: list[str]
    issuers: list[str]
    transition: bool
    created_at: datetime
    created_by: str
    expires_at: datetime | None
    revoked_at: datetime | None
    revoked_reason: str | None
    last_used_at: datetime | None


@dataclass(frozen=True, slots=True)
class ElevationRow:
    id: UUID
    session_id: UUID
    connection_ids: list[str]
    granted_at: datetime
    expires_at: datetime
    granted_by: str
    reason: str
    revoked_at: datetime | None


_CREDENTIAL_COLUMNS = list(brain_client_credentials.c)
_ELEVATION_COLUMNS = list(brain_admin_elevations.c)


def _credential(row: RowMapping) -> CredentialRow:
    return CredentialRow(**{column.name: row[column.name] for column in _CREDENTIAL_COLUMNS})


def _elevation(row: RowMapping) -> ElevationRow:
    return ElevationRow(**{column.name: row[column.name] for column in _ELEVATION_COLUMNS})


class PgClientCredentialRepo(BasePgRepository):
    """Own ``brain_client_credentials`` and ``brain_admin_elevations``."""

    table = brain_client_credentials
    fts_columns: list[str] = []

    async def issue(
        self,
        client_id: str,
        token_sha256: bytes,
        families: Sequence[str],
        issuers: Sequence[str],
        created_by: str,
        transition: bool = False,
        expires_at: datetime | None = None,
        *,
        session: AsyncSession | None = None,
    ) -> CredentialRow:
        if "admin" in families:
            raise ClientCredentialError(
                "admin_family_forbidden",
                "admin is not a credential family: administrative power is an elevation",
            )
        async with self._maybe_session(session, write=True) as sess:
            row = (
                (
                    await sess.execute(
                        sa.insert(brain_client_credentials)
                        .values(
                            client_id=client_id,
                            token_sha256=token_sha256,
                            families=list(families),
                            issuers=list(issuers),
                            created_by=created_by,
                            transition=transition,
                            expires_at=expires_at,
                        )
                        .returning(*_CREDENTIAL_COLUMNS)
                    )
                )
                .mappings()
                .one()
            )
        return _credential(row)

    async def active_rows(
        self, now: datetime, *, session: AsyncSession | None = None
    ) -> list[CredentialRow]:
        credentials = brain_client_credentials
        async with self._maybe_session(session, write=False) as sess:
            rows = (
                (
                    await sess.execute(
                        sa.select(credentials)
                        .where(
                            credentials.c.revoked_at.is_(None),
                            sa.or_(
                                credentials.c.expires_at.is_(None), credentials.c.expires_at > now
                            ),
                        )
                        .order_by(credentials.c.created_at, credentials.c.id)
                    )
                )
                .mappings()
                .all()
            )
        return [_credential(row) for row in rows]

    async def list_rows(self, *, session: AsyncSession | None = None) -> list[CredentialRow]:
        credentials = brain_client_credentials
        async with self._maybe_session(session, write=False) as sess:
            rows = (
                (
                    await sess.execute(
                        sa.select(credentials).order_by(credentials.c.created_at, credentials.c.id)
                    )
                )
                .mappings()
                .all()
            )
        return [_credential(row) for row in rows]

    async def revoke(
        self,
        credential_id: UUID,
        reason: str,
        now: datetime,
        *,
        session: AsyncSession | None = None,
    ) -> CredentialRow:
        if not reason.strip():
            raise ClientCredentialError("blank_reason", "a revocation needs a reason")
        credentials = brain_client_credentials
        async with self._maybe_session(session, write=True) as sess:
            row = (
                (
                    await sess.execute(
                        sa.update(credentials)
                        .where(
                            credentials.c.id == credential_id, credentials.c.revoked_at.is_(None)
                        )
                        .values(revoked_at=now, revoked_reason=reason)
                        .returning(*_CREDENTIAL_COLUMNS)
                    )
                )
                .mappings()
                .one_or_none()
            )
            if row is None:
                known = await sess.scalar(
                    sa.select(credentials.c.id).where(credentials.c.id == credential_id)
                )
                if known is None:
                    raise ClientCredentialError(
                        "unknown_credential", f"no credential {credential_id}"
                    )
                raise ClientCredentialError(
                    "already_revoked", f"credential {credential_id} is already revoked"
                )
        return _credential(row)

    async def touch_last_used(
        self,
        credential_ids: Sequence[UUID],
        now: datetime,
        *,
        session: AsyncSession | None = None,
    ) -> int:
        """Stamp ``last_used_at``, skipping rows stamped less than a minute ago."""
        if not credential_ids:
            return 0
        credentials = brain_client_credentials
        async with self._maybe_session(session, write=True) as sess:
            updated = await sess.execute(
                sa.update(credentials)
                .where(
                    credentials.c.id.in_(list(credential_ids)),
                    sa.or_(
                        credentials.c.last_used_at.is_(None),
                        credentials.c.last_used_at < now - LAST_USED_RESOLUTION,
                    ),
                )
                .values(last_used_at=now)
                .returning(credentials.c.id)
            )
            return len(updated.all())

    async def grant_elevation(
        self,
        session_id: UUID,
        connection_ids: Sequence[str],
        expires_at: datetime,
        granted_by: str,
        reason: str,
        now: datetime,
        *,
        session: AsyncSession | None = None,
    ) -> ElevationRow:
        if not connection_ids:
            raise ClientCredentialError("no_connections", "an elevation needs a connection")
        if not reason.strip():
            raise ClientCredentialError("blank_reason", "an elevation needs a reason")
        if not now < expires_at <= now + MAX_ELEVATION:
            raise ClientCredentialError(
                "invalid_window", "an elevation lasts at most 4 hours and must end after it starts"
            )
        async with self._maybe_session(session, write=True) as sess:
            # FOR SHARE: a session closing concurrently waits for this grant or is seen closed.
            owner = (
                await sess.execute(
                    sa.select(brain_sessions.c.status, brain_sessions.c.nature)
                    .where(brain_sessions.c.id == session_id)
                    .with_for_update(read=True)
                )
            ).one_or_none()
            if owner is None:
                raise ClientCredentialError("unknown_session", f"no session {session_id}")
            if owner.status != "open":
                raise ClientCredentialError(
                    "session_not_open", f"session {session_id} is {owner.status}, not open"
                )
            if owner.nature is not None:
                raise ClientCredentialError(
                    "session_not_operator",
                    f"session {session_id} is a {owner.nature} trace, not an operator session",
                )
            row = (
                (
                    await sess.execute(
                        sa.insert(brain_admin_elevations)
                        .values(
                            session_id=session_id,
                            connection_ids=list(connection_ids),
                            granted_at=now,
                            expires_at=expires_at,
                            granted_by=granted_by,
                            reason=reason,
                        )
                        .returning(*_ELEVATION_COLUMNS)
                    )
                )
                .mappings()
                .one()
            )
        return _elevation(row)

    async def active_elevations(
        self, now: datetime, *, session: AsyncSession | None = None
    ) -> list[ElevationRow]:
        elevations = brain_admin_elevations
        async with self._maybe_session(session, write=False) as sess:
            rows = (
                (
                    await sess.execute(
                        sa.select(elevations)
                        .where(elevations.c.revoked_at.is_(None), elevations.c.expires_at > now)
                        .order_by(elevations.c.granted_at, elevations.c.id)
                    )
                )
                .mappings()
                .all()
            )
        return [_elevation(row) for row in rows]

    async def end_elevation(
        self, elevation_id: UUID, now: datetime, *, session: AsyncSession | None = None
    ) -> ElevationRow:
        elevations = brain_admin_elevations
        async with self._maybe_session(session, write=True) as sess:
            row = (
                (
                    await sess.execute(
                        sa.update(elevations)
                        .where(elevations.c.id == elevation_id, elevations.c.revoked_at.is_(None))
                        .values(revoked_at=now)
                        .returning(*_ELEVATION_COLUMNS)
                    )
                )
                .mappings()
                .one_or_none()
            )
            if row is None:
                known = await sess.scalar(
                    sa.select(elevations.c.id).where(elevations.c.id == elevation_id)
                )
                if known is None:
                    raise ClientCredentialError("unknown_elevation", f"no elevation {elevation_id}")
                raise ClientCredentialError(
                    "already_ended", f"elevation {elevation_id} is already ended"
                )
        return _elevation(row)
