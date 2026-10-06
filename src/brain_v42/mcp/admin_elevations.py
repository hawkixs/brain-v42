"""Authenticate the operator hook independently of the MCP connection boundary."""

import asyncio
import json
import time
from collections.abc import Callable, Collection
from datetime import UTC, datetime, timedelta
from uuid import UUID

import sqlalchemy as sa
from starlette.requests import ClientDisconnect, Request
from starlette.responses import JSONResponse

from brain_v42.credentials.bucket import TokenBucket
from brain_v42.credentials.elevation_refusals import (
    ELEVATION_REFUSAL_STATUSES,
    emit_elevation_refusal,
)
from brain_v42.credentials.reasons import REFUSAL_STATUSES, emit_refusal
from brain_v42.credentials.verifier import CredentialRefused, CredentialVerifier
from brain_v42.db.tables import brain_session_connections, brain_sessions
from brain_v42.repositories.pg_client_credentials import (
    ClientCredentialError,
    PgClientCredentialRepo,
)

ELEVATION_PATH = "/admin/elevations"
_MAX_BODY_BYTES = 4096


class AdminElevations:
    """One server owns one bucket; credentials are checked before reading any body."""

    def __init__(
        self,
        *,
        verifier: CredentialVerifier,
        repository: Callable[[], PgClientCredentialRepo],
        elevatable_client_ids: Collection[str],
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self._verifier = verifier
        self._repository = repository
        self._allowed = frozenset(elevatable_client_ids)
        self._clock = clock
        self._bucket = TokenBucket(0.1, 3, monotonic=monotonic)

    def _transport_refusal(
        self, request: Request, reason: str, client_id: str | None
    ) -> JSONResponse:
        status = REFUSAL_STATUSES[reason]
        emit_refusal(
            reason,
            status=status,
            client_id=client_id,
            peer=None if request.client is None else request.client.host,
            path=ELEVATION_PATH,
        )
        headers = {"WWW-Authenticate": "Bearer"} if status == 401 else None
        if status == 429:
            headers = {"Retry-After": "10"}
        return JSONResponse({"error": reason}, status_code=status, headers=headers)

    def _business_refusal(
        self, request: Request, reason: str, session_id: UUID, client_id: str
    ) -> JSONResponse:
        status = ELEVATION_REFUSAL_STATUSES[reason]
        emit_elevation_refusal(
            reason,
            status=status,
            session_id=str(session_id),
            requesting_client_id=client_id,
            peer=None if request.client is None else request.client.host,
        )
        return JSONResponse({"error": reason}, status_code=status)

    async def handle(self, request: Request) -> JSONResponse:
        """Grant only the server's locked attribution snapshot, never prompt-supplied pairs."""
        authorization = request.headers.getlist("authorization")
        token = None
        if len(authorization) == 1:
            scheme, separator, candidate = authorization[0].partition(" ")
            if (
                scheme.lower() == "bearer"
                and separator
                and candidate
                and not any(char.isspace() for char in candidate)
            ):
                token = candidate
        try:
            if token is None:
                raise CredentialRefused("missing_token")
            principal = await self._verifier.verify(token)
        except CredentialRefused as exc:
            return self._transport_refusal(request, exc.reason, None)
        client_id = principal.client_id
        if "elevate" not in principal.families:
            return self._transport_refusal(request, "family_denied", client_id)
        if not self._bucket.allow(client_id):
            return self._transport_refusal(request, "rate_limited", client_id)

        body = bytearray()
        try:
            # A stalled hook must not retain an ingress slot indefinitely.
            async with asyncio.timeout(5):
                async for chunk in request.stream():
                    if len(body) + len(chunk) > _MAX_BODY_BYTES:
                        return JSONResponse({"error": "body_too_large"}, status_code=413)
                    body.extend(chunk)
        except (TimeoutError, ClientDisconnect):
            return JSONResponse({"error": "invalid_request"}, status_code=422)
        try:
            data = json.loads(body)
            if not isinstance(data, dict) or set(data) - {"session_id", "ttl_seconds", "reason"}:
                raise ValueError
            session_id = UUID(data["session_id"])
            reason = data["reason"]
            if not isinstance(reason, str) or not reason.strip() or len(reason) > 200:
                raise ValueError
        except (ValueError, TypeError, KeyError, AttributeError, RecursionError):
            return JSONResponse({"error": "invalid_request"}, status_code=422)
        ttl = data.get("ttl_seconds", 3600)
        if type(ttl) is not int or not 0 < ttl <= 14400:
            return self._business_refusal(request, "invalid_window", session_id, client_id)

        try:
            repo = self._repository()
            async with repo.transaction() as session:
                # Match the CLI's lock order: owner, then attributed connections.
                owner = (
                    (
                        await session.execute(
                            sa.select(brain_sessions.c.status, brain_sessions.c.nature)
                            .where(brain_sessions.c.id == session_id)
                            .with_for_update(read=True)
                        )
                    )
                    .mappings()
                    .one_or_none()
                )
                if owner is None or owner["status"] != "open":
                    raise ClientCredentialError("session_not_open", "session must be open")
                if owner["nature"] not in (None, "operator"):
                    raise ClientCredentialError("session_not_operator", "operator session required")
                links = brain_session_connections
                rows = (
                    (
                        await session.execute(
                            sa.select(links.c.connection_id, links.c.client_id)
                            .where(links.c.session_id == session_id, links.c.client_id.is_not(None))
                            .with_for_update(read=True)
                        )
                    )
                    .mappings()
                    .all()
                )
                connections = [row["connection_id"] for row in rows if row["client_id"] is not None]
                if not connections:
                    raise ClientCredentialError("no_attributed_connection", "no attributed pair")
                if not any(row["client_id"] in self._allowed for row in rows):
                    raise ClientCredentialError("no_elevatable_connection", "no allowlisted pair")
                now = self._clock()
                granted = await repo.grant_elevation(
                    session_id=session_id,
                    connection_ids=connections,
                    expires_at=now + timedelta(seconds=ttl),
                    granted_by=client_id,
                    reason=reason,
                    now=now,
                    elevatable_client_ids=self._allowed,
                    via="hook",
                    requested_by_client_id=client_id,
                    session=session,
                )
        except ClientCredentialError as exc:
            code = "session_not_open" if exc.code == "unknown_session" else exc.code
            if code in ELEVATION_REFUSAL_STATUSES:
                return self._business_refusal(request, code, session_id, client_id)
            return self._transport_refusal(request, "registry_unavailable", client_id)
        except Exception:
            # Never expose SQL parameters, exception text or connection identifiers.
            return self._transport_refusal(request, "registry_unavailable", client_id)
        return JSONResponse(
            {
                "elevation_id": str(granted.id),
                "expires_at": granted.expires_at.isoformat(),
                "connection_count": len(granted.connection_ids),
                "excluded_client_ids": sorted(granted.excluded_client_ids),
                "excluded_connection_count": granted.excluded_connection_count,
            }
        )
