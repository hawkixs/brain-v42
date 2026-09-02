"""Application service for explicit Brain session lifecycle."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Protocol
from uuid import UUID

import structlog

from brain_v42.config import get_settings
from brain_v42.models.brain_session import (
    MAX_CAPTURED_KNOWLEDGE_IDS,
    BrainSessionAbandonResult,
    BrainSessionCaptureConflictError,
    BrainSessionCaptureResult,
    BrainSessionClientKeyConflictError,
    BrainSessionConflictError,
    BrainSessionEndResult,
    BrainSessionError,
    BrainSessionFocusConflictError,
    BrainSessionHeartbeatResult,
    BrainSessionIdentityConflictError,
    BrainSessionInputError,
    BrainSessionListResult,
    BrainSessionNotFoundError,
    BrainSessionResumeResult,
    BrainSessionStartResult,
    BrainSessionStateError,
    BrainSessionStatus,
    BrainSessionTerminalConflictError,
)
from brain_v42.models.project_key import canonicalize_project_key

__all__ = [
    "BrainSessionClientKeyConflictError",
    "BrainSessionCaptureConflictError",
    "BrainSessionConflictError",
    "BrainSessionError",
    "BrainSessionFocusConflictError",
    "BrainSessionIdentityConflictError",
    "BrainSessionInputError",
    "BrainSessionNotFoundError",
    "BrainSessionService",
    "BrainSessionStateError",
    "BrainSessionTerminalConflictError",
]


logger = structlog.get_logger(__name__)


class BrainSessionRepository(Protocol):
    """Persistence contract required by the lifecycle service."""

    async def start(self, project_key: str, client_key: str) -> BrainSessionStartResult: ...

    async def resume(
        self, session_id: UUID, expected_client_key: str
    ) -> BrainSessionResumeResult: ...

    async def capture(
        self,
        session_id: UUID,
        expected_client_key: str,
        knowledge_ids: list[UUID],
    ) -> BrainSessionCaptureResult: ...

    async def heartbeat(
        self, session_id: UUID, expected_client_key: str
    ) -> BrainSessionHeartbeatResult: ...

    async def end(
        self,
        session_id: UUID,
        expected_client_key: str,
        summary: str,
        next_focus: str,
        expected_focus_revision: int,
        *,
        nothing_to_capture_reason: str | None,
    ) -> BrainSessionEndResult: ...

    async def list(
        self,
        *,
        project_key: str | None,
        status: str,
        limit: int,
        offset: int,
    ) -> BrainSessionListResult: ...

    async def abandon(
        self, session_id: UUID, expected_client_key: str, reason: str
    ) -> BrainSessionAbandonResult: ...

    async def absorb_derived_capture(
        self, session_id: UUID, connection_id: str, expected_client_key: str
    ) -> int: ...

    async def attributed_knowledge_ids(self, session_id: UUID) -> Sequence[UUID]: ...


class BrainSessionService:
    """Validate explicit lifecycle commands before persistence."""

    def __init__(self, repo: BrainSessionRepository) -> None:
        self.repo = repo

    def _absorption_connection(self) -> str | None:
        """The connection to absorb, or ``None`` — decided BEFORE touching the repo.

        The flag and the connection are read here so that a closed flag costs
        ZERO round trips, not one round trip that does nothing. A capability
        shipped closed that still paid its price on every command would be a
        regression nobody would see.

        ``None`` without a connection: stdio and stateless mode have no
        (project, connection) key. That is not a degraded case to compensate
        for, it is the auto-open contract.
        """
        try:
            if not get_settings().brain_session_derived_capture_enabled:
                return None
        except Exception:
            return None

        from brain_v42.provenance import get_current_transport  # noqa: PLC0415

        return (get_current_transport() or "").strip() or None

    async def _absorb_derived(self, session_id: UUID, expected_client_key: str) -> None:
        """Absorb what this connection's tracer collected.

        Does not raise for its own refusals: absorption accompanies an explicit
        command, it does not replace it and must not be able to make it fail.
        **ONE exception passes through: the inconsistent identity pair**, and
        that is not a refusal to absorb — it is a mistargeted command, which the
        repository will refuse just afterwards anyway, with the same error.
        Letting it surface from here is what guarantees no mutation precedes it.
        """
        connection_id = self._absorption_connection()
        if connection_id is None:
            # THIRD indistinguishable `0`, now named. Without this line,
            # "derived capture is closed" and "this transport has no
            # identifier" read the same in the log — that is, not at all, since
            # neither wrote anything.
            logger.debug(
                "session_derived_capture.absorption_skipped",
                session_id=str(session_id),
                reason=self._absorption_skip_reason(),
            )
            return
        await self.repo.absorb_derived_capture(session_id, connection_id, expected_client_key)

    def _absorption_skip_reason(self) -> str:
        """Pourquoi aucune absorption n'a été tentée — drapeau, ou transport."""
        try:
            if not get_settings().brain_session_derived_capture_enabled:
                return "disabled"
        except Exception:
            return "settings_unavailable"
        return "no_connection"

    async def start(self, project_key: str, client_key: str) -> BrainSessionStartResult:
        """Start or idempotently replay a concurrent session."""
        try:
            canonical_project = canonicalize_project_key(project_key)
        except (TypeError, ValueError) as exc:
            raise BrainSessionInputError(f"invalid project_key: {exc}") from exc
        normalized_client_key = _normalize_required(
            client_key, field_name="client_key", max_length=128
        )
        started = await self.repo.start(canonical_project, normalized_client_key)
        # BOTH branches, fresh and replay. The fresh one almost never absorbs
        # anything — ``started_at`` was just set, the window is empty — and
        # wiring the fresh one alone would look done while serving nothing.
        #
        # `start` is the ONLY one of the five that cannot absorb first: its
        # target does not exist until it materializes it. The other four receive
        # a `session_id`; this one RESOLVES it. Demanding an "absorb first"
        # order from it would force a wrong design.
        #
        # It therefore keeps the same PROMISE from the other end: absorb, then
        # REHYDRATE what it is about to return. Without that re-read, the replay
        # branch would return the ledger from BEFORE its own move — exactly the
        # one-call lag measured on `heartbeat` on 2026-08-25.
        #
        # The guard stays resolved before touching the result: a closed flag
        # must impose no result shape on callers who asked for nothing, nor cost
        # a single round trip.
        connection_id = self._absorption_connection()
        if connection_id is None:
            return started
        await self.repo.absorb_derived_capture(
            started.session.id, connection_id, normalized_client_key
        )
        attributed = await self.repo.attributed_knowledge_ids(started.session.id)
        return started.model_copy(
            update={
                "session": started.session.model_copy(
                    update={"attributed_knowledge_ids": list(attributed)}
                )
            }
        )

    async def resume(self, session_id: UUID, expected_client_key: str) -> BrainSessionResumeResult:
        """Attach to an existing open session.

        Reading does not mutate the focus, nor the session's DECLARED state —
        status, summary and next_focus are untouched. It does absorb the tracer
        ledger of this connection, which moves artifact ownership onto this
        session. That is provenance catching up with what already happened, not
        a change of what the session says about itself.
        """
        identity = _normalize_expected_client_key(expected_client_key)
        await self._absorb_derived(session_id, identity)
        return await self.repo.resume(session_id, identity)

    async def capture(
        self,
        session_id: UUID,
        expected_client_key: str,
        knowledge_ids: Sequence[UUID],
    ) -> BrainSessionCaptureResult:
        """Persist explicit artifact provenance for an open session."""
        identity = _normalize_expected_client_key(expected_client_key)
        captured = _normalize_captured_ids(knowledge_ids, require_nonempty=True)
        assert captured is not None
        await self._absorb_derived(session_id, identity)
        return await self.repo.capture(session_id, identity, captured)

    async def heartbeat(
        self, session_id: UUID, expected_client_key: str
    ) -> BrainSessionHeartbeatResult:
        """Refresh presence for an open session without changing its state."""
        identity = _normalize_expected_client_key(expected_client_key)
        await self._absorb_derived(session_id, identity)
        return await self.repo.heartbeat(session_id, identity)

    async def end(
        self,
        session_id: UUID,
        expected_client_key: str,
        summary: str,
        next_focus: str,
        expected_focus_revision: int,
        *,
        nothing_to_capture_reason: str | None = None,
    ) -> BrainSessionEndResult:
        """Validate and atomically persist a fail-closed session end."""
        normalized_summary = _normalize_required(summary, field_name="summary")
        normalized_focus = _normalize_required(next_focus, field_name="next_focus")
        identity = _normalize_expected_client_key(expected_client_key)
        _validate_revision(expected_focus_revision)
        reason = _normalize_capture_reason(nothing_to_capture_reason)
        # BEFORE the closure, and the order is the point: ``end`` reads the
        # ledger to decide how to close. Absorbing afterwards would make it
        # visible too late — the session would be closed having concluded it
        # produced nothing.
        await self._absorb_derived(session_id, identity)
        return await self.repo.end(
            session_id,
            identity,
            normalized_summary,
            normalized_focus,
            expected_focus_revision,
            nothing_to_capture_reason=reason,
        )

    async def list(
        self,
        project_key: str | None = None,
        status: str = "open",
        limit: int = 20,
        offset: int = 0,
    ) -> BrainSessionListResult:
        """List sessions, defaulting to currently open sessions."""
        canonical_project = canonicalize_project_key(project_key, strict=False)
        normalized_status = _normalize_status(status)
        if limit < 1 or limit > 100:
            raise BrainSessionInputError("limit must be between 1 and 100")
        if offset < 0:
            raise BrainSessionInputError("offset must be non-negative")
        return await self.repo.list(
            project_key=canonical_project,
            status=normalized_status,
            limit=limit,
            offset=offset,
        )

    async def abandon(
        self,
        session_id: UUID,
        expected_client_key: str,
        reason: str,
    ) -> BrainSessionAbandonResult:
        """Explicitly discard an open session without updating project focus."""
        identity = _normalize_expected_client_key(expected_client_key)
        normalized_reason = _normalize_required(reason, field_name="reason")
        return await self.repo.abandon(session_id, identity, normalized_reason)


def _normalize_required(value: str, *, field_name: str, max_length: int | None = None) -> str:
    if not isinstance(value, str):
        raise BrainSessionInputError(f"{field_name} must be a string")
    normalized = value.strip()
    if not normalized:
        raise BrainSessionInputError(f"{field_name} must not be blank")
    if max_length is not None and len(normalized) > max_length:
        raise BrainSessionInputError(f"{field_name} must contain at most {max_length} characters")
    return normalized


def _validate_revision(expected_focus_revision: int) -> None:
    if (
        isinstance(expected_focus_revision, bool)
        or not isinstance(expected_focus_revision, int)
        or expected_focus_revision < 0
    ):
        raise BrainSessionInputError("expected_focus_revision must be a non-negative integer")


def _normalize_captured_ids(
    captured_knowledge_ids: Sequence[UUID] | None,
    *,
    require_nonempty: bool = False,
) -> list[UUID] | None:
    if captured_knowledge_ids is None:
        return None
    captured = list(captured_knowledge_ids)
    if require_nonempty and not captured:
        raise BrainSessionInputError("knowledge_ids must contain at least one UUID")
    if any(not isinstance(value, UUID) for value in captured):
        raise BrainSessionInputError("captured_knowledge_ids must contain UUIDs")
    if len(captured) > MAX_CAPTURED_KNOWLEDGE_IDS:
        raise BrainSessionInputError(
            f"captured_knowledge_ids must contain at most {MAX_CAPTURED_KNOWLEDGE_IDS} items"
        )
    if len(captured) != len(set(captured)):
        raise BrainSessionInputError("captured_knowledge_ids must not contain duplicate UUIDs")
    return sorted(captured, key=str)


def _normalize_expected_client_key(value: str) -> str:
    return _normalize_required(
        value,
        field_name="expected_client_key",
        max_length=128,
    )


def _normalize_capture_reason(reason: str | None) -> str | None:
    if reason is None:
        return None
    return _normalize_required(reason, field_name="nothing_to_capture_reason")


def _normalize_status(status: str) -> str:
    if status in {"all", "stale"}:
        return status
    try:
        return BrainSessionStatus(status).value
    except (TypeError, ValueError) as exc:
        allowed = ", ".join(item.value for item in BrainSessionStatus)
        raise BrainSessionInputError(f"status must be one of: {allowed}") from exc
