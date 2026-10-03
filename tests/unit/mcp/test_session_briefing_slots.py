from datetime import UTC, datetime, timedelta
from uuid import uuid4

from brain_v42.mcp.tools.session_tools import (
    _focus_margin_line,
    _format_session_briefing,
    _section_bind_hint,
    _section_bound_slot,
    _section_slots,
    _section_to_distill,
)
from brain_v42.models.brain_session import BrainSessionCheckpoint
from brain_v42.models.focus_slot import (
    FocusSlot,
    FocusSlotView,
    PreviousSlotSession,
    SlotAnchor,
    SlotBriefing,
)
from brain_v42.services.dream_run_service import KillswitchState

NOW = datetime(2026, 10, 3, 12, tzinfo=UTC)
LOT = SlotAnchor(kind="lot", target_release="0.6.4")


def _slot(**overrides) -> dict:
    values = {
        "id": uuid4(),
        "project_key": "brain-v42",
        "title": "relay work",
        "body": "b" * 3,
        "revision": 2,
        "opened_at": NOW - timedelta(days=2),
        "body_updated_at": NOW,
        "anchors": [LOT],
    }
    values.update(overrides)
    return values


def _killswitches() -> KillswitchState:
    return KillswitchState(
        last_run_date=None,
        promote_enabled=False,
        promote_dry=False,
        reorg_enabled=False,
        reorg_dry=False,
        promote_clean_dry_nights=0,
        reorg_clean_dry_nights=0,
    )


def test_the_base_margin_line_is_unchanged_and_the_slot_one_counts_to_4000() -> None:
    assert _focus_margin_line(10) == "- Focus : 10 / 10000 caractères (marge 9990)"
    assert _focus_margin_line(3, 3, cap=4000, label="Slot") == (
        "- Slot : 3 / 4000 caractères (marge 3997 ; 3 octets)"
    )


def test_open_slots_list_anchors_age_holder_and_flags() -> None:
    bound_id = uuid4()
    view = SlotBriefing(
        open_slots=[
            FocusSlotView(**_slot(), bound_session_id=bound_id),
            FocusSlotView(**_slot(title="orphan"), is_stale=True, receipt_pending=True),
        ]
    )
    section = _section_slots(view, now=NOW)
    lines = section.splitlines()
    assert lines[0] == "### Slots (2 ouverts)"
    assert "relay work [lot:0.6.4] · il y a 2j · " in lines[1] and str(bound_id)[:8] in lines[1]
    assert lines[2].endswith("· orphelin · stale · reçu en attente")


def test_a_bound_session_gets_its_slot_its_predecessor_and_its_last_checkpoint() -> None:
    previous = PreviousSlotSession(
        session_id=uuid4(),
        ended_at=NOW,
        summary="s" * 1500,
        decisions=[(uuid4(), "keep the base short")],
        last_checkpoint=BrainSessionCheckpoint(
            session_id=uuid4(), seq=4, progress="p", next_step="n", created_at=NOW
        ),
    )
    view = SlotBriefing(bound_slot=FocusSlot(**_slot()), previous=previous)
    section = _section_bound_slot(view)
    assert section.startswith("### Slot : relay work (rév. 2)\nbbb\n- Slot : 3 / 4000")
    assert str(previous.session_id) in section
    assert ("Résumé : " + "s" * 1000 + "…") in section
    assert "keep the base short" in section and "#4 p → n" in section


def test_a_bound_slot_closed_on_a_receipt_is_marked_closed_in_the_heading() -> None:
    closed = FocusSlot(**_slot(closed_at=NOW, close_reason=f"receipt:{uuid4()}"))
    section = _section_bound_slot(SlotBriefing(bound_slot=closed))
    assert section.splitlines()[0] == "### Slot : relay work (rév. 2) (fermé sur reçu)"


def test_an_unbound_session_with_open_slots_gets_the_bind_hint_only() -> None:
    open_only = SlotBriefing(open_slots=[FocusSlotView(**_slot())])
    hint = _section_bind_hint(open_only)
    assert "brain_session_bind" in hint
    assert "sur commande explicite de l'utilisateur" in hint
    bound = SlotBriefing(open_slots=[FocusSlotView(**_slot())], bound_slot=FocusSlot(**_slot()))
    assert _section_bind_hint(bound) == ""
    assert _section_bind_hint(SlotBriefing()) == ""


def test_slots_closed_on_a_receipt_are_offered_for_distillation() -> None:
    closed = FocusSlot(**_slot(closed_at=NOW, close_reason=f"receipt:{uuid4()}"))
    section = _section_to_distill(SlotBriefing(to_distill=[closed]))
    assert section.startswith("### À distiller\n- relay work")
    assert _section_to_distill(SlotBriefing()) == ""


def test_the_slot_sections_follow_the_base_focus_and_vanish_without_slots() -> None:
    plain = _format_session_briefing(None, [], [], _killswitches(), None, [], [])
    assert "### Slot" not in plain and "À distiller" not in plain
    view = SlotBriefing(open_slots=[FocusSlotView(**_slot())], bound_slot=FocusSlot(**_slot()))
    rendered = _format_session_briefing(None, [], [], _killswitches(), None, [], [], slot_view=view)
    assert (
        rendered.index("### Focus")
        < rendered.index("### Slot : ")
        < rendered.index("### Slots (1 ouverts)")
    )
