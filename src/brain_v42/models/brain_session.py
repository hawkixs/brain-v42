"""Domain models and errors for explicit Brain session lifecycles."""

from __future__ import annotations

from datetime import datetime, timedelta
from enum import StrEnum
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from brain_v42.models.project_key import canonicalize_project_key

MAX_CAPTURED_KNOWLEDGE_IDS = 100
SESSION_STALE_AFTER = timedelta(hours=24)
# Two distinct thresholds, deliberately side by side so nobody ever confuses
# them: SESSION_STALE_AFTER (24 h) is a DERIVED flag shown to the client, it
# changes no status; AUTO_STALE_AFTER (7 d) is the threshold at which the
# SERVER abandons. The gap measured on 2026-08-07 between the most recent ghost
# (10.6 d) and the oldest living session (0.4 d) calibrates the second.
AUTO_STALE_AFTER = timedelta(days=7)
AUTO_STALE_ABANDONMENT_REASON = "auto_stale_7d"

# THIRD threshold, and the only one that does not speak of the same clock as
# the other two: SESSION_STALE_AFTER and AUTO_STALE_AFTER read
# `last_heartbeat_at`, declared PRESENCE; this one reads `last_observed_at`, the
# OBSERVATION the server makes. It applies to `agent` tracers only (ADR §0ter.5,
# signed).
#
# THIS IS NOT A CLOSING DELAY, and announcing it as one would be false: lodged
# in the nightly sweep, 4 h is an ELIGIBILITY threshold evaluated once a night.
# A tracer that goes inactive just after a pass lives until the next one — real
# worst-case latency ≈ 28 h.
AGENT_INACTIVE_AFTER = timedelta(hours=4)

#: Per-text ceiling of a checkpoint payload (SPEC-checkpoint §2.2). Crossing it
#: RAISES; it never truncates. `parse_and_validate` forgivingly clips a `topic`
#: because there a model is producing — here the payload is a JUDGMENT, and a
#: judgment cut at 2000 characters reads as complete while it is not.
MAX_CHECKPOINT_TEXT = 2000
#: Checkpoints per SESSION, not per night (SPEC-checkpoint §2.2). Under automatic
#: opening a tracer lives at most until the sweep, so 200 judgment notes inside a
#: single session is already a signal in itself. Fail-closed past it.
MAX_CHECKPOINTS_PER_SESSION = 200


class BrainSessionStatus(StrEnum):
    """Persistent lifecycle states.

    Three are controlled explicitly by the user. ``CLOSED_INACTIVE`` is the
    exception the covenant names: an ``agent`` tracer closed by the nightly
    sweep, without a ritual (migration 046, ADR §0.4 Q15 = route (3)).
    """

    OPEN = "open"
    ENDED = "ended"
    ABANDONED = "abandoned"
    CLOSED_INACTIVE = "closed_inactive"


class BrainSessionNature(StrEnum):
    """What KIND of actor a session belongs to.

    ``NULL`` in the database means "opened before migration 046" and is
    deliberately not backfilled — those sessions stay under the 7-day sweep.
    """

    AGENT = "agent"
    OPERATOR = "operator"


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


class BrainSessionCheckpointConflictError(BrainSessionConflictError):
    """A `seq` already used by this session, with DIFFERENT content.

    `ON CONFLICT DO NOTHING` returns zero rows for an exact replay and for a
    content collision alike, and the spec refuses to let the second pass silently
    (SPEC-checkpoint §1.1, settled by PLAN §4: "the same `seq` with a different
    payload is a non-destructive conflict, explicitly rejected"). The repository
    therefore rereads the stored row and compares the triple: identical means
    `replayed`, different raises this. Since `seq` comes from the CLIENT and agent
    retries are the norm (invariant C6), this collision is not theoretical.
    """


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
    # `Literal` and not `BrainSessionNature`, and that is a measured BUDGET
    # decision, not a shortcut: the enum generates a `$defs` entry copied into
    # the four derived output schemas (10170 bytes total) where the `Literal`
    # inlines (9381). Same constraint on the client side, 789 bytes less.
    # `BrainSessionNature` remains the form the CODE uses.
    #
    # C7 requires the STATE MACHINE to move with the CHECK. `nature` is part of
    # it: the `closed_inactive` branch of 046 constrains it to `agent`. The four
    # other columns of 046 — `started_by_actor`, `last_observed_at`, `intent`,
    # `connection_id` — are in NO CHECK. Three now have a writer (auto-open, and
    # observation for `last_observed_at`); `intent` still has none. Having a
    # writer is NOT a ticket of entry here: they still stay out — FastMCP
    # derives the tools' output schema from this model, and the five columns
    # took the total from 8487 to 11292 bytes — above the 9041 saving floor that
    # `test_discovery_contract_keeps_tool_identity_inputs_and_schema_budget`
    # guarantees. Each column will join this model with the commit that uses it,
    # and will pay for its schema then, deliberately.
    nature: Literal["agent", "operator"] | None = None
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
        # Every status gets an EXPLICIT branch, and the fallthrough raises.
        # This used to end in a bare `else: self._validate_abandoned_state()`.
        # Adding `closed_inactive` under that shape would have validated it with
        # the ABANDONED rules — which demand `abandonment_reason`, the very field
        # the 046 CHECK forbids on that branch. The Pydantic rail would then
        # reject exactly what the database accepts: a C7 divergence, silent.
        if self.status is BrainSessionStatus.OPEN:
            self._validate_open_state()
        elif self.status is BrainSessionStatus.ENDED:
            self._validate_ended_state()
        elif self.status is BrainSessionStatus.ABANDONED:
            self._validate_abandoned_state()
        elif self.status is BrainSessionStatus.CLOSED_INACTIVE:
            self._validate_closed_inactive_state()
        else:  # pragma: no cover - defended against a future fifth status
            raise ValueError(f"no terminal-state rule for status {self.status!r}")
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
        # The XOR "non-empty ledger XOR reason" was REMOVED with 047, not
        # weakened: it measured "did the client DECLARE". Derived capture
        # removes the only failure mode it caught (produced-but-not-declared)
        # and would now feed its signal from the SERVER side. A control is
        # hollow as soon as the controlled object can influence its own signal;
        # this one would have become a receipt the server issues to itself.
        # Above all, keeping it made every session whose ledger the server had
        # filled IMPOSSIBLE TO CLOSE.
        #
        # What remains is the only control the server cannot satisfy on the
        # user's behalf: a reason, once given, must say something.
        reason = self.nothing_to_capture_reason
        if reason is not None and not reason.strip():
            raise ValueError("nothing_to_capture_reason must not be blank")
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

    def _validate_closed_inactive_state(self) -> None:
        """Mirror of the 046 CHECK branch — deliberately, field for field.

        The asymmetry that matters: ``captured_knowledge_ids`` carries NO
        constraint here. ``abandoned`` forces it to zero, which would make an
        agent session that did its work declare an empty ledger in its terminal
        snapshot. And ``nothing_to_capture_reason`` is FORBIDDEN rather than
        required: on the ``ended`` branch it is a human judgement about why
        nothing was captured, and a server filling it in would manufacture
        judgement (objection C9).
        """
        if self.ended_at is None:
            raise ValueError("closed_inactive session requires ended_at")
        if self.nature != BrainSessionNature.AGENT:
            raise ValueError("only an agent session can be closed_inactive")
        if (
            self.summary is not None
            or self.next_focus is not None
            or self.nothing_to_capture_reason is not None
            or self.abandonment_reason is not None
            or self.end_expected_focus_revision is not None
            or self.focus_outcome is not None
            or self.focus_at_end is not None
            or self.focus_revision_at_end is not None
        ):
            raise ValueError("closed_inactive session cannot contain ritual outcome fields")
        if self.is_stale:
            raise ValueError("terminal session cannot be stale")

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
    #: Project artifacts created DURING the session and present in NO ledger.
    #: A MEASURE, never a gate: it cannot refuse a closure. This is what
    #: replaces the XOR — inform instead of punish.
    #:
    #: Non-influenceable by construction: a session cannot lower it by doing
    #: nothing. Inaction produces no artifact, hence no orphan; the number only
    #: goes down by actually attributing. A counter one could improve by staying
    #: silent would be the retired receipt under a new name.
    unattributed_in_window: int = Field(..., ge=0)


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


class BrainSessionCheckpoint(BaseModel):
    """One append-only semantic checkpoint of a session."""

    session_id: UUID
    seq: int = Field(..., ge=1)
    progress: str = Field(..., min_length=1, max_length=MAX_CHECKPOINT_TEXT)
    next_step: str = Field(..., min_length=1, max_length=MAX_CHECKPOINT_TEXT)
    blocker: str | None = Field(default=None, min_length=1, max_length=MAX_CHECKPOINT_TEXT)
    created_at: datetime


class BrainSessionCheckpointResult(BaseModel):
    """Outcome of publishing one checkpoint (SPEC-checkpoint §2).

    `replayed` distinguishes a stored row from an exact retry absorbed by the
    unique key; `checkpoint_count` is the count AFTER this call, so a caller can
    read how close it is to the ceiling without a second round trip.
    """

    session_id: UUID
    seq: int = Field(..., ge=1)
    created_at: datetime
    replayed: bool
    checkpoint_count: int = Field(..., ge=1, le=MAX_CHECKPOINTS_PER_SESSION)


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
    """An open session the sweep selected, in DRY as in WET."""

    id: UUID
    project_key: str
    client_key: str
    last_heartbeat_at: datetime
    #: NULL means "never observed" — hence out of reach of the 4 h rule, which
    #: only takes what it has seen alive (S3, settled).
    last_observed_at: datetime | None = None
    #: The terminal state THIS row received, or would receive. Mandatory and
    #: without a default: two rules write in the same statement, and a report
    #: that confused them would make precedence unverifiable.
    outcome: BrainSessionStatus

    @field_validator("outcome")
    @classmethod
    def _outcome_is_a_sweep_outcome(cls, value: BrainSessionStatus) -> BrainSessionStatus:
        if value not in (BrainSessionStatus.ABANDONED, BrainSessionStatus.CLOSED_INACTIVE):
            raise ValueError(f"{value} is not an outcome the sweep can produce")
        return value


class BrainSessionSweepResult(BaseModel):
    """Result of one server sweep, across all projects."""

    candidates: list[BrainSessionSweepCandidate]
    dry_run: bool
    #: PRESENCE threshold (7 d), read on `last_heartbeat_at`. Always active.
    cutoff: datetime
    #: OBSERVATION threshold (4 h), read on `last_observed_at`. ``None`` means
    #: the rule is closed — not that no session reached it.
    inactive_cutoff: datetime | None = None
    # Always 0 in DRY. Redundant with len(candidates) — deliberately: a log
    # must make "17 would have been abandoned" impossible to read as "17 were
    # abandoned".
    abandoned_count: int = Field(..., ge=0)
    #: A DISTINCT counter, never mixed with `abandoned_count`. The two rules
    #: produce two different terminal states — `abandoned` carries a reason and
    #: never a ledger, `closed_inactive` carries its ledger and no reason.
    #: Adding them would erase the one distinction 046 cost a migration to
    #: create.
    closed_inactive_count: int = Field(default=0, ge=0)
