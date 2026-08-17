"""Domain models and errors for explicit Brain session lifecycles."""

from __future__ import annotations

from datetime import datetime, timedelta
from enum import StrEnum
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from brain_v42.models.project_key import canonicalize_project_key

MAX_CAPTURED_KNOWLEDGE_IDS = 100
SESSION_STALE_AFTER = timedelta(hours=24)
# Deux seuils distincts, volontairement côte à côte pour qu'on ne les confonde
# jamais : SESSION_STALE_AFTER (24 h) est un flag DÉRIVÉ affiché au client, il
# ne change aucun statut ; AUTO_STALE_AFTER (7 j) est le seuil auquel le
# SERVEUR abandonne. Le fossé mesuré le 2026-08-07 entre le fantôme le plus
# récent (10,6 j) et la vivante la plus ancienne (0,4 j) calibre le second.
AUTO_STALE_AFTER = timedelta(days=7)
AUTO_STALE_ABANDONMENT_REASON = "auto_stale_7d"


class BrainSessionStatus(StrEnum):
    """Persistent lifecycle states controlled explicitly by the user."""

    OPEN = "open"
    ENDED = "ended"
    ABANDONED = "abandoned"


class BrainSessionFocusOutcome(StrEnum):
    """Persisted outcome of the one focus update attempted while ending."""

    APPLIED = "applied"
    CONFLICT = "conflict"


class BrainSessionError(Exception):
    """Base error for explicit Brain session lifecycle operations."""


class BrainSessionInputError(BrainSessionError, ValueError):
    """Raised when a lifecycle command contains invalid input."""


class BrainSessionNotFoundError(BrainSessionError, LookupError):
    """Raised when a requested session does not exist."""


class BrainSessionStateError(BrainSessionError):
    """Raised when an operation is illegal for the current session state."""


class BrainSessionConflictError(BrainSessionError):
    """Base error for concurrency and idempotency conflicts."""


class BrainSessionClientKeyConflictError(BrainSessionConflictError):
    """Raised when a client idempotency key cannot be safely replayed."""


class BrainSessionIdentityConflictError(BrainSessionConflictError):
    """Raised when a session UUID and expected client identity do not match."""


class BrainSessionCaptureConflictError(BrainSessionConflictError):
    """Raised when knowledge is already attributed to another session."""


class BrainSessionFocusConflictError(BrainSessionConflictError):
    """Raised when the project focus revision changed concurrently."""

    def __init__(
        self,
        message: str | None = None,
        *,
        current_focus: str | None = None,
        current_revision: int | None = None,
    ) -> None:
        self.current_focus = current_focus
        self.current_revision = current_revision
        detail = message or (
            f"project focus changed concurrently; current revision is {current_revision}"
        )
        super().__init__(detail)


class BrainSessionTerminalConflictError(BrainSessionConflictError):
    """Raised when a terminal replay differs from the persisted outcome."""


class BrainSession(BaseModel):
    """Persistent state of one explicitly controlled concurrent session."""

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    project_key: str = Field(..., max_length=50)
    client_key: str = Field(..., min_length=1, max_length=128)
    status: BrainSessionStatus = BrainSessionStatus.OPEN
    started_focus: str | None = None
    started_focus_revision: int = Field(..., ge=0)
    summary: str | None = None
    next_focus: str | None = None
    captured_knowledge_ids: list[UUID] = Field(
        default_factory=list,
        max_length=MAX_CAPTURED_KNOWLEDGE_IDS,
    )
    attributed_knowledge_ids: list[UUID] = Field(
        default_factory=list,
        max_length=MAX_CAPTURED_KNOWLEDGE_IDS,
        description=(
            "Effective artifact attributions loaded from the session ledger. Unlike the "
            "terminal captured_knowledge_ids snapshot, this remains observable while the "
            "session is open or abandoned."
        ),
    )
    nothing_to_capture_reason: str | None = None
    abandonment_reason: str | None = None
    end_expected_focus_revision: int | None = Field(default=None, ge=0)
    focus_outcome: BrainSessionFocusOutcome | None = None
    focus_at_end: str | None = None
    focus_revision_at_end: int | None = Field(default=None, ge=0)
    started_at: datetime
    last_heartbeat_at: datetime | None = None
    ended_at: datetime | None = None
    updated_at: datetime
    is_stale: bool = False

    @field_validator("project_key")
    @classmethod
    def _canonicalize_project_key(cls, value: str) -> str:
        return canonicalize_project_key(value)

    @field_validator("client_key")
    @classmethod
    def _normalize_client_key(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("client_key must not be blank")
        return normalized

    @model_validator(mode="after")
    def _validate_terminal_state(self) -> BrainSession:
        if self.last_heartbeat_at is None:
            self.last_heartbeat_at = self.updated_at
        if not self.attributed_knowledge_ids and self.captured_knowledge_ids:
            self.attributed_knowledge_ids = list(self.captured_knowledge_ids)
        if self.status is BrainSessionStatus.OPEN:
            self._validate_open_state()
        elif self.status is BrainSessionStatus.ENDED:
            self._validate_ended_state()
        else:
            self._validate_abandoned_state()
        return self

    def _validate_open_state(self) -> None:
        terminal_values = (
            self.summary,
            self.next_focus,
            self.nothing_to_capture_reason,
            self.abandonment_reason,
            self.end_expected_focus_revision,
            self.focus_outcome,
            self.focus_at_end,
            self.focus_revision_at_end,
            self.ended_at,
        )
        if any(value is not None for value in terminal_values) or self.captured_knowledge_ids:
            raise ValueError("open session cannot contain terminal outcome fields")

    def _validate_ended_state(self) -> None:
        if self.ended_at is None:
            raise ValueError("ended session requires ended_at")
        if not self.summary or not self.summary.strip():
            raise ValueError("ended session requires summary")
        if not self.next_focus or not self.next_focus.strip():
            raise ValueError("ended session requires next_focus")
        if self.abandonment_reason is not None:
            raise ValueError("ended session cannot contain abandonment_reason")
        if self.focus_outcome is None:
            raise ValueError("ended session requires focus_outcome")
        if self.is_stale:
            raise ValueError("terminal session cannot be stale")
        has_capture = bool(self.captured_knowledge_ids)
        reason = self.nothing_to_capture_reason
        has_reason = reason is not None
        if reason is not None and not reason.strip():
            raise ValueError("nothing_to_capture_reason must not be blank")
        if has_capture == has_reason:
            raise ValueError("ended session requires exactly one capture outcome")
        if set(self.attributed_knowledge_ids) != set(self.captured_knowledge_ids):
            raise ValueError("ended session attribution must match its capture snapshot")
        if self.end_expected_focus_revision is None:
            if (
                self.focus_outcome is not BrainSessionFocusOutcome.APPLIED
                or self.focus_at_end != self.next_focus
                or self.focus_revision_at_end is not None
            ):
                raise ValueError("legacy ended session has an invalid focus snapshot")
            return
        if self.focus_revision_at_end is None:
            raise ValueError("v4 ended session requires a final focus revision")
        if self.focus_outcome is BrainSessionFocusOutcome.APPLIED:
            if (
                self.focus_at_end != self.next_focus
                or self.focus_revision_at_end != self.end_expected_focus_revision + 1
            ):
                raise ValueError("applied focus snapshot is inconsistent")
        elif self.focus_revision_at_end == self.end_expected_focus_revision:
            raise ValueError("conflicted focus snapshot must expose a different revision")

    def _validate_abandoned_state(self) -> None:
        if self.ended_at is None:
            raise ValueError("abandoned session requires ended_at")
        if not self.abandonment_reason or not self.abandonment_reason.strip():
            raise ValueError("abandoned session requires abandonment_reason")
        if (
            self.summary is not None
            or self.next_focus is not None
            or self.nothing_to_capture_reason is not None
            or self.captured_knowledge_ids
            or self.end_expected_focus_revision is not None
            or self.focus_outcome is not None
            or self.focus_at_end is not None
            or self.focus_revision_at_end is not None
        ):
            raise ValueError("abandoned session cannot contain end outcome fields")
        if self.is_stale:
            raise ValueError("terminal session cannot be stale")


class BrainSessionStartResult(BaseModel):
    """Outcome of an idempotent session start."""

    session: BrainSession
    replayed: bool
    open_session_count: int = Field(..., ge=0)
    briefing: str = ""


class BrainSessionResumeResult(BaseModel):
    """Current context returned when attaching to an open session."""

    session: BrainSession
    open_session_count: int = Field(..., ge=0)
    current_focus: str | None
    current_focus_revision: int = Field(..., ge=0)
    briefing: str = ""


class BrainSessionEndResult(BaseModel):
    """Outcome of ending a session and conditionally updating shared focus."""

    session: BrainSession
    replayed: bool
    remaining_open_session_count: int = Field(..., ge=0)
    current_focus: str | None
    current_focus_revision: int = Field(..., ge=0)
    focus_outcome: BrainSessionFocusOutcome
    focus_at_end: str | None
    focus_revision_at_end: int | None = Field(default=None, ge=0)


class BrainSessionCaptureResult(BaseModel):
    """Persistent artifact attributions recorded for one session."""

    session: BrainSession
    captured_knowledge_ids: list[UUID] = Field(
        default_factory=list,
        max_length=MAX_CAPTURED_KNOWLEDGE_IDS,
    )
    newly_captured_knowledge_ids: list[UUID] = Field(default_factory=list)
    replayed_knowledge_ids: list[UUID] = Field(default_factory=list)
    replayed: bool


class BrainSessionHeartbeatResult(BaseModel):
    """Fresh presence marker for one still-open session."""

    session: BrainSession


class BrainSessionAbandonResult(BaseModel):
    """Outcome of explicitly discarding a session."""

    session: BrainSession
    replayed: bool
    remaining_open_session_count: int = Field(..., ge=0)


class BrainSessionListResult(BaseModel):
    """Paginated session listing."""

    sessions: list[BrainSession]
    total: int = Field(..., ge=0)
    limit: int = Field(..., ge=1)
    offset: int = Field(..., ge=0)


class BrainSessionSweepCandidate(BaseModel):
    """Une session ouverte retenue par le balayage, en DRY comme en WET."""

    id: UUID
    project_key: str
    client_key: str
    last_heartbeat_at: datetime


class BrainSessionSweepResult(BaseModel):
    """Résultat d'un balayage serveur, tous projets confondus."""

    candidates: list[BrainSessionSweepCandidate]
    dry_run: bool
    cutoff: datetime
    # Toujours 0 en DRY. Redondant avec len(candidates) — délibérément : un
    # journal doit rendre « 17 auraient été abandonnées » illisible comme
    # « 17 ont été abandonnées ».
    abandoned_count: int = Field(..., ge=0)
