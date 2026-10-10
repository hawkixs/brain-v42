"""Verify against a local registry snapshot without exposing bearer material."""

from __future__ import annotations

import asyncio
import hmac
from collections import OrderedDict
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from hashlib import sha256
from threading import Lock
from typing import TYPE_CHECKING, Protocol, runtime_checkable
from uuid import UUID

import structlog

from brain_v42.credentials.reasons import REFUSAL_STATUSES

if TYPE_CHECKING:
    from brain_v42.repositories.pg_client_credentials import CredentialRow, Disposition

_last_lookup_at: float | None = None
_lookup_lock = Lock()
_logger = structlog.get_logger(__name__)


def _reserve_lookup(now: float) -> bool:
    """Reserve before awaiting SQL so concurrent misses share the process budget."""
    global _last_lookup_at
    with _lookup_lock:
        if _last_lookup_at is not None and now - _last_lookup_at < 5.0:
            return False
        _last_lookup_at = now
        return True


class CredentialLookupRepository(Protocol):
    """Token-only readers also serve the telemetry sidecar, without actor resolution."""

    async def active_rows(self, now: datetime) -> Sequence[CredentialRow]: ...

    async def disposition_by_digest(
        self,
        token_sha256: bytes,
        now: datetime,
    ) -> tuple[str | None, Disposition]: ...


@runtime_checkable
class CredentialRepository(CredentialLookupRepository, Protocol):
    async def project_keys(self) -> Sequence[str]: ...


@dataclass(frozen=True, slots=True)
class VerifiedPrincipal:
    """Carry immutable authorisation data, never the token or its digest."""

    client_id: str
    families: frozenset[str]
    issuers: frozenset[str]
    credential_id: UUID
    expires_at: datetime | None


class CredentialRefused(Exception):
    """Expose a stable refusal reason; callers supply context to emit_refusal."""

    def __init__(self, reason: str) -> None:
        if reason not in REFUSAL_STATUSES:
            raise ValueError("unknown refusal reason")
        super().__init__(reason)
        self.reason = reason


@dataclass(frozen=True, slots=True)
class _SnapshotEntry:
    digest: bytes = field(repr=False)
    principal: VerifiedPrincipal


class CredentialVerifier:
    """Serve locally while bounding database misses and tolerance of a stale registry.

    Callers await an initial refresh and own the refresh-loop task. A failed refresh
    returns False without replacing the snapshot. Refusals carry only a reason: the
    transport supplies request context to emit_refusal exactly once. All verifier
    instances share the lookup budget and must use the same monotonic time domain.
    """

    def __init__(
        self,
        repository: CredentialLookupRepository,
        *,
        clock: Callable[[], datetime],
        monotonic: Callable[[], float],
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self._repository = repository
        self._clock = clock
        self._monotonic = monotonic
        self._sleep = sleep
        self._snapshot: tuple[_SnapshotEntry, ...] = ()
        self._projects: frozenset[str] | None = None
        self._last_success: float | None = None
        self._dispositions: OrderedDict[bytes, str] = OrderedDict()
        self._refresh_lock = asyncio.Lock()

    async def refresh(self) -> bool:
        """Publish only complete successful loads; never expose database error text."""
        async with self._refresh_lock:
            loaded_at = self._monotonic()
            try:
                rows = await self._repository.active_rows(self._clock())
                projects = (
                    frozenset(await self._repository.project_keys())
                    if isinstance(self._repository, CredentialRepository)
                    else None
                )
                snapshot = tuple(
                    _SnapshotEntry(
                        row.token_sha256,
                        VerifiedPrincipal(
                            row.client_id,
                            frozenset(row.families),
                            frozenset(row.issuers),
                            row.id,
                            row.expires_at,
                        ),
                    )
                    for row in rows
                )
            except Exception as exc:
                _logger.warning("credentials.registry_refresh_failed", error=type(exc).__name__)
                return False
            self._snapshot = snapshot
            self._projects = projects
            # A renewed row may expire or be revoked again with a different reason.
            for entry in snapshot:
                self._dispositions.pop(entry.digest, None)
            self._last_success = loaded_at
            return True

    def project_exists(self, key: str) -> bool:
        """Read the same bounded-staleness registry; absent project readers fail closed."""
        self._require_fresh_snapshot()
        if self._projects is None:
            raise CredentialRefused("registry_unavailable")
        return key in self._projects

    async def notify(self) -> bool:
        """Reload after NOTIFY without depending on its payload or delivery history."""
        return await self.refresh()

    async def run_listener_reconnected(self) -> bool:
        """Catch changes made while the LISTEN connection was unavailable."""
        return await self.refresh()

    async def run_refresh_loop(self) -> None:
        """Cover lost notifications and allow cancellation to stop the owning task."""
        while True:
            await self.refresh()
            await self._sleep(30.0)

    async def verify(self, token: str | None) -> VerifiedPrincipal:
        self._require_fresh_snapshot()
        if not token:
            raise CredentialRefused("missing_token")
        if len(token) > 4096:
            raise CredentialRefused("unknown_token")
        try:
            encoded = token.encode()
        except UnicodeEncodeError:
            raise CredentialRefused("unknown_token") from None
        digest = sha256(encoded).digest()
        principal = self._match(digest)
        if principal is not None:
            return principal
        cached = self._dispositions.get(digest)
        if cached is not None:
            self._dispositions.move_to_end(digest)
            raise CredentialRefused(cached)
        if not _reserve_lookup(self._monotonic()):
            raise CredentialRefused("unknown_token")
        try:
            _, disposition = await self._repository.disposition_by_digest(digest, self._clock())
        except Exception:
            raise CredentialRefused("registry_unavailable") from None
        self._require_fresh_snapshot()
        if disposition in {"revoked", "expired"}:
            reason = disposition + "_token"
            self._dispositions[digest] = reason
            if len(self._dispositions) > 1024:
                self._dispositions.popitem(last=False)
            raise CredentialRefused(reason)
        if disposition == "active":
            if not await self.refresh():
                raise CredentialRefused("registry_unavailable")
            principal = self._match(digest)
            if principal is not None:
                return principal
        raise CredentialRefused("unknown_token")

    def _match(self, digest: bytes) -> VerifiedPrincipal | None:
        for entry in self._snapshot:
            if hmac.compare_digest(digest, entry.digest):
                principal = entry.principal
                if principal.expires_at is not None and principal.expires_at <= self._clock():
                    raise CredentialRefused("expired_token")
                return principal
        return None

    def _require_fresh_snapshot(self) -> None:
        if self._last_success is None or self._monotonic() - self._last_success > 90.0:
            raise CredentialRefused("registry_unavailable")
