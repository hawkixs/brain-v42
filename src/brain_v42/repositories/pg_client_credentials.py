"""Client credential registry and admin elevations (migration 063).

Brain stores the SHA-256 digest of a bearer token and never the token. Two walls keep
``admin`` out of a credential: this module refuses it before any SQL, and the table's
CHECK refuses it again. Administrative power exists only as an elevation, granted to
the connections of one OPERATOR session and bounded to four hours. Only the connections
of an allowlisted client are elevated, and a session's attributed connections must all
belong to the client that owns it (``claim_or_check_session_owner``).

Every gesture (issue, revoke, grant, end) inserts its audit row into
``brain_credential_audit`` in the SAME transaction, so the two commit together or not at
all. The outbox is drained at-least-once through ``claim_unemitted_audit`` and
``mark_audit_emitted``, and the natural expiry of an elevation is audited by
``audit_expired_elevations``. No audit row holds a token, a digest or a connection id.

Rows are revoked, never deleted: there is no delete path here. Every method accepts an
optional ``session`` so a caller can compose it into a larger transaction, as the other
``pg_*`` repositories do; without one, each call opens its own.
"""

from __future__ import annotations

from collections.abc import Collection, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any, Literal
from uuid import UUID

import sqlalchemy as sa
from sqlalchemy import RowMapping
from sqlalchemy.ext.asyncio import AsyncSession

from brain_v42.db.tables import (
    brain_admin_elevations,
    brain_client_credentials,
    brain_credential_audit,
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

#: The clients whose connections an elevation may freeze, and the only ones that may claim
#: an operator session nobody owns yet. The server's configuration will override it.
DEFAULT_ELEVATABLE_CLIENT_IDS = frozenset({"workstation-claude"})

#: What a presented digest is, as far as the registry knows: ``unknown`` has no row.
Disposition = Literal["active", "revoked", "expired", "unknown"]

#: The longest elevation reason, in characters. The table's CHECK states the same bound.
MAX_REASON_LENGTH = 200


class ClientCredentialError(ValueError):
    """A refused registry operation, carrying a stable ``code`` for callers to branch on."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class ForeignClientAttachError(ClientCredentialError):
    """A client tried to attach to an operator session that is not its own.

    Carries both client ids for the caller's refusal event. The message names no
    connection id, and ``owner_client_id`` is None while nobody owns the session.
    """

    def __init__(self, requesting_client_id: str, owner_client_id: str | None) -> None:
        super().__init__(
            "foreign_client_attach",
            "a client cannot attach to an operator session that is not its own",
        )
        self.requesting_client_id = requesting_client_id
        self.owner_client_id = owner_client_id


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
    excluded_client_ids: list[str]
    excluded_connection_count: int
    revoked_at: datetime | None
    expiry_audited_at: datetime | None


@dataclass(frozen=True, slots=True)
class AuditRow:
    """One outbox row: ``payload`` is the event's body, ``emitted_at`` is None until drained."""

    id: int
    event: str
    elevation_id: UUID | None
    payload: dict[str, Any]
    created_at: datetime
    emitted_at: datetime | None


_CREDENTIAL_COLUMNS = list(brain_client_credentials.c)
_ELEVATION_COLUMNS = list(brain_admin_elevations.c)
_AUDIT_COLUMNS = list(brain_credential_audit.c)


def _credential(row: RowMapping) -> CredentialRow:
    return CredentialRow(**{column.name: row[column.name] for column in _CREDENTIAL_COLUMNS})


def _elevation(row: RowMapping) -> ElevationRow:
    return ElevationRow(**{column.name: row[column.name] for column in _ELEVATION_COLUMNS})


def _audit(row: RowMapping) -> AuditRow:
    return AuditRow(**{column.name: row[column.name] for column in _AUDIT_COLUMNS})


def _require_author(author: str) -> None:
    """Refuse a blank author: every audited gesture names who made it."""
    if not author.strip():
        raise ClientCredentialError("blank_author", "a gesture needs a non-blank author")


def _elevation_event(
    row: ElevationRow,
    session_label: str,
    *,
    via: str,
    client_id: str | None,
    author: str | None,
) -> dict[str, Any]:
    """The body of an elevation event: the grant's fields, never a connection id.

    ``via``, ``client_id`` and ``author`` are the GESTURE's (the grant's, or the ending
    one's); ``author`` is None for a natural expiry, which no one performs.
    ``session_label`` is the session's raw ``client_key``: the emitter sanitises it.
    """
    return {
        "elevation_id": str(row.id),
        "session_id": str(row.session_id),
        "session_label": session_label,
        "expires_at": row.expires_at.astimezone(UTC).isoformat(),
        "ttl_seconds": int((row.expires_at - row.granted_at).total_seconds()),
        "reason": row.reason,
        "author": author,
        "via": via,
        "client_id": client_id,
        "connection_count": len(row.connection_ids),
        "excluded_client_ids": list(row.excluded_client_ids),
        "excluded_connection_count": row.excluded_connection_count,
    }


async def _write_audit(
    sess: AsyncSession,
    event: str,
    payload: dict[str, Any],
    elevation_id: UUID | None = None,
) -> None:
    await sess.execute(
        sa.insert(brain_credential_audit).values(
            event=event, elevation_id=elevation_id, payload=payload
        )
    )


class PgClientCredentialRepo(BasePgRepository):
    """Own the credential registry, the elevations and their audit outbox."""

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
        _require_author(created_by)
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
            await _write_audit(
                sess,
                "credentials.issued",
                {
                    "credential_id": str(row["id"]),
                    "client_id": row["client_id"],
                    "families": list(row["families"]),
                    "author": row["created_by"],
                },
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

    async def disposition_by_digest(
        self, token_sha256: bytes, now: datetime, *, session: AsyncSession | None = None
    ) -> tuple[str | None, Disposition]:
        """Say why a digest is refused, which ``active_rows`` cannot: revoked rows count here.

        Returns ``(client_id, disposition)``, and ``(None, "unknown")`` when no row has
        the digest. A revoked row is ``revoked`` even if it has also expired: the
        operator's revocation is the more telling answer. A row is ``expired`` from the
        instant ``now`` reaches its ``expires_at``, as ``active_rows`` decides.
        """
        credentials = brain_client_credentials
        async with self._maybe_session(session, write=False) as sess:
            row = (
                await sess.execute(
                    sa.select(
                        credentials.c.client_id, credentials.c.revoked_at, credentials.c.expires_at
                    ).where(credentials.c.token_sha256 == token_sha256)
                )
            ).one_or_none()
        if row is None:
            return None, "unknown"
        if row.revoked_at is not None:
            return row.client_id, "revoked"
        if row.expires_at is not None and row.expires_at <= now:
            return row.client_id, "expired"
        return row.client_id, "active"

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
        author: str,
        session: AsyncSession | None = None,
    ) -> CredentialRow:
        """Revoke a credential. ``author`` is audited only: the table has no ``revoked_by``."""
        _require_author(author)
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
            await _write_audit(
                sess,
                "credentials.revoked",
                {
                    "credential_id": str(row["id"]),
                    "client_id": row["client_id"],
                    "families": list(row["families"]),
                    "reason": row["revoked_reason"],
                    "author": author,
                },
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
        elevatable_client_ids: Collection[str],
        via: Literal["hook", "cli"] = "cli",
        requested_by_client_id: str | None = None,
        session: AsyncSession | None = None,
    ) -> ElevationRow:
        """Freeze the ATTRIBUTED (connection, client) pairs of an allowlisted client.

        Attributed connections of any other client are not elevated: the row records their
        client ids and count (``excluded_*``) so the audit can report what was left out.

        Several elevations may be active at once, on one session or on several: a grant is
        never refused because another is in force.
        """
        _require_author(granted_by)
        connection_ids = list(dict.fromkeys(connection_ids))
        if not connection_ids:
            raise ClientCredentialError(
                "no_attributed_connection", f"session {session_id} has no connection to elevate"
            )
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
        if via == "cli" and requested_by_client_id is not None:
            raise ClientCredentialError(
                "requester_forbidden", "a cli elevation names no requesting credential"
            )
        if not elevatable_client_ids:
            raise ClientCredentialError(
                "no_elevatable_clients", "an elevation needs a non-empty set of elevatable clients"
            )
        if not now < expires_at <= now + MAX_ELEVATION:
            raise ClientCredentialError(
                "invalid_window", "an elevation lasts at most 4 hours and must end after it starts"
            )
        async with self._maybe_session(session, write=True) as sess:
            # FOR SHARE: a session closing concurrently waits for this grant or is seen closed.
            owner = (
                await sess.execute(
                    sa.select(
                        brain_sessions.c.status,
                        brain_sessions.c.nature,
                        brain_sessions.c.client_key,
                    )
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
            attributed = [
                (conn, linked[conn]) for conn in connection_ids if linked[conn] is not None
            ]
            if not attributed:
                raise ClientCredentialError(
                    "no_attributed_connection",
                    f"session {session_id} has no connection attributed to a credential",
                )
            frozen = [
                (conn, client) for conn, client in attributed if client in elevatable_client_ids
            ]
            excluded = [client for _, client in attributed if client not in elevatable_client_ids]
            if not frozen:
                raise ClientCredentialError(
                    "no_elevatable_connection",
                    f"session {session_id} has no connection attributed to an elevatable client",
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
                            excluded_client_ids=sorted(set(excluded)),
                            excluded_connection_count=len(excluded),
                        )
                        .returning(*_ELEVATION_COLUMNS)
                    )
                )
                .mappings()
                .one()
            )
            granted = _elevation(row)
            await _write_audit(
                sess,
                "credentials.elevated",
                _elevation_event(
                    granted,
                    owner.client_key,
                    via=via,
                    client_id=requested_by_client_id,
                    author=granted_by,
                ),
                granted.id,
            )
        return granted

    async def claim_or_check_session_owner(
        self,
        session_id: UUID,
        client_id: str,
        *,
        elevatable_client_ids: Collection[str],
        session: AsyncSession | None = None,
    ) -> str:
        """Bind an OPERATOR session to the client that attaches to it, or refuse another.

        Locks the session row, so two attaching clients cannot both claim it. Call it
        BEFORE writing the connection row: the trigger on ``brain_session_connections``
        refuses an attributed row whose client is not the opener. An unowned session is
        claimed only by an allowlisted client, so a write credential that knows the
        session id and client key cannot take a pre-cutover operator session ahead of the
        operator's own client. Fail-closed on ``nature``: only an agent trace is skipped,
        every other nature is an operator session, like the trigger. Agent traces are not checked, and ``client_id`` comes back.
        """
        async with self._maybe_session(session, write=True) as sess:
            owner = (
                await sess.execute(
                    sa.select(brain_sessions.c.nature, brain_sessions.c.opener_client_id)
                    .where(brain_sessions.c.id == session_id)
                    .with_for_update()
                )
            ).one_or_none()
            if owner is None:
                raise ClientCredentialError("unknown_session", f"no session {session_id}")
            if owner.nature == "agent":
                return client_id
            if owner.opener_client_id is None:
                if client_id not in elevatable_client_ids:
                    raise ForeignClientAttachError(client_id, None)
                await sess.execute(
                    sa.update(brain_sessions)
                    .where(brain_sessions.c.id == session_id)
                    .values(opener_client_id=client_id)
                )
                return client_id
            if owner.opener_client_id != client_id:
                raise ForeignClientAttachError(client_id, owner.opener_client_id)
            return client_id

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
        self,
        elevation_id: UUID,
        now: datetime,
        *,
        author: str,
        session: AsyncSession | None = None,
    ) -> ElevationRow:
        """End an elevation. ``author`` is audited only: the table has no ``ended_by``."""
        _require_author(author)
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
            ended = _elevation(row)
            session_label = await sess.scalar(
                sa.select(brain_sessions.c.client_key).where(
                    brain_sessions.c.id == ended.session_id
                )
            )
            # Only the CLI ends an elevation in 0.6.x: the ending gesture names no credential.
            await _write_audit(
                sess,
                "credentials.unelevated",
                _elevation_event(
                    ended, str(session_label), via="cli", client_id=None, author=author
                ),
                ended.id,
            )
        return ended

    async def audit_expired_elevations(
        self, now: datetime, *, session: AsyncSession | None = None
    ) -> int:
        """Audit, once each, the elevations that ran out without being ended.

        In ONE transaction: select the elevations expired at ``now``, neither ended nor
        audited yet (``FOR UPDATE SKIP LOCKED``, so two sweepers never take the same one),
        write one ``credentials.elevation_expired`` row per elevation and stamp
        ``expiry_audited_at``. Idempotent: an audited elevation is never selected again.
        Returns how many elevations it audited.
        """
        elevations = brain_admin_elevations
        async with self._maybe_session(session, write=True) as sess:
            due = (
                (
                    await sess.execute(
                        sa.select(elevations, brain_sessions.c.client_key.label("session_label"))
                        .join(brain_sessions, brain_sessions.c.id == elevations.c.session_id)
                        .where(
                            elevations.c.expires_at <= now,
                            elevations.c.revoked_at.is_(None),
                            elevations.c.expiry_audited_at.is_(None),
                        )
                        .order_by(elevations.c.expires_at, elevations.c.id)
                        .with_for_update(of=elevations, skip_locked=True)
                    )
                )
                .mappings()
                .all()
            )
            for row in due:
                expired = _elevation(row)
                await _write_audit(
                    sess,
                    "credentials.elevation_expired",
                    _elevation_event(
                        expired,
                        row["session_label"],
                        via=expired.via,
                        client_id=expired.requested_by_client_id,
                        author=None,
                    ),
                    expired.id,
                )
            if due:
                await sess.execute(
                    sa.update(elevations)
                    .where(elevations.c.id.in_([row["id"] for row in due]))
                    .values(expiry_audited_at=now)
                )
        return len(due)

    async def claim_unemitted_audit(self, limit: int, *, session: AsyncSession) -> list[AuditRow]:
        """Lock up to ``limit`` unemitted rows, oldest first, for the drainer to emit.

        ``FOR UPDATE SKIP LOCKED``: a concurrent drainer skips them. The locks last as long
        as ``session``'s transaction, so the caller emits, then calls ``mark_audit_emitted``
        in the SAME transaction. A crash between the two rolls the claim back and the rows
        are emitted again: the drain is at-least-once, and the consumer dedupes on
        ``(event, elevation_id)``.
        """
        audit = brain_credential_audit
        rows = (
            (
                await session.execute(
                    sa.select(audit)
                    .where(audit.c.emitted_at.is_(None))
                    .order_by(audit.c.id)
                    .limit(limit)
                    .with_for_update(skip_locked=True)
                )
            )
            .mappings()
            .all()
        )
        return [_audit(row) for row in rows]

    async def mark_audit_emitted(
        self, ids: Sequence[int], now: datetime, *, session: AsyncSession | None = None
    ) -> int:
        """Stamp ``emitted_at`` on rows not yet stamped: the first stamp stands."""
        if not ids:
            return 0
        audit = brain_credential_audit
        async with self._maybe_session(session, write=True) as sess:
            marked = await sess.execute(
                sa.update(audit)
                .where(audit.c.id.in_(list(ids)), audit.c.emitted_at.is_(None))
                .values(emitted_at=now)
                .returning(audit.c.id)
            )
            return len(marked.all())
