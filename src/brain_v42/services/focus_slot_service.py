"""Validate slot commands before persistence (ADR #34, spec §3).

`brain_slot_open` and `brain_slot_close` are explicit user commands; nothing
here is called by a hook, a sweep or a timer.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Protocol
from uuid import UUID

from brain_v42.models.brain_session import BrainSessionInputError
from brain_v42.models.focus_slot import (
    MAX_SLOT_ANCHORS,
    SLOT_BODY_MAX_LENGTH,
    SLOT_CLOSE_NOTE_MAX_LENGTH,
    SLOT_TITLE_MAX_LENGTH,
    FocusSlotCloseResult,
    FocusSlotError,
    FocusSlotListResult,
    FocusSlotOpenResult,
    SlotAnchor,
    SlotBriefing,
    SlotStatusFilter,
    validate_anchor_shape,
)
from brain_v42.models.project_key import canonicalize_project_key


class FocusSlotRepository(Protocol):
    async def open(
        self, project_key: str, title: str, body: str, anchors: Sequence[SlotAnchor]
    ) -> FocusSlotOpenResult: ...

    async def list(
        self, project_key: str, status: SlotStatusFilter, *, limit: int, offset: int
    ) -> FocusSlotListResult: ...

    async def close(
        self, slot_id: UUID, expected_revision: int, note: str
    ) -> FocusSlotCloseResult: ...

    async def briefing_view(self, project_key: str, session_id: UUID) -> SlotBriefing: ...


class FocusSlotService:
    def __init__(self, repo: FocusSlotRepository) -> None:
        self.repo = repo

    async def open(
        self, project_key: str, title: str, body: str, anchors: Sequence[SlotAnchor]
    ) -> FocusSlotOpenResult:
        canonical = _project(project_key)
        normalized_title = _text(title, "title", SLOT_TITLE_MAX_LENGTH)
        normalized_body = slot_body(body, field_name="body")
        if not anchors:
            raise FocusSlotError(
                "anchor_required", "a slot needs at least one anchor: a ticket, a lot or a PR"
            )
        if len(anchors) > MAX_SLOT_ANCHORS:
            raise FocusSlotError(
                "anchor_invalid", f"a slot carries at most {MAX_SLOT_ANCHORS} anchors"
            )
        validated = [validate_anchor_shape(anchor) for anchor in anchors]
        if len({anchor.key() for anchor in validated}) != len(validated):
            raise FocusSlotError("anchor_invalid", "duplicate anchor in the anchor list")
        return await self.repo.open(canonical, normalized_title, normalized_body, validated)

    async def list(
        self,
        project_key: str,
        status: SlotStatusFilter = "open",
        limit: int = 20,
        offset: int = 0,
    ) -> FocusSlotListResult:
        if status not in ("open", "closed", "all"):
            raise BrainSessionInputError("status must be one of: open, closed, all")
        if not 1 <= limit <= 100 or offset < 0:
            raise FocusSlotError(
                "invalid_limit", "limit must be between 1 and 100 and offset non-negative"
            )
        return await self.repo.list(_project(project_key), status, limit=limit, offset=offset)

    async def close(self, slot_id: UUID, expected_revision: int, note: str) -> FocusSlotCloseResult:
        if (
            isinstance(expected_revision, bool)
            or not isinstance(expected_revision, int)
            or expected_revision < 0
        ):
            raise BrainSessionInputError("expected_revision must be a non-negative integer")
        normalized = _text(note, "note", SLOT_CLOSE_NOTE_MAX_LENGTH)
        return await self.repo.close(slot_id, expected_revision, normalized)

    async def briefing(self, project_key: str, session_id: UUID) -> SlotBriefing:
        return await self.repo.briefing_view(_project(project_key), session_id)


def slot_body(value: str, *, field_name: str) -> str:
    """Trim, then refuse past 4,000 CHARACTERS with `slot_body_too_long` — never truncate."""
    if not isinstance(value, str) or not value.strip():
        raise BrainSessionInputError(f"{field_name} must not be blank")
    normalized = value.strip()
    if len(normalized) > SLOT_BODY_MAX_LENGTH:
        raise FocusSlotError(
            "slot_body_too_long",
            f"{field_name} has {len(normalized)} characters; a slot body holds at most "
            f"{SLOT_BODY_MAX_LENGTH}",
        )
    return normalized


def _text(value: str, field_name: str, max_length: int) -> str:
    if not isinstance(value, str) or not value.strip():
        raise BrainSessionInputError(f"{field_name} must not be blank")
    normalized = value.strip()
    if len(normalized) > max_length:
        raise BrainSessionInputError(f"{field_name} must contain at most {max_length} characters")
    return normalized


def _project(project_key: str) -> str:
    try:
        return canonicalize_project_key(project_key)
    except (TypeError, ValueError) as exc:
        raise BrainSessionInputError(f"invalid project_key: {exc}") from exc
