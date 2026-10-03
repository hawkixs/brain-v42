from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest

from brain_v42.mcp.business_errors import SURFACED_BUSINESS_ERRORS
from brain_v42.models.brain_session import BrainSessionError
from brain_v42.models.focus_slot import (
    MAX_SLOT_ANCHORS,
    SLOT_BODY_MAX_LENGTH,
    SLOT_CLOSE_NOTE_MAX_LENGTH,
    SLOT_STALE_AFTER,
    SLOT_TITLE_MAX_LENGTH,
    BrainSessionBindResult,
    BrainSessionRelayResult,
    FocusSlotError,
    SlotAnchor,
    slot_is_stale,
    validate_anchor_shape,
)


def test_bounds_are_the_spec_values() -> None:
    assert (SLOT_TITLE_MAX_LENGTH, SLOT_BODY_MAX_LENGTH, SLOT_CLOSE_NOTE_MAX_LENGTH) == (
        120,
        4_000,
        2_000,
    )
    assert MAX_SLOT_ANCHORS == 10
    assert SLOT_STALE_AFTER.days == 7


def test_slot_staleness_uses_body_and_bound_session_activity() -> None:
    now = datetime(2026, 10, 3, tzinfo=UTC)
    old = now - timedelta(days=8)
    assert slot_is_stale(
        is_open=True, bound=False, body_updated_at=old, last_bound_ended_at=None, now=now
    )
    assert not slot_is_stale(
        is_open=True, bound=True, body_updated_at=old, last_bound_ended_at=None, now=now
    )
    assert not slot_is_stale(
        is_open=True, bound=False, body_updated_at=old, last_bound_ended_at=now, now=now
    )


def test_error_carries_a_code_and_the_delivery_error_shape() -> None:
    error = FocusSlotError("slot_busy", "session x holds slot y")
    assert (error.code, error.message, str(error)) == (
        "slot_busy",
        "session x holds slot y",
        "slot_busy: session x holds slot y",
    )
    assert isinstance(error, BrainSessionError)
    assert any(isinstance(error, family) for family in SURFACED_BUSINESS_ERRORS)


@pytest.mark.parametrize(
    "anchor",
    [
        SlotAnchor(kind="ticket", ticket_id=uuid4()),
        SlotAnchor(kind="lot", target_release="0.6.4"),
        SlotAnchor(kind="pr", repository_id=1_234, pr_number=7),
    ],
)
def test_well_shaped_anchors_pass(anchor: SlotAnchor) -> None:
    assert validate_anchor_shape(anchor) is anchor


@pytest.mark.parametrize(
    "anchor",
    [
        SlotAnchor(kind="ticket"),
        SlotAnchor(kind="ticket", ticket_id=uuid4(), target_release="0.6.4"),
        SlotAnchor(kind="lot"),
        SlotAnchor(kind="lot", target_release="v0.6.4"),
        SlotAnchor(kind="lot", target_release="0.6"),
        SlotAnchor(kind="pr", repository_id=1),
        SlotAnchor(kind="pr", repository_id=1, pr_number=0),
        SlotAnchor(kind="pr", repository_id=1, pr_number=2, ticket_id=uuid4()),
    ],
)
def test_ill_shaped_anchors_are_anchor_invalid(anchor: SlotAnchor) -> None:
    with pytest.raises(FocusSlotError, match="^anchor_invalid: "):
        validate_anchor_shape(anchor)


def test_anchor_refs_are_stable_and_human_readable() -> None:
    ticket = uuid4()
    assert SlotAnchor(kind="ticket", ticket_id=ticket).ref == f"ticket:{ticket}"
    assert SlotAnchor(kind="lot", target_release="0.6.4").ref == "lot:0.6.4"
    assert SlotAnchor(kind="pr", repository_id=12, pr_number=3).ref == "pr:12#3"


# S12 guard: output-schema margin baseline is in the brain_session lifecycle schema test, not here.
def test_bind_and_relay_result_models_pin_their_field_sets() -> None:
    assert set(BrainSessionBindResult.model_fields) == {
        "session_id",
        "slot_id",
        "slot_revision",
        "slot_body",
    }
    assert set(BrainSessionRelayResult.model_fields) == {
        "ended_session_id",
        "session",
        "slot",
        "replayed",
        "briefing",
    }
