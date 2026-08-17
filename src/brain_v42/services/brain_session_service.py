"""Application service for explicit Brain session lifecycle."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Protocol
from uuid import UUID

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


class BrainSessionService:
    """Validate explicit lifecycle commands before persistence."""

    def __init__(self, repo: BrainSessionRepository) -> None:
        self.repo = repo

    async def start(self, project_key: str, client_key: str) -> BrainSessionStartResult:
        """Start or idempotently replay a concurrent session."""
        try:
            canonical_project = canonicalize_project_key(project_key)
        except (TypeError, ValueError) as exc:
            raise BrainSessionInputError(f"invalid project_key: {exc}") from exc
        normalized_client_key = _normalize_required(
            client_key, field_name="client_key", max_length=128
        )
        return await self.repo.start(canonical_project, normalized_client_key)

    async def resume(self, session_id: UUID, expected_client_key: str) -> BrainSessionResumeResult:
        """Attach to an existing open session."""
        identity = _normalize_expected_client_key(expected_client_key)
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
        return await self.repo.capture(session_id, identity, captured)

    async def heartbeat(
        self, session_id: UUID, expected_client_key: str
    ) -> BrainSessionHeartbeatResult:
        """Refresh presence for an open session without changing its state."""
        identity = _normalize_expected_client_key(expected_client_key)
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


def _validate_capture_outcome(
    captured_knowledge_ids: list[UUID] | None,
    nothing_to_capture_reason: str | None,
) -> None:
    has_captures = bool(captured_knowledge_ids)
    has_reason = nothing_to_capture_reason is not None
    if has_captures == has_reason:
        raise BrainSessionInputError(
            "provide exactly one of captured_knowledge_ids or nothing_to_capture_reason"
        )


def _normalize_status(status: str) -> str:
    if status in {"all", "stale"}:
        return status
    try:
        return BrainSessionStatus(status).value
    except (TypeError, ValueError) as exc:
        allowed = ", ".join(item.value for item in BrainSessionStatus)
        raise BrainSessionInputError(f"status must be one of: {allowed}") from exc
