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
from typing import Literal
from uuid import UUID

import sqlalchemy as sa
from sqlalchemy import RowMapping
from sqlalchemy.ext.asyncio import AsyncSession

from brain_v42.db.tables import (
    brain_admin_elevations,
    brain_client_credentials,
    brain_session_connections,
    brain_sessions,
)
from brain_v42.repositories.pg_base import BasePgRepository

#: A credential seen again within this delay is not rewritten: it bounds the write rate of
#: a busy client. The NOTIFY trigger ignores a ``last_used_at``-only UPDATE, so the stamp
#: costs no registry reload.
LAST_USED_RESOLUTION = timedelta(minutes=1)

#: The longest elevation. The table's CHECK states the same bound.
MAX_ELEVATION = timedelta(hours=4)

#: The longest elevation reason, in characters. The table's CHECK states the same bound.
MAX_REASON_LENGTH = 200


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
    connection_client_ids: list[str]
    granted_at: datetime
    expires_at: datetime
    granted_by: str
    reason: str
    via: str
    requested_by_client_id: str | None
    revoked_at: datetime | None
    expiry_audited_at: datetime | None


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
        via: Literal["hook", "cli"] = "cli",
        requested_by_client_id: str | None = None,
        session: AsyncSession | None = None,
    ) -> ElevationRow:
        """Freeze the ATTRIBUTED (connection, client) pairs of ``connection_ids``.

        Several elevations may be active at once, on one session or on several: a grant is
        never refused because another is in force.
        """
        connection_ids = list(dict.fromkeys(connection_ids))
        if not connection_ids:
            raise ClientCredentialError("no_connections", "an elevation needs a connection")
        if not reason.strip():
            raise ClientCredentialError("blank_reason", "an elevation needs a reason")
        if len(reason) > MAX_REASON_LENGTH:
            raise ClientCredentialError(
                "reason_too_long", f"an elevation reason has at most {MAX_REASON_LENGTH} characters"
            )
        if via == "hook" and requested_by_client_id is None:
            raise ClientCredentialError(
                "requester_required", "a hook elevation names the credential that asked for it"
            )
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
            # The grant is frozen on connections already linked to THIS session: one linked
            # afterwards, or to another session, never inherits admin.
            links = brain_session_connections
            linked = dict(
                (
                    await sess.execute(
                        sa.select(links.c.connection_id, links.c.client_id).where(
                            links.c.session_id == session_id,
                            links.c.connection_id.in_(connection_ids),
                        )
                    )
                )
                .tuples()
                .all()
            )
            unlinked = [conn for conn in connection_ids if conn not in linked]
            if unlinked:
                shown = ", ".join(f"{conn[:8]}..." for conn in unlinked)
                raise ClientCredentialError(
                    "connection_not_linked",
                    f"connection(s) {shown} not linked to session {session_id}",
                )
            # A historical connection has no attributed credential, and no pair to freeze.
            frozen = [(conn, linked[conn]) for conn in connection_ids if linked[conn] is not None]
            if not frozen:
                raise ClientCredentialError(
                    "no_attributed_connection",
                    f"session {session_id} has no connection attributed to a credential",
                )
            row = (
                (
                    await sess.execute(
                        sa.insert(brain_admin_elevations)
                        .values(
                            session_id=session_id,
                            connection_ids=[conn for conn, _ in frozen],
                            connection_client_ids=[client for _, client in frozen],
                            granted_at=now,
                            expires_at=expires_at,
                            granted_by=granted_by,
                            reason=reason,
                            via=via,
                            requested_by_client_id=requested_by_client_id,
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
        """Elevations in force: not revoked, not expired, and their session still open."""
        elevations = brain_admin_elevations
        async with self._maybe_session(session, write=False) as sess:
            rows = (
                (
                    await sess.execute(
                        sa.select(elevations)
                        .join(brain_sessions, brain_sessions.c.id == elevations.c.session_id)
                        .where(
                            elevations.c.revoked_at.is_(None),
                            elevations.c.expires_at > now,
                            brain_sessions.c.status == "open",
                        )
                        .order_by(elevations.c.granted_at, elevations.c.id)
                    )
                )
                .mappings()
                .all()
            )
        return [_elevation(row) for row in rows]

    async def has_active_elevation(
        self,
        connection_id: str,
        client_id: str,
        now: datetime,
        *,
        session: AsyncSession | None = None,
    ) -> bool:
        """Whether an elevation in force froze exactly this (connection, client) pair.

        Compares the frozen arrays, never ``brain_session_connections``: a connection
        linked, or re-attributed, after the grant is not elevated.
        """
        elevations = brain_admin_elevations
        frozen_pairs = (
            sa.func.unnest(elevations.c.connection_ids, elevations.c.connection_client_ids)
            .table_valued("connection_id", "client_id")
            .render_derived()
        )
        async with self._maybe_session(session, write=False) as sess:
            found = await sess.scalar(
                sa.select(
                    sa.exists(
                        sa.select(sa.literal(1))
                        .select_from(elevations)
                        .join(brain_sessions, brain_sessions.c.id == elevations.c.session_id)
                        .join(frozen_pairs, sa.true())
                        .where(
                            elevations.c.revoked_at.is_(None),
                            elevations.c.expires_at > now,
                            brain_sessions.c.status == "open",
                            frozen_pairs.c.connection_id == connection_id,
                            frozen_pairs.c.client_id == client_id,
                        )
                    )
                )
            )
        return bool(found)

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
